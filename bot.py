from __future__ import annotations

import asyncio
import html
import logging
import os
import random
import re
import time
from datetime import date, timedelta

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiohttp import web

# ---------------------------------------------------------------------------
# Налаштування (токен ТІЛЬКИ зі змінних середовища Render, не в коді!)
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задано змінну середовища BOT_TOKEN")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # твій Telegram ID (для скарг і поповнень)
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")  # пароль для входу в /admin

# Оплата криптою через @CryptoBot (https://t.me/CryptoBot -> Crypto Pay -> Create App)
CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN", "")
CRYPTO_PAY_API = "https://pay.crypt.bot/api"
# Орієнтовний курс USDT -> грн для нарахування балансу (онови за потреби)
USD_UAH_RATE = float(os.getenv("USD_UAH_RATE", "41"))

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# ---------------------------------------------------------------------------
# "База даних" у пам'яті (скидається при кожному перезапуску Render!)
# Для постійного зберігання підключи PostgreSQL (Render Postgres) або SQLite з диском.
# ---------------------------------------------------------------------------
users_db: dict[int, dict] = {}
queue: list[int] = []
active_chats: dict[int, int] = {}
user_counter = 1000

banned_users: set[int] = set()
reports: list[dict] = []  # {"id", "from", "on", "time", "status"}
report_counter = 0
authorized_admins: set[int] = set()  # хто вже ввів пароль у цій сесії

# Крипто-рахунки, очікують оплати: invoice_id -> {"user_id", "amount" (USDT), "credit" (грн)}
pending_crypto_invoices: dict[str, dict] = {}

# Пакети поповнення
STAR_PACKAGES = [50, 100, 250, 500]  # Telegram Stars; 1 star = 1 грн на баланс
CRYPTO_PACKAGES = [1, 5, 10, 20]  # USDT

DAY = 86400

# Магазин: ключ -> (назва, ціна, перк, тривалість у секундах; None = назавжди)
SHOP = {
    "nick": ("Зміна нікнейму", 20, None, None),
    "prio_1d": ("Пріоритет у пошуку (1 день)", 30, "priority", DAY),
    "vip_badge": ("VIP-значок у профілі", 40, "vip_badge", None),
    "blacklist": ("Розширений чорний список", 50, "blacklist", None),
    "gender_filter": ("Фільтр за статтю (1 тиждень)", 65, "gender_filter", 7 * DAY),
    "age_filter": ("Фільтр за віком (1 тиждень)", 65, "age_filter", 7 * DAY),
    "country_filter": ("Фільтр за країною (1 тиждень)", 65, "country_filter", 7 * DAY),
    "prem_1w": ("Premium на 1 тиждень", 80, "premium", 7 * DAY),
    "prem_1m": ("Premium на 1 місяць", 180, "premium", 30 * DAY),
    "prem_3m": ("Premium на 3 місяці", 350, "premium", 90 * DAY),
    "prem_6m": ("Premium на 6 місяців", 600, "premium", 180 * DAY),
    "prem_1y": ("Premium на 1 рік", 950, "premium", 365 * DAY),
    "prem_forever": ("Безлімітний Premium (назавжди)", 1400, "premium", None),
}
NICK_PRICE = SHOP["nick"][1]

# Каталог подарунків: ключ -> (назва, ціна в грн). Купуються "про запас" в інвентар,
# даруються будь-коли під час чату. Відсоток ціни падає отримувачу на баланс,
# решта лишається адміну (тобто просто не нараховується нікому).
GIFT_CATALOG = {
    "rose": ("🌹 Троянда", 10),
    "heart": ("❤️ Серце", 12),
    "rabbit": ("🐰 Зайчик", 18),
    "dog": ("🐶 Собачка", 18),
    "cat": ("🐱 Котик", 18),
    "cake": ("🎂 Тортик", 25),
    "teddy": ("🧸 Ведмедик", 40),
    "ring": ("💍 Каблучка", 75),
    "diamond": ("💎 Діамант", 150),
    "car": ("🚗 Машинка", 60),
    "crown": ("👑 Корона", 300),
}
# Частка від ціни подарунка, яка йде отримувачу (решта — дохід адміна)
GIFT_RECIPIENT_SHARE = float(os.getenv("GIFT_RECIPIENT_SHARE", "0.25"))  # 0.25 = 25%

gift_revenue_total = 0.0  # сумарний дохід адміна з подарунків (для статистики)
lottery_revenue_total = 0.0  # дохід адміна з рулетки (ставки мінус виплати)
BOT_USERNAME = ""  # заповнюється при старті (main()), для реферальних посилань

# ---------------------------------------------------------------------------
# Реферальна програма
# ---------------------------------------------------------------------------
REFERRAL_BONUS = float(os.getenv("REFERRAL_BONUS", "15"))  # грн авторові запрошення
REFERRAL_PREMIUM_EVERY = int(os.getenv("REFERRAL_PREMIUM_EVERY", "5"))  # кожні N запрошених
REFERRAL_PREMIUM_DAYS = int(os.getenv("REFERRAL_PREMIUM_DAYS", "5"))  # днів Premium у подарунок

# ---------------------------------------------------------------------------
# Реконнект з минулим співрозмовником (за згодою обох сторін)
# ---------------------------------------------------------------------------
reconnect_requests: dict[int, int] = {}  # acceptor_id -> requester_id (очікує відповіді)

# ---------------------------------------------------------------------------
# Рулетка
# ---------------------------------------------------------------------------
LOTTERY_COST = float(os.getenv("LOTTERY_COST", "15"))  # грн за один спін
# (шанс у %, тип виграшу, множник для money / None для gift)
LOTTERY_TABLE = [
    (45, "nothing", None),
    (25, "money", 1.5),
    (10, "money", 3),
    (12, "gift", None),
    (6, "money", 5),
    (2, "money", 20),
]


def spin_lottery():
    r = random.uniform(0, 100)
    cumulative = 0.0
    for weight, kind, mult in LOTTERY_TABLE:
        cumulative += weight
        if r <= cumulative:
            return kind, mult
    return "nothing", None


# ---------------------------------------------------------------------------
# Продаж подарунка назад (частковий викуп)
# ---------------------------------------------------------------------------
GIFT_SELLBACK_SHARE = float(os.getenv("GIFT_SELLBACK_SHARE", "0.5"))  # 50% від ціни

# ---------------------------------------------------------------------------
# Автобан за скарги та антиспам-фільтр (посилання/контакти в чаті)
# ---------------------------------------------------------------------------
AUTO_BAN_REPORTS = int(os.getenv("AUTO_BAN_REPORTS", "3"))  # скарг до автобану
LINK_REGEX = re.compile(
    r"(https?://\S+|t\.me/\S+|www\.\S+|@[a-zA-Z0-9_]{5,32})", re.IGNORECASE
)

# ---------------------------------------------------------------------------
# Чорний список (особисте блокування співрозмовників)
# ---------------------------------------------------------------------------
BLACKLIST_LIMIT_FREE = int(os.getenv("BLACKLIST_LIMIT_FREE", "20"))
BLACKLIST_LIMIT_PLUS = int(os.getenv("BLACKLIST_LIMIT_PLUS", "100"))  # з перком "blacklist" або Premium

# ---------------------------------------------------------------------------
# Реальні фільтри пошуку: стать, вік, країна (потребують перку або Premium)
# ---------------------------------------------------------------------------
# (ключ, підпис, предикат(вік:int) -> bool)
AGE_RANGES = [
    ("lt18", "До 18", lambda a: a < 18),
    ("18-22", "18-22", lambda a: 18 <= a <= 22),
    ("23-27", "23-27", lambda a: 23 <= a <= 27),
    ("28-32", "28-32", lambda a: 28 <= a <= 32),
    ("32-36", "32-36", lambda a: 32 <= a <= 36),
    ("gt36", "Понад 36", lambda a: a > 36),
]

# ---------------------------------------------------------------------------
# Щоденний бонус: кожен день — трохи грн, кожен 5-й день поспіль — подарунок
# ---------------------------------------------------------------------------
DAILY_BONUS_AMOUNT = float(os.getenv("DAILY_BONUS_AMOUNT", "2"))  # грн за звичайний день
DAILY_GIFT_EVERY = 5  # кожні N днів поспіль — подарунок замість грошей
DAILY_GIFT_MAX_PRICE = 25  # дарується щось із каталогу дешевше цієї суми

# ---------------------------------------------------------------------------
# Рівні за кількістю чатів
# ---------------------------------------------------------------------------
LEVELS = [
    (0, "🔰 Новачок"),
    (10, "⭐ Активний"),
    (30, "🏅 Досвідчений"),
    (75, "👑 Легенда"),
]


def get_level_title(u: dict) -> str:
    chats = u.get("total_chats", 0)
    title = LEVELS[0][1]
    for threshold, name in LEVELS:
        if chats >= threshold:
            title = name
    return title


# ---------------------------------------------------------------------------
# Досягнення
# ---------------------------------------------------------------------------
ACHIEVEMENTS = {
    "chat10": ("💬 Балакун", lambda u: u.get("total_chats", 0) >= 10),
    "chat50": ("🗣️ Легенда спілкування", lambda u: u.get("total_chats", 0) >= 50),
    "gift10": ("🎁 Щедра душа", lambda u: u.get("gifts_sent_count", 0) >= 10),
    "streak5": ("🔥 На хвилі", lambda u: u.get("checkin_streak", 0) >= 5),
    "premium": ("💎 Преміум-підписник", lambda u: is_premium(u)),
}


def get_earned_achievements(u: dict) -> list[str]:
    return [title for title, check in ACHIEVEMENTS.values() if check(u)]


def today_str() -> str:
    return date.today().isoformat()


def yesterday_str() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


