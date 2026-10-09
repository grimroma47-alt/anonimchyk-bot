from __future__ import annotations

import asyncio
import html
import logging
import os
import functools
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

# Хто й коли востаннє щось робив у боті — для кнопки "Онлайн".
# Тримаємо лише в пам'яті (не в базі), щоб не записувати базу щохвилини.
last_seen: dict[int, float] = {}
ONLINE_WINDOW = int(os.getenv("ONLINE_WINDOW", "300"))  # "онлайн" = активний за останні N секунд


@dp.update.outer_middleware()
async def track_last_seen(handler, event, data):
    try:
        user = data.get("event_from_user")
        if user is not None:
            last_seen[user.id] = time.time()
    except Exception:  # noqa: BLE001 — облік онлайну ніколи не має ламати обробку повідомлень
        pass
    return await handler(event, data)

# ---------------------------------------------------------------------------
# "База даних" у пам'яті (скидається при кожному перезапуску Render!)
# Для постійного зберігання підключи PostgreSQL (Render Postgres) або SQLite з диском.
# ---------------------------------------------------------------------------
users_db: dict[int, dict] = {}
queue: list[int] = []
search_mode: dict[int, str] = {}  # user_id у черзі -> "normal" або "flirt" (флірт шукає лише флірт)
active_chats: dict[int, int] = {}
user_counter = 1000

banned_users: set[int] = set()
reports: list[dict] = []  # {"id", "from", "on", "time", "status"}
report_counter = 0
ads: list[dict] = []  # реклама в черзі пошуку: {"id","text","url","active","shows","created"}
ad_counter = 0
authorized_admins: set[int] = set()  # хто вже ввів пароль у цій сесії

# Крипто-рахунки, очікують оплати: invoice_id -> {"user_id", "amount" (USDT), "credit" (грн)}
pending_crypto_invoices: dict[str, dict] = {}

# Оплата за реквізитами (переказ на картку / IBAN) з ручним підтвердженням адміном.
# Реквізити — ТІЛЬКИ в змінних середовища Render (не в коді на GitHub).
PAY_CARD = os.getenv("PAY_CARD", "").strip()  # номер картки
PAY_IBAN = os.getenv("PAY_IBAN", "").strip()  # IBAN (для ФОП)
PAY_RECIPIENT = os.getenv("PAY_RECIPIENT", "").strip()  # отримувач (ПІБ або ФОП ...)
PAY_TAX_ID = os.getenv("PAY_TAX_ID", "").strip()  # ЄДРПОУ / ІПН (якщо потрібно для IBAN)
# код -> {"user_id", "amount", "created", "status": "pending"/"confirmed"/"rejected", "receipt": bool}
manual_payments: dict[str, dict] = {}

# Автопідтвердження через Банку monobank (особистий API, лише читання).
MONO_TOKEN = os.getenv("MONO_TOKEN", "").strip()  # токен з api.monobank.ua
MONO_JAR_ID = os.getenv("MONO_JAR_ID", "").strip()  # id Банки (необов'язково — бот знайде сам)
MONO_JAR_TITLE = os.getenv("MONO_JAR_TITLE", "").strip()  # частина назви Банки, якщо їх кілька
PAY_JAR_LINK = os.getenv("PAY_JAR_LINK", "").strip()  # посилання на Банку (необов'язково)
mono_seen_ids: list[str] = []  # id операцій з виписки, які вже оброблено (щоб не зарахувати двічі)

# ---------------------------------------------------------------------------
# Групові кімнати за інтересами (фіксовані теми, невеликі групи, лише текст)
# ---------------------------------------------------------------------------
ROOM_TOPICS = {
    "music": "🎵 Музика",
    "movies": "🎬 Кіно та серіали",
    "games": "🎮 Ігри",
    "travel": "✈️ Подорожі",
    "it": "💻 IT та технології",
    "sport": "⚽ Спорт",
    "books": "📚 Книги",
    "pets": "🐾 Тварини",
}
ROOM_CAPACITY = int(os.getenv("ROOM_CAPACITY", "8"))  # макс. учасників в одній кімнаті

