"""
admin_panel.py — админ-панель для Telegram-бота офферов на aiogram 3.x.

Что умеет:
  • помнит всех, кто писал боту (профили: имя, username, первый/последний визит);
  • список доступа (allowlist): /allow и /revoke по ID или @username, кнопки в карточке;
  • /admin — главное меню: строка «бета / боевой режим», сводка, кнопки разделов и список людей;
  • 📦 Офферы (/offers): последние и активные офферы, карточка оффера (подарок, цена, статус,
    покупатель/получатель/продавец, вариант отправки, события), история переживает перезапуск;
  • 🔗 Бизнес (/business): кто подключил бота в Telegram Business (Чат-боты), активно ли подключение;
  • 🩺 Статус (/status): режим бета/боевой, какие функции оплаты подключены, а какие заглушки,
    статистика офферов, статистика доставки (сколько отправлено новым сообщением, сколько через
    правку, сколько ошибок PEER_FLOOD и каким вариантом текста их обошли), настройки бота;
  • /users и /log — список людей и журнал событий;
  • состояние (профили, доступ, история офферов, счётчики) сохраняется в JSON-файл.

Как подключить (в bot.py уже сделано):

    from admin_panel import (setup_admin, attach_bot, is_admin, is_allowed, track, who,
                             notify_admin, log_event, record_offer, bump, set_business)

    dp = Dispatcher()
    setup_admin(dp)                                    # middleware + роутер с командами
    attach_bot(offers=OFFERS, business=BUSINESS,       # живые данные бота (те же dict-ы)
               status=admin_status, money=money)       # статус оплаты/настроек и формат цены

Что бот сообщает панели:
    record_offer(o)               — снимок оффера (вызывать при создании и на каждом шаге сделки)
    bump("peer_flood")            — увеличить счётчик (доставка, PEER_FLOOD, варианты текста)
    log_event("текст")            — строка в /log
    set_business(user, True/False)— бизнес-подключение включено/выключено
    notify_admin(bot, text, user_id=…, offer_id=…) — уведомление админу с кнопкой «Карточка»/«Оффер»

Переменные окружения:
  ADMIN_ID         — твой Telegram ID (только ты видишь панель)
  ALLOWED_IDS      — ID через запятую, у кого доступ есть всегда
  ADMIN_STATE_FILE — путь к json (по умолчанию admin_state.json). На Railway файловая система
                     стирается при деплое: для постоянства подключи Volume и укажи путь на него.
"""
import json
import logging
import os
import re
import time
from collections import deque
from html import escape
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    TelegramObject,
    User,
)

log = logging.getLogger("admin_panel")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# ---------------------------------------------------------------------------
# 1. СОСТОЯНИЕ
# ---------------------------------------------------------------------------
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
STATE_FILE = Path(os.getenv("ADMIN_STATE_FILE", "admin_state.json"))

# user_id -> {"username", "name", "first_seen", "last_seen", "business", "business_since", ...}
PROFILES: dict[int, dict] = {}
# ID, которым разрешён доступ (кроме админа). Стартовое значение — из переменной окружения.
ALLOWED: set[int] = {int(x) for x in re.findall(r"\d+", os.getenv("ALLOWED_IDS", ""))}
USERNAMES: dict[str, int] = {}                 # username (lower) -> user_id
EVENTS: deque[str] = deque(maxlen=300)         # последние события
OFFER_LOG: dict[str, dict] = {}                # id оффера -> снимок (история переживает перезапуск)
COUNTERS: dict[str, int] = {}                  # счётчики доставки / PEER_FLOOD / вариантов текста
MAX_OFFER_LOG = 300

# Связь с ботом (заполняется через attach_bot)
_offers_ref: dict | None = None
_business_ref: dict | None = None
_status_provider: Callable[[], dict] | None = None
_money_fn: Callable[[dict], str] | None = None
_activity_provider: Callable[[int], list[str]] | None = None