BTN_SEARCH = "🔍 Шукати співрозмовника"
BTN_SHOP = "🏪 Магазин"
BTN_WALLET = "👛 Гаманець"
BTN_PROFILE = "👤 Мій профіль / Архів"
BTN_SETTINGS = "⚙️ Налаштування"
BTN_STOP = "❌ Завершити чат"
BTN_REPORT = "🚨 Поскаржитися"
BTN_GIFT = "🎁 Подарувати"
BTN_DAILY = "🎁 Щоденний бонус"
BTN_LOTTERY = "🎰 Рулетка"
BTN_TOP = "🏆 Топ дарувальників"
BTN_FILTERS = "🎯 Фільтри пошуку"
BTN_BLACKLIST_ADD = "🚫 Чорний список"


# ---------------------------------------------------------------------------
# Стани
# ---------------------------------------------------------------------------
class ProfileStates(StatesGroup):
    gender = State()
    age = State()
    country = State()


class WalletStates(StatesGroup):
    change_nick = State()


class GiftStates(StatesGroup):
    amount = State()


class AdminStates(StatesGroup):
    password = State()
    ban_id = State()
    unban_id = State()
    broadcast = State()


class FilterStates(StatesGroup):
    country = State()


# ---------------------------------------------------------------------------
# Клавіатури
# ---------------------------------------------------------------------------
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SEARCH)],
            [KeyboardButton(text=BTN_SHOP), KeyboardButton(text=BTN_WALLET)],
            [KeyboardButton(text=BTN_PROFILE), KeyboardButton(text=BTN_SETTINGS)],
            [KeyboardButton(text=BTN_DAILY), KeyboardButton(text=BTN_LOTTERY)],
            [KeyboardButton(text=BTN_TOP), KeyboardButton(text=BTN_FILTERS)],
        ],
        resize_keyboard=True,
    )


def get_chat_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_GIFT)],
            [KeyboardButton(text=BTN_STOP), KeyboardButton(text=BTN_REPORT)],
            [KeyboardButton(text=BTN_BLACKLIST_ADD)],
        ],
        resize_keyboard=True,
    )


def get_gift_menu_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💰 Гроші з балансу", callback_data="gift_money")],
            [InlineKeyboardButton(text="🎁 Подарунок з інвентарю", callback_data="gift_inventory")],
            [InlineKeyboardButton(text="🛒 Магазин подарунків", callback_data="gift_catalog")],
        ]
    )


def get_gift_catalog_keyboard():
    """Магазин подарунків: купити подарунок про запас (в інвентар)."""
    rows = [
        [InlineKeyboardButton(text=f"{title} — {price} грн", callback_data=f"buygift_{key}")]
        for key, (title, price) in GIFT_CATALOG.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_gift_inventory_keyboard(u: dict):
    """Що з інвентарю можна подарувати зараз (тільки те, що є в наявності)."""
    rows = []
    for key, count in u.get("gifts", {}).items():
        if count <= 0:
            continue
        title, price = GIFT_CATALOG[key]
        rows.append(
            [InlineKeyboardButton(text=f"{title} ×{count}", callback_data=f"sendgift_{key}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_gender_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Хлопець"), KeyboardButton(text="Дівчина")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def get_shop_keyboard():
    rows = []
    for i, (key, (title, price, _, _)) in enumerate(SHOP.items(), start=1):
        rows.append(
            [InlineKeyboardButton(text=f"{i}. {title} — {price} грн", callback_data=f"buy_{key}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_wallet_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Поповнити баланс", callback_data="deposit")],
            [InlineKeyboardButton(text=f"✏️ Змінити нік ({NICK_PRICE} грн)", callback_data="buy_nick")],
            [InlineKeyboardButton(text="🎒 Інвентар подарунків", callback_data="inv_open")],
        ]
    )


def get_rating_keyboard(partner_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👍", callback_data=f"rate_up_{partner_id}"),
                InlineKeyboardButton(text="👎", callback_data=f"rate_down_{partner_id}"),
            ]
        ]
    )


def get_settings_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BTN_FILTERS, callback_data="filters_menu")],
            [InlineKeyboardButton(text="📋 Чорний список", callback_data="bl_view")],
        ]
    )


def filter_active(u: dict, perk: str) -> bool:
    """Чи доступний юзеру цей фільтр (куплений перк або Premium)."""
    return is_premium(u) or has_perk(u, perk)


def get_filters_menu_keyboard(u: dict):
    gender_on = filter_active(u, "gender_filter")
    age_on = filter_active(u, "age_filter")
    country_on = filter_active(u, "country_filter")

    gender_val = u.get("filter_gender") or "будь-яка"
    ranges = u.get("filter_age_ranges") or set()
    age_val = (
        ", ".join(label for key, label, _ in AGE_RANGES if key in ranges) if ranges else "будь-який"
    )
    country_val = u.get("filter_country") or "будь-яка"

    rows = [
        [
            InlineKeyboardButton(
                text=f"{'👫' if gender_on else '🔒'} Стать: {gender_val if gender_on else 'куплено в магазині'}",
                callback_data="f_gender_menu" if gender_on else "buy_gender_filter",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"{'🎂' if age_on else '🔒'} Вік: {age_val if age_on else 'куплено в магазині'}",
                callback_data="f_age_menu" if age_on else "buy_age_filter",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"{'🌍' if country_on else '🔒'} Країна: {country_val if country_on else 'куплено в магазині'}",
                callback_data="f_country_menu" if country_on else "buy_country_filter",
            )
        ],
        [InlineKeyboardButton(text="🔄 Скинути всі фільтри", callback_data="f_reset")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_filter_gender_keyboard(u: dict):
    current = u.get("filter_gender")
    def mark(v):
        return "✅ " if current == v else ""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{mark('Хлопець')}Хлопець", callback_data="fg_Хлопець")],
            [InlineKeyboardButton(text=f"{mark('Дівчина')}Дівчина", callback_data="fg_Дівчина")],
            [InlineKeyboardButton(text="🔄 Будь-яка (скинути)", callback_data="fg_reset")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="filters_menu")],
        ]
    )