rooms: dict[str, dict] = {}  # room_id -> {"topic": ключ з ROOM_TOPICS, "members": set[int]}
user_room: dict[int, str] = {}  # user_id -> room_id (в якій кімнаті зараз людина)
room_counter = 0

# Пакети поповнення
STAR_PACKAGES = [50, 100, 250, 500]  # Telegram Stars; 1 star = 1 грн на баланс
CRYPTO_PACKAGES = [1, 5, 10, 20]  # USDT

# Поповнення на довільну суму + накопичувальний бонус
TOPUP_MIN = int(os.getenv("TOPUP_MIN", "10"))  # мінімальна сума поповнення, грн
TOPUP_MAX = int(os.getenv("TOPUP_MAX", "10000"))  # максимальна сума поповнення, грн
TOPUP_QUICK_AMOUNTS = [50, 100, 250, 500, 1000]  # швидкі кнопки сум
TOPUP_BONUS_STEP = float(os.getenv("TOPUP_BONUS_STEP", "500"))  # за кожні N грн поповнень...
TOPUP_BONUS_AMOUNT = float(os.getenv("TOPUP_BONUS_AMOUNT", "50"))  # ...бонус стільки грн

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
    "chocolate": ("🍫 Шоколадка", 15),
    "rabbit": ("🐰 Зайчик", 18),
    "dog": ("🐶 Собачка", 18),
    "cat": ("🐱 Котик", 18),
    "cake": ("🎂 Тортик", 25),
    "bouquet": ("💐 Букет", 35),
    "teddy": ("🧸 Ведмедик", 40),
    "car": ("🚗 Машинка", 60),
    "ring": ("💍 Каблучка", 75),
    "unicorn": ("🦄 Єдиноріг", 100),
    "diamond": ("💎 Діамант", 150),
    "console": ("🎮 Приставка", 200),
    "crown": ("👑 Корона", 300),
    "rocket": ("🚀 Ракета", 500),
    "castle": ("🏰 Замок", 1000),
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
# Друзі (додаються за взаємною згодою з активного чату).
# Повністю окремий, незалежний від реконнекту механізм — щоб не чіпати
# вже перевірений код реконнекту і тримати ризик нового коду ізольованим.
# ---------------------------------------------------------------------------
friend_add_requests: dict[int, int] = {}  # acceptor_id -> requester_id (запит у друзі)
# Переписка з друзями працює як "скринька" — окремо від анонімного чату.
# (chat_id отримувача, message_id у нього) -> id друга, який надіслав.
# Потрібно, щоб відповідь свайпом (Reply) пішла саме тому другові.
friend_msg_map: dict[tuple[int, int], int] = {}
FRIEND_MSG_MAP_LIMIT = 50000  # щоб пам'ять не росла безкінечно

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
UNBAN_BASE_PRICE = float(os.getenv("UNBAN_BASE_PRICE", "70"))  # перше платне розблокування, грн
UNBAN_PRICE_STEP = float(os.getenv("UNBAN_PRICE_STEP", "70"))  # +стільки грн за кожен наступний бан
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
BTN_ROOMS = "👥 Кімнати за інтересами"
BTN_ROOM_LEAVE = "🚪 Вийти з кімнати"
BTN_ROOM_REPORT = "🚨 Поскаржитися на учасника"
BTN_HELP = "🆘 Допомога"
BTN_FRIENDS = "👫 Друзі"
BTN_TOPUP = "💳 Поповнити баланс"
BTN_ONLINE = "👥 Онлайн"
BTN_PREMIUM = "💎 Premium"
BTN_FLIRT = "❤️ Флірт-пошук"
BTN_ADD_FRIEND = "🤝 Додати в друзі"


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
    support_reply = State()
    ad_text = State()
    ad_url = State()


class FilterStates(StatesGroup):
    country = State()


class SupportStates(StatesGroup):
    message = State()


class FriendStates(StatesGroup):
    write = State()


class TopupStates(StatesGroup):
    amount = State()
    receipt = State()