def load_state() -> None:
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.exception("не смог прочитать %s", STATE_FILE)
        return
    for uid, p in data.get("profiles", {}).items():
        PROFILES[int(uid)] = p
        if p.get("username"):
            USERNAMES[p["username"].lower()] = int(uid)
    ALLOWED.update(int(x) for x in data.get("allowed", []))
    OFFER_LOG.update(data.get("offers", {}))
    COUNTERS.update({k: int(v) for k, v in data.get("counters", {}).items()})


def save_state() -> None:
    try:
        payload = json.dumps(
            {"profiles": PROFILES, "allowed": sorted(ALLOWED), "offers": OFFER_LOG, "counters": COUNTERS},
            ensure_ascii=False,
        )
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(STATE_FILE)  # атомарно: файл не повредится при падении
    except OSError:
        log.exception("не смог записать %s", STATE_FILE)


def set_activity_provider(fn: Callable[[int], list[str]]) -> None:
    """Необязательно: дописать свои строки в карточку пользователя."""
    global _activity_provider
    _activity_provider = fn


def attach_bot(
    *,
    offers: dict | None = None,
    business: dict | None = None,
    status: Callable[[], dict] | None = None,
    money: Callable[[dict], str] | None = None,
) -> None:
    """Связать панель с ботом. offers/business — те же словари, что живут в боте."""
    global _offers_ref, _business_ref, _status_provider, _money_fn
    _offers_ref, _business_ref, _status_provider, _money_fn = offers, business, status, money


def log_event(text: str) -> None:
    EVENTS.append(f"{time.strftime('%d.%m %H:%M:%S')} · {text}")


def bump(name: str, n: int = 1) -> None:
    """Счётчик для раздела «Статус» (например peer_flood, sent_new, variant:no_buttons)."""
    COUNTERS[name] = COUNTERS.get(name, 0) + n
    save_state()


# ---------------------------------------------------------------------------
# 2. ПРАВА И ПРОФИЛИ
# ---------------------------------------------------------------------------
def is_admin(uid: int | None) -> bool:
    return bool(ADMIN_ID) and uid == ADMIN_ID


def is_allowed(uid: int | None) -> bool:
    return uid is not None and (is_admin(uid) or uid in ALLOWED)


def track(u: User | None) -> None:
    if not u or u.is_bot:
        return
    now = time.time()
    p = PROFILES.setdefault(u.id, {"first_seen": now})
    p.update(username=u.username, name=u.full_name, last_seen=now)
    if u.username:
        USERNAMES[u.username.lower()] = u.id


def set_business(u: User | None, enabled: bool, bc_id: str | None = None) -> None:
    """Бот сообщает: пользователь подключил/отключил его в Telegram Business."""
    if not u:
        return
    track(u)
    p = PROFILES.setdefault(u.id, {"first_seen": time.time()})
    now = time.time()
    p["business"] = bool(enabled)
    p["business_changed"] = now
    if enabled:
        p.setdefault("business_since", now)
    save_state()
    log_event(f"бизнес-подключение {'включено' if enabled else 'выключено'}: {u.id}")


def is_connected(uid: int) -> bool:
    if _business_ref is not None:
        return uid in set(_business_ref.values())
    return bool(PROFILES.get(uid, {}).get("business"))


def resolve_user(arg: str) -> int | None:
    arg = (arg or "").strip()
    if m := re.fullmatch(r"@?(\d{5,})", arg):
        return int(m.group(1))
    if m := re.fullmatch(r"@(\w{4,32})", arg):
        return USERNAMES.get(m.group(1).lower())
    return None


