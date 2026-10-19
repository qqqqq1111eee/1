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
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BusinessConnection,
    CallbackQuery,
    InlineKeyboardButton,
    InlineQuery,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    User,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("offer_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

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
    "• добавь слово <code>gram</code>, если цена в Gram (по умолчанию ★ Stars)"
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
    return f"{o['price']:,} Gram"


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


def parse_query(text: str) -> dict | None:
    """Формат: <цена> <ссылка на подарок> [минуты] [stars|gram] [@username]"""
    link = None
    nums: list[int] = []
    currency = "stars"
    for tok in text.split():
        low = tok.lower()
        if re.match(r"https?://t\.me/nft/\S+", tok):
            link = tok
        elif low in {"gram", "грам", "грамм", "ton", "тон"}:
            currency = "gram"
        elif low in {"stars", "star", "зв", "звезд", "звезды", "⭐", "★"}:
            currency = "stars"
        elif re.fullmatch(r"\d[\d,_]*", tok):
            nums.append(int(re.sub(r"[,_]", "", tok)))
    if not link or not nums or nums[0] <= 0:
        return None
    minutes = nums[1] if len(nums) > 1 else 60
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
    for role, (chat_id, message_id, bc_id) in o["msgs"].items():
        text, kb, lp = view_for(o, role)
        try:
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

@dp.message(CommandStart())
async def start(msg: Message, bot: Bot):
    remember(msg.from_user)
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
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        log.warning("send failed: %s", e)
        return await msg.answer("Не получилось отправить оффер: получатель должен нажать /start у бота.")

    OFFERS[o["id"]] = o


@dp.inline_query()
async def on_inline(q: InlineQuery):
    """Если в BotFather включён Inline Mode, клиент ждёт ответа на «@бот ...».
    Отвечаем сразу пустым списком — тогда сообщение уходит как обычный текст,
    а его подхватывает business-обработчик ниже."""
    try:
        await q.answer([], cache_time=0, is_personal=True)
    except TelegramBadRequest as e:
        log.info("inline answer failed: %s", e)


@dp.business_message(F.text)
async def biz_mention(msg: Message, bot: Bot):
    """Владелец пишет в любом личном чате «@бот 1500 ссылка 60» — бот заменяет это оффером от его имени."""
    bc_id = msg.business_connection_id
    me = await bot.me()
    text = (msg.text or "").strip()
    prefix = "@" + (me.username or "").lower()
    log.info("business_message chat=%s from=%s text=%r", msg.chat.id, msg.from_user and msg.from_user.id, text[:60])
    if not bc_id or not text.lower().startswith(prefix):
        log.info("skip: text doesn't start with %s", prefix)
        return
    owner = await business_owner(bot, bc_id)
    if owner is None or not msg.from_user or msg.from_user.id != owner:
        log.info("skip: sender %s is not owner %s", msg.from_user and msg.from_user.id, owner)
        return
    await business_offer(msg, text[len(prefix):].strip(), bot, bc_id)


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
    try:
        mid = await send_view(bot, msg.chat.id, view_for(o, "shared"), bc_id)
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        log.warning("business send failed: %s", e)
        # сообщаем владельцу причину в личку с ботом (если он когда-то нажимал /start)
        try:
            await bot.send_message(
                msg.from_user.id,
                f"⚠️ Не удалось отправить оффер в чат с {escape(msg.chat.full_name or str(msg.chat.id))}:\n"
                f"<code>{escape(str(e))}</code>",
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        return
    o["msgs"]["shared"] = (msg.chat.id, mid, bc_id)
    OFFERS[o["id"]] = o

    # убираем саму команду «/offer ...» из чата (нужно право на удаление сообщений)
    try:
        await bot.delete_business_messages(business_connection_id=bc_id, message_ids=[msg.message_id])
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        log.info("can't delete command message: %s", e)


def get_offer(cb: CallbackQuery) -> dict | None:
    return OFFERS.get(cb.data.split(":", 1)[1])


@dp.callback_query(F.data.startswith("acc:"))
async def on_accept(cb: CallbackQuery):
    o = get_offer(cb)
    if not o:
        return await cb.answer("Оффер не найден (бот перезапускался).", show_alert=True)
    if o["status"] != "pending":
        return await cb.answer("Оффер уже обработан.")
    if o["expires"] <= time.time():
        o["status"] = "expired"
        await refresh(cb.bot, o)
        return await cb.answer("Срок истёк.")
    if cb.from_user.id == o["buyer_id"]:
        return await cb.answer("Нельзя принять собственный оффер.", show_alert=True)
    if o["target_id"] and cb.from_user.id != o["target_id"]:
        return await cb.answer("Этот оффер адресован другому пользователю.", show_alert=True)
    o["status"] = "processing"  # защита от двойного нажатия
    if not await reserve_payment(o):
        o["status"] = "pending"
        return await cb.answer("Не удалось зарезервировать оплату.", show_alert=True)
    o["status"] = "accepted"
    o["seller_id"] = cb.from_user.id
    o["seller_name"] = cb.from_user.full_name
    await refresh(cb.bot, o)
    await cb.answer("Сделка принята!")


@dp.callback_query(F.data.startswith("dec:"))
async def on_decline(cb: CallbackQuery):
    o = get_offer(cb)
    if not o:
        return await cb.answer("Оффер не найден.", show_alert=True)
    if o["status"] != "pending":
        return await cb.answer("Оффер уже обработан.")
    if o["target_id"] and cb.from_user.id not in (o["target_id"], o["buyer_id"]):
        return await cb.answer("Этот оффер адресован другому пользователю.", show_alert=True)
    o["status"] = "declined"
    await refresh(cb.bot, o)
    await cb.answer("Сделка отклонена.")


@dp.callback_query(F.data.startswith("cx:"))
async def on_cancel(cb: CallbackQuery):
    o = get_offer(cb)
    if not o or o["status"] != "pending":
        return await cb.answer("Недоступно.", show_alert=True)
    if cb.from_user.id != o["buyer_id"]:
        return await cb.answer("Отменить может только автор оффера.", show_alert=True)
    o["status"] = "declined"
    await refresh(cb.bot, o)
    await cb.answer("Оффер отменён.")


@dp.callback_query(F.data.startswith("sc:"))
async def on_seller_confirm(cb: CallbackQuery):
    """Продавец: «Подтвердить передачу»."""
    o = get_offer(cb)
    if not o or o["status"] != "accepted":
        return await cb.answer("Недоступно.", show_alert=True)
    if cb.from_user.id != o["seller_id"]:
        return await cb.answer("Подтвердить передачу может только продавец.", show_alert=True)
    o["status"] = "seller_confirmed"
    await refresh(cb.bot, o)
    await cb.answer("Ждём подтверждения покупателя.")


@dp.callback_query(F.data.startswith("bc:"))
async def on_buyer_confirm(cb: CallbackQuery):
    """Покупатель: «Подтвердить получение» -> финал."""
    o = get_offer(cb)
    if not o or o["status"] != "seller_confirmed":
        return await cb.answer("Недоступно.", show_alert=True)
    if cb.from_user.id != o["buyer_id"]:
        return await cb.answer("Подтвердить получение может только покупатель.", show_alert=True)
    o["status"] = "done"
    await release_payment(o)
    await refresh(cb.bot, o)
    await cb.answer("Готово!")


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Задай переменную окружения BOT_TOKEN")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
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
