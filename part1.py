from __future__ import annotations

import asyncio
import html
import logging
import os
import functools
import random
import re
import time
from datetime import date, datetime, timedelta, timezone

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
            stats_day()["active"].add(user.id)
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
# Статистика для адміна: дата (YYYY-MM-DD, Київ) -> лічильники за день. Зберігається в базі.
stats_days: dict[str, dict] = {}
# Поточні чати: user_id -> {"start": час, "mode": режим, "msgs": скільки написав} (лише в пам'яті)
chat_meta: dict[int, dict] = {}
last_chat_mode: dict[int, str] = {}  # у якому режимі був останній чат (для автопошуку)
# Банери-картинки для екранів: місце -> file_id фото в Telegram. Ставить адмін з телефона.
banners: dict[str, str] = {}
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
# Друга вкладка пошуку за інтересами — захоплення (лише для пошуку 1-на-1)
HOBBY_TOPICS = {
    "cook": "🍳 Кулінарія",
    "fitness": "💪 Фітнес",
    "art": "🎨 Малювання",
    "photo": "📸 Фото",
    "instr": "🎸 Музичні інструменти",
    "plants": "🌱 Рослини",
    "cars": "🚗 Авто",
    "yoga": "🧘 Йога та медитація",
    "craft": "✂️ Рукоділля",
    "fishing": "🎣 Риболовля",
    "anime": "🍥 Аніме",
    "dance": "💃 Танці",
}
INTEREST_LABELS = {**ROOM_TOPICS, **HOBBY_TOPICS}
ROOM_CAPACITY = int(os.getenv("ROOM_CAPACITY", "8"))  # макс. учасників в одній кімнаті

pending_rating: dict[int, int] = {}  # хто -> кого ще може оцінити після чату
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


def _kyiv_tz():
    try:
        from zoneinfo import ZoneInfo

        for name in ("Europe/Kyiv", "Europe/Kiev"):
            try:
                return ZoneInfo(name)
            except Exception:  # noqa: BLE001
                pass
    except ImportError:
        pass
    return timezone(timedelta(hours=3))  # запасний варіант, якщо на сервері немає бази часових поясів


KYIV_TZ = _kyiv_tz()


def kyiv_today() -> date:
    """Сьогоднішня дата за Києвом (сервер Render живе за UTC)."""
    return datetime.now(KYIV_TZ).date()


def today_str() -> str:
    return kyiv_today().isoformat()


def yesterday_str() -> str:
    return (kyiv_today() - timedelta(days=1)).isoformat()


BTN_SEARCH = "🔍 Шукати співрозмовника"
BTN_SHOP = "🏪 Магазин"
BTN_WALLET = "👛 Гаманець"
BTN_PROFILE = "👤 Мій профіль / Архів"
BTN_SETTINGS = "⚙️ Налаштування"
BTN_STOP = "❌ Завершити чат"
BTN_REPORT = "🚨 Поскаржитися"
BTN_GIFT = "🎁 Подарувати"
BTN_DAILY = "🎁 Щоденний бонус"
BTN_TASKS = "📋 Завдання"
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
BTN_INTERESTS = "🧩 За інтересами"
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
            [KeyboardButton(text=BTN_FLIRT), KeyboardButton(text=BTN_INTERESTS)],
            [KeyboardButton(text=BTN_TOPUP), KeyboardButton(text=BTN_WALLET)],
            [KeyboardButton(text=BTN_PREMIUM), KeyboardButton(text=BTN_SHOP)],
            [KeyboardButton(text=BTN_DAILY), KeyboardButton(text=BTN_TASKS)],
            [KeyboardButton(text=BTN_PROFILE), KeyboardButton(text=BTN_SETTINGS)],
            [KeyboardButton(text=BTN_FRIENDS), KeyboardButton(text=BTN_ROOMS)],
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


def _onoff(v) -> str:
    return "увімк ✅" if v else "вимк"