# ---------------------------------------------------------------------------
# Клавіатури
# ---------------------------------------------------------------------------
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SEARCH)],
            [KeyboardButton(text=BTN_FLIRT), KeyboardButton(text=BTN_ROOMS)],
            [KeyboardButton(text=BTN_TOPUP), KeyboardButton(text=BTN_WALLET)],
            [KeyboardButton(text=BTN_PREMIUM), KeyboardButton(text=BTN_SHOP)],
            [KeyboardButton(text=BTN_DAILY), KeyboardButton(text=BTN_PROFILE)],
            [KeyboardButton(text=BTN_SETTINGS), KeyboardButton(text=BTN_FRIENDS)],
            [KeyboardButton(text=BTN_FILTERS), KeyboardButton(text=BTN_ONLINE)],
            [KeyboardButton(text=BTN_LOTTERY), KeyboardButton(text=BTN_TOP)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
    )


def get_room_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ROOM_REPORT)],
            [KeyboardButton(text=BTN_ROOM_LEAVE)],
        ],
        resize_keyboard=True,
    )


def topic_online_count(topic_key: str) -> int:
    return sum(len(r["members"]) for r in rooms.values() if r["topic"] == topic_key)


def get_room_topics_keyboard():
    rows = [
        [
            InlineKeyboardButton(
                text=f"{title} — {topic_online_count(key)} 👥 онлайн",
                callback_data=f"room_join_{key}",
            )
        ]
        for key, title in ROOM_TOPICS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_chat_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_GIFT), KeyboardButton(text=BTN_ADD_FRIEND)],
            [KeyboardButton(text=BTN_STOP), KeyboardButton(text=BTN_REPORT)],
            [KeyboardButton(text=BTN_BLACKLIST_ADD)],
        ],
        resize_keyboard=True,
    )


def get_friends_keyboard(u: dict):
    friends = u.get("friends") or set()
    if not friends:
        return None
    rows = []
    for fid in sorted(friends):
        f = users_db.get(fid)
        name = f["nickname"] if f else str(fid)
        rows.append(
            [
                InlineKeyboardButton(text=f"✍️ {name}", callback_data=f"fmsg_{fid}"),
                InlineKeyboardButton(text="❌", callback_data=f"friend_remove_{fid}"),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    buttons = [
        InlineKeyboardButton(text=f"{title} — {price} грн", callback_data=f"buygift_{key}")
        for key, (title, price) in GIFT_CATALOG.items()
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
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
            [InlineKeyboardButton(text="💳 Поповнити баланс", callback_data="topup_open")],
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


def get_settings_keyboard(u: dict | None = None):
    protect_on = bool(u and u.get("protect_media"))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BTN_FILTERS, callback_data="filters_menu")],
            [InlineKeyboardButton(text="📋 Чорний список", callback_data="bl_view")],
            [
                InlineKeyboardButton(
                    text=f"🔒 Захист моїх медіа: {'увімк ✅' if protect_on else 'вимк'} (💎)",
                    callback_data="toggle_protect",
                )
            ],
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
            [InlineKeyboardButton(text="📣 Реклама в пошуку", callback_data="adm_ads")],
            [InlineKeyboardButton(text="🏦 Оплати на перевірці", callback_data="adm_payments")],
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
            "friends": set(),  # user_id друзів (додані за взаємною згодою)
            "topup_total": 0.0,  # скільки грн поповнено реальними оплатами (для бонусу)
            "protect_media": False,  # Premium: заборонити співрозмовникам пересилати/зберігати мої повідомлення
            "ban_type": None,  # "auto" — автобан за скарги (можна викупити), "admin" — бан адміном
            "ban_count": 0,  # скільки разів отримував автобан (від цього росте ціна розблокування)
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


def admin_label(user_id: int) -> str:
    """Для адміна: нік і наш ID (ID_1001), а Telegram ID — дрібно, для команд."""
    u = users_db.get(user_id)
    if u is None:
        return f"<code>{user_id}</code>"
    return f"{esc(u['nickname'])} ({esc(u['custom_id'])}) · <code>{user_id}</code>"


def resolve_user_id(text: str) -> int | None:
    """Приймає і наш ID (ID_1001), і Telegram ID. Повертає Telegram ID або None."""
    text = (text or "").strip()
    m = re.fullmatch(r"(?i)id_?(\d+)", text)
    if m:
        wanted = f"ID_{int(m.group(1))}"
        for uid, u in users_db.items():
            if u.get("custom_id") == wanted:
                return uid
        return None
    try:
        return int(text)
    except ValueError:
        return None


def should_protect(u: dict | None) -> bool:
    """Чи захищати повідомлення цієї людини від пересилання/збереження (Premium + увімкнено)."""
    return bool(u and u.get("protect_media") and is_premium(u))


def unban_price(u: dict) -> float:
    """Ціна розблокування росте з кожним автобаном: 70, 140, 210... (налаштовується)."""
    n = max(int(u.get("ban_count") or 1), 1)
    return UNBAN_BASE_PRICE + UNBAN_PRICE_STEP * (n - 1)


def register_auto_ban(u: dict):
    u["ban_type"] = "auto"
    u["ban_count"] = int(u.get("ban_count") or 0) + 1


def banned_notice(user_id: int):
    """Текст і кнопки для заблокованого користувача."""
    u = init_user(user_id)
    if u.get("ban_type") != "auto":
        return "⛔ Вас заблоковано в цьому боті.", None
    price = unban_price(u)
    text = (
        "🚨 Вас заблоковано через велику кількість скарг на ваш акаунт.\n\n"
        f"Щоб відновити доступ, можна придбати розблокування за <b>{price:.0f} грн</b> з балансу.\n\n"
        "⚠️ Після кожного наступного блокування ціна розблокування зростатиме."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🔓 Розблокування ({price:.0f} грн)", callback_data="unban_buy")],
            [InlineKeyboardButton(text="💳 Поповнити баланс", callback_data="topup_open")],
        ]
    )
    return text, kb


