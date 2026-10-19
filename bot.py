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
            try:
                m = await bot.send_message(
                    chat_id, text, reply_markup=kb, link_preview_options=lp,
                    business_connection_id=bc_id,
                )
                o["msgs"][role] = (chat_id, m.message_id, bc_id)
                try:
                    await bot.delete_business_messages(business_connection_id=bc_id, message_ids=[message_id])
                except TelegramAPIError as e:
                    log.info("can't delete old offer message: %s", e)
                continue
            except TelegramAPIError as e:
                log.warning("resend failed (%s), editing instead", e)

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
CANCEL = {"отмена", "отменить", "cancel"}
SELLER_DONE = {"передал", "передала", "передано", "отправил", "отправила"}
BUYER_DONE = {"получил", "получила", "получено", "получен"}
LIVE = {"pending", "accepted", "seller_confirmed"}


def active_offer(chat_id: int, bc_id: str) -> dict | None:
    cands = [
        o for o in OFFERS.values()
        if o["status"] in LIVE
        and o["msgs"].get("shared", (None, None, None))[0] == chat_id
        and o["msgs"]["shared"][2] == bc_id
    ]
    return max(cands, key=lambda o: o["expires"]) if cands else None


@dp.business_message(F.text)
async def biz_text(msg: Message, bot: Bot):
    bc_id = msg.business_connection_id
    if not bc_id or not msg.from_user:
        return
    owner = await business_owner(bot, bc_id)
    if owner is None:
        return
    me = await bot.me()
    text = (msg.text or "").strip()
    prefix = "@" + (me.username or "").lower()
    is_owner = msg.from_user.id == owner
    log.info("business_message chat=%s from=%s owner=%s text=%r", msg.chat.id, msg.from_user.id, is_owner, text[:60])

    # диагностика: владелец пишет /bottest в чате — бот пробует 3 вида сообщений и сообщает результат
    if is_owner and text.lower() in {"/bottest", "/тест"} and msg.chat.id != owner:
        return await business_selftest(msg, bot, bc_id, owner)

    # владелец создаёт оффер: «@бот ссылка 700 120»
    # имя бота сверяем терпимо: цифра 0, латинская o и русская о считаются одним символом
    def _norm(t: str) -> str:
        return t.lower().replace("o", "0").replace("о", "0")

    first = text.split(None, 1)[0] if text else ""
    if is_owner and first.startswith("@") and _norm(first) == _norm(prefix):
        return await business_offer(msg, text[len(first):].strip(), bot, bc_id)

    # ответы словами в чате с активным оффером
    o = active_offer(msg.chat.id, bc_id)
    if not o:
        return
    word = re.sub(r"[^\w]", "", text.lower())
    st = o["status"]
    if st == "pending":
        if o["expires"] <= time.time():
            o["status"] = "expired"
        elif word in CANCEL and is_owner:
            o["status"] = "declined"
        elif word in DECLINE and not is_owner:
            o["status"] = "declined"
        elif word in ACCEPT and not is_owner and msg.from_user.id == o["target_id"]:
            o["status"] = "processing"
            if not await reserve_payment(o):
                o["status"] = "pending"
                return
            o["status"] = "accepted"
            o["seller_id"] = msg.from_user.id
            o["seller_name"] = msg.from_user.full_name
        else:
            return
    elif st == "accepted" and word in SELLER_DONE and not is_owner and msg.from_user.id == o["seller_id"]:
        o["status"] = "seller_confirmed"
    elif st == "seller_confirmed" and word in BUYER_DONE and is_owner:
        o["status"] = "done"
        await release_payment(o)
    else:
        return
    await refresh(bot, o)


_TASKS: set = set()