def get_filter_age_keyboard(u: dict):
    selected = u.get("filter_age_ranges") or set()
    rows = []
    for key, label, _ in AGE_RANGES:
        mark = "✅ " if key in selected else ""
        rows.append([InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"fa_{key}")])
    rows.append([InlineKeyboardButton(text="🔄 Скинути", callback_data="fa_reset")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="filters_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_blacklist_keyboard(u: dict):
    bl = u.get("blacklist") or set()
    if not bl:
        return None
    rows = []
    for uid in sorted(bl):
        other = users_db.get(uid)
        label = other["nickname"] if other else str(uid)
        rows.append(
            [InlineKeyboardButton(text=f"❌ Прибрати: {label}", callback_data=f"bl_remove_{uid}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_admin_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")],
            [InlineKeyboardButton(text="🚨 Скарги", callback_data="adm_reports_0")],
            [
                InlineKeyboardButton(text="⛔ Забанити", callback_data="adm_ban"),
                InlineKeyboardButton(text="✅ Розбанити", callback_data="adm_unban"),
            ],
            [InlineKeyboardButton(text="📋 Список забанених", callback_data="adm_banlist")],
            [InlineKeyboardButton(text="📢 Розсилка всім", callback_data="adm_broadcast")],
        ]
    )


# ---------------------------------------------------------------------------
# Допоміжні функції
# ---------------------------------------------------------------------------
def esc(value) -> str:
    """Екранування користувацького тексту для HTML-режиму."""
    return html.escape(str(value))


def init_user(user_id: int) -> dict:
    global user_counter
    if user_id not in users_db:
        user_counter += 1
        users_db[user_id] = {
            "custom_id": f"ID_{user_counter}",
            "nickname": f"Користувач_{user_counter}",
            "gender": "Не вказано",
            "age": "Не вказано",
            "country": "Не вказано",
            "balance": 0.0,
            "perks": {},  # перк -> час закінчення (float('inf') = назавжди)
            "gifts": {},  # ключ подарунка -> кількість в інвентарі
            "archive": [],
            "last_checkin_date": None,  # дата останнього щоденного бонусу (YYYY-MM-DD)
            "checkin_streak": 0,  # скільки днів поспіль заходив за бонусом
            "total_chats": 0,  # скільки разів знайшов співрозмовника (для рівня)
            "gifts_sent_count": 0,  # скільки подарунків подарував (для досягнень)
            "referred_by": None,  # хто запросив цього користувача
            "referral_count": 0,  # скільки людей запросив сам
            "reports_received": 0,  # скільки скарг отримав (для автобану)
            "rating_up": 0,  # 👍 після чатів
            "rating_down": 0,  # 👎 після чатів
            "blacklist": set(),  # user_id, яких ця людина заблокувала особисто
            "filter_gender": None,  # бажана стать співрозмовника (None = будь-яка)
            "filter_age_ranges": set(),  # ключі з AGE_RANGES (порожньо = будь-який вік)
            "filter_country": None,  # бажана країна співрозмовника (None = будь-яка)
            "last_partner_id": None,  # останній співрозмовник (для реконнекту)
        }
    return users_db[user_id]


def has_perk(u: dict, perk: str) -> bool:
    return u["perks"].get(perk, 0) > time.time()


def is_premium(u: dict) -> bool:
    return has_perk(u, "premium")


def get_age_int(u: dict) -> int | None:
    age = u.get("age")
    return int(age) if isinstance(age, str) and age.isdigit() else None


def age_in_ranges(age: int, range_keys: set) -> bool:
    if not range_keys:
        return True
    for key, _label, pred in AGE_RANGES:
        if key in range_keys and pred(age):
            return True
    return False


def passes_filters(viewer: dict, candidate: dict) -> bool:
    """Чи підходить candidate під активні фільтри пошуку viewer (стать/вік/країна)."""
    if filter_active(viewer, "gender_filter"):
        want = viewer.get("filter_gender")
        if want and candidate.get("gender") != want:
            return False
    if filter_active(viewer, "age_filter"):
        wanted_ranges = viewer.get("filter_age_ranges") or set()
        if wanted_ranges:
            c_age = get_age_int(candidate)
            if c_age is None or not age_in_ranges(c_age, wanted_ranges):
                return False
    if filter_active(viewer, "country_filter"):
        want_country = viewer.get("filter_country")
        if want_country and candidate.get("country", "").strip().lower() != want_country.strip().lower():
            return False
    return True


def is_blacklisted(a: dict, a_id: int, b: dict, b_id: int) -> bool:
    return b_id in (a.get("blacklist") or set()) or a_id in (b.get("blacklist") or set())


def short_info(u: dict) -> str:
    badge = " 💎" if is_premium(u) else ""
    vip = " ⭐VIP" if has_perk(u, "vip_badge") else ""
    return f"{esc(u['gender'])}, {esc(u['age'])}, {esc(u['country'])}{badge}{vip}"


async def safe_send(chat_id: int, text: str, **kwargs) -> bool:
    try:
        await bot.send_message(chat_id, text, **kwargs)
        return True
    except TelegramAPIError as e:
        logging.warning("Не вдалося надіслати %s: %s", chat_id, e)
        return False


def end_chat(user_id: int):
    """Розриває чат, запам'ятовує останнього співрозмовника (для реконнекту) і повертає його ID."""
    partner_id = active_chats.pop(user_id, None)
    if partner_id is not None:
        active_chats.pop(partner_id, None)
        if user_id in users_db:
            users_db[user_id]["last_partner_id"] = partner_id
        if partner_id in users_db:
            users_db[partner_id]["last_partner_id"] = user_id
    return partner_id


def get_post_chat_keyboard(partner_id: int):
    """Оцінка співрозмовника + запит на повторний зв'язок після завершення чату."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👍", callback_data=f"rate_up_{partner_id}"),
                InlineKeyboardButton(text="👎", callback_data=f"rate_down_{partner_id}"),
            ],
            [InlineKeyboardButton(text="🔄 Запросити повторний зв'язок", callback_data="reconnect_request")],
        ]
    )


async def send_post_chat_menu(chat_id: int, partner_id: int):
    """Пропонує оцінити співрозмовника й за бажанням надіслати запит на реконект."""
    await safe_send(
        chat_id,
        "Оціни співрозмовника, будь ласка:",
        reply_markup=get_post_chat_keyboard(partner_id),
    )


# ---------------------------------------------------------------------------
# CryptoPay (@CryptoBot) — оплата криптою
# ---------------------------------------------------------------------------
async def create_crypto_invoice(user_id: int, amount_usdt: float) -> dict | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{CRYPTO_PAY_API}/createInvoice",
                headers={"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN},
                json={
                    "asset": "USDT",
                    "amount": str(amount_usdt),
                    "description": f"Поповнення балансу боту (user {user_id})",
                    "payload": str(user_id),
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
    except Exception as e:
        logging.warning("Помилка створення CryptoPay рахунку: %s", e)
        return None
    if not data.get("ok"):
        logging.warning("CryptoPay createInvoice відмова: %s", data)
        return None
    return data["result"]


async def check_and_credit_invoice(invoice_id: str) -> bool:
    """Перевіряє статус рахунку і нараховує баланс, якщо оплачено. True, якщо нарахував."""
    info = pending_crypto_invoices.get(invoice_id)
    if info is None:
        return False
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{CRYPTO_PAY_API}/getInvoices",
                headers={"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN},
                params={"invoice_ids": invoice_id},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
    except Exception as e:
        logging.warning("Помилка перевірки CryptoPay: %s", e)
        return False

    if not data.get("ok"):
        return False
    items = data["result"]["items"]
    if not items or items[0]["status"] != "paid":
        return False

    pending_crypto_invoices.pop(invoice_id, None)
    u = init_user(info["user_id"])
    u["balance"] += info["credit"]
    return True


async def crypto_poll_loop():
    """Фоново перевіряє неоплачені крипто-рахунки й нараховує баланс автоматично."""
    while True:
        await asyncio.sleep(20)
        if not CRYPTO_PAY_TOKEN or not pending_crypto_invoices:
            continue
        for invoice_id in list(pending_crypto_invoices.keys()):
            info = pending_crypto_invoices.get(invoice_id)
            if info is None:
                continue
            credited = await check_and_credit_invoice(invoice_id)
            if credited:
                await safe_send(
                    info["user_id"],
                    f"✅ Оплату {info['amount']} USDT отримано! Баланс поповнено на {info['credit']:.2f} грн.",
                )


# ---------------------------------------------------------------------------
# Команди
# ---------------------------------------------------------------------------
@dp.message(CommandStart())
async def start_handler(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    is_new_user = user_id not in users_db
    u = init_user(user_id)

    # Реферальне посилання: /start ref_<id>
    if is_new_user:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) == 2 and parts[1].startswith("ref_"):
            try:
                referrer_id = int(parts[1][len("ref_"):])
            except ValueError:
                referrer_id = None
            if referrer_id and referrer_id != user_id and referrer_id in users_db:
                u["referred_by"] = referrer_id
                ref = users_db[referrer_id]
                ref["referral_count"] = ref.get("referral_count", 0) + 1
                ref["balance"] += REFERRAL_BONUS
                await safe_send(
                    referrer_id,
                    f"👥 За вашим запрошенням приєднався новий користувач!\n"
                    f"+{REFERRAL_BONUS:.0f} грн на баланс. Дякуємо! 🎉",
                )
                if ref["referral_count"] % REFERRAL_PREMIUM_EVERY == 0:
                    now = time.time()
                    start = max(now, ref["perks"].get("premium", 0))
                    ref["perks"]["premium"] = start + REFERRAL_PREMIUM_DAYS * DAY
                    await safe_send(
                        referrer_id,
                        f"🎉 Ти запросив вже {ref['referral_count']} друзів!\n"
                        f"У подарунок — {REFERRAL_PREMIUM_DAYS} днів Premium 💎",
                    )

    await message.answer(
        f"Привіт, {esc(message.from_user.first_name)}! Вітаємо в анонімному чаті! 🤫\n\n"
        f"Твій унікальний номер: <b>{u['custom_id']}</b>\n"
        "Заповни профіль командою /edit_profile, щоб отримувати кращі рекомендації.",
        reply_markup=get_main_keyboard(),
    )


@dp.message(Command("cancel"))
async def cancel_handler(message: types.Message, state: FSMContext):
    await state.clear()
    kb = get_chat_keyboard() if message.from_user.id in active_chats else get_main_keyboard()
    await message.answer("Дію скасовано.", reply_markup=kb)


@dp.message(Command("addbalance"))
async def add_balance_admin(message: types.Message):
    """Тільки для адміна: /addbalance <user_id> <сума>"""
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    try:
        target, amount = int(parts[1]), float(parts[2])
    except (IndexError, ValueError):
        await message.answer("Формат: /addbalance <user_id> <сума>")
        return
    if target not in users_db:
        await message.answer("Такого користувача немає в базі.")
        return
    users_db[target]["balance"] += amount
    await message.answer(f"✅ Баланс {target} поповнено на {amount:.2f} грн.")
    await safe_send(target, f"💰 Ваш баланс поповнено на {amount:.2f} грн.")


# ---------------------------------------------------------------------------
# Профіль та архів
# ---------------------------------------------------------------------------
@dp.message(F.text == BTN_PROFILE)
async def profile_handler(message: types.Message, state: FSMContext):
    await state.clear()
    u = init_user(message.from_user.id)
    archive_text = esc("\n".join(u["archive"][-5:])) if u["archive"] else "Історія порожня"

    gifts = {k: c for k, c in u.get("gifts", {}).items() if c > 0}
    if gifts:
        gifts_text = ", ".join(f"{GIFT_CATALOG[k][0]} ×{c}" for k, c in gifts.items())
    else:
        gifts_text = "порожньо"

    earned = get_earned_achievements(u)
    achievements_text = ", ".join(earned) if earned else "ще немає — спілкуйся й даруй подарунки!"

    text = (
        "👤 <b>Твій профіль:</b>\n"
        f"• <b>ID / Нік:</b> {esc(u['nickname'])} ({u['custom_id']})\n"
        f"• <b>Стать:</b> {esc(u['gender'])}\n"
        f"• <b>Вік:</b> {esc(u['age'])}\n"
        f"• <b>Країна:</b> {esc(u['country'])}\n"
        f"• <b>Преміум:</b> {'Так 💎' if is_premium(u) else 'Ні ❌'}\n"
        f"• <b>VIP-значок:</b> {'Так ⭐' if has_perk(u, 'vip_badge') else 'Ні'}\n"
        f"• <b>Рівень:</b> {get_level_title(u)} ({u.get('total_chats', 0)} чатів)\n"
        f"• <b>Серія входів:</b> {u.get('checkin_streak', 0)} 🔥\n\n"
        f"🏆 <b>Досягнення:</b> {achievements_text}\n\n"
        f"🎒 <b>Інвентар подарунків:</b> {gifts_text}\n\n"
        f"📜 <b>Архів останніх змін профілю:</b>\n{archive_text}\n\n"
        "Хочеш оновити дані? Введи /edit_profile"
    )
    await message.answer(text)


@dp.message(Command("edit_profile"))
async def edit_profile_start(message: types.Message, state: FSMContext):
    init_user(message.from_user.id)
    await message.answer("Вкажи свою стать (Хлопець / Дівчина):", reply_markup=get_gender_keyboard())
    await state.set_state(ProfileStates.gender)


@dp.message(ProfileStates.gender, F.text)
async def process_gender(message: types.Message, state: FSMContext):
    gender = message.text.strip()
    if gender not in ("Хлопець", "Дівчина"):
        await message.answer("Оберіть варіант кнопкою: Хлопець або Дівчина.")
        return
    await state.update_data(gender=gender)
    await message.answer("Вкажи свій вік (числом, наприклад 18):", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(ProfileStates.age)


@dp.message(ProfileStates.age, F.text)
async def process_age(message: types.Message, state: FSMContext):
    age = message.text.strip()
    if not age.isdigit() or not (10 <= int(age) <= 99):
        await message.answer("Введіть вік числом від 10 до 99.")
        return
    await state.update_data(age=age)
    await message.answer("Вкажи свою країну або місто:")
    await state.set_state(ProfileStates.country)


@dp.message(ProfileStates.country, F.text)
async def process_country(message: types.Message, state: FSMContext):
    country = message.text.strip()[:50]
    data = await state.get_data()
    u = init_user(message.from_user.id)

    stamp = time.strftime("%d.%m.%Y %H:%M")
    u["archive"].append(f"{stamp} — Стать: {u['gender']}, Вік: {u['age']}, Країна: {u['country']}")
    u["archive"] = u["archive"][-50:]

    u["gender"] = data.get("gender", u["gender"])
    u["age"] = data.get("age", u["age"])
    u["country"] = country

    await state.clear()
    kb = get_chat_keyboard() if message.from_user.id in active_chats else get_main_keyboard()
    await message.answer("✅ Профіль оновлено! Попередні дані збережено в архів.", reply_markup=kb)


# ---------------------------------------------------------------------------
# Налаштування
# ---------------------------------------------------------------------------
@dp.message(F.text == BTN_SETTINGS)
async def settings_handler(message: types.Message, state: FSMContext):
    await state.clear()
    u = init_user(message.from_user.id)
    ref_link = (
        f"https://t.me/{BOT_USERNAME}?start=ref_{message.from_user.id}"
        if BOT_USERNAME
        else "(посилання буде доступне трохи пізніше)"
    )
    await message.answer(
        "⚙️ <b>Налаштування</b>\n\n"
        "/edit_profile — змінити профіль\n"
        "/cancel — скасувати поточну дію\n\n"
        "👥 <b>Запрошуй друзів і заробляй:</b>\n"
        f"За кожного друга, що запустить бота за твоїм посиланням — "
        f"+{REFERRAL_BONUS:.0f} грн на баланс.\n"
        f"А кожні {REFERRAL_PREMIUM_EVERY} запрошених — {REFERRAL_PREMIUM_DAYS} днів Premium у подарунок 💎\n"
        f"Твоє посилання:\n<code>{esc(ref_link)}</code>\n"
        f"Запрошено людей: {u.get('referral_count', 0)}",
        reply_markup=get_settings_keyboard(),
    )


# ---------------------------------------------------------------------------
# Фільтри пошуку (стать / вік / країна)
# ---------------------------------------------------------------------------
@dp.message(F.text == BTN_FILTERS)
@dp.message(Command("filters"))
async def filters_command(message: types.Message, state: FSMContext):
    await state.clear()
    u = init_user(message.from_user.id)
    await message.answer(
        "🎯 <b>Фільтри пошуку</b>\n\n"
        "Обери, яких співрозмовників шукати. Фільтри, позначені 🔒, "
        "потрібно спочатку розблокувати в магазині (або купити Premium — тоді доступні всі).",
        reply_markup=get_filters_menu_keyboard(u),
    )


@dp.callback_query(F.data == "filters_menu")
async def filters_menu_cb(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    u = init_user(call.from_user.id)
    try:
        await call.message.edit_text(
            "🎯 <b>Фільтри пошуку</b>\n\nОбери, яких співрозмовників шукати:",
            reply_markup=get_filters_menu_keyboard(u),
        )
    except TelegramAPIError:
        await call.message.answer(
            "🎯 <b>Фільтри пошуку</b>\n\nОбери, яких співрозмовників шукати:",
            reply_markup=get_filters_menu_keyboard(u),
        )
    await call.answer()


@dp.callback_query(F.data == "f_gender_menu")
async def f_gender_menu(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    if not filter_active(u, "gender_filter"):
        await call.answer("Спочатку розблокуй цей фільтр у магазині", show_alert=True)
        return
    await call.message.edit_text(
        "👫 Яку стать співрозмовника шукати?", reply_markup=get_filter_gender_keyboard(u)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("fg_"))
async def fg_set(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    if not filter_active(u, "gender_filter"):
        await call.answer("Спочатку розблокуй цей фільтр у магазині", show_alert=True)
        return
    value = call.data[len("fg_"):]
    u["filter_gender"] = None if value == "reset" else value
    await call.message.edit_text(
        "👫 Яку стать співрозмовника шукати?", reply_markup=get_filter_gender_keyboard(u)
    )
    await call.answer("Збережено ✅")


@dp.callback_query(F.data == "f_age_menu")
async def f_age_menu(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    if not filter_active(u, "age_filter"):
        await call.answer("Спочатку розблокуй цей фільтр у магазині", show_alert=True)
        return
    await call.message.edit_text(
        "🎂 Обери бажані діапазони віку (можна декілька):", reply_markup=get_filter_age_keyboard(u)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("fa_"))
async def fa_toggle(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    if not filter_active(u, "age_filter"):
        await call.answer("Спочатку розблокуй цей фільтр у магазині", show_alert=True)
        return
    value = call.data[len("fa_"):]
    u.setdefault("filter_age_ranges", set())
    if value == "reset":
        u["filter_age_ranges"] = set()
    elif value in u["filter_age_ranges"]:
        u["filter_age_ranges"].discard(value)
    else:
        u["filter_age_ranges"].add(value)
    await call.message.edit_text(
        "🎂 Обери бажані діапазони віку (можна декілька):", reply_markup=get_filter_age_keyboard(u)
    )
    await call.answer("Збережено ✅")


@dp.callback_query(F.data == "f_country_menu")
async def f_country_menu(call: types.CallbackQuery, state: FSMContext):
    u = init_user(call.from_user.id)
    if not filter_active(u, "country_filter"):
        await call.answer("Спочатку розблокуй цей фільтр у магазині", show_alert=True)
        return
    current = u.get("filter_country") or "будь-яка"
    await call.message.answer(
        f"🌍 Поточний фільтр країни: <b>{esc(current)}</b>\n\n"
        "Введи назву країни (як у профілі), яку шукати, або /cancel.\n"
        "Щоб скинути — напиши «скинути»."
    )
    await state.set_state(FilterStates.country)
    await call.answer()


@dp.message(FilterStates.country, F.text)
async def f_country_set(message: types.Message, state: FSMContext):
    await state.clear()
    u = init_user(message.from_user.id)
    text = message.text.strip()
    if text.lower() in ("скинути", "/reset"):
        u["filter_country"] = None
        await message.answer("🔄 Фільтр країни скинуто.", reply_markup=get_main_keyboard())
        return
    u["filter_country"] = text[:50]
    await message.answer(
        f"✅ Фільтр країни встановлено: <b>{esc(u['filter_country'])}</b>", reply_markup=get_main_keyboard()
    )


@dp.callback_query(F.data == "f_reset")
async def f_reset(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    u["filter_gender"] = None
    u["filter_age_ranges"] = set()
    u["filter_country"] = None
    await call.message.edit_text(
        "🎯 <b>Фільтри пошуку</b>\n\nВсі фільтри скинуто. Обери, яких співрозмовників шукати:",
        reply_markup=get_filters_menu_keyboard(u),
    )
    await call.answer("Скинуто ✅")


# ---------------------------------------------------------------------------
# Чорний список
# ---------------------------------------------------------------------------
@dp.callback_query(F.data == "bl_view")
async def bl_view(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    kb = get_blacklist_keyboard(u)
    if kb is None:
        await call.message.answer("📋 Твій чорний список порожній.")
        await call.answer()
        return
    limit = BLACKLIST_LIMIT_PLUS if (is_premium(u) or has_perk(u, "blacklist")) else BLACKLIST_LIMIT_FREE
    await call.message.answer(
        f"📋 <b>Чорний список</b> ({len(u.get('blacklist', set()))}/{limit})\n\n"
        "Ці користувачі більше не з'являться в твоєму пошуку. Натисни, щоб прибрати когось зі списку:",
        reply_markup=kb,
    )
    await call.answer()


@dp.callback_query(F.data.startswith("bl_remove_"))
async def bl_remove(call: types.CallbackQuery):
    target_id = int(call.data[len("bl_remove_"):])
    u = init_user(call.from_user.id)
    u.get("blacklist", set()).discard(target_id)
    kb = get_blacklist_keyboard(u)
    if kb is None:
        await call.message.edit_text("📋 Твій чорний список порожній.")
    else:
        await call.message.edit_reply_markup(reply_markup=kb)
    await call.answer("Прибрано ✅")


@dp.message(F.text == BTN_BLACKLIST_ADD)
async def blacklist_add_in_chat(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    partner_id = active_chats.get(user_id)
    if partner_id is None:
        await message.answer(
            "Додавати в чорний список можна лише під час чату.", reply_markup=get_main_keyboard()
        )
        return

    u = init_user(user_id)
    u.setdefault("blacklist", set())
    limit = BLACKLIST_LIMIT_PLUS if (is_premium(u) or has_perk(u, "blacklist")) else BLACKLIST_LIMIT_FREE
    if partner_id not in u["blacklist"] and len(u["blacklist"]) >= limit:
        await message.answer(
            f"❌ Чорний список заповнено ({limit}). Розшир його в магазині або онови Premium."
        )
        return

    u["blacklist"].add(partner_id)
    end_chat(user_id)
    await message.answer(
        "🚫 Користувача додано в чорний список — більше не з'явиться в пошуку. Чат завершено.",
        reply_markup=get_main_keyboard(),
    )
    await safe_send(partner_id, "Співрозмовник завершив чат.", reply_markup=get_main_keyboard())


# ---------------------------------------------------------------------------
# Щоденний бонус
# ---------------------------------------------------------------------------
@dp.message(F.text == BTN_DAILY)
async def daily_checkin(message: types.Message, state: FSMContext):
    await state.clear()
    u = init_user(message.from_user.id)
    today = today_str()

    if u.get("last_checkin_date") == today:
        streak = u.get("checkin_streak", 0)
        left = DAILY_GIFT_EVERY - (streak % DAILY_GIFT_EVERY)
        await message.answer(
            f"✅ Бонус за сьогодні вже отримано. Серія: {streak} 🔥\n"
            f"Повертайся завтра! До подарунка дня лишилось: {left}."
        )
        return

    if u.get("last_checkin_date") == yesterday_str():
        u["checkin_streak"] = u.get("checkin_streak", 0) + 1
    else:
        u["checkin_streak"] = 1  # серія перервалась або це перший вхід

    u["last_checkin_date"] = today
    streak = u["checkin_streak"]

    if streak % DAILY_GIFT_EVERY == 0:
        cheap_keys = [k for k, (_, p) in GIFT_CATALOG.items() if p <= DAILY_GIFT_MAX_PRICE]
        key = random.choice(cheap_keys)
        title, _price = GIFT_CATALOG[key]
        u.setdefault("gifts", {})
        u["gifts"][key] = u["gifts"].get(key, 0) + 1
        await message.answer(
            f"🔥 Серія {streak} днів поспіль!\n"
            f"🎁 Подарунок дня: <b>{esc(title)}</b> додано в інвентар!"
        )
    else:
        u["balance"] += DAILY_BONUS_AMOUNT
        left = DAILY_GIFT_EVERY - (streak % DAILY_GIFT_EVERY)
        await message.answer(
            f"✅ Щоденний бонус: +{DAILY_BONUS_AMOUNT:.0f} грн.\n"
            f"Серія: {streak} 🔥 (до подарунка лишилось {left} дн.)"
        )


# ---------------------------------------------------------------------------
# Гаманець
# ---------------------------------------------------------------------------
@dp.message(F.text == BTN_WALLET)
async def wallet_handler(message: types.Message, state: FSMContext):
    await state.clear()
    u = init_user(message.from_user.id)
    await message.answer(
        "👛 <b>Твій гаманець</b>\n\n"
        f"• <b>Баланс:</b> {u['balance']:.2f} грн\n"
        f"• <b>Нік:</b> {esc(u['nickname'])}",
        reply_markup=get_wallet_keyboard(),
    )


@dp.callback_query(F.data == "deposit")
async def deposit_start(call: types.CallbackQuery):
    buttons = [[InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="dep_stars")]]
    if CRYPTO_PAY_TOKEN:
        buttons.append([InlineKeyboardButton(text="💎 Крипта (USDT/TON)", callback_data="dep_crypto")])
    await call.message.answer(
        "💳 Оберіть спосіб поповнення балансу:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await call.answer()


@dp.callback_query(F.data == "dep_stars")
async def dep_stars_menu(call: types.CallbackQuery):
    buttons = [
        [InlineKeyboardButton(text=f"⭐ {a} → {a} грн", callback_data=f"dep_stars_{a}")]
        for a in STAR_PACKAGES
    ]
    await call.message.answer(
        "⭐ Оберіть пакет Telegram Stars:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("dep_stars_"))
async def dep_stars_pay(call: types.CallbackQuery):
    amount = int(call.data.rsplit("_", 1)[-1])
    await bot.send_invoice(
        chat_id=call.message.chat.id,
        title=f"Поповнення на {amount} грн",
        description=f"Купівля {amount} Telegram Stars для поповнення балансу бота",
        payload=f"stars_{amount}_{call.from_user.id}",
        provider_token="",  # для Stars (валюта XTR) токен провайдера не потрібен
        currency="XTR",
        prices=[types.LabeledPrice(label=f"{amount} Stars", amount=amount)],
    )
    await call.answer()


@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    payment = message.successful_payment
    if payment.currency != "XTR":
        return
    amount = payment.total_amount  # для XTR це кількість зірок напряму
    u = init_user(message.from_user.id)
    u["balance"] += amount
    await message.answer(
        f"✅ Оплату отримано! Баланс поповнено на {amount} грн.", reply_markup=get_main_keyboard()
    )


@dp.callback_query(F.data == "dep_crypto")
async def dep_crypto_menu(call: types.CallbackQuery):
    if not CRYPTO_PAY_TOKEN:
        await call.answer("Оплата криптою зараз недоступна", show_alert=True)
        return
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{a} USDT (~{a * USD_UAH_RATE:.0f} грн)", callback_data=f"dep_crypto_{a}"
            )
        ]
        for a in CRYPTO_PACKAGES
    ]
    await call.message.answer(
        "💎 Оберіть суму поповнення в USDT:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("dep_crypto_"))
async def dep_crypto_create(call: types.CallbackQuery):
    amount = float(call.data.rsplit("_", 1)[-1])
    result = await create_crypto_invoice(call.from_user.id, amount)
    if result is None:
        await call.message.answer("❌ Не вдалося створити рахунок. Спробуйте пізніше.")
        await call.answer()
        return

    invoice_id = str(result["invoice_id"])
    pay_url = result.get("pay_url") or result.get("bot_invoice_url") or result.get("mini_app_invoice_url")
    credit = amount * USD_UAH_RATE
    pending_crypto_invoices[invoice_id] = {
        "user_id": call.from_user.id,
        "amount": amount,
        "credit": credit,
    }

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатити", url=pay_url)],
            [InlineKeyboardButton(text="✅ Перевірити оплату", callback_data=f"dep_check_{invoice_id}")],
        ]
    )
    await call.message.answer(
        f"Рахунок на {amount} USDT створено.\n"
        f"Після оплати баланс поповниться автоматично (~{credit:.0f} грн), "
        "або натисніть «Перевірити оплату».",
        reply_markup=kb,
    )
    await call.answer()


@dp.callback_query(F.data.startswith("dep_check_"))
async def dep_check(call: types.CallbackQuery):
    invoice_id = call.data[len("dep_check_"):]
    if invoice_id not in pending_crypto_invoices:
        await call.answer("Рахунок вже оброблено або не знайдено", show_alert=True)
        return
    credited = await check_and_credit_invoice(invoice_id)
    if credited:
        await call.message.answer("✅ Оплату підтверджено, баланс поповнено!")
        await call.answer()
    else:
        await call.answer("Оплату ще не отримано. Спробуйте за хвилину.", show_alert=True)


async def start_nick_change(call: types.CallbackQuery, state: FSMContext):
    u = init_user(call.from_user.id)
    if u["balance"] < NICK_PRICE:
        await call.message.answer(
            f"❌ Недостатньо коштів. Зміна ніку коштує {NICK_PRICE} грн, на балансі {u['balance']:.2f} грн."
        )
    else:
        await call.message.answer("Введіть новий нікнейм (до 24 символів) або /cancel:")
        await state.set_state(WalletStates.change_nick)


@dp.message(WalletStates.change_nick, F.text)
async def change_nick_finish(message: types.Message, state: FSMContext):
    u = init_user(message.from_user.id)
    nick = message.text.strip()
    if not (2 <= len(nick) <= 24):
        await message.answer("Нікнейм має містити від 2 до 24 символів. Спробуйте ще раз або /cancel.")
        return
    if u["balance"] < NICK_PRICE:
        await state.clear()
        await message.answer("❌ Недостатньо коштів.")
        return
    u["balance"] -= NICK_PRICE
    u["nickname"] = nick
    await state.clear()
    await message.answer(f"🎉 Нікнейм змінено на <b>{esc(nick)}</b> (списано {NICK_PRICE} грн).")


# ---------------------------------------------------------------------------
# Магазин
# ---------------------------------------------------------------------------
@dp.message(F.text == BTN_SHOP)
async def shop_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏪 <b>Магазин послуг та товарів</b>\n\nОберіть позицію для купівлі:",
        reply_markup=get_shop_keyboard(),
    )
    await message.answer(
        "🎁 <b>Магазин подарунків</b>\n\n"
        "Купи подарунок про запас — даруватимеш його співрозмовникам у чаті, "
        f"коли захочеш. Отримувач одразу отримає {GIFT_RECIPIENT_SHARE * 100:.0f}% "
        "вартості на баланс.",
        reply_markup=get_gift_catalog_keyboard(),
    )


@dp.callback_query(F.data.startswith("buy_"))
async def buy_item(call: types.CallbackQuery, state: FSMContext):
    key = call.data[4:]
    item = SHOP.get(key)
    if item is None:
        await call.answer("Невідомий товар", show_alert=True)
        return

    if key == "nick":
        await start_nick_change(call, state)
        await call.answer()
        return

    title, price, perk, seconds = item
    u = init_user(call.from_user.id)

    if u["balance"] < price:
        await call.message.answer(
            f"❌ Недостатньо коштів. Ціна: {price} грн. Ваш баланс: {u['balance']:.2f} грн."
        )
        await call.answer()
        return

    u["balance"] -= price
    now = time.time()
    if seconds is None:
        u["perks"][perk] = float("inf")
    else:
        start = max(now, u["perks"].get(perk, 0))  # продовжуємо, а не перезаписуємо
        u["perks"][perk] = start + seconds

    await call.message.answer(f"🎉 Куплено: <b>{esc(title)}</b>. Списано {price} грн.")
    await call.answer()


@dp.callback_query(F.data.startswith("buygift_"))
async def buy_gift_item(call: types.CallbackQuery):
    """Купівля подарунка про запас (в інвентар), без прив'язки до співрозмовника."""
    key = call.data[len("buygift_"):]
    item = GIFT_CATALOG.get(key)
    if item is None:
        await call.answer("Невідомий подарунок", show_alert=True)
        return

    title, price = item
    u = init_user(call.from_user.id)

    if u["balance"] < price:
        await call.message.answer(
            f"❌ Недостатньо коштів. Ціна: {price} грн. Ваш баланс: {u['balance']:.2f} грн."
        )
        await call.answer()
        return

    u["balance"] -= price
    u.setdefault("gifts", {})
    u["gifts"][key] = u["gifts"].get(key, 0) + 1

    # Якщо зараз в активному чаті — пропонуємо подарувати щойно куплене одразу
    if call.from_user.id in active_chats:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"🎁 Подарувати {title} зараз", callback_data=f"sendgift_{key}")]
            ]
        )
        await call.message.answer(
            f"🎁 Куплено: <b>{esc(title)}</b>! Додано в інвентар.", reply_markup=kb
        )
    else:
        await call.message.answer(
            f"🎁 Куплено: <b>{esc(title)}</b>! Тепер у твоєму інвентарі. "
            "Подарувати його можна будь-якому співрозмовнику під час чату."
        )
    await call.answer()