def calc_topup_bonus(total_before: float, amount: float) -> float:
    """Скільки бонусу дає поповнення: за кожен новий перетнутий рубіж TOPUP_BONUS_STEP."""
    if TOPUP_BONUS_STEP <= 0 or TOPUP_BONUS_AMOUNT <= 0 or amount <= 0:
        return 0.0
    steps = int((total_before + amount) // TOPUP_BONUS_STEP) - int(total_before // TOPUP_BONUS_STEP)
    return max(steps, 0) * TOPUP_BONUS_AMOUNT


def apply_topup_bonus(u: dict, amount: float) -> float:
    """Враховує реальне поповнення (Stars/крипта) і нараховує бонус. Повертає суму бонусу."""
    total_before = float(u.get("topup_total") or 0.0)
    bonus = calc_topup_bonus(total_before, amount)
    u["topup_total"] = total_before + amount
    if bonus:
        u["balance"] += bonus
    return bonus


def topup_progress_text(u: dict) -> str:
    if TOPUP_BONUS_STEP <= 0 or TOPUP_BONUS_AMOUNT <= 0:
        return ""
    done = float(u.get("topup_total") or 0.0) % TOPUP_BONUS_STEP
    filled = min(int(done / TOPUP_BONUS_STEP * 10), 10)
    bar = "▓" * filled + "░" * (10 - filled)
    return (
        f"🎁 До бонусу +{TOPUP_BONUS_AMOUNT:.0f} грн: {done:.0f}/{TOPUP_BONUS_STEP:.0f} грн\n{bar}"
    )


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


def remove_from_room(user_id: int):
    """Прибирає користувача з його групової кімнати (якщо є) і чистить порожні кімнати."""
    room_id = user_room.pop(user_id, None)
    if room_id is None:
        return
    room = rooms.get(room_id)
    if room is None:
        return
    room["members"].discard(user_id)
    if not room["members"]:
        rooms.pop(room_id, None)


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
    bonus = apply_topup_bonus(u, info["credit"])
    request_save()
    if bonus:
        await safe_send(
            info["user_id"],
            f"🎉 Бонус +{bonus:.0f} грн за кожні {TOPUP_BONUS_STEP:.0f} грн поповнень!\n\n"
            + topup_progress_text(u),
        )
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
# База даних (Postgres): збереження балансів, профілів, друзів тощо між перезапусками.
# Весь важливий стан зберігається одним "знімком". Якщо DATABASE_URL не задано —
# бот працює як раніше, лише в пам'яті.
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SAVE_INTERVAL = int(os.getenv("SAVE_INTERVAL", "60"))  # секунд між автозбереженнями

_db_ready = False  # True лише після успішного завантаження — щоб не затерти базу порожнім станом
_last_saved_blob: bytes | None = None
_save_event = asyncio.Event()

# "Естафета" між копіями бота під час деплою: працює і пише в базу лише власник.
# Нова копія чекає, поки стара збереже дані й віддасть естафету, — тоді нічого не губиться.
INSTANCE_ID = os.urandom(6).hex()
LEASE_HEARTBEAT_SECONDS = 10  # як часто власник відмічається в базі
LEASE_STALE_SECONDS = 45  # без відмітки стільки секунд — копія вважається мертвою
LEASE_MAX_WAIT = int(os.getenv("LEASE_MAX_WAIT", "180"))  # макс. очікування старої копії, сек
_lease_held = False
_background_tasks: set = set()


def request_save():
    """Попросити зберегти стан якнайшвидше (напр. одразу після оплати)."""
    _save_event.set()


def _db_connect():
    import ssl
    from urllib.parse import parse_qs, unquote, urlparse

    import pg8000.native

    url = urlparse(DATABASE_URL)
    sslmode = (parse_qs(url.query).get("sslmode") or ["require"])[0]
    host = url.hostname or "localhost"
    port = url.port or 5432
    if sslmode == "disable":
        ssl_context = None
    elif sslmode in ("verify-ca", "verify-full"):
        ssl_context = ssl.create_default_context()
    else:
        # як sslmode=require: з'єднання шифроване, сертифікат не перевіряється
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    if sslmode not in ("verify-ca", "verify-full"):
        # Перетворюємо назву сервера на IP-адресу (спершу IPv4): бібліотека на Render
        # падала з "... does not appear to be an IPv4 or IPv6 address" на назві сервера.
        import socket

        try:
            infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        except socket.gaierror:
            infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        if not infos:
            raise ConnectionError(f"Не вдалося знайти IP-адресу сервера бази {host}")
        host = infos[0][4][0]

    return pg8000.native.Connection(
        user=unquote(url.username or ""),
        password=unquote(url.password or ""),
        host=host,
        port=port,
        database=(url.path or "/postgres").lstrip("/") or "postgres",
        ssl_context=ssl_context,
        timeout=30,
    )


def _db_init_tables():
    con = _db_connect()
    try:
        con.run(
            "CREATE TABLE IF NOT EXISTS bot_state ("
            " id INTEGER PRIMARY KEY,"
            " data BYTEA NOT NULL,"
            " prev_data BYTEA,"
            " updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        con.run(
            "CREATE TABLE IF NOT EXISTS bot_lease ("
            " id INTEGER PRIMARY KEY,"
            " owner TEXT,"
            " heartbeat TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
    finally:
        con.close()


def _db_try_acquire(force: bool = False) -> bool:
    """Забрати естафету: якщо вона вільна, наша, прострочена — або примусово."""
    con = _db_connect()
    try:
        rows = con.run(
            "INSERT INTO bot_lease (id, owner, heartbeat) VALUES (1, :me, now()) "
            "ON CONFLICT (id) DO UPDATE SET owner = EXCLUDED.owner, heartbeat = now() "
            "WHERE bot_lease.owner IS NULL OR bot_lease.owner = EXCLUDED.owner "
            f"OR bot_lease.heartbeat < now() - interval '{LEASE_STALE_SECONDS} seconds' OR :force "
            "RETURNING owner",
            me=INSTANCE_ID,
            force=bool(force),
        )
        return bool(rows)
    finally:
        con.close()


def _db_heartbeat() -> bool:
    """Відмітка "я живий". False — естафету в нас забрали."""
    con = _db_connect()
    try:
        rows = con.run(
            "UPDATE bot_lease SET heartbeat = now() WHERE id = 1 AND owner = :me RETURNING id",
            me=INSTANCE_ID,
        )
        return bool(rows)
    finally:
        con.close()


def _db_release():
    con = _db_connect()
    try:
        con.run("UPDATE bot_lease SET owner = NULL WHERE id = 1 AND owner = :me", me=INSTANCE_ID)
    finally:
        con.close()


def _db_load_blob() -> bytes | None:
    con = _db_connect()
    try:
        rows = con.run("SELECT data FROM bot_state WHERE id = 1")
        return bytes(rows[0][0]) if rows else None
    finally:
        con.close()


def _db_load_prev_blob() -> bytes | None:
    con = _db_connect()
    try:
        rows = con.run("SELECT prev_data FROM bot_state WHERE id = 1")
        return bytes(rows[0][0]) if rows and rows[0][0] is not None else None
    finally:
        con.close()


def _db_save_blob(blob: bytes) -> bool:
    """Записує дані, ЛИШЕ якщо естафета наша (одним запитом, з блокуванням). False — не наша."""
    con = _db_connect()
    try:
        # попередню версію зберігаємо в prev_data — запасна копія
        rows = con.run(
            "WITH lease AS (SELECT 1 FROM bot_lease WHERE id = 1 AND owner = :me FOR UPDATE) "
            "INSERT INTO bot_state (id, data) SELECT 1, :d FROM lease "
            "ON CONFLICT (id) DO UPDATE SET prev_data = bot_state.data, data = EXCLUDED.data, updated_at = now() "
            "RETURNING id",
            d=blob,
            me=INSTANCE_ID,
        )
        return bool(rows)
    finally:
        con.close()


def _make_snapshot() -> bytes:
    import pickle

    return pickle.dumps(
        {
            "version": 1,
            "users_db": users_db,
            "user_counter": user_counter,
            "banned_users": banned_users,
            "reports": reports,
            "report_counter": report_counter,
            "pending_crypto_invoices": pending_crypto_invoices,
            "manual_payments": manual_payments,
            "mono_seen_ids": mono_seen_ids,
            "gift_revenue_total": gift_revenue_total,
            "lottery_revenue_total": lottery_revenue_total,
            "ads": ads,
            "ad_counter": ad_counter,
        },
        protocol=4,
    )


def _apply_snapshot(blob: bytes):
    import copy
    import pickle

    global user_counter, report_counter, gift_revenue_total, lottery_revenue_total, ad_counter
    data = pickle.loads(blob)

    users_db.clear()
    users_db.update(data.get("users_db", {}))
    banned_users.clear()
    banned_users.update(data.get("banned_users", set()))
    reports.clear()
    reports.extend(data.get("reports", []))
    pending_crypto_invoices.clear()
    pending_crypto_invoices.update(data.get("pending_crypto_invoices", {}))
    manual_payments.clear()
    manual_payments.update(data.get("manual_payments", {}))
    mono_seen_ids.clear()
    mono_seen_ids.extend(data.get("mono_seen_ids", []))
    user_counter = data.get("user_counter", user_counter)
    report_counter = data.get("report_counter", report_counter)
    gift_revenue_total = data.get("gift_revenue_total", gift_revenue_total)
    lottery_revenue_total = data.get("lottery_revenue_total", lottery_revenue_total)
    ads.clear()
    ads.extend(data.get("ads", []))
    ad_counter = data.get("ad_counter", ad_counter)

    # Якщо в нових версіях бота з'являться нові поля профілю — додаємо їх старим користувачам.
    saved_counter = user_counter
    template = init_user(-1)
    users_db.pop(-1, None)
    user_counter = saved_counter
    for u in users_db.values():
        for key, value in template.items():
            if key not in u:
                u[key] = copy.deepcopy(value)


async def load_state_from_db():
    """Завантажує стан з бази при старті. Без успішного завантаження збереження вимкнене."""
    global _db_ready, _last_saved_blob, _lease_held
    if not DATABASE_URL:
        logging.warning("DATABASE_URL не задано — дані зберігаються лише в пам'яті і зникнуть після перезапуску.")
        return

    last_error = None
    for attempt in range(1, 6):
        try:
            await asyncio.to_thread(_db_init_tables)
            break
        except Exception as e:  # noqa: BLE001
            last_error = e
            logging.warning("База даних недоступна (спроба %s/5): %s", attempt, e)
            await asyncio.sleep(3 * attempt)
    else:
        # Краще не запускатись, ніж запуститись з порожніми даними і затерти ними базу.
        raise SystemExit(f"❌ Не вдалося підключитися до бази даних: {last_error}")

    # Чекаємо, поки попередня копія бота (під час деплою) збереже дані й віддасть естафету.
    waited = 0.0
    announced = False
    while True:
        force = waited >= LEASE_MAX_WAIT
        if force:
            logging.warning("Стара копія бота не віддає естафету %s с — перебираю її примусово.", LEASE_MAX_WAIT)
        try:
            if await asyncio.to_thread(_db_try_acquire, force):
                break
        except Exception as e:  # noqa: BLE001
            logging.warning("Помилка при отриманні естафети: %s", e)
        if not announced:
            logging.info("Чекаю, поки попередня копія бота збереже дані і вимкнеться...")
            announced = True
        await asyncio.sleep(3)
        waited += 3
    _lease_held = True
    logging.info("Естафету отримано (копія %s).", INSTANCE_ID)

    last_error = None
    for attempt in range(1, 6):
        try:
            blob = await asyncio.to_thread(_db_load_blob)
            break
        except Exception as e:  # noqa: BLE001
            last_error = e
            logging.warning("Не вдалося прочитати дані (спроба %s/5): %s", attempt, e)
            await asyncio.sleep(3 * attempt)
    else:
        raise SystemExit(f"❌ Не вдалося прочитати дані з бази: {last_error}")

    if blob is None:
        logging.info("База даних порожня — починаємо з нуля.")
    else:
        try:
            _apply_snapshot(blob)
        except Exception as e:  # noqa: BLE001
            logging.error("Основна копія даних пошкоджена (%s) — пробую запасну.", e)
            prev = await asyncio.to_thread(_db_load_prev_blob)
            if prev is None:
                raise SystemExit("❌ Дані в базі пошкоджені, а запасної копії немає.")
            _apply_snapshot(prev)
        logging.info("Дані завантажено з бази: %s користувачів.", len(users_db))
        _last_saved_blob = blob

    _db_ready = True


async def save_state_to_db():
    global _last_saved_blob
    if not _db_ready:
        return
    try:
        blob = _make_snapshot()  # знімок робимо в основному потоці — дані не змінюються посередині
        if blob == _last_saved_blob:
            return  # нічого не змінилось
        if await asyncio.to_thread(_db_save_blob, blob):
            _last_saved_blob = blob
        else:
            _on_lease_lost()
    except Exception as e:  # noqa: BLE001
        logging.error("Не вдалося зберегти дані в базу: %s", e)


def _on_lease_lost():
    """Естафету забрала інша (новіша) копія бота — ця копія більше не пише в базу і зупиняється."""
    global _db_ready, _lease_held
    if not _lease_held and not _db_ready:
        return
    _db_ready = False
    _lease_held = False
    logging.error("Естафету забрала інша копія бота — ця копія зупиняється, щоб не зіпсувати дані.")
    try:
        # тримаємо посилання на задачу, щоб її не прибрав збирач сміття до виконання
        _background_tasks.add(asyncio.get_running_loop().create_task(dp.stop_polling()))
    except Exception as e:  # noqa: BLE001
        logging.warning("Не вдалося зупинити polling: %s", e)


async def lease_heartbeat_loop():
    while True:
        await asyncio.sleep(LEASE_HEARTBEAT_SECONDS)
        if not _lease_held:
            continue
        try:
            still_mine = await asyncio.to_thread(_db_heartbeat)
        except Exception as e:  # noqa: BLE001
            logging.warning("Не вдалося відмітитись у базі: %s", e)
            continue
        if not still_mine:
            _on_lease_lost()


async def release_lease():
    """При зупинці (вже після фінального збереження) віддаємо естафету новій копії."""
    global _lease_held
    if not _lease_held:
        return
    try:
        await asyncio.to_thread(_db_release)
        logging.info("Естафету віддано.")
    except Exception as e:  # noqa: BLE001
        logging.warning("Не вдалося віддати естафету (нова копія перебере її за %s с): %s", LEASE_STALE_SECONDS, e)
    _lease_held = False


async def persistence_loop():
    while True:
        try:
            await asyncio.wait_for(_save_event.wait(), timeout=SAVE_INTERVAL)
        except asyncio.TimeoutError:
            pass
        _save_event.clear()
        await save_state_to_db()


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
        "Змінити профіль можна будь-коли командою /edit_profile.",
        reply_markup=get_main_keyboard(),
    )
    if not profile_complete(u):
        await start_onboarding(message.chat.id, state)


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
        target, amount = resolve_user_id(parts[1]), float(parts[2].replace(",", "."))
    except (IndexError, ValueError):
        await message.answer("Формат: /addbalance ID_1001 80  (або Telegram ID замість ID_1001)")
        return
    if target is None:
        await message.answer("Не знайшов такого користувача. Перевір ID (напр. ID_1001).")
        return
    if target not in users_db:
        await message.answer("Такого користувача немає в базі.")
        return
    users_db[target]["balance"] += amount
    request_save()
    await message.answer(f"✅ Баланс {admin_label(target)} поповнено на {amount:.2f} грн.")
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
        reply_markup=get_settings_keyboard(u),
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
# Допомога / чат з адміністратором
# ---------------------------------------------------------------------------
@dp.message(F.text == BTN_HELP)
@dp.message(Command("help"))
async def help_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🆘 <b>Допомога</b>\n\nОбери, що потрібно:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❓ Часті запитання", callback_data="help_faq")],
                [InlineKeyboardButton(text="💬 Написати адміну", callback_data="support_start")],
            ]
        ),
    )


