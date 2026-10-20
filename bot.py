import logging
import os
import re
import time
import uuid
import asyncio
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BusinessConnection,
    CallbackQuery,
    InlineKeyboardButton,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    User,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("offer_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# Кнопки под оффером в бизнес-чатах. Метка «By @бот» от кнопок НЕ зависит: Telegram сам
# ставит её (только владельцу аккаунта) на любое сообщение, отправленное бизнес-ботом.
# OFFER_BUTTONS=0 — убрать кнопки, останутся только ответы словами.
BUTTONS = os.getenv("OFFER_BUTTONS", "1") == "1"
# Дублировать оффер получателю в личку с ботом (OFFER_DM=1 — включить; нужен /start у получателя).
ALSO_DM = os.getenv("OFFER_DM", "0") == "1"
# После каждого шага сделки слать в бизнес-чат новое сообщение вместо правки старого (OFFER_RESEND=0 — только правки).
RESEND = os.getenv("OFFER_RESEND", "1") == "1"

# OFFER_DEEPLINK=1 — кнопки в бизнес-чате становятся ссылками на бота (t.me/бот?start=...).
# По умолчанию выключено: кнопки «Принять/Отклонить» обычные и нажимаются прямо в чате.
DEEPLINK = os.getenv("OFFER_DEEPLINK", "0") == "1"
BOT_USERNAME = ""  # заполняется при старте

dp = Dispatcher()

# Пока всё в памяти; потом заменить на SQLite/Postgres.
OFFERS: dict[str, dict] = {}
USERS: dict[str, int] = {}  # username (lower) -> user_id, заполняется по /start и /offer
BUSINESS: dict[str, int] = {}  # business_connection_id -> user_id владельца аккаунта

# Статусы: pending -> accepted -> seller_confirmed -> done
#          \-> declined / expired


# ---------- ОПЛАТА (сюда подключаешь свою логику) ----------

async def reserve_payment(o: dict) -> bool:
    """Вызывается, когда продавец нажал «Принять».
    Здесь проверка/заморозка средств покупателя. True — деньги зарезервированы."""
    return True  # TODO


async def release_payment(o: dict) -> None:
    """Вызывается, когда покупатель подтвердил получение подарка.
    Здесь выплата продавцу."""
    return None  # TODO


# ---------- helpers ----------

HELP = (
    "👋 <b>Офер-бот</b>\n\n"
    "Чтобы кинуть оффер на подарок, в любом чате напиши:\n"
    "@{username} <b>1500</b> https://t.me/nft/MirageLamp-71292 <b>60</b>\n\n"
    "• <b>1500</b> — цена\n"
    "• ссылка — подарок\n"
    "• <b>60</b> — время действия в минутах (по умолчанию 60)\n"
    "• добавь слово <code>gram</code>, если цена в Gram (по умолчанию ★ Stars)\n\n"
    "<b>Дальше в чате словами:</b>\n"
    "• получатель: «принять» или «отклонить»\n"
    "• получатель после передачи: «передал»\n"
    "• ты после получения: «получил»\n"
    "• отменить оффер: «отмена»"
)


async def help_text(bot: Bot) -> str:
    me = await bot.me()
    return HELP.replace("{username}", me.username or "bot")


def remember(u: User | None) -> None:
    if u and u.username:
        USERS[u.username.lower()] = u.id


def gift_title(url: str) -> str:
    """https://t.me/nft/MirageLamp-71292 -> Mirage Lamp #71292"""
    slug = url.split("?")[0].rstrip("/").split("/")[-1]
    if "-" in slug:
        name, num = slug.rsplit("-", 1)
        name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
        return f"{name} #{num}"
    return slug


def money(o: dict) -> str:
    if o["currency"] == "stars":
        return f"{o['price']} ★"
    p = o["price"]
    return f"{p:,.2f}".rstrip("0").rstrip(".") + " Gram" if isinstance(p, float) else f"{p:,} Gram"


def cur_name(o: dict) -> str:
    return "Stars" if o["currency"] == "stars" else "Gram"


def user_link(uid: int, text: str) -> str:
    return f'<a href="tg://user?id={uid}">{escape(text)}</a>'


def gift_link(o: dict) -> str:
    return f'<a href="{escape(o["url"])}">{escape(o["title"])}</a>'


def time_left(o: dict) -> tuple[int, int]:
    rem = max(0, int(o["expires"] - time.time()))
    return rem // 3600, (rem % 3600) // 60


def preview_on(o: dict) -> LinkPreviewOptions:
    # карточка подарка: Telegram / Mirage Lamp #71292 + картинка + «ПОКАЗАТЬ ПОДАРОК»
    return LinkPreviewOptions(url=o["url"], prefer_large_media=True)


PREVIEW_OFF = LinkPreviewOptions(is_disabled=True)

TEXT_HINTS = {
    "pending": "✍️ Получатель, ответьте в чат: «принять» или «отклонить».",
    "accepted": "✍️ Продавец, передайте подарок и напишите «передал».",
    "seller_confirmed": "✍️ Покупатель, подтвердите получение — напишите «получил».",
}


_ACTION_RE = re.compile(r"^(acc|dec|cx|sc|bc):(\w+)$")


def deeplink_kb(kb: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    if not kb or not BOT_USERNAME:
        return kb
    rows = []
    for row in kb.inline_keyboard:
        new_row = []
        for b in row:
            m = _ACTION_RE.match(b.callback_data or "")
            if m:
                b = InlineKeyboardButton(
                    text=b.text, url=f"https://t.me/{BOT_USERNAME}?start={m.group(1)}_{m.group(2)}"
                )
            new_row.append(b)
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def render(o: dict, role: str, bc_id: str | None = None):
    """view_for + для бизнес-чатов: без кнопок, с подсказкой что писать словами."""
    text, kb, lp = view_for(o, role)
    # запасные упрощения для бизнес-отправки (включаются, если Telegram отвечает PEER_FLOOD)
    if o.get("strip_links"):
        text = re.sub(r'<a href="tg://user\?id=\d+">(.*?)</a>', r"\1", text)
    if o.get("alt_timer"):
        text = text.replace("🕒", "⏱")
    if o.get("no_timer"):
        # диагностика показала: ссылка на подарок + строка про срок в одном сообщении → PEER_FLOOD
        text = re.sub(r"\n\n🕒[^\n]*$", "", text)
    if o.get("plain_gift"):
        # без ссылки на подарок в тексте (ссылка + слова про продажу/передачу провоцируют PEER_FLOOD)
        text = re.sub(r'<a href="https?://t\.me/nft/[^"]+">(.*?)</a>', r"\1", text)
        lp = PREVIEW_OFF
    if o.get("simple_preview") and not lp.is_disabled:
        lp = LinkPreviewOptions(url=o["url"])
    if o.get("raw_url"):
        # ссылка на подарок видна целиком, а не спрятана за словами
        text = re.sub(r'<a href="(https?://t\.me/nft/[^"]+)">(.*?)</a>', r"\2\n\1", text)
        if not lp.is_disabled:
            lp = None
    if o.get("auto_preview") and not lp.is_disabled:
        lp = None  # без параметров: Telegram сам подтянет карточку по ссылке на подарок в тексте
    if o.get("no_preview"):
        lp = PREVIEW_OFF
    if kb and o.get("no_url_buttons"):
        rows = [[b for b in row if not b.url] for row in kb.inline_keyboard]
        rows = [r for r in rows if r]
        kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    if bc_id and DEEPLINK:
        kb = deeplink_kb(kb)
    if bc_id and (not BUTTONS or o.get("no_buttons")):
        kb = None
        hint = TEXT_HINTS.get(o["status"])
        if hint:
            text += "\n\n" + hint
    return text, kb, lp


def parse_query(text: str) -> dict | None:
    """Формат: <цена> <ссылка на подарок> [минуты] [stars|gram] [@username]"""
    link = None
    nums: list[int | float] = []
    currency = "stars"
    for tok in text.split():
        low = tok.lower()
        if re.match(r"https?://t\.me/nft/\S+", tok):
            link = tok
        elif low in {"gram", "грам", "грамм", "ton", "тон"}:
            currency = "gram"
        elif low in {"stars", "star", "зв", "звезд", "звезды", "⭐", "★"}:
            currency = "stars"
        elif re.fullmatch(r"\d[\d,_]*(?:\.\d+)?", tok):
            clean = re.sub(r"[,_]", "", tok)
            nums.append(float(clean) if "." in clean else int(clean))
    if not link or not nums or nums[0] <= 0:
        return None
    if currency == "stars" and nums[0] != int(nums[0]):
        return None  # у Stars не бывает дробных значений
    if currency == "stars":
        nums[0] = int(nums[0])
    minutes = int(nums[1]) if len(nums) > 1 else 60
    minutes = max(1, min(minutes, 24 * 60))
    return {"url": link, "price": nums[0], "minutes": minutes, "currency": currency}


# ---------- views: (text, keyboard, link_preview) ----------
# role: "seller" (получатель оффера), "buyer" (кто кинул оффер), "shared" (общий чат)

def pending_view(o: dict):
    h, m = time_left(o)
    text = (
        f"🎁 {user_link(o['buyer_id'], 'Пользователь')} предлагает Вам {money(o)} "
        f"за подарок {gift_link(o)}.\n\n"
        f"🕒 Предложение действует ещё {h} ч. {m} мин."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✓ Принять", callback_data=f"acc:{o['id']}"),
        InlineKeyboardButton(text="✕ Отклонить", callback_data=f"dec:{o['id']}"),
    ]])
    return text, kb, preview_on(o)