@dp.callback_query(F.data == "inv_open")
async def inventory_open(call: types.CallbackQuery):
    """Інвентар подарунків: перегляд і продаж назад (частковий викуп)."""
    u = init_user(call.from_user.id)
    owned = {k: c for k, c in u.get("gifts", {}).items() if c > 0}
    if not owned:
        await call.message.answer(
            "🎒 Твій інвентар порожній.\nКупити подарунок можна в магазині — 🏪 Магазин.",
        )
        await call.answer()
        return

    rows = []
    for key, count in owned.items():
        title, price = GIFT_CATALOG[key]
        sellback = round(price * GIFT_SELLBACK_SHARE, 2)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{title} ×{count} — продати за {sellback:.0f} грн",
                    callback_data=f"sellgift_{key}",
                )
            ]
        )
    await call.message.answer(
        "🎒 <b>Інвентар подарунків</b>\n\n"
        f"Продаж подарунка назад повертає {GIFT_SELLBACK_SHARE * 100:.0f}% його ціни на баланс.\n"
        "Обери, що продати:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("sellgift_"))
async def sell_gift_item(call: types.CallbackQuery):
    """Продаж подарунка з інвентарю назад системі за частину ціни."""
    key = call.data[len("sellgift_"):]
    item = GIFT_CATALOG.get(key)
    if item is None:
        await call.answer("Невідомий подарунок", show_alert=True)
        return

    u = init_user(call.from_user.id)
    have = u.get("gifts", {}).get(key, 0)
    if have <= 0:
        await call.answer("У тебе немає такого подарунка", show_alert=True)
        return

    title, price = item
    sellback = round(price * GIFT_SELLBACK_SHARE, 2)
    u["gifts"][key] = have - 1
    u["balance"] += sellback

    await call.message.answer(
        f"✅ Продано: <b>{esc(title)}</b>. На баланс нараховано {sellback:.2f} грн."
    )
    await call.answer()


# ---------------------------------------------------------------------------
# Рулетка (лотерея за грн)
# ---------------------------------------------------------------------------
def get_lottery_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🎲 Крутити", callback_data="lottery_spin")]]
    )