# ---------------------------------------------------------------------------
# 3. ОФФЕРЫ: снимки, формат цены и подарка
# ---------------------------------------------------------------------------
STATUS = {
    "pending": ("⏳", "ждёт ответа"),
    "processing": ("⏳", "обрабатывается"),
    "accepted": ("🤝", "принят, ждём передачу"),
    "seller_confirmed": ("📦", "передан, ждём подтверждения"),
    "done": ("✅", "завершён"),
    "declined": ("❌", "отклонён / отменён"),
    "expired": ("⌛", "срок истёк"),
}
ACTIVE = {"pending", "processing", "accepted", "seller_confirmed"}
_FLAG_KEYS = (
    "strip_links", "alt_timer", "no_timer", "plain_gift", "raw_url", "simple_preview",
    "auto_preview", "no_preview", "no_buttons", "no_url_buttons",
)


def variant_name(o: dict) -> str:
    """Каким вариантом текста ушёл оффер (обход PEER_FLOOD): full или набор упрощений."""
    return "+".join(k for k in _FLAG_KEYS if o.get(k)) or "full"


def summarize(o: dict) -> dict:
    return {
        "id": o.get("id"), "created": o.get("created") or 0,
        "buyer_id": o.get("buyer_id"), "target_id": o.get("target_id"), "target_name": o.get("target_name"),
        "seller_id": o.get("seller_id"), "seller_name": o.get("seller_name"),
        "url": o.get("url"), "title": o.get("title"),
        "price": o.get("price"), "currency": o.get("currency"),
        "status": o.get("status"), "expires": o.get("expires"),
        "variant": variant_name(o), "updated": time.time(),
    }


def record_offer(o: dict) -> None:
    """Бот вызывает при создании оффера и после каждого шага сделки."""
    if not o.get("id"):
        return
    OFFER_LOG[o["id"]] = summarize(o)
    if len(OFFER_LOG) > MAX_OFFER_LOG:
        newest = sorted(OFFER_LOG.values(), key=lambda s: s.get("created") or 0, reverse=True)[:MAX_OFFER_LOG]
        OFFER_LOG.clear()
        OFFER_LOG.update({s["id"]: s for s in newest})
    save_state()


def all_offers() -> list[dict]:
    merged = dict(OFFER_LOG)
    for o in (_offers_ref or {}).values():          # живые данные важнее сохранённых
        if o.get("id"):
            merged[o["id"]] = summarize(o)
    return sorted(merged.values(), key=lambda s: s.get("created") or 0, reverse=True)


def find_offer(key: str) -> dict | None:
    key = (key or "").strip().lower()
    if not key:
        return None
    items = all_offers()
    for s in items:
        if s["id"] == key:
            return s
    hits = [s for s in items if s["id"].startswith(key)]
    return hits[0] if len(hits) == 1 else None


def fmt_money(s: dict) -> str:
    """Формат цены: Stars -> «1500 ★», Gram -> «12.5 Gram» (формат берём у бота, если он передан)."""
    if _money_fn:
        try:
            return _money_fn(s)
        except Exception:  # noqa: BLE001
            pass
    p = s.get("price") or 0
    if s.get("currency") == "stars":
        return f"{int(p)} ★"
    return (f"{p:,.2f}".rstrip("0").rstrip(".") if isinstance(p, float) else f"{p:,}") + " Gram"


def gift_label(s: dict) -> str:
    """Подарок: «Mirage Lamp #71292» (название берём из ссылки t.me/nft/…, если его нет)."""
    if s.get("title"):
        return s["title"]
    slug = (s.get("url") or "").split("?")[0].rstrip("/").split("/")[-1]
    if "-" in slug:
        name, num = slug.rsplit("-", 1)
        return f"{re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', name)} #{num}"
    return slug or "?"


def status_text(s: dict) -> str:
    emoji, label = STATUS.get(s.get("status"), ("•", s.get("status") or "?"))
    return f"{emoji} {label}"