async def business_selftest(msg: Message, bot: Bot, bc_id: str, owner: int) -> None:
    """Проверяет, какие сообщения Telegram разрешает слать от вашего имени (чтобы понять причину PEER_FLOOD)."""
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✓ Тест", callback_data="noop:0")]])
    variants = [
        ("1. обычный текст", "Тест 1: обычный текст", None),
        ("2. текст со ссылкой на подарок", "Тест 2: https://t.me/nft/MirageLamp-71292", None),
        ("3. текст с кнопкой", "Тест 3: с кнопкой", kb),
        ("4. спрятанная ссылка (слова вместо адреса)", 'Тест 4: <a href="https://t.me/nft/MirageLamp-71292">Mirage Lamp #71292</a>', None),
        ("5. две кнопки", "Тест 5: две кнопки", InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✓ Принять", callback_data="noop:1"),
            InlineKeyboardButton(text="✕ Отклонить", callback_data="noop:2"),
        ]])),
    ]
    # 6-7: настоящий оффер — тот же render(), те же preview и tg://user-ссылка, что и в бою
    fake = {
        "id": "selftest", "buyer_id": owner, "target_id": msg.chat.id,
        "target_name": msg.chat.full_name, "url": "https://t.me/nft/MirageLamp-71292",
        "title": "Mirage Lamp #71292", "price": 1500, "currency": "stars",
        "expires": time.time() + 3600, "status": "pending",
        "seller_id": None, "seller_name": None, "msgs": {},
    }
    t6, k6, lp6 = render(fake, "shared", bc_id)
    variants.append(("6. НАСТОЯЩИЙ оффер (как в бою: ссылка на профиль + карточка подарка + кнопки)", t6, k6, lp6))
    fake["strip_links"] = True
    t7, k7, lp7 = render(fake, "shared", bc_id)
    variants.append(("7. оффер без ссылки на профиль", t7, k7, lp7))

    results = []
    delete_error = None
    for name, t, k, *rest in variants:
        lp = rest[0] if rest else PREVIEW_OFF
        try:
            m = await bot.send_message(
                msg.chat.id, t, reply_markup=k, business_connection_id=bc_id,
                link_preview_options=lp,
            )
            results.append(f"✅ {name}")
            try:
                await bot.delete_business_messages(business_connection_id=bc_id, message_ids=[m.message_id])
            except TelegramAPIError as e:
                delete_error = f"{type(e).__name__}: {e}"
        except TelegramAPIError as e:
            results.append(f"❌ {name}: {type(e).__name__}: {e}")
            if "PEER_FLOOD" in str(e) and name.startswith("1."):
                results.append("⛔ Даже обычный текст без ссылок и кнопок блокируется — дело не в содержимом, "
                               "остальные тесты пропущены (чтобы не копить лимиты).")
                break
        await asyncio.sleep(1)
    if delete_error:
        results.append(
            "⚠️ Удалить тестовые сообщения не удалось (" + delete_error + "). "
            "Включи боту право «Удаление сообщений» в Настройки → Telegram для бизнеса → Чат-боты."
        )
    report = f"🧪 Результат теста отправки от вашего имени (чат {msg.chat.id}):\n" + "\n".join(results)
    log.info(report.replace("\n", " | "))
    try:
        await bot.delete_business_messages(business_connection_id=bc_id, message_ids=[msg.message_id])
    except TelegramAPIError:
        pass
    try:
        await bot.send_message(owner, escape(report))
    except TelegramAPIError:
        pass


async def _second_edit(bot: Bot, o: dict) -> None:
    for delay in (2.0, 5.0):  # на всякий случай два раза
        await asyncio.sleep(delay)
        text, kb, lp = render(o, "shared", o["msgs"]["shared"][2])
        try:
            await bot.edit_message_text(
                text=text + ("" if delay == 2.0 else "\u200b"),  # чередуем, чтобы правка отличалась от предыдущей
                chat_id=o["msgs"]["shared"][0], message_id=o["msgs"]["shared"][1],
                business_connection_id=o["msgs"]["shared"][2],
                reply_markup=kb, link_preview_options=lp,
            )
            log.info("second edit done (delay %s)", delay)
        except TelegramBadRequest as e:
            log.info("second edit failed: %s", e)