@dp.message(F.text == BTN_LOTTERY)
async def lottery_menu(message: types.Message, state: FSMContext):
    await state.clear()
    u = init_user(message.from_user.id)
    await message.answer(
        "🎰 <b>Рулетка</b>\n\n"
        f"Один спін коштує {LOTTERY_COST:.0f} грн.\n"
        "Можливі призи: трохи грошей, великий виграш грошей або випадковий подарунок!\n"
        f"Твій баланс: {u['balance']:.2f} грн.",
        reply_markup=get_lottery_keyboard(),
    )


@dp.callback_query(F.data == "lottery_spin")
async def lottery_spin_handler(call: types.CallbackQuery):
    global lottery_revenue_total
    u = init_user(call.from_user.id)

    if u["balance"] < LOTTERY_COST:
        await call.answer(
            f"❌ Недостатньо коштів. Ціна спіну: {LOTTERY_COST:.0f} грн.", show_alert=True
        )
        return

    u["balance"] -= LOTTERY_COST
    lottery_revenue_total += LOTTERY_COST

    kind, mult = spin_lottery()
    if kind == "nothing":
        text = "😔 На цей раз нічого не випало. Спробуй ще раз!"
    elif kind == "money":
        win = round(LOTTERY_COST * mult, 2)
        u["balance"] += win
        lottery_revenue_total -= win
        text = f"🎉 Виграш: <b>{win:.2f} грн</b>! Зараховано на баланс."
    else:  # gift
        key = random.choice(list(GIFT_CATALOG.keys()))
        title, gift_price = GIFT_CATALOG[key]
        u.setdefault("gifts", {})
        u["gifts"][key] = u["gifts"].get(key, 0) + 1
        lottery_revenue_total -= gift_price
        text = f"🎁 Виграш: подарунок <b>{esc(title)}</b>! Додано в інвентар."

    text += f"\n\n💰 Баланс: {u['balance']:.2f} грн."
    await call.message.answer(text, reply_markup=get_lottery_keyboard())
    await call.answer()