# ---------------------------------------------------------------------------
# 4. MIDDLEWARE И ФИЛЬТР
# ---------------------------------------------------------------------------
class TrackUsers(BaseMiddleware):
    """Запоминает автора каждого апдейта и сообщает админу о новых людях."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        # Сообщения в бизнес-чатах пишут собеседники владельца — посторонних людей не запоминаем
        # и не шлём из-за них уведомления; учитываем только самих владельцев подключений.
        if any(getattr(event, k, None) for k in ("business_message", "edited_business_message",
                                                 "deleted_business_messages")):
            if not (user and is_connected(user.id)):
                return await handler(event, data)
        is_new = bool(user and not user.is_bot and user.id not in PROFILES)
        track(user)
        if is_new:
            save_state()
            log_event(f"новый пользователь {user.id}")
            if not is_admin(user.id) and data.get("bot"):
                status = "✅ в списке разрешённых" if is_allowed(user.id) else "⛔ НЕ в списке разрешённых"
                await notify_admin(data["bot"], f"🔔 Новый пользователь: {who(user.id)}\n{status}", user.id)
        return await handler(event, data)


class IsAdmin(Filter):
    async def __call__(self, event: TelegramObject, event_from_user: User | None = None) -> bool:
        return event_from_user is not None and is_admin(event_from_user.id)


# ---------------------------------------------------------------------------
# 5. ТЕКСТЫ И КЛАВИАТУРЫ
# ---------------------------------------------------------------------------
def short(uid: int | None) -> str:
    p = PROFILES.get(uid or 0, {})
    return "@" + p["username"] if p.get("username") else (p.get("name") or str(uid))


def who(uid: int) -> str:
    p = PROFILES.get(uid, {})
    name = escape(p.get("name") or str(uid))
    un = f" @{p['username']}" if p.get("username") else ""
    return f'<a href="tg://user?id={uid}">{name}</a>{un} (<code>{uid}</code>)'


def fmt_time(ts: float | None) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(ts)) if ts else "—"


def target_label(s: dict) -> str:
    tid = s.get("target_id")
    if tid and tid in PROFILES:
        return short(tid)
    return s.get("target_name") or (str(tid) if tid else "—")


def offer_line(s: dict) -> str:
    return (
        f"{fmt_time(s.get('created'))} · {escape(short(s.get('buyer_id')))} → {escape(str(target_label(s)))} · "
        f"{escape(gift_label(s))} · {escape(fmt_money(s))} · {status_text(s)}"
    )


def _status_info() -> dict:
    if not _status_provider:
        return {}
    try:
        return _status_provider() or {}
    except Exception:  # noqa: BLE001
        log.exception("status provider failed")
        return {}


def mode_line() -> str:
    """Строка «бета / боевой режим» — показывается в /admin и /status."""
    st = _status_info()
    if "live" not in st:
        return "Режим: ❔ не задан (бот не передал статус)"
    if st["live"]:
        return "Режим: 🟢 <b>боевой</b> — оплата подключена"
    return "Режим: 🧪 <b>бета</b> — оплата не подключена, офферы только между разрешёнными"


async def notify_admin(bot: Bot, text: str, user_id: int | None = None, offer_id: str | None = None) -> None:
    """Сообщение админу в личку; с user_id — кнопка «Карточка», с offer_id — кнопка «Оффер»."""
    if not ADMIN_ID:
        return
    row = []
    if user_id:
        row.append(InlineKeyboardButton(text="👤 Карточка", callback_data=f"adm:u:{user_id}"))
    if offer_id:
        row.append(InlineKeyboardButton(text="📦 Оффер", callback_data=f"adm:o:{offer_id}"))
    kb = InlineKeyboardMarkup(inline_keyboard=[row]) if row else None
    try:
        await bot.send_message(ADMIN_ID, text, reply_markup=kb, link_preview_options=NO_PREVIEW)
    except TelegramAPIError as e:
        log.info("не удалось уведомить админа: %s", e)


MENU_ROW = [
    InlineKeyboardButton(text="📦 Офферы", callback_data="adm:o:list"),
    InlineKeyboardButton(text="🔗 Бизнес", callback_data="adm:b:0"),
    InlineKeyboardButton(text="🩺 Статус", callback_data="adm:s:0"),
]
BACK_ROW = [InlineKeyboardButton(text="🛠 Меню", callback_data="adm:l:0")]


async def show_admin(bot: Bot, chat_id: int) -> None:
    users = sorted(PROFILES.items(), key=lambda kv: kv[1].get("last_seen", 0), reverse=True)[:20]
    allowed_total = len(ALLOWED | ({ADMIN_ID} if ADMIN_ID else set()))
    offers = all_offers()
    active = sum(1 for s in offers if s.get("status") in ACTIVE)
    conns = len(_business_ref) if _business_ref is not None else sum(1 for p in PROFILES.values() if p.get("business"))
    text = (
        "🛠 <b>Админ-панель</b>\n\n"
        f"{mode_line()}\n"
        f"Пользователей: {len(PROFILES)} · с доступом: {allowed_total}\n"
        f"Офферов: {len(offers)} · в процессе: {active}\n"
        f"Бизнес-подключений: {conns}\n\n"
        "Команды: /offers, /business, /status, /users, /log, "
        "/allow &lt;id или @username&gt;, /revoke &lt;id или @username&gt;\n"
        "Нажми на человека, чтобы открыть карточку."
    )
    rows = [MENU_ROW] + [
        [InlineKeyboardButton(
            text=f"{'✅' if is_allowed(uid) else '⛔'} {short(uid)}" + (" · бизнес" if is_connected(uid) else ""),
            callback_data=f"adm:u:{uid}",  # формат: adm:<действие>:<аргумент>
        )]
        for uid, _ in users
    ]
    await bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                           link_preview_options=NO_PREVIEW)


async def show_offers(bot: Bot, chat_id: int, only_active: bool = False) -> None:
    items_all = all_offers()
    items = [s for s in items_all if not only_active or s.get("status") in ACTIVE][:15]
    title = "Активные офферы" if only_active else "Последние офферы"
    if not items:
        text = f"📦 <b>{title}</b>\n\nПока пусто."
    else:
        text = f"📦 <b>{title}</b> ({len(items)} из {len(items_all)})\n\n" + "\n".join(offer_line(s) for s in items)
    rows = [
        [InlineKeyboardButton(
            text=f"{STATUS.get(s.get('status'), ('•',))[0]} {gift_label(s)} · {fmt_money(s)}"[:60],
            callback_data=f"adm:o:{s['id']}",
        )]
        for s in items[:10]
    ]
    rows.append([
        InlineKeyboardButton(text="Все" if only_active else "Только активные",
                             callback_data="adm:o:list" if only_active else "adm:o:active"),
        *BACK_ROW,
    ])
    await bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                           link_preview_options=NO_PREVIEW)


async def send_offer_card(bot: Bot, chat_id: int, key: str) -> None:
    s = find_offer(key)
    if not s:
        return await bot.send_message(chat_id, "Оффер не найден (в истории хранятся последние "
                                               f"{MAX_OFFER_LOG}).")
    gift = f'<a href="{escape(s["url"])}">{escape(gift_label(s))}</a>' if s.get("url") else escape(gift_label(s))
    tid = s.get("target_id")
    lines = [
        f"🎁 {gift}",
        f"Цена: <b>{escape(fmt_money(s))}</b>",
        f"Статус: {status_text(s)}",
        f"Покупатель: {who(s['buyer_id'])}" if s.get("buyer_id") else "Покупатель: —",
        f"Получатель: {who(tid)}" if tid else f"Получатель: {escape(str(target_label(s)))}",
    ]
    if s.get("seller_id"):
        lines.append(f"Продавец (принял): {who(s['seller_id'])}")
    created = f"Создан: {fmt_time(s.get('created'))}"
    if s.get("expires"):
        created += f" · действует до {fmt_time(s['expires'])}"
    lines += [created, f"Вариант отправки: <code>{escape(s.get('variant') or 'full')}</code>",
              f"ID: <code>{s['id']}</code>"]
    ev = [e for e in EVENTS if s["id"] in e][-6:]
    if ev:
        lines.append("\nСобытия:\n" + escape("\n".join(ev)))
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ К офферам", callback_data="adm:o:list"), *BACK_ROW,
    ]])
    await bot.send_message(chat_id, "\n".join(lines), reply_markup=kb, link_preview_options=NO_PREVIEW)


async def show_business(bot: Bot, chat_id: int) -> None:
    people = sorted(
        ((uid, p) for uid, p in PROFILES.items() if p.get("business_since")),
        key=lambda kv: kv[1].get("business_changed", 0), reverse=True,
    )
    active = sum(1 for uid, _ in people if is_connected(uid))
    if not people:
        text = ("🔗 <b>Бизнес-подключения</b>\n\nПока никто не подключил бота: "
                "Настройки → Telegram Business → Чат-боты.")
    else:
        lines = [
            f"{'🟢' if is_connected(uid) else '⚪️'} {who(uid)} · с {fmt_time(p.get('business_since'))}"
            f"{'' if is_connected(uid) else ' · отключён ' + fmt_time(p.get('business_changed'))}"
            for uid, p in people[:25]
        ]
        text = (f"🔗 <b>Бизнес-подключения</b>\nАктивных: {active} из {len(people)}\n\n" + "\n".join(lines) +
                "\n\n🟢 — бот может отправлять сообщения от имени аккаунта.")
    rows = [[InlineKeyboardButton(text=f"{'🟢' if is_connected(uid) else '⚪️'} {short(uid)}",
                                  callback_data=f"adm:u:{uid}")] for uid, _ in people[:10]]
    rows.append(BACK_ROW)
    await bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                           link_preview_options=NO_PREVIEW)


COUNTER_LABELS = {
    "sent_new": "оффер отправлен новым сообщением от имени аккаунта",
    "sent_edit": "оффер ушёл через правку вашего сообщения (получатель может не видеть)",
    "send_failed": "оффер не удалось отправить совсем",
    "resend_ok": "шагов сделки доставлено новым сообщением",
    "resend_failed": "шагов сделки не доставлено (правка вместо сообщения)",
    "peer_flood": "ошибок PEER_FLOOD от Telegram",
    "gate_blocked": "офферов заблокировано (нет доступа / бета-режим)",
}


def counters_text() -> str:
    lines = [f"• {label}: <b>{COUNTERS[k]}</b>" for k, label in COUNTER_LABELS.items() if COUNTERS.get(k)]
    lines += [f"• вариант <code>{escape(k[8:])}</code>: <b>{v}</b>"
              for k, v in sorted(COUNTERS.items()) if k.startswith("variant:")]
    return "\n".join(lines) or "• пока нет данных"


async def show_status(bot: Bot, chat_id: int) -> None:
    st = _status_info()
    offers = all_offers()
    by_status: dict[str, int] = {}
    totals: dict[str, float] = {}
    for s in offers:
        by_status[s.get("status") or "?"] = by_status.get(s.get("status") or "?", 0) + 1
        if s.get("status") == "done":
            cur = s.get("currency") or "?"
            totals[cur] = totals.get(cur, 0) + (s.get("price") or 0)
    stat_line = " · ".join(f"{STATUS.get(k, ('•',))[0]} {v}" for k, v in by_status.items()) or "—"
    done_line = " · ".join(escape(fmt_money({"currency": c, "price": v})) for c, v in totals.items()) or "—"

    hooks = st.get("hooks") or {}
    pay_lines = [f"{'✅ подключено' if ok else '▫️ заглушка'} — <code>{escape(name)}</code>"
                 for name, ok in hooks.items()] or ["▫️ bot_payments.py не найден — всё работает как заглушка"]
    flags = st.get("flags") or {}
    flag_lines = [f"{'✅' if v is True else '▫️' if v is False else '•'} {escape(str(k))}"
                  f"{'' if isinstance(v, bool) else ': ' + escape(str(v))}" for k, v in flags.items()]

    text = (
        "🩺 <b>Статус бота</b>\n\n"
        f"{mode_line()}\n\n"
        "💳 <b>Оплата</b> (bot_payments.py)\n" + "\n".join(pay_lines) + "\n\n"
        f"📦 <b>Офферы</b>: всего {len(offers)}\nПо статусам: {stat_line}\nЗавершено на сумму: {done_line}\n\n"
        "📡 <b>Доставка в Business-чаты</b> (обход PEER_FLOOD)\n" + counters_text() + "\n\n"
        "⚙️ <b>Настройки бота</b>\n" + ("\n".join(flag_lines) or "• не переданы")
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Обновить", callback_data="adm:s:0"), *BACK_ROW,
    ]])
    await bot.send_message(chat_id, text, reply_markup=kb, link_preview_options=NO_PREVIEW)


async def send_card(bot: Bot, chat_id: int, uid: int) -> None:
    p = PROFILES.get(uid, {})
    sent = [s for s in all_offers() if s.get("buyer_id") == uid]
    got = [s for s in all_offers() if s.get("target_id") == uid]
    caption = (
        f"👤 {who(uid)}\n"
        f"Доступ: {'✅ разрешён' if is_allowed(uid) else '⛔ закрыт'}{' (админ)' if is_admin(uid) else ''}\n"
        f"Бизнес-подключение: {'🟢 активно' if is_connected(uid) else '⚪️ нет'}\n"
        f"Впервые: {fmt_time(p.get('first_seen'))} · последний раз: {fmt_time(p.get('last_seen'))}\n"
        f"Офферов: отправил {len(sent)} · получил {len(got)}"
    )
    if _activity_provider:
        try:
            extra = _activity_provider(uid)
        except Exception:  # noqa: BLE001
            log.exception("activity provider failed")
            extra = []
        if extra:
            caption += "\n" + "\n".join(extra)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Разрешить", callback_data=f"adm:a:{uid}"),
         InlineKeyboardButton(text="⛔ Закрыть доступ", callback_data=f"adm:r:{uid}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="adm:l:0")],
    ])
    done = False
    try:  # аватарка, если получится
        photos = await bot.get_user_profile_photos(uid, limit=1)
        if photos.total_count:
            await bot.send_photo(chat_id, photos.photos[0][-1].file_id, caption=caption, reply_markup=kb)
            done = True
    except TelegramAPIError as e:
        log.info("аватар %s не получен: %s", uid, e)
    if not done:
        await bot.send_message(chat_id, caption, reply_markup=kb, link_preview_options=NO_PREVIEW)
    last = sorted(sent + got, key=lambda s: s.get("created") or 0, reverse=True)[:8]
    if last:
        rows = [[InlineKeyboardButton(text=f"{STATUS.get(s.get('status'), ('•',))[0]} {gift_label(s)} · {fmt_money(s)}"[:60],
                                      callback_data=f"adm:o:{s['id']}")] for s in last[:5]]
        await bot.send_message(chat_id, "Последние офферы:\n" + "\n".join(offer_line(s) for s in last),
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                               link_preview_options=NO_PREVIEW)


# ---------------------------------------------------------------------------
# 6. ХЭНДЛЕРЫ — только для админа
# ---------------------------------------------------------------------------
router = Router(name="admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


@router.message(Command("admin"))
async def cmd_admin(msg: Message, bot: Bot):
    await show_admin(bot, msg.chat.id)


@router.message(Command("offers"))
async def cmd_offers(msg: Message, command: CommandObject, bot: Bot):
    arg = (command.args or "").strip().lower()
    if arg and arg not in {"active", "активные"}:
        if find_offer(arg):
            return await send_offer_card(bot, msg.chat.id, arg)
        return await msg.answer("Оффер не найден. Формат: /offers, /offers active или /offers &lt;id&gt;")
    await show_offers(bot, msg.chat.id, only_active=bool(arg))


@router.message(Command("business"))
async def cmd_business(msg: Message, bot: Bot):
    await show_business(bot, msg.chat.id)


@router.message(Command("status"))
async def cmd_status(msg: Message, bot: Bot):
    await show_status(bot, msg.chat.id)


@router.message(Command("users"))
async def cmd_users(msg: Message):
    rows = sorted(PROFILES.items(), key=lambda kv: kv[1].get("last_seen", 0), reverse=True)[:30]
    lines = [f"{'✅' if is_allowed(u) else '⛔'} {escape(short(u))} · {fmt_time(p.get('last_seen'))}"
             f"{' · бизнес' if is_connected(u) else ''}" for u, p in rows]
    await msg.answer("\n".join(lines) or "Пользователей пока нет.")


@router.message(Command("log"))
async def cmd_log(msg: Message):
    last = list(EVENTS)[-25:]
    await msg.answer(escape("\n".join(last)) if last else "Журнал пуст.")


@router.message(Command("allow"))
@router.message(Command("revoke"))
async def cmd_allow_revoke(msg: Message, command: CommandObject):
    uid = resolve_user(command.args or "")
    if not uid:
        return await msg.answer("Формат: /allow 123456789 или /allow @username (@username работает, "
                                "если человек уже писал боту)")
    if command.command == "allow":
        ALLOWED.add(uid)
        save_state()
        log_event(f"админ разрешил {uid}")
        return await msg.answer(f"✅ Разрешён: {who(uid)}", link_preview_options=NO_PREVIEW)
    if is_admin(uid):
        return await msg.answer("Админа закрыть нельзя.")
    ALLOWED.discard(uid)
    save_state()
    log_event(f"админ закрыл доступ {uid}")
    await msg.answer(f"⛔ Доступ закрыт: {who(uid)}", link_preview_options=NO_PREVIEW)


@router.callback_query(F.data.startswith("adm:"))
async def on_admin_button(cb: CallbackQuery):
    _, act, raw = cb.data.split(":", 2)
    chat_id = cb.message.chat.id
    await_bot = cb.bot

    if act == "l":                                  # меню
        await cb.answer()
        return await show_admin(await_bot, chat_id)
    if act == "o":                                  # офферы: list / active / <id>
        await cb.answer()
        if raw in ("list", "active"):
            return await show_offers(await_bot, chat_id, only_active=(raw == "active"))
        return await send_offer_card(await_bot, chat_id, raw)
    if act == "b":                                  # бизнес-подключения
        await cb.answer()
        return await show_business(await_bot, chat_id)
    if act == "s":                                  # статус
        await cb.answer()
        return await show_status(await_bot, chat_id)

    uid = int(raw) if raw.isdigit() else 0
    if act == "a" and uid:                          # разрешить
        ALLOWED.add(uid)
        save_state()
        log_event(f"админ разрешил {uid} (кнопка)")
        await cb.answer("Разрешён")
    elif act == "r" and uid:                        # закрыть
        if is_admin(uid):
            return await cb.answer("Админа закрыть нельзя", show_alert=True)
        ALLOWED.discard(uid)
        save_state()
        log_event(f"админ закрыл доступ {uid} (кнопка)")
        await cb.answer("Доступ закрыт")
    else:                                           # "u" — открыть карточку
        await cb.answer()
    if uid:
        await send_card(await_bot, chat_id, uid)


# ---------------------------------------------------------------------------
# 7. ПОДКЛЮЧЕНИЕ
# ---------------------------------------------------------------------------
def setup_admin(dp: Dispatcher) -> None:
    """Один вызов в bot.py: загружает состояние, включает трекинг и подключает команды."""
    load_state()
    dp.update.outer_middleware(TrackUsers())
    dp.include_router(router)
    log.info("админ-панель подключена, ADMIN_ID=%s, файл состояния: %s", ADMIN_ID or "не задан", STATE_FILE)