def buyer_wait_view(o: dict):
    h, m = time_left(o)
    who = user_link(o["target_id"], o["target_name"]) if o.get("target_id") else escape(o["target_name"] or "получателю")
    text = (
        f"📨 Оффер {money(o)} за {gift_link(o)} отправлен: {who}.\n\n"
        f"🕒 Действует ещё {h} ч. {m} мин."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✕ Отменить оффер", callback_data=f"cx:{o['id']}")],
    ])
    return text, kb, preview_on(o)


def accepted_view(o: dict):
    text = (
        f"🎁 Подарок: {gift_link(o)} успешно продан!\n\n"
        f"• Передайте подарок {user_link(o['buyer_id'], 'покупателю')} "
        f"для зачисления {cur_name(o)}."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        # открывает страницу подарка: там «Передать» и выбор покупателя
        [InlineKeyboardButton(text="Передать", url=o["url"])],
        [InlineKeyboardButton(text="✓ Подтвердить передачу", callback_data=f"sc:{o['id']}")],
    ])
    return text, kb, preview_on(o)


def buyer_accepted_view(o: dict):
    who = user_link(o["seller_id"], o["seller_name"])
    text = f"🎁 {who} принял оффер на {gift_link(o)}.\n\n• Ожидайте передачи подарка."
    return text, None, preview_on(o)