# ---------------------------------------------------------------------------
# Пошук та чат
# ---------------------------------------------------------------------------
@dp.message(F.text == BTN_SEARCH)
async def search_partner(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id

    if user_id in banned_users:
        await message.answer("⛔ Вас заблоковано в цьому боті.")
        return

    u = init_user(user_id)

    if user_id in active_chats:
        await message.answer("Ти вже перебуваєш у чаті!", reply_markup=get_chat_keyboard())
        return
    if user_id in queue:
        await message.answer("Ти вже в черзі пошуку. Зачекай трохи... ⏳")
        return

    match_index = None
    for i, candidate_id in enumerate(queue):
        p_candidate = init_user(candidate_id)
        if is_blacklisted(u, user_id, p_candidate, candidate_id):
            continue
        if not passes_filters(u, p_candidate) or not passes_filters(p_candidate, u):
            continue
        match_index = i
        break

    if match_index is not None:
        partner_id = queue.pop(match_index)
        active_chats[user_id] = partner_id
        active_chats[partner_id] = user_id
        p = init_user(partner_id)

        ok_partner = await safe_send(
            partner_id, f"Партнера знайдено! 🤫\nІнфо: {short_info(u)}", reply_markup=get_chat_keyboard()
        )
        if not ok_partner:
            # співрозмовник заблокував бота — відкочуємо і ставимо користувача в чергу
            end_chat(user_id)
            queue.append(user_id)
            await message.answer("Співрозмовник виявився недоступним. Шукаємо далі... ⏳")
            return

        u["total_chats"] = u.get("total_chats", 0) + 1
        p["total_chats"] = p.get("total_chats", 0) + 1

        await message.answer(
            f"Партнера знайдено! 🤫\nІнфо: {short_info(p)}", reply_markup=get_chat_keyboard()
        )
    else:
        if has_perk(u, "priority") or is_premium(u):
            queue.insert(0, user_id)
        else:
            queue.append(user_id)
        await message.answer("Шукаємо співрозмовника... Зачекай ⏳")


@dp.message(F.text == BTN_STOP)
@dp.message(Command("stop"))
async def stop_chat(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id

    if user_id in queue:
        queue.remove(user_id)
        await message.answer("Пошук зупинено.", reply_markup=get_main_keyboard())
        return

    partner_id = end_chat(user_id)
    if partner_id is None:
        await message.answer("Ти зараз не в чаті.", reply_markup=get_main_keyboard())
        return

    await message.answer("Чат завершено.", reply_markup=get_main_keyboard())
    await safe_send(partner_id, "Співрозмовник завершив чат.", reply_markup=get_main_keyboard())
    await send_post_chat_menu(user_id, partner_id)
    await send_post_chat_menu(partner_id, user_id)


@dp.callback_query(F.data.startswith("rate_up_"))
async def rate_up_handler(call: types.CallbackQuery):
    target_id = int(call.data[len("rate_up_"):])
    u = init_user(target_id)
    u["rating_up"] = u.get("rating_up", 0) + 1
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await call.answer("Дякуємо за оцінку! 👍")


@dp.callback_query(F.data.startswith("rate_down_"))
async def rate_down_handler(call: types.CallbackQuery):
    target_id = int(call.data[len("rate_down_"):])
    u = init_user(target_id)
    u["rating_down"] = u.get("rating_down", 0) + 1
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await call.answer("Дякуємо за оцінку! 👎")


# ---------------------------------------------------------------------------
# Реконнект з минулим співрозмовником (за згодою обох сторін)
# ---------------------------------------------------------------------------
@dp.callback_query(F.data == "reconnect_request")
async def reconnect_request(call: types.CallbackQuery):
    user_id = call.from_user.id
    u = init_user(user_id)
    target_id = u.get("last_partner_id")

    if not target_id or target_id not in users_db:
        await call.answer("Немає з ким відновлювати зв'язок.", show_alert=True)
        return
    if user_id in banned_users or target_id in banned_users:
        await call.answer("Недоступно.", show_alert=True)
        return
    if user_id in active_chats or target_id in active_chats:
        await call.answer("Хтось із вас зараз уже в іншому чаті.", show_alert=True)
        return

    p = init_user(target_id)
    if is_blacklisted(u, user_id, p, target_id):
        await call.answer("Недоступно.", show_alert=True)
        return

    reconnect_requests[target_id] = user_id
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Прийняти", callback_data="reconnect_accept"),
                InlineKeyboardButton(text="❌ Відхилити", callback_data="reconnect_decline"),
            ]
        ]
    )
    ok = await safe_send(
        target_id, "🔄 Твій минулий співрозмовник хоче поновити чат. Прийняти?", reply_markup=kb
    )
    if ok:
        await call.answer("Запит надіслано! Чекай на відповідь.", show_alert=True)
    else:
        reconnect_requests.pop(target_id, None)
        await call.answer("Не вдалося надіслати запит — співрозмовник недоступний.", show_alert=True)