def get_settings_keyboard(u: dict | None = None):
    u = u or {}
    media = "без фото/відео 🛡" if u.get("media_mode") == "safe" else "усі"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Мій профіль", callback_data="pe_menu")],
            [InlineKeyboardButton(text=BTN_FILTERS, callback_data="filters_menu")],
            [InlineKeyboardButton(text=f"📷 Медіа від співрозмовника: {media}", callback_data="set_media")],
            [InlineKeyboardButton(text=f"🔁 Автопошук після чату: {_onoff(u.get('auto_search'))}", callback_data="set_auto")],
            [
                InlineKeyboardButton(text="🙈 Приватність", callback_data="set_privacy"),
                InlineKeyboardButton(text="🔔 Сповіщення", callback_data="set_notify"),
            ],
            [InlineKeyboardButton(text="📋 Чорний список", callback_data="bl_view")],
            [
                InlineKeyboardButton(
                    text=f"🔒 Захист моїх медіа: {'увімк ✅' if u.get('protect_media') else 'вимк'} (💎)",
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
            [InlineKeyboardButton(text="🖼 Банери", callback_data="adm_banners")],
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
            "low_warned": False,  # чи попереджали про низький рейтинг
            "interests": set(),  # ключі з INTEREST_LABELS (до 3), для підбору співрозмовника
            "tasks_day": None,  # на яку дату (Київ) видано щоденні завдання
            "tasks_list": [],  # ключі з TASK_POOL на сьогодні
            "tasks_progress": {},  # ключ -> скільки вже зроблено
            "tasks_done": set(),  # виконані сьогодні
            "tasks_bonus": False,  # чи отримано бонус за всі завдання сьогодні
            "media_mode": "all",  # "all" — приймати все, "safe" — без фото/відео/GIF/файлів від співрозмовника
            "auto_search": False,  # після завершення чату одразу шукати наступного
            "hide_age": False,  # не показувати співрозмовнику вік
            "hide_country": False,  # не показувати співрозмовнику країну
            "hide_interests": False,  # не показувати співрозмовнику інтереси
            "notify_tasks": True,  # повідомлення про виконані завдання
            "notify_tips": True,  # підказки (напр. про фільтр за статтю)
            "allow_invites": True,  # приймати запрошення в чат від друзів/минулих співрозмовників
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
        if user_id > 0:
            stat_add("new")
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


RATING_MIN_VOTES = 5  # з якої кількості оцінок показуємо рейтинг
LOW_RATING_MIN_VOTES = 8  # з якої кількості оцінок можна вважати рейтинг низьким
LOW_RATING_PERCENT = 40  # нижче цього % 👍 — «низький рейтинг», такі рідше потрапляють до інших


def rating_percent(u: dict) -> int | None:
    up, down = u.get("rating_up", 0), u.get("rating_down", 0)
    return round(up * 100 / (up + down)) if up + down else None


def is_low_rated(u: dict) -> bool:
    up, down = u.get("rating_up", 0), u.get("rating_down", 0)
    return up + down >= LOW_RATING_MIN_VOTES and up * 100 < LOW_RATING_PERCENT * (up + down)


def short_info(u: dict, public: bool = True) -> str:
    """public=True — як бачить співрозмовник (з урахуванням приватності), False — як бачить сам власник."""
    badge = " 💎" if is_premium(u) else ""
    vip = " ⭐VIP" if has_perk(u, "vip_badge") else ""
    pct = rating_percent(u)
    rating = f" · 👍 {pct}%" if pct is not None and u.get("rating_up", 0) + u.get("rating_down", 0) >= RATING_MIN_VOTES else ""
    age = "🙈 вік приховано" if public and u.get("hide_age") else esc(u["age"])
    country = "🙈 країну приховано" if public and u.get("hide_country") else esc(u["country"])
    return f"{esc(u['gender'])}, {age}, {country}{badge}{vip}{rating}"


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
        on_chat_ended(user_id, partner_id)
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
    pending_rating[chat_id] = partner_id  # оцінити можна лише останнього співрозмовника і лише раз
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
    record_topup("crypto", info["credit"])
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
            "stats_days": stats_days,
            "banners": banners,
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
    stats_days.clear()
    stats_days.update(data.get("stats_days", {}))
    banners.clear()
    banners.update(data.get("banners", {}))

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




# === КІНЕЦЬ part1.py ===