def seller_wait_view(o: dict):
    text = (
        f"🎁 Передача подарка {gift_link(o)} подтверждена.\n\n"
        f"• Ждём подтверждения от покупателя."
    )
    return text, None, preview_on(o)


def seller_confirmed_view(o: dict):
    text = (
        f"🎁 Подарок: {gift_link(o)} передан продавцом.\n\n"
        f"• {user_link(o['buyer_id'], 'Покупатель')}, подтвердите получение подарка."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✓ Подтвердить получение", callback_data=f"bc:{o['id']}")],
    ])
    return text, kb, preview_on(o)


def done_view(o: dict):
    text = (
        f"🎁 NFT {gift_link(o)} успешно передан!\n\n"
        f"• Средства будут зачислены в течение 30 минут."
    )
    return text, None, preview_on(o)


def declined_view(o: dict):
    return f"🎁 Сделка отклонена.\n\nПодарок: {gift_link(o)}", None, PREVIEW_OFF


def expired_view(o: dict):
    return f"🎁 Срок предложения истёк.\n\nПодарок: {gift_link(o)}", None, PREVIEW_OFF


def view_for(o: dict, role: str):
    s = o["status"]
    if s == "done":
        return done_view(o)
    if s == "declined":
        return declined_view(o)
    if s == "expired":
        return expired_view(o)
    if s == "accepted":
        return buyer_accepted_view(o) if role == "buyer" else accepted_view(o)
    if s == "seller_confirmed":
        return seller_wait_view(o) if role == "seller" else seller_confirmed_view(o)
    # pending / processing
    return buyer_wait_view(o) if role == "buyer" else pending_view(o)