@dp.callback_query(F.data == "reconnect_accept")
async def reconnect_accept(call: types.CallbackQuery):
    acceptor_id = call.from_user.id
    requester_id = reconnect_requests.pop(acceptor_id, None)

    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass

    if requester_id is None:
        await call.answer("Запит уже неактуальний.", show_alert=True)
        return
    if requester_id in active_chats or acceptor_id in active_chats:
        await call.answer("Хтось із вас вже в іншому чаті.", show_alert=True)
        return
    if requester_id in banned_users or acceptor_id in banned_users:
        await call.answer("Недоступно.", show_alert=True)
        return

    if requester_id in queue:
        queue.remove(requester_id)
    if acceptor_id in queue:
        queue.remove(acceptor_id)

    active_chats[requester_id] = acceptor_id
    active_chats[acceptor_id] = requester_id

    await call.message.answer("✅ Чат відновлено!", reply_markup=get_chat_keyboard())
    await safe_send(
        requester_id, "✅ Співрозмовник прийняв запит — чат відновлено!", reply_markup=get_chat_keyboard()
    )
    await call.answer()


@dp.callback_query(F.data == "reconnect_decline")
async def reconnect_decline(call: types.CallbackQuery):
    acceptor_id = call.from_user.id
    requester_id = reconnect_requests.pop(acceptor_id, None)

    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass

    if requester_id is not None:
        await safe_send(requester_id, "❌ Співрозмовник відхилив запит на повторний зв'язок.")
    await call.answer("Відхилено.")


# ---------------------------------------------------------------------------
# Лідерборд найщедріших дарувальників
# ---------------------------------------------------------------------------
async def show_top_gifters(message: types.Message):
    top = sorted(
        users_db.items(), key=lambda kv: kv[1].get("gifts_sent_count", 0), reverse=True
    )[:10]
    top = [(uid, u) for uid, u in top if u.get("gifts_sent_count", 0) > 0]

    if not top:
        await message.answer("Поки що ніхто не дарував подарунків. Будь першим! 🎁")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Топ дарувальників подарунків</b>\n"]
    for i, (uid, u) in enumerate(top, start=1):
        rank = medals[i - 1] if i <= 3 else f"{i}."
        lines.append(f"{rank} {esc(u['nickname'])} — {u.get('gifts_sent_count', 0)} 🎁")
    await message.answer("\n".join(lines))


@dp.message(F.text == BTN_TOP)
@dp.message(Command("top"))
async def top_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await show_top_gifters(message)


@dp.message(F.text == BTN_REPORT)
async def report_handler(message: types.Message):
    # Раніше ця кнопка не мала обробника, і текст "🚨 Поскаржитися" пересилався співрозмовнику.
    global report_counter
    user_id = message.from_user.id
    partner_id = active_chats.get(user_id)
    if partner_id is None:
        await message.answer("Скарга можлива лише під час чату.", reply_markup=get_main_keyboard())
        return

    report_counter += 1
    reports.append(
        {
            "id": report_counter,
            "from": user_id,
            "on": partner_id,
            "time": time.strftime("%d.%m.%Y %H:%M"),
            "status": "нова",
        }
    )

    p = init_user(partner_id)
    p["reports_received"] = p.get("reports_received", 0) + 1
    auto_banned = False
    if p["reports_received"] >= AUTO_BAN_REPORTS and partner_id not in banned_users:
        banned_users.add(partner_id)
        auto_banned = True
        if partner_id in queue:
            queue.remove(partner_id)
        await safe_send(
            partner_id, f"⛔ Вас автоматично заблоковано після {AUTO_BAN_REPORTS} скарг."
        )

    if ADMIN_ID:
        extra = (
            f"\n\n⛔ Автобан: досягнуто {AUTO_BAN_REPORTS} скарг, користувача заблоковано автоматично."
            if auto_banned
            else ""
        )
        await safe_send(
            ADMIN_ID,
            f"🚨 Нова скарга #{report_counter}\nВід: <code>{user_id}</code>\nНа: <code>{partner_id}</code>{extra}\n\n"
            "Переглянути список: /admin",
        )
    end_chat(user_id)
    await message.answer(
        "🚨 Скаргу надіслано, чат завершено. Дякуємо!", reply_markup=get_main_keyboard()
    )
    await safe_send(partner_id, "Співрозмовник завершив чат.", reply_markup=get_main_keyboard())