async def business_offer(msg: Message, args: str, bot: Bot, bc_id: str):
    """Оффер от имени владельца аккаунта (без «via @bot»)."""
    data = parse_query(args)
    if not data or msg.chat.type != "private" or msg.chat.id == msg.from_user.id:
        log.info("skip offer: parse=%s chat_type=%s self_chat=%s", bool(data), msg.chat.type, msg.chat.id == msg.from_user.id)
        return  # молча игнорируем, чтобы не светить ошибки собеседнику

    o = {
        "id": uuid.uuid4().hex[:10],
        "buyer_id": msg.from_user.id,
        "target_id": msg.chat.id,
        "target_name": msg.chat.full_name,
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
    mid, edited, err = None, False, None
    text, kb, lp = render(o, "shared", bc_id)

    # 1) отправляем НОВОЕ сообщение от вашего имени — его увидит получатель.
    #    Если Telegram отвечает PEER_FLOOD, пробуем всё более простые варианты текста.
    attempts = [
        {"strip_links": True},
        {},
        {"strip_links": True, "raw_url": True},
        {"strip_links": True, "raw_url": True, "no_preview": True},
        {"strip_links": True, "raw_url": True, "no_buttons": True},
        {"strip_links": True, "raw_url": True, "no_buttons": True, "no_preview": True},
    ]
    errs: list[str] = []
    used: dict = {}
    for flags in attempts:
        for k_ in ("strip_links", "simple_preview", "auto_preview", "no_preview", "raw_url", "no_buttons"):
            o.pop(k_, None)
        o.update(flags)
        text, kb, lp = render(o, "shared", bc_id)
        try:
            mid = await send_view(bot, msg.chat.id, (text, kb, lp), bc_id)
            log.info("offer: sent as a new business message (variant %s)", flags or "full")
            used = flags
            break
        except TelegramAPIError as e:
            err = e
            errs.append(f"{flags or 'full'}: {type(e).__name__}: {e}")
            if "PEER_FLOOD" in str(e):
                # Telegram режет ЛЮБОЕ новое сообщение в этот чат — упрощать текст бесполезно,
                # а повторные попытки только ухудшают лимиты. Сразу переходим к правке.
                log.warning("PEER_FLOOD: новые сообщения в чат %s запрещены, остальные варианты пропущены", msg.chat.id)
                break
            log.warning("business send failed (variant %s): %s: %s", flags or "full", type(e).__name__, e)

    if mid is not None and errs:
        try:
            await bot.send_message(
                msg.from_user.id,
                f"ℹ️ Оффер ушёл упрощённым вариантом {escape(str(used or 'full'))}. Ошибки предыдущих попыток:\n"
                + "\n".join(f"<code>{escape(x)}</code>" for x in errs[:5]),
            )
        except TelegramAPIError:
            pass

    # 2) запасной вариант: заменить ваше сообщение (получатель такую правку может не увидеть)
    if mid is None:
        try:
            await bot.edit_message_text(
                text=text + "\u200b",
                chat_id=msg.chat.id, message_id=msg.message_id,
                business_connection_id=bc_id, reply_markup=kb, link_preview_options=lp,
            )
            mid, edited = msg.message_id, True
            log.info("offer: edited the owner's message in place (fallback)")
            # новое сообщение отправить не удалось — сообщаем владельцу почему (получатель в этом режиме часто ничего не видит)
            try:
                await bot.send_message(
                    msg.from_user.id,
                    "⚠️ Новое сообщение с оффером Telegram не принял, поэтому я отредактировал твоё сообщение "
                    "(в этом режиме получатель может не увидеть оффер).\n"
                    f"Причина: <code>{escape(type(err).__name__ + ': ' + str(err))}</code>",
                )
            except TelegramAPIError:
                pass
        except TelegramAPIError as e:
            log.warning("edit in place failed: %s", e)

    if mid is None:
        # сообщаем владельцу причину в личку с ботом (если он когда-то нажимал /start)
        try:
            await bot.send_message(
                msg.from_user.id,
                f"⚠️ Не удалось отправить оффер в чат с {escape(msg.chat.full_name or str(msg.chat.id))}:\n"
                f"<code>{escape(str(err))}</code>",
            )
        except TelegramAPIError:
            pass
        return
    o["msgs"]["shared"] = (msg.chat.id, mid, bc_id)
    OFFERS[o["id"]] = o

    # Первая правка чужого сообщения доходит только до владельца, вторая — до получателя.
    # Поэтому через пару секунд правим ещё раз (убираем невидимый символ).
    if edited:
        t = asyncio.create_task(_second_edit(bot, o))
        _TASKS.add(t)
        t.add_done_callback(_TASKS.discard)

    # запасной канал: дублируем оффер получателю в личку с ботом (нужно, чтобы он хоть раз нажал /start)
    if ALSO_DM:
        try:
            o["msgs"]["seller"] = (msg.chat.id, await send_view(bot, msg.chat.id, view_for(o, "seller")), None)
        except TelegramAPIError as e:
            log.info("dm to recipient failed: %s", e)
            try:
                await bot.send_message(
                    msg.from_user.id,
                    "ℹ️ Получатель пока не видит кнопки в личке с ботом: ему нужно один раз нажать /start у бота.",
                )
            except TelegramAPIError:
                pass

    # убираем исходную команду из чата (нужно право на удаление сообщений), если не редактировали её
    if not edited:
        try:
            await bot.delete_business_messages(business_connection_id=bc_id, message_ids=[msg.message_id])
        except TelegramAPIError as e:
            log.warning("can't delete command message (нет права «Удаление сообщений» у бота в Telegram Business?): %s", e)


async def do_action(bot: Bot, action: str, oid: str, u: User) -> tuple[str, bool]:
    """Единая логика шагов сделки. Вызывается и из callback-кнопок, и из ссылок t.me/бот?start=...
    Возвращает (текст ответа, показывать ли как alert)."""
    o = OFFERS.get(oid)
    if not o:
        return "Оффер не найден (бот перезапускался).", True
    st = o["status"]
    if action == "acc":
        if st != "pending":
            return "Оффер уже обработан.", False
        if o["expires"] <= time.time():
            o["status"] = "expired"
            await refresh(bot, o)
            return "Срок истёк.", False
        if u.id == o["buyer_id"]:
            return "Нельзя принять собственный оффер.", True
        if o["target_id"] and u.id != o["target_id"]:
            return "Этот оффер адресован другому пользователю.", True
        o["status"] = "processing"  # защита от двойного нажатия
        if not await reserve_payment(o):
            o["status"] = "pending"
            return "Не удалось зарезервировать оплату.", True
        o["status"] = "accepted"
        o["seller_id"] = u.id
        o["seller_name"] = u.full_name
        await refresh(bot, o)
        return "Сделка принята! Передайте подарок покупателю и подтвердите передачу.", False
    if action == "dec":
        if st != "pending":
            return "Оффер уже обработан.", False
        if o["target_id"] and u.id not in (o["target_id"], o["buyer_id"]):
            return "Этот оффер адресован другому пользователю.", True
        o["status"] = "declined"
        await refresh(bot, o)
        return "Сделка отклонена.", False
    if action == "cx":
        if st != "pending":
            return "Недоступно.", True
        if u.id != o["buyer_id"]:
            return "Отменить может только автор оффера.", True
        o["status"] = "declined"
        await refresh(bot, o)
        return "Оффер отменён.", False
    if action == "sc":
        if st != "accepted":
            return "Недоступно.", True
        if u.id != o["seller_id"]:
            return "Подтвердить передачу может только продавец.", True
        o["status"] = "seller_confirmed"
        await refresh(bot, o)
        return "Ждём подтверждения покупателя.", False
    if action == "bc":
        if st != "seller_confirmed":
            return "Недоступно.", True
        if u.id != o["buyer_id"]:
            return "Подтвердить получение может только покупатель.", True
        o["status"] = "done"
        await release_payment(o)
        await refresh(bot, o)
        return "Готово!", False
    return "Неизвестное действие.", True


@dp.callback_query(F.data.regexp(r"^(acc|dec|cx|sc|bc):"))
async def on_action(cb: CallbackQuery):
    action, oid = cb.data.split(":", 1)
    log.info("callback %s from user %s (inline=%s, msg=%s)", cb.data, cb.from_user.id,
             bool(cb.inline_message_id), getattr(cb.message, "message_id", None))
    o = OFFERS.get(oid)
    if o and cb.inline_message_id and "shared" not in o["msgs"]:
        o["msgs"]["shared"] = (None, cb.inline_message_id, None)
    text, alert = "Что-то пошло не так, попробуйте ещё раз.", True
    try:
        text, alert = await do_action(cb.bot, action, oid, cb.from_user)
    except Exception:  # noqa: BLE001
        log.exception("do_action failed")
    try:
        await cb.answer(text, show_alert=alert)
    except TelegramAPIError as e:
        log.info("callback answer failed: %s", e)


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Задай переменную окружения BOT_TOKEN")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    global BOT_USERNAME
    BOT_USERNAME = (await bot.me()).username or ""
    log.info("bot username: %s, deeplink buttons: %s", BOT_USERNAME, DEEPLINK)
    await dp.start_polling(
        bot,
        allowed_updates=[
            "message", "callback_query", "inline_query",
            "business_connection", "business_message",
            "edited_business_message", "deleted_business_messages",
        ],
    )


if __name__ == "__main__":
    asyncio.run(main())