async def refresh(bot: Bot, o: dict) -> None:
    """Обновляет все сообщения оффера (у получателя и у покупателя)."""
    for role, (chat_id, message_id, bc_id) in list(o["msgs"].items()):
        text, kb, lp = render(o, role, bc_id)

        # Правки сообщений, отправленных от имени владельца, получатель может не увидеть.
        # Поэтому в бизнес-чатах шлём НОВОЕ сообщение с актуальным состоянием, а старое удаляем.
        if bc_id and chat_id is not None and RESEND:
            ladder = [
                {"plain_gift": True},
                {"plain_gift": True, "no_url_buttons": True},
                {"plain_gift": True, "no_url_buttons": True, "no_buttons": True},
                {},
            ]
            errs: list[str] = []
            sent = False
            for flags in ladder:
                t_, k_, lp_ = render({**o, **flags}, role, bc_id)
                try:
                    m = await bot.send_message(
                        chat_id, t_, reply_markup=k_, link_preview_options=lp_,
                        business_connection_id=bc_id,
                    )
                except TelegramAPIError as e:
                    errs.append(f"{flags or 'full'}: {type(e).__name__}: {e}")
                    log.warning("resend failed (status %s, variant %s): %s", o["status"], flags or "full", e)
                    continue
                o["msgs"][role] = (chat_id, m.message_id, bc_id)
                log.info("resend ok (status %s, variant %s)", o["status"], flags or "full")
                try:
                    await bot.delete_business_messages(business_connection_id=bc_id, message_ids=[message_id])
                except TelegramAPIError as e:
                    log.info("can't delete old offer message: %s", e)
                sent = True
                break
            if sent:
                continue
            log.warning("resend failed in all variants, editing instead")
            try:
                await bot.send_message(
                    o["buyer_id"],
                    f"⚠️ Не смог отправить новое сообщение по шагу сделки «{escape(o['status'])}» в чат — "
                    "правлю старое (собеседник может не увидеть).\n"
                    + "\n".join(f"<code>{escape(x)}</code>" for x in errs[:4]),
                )
            except TelegramAPIError:
                pass

        try:
            if chat_id is None:  # сообщение, отправленное через inline
                await bot.edit_message_text(
                    text=text, inline_message_id=message_id,
                    reply_markup=kb, link_preview_options=lp,
                )
            else:
                await bot.edit_message_text(
                    text=text, chat_id=chat_id, message_id=message_id,
                    reply_markup=kb, link_preview_options=lp,
                    business_connection_id=bc_id,
                )
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                log.warning("edit failed (%s): %s", role, e)


async def send_view(bot: Bot, chat_id: int, view, bc_id: str | None = None) -> int:
    text, kb, lp = view
    m = await bot.send_message(
        chat_id, text, reply_markup=kb, link_preview_options=lp,
        business_connection_id=bc_id,
    )
    return m.message_id


# ---------- handlers ----------

@dp.update.outer_middleware()
async def log_updates(handler, event, data):
    try:
        kinds = [k for k in event.model_fields_set if k != "update_id"]
        log.info("update %s: %s", getattr(event, "update_id", "?"), kinds)
    except Exception:  # noqa: BLE001
        pass
    return await handler(event, data)


@dp.message(CommandStart())
async def start(msg: Message, command: CommandObject, bot: Bot):
    remember(msg.from_user)
    m = re.fullmatch(r"(acc|dec|cx|sc|bc)_(\w+)", command.args or "")
    if m and msg.from_user:
        log.info("deeplink %s from user %s", command.args, msg.from_user.id)
        text, _ = await do_action(bot, m.group(1), m.group(2), msg.from_user)
        return await msg.answer(text, link_preview_options=PREVIEW_OFF)
    await msg.answer(await help_text(bot), link_preview_options=PREVIEW_OFF)