@dp.callback_query(F.data == "help_faq")
async def help_faq(call: types.CallbackQuery):
    await call.message.answer(
        "❓ <b>Часті запитання</b>\n\n"
        "• <b>Як почати чат?</b> — натисни «🔍 Шукати співрозмовника».\n"
        "• <b>Як поповнити баланс?</b> — 👛 Гаманець → 💳 Поповнити баланс "
        "(Telegram Stars або криптою).\n"
        "• <b>Як поскаржитись на співрозмовника?</b> — кнопка «🚨 Поскаржитися» під час чату.\n"
        "• <b>Що дає Premium?</b> — фільтри пошуку, пріоритет у черзі, VIP-значок (дивись 🏪 Магазин).\n"
        "• <b>Як запросити в друзі?</b> — кнопка «🤝 Додати в друзі» під час чату, за взаємною згодою.\n\n"
        "Не знайшов відповіді? Тисни «💬 Написати адміну»."
    )
    await call.answer()


@dp.callback_query(F.data == "support_start")
async def support_start(call: types.CallbackQuery, state: FSMContext):
    await call.message.answer("✍️ Напиши своє повідомлення для адміністратора (або /cancel):")
    await state.set_state(SupportStates.message)
    await call.answer()


@dp.message(SupportStates.message, F.text)
async def support_finish(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    u = init_user(user_id)

    if not ADMIN_ID:
        await message.answer("⚠️ Підтримка тимчасово недоступна.", reply_markup=get_main_keyboard())
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✍️ Відповісти", callback_data=f"adm_reply_{user_id}")]]
    )
    ok = await safe_send(
        ADMIN_ID,
        f"🆘 <b>Повідомлення від</b> {admin_label(user_id)}:\n\n{esc(message.text)}",
        reply_markup=kb,
    )
    if ok:
        await message.answer(
            "✅ Повідомлення надіслано адміну. Відповідь прийде прямо сюди.",
            reply_markup=get_main_keyboard(),
        )
    else:
        await message.answer("❌ Не вдалося надіслати. Спробуй пізніше.", reply_markup=get_main_keyboard())


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
        f"• <b>Нік:</b> {esc(u['nickname'])}"
        + (f"\n\n{topup_progress_text(u)}" if topup_progress_text(u) else ""),
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
    bonus = apply_topup_bonus(u, amount)
    request_save()
    text = f"✅ Оплату отримано! Баланс поповнено на {amount} грн."
    if bonus:
        text += f"\n🎉 Бонус +{bonus:.0f} грн за кожні {TOPUP_BONUS_STEP:.0f} грн поповнень!"
    progress = topup_progress_text(u)
    if progress:
        text += f"\n\n{progress}"
    await message.answer(text, reply_markup=get_main_keyboard())


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
    request_save()

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


# === КІНЕЦЬ part1.py ===