# ---------------------------------------------------------------------------
# Подарунки співрозмовнику (тільки під час активного чату)
# ---------------------------------------------------------------------------
@dp.message(F.text == BTN_GIFT)
async def gift_menu(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if user_id not in active_chats:
        await message.answer("Дарувати можна лише співрозмовнику під час чату.", reply_markup=get_main_keyboard())
        return
    await message.answer("🎁 Що подаруєш співрозмовнику?", reply_markup=get_gift_menu_keyboard())


@dp.callback_query(F.data == "gift_money")
async def gift_money_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in active_chats:
        await call.answer("Чат вже завершено", show_alert=True)
        return
    await call.message.answer("Введіть суму в грн, яку хочете подарувати (або /cancel):")
    await state.set_state(GiftStates.amount)
    await call.answer()


@dp.message(GiftStates.amount, F.text)
async def gift_money_finish(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    partner_id = active_chats.get(user_id)
    if partner_id is None:
        await message.answer("Чат вже завершено.", reply_markup=get_main_keyboard())
        return

    try:
        amount = float(message.text.strip().replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введіть додатне число, наприклад 20 або 15.5.")
        return

    u = init_user(user_id)
    if u["balance"] < amount:
        await message.answer(f"❌ Недостатньо коштів. Ваш баланс: {u['balance']:.2f} грн.")
        return

    u["balance"] -= amount
    p = init_user(partner_id)
    p["balance"] += amount

    await message.answer(
        f"🎁 Подаровано {amount:.2f} грн співрозмовнику!", reply_markup=get_chat_keyboard()
    )
    await safe_send(partner_id, f"🎁 Співрозмовник подарував вам {amount:.2f} грн!")


@dp.callback_query(F.data == "gift_catalog")
async def gift_catalog_in_chat(call: types.CallbackQuery):
    if call.from_user.id not in active_chats:
        await call.answer("Чат вже завершено", show_alert=True)
        return
    await call.message.answer(
        "🛒 <b>Магазин подарунків</b>\n\nКупи подарунок — одразу запропоную подарувати його співрозмовнику:",
        reply_markup=get_gift_catalog_keyboard(),
    )
    await call.answer()


@dp.callback_query(F.data == "gift_inventory")
async def gift_inventory_menu(call: types.CallbackQuery):
    if call.from_user.id not in active_chats:
        await call.answer("Чат вже завершено", show_alert=True)
        return
    u = init_user(call.from_user.id)
    kb = get_gift_inventory_keyboard(u)
    if not kb.inline_keyboard:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🛒 Відкрити магазин подарунків", callback_data="gift_catalog")]
            ]
        )
        await call.message.answer(
            "У тебе ще немає подарунків в інвентарі.", reply_markup=kb
        )
        await call.answer()
        return
    await call.message.answer("🎁 Що подаруєш співрозмовнику зі свого інвентарю?", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("sendgift_"))
async def send_gift_item(call: types.CallbackQuery):
    global gift_revenue_total
    user_id = call.from_user.id
    partner_id = active_chats.get(user_id)
    if partner_id is None:
        await call.answer("Чат вже завершено", show_alert=True)
        return

    key = call.data[len("sendgift_"):]
    item = GIFT_CATALOG.get(key)
    if item is None:
        await call.answer("Невідомий подарунок", show_alert=True)
        return

    u = init_user(user_id)
    have = u.get("gifts", {}).get(key, 0)
    if have <= 0:
        await call.answer("У тебе немає такого подарунка в інвентарі", show_alert=True)
        return

    title, price = item
    u["gifts"][key] = have - 1
    u["gifts_sent_count"] = u.get("gifts_sent_count", 0) + 1

    recipient_amount = round(price * GIFT_RECIPIENT_SHARE, 2)
    admin_amount = round(price - recipient_amount, 2)
    gift_revenue_total += admin_amount

    p = init_user(partner_id)
    p["balance"] += recipient_amount

    await call.message.answer(f"🎁 Подаровано: <b>{esc(title)}</b>!")
    await safe_send(
        partner_id,
        f"🎁 Співрозмовник подарував вам: <b>{esc(title)}</b>!\n"
        f"На баланс нараховано {recipient_amount:.2f} грн.",
    )
    await call.answer()


# ---------------------------------------------------------------------------
# Адмін-панель (прихована, тільки для ADMIN_ID + пароль)
# ---------------------------------------------------------------------------
@dp.message(Command("admin"))
async def admin_entry(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if not ADMIN_ID or user_id != ADMIN_ID:
        return  # для всіх інших ця команда ніби не існує

    if not ADMIN_PASSWORD:
        await message.answer("⚠️ Не задано ADMIN_PASSWORD у змінних середовища.")
        return

    if user_id in authorized_admins:
        await message.answer("🔐 <b>Адмін-панель</b>", reply_markup=get_admin_keyboard())
        return

    await message.answer("🔐 Введіть пароль для доступу до адмін-панелі:")
    await state.set_state(AdminStates.password)


@dp.message(AdminStates.password, F.text)
async def admin_password(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    await state.clear()
    if message.text.strip() != ADMIN_PASSWORD:
        await message.answer("❌ Невірний пароль.")
        return
    authorized_admins.add(user_id)
    await message.answer("✅ Доступ надано.\n\n🔐 <b>Адмін-панель</b>", reply_markup=get_admin_keyboard())


def admin_only(func):
    async def wrapper(call: types.CallbackQuery, *args, **kwargs):
        if call.from_user.id != ADMIN_ID or call.from_user.id not in authorized_admins:
            await call.answer("Доступ заборонено", show_alert=True)
            return
        return await func(call, *args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper


@dp.callback_query(F.data == "adm_stats")
@admin_only
async def adm_stats(call: types.CallbackQuery):
    total_users = len(users_db)
    premium_count = sum(1 for u in users_db.values() if is_premium(u))
    total_referrals = sum(u.get("referral_count", 0) for u in users_db.values())
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"• Користувачів: {total_users}\n"
        f"• У черзі пошуку: {len(queue)}\n"
        f"• Активних чатів: {len(active_chats) // 2}\n"
        f"• Premium: {premium_count}\n"
        f"• Забанено: {len(banned_users)}\n"
        f"• Скарг усього: {len(reports)}\n"
        f"• Дохід з подарунків: {gift_revenue_total:.2f} грн\n"
        f"• Дохід з рулетки: {lottery_revenue_total:.2f} грн\n"
        f"• Запрошень за реферальною програмою: {total_referrals}\n"
        f"• Автобан після {AUTO_BAN_REPORTS} скарг (users у режимі спостереження: "
        f"{sum(1 for u in users_db.values() if 0 < u.get('reports_received', 0) < AUTO_BAN_REPORTS)})\n"
    )
    await call.message.answer(text)
    await call.answer()


@dp.callback_query(F.data.startswith("adm_reports_"))
@admin_only
async def adm_reports(call: types.CallbackQuery):
    page = int(call.data.split("_")[-1])
    per_page = 5
    pending = [r for r in reports if r["status"] == "нова"]

    if not pending:
        await call.message.answer("🚨 Нових скарг немає.")
        await call.answer()
        return

    chunk = pending[page * per_page : (page + 1) * per_page]
    if not chunk:
        await call.answer("Більше немає скарг", show_alert=True)
        return

    lines = ["🚨 <b>Скарги (нові):</b>\n"]
    buttons = []
    for r in chunk:
        lines.append(f"#{r['id']} — {r['time']}\nВід <code>{r['from']}</code> на <code>{r['on']}</code>\n")
        buttons.append(
            [
                InlineKeyboardButton(text=f"⛔ Бан #{r['id']} (на кого скарга)", callback_data=f"adm_banrep_{r['id']}"),
                InlineKeyboardButton(text=f"✅ Закрити #{r['id']}", callback_data=f"adm_closerep_{r['id']}"),
            ]
        )
    if len(pending) > (page + 1) * per_page:
        buttons.append([InlineKeyboardButton(text="➡️ Далі", callback_data=f"adm_reports_{page + 1}")])

    await call.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


@dp.callback_query(F.data.startswith("adm_banrep_"))
@admin_only
async def adm_ban_from_report(call: types.CallbackQuery):
    report_id = int(call.data.split("_")[-1])
    rep = next((r for r in reports if r["id"] == report_id), None)
    if rep is None:
        await call.answer("Скаргу не знайдено", show_alert=True)
        return
    banned_users.add(rep["on"])
    rep["status"] = "оброблена"
    await safe_send(rep["on"], "⛔ Вас заблоковано адміністратором за скаргою.")
    await call.message.answer(f"⛔ Користувача <code>{rep['on']}</code> забанено.")
    await call.answer()


@dp.callback_query(F.data.startswith("adm_closerep_"))
@admin_only
async def adm_close_report(call: types.CallbackQuery):
    report_id = int(call.data.split("_")[-1])
    rep = next((r for r in reports if r["id"] == report_id), None)
    if rep is None:
        await call.answer("Скаргу не знайдено", show_alert=True)
        return
    rep["status"] = "закрита"
    await call.message.answer(f"✅ Скаргу #{report_id} закрито без дій.")
    await call.answer()


@dp.callback_query(F.data == "adm_ban")
@admin_only
async def adm_ban_start(call: types.CallbackQuery, state: FSMContext):
    await call.message.answer("Введіть user_id, якого треба забанити:")
    await state.set_state(AdminStates.ban_id)
    await call.answer()


@dp.message(AdminStates.ban_id, F.text)
async def adm_ban_finish(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    try:
        target = int(message.text.strip())
    except ValueError:
        await message.answer("Потрібно надіслати число (user_id).")
        return
    banned_users.add(target)
    end_chat(target)
    if target in queue:
        queue.remove(target)
    await message.answer(f"⛔ Користувача <code>{target}</code> забанено.")
    await safe_send(target, "⛔ Вас заблоковано адміністратором.")


@dp.callback_query(F.data == "adm_unban")
@admin_only
async def adm_unban_start(call: types.CallbackQuery, state: FSMContext):
    await call.message.answer("Введіть user_id, якого треба розбанити:")
    await state.set_state(AdminStates.unban_id)
    await call.answer()


@dp.message(AdminStates.unban_id, F.text)
async def adm_unban_finish(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    try:
        target = int(message.text.strip())
    except ValueError:
        await message.answer("Потрібно надіслати число (user_id).")
        return
    banned_users.discard(target)
    await message.answer(f"✅ Користувача <code>{target}</code> розбанено.")
    await safe_send(target, "✅ Вас розблоковано адміністратором.")


@dp.callback_query(F.data == "adm_banlist")
@admin_only
async def adm_banlist(call: types.CallbackQuery):
    if not banned_users:
        await call.message.answer("Список забанених порожній.")
    else:
        ids = "\n".join(f"• <code>{uid}</code>" for uid in sorted(banned_users))
        await call.message.answer(f"⛔ <b>Забанені користувачі:</b>\n{ids}")
    await call.answer()


@dp.callback_query(F.data == "adm_broadcast")
@admin_only
async def adm_broadcast_start(call: types.CallbackQuery, state: FSMContext):
    await call.message.answer("Введіть текст розсилки для ВСІХ користувачів (або /cancel):")
    await state.set_state(AdminStates.broadcast)
    await call.answer()


@dp.message(AdminStates.broadcast, F.text)
async def adm_broadcast_finish(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    text = message.text
    sent, failed = 0, 0
    for uid in list(users_db.keys()):
        ok = await safe_send(uid, f"📢 {esc(text)}")
        sent += ok
        failed += not ok
    await message.answer(f"✅ Розіслано: {sent}. Не вдалося: {failed}.")


# ---------------------------------------------------------------------------
# Пересилання повідомлень (завжди останнім!)
# ---------------------------------------------------------------------------
@dp.message()
async def relay_messages(message: types.Message):
    user_id = message.from_user.id

    partner_id = active_chats.get(user_id)
    if partner_id is None:
        await message.answer("Скористайтеся меню нижче:", reply_markup=get_main_keyboard())
        return

    text_to_check = message.text or message.caption
    if text_to_check and LINK_REGEX.search(text_to_check):
        await message.answer(
            "🚫 Повідомлення з посиланнями або контактами заборонено — спілкуйтесь анонімно в боті."
        )
        return

    try:
        await message.copy_to(chat_id=partner_id)
    except TelegramAPIError:
        end_chat(user_id)
        await message.answer(
            "Не вдалося доставити повідомлення, чат завершено.", reply_markup=get_main_keyboard()
        )


# ---------------------------------------------------------------------------
# Веб-сервер для Render (інакше "No open ports detected")
# ---------------------------------------------------------------------------
async def handle_ping(request: web.Request):
    return web.Response(text="Bot is running!")


async def start_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info("Веб-сервер запущено на порту %s", port)
    return runner


async def setup_bot_commands():
    """Перекладає меню команд '/' на українську (замість заглушок command1, command2...)."""
    default_commands = [
        types.BotCommand(command="start", description="🚀 Почати / перезапустити бота"),
        types.BotCommand(command="edit_profile", description="✏️ Редагувати профіль"),
        types.BotCommand(command="stop", description="❌ Завершити чат"),
        types.BotCommand(command="cancel", description="⬅️ Скасувати поточну дію"),
        types.BotCommand(command="top", description="🏆 Топ дарувальників подарунків"),
        types.BotCommand(command="filters", description="🎯 Фільтри пошуку"),
    ]
    await bot.set_my_commands(default_commands, scope=types.BotCommandScopeDefault())

    if ADMIN_ID:
        admin_commands = default_commands + [
            types.BotCommand(command="admin", description="🔐 Адмін-панель"),
            types.BotCommand(command="addbalance", description="💰 Поповнити баланс користувачу"),
        ]
        try:
            await bot.set_my_commands(
                admin_commands, scope=types.BotCommandScopeChat(chat_id=ADMIN_ID)
            )
        except TelegramAPIError as e:
            # адмін ще жодного разу не писав боту — Telegram не дає встановити команди для нього
            logging.warning("Не вдалося встановити адмін-команди: %s", e)


async def main():
    global BOT_USERNAME
    logging.basicConfig(level=logging.INFO)
    runner = await start_web_server()
    poll_task = asyncio.create_task(crypto_poll_loop())
    try:
        # Скидаємо webhook і старі апдейти, щоб менше конфліктувати при редеплої
        await bot.delete_webhook(drop_pending_updates=True)
        await setup_bot_commands()
        me = await bot.get_me()
        BOT_USERNAME = me.username or ""
        await dp.start_polling(bot)
    finally:
        poll_task.cancel()
        await bot.session.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