@dp.business_connection()
async def on_business_connection(bc: BusinessConnection):
    if bc.is_enabled:
        BUSINESS[bc.id] = bc.user.id
        log.info("business connected: %s (user %s)", bc.id, bc.user.id)
    else:
        BUSINESS.pop(bc.id, None)
        log.info("business disconnected: %s", bc.id)


async def business_owner(bot: Bot, bc_id: str) -> int | None:
    if bc_id not in BUSINESS:
        try:
            bc = await bot.get_business_connection(bc_id)
            BUSINESS[bc_id] = bc.user.id
        except Exception as e:  # noqa: BLE001
            log.warning("get_business_connection failed: %s", e)
            return None
    return BUSINESS[bc_id]


@dp.message(Command("offer"))
@dp.business_message(Command("offer"))
async def cmd_offer(msg: Message, command: CommandObject, bot: Bot):
    bc_id = msg.business_connection_id  # не None, если команду написали из чата аккаунта с подключённым ботом
    if bc_id:
        # реагируем только на команды самого владельца аккаунта
        owner = await business_owner(bot, bc_id)
        if owner is None or not msg.from_user or msg.from_user.id != owner:
            return
        return await business_offer(msg, command.args or "", bot, bc_id)

    remember(msg.from_user)
    args = command.args or ""
    data = parse_query(args)
    if not data:
        return await msg.answer(await help_text(bot), link_preview_options=PREVIEW_OFF)

    # получатель: ответ на сообщение или @username
    target_id, target_name = None, None
    rp = msg.reply_to_message
    if rp and rp.from_user and not rp.from_user.is_bot:
        remember(rp.from_user)
        target_id, target_name = rp.from_user.id, rp.from_user.full_name
    else:
        m = re.search(r"@(\w{4,32})", args)
        if m:
            target_name = "@" + m.group(1)
            target_id = USERS.get(m.group(1).lower())

    if target_id == msg.from_user.id:
        return await msg.answer("Нельзя кинуть оффер самому себе.")

    private = msg.chat.type == "private"
    if private and not target_id:
        return await msg.answer(
            "Не знаю такого получателя. Укажи его @username — "
            "и пусть он сначала нажмёт /start у бота."
        )

    if len(OFFERS) > 5000:
        now = time.time()
        for k in [k for k, v in OFFERS.items() if v["expires"] < now - 86400]:
            OFFERS.pop(k, None)

    o = {
        "id": uuid.uuid4().hex[:10],
        "buyer_id": msg.from_user.id,
        "target_id": target_id,
        "target_name": target_name,
        "url": data["url"],
        "title": gift_title(data["url"]),
        "price": data["price"],
        "currency": data["currency"],
        "expires": time.time() + data["minutes"] * 60,
        "status": "pending",
        "seller_id": None,
        "seller_name": None,
        "msgs": {},
    }

    try:
        if private:
            o["msgs"]["seller"] = (target_id, await send_view(bot, target_id, view_for(o, "seller")), None)
            o["msgs"]["buyer"] = (msg.chat.id, await send_view(bot, msg.chat.id, view_for(o, "buyer")), None)
        else:
            o["msgs"]["shared"] = (msg.chat.id, await send_view(bot, msg.chat.id, view_for(o, "shared")), None)
    except TelegramAPIError as e:
        log.warning("send failed: %s", e)
        return await msg.answer("Не получилось отправить оффер: получатель должен нажать /start у бота.")

    OFFERS[o["id"]] = o


@dp.inline_query()
async def on_inline(q: InlineQuery):
    """Inline-офферов больше нет (они дают «via @бот»). Отвечаем пустым списком,
    чтобы «@бот ...» уходило обычным текстом и его подхватил business-обработчик."""
    try:
        await q.answer([], cache_time=0, is_personal=True)
    except TelegramBadRequest as e:
        log.info("inline answer failed: %s", e)


ACCEPT = {"принять", "принимаю", "приняла", "accept", "ok", "ок", "да"}
DECLINE = {"отклонить", "отклоняю", "нет", "отказ", "decline"}
CANCEL = {"отмена", "отме