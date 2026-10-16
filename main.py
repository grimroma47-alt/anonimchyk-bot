from __future__ import annotations

import asyncio
import html
import logging
import os
import functools
import random
import re
import time
from urllib.parse import quote
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
            CUR_LANG.set((users_db.get(user.id) or {}).get("lang", "uk") if LANG_RU_ENABLED else "uk")
            last_seen[user.id] = time.time()
            stats_day()["active"].add(user.id)
            known = users_db.get(user.id)
            if known is not None:
                known["tg_username"] = user.username
                known["tg_name"] = " ".join(x for x in (user.first_name, user.last_name) if x) or None
    except Exception:  # noqa: BLE001 — облік онлайну ніколи не має ламати обробку повідомлень
        pass
    return await handler(event, data)

# ---------------------------------------------------------------------------
# 🌐 Мови: перекладач «на виході». Українська — основна; російська вмикається LANG_RU=1 у Render.
# Код бота пише тексти українською, а перед відправкою людині з мовою "ru" вони перекладаються
# за словником RU_PAIRS (файл lang_ru.py). Кнопки, натиснуті російською, перекладаються назад.
# Повідомлення людей одне одному НЕ перекладаються (copy_to і захищені шматки protect_text).
# ---------------------------------------------------------------------------
import contextvars

LANG_RU_ENABLED = os.getenv("LANG_RU", "0") == "1"
LANG_LABELS = {"uk": "🇺🇦 Українська", "ru": "🌐 Русский"}
CUR_LANG: contextvars.ContextVar = contextvars.ContextVar("cur_lang", default="uk")
_P0, _P1 = "⁣", "⁤"  # невидимі межі «не перекладати» (текст людей)

if "RU_PAIRS" not in globals():
    try:
        from lang_ru import RU_PAIRS  # коли main.py запускається напряму, а не через запускач
    except ImportError:
        RU_PAIRS = {}
        if LANG_RU_ENABLED:
            logging.warning("🌐 LANG_RU=1, але файл lang_ru.py не знайдено поруч із ботом — переклад вимкнено")

_CYR = "а-яіїєґёА-ЯІЇЄҐЁ'ʼ"
_GRN_RX = re.compile(f"(?<![{_CYR}])грн(?![{_CYR}])")
_tr_rx = None
_tr_map: dict = {}
_tr_back: dict = {}


def _tr_build():
    global _tr_rx, _tr_map, _tr_back
    _tr_map = {k: v for k, v in RU_PAIRS.items() if k and k != v}
    parts = []
    for k in sorted(_tr_map, key=len, reverse=True):
        if len(k.strip()) < 3:
            continue  # короткі шматки («з», «Ні») — лише як цілий текст, не всередині речень
        pat = re.escape(k)
        if re.match(f"[{_CYR}]", k[0]):
            pat = f"(?<![{_CYR}])" + pat
        if re.match(f"[{_CYR}]", k[-1]):
            pat += f"(?![{_CYR}])"
        parts.append(pat)
    _tr_rx = re.compile("|".join(parts)) if parts else None
    # зворотний словник лише для цілих фраз (кнопки, короткі відповіді)
    _tr_back = {v.strip(): k.strip() for k, v in _tr_map.items() if "\n" not in k and len(k) <= 64}


def protect_text(text) -> str:
    """Позначає текст людини, щоб перекладач його не чіпав."""
    return f"{_P0}{text}{_P1}"


def tr_ru(text: str) -> str:
    if _tr_rx is None:
        _tr_build()
    if not text or _tr_rx is None:
        return text
    if text in _tr_map:
        out = _tr_map[text]
    else:
        out = _tr_rx.sub(lambda m: _tr_map.get(m.group(0), m.group(0)), text)
    # для російськомовних ціни показуємо в зірках Telegram: 1 грн балансу = 1 ⭐
    return _GRN_RX.sub("⭐", out)


def translate_out(text, lang: str):
    """Переклад вихідного тексту; шматки між _P0/_P1 лишаються як є. Для "uk" лише прибирає позначки."""
    if not isinstance(text, str):
        return text
    if _P0 not in text:
        return tr_ru(text) if lang == "ru" else text
    out = []
    for i, chunk in enumerate(re.split(f"({_P0}.*?{_P1})", text, flags=re.S)):
        if chunk.startswith(_P0) and chunk.endswith(_P1):
            out.append(chunk[1:-1])
        else:
            out.append(tr_ru(chunk) if lang == "ru" else chunk)
    return "".join(out)


DEFAULT_COMMANDS: list = []  # заповнюється при старті; для меню команд мовою людини


async def apply_lang_commands(user_id: int, lang: str):
    """Меню команд (кнопка «/») мовою, яку людина обрала в боті."""
    if not DEFAULT_COMMANDS or user_id == ADMIN_ID:
        return  # у адміна свій список команд — не чіпаємо
    try:
        scope = types.BotCommandScopeChat(chat_id=user_id)
        if lang == "ru":
            cmds = [types.BotCommand(command=c.command, description=tr_ru(c.description)) for c in DEFAULT_COMMANDS]
            await bot.set_my_commands(cmds, scope=scope)
        else:
            await bot.delete_my_commands(scope=scope)
    except Exception as e:  # noqa: BLE001
        logging.warning("Команди для мови: %s", e)


def user_lang(user_id) -> str:
    if not LANG_RU_ENABLED:
        return "uk"
    u = users_db.get(user_id) if isinstance(user_id, int) else None
    return (u or {}).get("lang", "uk") if u is not None else CUR_LANG.get()


def _tr_markup(markup, lang):
    if markup is None:
        return None
    if hasattr(markup, "inline_keyboard"):
        rows = [[b.model_copy(update={"text": translate_out(b.text, lang)}) for b in row] for row in markup.inline_keyboard]
        return markup.model_copy(update={"inline_keyboard": rows})
    if hasattr(markup, "keyboard"):
        rows = [[b.model_copy(update={"text": translate_out(b.text, lang)}) for b in row] for row in markup.keyboard]
        return markup.model_copy(update={"keyboard": rows})
    return markup


async def i18n_request_middleware(make_request, bot_, method):
    """Перекладає все, що бот надсилає (текст, підписи, кнопки, спливаючі відповіді)."""
    try:
        chat_id = getattr(method, "chat_id", None)
        lang = user_lang(chat_id) if isinstance(chat_id, int) else (CUR_LANG.get() if LANG_RU_ENABLED else "uk")
        upd = {}
        for name in ("text", "caption", "title", "description"):
            val = getattr(method, name, None)
            if isinstance(val, str) and (lang == "ru" or _P0 in val):
                upd[name] = translate_out(val, lang)
        markup = getattr(method, "reply_markup", None)
        if lang == "ru" and markup is not None:
            upd["reply_markup"] = _tr_markup(markup, lang)
        if upd:
            method = method.model_copy(update=upd)
    except Exception as e:  # noqa: BLE001 — переклад ніколи не має ламати відправку
        logging.warning("i18n out: %s", e)
    return await make_request(bot_, method)


try:
    bot.session.middleware(i18n_request_middleware)
except Exception as e:  # noqa: BLE001
    logging.warning("i18n: не вдалося підключити перекладач: %s", e)


@dp.message.outer_middleware()
async def i18n_incoming(handler, event, data):
    """Кнопки, натиснуті російською, перетворюємо назад на українські — і стара логіка працює як є."""
    try:
        uid = event.from_user.id if event.from_user else None
        text = event.text
        if text and user_lang(uid) == "ru":
            if _tr_rx is None:
                _tr_build()
            back = _tr_back.get(text.strip())
            in_talk = uid in active_chats or uid in user_room
            # у чаті перекладаємо лише кнопки (починаються з емодзі), а не слова, які людина пише співрозмовнику
            if back and (not in_talk or not re.match(f"[{_CYR}A-Za-z0-9]", text.strip()[0])):
                new = event.model_copy(update={"text": back})
                try:
                    new = new.as_(event.bot)  # прив'язка до бота, щоб message.answer() працював
                except Exception:  # noqa: BLE001
                    pass
                event = new
    except Exception as e:  # noqa: BLE001
        logging.warning("i18n in: %s", e)
    return await handler(event, data)


# ---------------------------------------------------------------------------
# "База даних" у пам'яті (скидається при кожному перезапуску Render!)
# Для постійного зберігання підключи PostgreSQL (Render Postgres) або SQLite з диском.
# ---------------------------------------------------------------------------
users_db: dict[int, dict] = {}
queue: list[int] = []
queue_since: dict[int, float] = {}  # user_id -> коли став у чергу (щоб прибирати тих, хто пішов)
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
# Щотижневий конкурс запрошень: {"week": "2026-W41", "counts": {user_id: [кількість, час останнього]},
# "awarded": [тижні, за які призи вже видано], "history": [{"week", "winners": [(user_id, n, приз)]}]}
ref_contest: dict = {"week": None, "counts": {}, "awarded": [], "history": []}
# 🏆 Топ тижня співрозмовників (за 👍/👎). Тиждень = з неділі 20:00 до неділі 20:00 за Києвом.
# {"week", "votes": {user_id: [👍, 👎]}, "pairs": {"хто:кого"}, "published": [тижні], "history": [{"week","winners"}]}
top_week: dict = {"week": None, "votes": {}, "pairs": set(), "published": [], "history": []}
meet_hour: dict = {"reminded": None, "summary": None, "chats_start": None, "peak": 0, "sent": 0}
last_chat_seconds: dict[int, float] = {}  # тривалість щойно завершеного чату (для права оцінки в топі)
pending_top: dict[int, bool] = {}  # чи піде наступна оцінка цієї людини в топ тижня
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
# Призи — ТІЛЬКИ всередині бота (перки й подарунки), жодних гривень.
# Так рулетка не є азартною грою на гроші й не заважає майбутньому виводу коштів.
# (шанс у %, тип, параметр): perk -> (ключ перка, секунд), gift -> (мін. ціна, макс. ціна)
LOTTERY_TABLE = [
    (30, "nothing", None),
    (22, "perk", ("priority", DAY)),
    (20, "gift", (10, 25)),
    (15, "perk", ("premium", DAY)),
    (7, "perk", ("gender_filter", 3 * DAY)),
    (4, "perk", ("premium", 7 * DAY)),
    (1.5, "gift", (35, 100)),
    (0.5, "perk", ("premium", 30 * DAY)),
]
LOTTERY_PERK_NAMES = {
    "priority": "🚀 Пріоритет у пошуку",
    "premium": "💎 Premium",
    "gender_filter": "🎯 Фільтр за статтю",
}


def spin_lottery():
    r = random.uniform(0, 100)
    cumulative = 0.0
    for weight, kind, param in LOTTERY_TABLE:
        cumulative += weight
        if r <= cumulative:
            return kind, param
    return "nothing", None


def lottery_days_word(days: int) -> str:
    if days % 10 == 1 and days % 100 != 11:
        return f"{days} день"
    if days % 10 in (2, 3, 4) and days % 100 not in (12, 13, 14):
        return f"{days} дні"
    return f"{days} днів"


def lottery_random_gift(lo: float, hi: float) -> str:
    keys = [k for k, (_t, p) in GIFT_CATALOG.items() if lo <= p <= hi]
    return random.choice(keys or list(GIFT_CATALOG.keys()))


def lottery_apply_prize(u: dict, kind: str, param) -> str:
    """Видає приз і повертає текст. Гроші на баланс НЕ нараховуються ніколи."""
    if kind == "nothing":
        return "😔 На цей раз нічого не випало. Спробуй ще раз!"
    if kind == "perk":
        perk, seconds = param
        current = u["perks"].get(perk, 0)
        if current == float("inf"):
            # вже назавжди — замість перка даємо подарунок
            kind, param = "gift", (10, 25)
        else:
            u["perks"][perk] = max(time.time(), current) + seconds
            days = int(seconds // DAY)
            big = perk == "premium" and days >= 30
            head = "🏆 <b>ДЖЕКПОТ!</b> " if big else "🎉 Виграш: "
            return f"{head}<b>{LOTTERY_PERK_NAMES[perk]} на {lottery_days_word(days)}</b>! Вже активовано."
    key = lottery_random_gift(*param)
    title, _price = GIFT_CATALOG[key]
    u.setdefault("gifts", {})
    u["gifts"][key] = u["gifts"].get(key, 0) + 1
    return f"🎁 Виграш: подарунок <b>{esc(title)}</b>! Додано в інвентар."


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
    "top_week": ("🏆 Був у топі", lambda u: u.get("top_week_count", 0) > 0),
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


class MoodStates(StatesGroup):
    song = State()


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
    media = "без фото/відео 🛡" if media_is_safe(u) else "усі"
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
            [InlineKeyboardButton(text="🏆 Конкурс запрошень", callback_data="contest_open")],
            [InlineKeyboardButton(text="📋 Чорний список", callback_data="bl_view")],
            *([[InlineKeyboardButton(text=f"🌐 Мова / Язык: {LANG_LABELS.get(u.get('lang', 'uk'), '')}", callback_data="lang_menu")]]
              if LANG_RU_ENABLED else []),
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
            [InlineKeyboardButton(text="🔗 Посилання з мітками", callback_data="adm_links")],
            [InlineKeyboardButton(text="🎁 Подарувати подарунок", callback_data="adm_gift")],
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


DEFAULT_NICK_RE = re.compile(r"^(Користувач|Незнайомець|Незнайомка)_\d+$")


def default_nickname(u: dict) -> str:
    """Автонік: «Незнайомка_N» для дівчат, «Незнайомець_N» для решти; N — порядковий номер з ID."""
    num = re.sub(r"\D", "", str(u.get("custom_id", ""))) or "0"
    base = "Незнайомка" if u.get("gender") == "Дівчина" else "Незнайомець"
    return f"{base}_{num}"


def refresh_default_nick(u: dict) -> None:
    """Оновлює нік за статтю, лише якщо людина не ставила власний (купленим ніком не чіпаємо)."""
    if DEFAULT_NICK_RE.match(str(u.get("nickname", ""))):
        u["nickname"] = default_nickname(u)


def init_user(user_id: int) -> dict:
    global user_counter
    if user_id not in users_db:
        user_counter += 1
        users_db[user_id] = {
            "custom_id": f"ID_{user_counter}",
            "nickname": f"Незнайомець_{user_counter}",
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
            "notify_waiting": True,  # кликати, коли хтось чекає співрозмовника
            "notify_meet": True,  # ⏰ нагадування про Годину знайомств (щодня о 20:55)
            "mood": None,  # 🎭 ключ з MOODS (діє MOOD_TTL)
            "mood_at": 0.0,  # коли обрав настрій
            "song": None,  # 🎵 пісня настрою (текст від людини, до SONG_MAX символів)
            "song_at": 0.0,  # коли вказав пісню
            "wait_ping_at": 0.0,  # коли востаннє кликали цю людину (час)
            "joined_at": None,  # коли вперше зайшов у бот (час); у старих користувачів невідомо
            "tg_username": None,  # @username у Telegram (для списку новачків в адмінці)
            "tg_name": None,  # ім'я в Telegram
            "lang": "uk",  # мова інтерфейсу: "uk" або "ru"
            "top_week_count": 0,  # скільки разів потрапляв у топ-3 тижня
            "founder_no": None,  # 👑 номер засновника (перші FOUNDERS_LIMIT, хто справді поспілкувався)
            "source": None,  # звідки прийшов: tt / yt / ig / ref / direct / власна мітка (з посилання ?start=...)
            "media_mode_set": False,  # чи людина сама обирала режим медіа (інакше для неповнолітніх — «без фото/відео»)
            "safety_memo_shown": False,  # чи показували пам'ятку з безпеки (для неповнолітніх)
            "ref_qualified": False,  # чи вже зарахований своєму запрошувачу в конкурсі (після першого чату)
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
            users_db[user_id]["joined_at"] = time.time()
            try:
                stats_day().setdefault("new_ids", []).append(user_id)
            except Exception as e:  # noqa: BLE001 — статистика ніколи не має ламати бота
                logging.warning("Статистика новачків: %s", e)
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
    if viewer.get("lang", "uk") != candidate.get("lang", "uk"):
        return False  # люди з різною мовою інтерфейсу не з'єднуються
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
    vip += " 👑" if u.get("founder_no") else ""
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


def is_minor(u: dict | None) -> bool:
    age = get_age_int(u) if u else None
    return age is not None and age < 18


def media_is_safe(u: dict | None) -> bool:
    """Чи НЕ приймає людина фото/відео від співрозмовника. Неповнолітнім — так за замовчуванням."""
    if not u:
        return False
    if u.get("media_mode_set"):
        return u.get("media_mode") == "safe"
    return u.get("media_mode") == "safe" or is_minor(u)


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
    pending_top[chat_id] = last_chat_seconds.pop(chat_id, 0.0) >= TOP_MIN_CHAT_SECONDS
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
            "ref_contest": ref_contest,
            "top_week": top_week,
            "meet_hour": meet_hour,
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
    ref_contest.clear()
    ref_contest.update(data.get("ref_contest") or {"week": None, "counts": {}, "awarded": [], "history": []})
    top_week.clear()
    top_week.update(data.get("top_week") or {"week": None, "votes": {}, "pairs": set(), "published": [], "history": []})
    meet_hour.clear()
    meet_hour.update(data.get("meet_hour") or {"reminded": None, "summary": None, "chats_start": None, "peak": 0, "sent": 0})

    # Якщо в нових версіях бота з'являться нові поля профілю — додаємо їх старим користувачам.
    saved_counter = user_counter
    template = init_user(-1)
    users_db.pop(-1, None)
    user_counter = saved_counter
    old_chatters = sorted(
        (uid for uid, u in users_db.items() if "founder_no" not in u and u.get("total_chats") and uid != ADMIN_ID),
        key=lambda uid: int(re.sub(r"\D", "", str(users_db[uid].get("custom_id", ""))) or 0),
    )
    for u in users_db.values():
        if "ref_qualified" not in u:
            # ті, хто вже спілкувався до запуску конкурсу, не зараховуються в нього заднім числом
            u["ref_qualified"] = bool(u.get("total_chats"))
        for key, value in template.items():
            if key not in u:
                u[key] = copy.deepcopy(value)
        refresh_default_nick(u)  # «Користувач_N» → «Незнайомець_N» / «Незнайомка_N»
    # 👑 ті, хто спілкувався ще до появи значка, стають першими засновниками (за порядком приходу)
    next_no = max((u.get("founder_no") or 0 for u in users_db.values()), default=0) + 1
    for uid in old_chatters:
        if next_no > FOUNDERS_LIMIT:
            break
        if uid not in banned_users and not users_db[uid].get("founder_no"):
            users_db[uid]["founder_no"] = next_no
            next_no += 1


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
# Мітки джерел: t.me/<бот>?start=tt — щоб бачити, звідки приходять люди
# ---------------------------------------------------------------------------
SOURCE_LABELS = {
    "tt": "🎵 TikTok",
    "yt": "▶️ YouTube",
    "ig": "📸 Instagram",
    "tgads": "📣 Telegram Ads",
    "ref": "🤝 Запрошення друзів",
    "direct": "🔎 Напряму / пошук",
}


def source_from_start(arg: str) -> str:
    arg = (arg or "").strip()
    if not arg:
        return "direct"
    if arg.startswith("ref_"):
        return "ref"
    code = re.sub(r"[^a-z0-9_]", "", arg.lower())[:32]
    return code or "direct"


def source_label(code: str | None) -> str:
    if not code:
        return "❔ невідомо (до міток)"
    return SOURCE_LABELS.get(code, f"💬 {code}")


def source_breakdown(ids: list[int]) -> list[tuple[str, int, int]]:
    """[(джерело, скільки прийшло, скільки з них уже спілкувались)] — найбільші зверху."""
    agg: dict[str, list[int]] = {}
    for uid in ids:
        u = users_db.get(uid)
        if not u:
            continue
        row = agg.setdefault(u.get("source") or "", [0, 0])
        row[0] += 1
        if u.get("total_chats", 0) > 0:
            row[1] += 1
    return sorted(((k, v[0], v[1]) for k, v in agg.items()), key=lambda x: -x[1])


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
        u["source"] = source_from_start(parts[1] if len(parts) == 2 else "")
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

    code = (getattr(message.from_user, "language_code", None) or "uk").lower()
    if LANG_RU_ENABLED and is_new_user and not code.startswith("uk"):
        await message.answer(
            "🌐 Обери мову / Выбери язык:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=LANG_LABELS["uk"], callback_data="lang_new_uk"),
                InlineKeyboardButton(text=LANG_LABELS["ru"], callback_data="lang_new_ru"),
            ]]),
        )
        return
    await send_welcome(message.chat.id, message.from_user.first_name, u, state, message)


async def send_welcome(chat_id: int, first_name, u: dict, state: FSMContext, message=None):
    user_id = chat_id
    welcome = (
        f"Привіт, {protect_text(esc(first_name))}! Вітаємо в анонімному чаті! 🤫\n\n"
        f"Твій унікальний номер: <b>{u['custom_id']}</b>\n"
        "Змінити профіль можна будь-коли: ⚙️ Налаштування → ✏️ Мій профіль.\n\n"
        "📜 Користуючись ботом, ти погоджуєшся з правилами: /rules"
    )
    if not u.get("founder_no") and founder_promo_line():
        welcome += "\n\n" + founder_promo_line()
    if not await send_banner(user_id, "start", caption=welcome, reply_markup=get_main_keyboard()):
        if message is not None:
            await message.answer(welcome, reply_markup=get_main_keyboard())
        else:
            await safe_send(chat_id, welcome, reply_markup=get_main_keyboard())
    if not profile_complete(u):
        await start_onboarding(chat_id, state)


@dp.callback_query(F.data.in_({"lang_new_uk", "lang_new_ru"}))
async def lang_new_pick(call: types.CallbackQuery, state: FSMContext):
    u = init_user(call.from_user.id)
    u["lang"] = "ru" if call.data == "lang_new_ru" else "uk"
    request_save()
    CUR_LANG.set(u["lang"])
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await call.answer()
    await apply_lang_commands(call.from_user.id, u["lang"])
    await send_welcome(call.from_user.id, call.from_user.first_name, u, state)


@dp.callback_query(F.data == "lang_menu")
async def lang_menu(call: types.CallbackQuery):
    await call.message.answer(
        "🌐 Обери мову / Выбери язык:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=LANG_LABELS["uk"], callback_data="lang_set_uk"),
            InlineKeyboardButton(text=LANG_LABELS["ru"], callback_data="lang_set_ru"),
        ]]),
    )
    await call.answer()


@dp.callback_query(F.data.in_({"lang_set_uk", "lang_set_ru"}))
async def lang_set(call: types.CallbackQuery):
    uid = call.from_user.id
    u = init_user(uid)
    new = "ru" if call.data == "lang_set_ru" else "uk"
    if uid in active_chats or uid in queue or uid in user_room:
        await call.answer("Спочатку заверши чат або пошук.", show_alert=True)
        return
    u["lang"] = new
    request_save()
    CUR_LANG.set(new)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await call.answer()
    await apply_lang_commands(uid, new)
    await safe_send(uid, "✅ Мову змінено: українська." if new == "uk" else "✅ Язык изменён: русский.",
                    reply_markup=get_main_keyboard())


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
        + (f"• <b>Засновник:</b> 👑 №{u['founder_no']} з {FOUNDERS_LIMIT}\n" if u.get("founder_no") else "")
        + f"• <b>Рівень:</b> {get_level_title(u)} ({u.get('total_chats', 0)} чатів)\n"
        f"• <b>Серія входів:</b> {u.get('checkin_streak', 0)} 🔥\n"
        f"• <b>Рейтинг:</b> {rating_line(u)}\n"
        f"• <b>Інтереси:</b> {interests_line(u)}\n"
        f"• <b>Настрій:</b> {mood_line(u)}\n\n"
        f"🏆 <b>Досягнення:</b> {achievements_text}\n\n"
        f"🎒 <b>Інвентар подарунків:</b> {gifts_text}\n\n"
        f"📜 <b>Архів останніх змін профілю:</b>\n{archive_text}\n\n"
        "Хочеш оновити дані? Введи /edit_profile"
    )
    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🧩 Мої інтереси", callback_data="pi_open"),
                              InlineKeyboardButton(text="🎭 Мій настрій", callback_data="mood_open")]]
        ),
    )


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
    refresh_default_nick(u)
    u["age"] = data.get("age", u["age"])
    u["country"] = country

    await state.clear()
    kb = get_chat_keyboard() if message.from_user.id in active_chats else get_main_keyboard()
    await message.answer("✅ Профіль оновлено! Попередні дані збережено в архів.", reply_markup=kb)
    await maybe_send_safety_memo(message.from_user.id, u)


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
        "Натискай кнопки нижче — усе змінюється одразу.\n"
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
    await auto_search_after_chat(user_id)
    await auto_search_after_chat(partner_id)


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
        "• <b>Правила й безпека</b> — /rules та /safety. Питання щодо оплат — /paysupport.\n\n"
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
        f"🆘 <b>Повідомлення від</b> {admin_label(user_id)}:\n\n{protect_text(esc(message.text))}",
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
    task_event(message.from_user.id, "daily")

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
        stat_add("daily_paid", DAILY_BONUS_AMOUNT)
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
    record_topup("stars", amount)
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
    if DEFAULT_NICK_RE.match(nick):
        await message.answer("Такі ніки бот видає автоматично — придумай свій 🙂 Або /cancel.")
        return
    if u["balance"] < NICK_PRICE:
        await state.clear()
        await message.answer("❌ Недостатньо коштів.")
        return
    u["balance"] -= NICK_PRICE
    record_spend("shop", NICK_PRICE)
    u["nickname"] = nick
    await state.clear()
    await message.answer(f"🎉 Нікнейм змінено на <b>{esc(nick)}</b> (списано {NICK_PRICE} грн).")


# ---------------------------------------------------------------------------
# Магазин
# ---------------------------------------------------------------------------
@dp.message(F.text == BTN_SHOP)
async def shop_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await send_banner(message.from_user.id, "shop")
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
            f"❌ Недостатньо коштів. Ціна: {price} грн. Ваш баланс: {u['balance']:.2f} грн.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="💳 Поповнити баланс", callback_data="topup_open")]]
            ),
        )
        await call.answer()
        return

    u["balance"] -= price
    record_spend("premium" if perk == "premium" else "shop", price)
    request_save()
    now = time.time()
    if seconds is None:
        u["perks"][perk] = float("inf")
    else:
        start = max(now, u["perks"].get(perk, 0))  # продовжуємо, а не перезаписуємо
        u["perks"][perk] = start + seconds

    await call.message.answer(f"🎉 Куплено: <b>{esc(title)}</b>. Списано {price} грн.")
    if key == "gender_filter":
        await call.message.answer(
            "👫 Яку стать співрозмовника шукати?", reply_markup=get_filter_gender_keyboard(u)
        )
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
    record_spend("gifts", price)
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
# Поповнення на довільну суму (велика кнопка в меню) + бонус за кожні 500 грн
# ---------------------------------------------------------------------------
TOPUP_AMOUNT_REGEX = re.compile(r"^\s*(\d{1,6})(?:[.,]0+)?\s*(?:грн|uah|₴)?\s*$", re.IGNORECASE)


def get_topup_amounts_keyboard():
    row1 = [
        InlineKeyboardButton(text=f"{a} грн", callback_data=f"topup_amt_{a}") for a in TOPUP_QUICK_AMOUNTS[:3]
    ]
    row2 = [
        InlineKeyboardButton(text=f"{a} грн", callback_data=f"topup_amt_{a}") for a in TOPUP_QUICK_AMOUNTS[3:]
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row for row in (row1, row2) if row])


async def topup_ask_amount(message: types.Message, user_id: int, state: FSMContext):
    u = init_user(user_id)
    await state.set_state(TopupStates.amount)
    text = (
        "💳 <b>Поповнення балансу</b>\n\n"
        f"Напиши суму в гривнях ({TOPUP_MIN}–{TOPUP_MAX}) або обери готову нижче.\n"
    )
    if TOPUP_BONUS_STEP > 0 and TOPUP_BONUS_AMOUNT > 0:
        text += (
            f"\n🎁 За кожні <b>{TOPUP_BONUS_STEP:.0f} грн</b> поповнень — бонус "
            f"<b>+{TOPUP_BONUS_AMOUNT:.0f} грн</b>!\n{topup_progress_text(u)}"
        )
    await message.answer(text, reply_markup=get_topup_amounts_keyboard())


async def topup_show_methods(message: types.Message, user_id: int, amount: int):
    u = init_user(user_id)
    total = float(u.get("topup_total") or 0.0)
    bonus = calc_topup_bonus(total, amount)
    text = f"💳 Сума поповнення: <b>{amount} грн</b>\n"
    if bonus:
        text += f"🎉 Ця оплата принесе бонус <b>+{bonus:.0f} грн</b>!\n"
    elif TOPUP_BONUS_STEP > 0 and TOPUP_BONUS_AMOUNT > 0:
        left = TOPUP_BONUS_STEP - ((total + amount) % TOPUP_BONUS_STEP)
        text += f"🎁 Після цієї оплати до бонусу лишиться {left:.0f} грн.\n"
    text += "\nОбери спосіб оплати:"

    buttons = [[InlineKeyboardButton(text=f"⭐ Telegram Stars ({amount} ⭐)", callback_data=f"topup_stars_{amount}")]]
    if CRYPTO_PAY_TOKEN:
        usdt = round(amount / USD_UAH_RATE, 2)
        buttons.append(
            [InlineKeyboardButton(text=f"💎 Крипта (~{usdt} USDT)", callback_data=f"topup_crypto_{amount}")]
        )
    ua_only = user_lang(user_id) != "ru"  # Банка й переказ на картку — лише для українців (гривня)
    if mono_jar_link() and ua_only:
        buttons.append(
            [InlineKeyboardButton(text="🫙 Оплатити через Банку monobank", callback_data=f"topup_jar_{amount}")]
        )
    if (PAY_CARD or PAY_IBAN) and ua_only:
        buttons.append(
            [InlineKeyboardButton(text="🏦 Переказ на картку / за реквізитами", callback_data=f"topup_bank_{amount}")]
        )
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


def _parse_topup_amount(raw: str) -> int | None:
    try:
        amount = int(raw)
    except (TypeError, ValueError):
        return None
    if not (TOPUP_MIN <= amount <= TOPUP_MAX):
        return None
    return amount


@dp.message(F.text == BTN_TOPUP)
@dp.message(Command("topup"))
async def topup_start(message: types.Message, state: FSMContext):
    await send_banner(message.from_user.id, "topup")
    await topup_ask_amount(message, message.from_user.id, state)


@dp.callback_query(F.data == "topup_open")
async def topup_open(call: types.CallbackQuery, state: FSMContext):
    await topup_ask_amount(call.message, call.from_user.id, state)
    await call.answer()


def _is_topup_amount_text(message: types.Message) -> bool:
    return bool(message.text and TOPUP_AMOUNT_REGEX.match(message.text))


@dp.message(TopupStates.amount, _is_topup_amount_text)
async def topup_amount_entered(message: types.Message, state: FSMContext):
    m = TOPUP_AMOUNT_REGEX.match(message.text)
    amount = _parse_topup_amount(m.group(1)) if m else None
    if amount is None:
        await message.answer(f"Сума має бути від {TOPUP_MIN} до {TOPUP_MAX} грн. Спробуй ще раз або /cancel.")
        return
    await state.clear()
    await topup_show_methods(message, message.from_user.id, amount)


@dp.callback_query(F.data.startswith("topup_amt_"))
async def topup_amount_button(call: types.CallbackQuery, state: FSMContext):
    amount = _parse_topup_amount(call.data[len("topup_amt_"):])
    if amount is None:
        await call.answer("Недоступна сума.", show_alert=True)
        return
    await state.clear()
    await topup_show_methods(call.message, call.from_user.id, amount)
    await call.answer()


@dp.callback_query(F.data.startswith("topup_stars_"))
async def topup_pay_stars(call: types.CallbackQuery):
    amount = _parse_topup_amount(call.data[len("topup_stars_"):])
    if amount is None:
        await call.answer("Недоступна сума.", show_alert=True)
        return
    try:
        await bot.send_invoice(
            chat_id=call.message.chat.id,
            title=f"Поповнення на {amount} грн",
            description=f"Купівля {amount} Telegram Stars для поповнення балансу бота",
            payload=f"stars_{amount}_{call.from_user.id}",
            provider_token="",  # для Stars (валюта XTR) токен провайдера не потрібен
            currency="XTR",
            prices=[types.LabeledPrice(label=f"{amount} Stars", amount=amount)],
        )
    except TelegramAPIError as e:
        logging.warning("Не вдалося створити рахунок Stars на %s: %s", amount, e)
        await call.message.answer(
            "❌ Telegram не прийняв рахунок на таку суму в Stars. Спробуй меншу суму або оплату криптою."
        )
    await call.answer()


@dp.callback_query(F.data.startswith("topup_crypto_"))
async def topup_pay_crypto(call: types.CallbackQuery):
    if not CRYPTO_PAY_TOKEN:
        await call.answer("Оплата криптою зараз недоступна", show_alert=True)
        return
    amount = _parse_topup_amount(call.data[len("topup_crypto_"):])
    if amount is None:
        await call.answer("Недоступна сума.", show_alert=True)
        return
    usdt = round(amount / USD_UAH_RATE, 2)
    result = await create_crypto_invoice(call.from_user.id, usdt)
    if result is None:
        await call.message.answer(
            "❌ Не вдалося створити рахунок. Можливо, сума замала для крипти — спробуй більшу або Stars."
        )
        await call.answer()
        return

    invoice_id = str(result["invoice_id"])
    pay_url = result.get("pay_url") or result.get("bot_invoice_url") or result.get("mini_app_invoice_url")
    pending_crypto_invoices[invoice_id] = {
        "user_id": call.from_user.id,
        "amount": usdt,
        "credit": float(amount),  # зараховуємо рівно ту суму в грн, яку людина обрала
    }
    request_save()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатити", url=pay_url)],
            [InlineKeyboardButton(text="✅ Перевірити оплату", callback_data=f"dep_check_{invoice_id}")],
        ]
    )
    await call.message.answer(
        f"Рахунок на {usdt} USDT створено.\n"
        f"Після оплати баланс поповниться на {amount} грн автоматично, "
        "або натисни «Перевірити оплату».",
        reply_markup=kb,
    )
    await call.answer()


# ---------------------------------------------------------------------------
# Захист медіа (Premium) і платне розблокування після автобану
# ---------------------------------------------------------------------------
async def _toggle_protect(user_id: int) -> str:
    u = init_user(user_id)
    if not is_premium(u):
        return (
            "🔒 <b>Захист медіа</b> доступний з Premium 💎\n\n"
            "Співрозмовники не зможуть пересилати й зберігати твої фото, відео та повідомлення. "
            f"Premium можна придбати в «{BTN_SHOP}»."
        )
    u["protect_media"] = not u.get("protect_media")
    request_save()
    if u["protect_media"]:
        return (
            "🔒 Захист медіа <b>увімкнено</b>.\n"
            "Співрозмовники та друзі не зможуть пересилати чи зберігати твої повідомлення й медіа "
            "(на більшості телефонів — і робити скріншоти)."
        )
    return "🔓 Захист медіа <b>вимкнено</b>."


@dp.callback_query(F.data == "toggle_protect")
async def toggle_protect_button(call: types.CallbackQuery):
    text = await _toggle_protect(call.from_user.id)
    try:
        await call.message.edit_reply_markup(reply_markup=get_settings_keyboard(init_user(call.from_user.id)))
    except TelegramAPIError:
        pass
    await call.message.answer(text)
    await call.answer()


@dp.message(Command("silent"))
async def silent_command(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(await _toggle_protect(message.from_user.id))


@dp.callback_query(F.data == "unban_buy")
async def unban_buy(call: types.CallbackQuery):
    user_id = call.from_user.id
    u = init_user(user_id)
    if user_id not in banned_users:
        await call.answer("Ти не заблокований 🙂", show_alert=True)
        return
    if u.get("ban_type") != "auto":
        await call.answer("Цей бан можна зняти лише через адміністратора (🆘 Допомога).", show_alert=True)
        return

    price = unban_price(u)
    if u["balance"] < price:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="💳 Поповнити баланс", callback_data="topup_open")]]
        )
        await call.message.answer(
            f"❌ Недостатньо коштів. Розблокування коштує {price:.0f} грн, на балансі {u['balance']:.2f} грн.",
            reply_markup=kb,
        )
        await call.answer()
        return

    u["balance"] -= price
    record_spend("unban", price)
    banned_users.discard(user_id)
    u["ban_type"] = None
    u["reports_received"] = 0
    request_save()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    next_price = UNBAN_BASE_PRICE + UNBAN_PRICE_STEP * int(u.get("ban_count") or 1)
    await call.message.answer(
        f"✅ Доступ відновлено! Списано {price:.0f} грн.\n"
        f"Будь ласка, дотримуйся правил — наступне розблокування коштуватиме {next_price:.0f} грн.",
        reply_markup=get_main_keyboard(),
    )
    if ADMIN_ID:
        await safe_send(
            ADMIN_ID,
            f"🔓 Користувач {admin_label(user_id)} викупив розблокування за {price:.0f} грн "
            f"(автобан №{u.get('ban_count', 1)}).",
        )
    await call.answer()


# ---------------------------------------------------------------------------
# Онлайн: скільки людей зараз у боті
# ---------------------------------------------------------------------------
@dp.message(F.text == BTN_ONLINE)
@dp.message(Command("online"))
async def online_stats(message: types.Message, state: FSMContext):
    await state.clear()
    now = time.time()
    online = sum(1 for ts in last_seen.values() if now - ts <= ONLINE_WINDOW)
    online = max(online, 1)  # той, хто натиснув кнопку, точно онлайн
    in_chats = len(active_chats)
    searching = len(queue)
    flirting = sum(1 for uid in queue if search_mode.get(uid) == "flirt")
    flirt_boys = sum(
        1 for uid in queue if search_mode.get(uid) == "flirt" and users_db.get(uid, {}).get("gender") == "Хлопець"
    )
    flirt_note = f"{flirting} (👦 {flirt_boys} · 👧 {flirting - flirt_boys})" if flirting else "0"
    in_rooms = len(user_room)
    await message.answer(
        "👥 <b>Зараз у боті</b>\n\n"
        f"🟢 Онлайн: <b>{online}</b>\n"
        f"💬 Спілкуються в чатах: <b>{in_chats}</b>\n"
        f"🔍 Шукають співрозмовника: <b>{searching}</b> (з них ❤️ флірт: {flirt_note})\n"
        f"🏠 У кімнатах: <b>{in_rooms}</b>\n\n"
        f"{meet_status_line()}\n\n"
        f"<i>Онлайн — ті, хто був активний за останні {ONLINE_WINDOW // 60} хв.</i>"
    )


# ---------------------------------------------------------------------------
# Сторінка Premium: переваги, статус, тарифи зі знижкою, безкоштовно через друзів
# ---------------------------------------------------------------------------
PREMIUM_PLAN_KEYS = ["prem_1w", "prem_1m", "prem_3m", "prem_6m", "prem_1y", "prem_forever"]


def premium_status_text(u: dict) -> str:
    until = u.get("perks", {}).get("premium", 0)
    if until == float("inf"):
        return "✅ У тебе <b>безлімітний Premium</b> 💎"
    if until > time.time():
        days = int((until - time.time()) // DAY) + 1
        return f"✅ Premium активний до <b>{time.strftime('%d.%m.%Y', time.gmtime(until))}</b> (ще ~{days} дн.)"
    return "❌ Premium не активний"


def premium_plans() -> list[tuple[str, str, int, str]]:
    """(ключ, назва, ціна, примітка про вигоду) — на основі цін у SHOP."""
    week = SHOP.get("prem_1w")
    base_per_day = week[1] / 7 if week else None
    plans = []
    for key in PREMIUM_PLAN_KEYS:
        item = SHOP.get(key)
        if not item:
            continue
        title, price, _perk, seconds = item
        note = ""
        if seconds and base_per_day and key != "prem_1w":
            per_day = price / (seconds / DAY)
            saving = round((1 - per_day / base_per_day) * 100)
            per_month = per_day * 30
            if saving > 0:
                note = f"−{saving}%" if seconds <= 31 * DAY else f"≈{per_month:.0f} грн/міс, −{saving}%"
        plans.append((key, title.replace("Premium на ", "").replace("Безлімітний Premium (назавжди)", "Назавжди"), price, note))
    return plans


def premium_page():
    lines = [
        "💎 <b>Premium</b>\n",
        "Що дає Premium:",
        "🚀 <b>Пріоритет у пошуку</b> — ти першим у черзі",
        "🎯 <b>Фільтри</b> — стать, вік і країна співрозмовника",
        f"🚫 <b>Більший чорний список</b> — до {BLACKLIST_LIMIT_PLUS} замість {BLACKLIST_LIMIT_FREE}",
        "🔒 <b>Захист медіа</b> — твої фото й повідомлення не можна переслати чи зберегти (/silent)",
        "📣 <b>Без реклами</b> під час пошуку",
        "📋 <b>Нагороди за завдання ×2</b>",
        "💎 <b>Значок</b> у профілі й досягнення",
        "",
    ]
    rows = []
    for key, title, price, note in premium_plans():
        label = f"{title} — {price} грн" + (f" ({note})" if note else "")
        rows.append([InlineKeyboardButton(text=label, callback_data=f"buy_{key}")])
    rows.append([InlineKeyboardButton(text="🎁 Отримати безкоштовно", callback_data="prem_free")])
    rows.append([InlineKeyboardButton(text="🏆 Конкурс: Premium на 30 днів", callback_data="contest_open")])
    rows.append([InlineKeyboardButton(text="💳 Поповнити баланс", callback_data="topup_open")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(F.text == BTN_PREMIUM)
@dp.message(Command("premium"))
async def premium_menu(message: types.Message, state: FSMContext):
    await state.clear()
    u = init_user(message.from_user.id)
    await send_banner(message.from_user.id, "premium")
    text, kb = premium_page()
    text += f"{premium_status_text(u)}\n💰 Баланс: {u['balance']:.2f} грн\n\nОбери тариф:"
    await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data == "prem_free")
async def premium_free(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    link = (
        f"https://t.me/{BOT_USERNAME}?start=ref_{call.from_user.id}"
        if BOT_USERNAME
        else "(посилання буде доступне трохи пізніше)"
    )
    done = u.get("referral_count", 0)
    left = REFERRAL_PREMIUM_EVERY - (done % REFERRAL_PREMIUM_EVERY) if REFERRAL_PREMIUM_EVERY else 0
    await call.message.answer(
        "🎁 <b>Premium безкоштовно</b>\n\n"
        f"Запроси {REFERRAL_PREMIUM_EVERY} друзів за своїм посиланням — отримаєш "
        f"<b>{REFERRAL_PREMIUM_DAYS} днів Premium</b>. А за кожного друга ще й +{REFERRAL_BONUS:.0f} грн на баланс.\n\n"
        f"Твоє посилання:\n<code>{esc(link)}</code>\n\n"
        f"Запрошено: {done}. До наступного Premium: {left}."
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
    await send_banner(message.from_user.id, "lottery")
    u = init_user(message.from_user.id)
    await message.answer(
        "🎰 <b>Рулетка</b>\n\n"
        f"Один спін коштує {LOTTERY_COST:.0f} грн.\n\n"
        "<b>Що може випасти:</b>\n"
        "🚀 Пріоритет у пошуку на 1 день — 22%\n"
        "🎁 Подарунок в інвентар — 21,5%\n"
        "💎 Premium на 1 день — 15%\n"
        "🎯 Фільтр за статтю на 3 дні — 7%\n"
        "💎 Premium на 7 днів — 4%\n"
        "🏆 Джекпот: Premium на 30 днів — 0,5%\n\n"
        "Призи діють лише в боті, гривні з рулетки не випадають.\n"
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
    record_spend("lottery", LOTTERY_COST)

    kind, param = spin_lottery()
    text = lottery_apply_prize(u, kind, param)
    request_save()

    text += f"\n\n💰 Баланс: {u['balance']:.2f} грн."
    await call.message.answer(text, reply_markup=get_lottery_keyboard())
    await call.answer()


# ---------------------------------------------------------------------------
# Групові кімнати за інтересами
# ---------------------------------------------------------------------------
@dp.message(F.text == BTN_ROOMS)
@dp.message(Command("rooms"))
async def rooms_menu(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id

    if user_id in banned_users:
        ban_text, ban_kb = banned_notice(user_id)
        await message.answer(ban_text, reply_markup=ban_kb)
        return
    if user_id in user_room:
        await message.answer("Ти вже в кімнаті. Спочатку вийди з неї.", reply_markup=get_room_keyboard())
        return
    if user_id in active_chats:
        await message.answer("Спочатку заверши приватний чат.", reply_markup=get_chat_keyboard())
        return

    await message.answer(
        "👥 <b>Кімнати за інтересами</b>\n\n"
        f"Обери тему — потрапиш у групу до {ROOM_CAPACITY} людей, які говорять про те саме. "
        "У кімнатах поки підтримується лише текст — це для безпеки спілкування в групі.",
        reply_markup=get_room_topics_keyboard(),
    )


@dp.callback_query(F.data.startswith("room_join_"))
async def room_join(call: types.CallbackQuery):
    user_id = call.from_user.id

    if user_id in banned_users:
        await call.answer("⛔ Вас заблоковано в цьому боті.", show_alert=True)
        return
    if user_id in user_room:
        await call.answer("Ти вже в кімнаті. Спочатку вийди з неї.", show_alert=True)
        return
    if user_id in active_chats or user_id in queue:
        await call.answer("Спочатку заверши приватний чат або пошук.", show_alert=True)
        return

    topic_key = call.data[len("room_join_"):]
    if topic_key not in ROOM_TOPICS:
        await call.answer("Невідома тема", show_alert=True)
        return

    global room_counter
    target_room_id = None
    lang = (users_db.get(user_id) or {}).get("lang", "uk")
    for rid, r in rooms.items():
        if r["topic"] == topic_key and r.get("lang", "uk") == lang and len(r["members"]) < ROOM_CAPACITY:
            target_room_id = rid
            break
    if target_room_id is None:
        room_counter += 1
        target_room_id = f"room_{room_counter}"
        rooms[target_room_id] = {"topic": topic_key, "members": set(), "lang": lang}

    room = rooms[target_room_id]
    u = init_user(user_id)

    for member_id in room["members"]:
        await safe_send(member_id, f"🆕 <b>{esc(u['nickname'])}</b> приєднався до кімнати!")

    room["members"].add(user_id)
    user_room[user_id] = target_room_id

    await call.message.answer(
        f"✅ Ти приєднався до кімнати: <b>{esc(ROOM_TOPICS[topic_key])}</b> "
        f"({len(room['members'])}/{ROOM_CAPACITY} 👥)\n\n"
        "Просто пиши текстом — повідомлення побачать усі учасники кімнати.",
        reply_markup=get_room_keyboard(),
    )
    await call.answer()


@dp.message(F.text == BTN_ROOM_LEAVE)
async def room_leave(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    room_id = user_room.get(user_id)
    if room_id is None:
        await message.answer("Ти зараз не в кімнаті.", reply_markup=get_main_keyboard())
        return

    room = rooms.get(room_id)
    u = init_user(user_id)
    remove_from_room(user_id)

    if room:
        for member_id in room["members"]:
            await safe_send(member_id, f"🚪 <b>{esc(u['nickname'])}</b> покинув кімнату.")

    await message.answer("Ти вийшов з кімнати.", reply_markup=get_main_keyboard())


@dp.message(F.text == BTN_ROOM_REPORT)
async def room_report_start(message: types.Message):
    user_id = message.from_user.id
    room_id = user_room.get(user_id)
    if room_id is None:
        await message.answer("Ти зараз не в кімнаті.", reply_markup=get_main_keyboard())
        return
    room = rooms.get(room_id)
    others = [uid for uid in room["members"] if uid != user_id] if room else []
    if not others:
        await message.answer("У кімнаті, крім тебе, нікого немає.")
        return

    rows = [
        [InlineKeyboardButton(text=users_db[uid]["nickname"], callback_data=f"roomrep_{uid}")]
        for uid in others
    ]
    await message.answer(
        "🚨 На кого з учасників кімнати поскаржитись?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(F.data.startswith("roomrep_"))
async def room_report_submit(call: types.CallbackQuery):
    global report_counter
    target_id = int(call.data[len("roomrep_"):])
    user_id = call.from_user.id

    report_counter += 1
    reports.append(
        {
            "id": report_counter,
            "from": user_id,
            "on": target_id,
            "time": time.strftime("%d.%m.%Y %H:%M"),
            "status": "нова",
        }
    )

    p = init_user(target_id)
    p["reports_received"] = p.get("reports_received", 0) + 1
    auto_banned = False
    if p["reports_received"] >= AUTO_BAN_REPORTS and target_id not in banned_users:
        banned_users.add(target_id)
        register_auto_ban(p)
        request_save()
        auto_banned = True
        if target_id in queue:
            queue.remove(target_id)
        remove_from_room(target_id)
        end_chat(target_id)
        ban_text, ban_kb = banned_notice(target_id)
        await safe_send(target_id, ban_text, reply_markup=ban_kb)

    if ADMIN_ID:
        extra = (
            f"\n\n⛔ Автобан: досягнуто {AUTO_BAN_REPORTS} скарг, користувача заблоковано автоматично."
            if auto_banned
            else ""
        )
        await safe_send(
            ADMIN_ID,
            f"🚨 Скарга з кімнати #{report_counter}\nВід: {admin_label(user_id)}\nНа: {admin_label(target_id)}{extra}",
        )

    await call.message.answer("🚨 Скаргу надіслано, дякуємо!")
    await call.answer()


# ---------------------------------------------------------------------------
# Пошук та чат
# ---------------------------------------------------------------------------
@dp.message(F.text == BTN_SEARCH)
async def search_partner(message: types.Message, state: FSMContext):
    await run_search(message, state, "normal")


FLIRT_MIN_AGE = 18
FLIRT_RULES = (
    "❤️ <b>Флірт-чат</b>\n"
    "• Повага і згода — понад усе. «Ні» означає «ні».\n"
    "• Жодних інтимних фото без явної згоди співрозмовника.\n"
    "• Не тиснемо і не просимо грошей чи контактів.\n"
    f"• Порушують — тисни «{BTN_REPORT}»."
)


def min_age_ever(u: dict) -> int | None:
    """Найменший вік, який будь-коли був у профілі (поточний, архів, онбординг)."""
    ages = []
    cur = get_age_int(u)
    if cur is not None:
        ages.append(cur)
    if u.get("min_age_seen") is not None:
        ages.append(int(u["min_age_seen"]))
    for line in u.get("archive") or []:
        m = re.search(r"Вік:\s*(\d+)", line)
        if m:
            ages.append(int(m.group(1)))
    return min(ages) if ages else None


def flirt_block_reason(u: dict) -> str | None:
    """Чому людині не можна у флірт-пошук (або None — можна)."""
    age = get_age_int(u)
    if age is None or u.get("gender") not in ("Хлопець", "Дівчина"):
        return "❤️ Щоб увімкнути флірт-пошук, вкажи у профілі стать і вік: /edit_profile"
    if age < FLIRT_MIN_AGE:
        return f"❤️ Флірт-пошук доступний лише з {FLIRT_MIN_AGE} років."
    # Перевірку історії віку (min_age_ever) вимкнено за рішенням власника.
    # Щоб увімкнути: якщо min_age_ever(u) < FLIRT_MIN_AGE — повертати відмову.
    return None


@dp.message(F.text == BTN_FLIRT)
@dp.message(Command("flirt"))
async def flirt_search(message: types.Message, state: FSMContext):
    u = init_user(message.from_user.id)
    reason = flirt_block_reason(u)
    if reason and message.from_user.id not in banned_users:
        await state.clear()
        await message.answer(reason)
        return
    await run_search(message, state, "flirt")


async def run_search(message: types.Message, state: FSMContext, mode: str = "normal", user_id: int | None = None):
    await state.clear()
    user_id = user_id or message.from_user.id

    if user_id in banned_users:
        ban_text, ban_kb = banned_notice(user_id)
        await message.answer(ban_text, reply_markup=ban_kb)
        return

    u = init_user(user_id)

    if user_id in active_chats:
        await message.answer("Ти вже перебуваєш у чаті!", reply_markup=get_chat_keyboard())
        return
    if user_id in queue:
        await message.answer("Ти вже в черзі пошуку. Зачекай трохи... ⏳")
        return
    if user_id in user_room:
        await message.answer("Спочатку вийди з групової кімнати.", reply_markup=get_room_keyboard())
        return

    best = None  # (бали, позиція в черзі)
    stale = []  # давно чекають — спершу питаємо «ти тут?», а не з'єднуємо наосліп
    now_ts = time.time()
    for i, candidate_id in enumerate(queue):
        if search_mode.get(candidate_id, "normal") != mode:
            continue  # флірт шукає лише флірт, звичайний — лише звичайний
        p_candidate = init_user(candidate_id)
        if is_blacklisted(u, user_id, p_candidate, candidate_id):
            continue
        if mode == "flirt" and p_candidate.get("gender") == u.get("gender"):
            continue  # у флірті з'єднуємо лише хлопця з дівчиною
        if not passes_filters(u, p_candidate) or not passes_filters(p_candidate, u):
            continue
        if now_ts - queue_since.get(candidate_id, now_ts) > QUEUE_STALE_AFTER:
            stale.append(candidate_id)
            continue
        score = match_score(u, p_candidate, mode)
        if best is None or score > best[0]:
            best = (score, i)
            if score >= MATCH_SCORE_MAX:
                break
    match_index = best[1] if best else None

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
            queue_since[user_id] = time.time()
            search_mode[user_id] = mode
            await message.answer("Співрозмовник виявився недоступним. Шукаємо далі... ⏳")
            return

        u["total_chats"] = u.get("total_chats", 0) + 1
        p["total_chats"] = p.get("total_chats", 0) + 1
        on_chat_started(user_id, partner_id, mode)

        await message.answer(
            f"Партнера знайдено! 🤫\nІнфо: {short_info(p)}", reply_markup=get_chat_keyboard()
        )
        if mode == "flirt":
            await message.answer(FLIRT_RULES)
            await safe_send(partner_id, FLIRT_RULES)
        elif mode.startswith("int:"):
            topic_note = f"🧩 Ваша спільна тема: <b>{INTEREST_LABELS.get(mode[4:], mode[4:])}</b> — є з чого почати 😉"
            await message.answer(topic_note)
            await safe_send(partner_id, topic_note)
        if not mode.startswith("int:"):
            await send_interest_notes(user_id, u, partner_id, p)
        await send_vibe_notes(user_id, u, partner_id, p)
        await send_match_safety(user_id, u)
        await send_match_safety(partner_id, p)
    else:
        if has_perk(u, "priority") or is_premium(u):
            queue.insert(0, user_id)
        else:
            queue.append(user_id)
        queue_since[user_id] = time.time()
        search_mode[user_id] = mode
        for sid in stale:
            await ask_still_here(sid)
        if mode == "flirt":
            looking_for = "дівчину" if u.get("gender") == "Хлопець" else "хлопця"
            wait_text = f"❤️ Шукаємо {looking_for} для флірту... Зачекай ⏳"
        elif mode.startswith("int:"):
            wait_text = (
                f"🧩 Шукаємо співрозмовника за темою {INTEREST_LABELS.get(mode[4:], mode[4:])}... Зачекай ⏳\n"
                f"Якщо довго нікого немає — натисни «{BTN_STOP}» і спробуй звичайний пошук."
            )
        else:
            wait_text = "Шукаємо співрозмовника... Зачекай ⏳"
            schedule_filter_nudge(user_id)
        if not current_mood(u) and mode != "flirt":
            wait_text += "\n\n🎭 Поки чекаєш — вкажи свій настрій: /mood. Бот спершу шукатиме когось на одній хвилі."
        await message.answer(wait_text)
        await maybe_show_ad(user_id)
        schedule_stuck_offer(user_id, mode)
        schedule_wait_ping(user_id, mode)
        schedule_admin_wait_alert(user_id, mode)


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
    await auto_search_after_chat(user_id)
    await auto_search_after_chat(partner_id)


@dp.callback_query(F.data.startswith("rate_up_"))
async def rate_up_handler(call: types.CallbackQuery):
    target_id = int(call.data[len("rate_up_"):])
    if pending_rating.get(call.from_user.id) != target_id:
        await call.answer("Оцінку вже враховано 🙂")
        return
    pending_rating.pop(call.from_user.id, None)
    u = init_user(target_id)
    u["rating_up"] = u.get("rating_up", 0) + 1
    if pending_top.pop(call.from_user.id, False):
        top_record_vote(call.from_user.id, target_id, True)
    task_event(call.from_user.id, "rate")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await call.answer("Дякуємо за оцінку! 👍")


@dp.callback_query(F.data.startswith("rate_down_"))
async def rate_down_handler(call: types.CallbackQuery):
    target_id = int(call.data[len("rate_down_"):])
    if pending_rating.get(call.from_user.id) != target_id:
        await call.answer("Оцінку вже враховано 🙂")
        return
    pending_rating.pop(call.from_user.id, None)
    u = init_user(target_id)
    u["rating_down"] = u.get("rating_down", 0) + 1
    if pending_top.pop(call.from_user.id, False):
        top_record_vote(call.from_user.id, target_id, False)
    task_event(call.from_user.id, "rate")
    await maybe_warn_low_rating(target_id, u)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await call.answer("Дякуємо за оцінку! 👎")


# ---------------------------------------------------------------------------
# Реконнект з минулим співрозмовником і запрошення друзів у чат (за згодою)
# ---------------------------------------------------------------------------
async def try_send_chat_invite(requester_id: int, target_id: int | None) -> str:
    """Надсилає target_id запит почати чат із requester_id. Повертає текст помилки, або "" при успіху."""
    if not target_id or target_id not in users_db:
        return "Користувача не знайдено."
    if requester_id in banned_users or target_id in banned_users:
        return "Недоступно."
    if requester_id in active_chats or target_id in active_chats:
        return "Хтось із вас зараз уже в іншому чаті."
    if requester_id in queue or target_id in queue:
        return "Хтось із вас зараз у черзі пошуку."
    if requester_id in user_room or target_id in user_room:
        return "Хтось із вас зараз у груповій кімнаті."

    u = init_user(requester_id)
    p = init_user(target_id)
    if is_blacklisted(u, requester_id, p, target_id):
        return "Недоступно."
    if not p.get("allow_invites", True):
        return "Ця людина зараз не приймає запрошень у чат."

    reconnect_requests[target_id] = requester_id
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Прийняти", callback_data="reconnect_accept"),
                InlineKeyboardButton(text="❌ Відхилити", callback_data="reconnect_decline"),
            ]
        ]
    )
    ok = await safe_send(
        target_id, f"🔄 <b>{esc(u['nickname'])}</b> хоче почати з тобою чат. Прийняти?", reply_markup=kb
    )
    if not ok:
        reconnect_requests.pop(target_id, None)
        return "Співрозмовник зараз недоступний."
    return ""


@dp.callback_query(F.data == "reconnect_request")
async def reconnect_request(call: types.CallbackQuery):
    user_id = call.from_user.id
    u = init_user(user_id)
    target_id = u.get("last_partner_id")

    if not target_id:
        await call.answer("Немає з ким відновлювати зв'язок.", show_alert=True)
        return

    error = await try_send_chat_invite(user_id, target_id)
    if error:
        await call.answer(error, show_alert=True)
    else:
        await call.answer("Запит надіслано! Чекай на відповідь.", show_alert=True)


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
    if requester_id in user_room or acceptor_id in user_room:
        await call.answer("Хтось із вас зараз у груповій кімнаті.", show_alert=True)
        return

    if requester_id in queue:
        queue.remove(requester_id)
    if acceptor_id in queue:
        queue.remove(acceptor_id)

    active_chats[requester_id] = acceptor_id
    active_chats[acceptor_id] = requester_id
    on_chat_started(requester_id, acceptor_id, "friend")

    await call.message.answer("✅ Чат розпочато!", reply_markup=get_chat_keyboard())
    await safe_send(
        requester_id, "✅ Співрозмовник прийняв запит — чат розпочато!", reply_markup=get_chat_keyboard()
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
# Друзі (повністю окремий механізм від реконнекту — нижче)
# ---------------------------------------------------------------------------
@dp.message(F.text == BTN_ADD_FRIEND)
async def add_friend_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    partner_id = active_chats.get(user_id)
    if partner_id is None:
        await message.answer("Додавати в друзі можна лише під час чату.", reply_markup=get_main_keyboard())
        return

    u = init_user(user_id)
    if partner_id in (u.get("friends") or set()):
        await message.answer("Ви вже друзі! 👫")
        return

    friend_add_requests[partner_id] = user_id
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Прийняти", callback_data="friend_accept"),
                InlineKeyboardButton(text="❌ Відхилити", callback_data="friend_decline"),
            ]
        ]
    )
    ok = await safe_send(
        partner_id, f"🤝 <b>{esc(u['nickname'])}</b> хоче додати тебе в друзі. Прийняти?", reply_markup=kb
    )
    if ok:
        await message.answer("🤝 Запит надіслано!")
    else:
        friend_add_requests.pop(partner_id, None)
        await message.answer("❌ Не вдалося надіслати запит.")


@dp.callback_query(F.data == "friend_accept")
async def friend_accept(call: types.CallbackQuery):
    acceptor_id = call.from_user.id
    requester_id = friend_add_requests.pop(acceptor_id, None)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass

    if requester_id is None:
        await call.answer("Запит уже неактуальний.", show_alert=True)
        return

    u = init_user(acceptor_id)
    p = init_user(requester_id)
    u.setdefault("friends", set()).add(requester_id)
    p.setdefault("friends", set()).add(acceptor_id)

    await call.message.answer(
        f"🤝 Тепер ви з <b>{esc(p['nickname'])}</b> у друзях! Знайдеш їх у вкладці «{BTN_FRIENDS}»."
    )
    await safe_send(requester_id, f"🤝 <b>{esc(u['nickname'])}</b> прийняв твій запит у друзі!")
    await call.answer()


@dp.callback_query(F.data == "friend_decline")
async def friend_decline(call: types.CallbackQuery):
    acceptor_id = call.from_user.id
    requester_id = friend_add_requests.pop(acceptor_id, None)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass

    if requester_id is not None:
        await safe_send(requester_id, "❌ Запит у друзі відхилено.")
    await call.answer("Відхилено.")


@dp.message(F.text == BTN_FRIENDS)
@dp.message(Command("friends"))
async def friends_list(message: types.Message, state: FSMContext):
    await state.clear()
    u = init_user(message.from_user.id)
    kb = get_friends_keyboard(u)
    if kb is None:
        await message.answer(
            "👫 У тебе поки немає друзів.\n"
            "Додай когось під час чату кнопкою «🤝 Додати в друзі» — за взаємною згодою."
        )
        return
    await message.answer(
        "👫 <b>Твої друзі</b>\n\n"
        "Натисни «✍️» біля імені, щоб написати другу, або «❌», щоб прибрати з друзів.\n\n"
        "💡 Переписка з друзями працює завжди — навіть коли ти в анонімному чаті чи шукаєш нового "
        "співрозмовника. Щоб відповісти другу, просто зроби свайп (Reply) на його повідомлення.",
        reply_markup=kb,
    )


def _remember_friend_msg(recipient_id: int, message_id: int, sender_id: int):
    if len(friend_msg_map) >= FRIEND_MSG_MAP_LIMIT:
        # прибираємо найстаріший запис (dict зберігає порядок додавання)
        friend_msg_map.pop(next(iter(friend_msg_map)), None)
    friend_msg_map[(recipient_id, message_id)] = sender_id


async def deliver_friend_message(sender_id: int, target_id: int, message: types.Message) -> str | None:
    """Надсилає повідомлення другу. Повертає текст помилки або None, якщо все ок."""
    u = init_user(sender_id)
    p = users_db.get(target_id)
    if (
        p is None
        or target_id not in (u.get("friends") or set())
        or sender_id not in (p.get("friends") or set())
    ):
        return "Ця людина більше не у твоїх друзях."
    if sender_id in banned_users or target_id in banned_users:
        return "Недоступно."
    if is_blacklisted(u, sender_id, p, target_id):
        return "Недоступно."

    text_to_check = message.text or message.caption
    if text_to_check and LINK_REGEX.search(text_to_check):
        return "🚫 Посилання та контакти заборонені — спілкуйтесь у боті."

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="↩️ Відповісти", callback_data=f"fmsg_{sender_id}")]]
    )
    header = f"💌 <b>{esc(u['nickname'])}</b> (друг):"
    protect = should_protect(u)
    try:
        if message.text:
            sent = await bot.send_message(
                target_id, f"{header}\n{protect_text(esc(message.text))}", reply_markup=kb, protect_content=protect
            )
            _remember_friend_msg(target_id, sent.message_id, sender_id)
        else:
            head = await bot.send_message(target_id, header)
            _remember_friend_msg(target_id, head.message_id, sender_id)
            copied = await message.copy_to(chat_id=target_id, reply_markup=kb, protect_content=protect)
            _remember_friend_msg(target_id, copied.message_id, sender_id)
    except TelegramAPIError:
        return "Не вдалося доставити — друг зараз недоступний."
    return None


@dp.callback_query(F.data.startswith("fmsg_"))
async def friend_write_start(call: types.CallbackQuery, state: FSMContext):
    try:
        target_id = int(call.data[len("fmsg_"):])
    except ValueError:
        await call.answer()
        return

    u = init_user(call.from_user.id)
    if target_id not in (u.get("friends") or set()):
        await call.answer("Ця людина більше не у твоїх друзях.", show_alert=True)
        return

    f = users_db.get(target_id)
    name = f["nickname"] if f else str(target_id)
    await state.set_state(FriendStates.write)
    await state.update_data(friend_target=target_id)
    await call.message.answer(
        f"✍️ Напиши повідомлення для <b>{esc(name)}</b> (або /cancel).\n"
        "Анонімний чат при цьому не переривається."
    )
    await call.answer()


@dp.callback_query(F.data.startswith("friend_remove_"))
async def friend_remove(call: types.CallbackQuery):
    target_id = int(call.data[len("friend_remove_"):])
    u = init_user(call.from_user.id)
    u.get("friends", set()).discard(target_id)
    p = users_db.get(target_id)
    if p:
        p.get("friends", set()).discard(call.from_user.id)

    kb = get_friends_keyboard(u)
    if kb is None:
        try:
            await call.message.edit_text("👫 У тебе більше немає друзів у списку.")
        except TelegramAPIError:
            await call.message.answer("👫 У тебе більше немає друзів у списку.")
    else:
        try:
            await call.message.edit_reply_markup(reply_markup=kb)
        except TelegramAPIError:
            pass
    await call.answer("Прибрано з друзів.")


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
        register_auto_ban(p)
        request_save()
        auto_banned = True
        if partner_id in queue:
            queue.remove(partner_id)
        remove_from_room(partner_id)
        ban_text, ban_kb = banned_notice(partner_id)
        await safe_send(partner_id, ban_text, reply_markup=ban_kb)

    if ADMIN_ID:
        extra = (
            f"\n\n⛔ Автобан: досягнуто {AUTO_BAN_REPORTS} скарг, користувача заблоковано автоматично."
            if auto_banned
            else ""
        )
        await safe_send(
            ADMIN_ID,
            f"🚨 Нова скарга #{report_counter}\nВід: {admin_label(user_id)}\nНа: {admin_label(partner_id)}{extra}\n\n"
            "Переглянути список: /admin",
        )
    end_chat(user_id)
    await message.answer(
        "🚨 Скаргу надіслано, чат завершено. Дякуємо!", reply_markup=get_main_keyboard()
    )
    await safe_send(partner_id, "Співрозмовник завершив чат.", reply_markup=get_main_keyboard())
    await auto_search_after_chat(user_id)
    await auto_search_after_chat(partner_id)


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
    task_event(user_id, "gift")

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
    # functools.wraps — щоб aiogram бачив справжні параметри функції (call, state тощо)
    # і не передавав у неї зайвих аргументів.
    @functools.wraps(func)
    async def wrapper(call: types.CallbackQuery, *args, **kwargs):
        if call.from_user.id != ADMIN_ID or call.from_user.id not in authorized_admins:
            await call.answer("Доступ заборонено", show_alert=True)
            return
        return await func(call, *args, **kwargs)

    return wrapper


STATS_PERIODS = {
    "today": ("Сьогодні", 1, 0),
    "yday": ("Вчора", 1, 1),
    "7": ("7 днів", 7, 0),
    "30": ("30 днів", 30, 0),
}
TOPUP_SOURCES = {"stars": "⭐ Stars", "card": "💳 Реквізити", "jar": "🏦 Банка", "crypto": "🪙 Крипта"}
SPEND_CATEGORIES = {
    "premium": "💎 Premium",
    "shop": "🏪 Магазин",
    "gifts": "🎁 Подарунки",
    "lottery": "🎰 Рулетка",
    "unban": "🔓 Розбан",
}


def _days_back(count: int, skip: int = 0) -> list[str]:
    today = kyiv_today()
    return [(today - timedelta(days=skip + i)).isoformat() for i in range(count)]


def _sum_stats(days: list[str]) -> dict:
    total = _new_stats_day()
    for day in days:
        d = stats_days.get(day)
        if not d:
            continue
        for key, val in d.items():
            if key == "active":
                total["active"] |= val
            elif isinstance(val, dict):
                for sub, amount in val.items():
                    total[key][sub] = total[key].get(sub, 0) + amount
            else:
                total[key] = total.get(key, 0) + val
    return total


def _spark(values: list[float]) -> str:
    bars = "▁▂▃▄▅▆▇█"
    top = max(values) if values else 0
    if not top:
        return "▁" * len(values)
    return "".join(bars[min(int(v / top * (len(bars) - 1) + 0.5), len(bars) - 1)] for v in values)


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _money(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ")


def admin_stats_text(period: str) -> str:
    title, count, skip = STATS_PERIODS.get(period, STATS_PERIODS["today"])
    days = _days_back(count, skip)
    t = _sum_stats(days)
    now = time.time()
    online = sum(1 for ts in last_seen.values() if now - ts <= ONLINE_WINDOW)
    premium_count = sum(1 for u in users_db.values() if is_premium(u))

    if count == 1:
        when = datetime.fromisoformat(days[0]).strftime("%d.%m")
        head = f"📅 <b>{title}</b> ({when})"
    else:
        head = f"📅 <b>За {title}</b>"

    topup_total = sum(t["topup"].values())
    topup_parts = " · ".join(f"{lbl} {_money(t['topup'][k])}" for k, lbl in TOPUP_SOURCES.items() if t["topup"].get(k))
    spent_total = sum(t["spent"].values())
    spent_parts = " · ".join(
        f"{lbl} {_money(t['spent'][k])}" for k, lbl in SPEND_CATEGORIES.items() if t["spent"].get(k)
    )
    lines = [
        "📊 <b>Статистика</b>",
        "",
        f"👥 Усього користувачів: <b>{len(users_db)}</b> · 💎 Premium: {premium_count} · ⛔ бан: {len(banned_users)}",
        f"🟢 Зараз: онлайн {online} · у чатах {len(active_chats) // 2} · шукають {len(queue)} · у кімнатах {len(user_room)}",
        "",
        head,
        f"👑 Засновників: <b>{founders_count()}</b> з {FOUNDERS_LIMIT}",
        f"🆕 Нових: <b>{t['new']}</b>",
        *[
            f"     {source_label(code)}: {n}" + (f" (спілкувались {chatted})" if n else "")
            for code, n, chatted in source_breakdown(list(dict.fromkeys(t.get("new_ids", []))))
        ],
        f"🙋 Активних: <b>{len(t['active'])}</b>",
        f"💬 Чатів: <b>{t['chats']}</b> (справжніх від 1 хв: {t['real_chats']})",
        f"     ❤️ флірт {t['flirt']} · 🧩 інтереси {t['interest']} · 👫 друзі {t['friends']}",
        f"✉️ Повідомлень: {t['msgs']}",
        f"👋 Покликано «хтось шукає»: {t.get('wait_pings', 0)}",
        "",
        f"💰 Поповнення: <b>{_money(topup_total)} грн</b> ({t['topup_n']} {_plural(t['topup_n'], 'оплата', 'оплати', 'оплат')})",
    ]
    if topup_parts:
        lines.append(f"     {topup_parts}")
    lines.append(f"🛒 Витратили з балансу: <b>{_money(spent_total)} грн</b>")
    if spent_parts:
        lines.append(f"     {spent_parts}")
    lines.append(
        f"🎁 Роздано безкоштовно: щоденні {_money(t['daily_paid'])} грн · "
        f"завдання {_money(t['tasks_paid'])} грн ({t['tasks_done']} виконано)"
    )
    if count > 1:
        week = list(reversed(_days_back(count)))
        new_series = [stats_days.get(d, {}).get("new", 0) for d in week]
        money_series = [sum(stats_days.get(d, {}).get("topup", {}).values()) for d in week]
        active_series = [len(stats_days.get(d, {}).get("active", ())) for d in week]
        lines += [
            "",
            "📈 По днях (зліва старіші):",
            f"🆕 <code>{_spark(new_series)}</code> макс {max(new_series)}",
            f"🙋 <code>{_spark(active_series)}</code> макс {max(active_series)}",
            f"💰 <code>{_spark(money_series)}</code> макс {_money(max(money_series))} грн",
        ]
    lines += [
        "",
        "📦 <b>За весь час</b>",
        f"• Дохід з подарунків: {gift_revenue_total:.0f} грн · з рулетки: {lottery_revenue_total:.0f} грн",
        f"• Запрошено друзями: {sum(u.get('referral_count', 0) for u in users_db.values())}",
        f"• Скарг: {len(reports)} · кімнат зараз: {len(rooms)}",
    ]
    if t["topup"].get("stars"):
        lines.append("\n<i>⭐ Stars показані як зараховані грн — реально після комісії Telegram отримаєш менше.</i>")
    return "\n".join(lines)


def admin_stats_keyboard(period: str):
    row = [
        InlineKeyboardButton(text=("• " if key == period else "") + label, callback_data=f"adm_st_{key}")
        for key, (label, _c, _s) in STATS_PERIODS.items()
    ]
    _title, count, skip = STATS_PERIODS.get(period, STATS_PERIODS["today"])
    new_n = _sum_stats(_days_back(count, skip))["new"]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            row[:2],
            row[2:],
            [InlineKeyboardButton(text=f"🆕 Нові учасники ({new_n}) →", callback_data=f"adm_new_{period}_0")],
            [InlineKeyboardButton(text="🔄 Оновити", callback_data=f"adm_st_{period}")],
        ]
    )


NEW_LIST_PER_PAGE = 10


def new_users_for_period(period: str) -> list[int]:
    """Новачки за період, найновіші зверху (без дублів і видалених)."""
    _title, count, skip = STATS_PERIODS.get(period, STATS_PERIODS["today"])
    ids = []
    for day in _days_back(count, skip):
        ids += list(stats_days.get(day, {}).get("new_ids", []))
    seen, result = set(), []
    for uid in ids:
        if uid in users_db and uid not in seen:
            seen.add(uid)
            result.append(uid)
    result.sort(key=lambda x: users_db[x].get("joined_at") or 0, reverse=True)
    return result


def new_user_line(n: int, uid: int) -> str:
    u = users_db[uid]
    if u.get("tg_username"):
        who = f"@{esc(u['tg_username'])}"
    else:
        who = f'<a href="tg://user?id={uid}">{esc(u.get("tg_name") or "профіль")}</a>'
    if u.get("tg_username") and u.get("tg_name"):
        who += f" ({esc(u['tg_name'])})"
    when = ""
    if u.get("joined_at"):
        when = datetime.fromtimestamp(u["joined_at"], KYIV_TZ).strftime("%d.%m %H:%M")
    profile = ", ".join(
        str(v) for v in (u.get("gender"), u.get("age"), u.get("country")) if v and v != "Не вказано"
    ) or "профіль не заповнений"
    extra = [source_label(u.get("source")), f"💬 чатів: {u.get('total_chats', 0)}"]
    if is_premium(u):
        extra.append("💎")
    if uid in banned_users:
        extra.append("⛔ бан")
    ref = u.get("referred_by")
    if ref and ref in users_db:
        extra.append(f"🤝 запросив {esc(users_db[ref].get('custom_id', ref))}")
    head = f"{n}. <b>{esc(u.get('custom_id', ''))}</b> · {who}"
    if when:
        head += f" · 🕐 {when}"
    return f"{head}\n     {esc(profile)} · " + " · ".join(extra) + f"\n     <code>{uid}</code>"


@dp.callback_query(F.data.startswith("adm_new_"))
@admin_only
async def adm_new_users(call: types.CallbackQuery):
    try:
        period, page = call.data[len("adm_new_"):].rsplit("_", 1)
        page = int(page)
    except ValueError:
        period, page = "today", 0
    if period not in STATS_PERIODS:
        period = "today"
    title = STATS_PERIODS[period][0]
    ids = new_users_for_period(period)
    pages = max(1, (len(ids) + NEW_LIST_PER_PAGE - 1) // NEW_LIST_PER_PAGE)
    page = min(max(page, 0), pages - 1)
    chunk = ids[page * NEW_LIST_PER_PAGE:(page + 1) * NEW_LIST_PER_PAGE]
    lines = [f"🆕 <b>Нові учасники — {title.lower() if period in ('today', 'yday') else 'за ' + title}</b> ({len(ids)})", ""]
    if not chunk:
        lines.append("Поки нікого 🙂")
    for i, uid in enumerate(chunk, start=page * NEW_LIST_PER_PAGE + 1):
        lines.append(new_user_line(i, uid))
        lines.append("")
    counted = _sum_stats(_days_back(STATS_PERIODS[period][1], STATS_PERIODS[period][2]))["new"]
    if counted > len(ids):
        lines.append(f"<i>Ще {counted - len(ids)} — зайшли до оновлення бота, тому їх немає в списку.</i>")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm_new_{period}_{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"adm_new_{period}_{page + 1}"))
    kb = [nav] if nav else []
    gift_btns = [
        InlineKeyboardButton(text=f"🎁 {users_db[uid].get('custom_id', uid)}", callback_data=f"adm_gift_to:{uid}")
        for uid in chunk
    ]
    kb += [gift_btns[i:i + 3] for i in range(0, len(gift_btns), 3)]
    kb.append([InlineKeyboardButton(text="⬅️ До статистики", callback_data=f"adm_st_{period}")])
    try:
        await call.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), disable_web_page_preview=True)
    except TelegramAPIError:
        pass
    await call.answer()


@dp.callback_query(F.data == "adm_links")
@admin_only
async def adm_links(call: types.CallbackQuery):
    base = f"https://t.me/{BOT_USERNAME}?start=" if BOT_USERNAME else "https://t.me/ВАШ_БОТ?start="
    lines = [
        "🔗 <b>Посилання з мітками</b>",
        "Став різні посилання в різних місцях — і в статистиці буде видно, звідки прийшли люди.",
        "",
    ]
    for code in ("tt", "yt", "ig", "tgads"):
        lines.append(f"{SOURCE_LABELS[code]}\n<code>{base}{code}</code>\n")
    lines += [
        "💬 <b>Власна мітка</b> — для конкретного чату чи реклами в каналі:",
        f"<code>{base}chat1</code>",
        "Після <code>start=</code> пиши будь-що латиницею й цифрами (напр. <code>kanal_memy</code>) — "
        "у статистиці з'явиться окремим рядком.",
        "",
        "<i>Мітка зараховується лише новим людям — тим, хто відкрив бот уперше.</i>",
    ]
    await call.message.answer("\n".join(lines), disable_web_page_preview=True)
    await call.answer()


@dp.callback_query(F.data == "adm_stats")
@admin_only
async def adm_stats(call: types.CallbackQuery):
    await call.message.answer(admin_stats_text("today"), reply_markup=admin_stats_keyboard("today"))
    await call.answer()


@dp.callback_query(F.data.startswith("adm_st_"))
@admin_only
async def adm_stats_period(call: types.CallbackQuery):
    period = call.data[len("adm_st_"):]
    if period not in STATS_PERIODS:
        period = "today"
    try:
        await call.message.edit_text(admin_stats_text(period), reply_markup=admin_stats_keyboard(period))
    except TelegramAPIError:
        pass  # «message is not modified» — нічого не змінилось
    await call.answer("Оновлено")


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
        lines.append(f"#{r['id']} — {r['time']}\nВід {admin_label(r['from'])}\nНа {admin_label(r['on'])}\n")
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
    init_user(rep["on"])["ban_type"] = "admin"
    rep["status"] = "оброблена"
    end_chat(rep["on"])
    remove_from_room(rep["on"])
    if rep["on"] in queue:
        queue.remove(rep["on"])
    await safe_send(rep["on"], "⛔ Вас заблоковано адміністратором за скаргою.")
    await call.message.answer(f"⛔ Користувача {admin_label(rep['on'])} забанено.")
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
    await call.message.answer("Введіть ID користувача, якого треба забанити (наш ID_1001 або Telegram ID):")
    await state.set_state(AdminStates.ban_id)
    await call.answer()


@dp.message(AdminStates.ban_id, F.text)
async def adm_ban_finish(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    target = resolve_user_id(message.text)
    if target is None:
        await message.answer("Не знайшов такого користувача. Надішли Telegram ID або наш ID (напр. ID_1001).")
        return
    banned_users.add(target)
    init_user(target)["ban_type"] = "admin"
    end_chat(target)
    remove_from_room(target)
    if target in queue:
        queue.remove(target)
    await message.answer(f"⛔ Користувача {admin_label(target)} забанено.")
    await safe_send(target, "⛔ Вас заблоковано адміністратором.")


@dp.callback_query(F.data == "adm_unban")
@admin_only
async def adm_unban_start(call: types.CallbackQuery, state: FSMContext):
    await call.message.answer("Введіть ID користувача, якого треба розбанити (наш ID_1001 або Telegram ID):")
    await state.set_state(AdminStates.unban_id)
    await call.answer()


@dp.message(AdminStates.unban_id, F.text)
async def adm_unban_finish(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    target = resolve_user_id(message.text)
    if target is None:
        await message.answer("Не знайшов такого користувача. Надішли Telegram ID або наш ID (напр. ID_1001).")
        return
    banned_users.discard(target)
    if target in users_db:
        users_db[target]["ban_type"] = None
        users_db[target]["reports_received"] = 0
    await message.answer(f"✅ Користувача {admin_label(target)} розбанено.")
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


@dp.callback_query(F.data.startswith("adm_reply_"))
@admin_only
async def adm_reply_start(call: types.CallbackQuery, state: FSMContext):
    target_id = int(call.data[len("adm_reply_"):])
    await state.update_data(support_target=target_id)
    await state.set_state(AdminStates.support_reply)
    await call.message.answer(
        f"✍️ Введіть відповідь для користувача <code>{target_id}</code> (або /cancel):"
    )
    await call.answer()


@dp.message(AdminStates.support_reply, F.text)
async def adm_reply_finish(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    target_id = data.get("support_target")
    await state.clear()
    if not target_id:
        await message.answer("Помилка: не знайдено отримувача.")
        return
    ok = await safe_send(target_id, f"📩 <b>Відповідь від підтримки:</b>\n{protect_text(esc(message.text))}")
    if ok:
        await message.answer("✅ Відповідь надіслано користувачу.")
    else:
        await message.answer("❌ Не вдалося надіслати — користувач недоступний.")


# ---------------------------------------------------------------------------
# Реклама в черзі пошуку (керується з адмінки; Premium — без реклами)
# ---------------------------------------------------------------------------
ADS_COOLDOWN = int(os.getenv("ADS_COOLDOWN", "300"))  # не частіше ніж раз на N секунд для людини
AD_MAX_LEN = 300
ad_last_shown: dict[int, float] = {}


def pick_ad_for(user_id: int, now: float | None = None) -> dict | None:
    """Яку рекламу показати людині (або None): без Premium, з паузою між показами."""
    now = time.time() if now is None else now
    u = init_user(user_id)
    if is_premium(u):
        return None
    active = [a for a in ads if a.get("active")]
    if not active:
        return None
    if now - ad_last_shown.get(user_id, 0) < ADS_COOLDOWN:
        return None
    return random.choice(active)


def _ad_keyboard(ad: dict):
    if not ad.get("url"):
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👉 Перейти", url=ad["url"])]])


def _ad_text(ad: dict) -> str:
    return f"📣 <i>Реклама</i>\n\n{protect_text(esc(ad['text']))}"


async def maybe_show_ad(user_id: int):
    try:
        ad = pick_ad_for(user_id)
        if ad is None:
            return
        if await safe_send(user_id, _ad_text(ad), reply_markup=_ad_keyboard(ad)):
            ad["shows"] = ad.get("shows", 0) + 1
            ad_last_shown[user_id] = time.time()
    except Exception as e:  # noqa: BLE001 — реклама ніколи не має ламати пошук
        logging.warning("Не вдалося показати рекламу: %s", e)


def normalize_ad_url(raw: str) -> str | None:
    raw = (raw or "").strip()
    if raw.startswith("@") and len(raw) > 1:
        return f"https://t.me/{raw[1:]}"
    if raw.startswith("t.me/"):
        return f"https://{raw}"
    if raw.startswith(("https://", "http://")) and " " not in raw:
        return raw
    return None


def ads_admin_view():
    lines = ["📣 <b>Реклама в черзі пошуку</b>", "Показується тим, хто чекає співрозмовника (крім Premium)."]
    rows = []
    if not ads:
        lines.append("\nПоки немає жодного оголошення.")
    for ad in ads:
        status = "✅ активна" if ad.get("active") else "⏸ на паузі"
        link = f"\n🔗 {esc(ad['url'])}" if ad.get("url") else ""
        lines.append(
            f"\n<b>#{ad['id']}</b> — {status}, показів: {ad.get('shows', 0)}\n{esc(ad['text'][:150])}{link}"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{'⏸ Пауза' if ad.get('active') else '▶️ Увімкнути'} #{ad['id']}",
                    callback_data=f"adm_adtoggle_{ad['id']}",
                ),
                InlineKeyboardButton(text=f"🗑 Видалити #{ad['id']}", callback_data=f"adm_addel_{ad['id']}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="➕ Додати оголошення", callback_data="adm_adnew")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "adm_ads")
@admin_only
async def adm_ads(call: types.CallbackQuery):
    text, kb = ads_admin_view()
    await call.message.answer(text, reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "adm_adnew")
@admin_only
async def adm_ad_new(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.ad_text)
    await call.message.answer(
        f"✍️ Надішли текст реклами: 2–3 рядки, до {AD_MAX_LEN} символів (або /cancel)."
    )
    await call.answer()


@dp.message(AdminStates.ad_text, F.text)
async def adm_ad_text(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.strip()
    if len(text) > AD_MAX_LEN:
        await message.answer(f"Задовго: {len(text)} символів, максимум {AD_MAX_LEN}. Скороти і надішли ще раз.")
        return
    await state.update_data(ad_text=text)
    await state.set_state(AdminStates.ad_url)
    await message.answer(
        "🔗 Тепер надішли посилання для кнопки «👉 Перейти»:\n"
        "• https://... або @username каналу/бота\n"
        "• або <code>-</code>, якщо реклама без посилання."
    )


@dp.message(AdminStates.ad_url, F.text)
async def adm_ad_url(message: types.Message, state: FSMContext):
    global ad_counter
    if message.from_user.id != ADMIN_ID:
        return
    raw = message.text.strip()
    url = None
    if raw != "-":
        url = normalize_ad_url(raw)
        if url is None:
            await message.answer("Не схоже на посилання. Надішли https://..., @username або «-».")
            return
    data = await state.get_data()
    await state.clear()
    ad_counter += 1
    ad = {
        "id": ad_counter,
        "text": data.get("ad_text", ""),
        "url": url,
        "active": True,
        "shows": 0,
        "created": time.strftime("%d.%m.%Y"),
    }
    ads.append(ad)
    request_save()
    await message.answer("✅ Оголошення додано. Ось як його бачитимуть користувачі:")
    ok = await safe_send(message.chat.id, _ad_text(ad), reply_markup=_ad_keyboard(ad))
    if not ok:
        ad["active"] = False
        await message.answer("⚠️ Telegram не прийняв це оголошення (ймовірно, погане посилання) — поставив на паузу.")
    text, kb = ads_admin_view()
    await message.answer(text, reply_markup=kb)


def _find_ad(call_data: str, prefix: str) -> dict | None:
    try:
        ad_id = int(call_data[len(prefix):])
    except ValueError:
        return None
    return next((a for a in ads if a["id"] == ad_id), None)


@dp.callback_query(F.data.startswith("adm_adtoggle_"))
@admin_only
async def adm_ad_toggle(call: types.CallbackQuery):
    ad = _find_ad(call.data, "adm_adtoggle_")
    if ad is None:
        await call.answer("Оголошення не знайдено", show_alert=True)
        return
    ad["active"] = not ad.get("active")
    request_save()
    text, kb = ads_admin_view()
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramAPIError:
        await call.message.answer(text, reply_markup=kb)
    await call.answer("Увімкнено" if ad["active"] else "На паузі")


@dp.callback_query(F.data.startswith("adm_addel_"))
@admin_only
async def adm_ad_delete(call: types.CallbackQuery):
    ad = _find_ad(call.data, "adm_addel_")
    if ad is None:
        await call.answer("Оголошення не знайдено", show_alert=True)
        return
    ads.remove(ad)
    request_save()
    text, kb = ads_admin_view()
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramAPIError:
        await call.message.answer(text, reply_markup=kb)
    await call.answer("Видалено")


# ---------------------------------------------------------------------------
# Оплата за реквізитами: код у призначенні + квитанція → адмін підтверджує кнопкою
# ---------------------------------------------------------------------------
def new_payment_code() -> str:
    while True:
        code = f"P{random.randint(100000, 999999)}"
        if code not in manual_payments:
            return code


def bank_details_text(code: str, amount: int) -> str:
    lines = ["🏦 <b>Оплата за реквізитами</b>\n", f"💵 До сплати: <b>{amount} грн</b>\n"]
    lines.append("Переказ через банківський застосунок (дані копіюються натисканням):")
    if PAY_RECIPIENT:
        lines.append(f"▶️ Отримувач: <code>{esc(PAY_RECIPIENT)}</code>")
    if PAY_CARD:
        lines.append(f"▶️ Картка: <code>{esc(PAY_CARD)}</code>")
    if PAY_IBAN:
        lines.append(f"▶️ IBAN: <code>{esc(PAY_IBAN)}</code>")
    if PAY_TAX_ID:
        lines.append(f"▶️ ЄДРПОУ / ІПН: <code>{esc(PAY_TAX_ID)}</code>")
    lines.append(f"▶️ Призначення / коментар: <code>Поповнення {code}</code>")
    lines.append(f"▶️ Сума: <code>{amount}</code> грн\n")
    lines.append(
        f"⚠️ <b>Обов'язково</b> вкажи код <b>{code}</b> у призначенні або коментарі до переказу "
        "і переказуй рівно цю суму — інакше ми не зможемо знайти твій платіж."
    )
    lines.append("📸 Після оплати натисни кнопку нижче і надішли скріншот квитанції — так зарахуємо швидше.")
    lines.append("⏳ Платежі перевіряються вручну, зазвичай — протягом кількох годин.")
    return "\n".join(lines)


@dp.callback_query(F.data.startswith("topup_bank_"))
async def topup_pay_bank(call: types.CallbackQuery):
    if not (PAY_CARD or PAY_IBAN):
        await call.answer("Цей спосіб оплати зараз недоступний", show_alert=True)
        return
    amount = _parse_topup_amount(call.data[len("topup_bank_"):])
    if amount is None:
        await call.answer("Недоступна сума.", show_alert=True)
        return
    code = new_payment_code()
    manual_payments[code] = {
        "user_id": call.from_user.id,
        "amount": amount,
        "created": time.strftime("%d.%m.%Y %H:%M"),
        "ts": time.time(),
        "status": "pending",
        "receipt": False,
    }
    request_save()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✅ Я оплатив — надіслати квитанцію", callback_data=f"bank_paid_{code}")]]
    )
    await call.message.answer(bank_details_text(code, amount), reply_markup=kb)
    await call.answer()


def _payment_admin_kb(code: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"pay_ok_{code}"),
                InlineKeyboardButton(text="❌ Відхилити", callback_data=f"pay_no_{code}"),
            ]
        ]
    )


def _payment_caption(code: str, p: dict) -> str:
    return (
        f"🏦 <b>Оплата за реквізитами {code}</b>\n"
        f"Від: {admin_label(p['user_id'])}\n"
        f"Сума: <b>{p['amount']} грн</b>\nСтворено: {p['created']}\n\n"
        f"Перевір у банку надходження з призначенням «Поповнення {code}»."
    )


@dp.callback_query(F.data.startswith("bank_paid_"))
async def bank_paid(call: types.CallbackQuery, state: FSMContext):
    code = call.data[len("bank_paid_"):]
    p = manual_payments.get(code)
    if p is None or p["user_id"] != call.from_user.id:
        await call.answer("Платіж не знайдено.", show_alert=True)
        return
    if p["status"] != "pending":
        await call.answer("Цей платіж уже оброблено.", show_alert=True)
        return
    await state.set_state(TopupStates.receipt)
    await state.update_data(payment_code=code)
    await call.message.answer("📸 Надішли скріншот або файл квитанції про оплату (або /cancel).")
    await call.answer()


@dp.message(TopupStates.receipt)
async def bank_receipt(message: types.Message, state: FSMContext):
    if not (message.photo or message.document):
        await message.answer("Потрібен скріншот або файл квитанції 📸 (або /cancel).")
        return
    data = await state.get_data()
    await state.clear()
    code = data.get("payment_code")
    p = manual_payments.get(code or "")
    if p is None or p["status"] != "pending":
        await message.answer("Цей платіж уже оброблено або не знайдено.")
        return
    p["receipt"] = True
    request_save()
    if ADMIN_ID:
        try:
            await message.copy_to(
                chat_id=ADMIN_ID, caption=_payment_caption(code, p), reply_markup=_payment_admin_kb(code)
            )
        except TelegramAPIError as e:
            logging.warning("Не вдалося надіслати квитанцію адміну: %s", e)
            await safe_send(ADMIN_ID, _payment_caption(code, p), reply_markup=_payment_admin_kb(code))
    await message.answer(
        f"✅ Квитанцію отримано! Як тільки адміністратор підтвердить платіж {code}, "
        "баланс поповниться і тобі прийде повідомлення."
    )


async def _resolve_payment(code: str, approve: bool) -> str:
    """Підтвердити/відхилити платіж. Повертає текст для адміна."""
    p = manual_payments.get(code)
    if p is None:
        return f"Платіж {code} не знайдено."
    if p["status"] != "pending":
        return f"Платіж {code} уже оброблено ({'підтверджено' if p['status'] == 'confirmed' else 'відхилено'})."
    if approve:
        p["status"] = "confirmed"
        u = init_user(p["user_id"])
        u["balance"] += p["amount"]
        record_topup("jar" if p.get("method") == "jar" else "card", p["amount"])
        bonus = apply_topup_bonus(u, p["amount"])
        request_save()
        text = f"✅ Оплату {code} підтверджено! Баланс поповнено на {p['amount']} грн."
        if bonus:
            text += f"\n🎉 Бонус +{bonus:.0f} грн за кожні {TOPUP_BONUS_STEP:.0f} грн поповнень!"
        progress = topup_progress_text(u)
        if progress:
            text += f"\n\n{progress}"
        await safe_send(p["user_id"], text)
        return f"✅ {code}: зараховано {p['amount']} грн користувачу {admin_label(p['user_id'])}."
    p["status"] = "rejected"
    request_save()
    await safe_send(
        p["user_id"],
        f"❌ Платіж {code} не знайдено в банку. Якщо ти точно оплатив — напиши в «{BTN_HELP}» "
        "і додай квитанцію.",
    )
    return f"❌ {code}: відхилено."


def _is_admin(user_id: int) -> bool:
    return bool(ADMIN_ID) and user_id == ADMIN_ID


@dp.callback_query(F.data.startswith("pay_ok_") | F.data.startswith("pay_no_"))
async def pay_resolve_button(call: types.CallbackQuery):
    if not _is_admin(call.from_user.id):
        await call.answer("Доступ заборонено", show_alert=True)
        return
    approve = call.data.startswith("pay_ok_")
    code = call.data[len("pay_ok_"):]
    result = await _resolve_payment(code, approve)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await call.message.answer(result)
    await call.answer()


@dp.message(Command("confirm"))
@dp.message(Command("reject"))
async def pay_resolve_command(message: types.Message):
    """Адмін: /confirm P123456 або /reject P123456 — якщо платіж прийшов без квитанції."""
    if not _is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Формат: /confirm P123456 або /reject P123456")
        return
    approve = parts[0].lower().startswith("/confirm")
    await message.answer(await _resolve_payment(parts[1].strip().upper(), approve))


@dp.callback_query(F.data == "adm_payments")
async def adm_payments(call: types.CallbackQuery):
    if not _is_admin(call.from_user.id):
        await call.answer("Доступ заборонено", show_alert=True)
        return
    pending = [(c, p) for c, p in manual_payments.items() if p["status"] == "pending"]
    if not pending:
        await call.message.answer("🏦 Немає оплат, що очікують перевірки.")
        await call.answer()
        return
    await call.message.answer(f"🏦 <b>Оплати на перевірці: {len(pending)}</b> (показую останні 10)")
    for code, p in pending[-10:]:
        mark = "📸 є квитанція" if p.get("receipt") else "без квитанції"
        await call.message.answer(f"{_payment_caption(code, p)}\n({mark})", reply_markup=_payment_admin_kb(code))
    await call.answer()


# ---------------------------------------------------------------------------
# Переписка з друзями (окремо від анонімного чату)
# ---------------------------------------------------------------------------
@dp.message(FriendStates.write)
async def friend_write_finish(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target_id = data.get("friend_target")
    await state.clear()
    if not target_id:
        await message.answer("Не вдалося визначити друга. Відкрий «👫 Друзі» ще раз.")
        return
    err = await deliver_friend_message(message.from_user.id, target_id, message)
    if err:
        await message.answer(f"❌ {err}")
    else:
        await message.answer("✅ Надіслано другу.")


def _is_reply_to_friend(message: types.Message) -> bool:
    r = message.reply_to_message
    return r is not None and (message.chat.id, r.message_id) in friend_msg_map


@dp.message(_is_reply_to_friend)
async def friend_reply(message: types.Message):
    target_id = friend_msg_map.get((message.chat.id, message.reply_to_message.message_id))
    if not target_id:
        return
    err = await deliver_friend_message(message.from_user.id, target_id, message)
    if err:
        await message.answer(f"❌ {err}")
    else:
        await message.answer("✅ Надіслано другу.")


# ---------------------------------------------------------------------------
# Оплата через Банку monobank з автоматичним підтвердженням
# (бот раз на хвилину читає виписку Банки і шукає код платежу в коментарі)
# ---------------------------------------------------------------------------
MONO_API = "https://api.monobank.ua"
MONO_POLL_SECONDS = 65  # API дозволяє виписку не частіше ніж раз на 60 с
MONO_SEEN_LIMIT = 2000
MONO_CODE_REGEX = re.compile(r"[PР]\s?(\d{6})", re.IGNORECASE)  # латинська P або кирилична Р
mono_state = {"jar": None, "last_poll": None, "last_error": None, "jars": []}


def mono_jar_link() -> str:
    if PAY_JAR_LINK:
        return PAY_JAR_LINK
    jar = mono_state.get("jar")
    if jar and jar.get("sendId"):
        send_id = jar["sendId"]
        return send_id if send_id.startswith("http") else f"https://send.monobank.ua/{send_id}"
    return ""


async def _mono_get(path: str):
    """GET до API monobank. Повертає (статус, json або None)."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{MONO_API}{path}", headers={"X-Token": MONO_TOKEN}, timeout=aiohttp.ClientTimeout(total=20)
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:  # noqa: BLE001
                data = None
            return resp.status, data


def _choose_jar(jars: list[dict]) -> dict | None:
    if MONO_JAR_ID:
        return next((j for j in jars if j.get("id") == MONO_JAR_ID), None)
    if MONO_JAR_TITLE:
        wanted = MONO_JAR_TITLE.lower()
        matches = [j for j in jars if wanted in (j.get("title") or "").lower()]
        return matches[0] if len(matches) == 1 else None
    uah = [j for j in jars if j.get("currencyCode") in (980, None)]
    return uah[0] if len(uah) == 1 else None


async def mono_detect_jar() -> bool:
    status, data = await _mono_get("/personal/client-info")
    if status != 200 or not isinstance(data, dict):
        desc = (data or {}).get("errorDescription") if isinstance(data, dict) else None
        mono_state["last_error"] = f"client-info: HTTP {status} {desc or ''}".strip()
        return False
    jars = data.get("jars") or []
    mono_state["jars"] = jars
    jar = _choose_jar(jars)
    if jar is None:
        mono_state["last_error"] = (
            f"Не вдалося вибрати Банку (знайдено: {len(jars)}). "
            "Задай MONO_JAR_TITLE (частину назви) або MONO_JAR_ID — список: /mono"
        )
        return False
    mono_state["jar"] = jar
    mono_state["last_error"] = None
    logging.info("Monobank: використовую Банку «%s».", jar.get("title"))
    return True


def find_payment_code(comment: str) -> str | None:
    m = MONO_CODE_REGEX.search(comment or "")
    return f"P{m.group(1)}" if m else None


MONO_MATCH_WINDOW = 30 * 60  # оплата без коду: шукаємо заявку з тією ж сумою за останні 30 хв
# Надходження без коду, які чекають вибору адміна (лише в пам'яті): ключ -> {"paid", "comment"}
mono_unmatched: dict[str, dict] = {}
_mono_unmatched_seq = [0]


def _amount_candidates(paid, item_time: float) -> list[str]:
    """Неоплачені заявки з точно такою ж сумою, створені незадовго до оплати."""
    found = []
    for code, p in manual_payments.items():
        if p.get("status") != "pending" or not p.get("ts"):
            continue
        if abs(float(p["amount"]) - float(paid)) >= 0.01:
            continue
        if p["ts"] - 120 <= item_time <= p["ts"] + MONO_MATCH_WINDOW:
            found.append(code)
    return found


def _recent_pending(item_time: float, limit: int = 5) -> list[str]:
    recent = [
        (p["ts"], code)
        for code, p in manual_payments.items()
        if p.get("status") == "pending" and p.get("ts") and 0 <= item_time - p["ts"] + 120 <= 86400
    ]
    return [code for _ts, code in sorted(recent, reverse=True)[:limit]]


async def _credit_jar_payment(code: str, paid, how: str) -> str:
    p = manual_payments[code]
    expected = p["amount"]
    if abs(float(paid) - float(expected)) >= 0.01:
        p["expected_amount"] = expected
        p["amount"] = paid  # зараховуємо фактично отриману суму
    p["method"] = "jar"
    result = await _resolve_payment(code, True)
    if ADMIN_ID:
        note = "" if abs(float(paid) - float(expected)) < 0.01 else f" (очікувалось {expected} грн)"
        await safe_send(ADMIN_ID, f"🤖 {how}{note}:\n{result}")
    return result


async def process_jar_statement(items: list[dict]) -> int:
    """Обробляє операції з виписки Банки. Повертає, скільки платежів зараховано."""
    credited = 0
    changed = False
    seen = set(mono_seen_ids)
    for item in sorted(items, key=lambda i: i.get("time", 0)):
        item_id = str(item.get("id") or "")
        amount_kop = item.get("amount") or 0
        if not item_id or item_id in seen or amount_kop <= 0:
            continue
        seen.add(item_id)
        mono_seen_ids.append(item_id)
        changed = True
        paid = round(amount_kop / 100, 2)
        if float(paid).is_integer():
            paid = int(paid)
        item_time = float(item.get("time") or time.time())
        comment = item.get("comment") or ""

        # 1) код у коментарі
        code = find_payment_code(comment)
        p = manual_payments.get(code) if code else None
        if p is not None and p.get("status") == "pending":
            await _credit_jar_payment(code, paid, "Автоматично через Банку")
            credited += 1
            continue

        # 2) коду немає — шукаємо заявку з тією ж сумою
        candidates = _amount_candidates(paid, item_time)
        if len(candidates) == 1:
            await _credit_jar_payment(candidates[0], paid, "Зараховано за сумою (у коментарі не було коду)")
            credited += 1
            continue

        # 3) кілька або жодної — питаємо адміна кнопками
        if not ADMIN_ID:
            continue
        options = candidates or _recent_pending(item_time)
        text = (
            f"🫙 Надходження в Банку без коду: <b>{paid:g} грн</b>\n"
            f"Коментар: «{esc(comment) or '—'}»\n"
        )
        kb = None
        if options:
            _mono_unmatched_seq[0] += 1
            key = str(_mono_unmatched_seq[0])
            mono_unmatched[key] = {"paid": paid, "comment": comment}
            text += (
                "Кілька заявок з такою сумою — обери, кому зарахувати:"
                if candidates
                else "Заявки з такою сумою немає. Ось останні неоплачені — обери, кому зарахувати:"
            )
            rows = []
            for c in options:
                cp = manual_payments[c]
                cu = users_db.get(cp["user_id"]) or {}
                label = f"✅ {c}: {cu.get('nickname', cp['user_id'])} ({cu.get('custom_id', '?')}), заявка {cp['amount']} грн"
                rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"jp_{key}_{c}")])
            kb = InlineKeyboardMarkup(inline_keyboard=rows)
        else:
            text += "Неоплачених заявок немає. Якщо це оплата від користувача — зарахуй вручну (/addbalance)."
        await safe_send(ADMIN_ID, text, reply_markup=kb)
    if len(mono_seen_ids) > MONO_SEEN_LIMIT:
        del mono_seen_ids[: len(mono_seen_ids) - MONO_SEEN_LIMIT]
    if changed:
        request_save()
    return credited


@dp.callback_query(F.data.startswith("jp_"))
async def jar_pick(call: types.CallbackQuery):
    """Адмін вибрав, кому зарахувати надходження без коду."""
    if not _is_admin(call.from_user.id):
        await call.answer("Доступ заборонено", show_alert=True)
        return
    try:
        _prefix, key, code = call.data.split("_", 2)
    except ValueError:
        await call.answer()
        return
    entry = mono_unmatched.get(key)
    if entry is None:
        await call.answer(
            "Це надходження вже оброблено або бот перезапускався. Зарахуй вручну: /addbalance", show_alert=True
        )
        return
    p = manual_payments.get(code)
    if p is None or p.get("status") != "pending":
        await call.answer("Ця заявка вже оброблена. Обери іншу.", show_alert=True)
        return
    mono_unmatched.pop(key, None)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await _credit_jar_payment(code, entry["paid"], "Зараховано вручну з Банки")
    await call.answer("Зараховано ✅")


async def mono_poll_once():
    now = int(time.time())
    # стежимо лише за свіжими заявками (до 3 діб); старші можна підтвердити вручну
    pending = [
        p
        for p in manual_payments.values()
        if p.get("status") == "pending" and p.get("ts") and now - p["ts"] < 3 * 86400
    ]
    if not pending:
        return
    oldest = min(int(p["ts"]) for p in pending) - 300
    frm = max(oldest, now - 2682000 + 60)  # не більше 31 доби + 1 год
    status, data = await _mono_get(f"/personal/statement/{mono_state['jar']['id']}/{frm}/{now}")
    mono_state["last_poll"] = time.strftime("%d.%m.%Y %H:%M:%S")
    if status != 200 or not isinstance(data, list):
        desc = data.get("errorDescription") if isinstance(data, dict) else ""
        mono_state["last_error"] = f"statement: HTTP {status} {desc or ''}".strip()
        logging.warning("Monobank: %s", mono_state["last_error"])
        return
    mono_state["last_error"] = None
    await process_jar_statement(data)


async def mono_poll_loop():
    if not MONO_TOKEN:
        return
    await asyncio.sleep(10)
    while True:
        try:
            if mono_state.get("jar") is None:
                if not await mono_detect_jar():
                    logging.warning("Monobank: %s", mono_state["last_error"])
                    await asyncio.sleep(300)
                    continue
                await asyncio.sleep(MONO_POLL_SECONDS)  # окремий ліміт для client-info / statement
            await mono_poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            mono_state["last_error"] = str(e)
            logging.warning("Monobank: помилка перевірки Банки: %s", e)
        await asyncio.sleep(MONO_POLL_SECONDS)


@dp.callback_query(F.data.startswith("topup_jar_"))
async def topup_pay_jar(call: types.CallbackQuery):
    link = mono_jar_link()
    if not link:
        await call.answer("Цей спосіб оплати зараз недоступний", show_alert=True)
        return
    amount = _parse_topup_amount(call.data[len("topup_jar_"):])
    if amount is None:
        await call.answer("Недоступна сума.", show_alert=True)
        return
    code = new_payment_code()
    manual_payments[code] = {
        "user_id": call.from_user.id,
        "amount": amount,
        "created": time.strftime("%d.%m.%Y %H:%M"),
        "ts": time.time(),
        "status": "pending",
        "receipt": False,
        "method": "jar",
    }
    request_save()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🫙 Відкрити Банку і оплатити", url=link)],
            [InlineKeyboardButton(text="📸 Не зарахувалось? Надіслати квитанцію", callback_data=f"bank_paid_{code}")],
        ]
    )
    auto = bool(MONO_TOKEN and mono_state.get("jar"))
    await call.message.answer(
        "🫙 <b>Оплата через Банку monobank</b>\n\n"
        f"1️⃣ Натисни «Відкрити Банку» і введи суму <b>{amount} грн</b>.\n"
        f"2️⃣ У полі <b>коментар</b> обов'язково напиши код: <code>{code}</code>\n"
        "3️⃣ Оплати будь-якою карткою.\n\n"
        + (
            "🤖 Баланс поповниться <b>автоматично</b> за 1–2 хвилини після оплати."
            if auto
            else "⏳ Платіж перевіряється вручну, зазвичай протягом кількох годин."
        )
        + f"\n\n⚠️ Без коду {code} у коментарі ми не зможемо знайти твій платіж.",
        reply_markup=kb,
    )
    await call.answer()


@dp.message(Command("mono"))
async def mono_status(message: types.Message):
    """Адмін: стан підключення до Банки monobank."""
    if not _is_admin(message.from_user.id):
        return
    if not MONO_TOKEN:
        await message.answer("🫙 MONO_TOKEN не задано в Render — автопідтвердження вимкнене.")
        return
    jar = mono_state.get("jar")
    pending = sum(1 for p in manual_payments.values() if p.get("status") == "pending" and p.get("method") == "jar")
    lines = ["🫙 <b>Банка monobank</b>"]
    if jar:
        lines.append(f"Банка: «{esc(jar.get('title'))}», баланс {jar.get('balance', 0) / 100:g} грн")
        lines.append(f"Посилання: {esc(mono_jar_link())}")
    else:
        lines.append("Банку ще не вибрано.")
    lines.append(f"Оплат через Банку на перевірці: {pending}")
    lines.append(f"Остання перевірка виписки: {mono_state.get('last_poll') or '—'}")
    if mono_state.get("last_error"):
        lines.append(f"⚠️ Помилка: {esc(mono_state['last_error'])}")
    if mono_state.get("jars"):
        lines.append("\nУсі твої Банки (для MONO_JAR_TITLE / MONO_JAR_ID):")
        for j in mono_state["jars"]:
            lines.append(f"• «{esc(j.get('title'))}» — <code>{esc(j.get('id'))}</code>")
    await message.answer("\n".join(lines))


# ---------------------------------------------------------------------------
# Статистика: щоденні лічильники для адміна
# ---------------------------------------------------------------------------
STATS_KEEP_DAYS = 62  # скільки днів історії тримаємо в базі


def _new_stats_day() -> dict:
    return {
        "new": 0,  # нових користувачів
        "active": set(),  # хто щось робив у боті (унікальні)
        "chats": 0,  # з'єднань усього
        "real_chats": 0,  # чатів від 1 хв, де обидва писали
        "flirt": 0,
        "interest": 0,
        "friends": 0,
        "msgs": 0,  # повідомлень у чатах 1-на-1
        "topup": {},  # джерело -> грн
        "topup_n": 0,  # кількість оплат
        "spent": {},  # на що витрачали баланс -> грн
        "daily_paid": 0.0,  # роздано щоденними бонусами
        "tasks_paid": 0.0,  # роздано за завдання
        "tasks_done": 0,
        "new_ids": [],  # user_id новачків за день (для списку в адмінці)
    }


def stats_day(day: str | None = None) -> dict:
    day = day or today_str()
    d = stats_days.get(day)
    if d is None:
        d = stats_days[day] = _new_stats_day()
        cutoff = (kyiv_today() - timedelta(days=STATS_KEEP_DAYS)).isoformat()
        for old in [k for k in stats_days if k < cutoff]:
            stats_days.pop(old, None)
    return d


def stat_add(key: str, n: float = 1, sub: str | None = None):
    try:
        d = stats_day()
        if sub is None:
            d[key] = d.get(key, 0) + n
        else:
            bucket = d.setdefault(key, {})
            bucket[sub] = bucket.get(sub, 0) + n
    except Exception as e:  # noqa: BLE001 — статистика ніколи не має ламати бота
        logging.warning("Статистика: %s", e)


def record_topup(source: str, amount: float):
    stat_add("topup", float(amount), sub=source)
    stat_add("topup_n")


def record_spend(category: str, amount: float):
    stat_add("spent", float(amount), sub=category)


# ---------------------------------------------------------------------------
# Щоденні завдання: 3 на день, нагорода на баланс, з Premium ×2
# ---------------------------------------------------------------------------
REAL_CHAT_SECONDS = 60  # чат зараховується, якщо тривав від 1 хв і обидва написали
LONG_CHAT_SECONDS = 300
TASKS_PER_DAY = 3
TASKS_ALL_BONUS = float(os.getenv("TASKS_ALL_BONUS", "2"))  # грн за виконання всіх завдань дня
TASKS_PREMIUM_MULT = 2

# ключ -> (назва, подія, скільки треба, нагорода грн)
TASK_POOL = {
    "daily": ("🎁 Забери щоденний бонус", "daily", 1, 1),
    "chats3": ("💬 Поспілкуйся в 3 чатах", "chat", 3, 1),
    "rate2": ("👍 Оціни 2 співрозмовників", "rate", 2, 1),
    "msgs20": ("✉️ Надішли 20 повідомлень у чатах", "msg", 20, 1),
    "longchat": ("⏱ Поспілкуйся з кимось 5 хвилин", "long_chat", 1, 2),
    "flirt1": ("❤️ Поспілкуйся у флірт-чаті", "flirt_chat", 1, 1),
    "interest1": ("🧩 Поспілкуйся за інтересами", "interest_chat", 1, 1),
    "gift1": ("🎁 Подаруй подарунок співрозмовнику", "gift", 1, 2),
    "room1": ("👥 Напиши в кімнаті за інтересами", "room_msg", 1, 1),
}


def ensure_tasks(user_id: int, u: dict) -> list[str]:
    """Видає завдання на сьогодні (однакові протягом дня, у кожного свій набір)."""
    today = today_str()
    if u.get("tasks_day") != today or not u.get("tasks_list"):
        pool = [k for k in TASK_POOL if k != "daily"]
        if flirt_block_reason(u) is not None:
            pool.remove("flirt1")  # флірт лише з 18 і з заповненим профілем
        rng = random.Random(f"{today}:{user_id}")
        u["tasks_day"] = today
        u["tasks_list"] = ["daily"] + rng.sample(pool, TASKS_PER_DAY - 1)
        u["tasks_progress"] = {}
        u["tasks_done"] = set()
        u["tasks_bonus"] = False
    return [k for k in u["tasks_list"] if k in TASK_POOL]


def _task_mult(u: dict) -> int:
    return TASKS_PREMIUM_MULT if is_premium(u) else 1


def _notify_later(user_id: int, text: str):
    try:
        asyncio.get_running_loop().create_task(safe_send(user_id, text))
    except RuntimeError:
        pass


def task_event(user_id: int, event: str, n: int = 1):
    """Зараховує дію в щоденні завдання; за виконане — нагорода і повідомлення."""
    try:
        u = users_db.get(user_id)
        if u is None or user_id in banned_users:
            return
        keys = ensure_tasks(user_id, u)
        mult = _task_mult(u)
        notes = []
        for k in keys:
            title, ev, goal, reward = TASK_POOL[k]
            if ev != event or k in u["tasks_done"]:
                continue
            done_now = u["tasks_progress"].get(k, 0) + n
            u["tasks_progress"][k] = min(done_now, goal)
            if done_now >= goal:
                u["tasks_done"].add(k)
                pay = reward * mult
                u["balance"] += pay
                stat_add("tasks_paid", pay)
                stat_add("tasks_done")
                notes.append(f"✅ Завдання виконано: {title} — <b>+{pay:g} грн</b>")
        if not notes:
            return
        done_cnt = len([k for k in keys if k in u["tasks_done"]])
        if done_cnt >= len(keys) and not u.get("tasks_bonus"):
            u["tasks_bonus"] = True
            bonus = TASKS_ALL_BONUS * mult
            if bonus:
                u["balance"] += bonus
                stat_add("tasks_paid", bonus)
                notes.append(f"🏆 Усі завдання на сьогодні виконано! Бонус <b>+{bonus:g} грн</b>")
        else:
            notes.append(f"📋 Виконано {done_cnt}/{len(keys)} — решта в «{BTN_TASKS}»")
        request_save()
        if u.get("notify_tasks", True):
            _notify_later(user_id, "\n".join(notes))
    except Exception as e:  # noqa: BLE001 — завдання ніколи не мають ламати бота
        logging.warning("Завдання: %s", e)


def on_chat_started(a: int, b: int, mode: str):
    now = time.time()
    for x in (a, b):
        chat_meta[x] = {"start": now, "mode": mode, "msgs": 0}
    stat_add("chats")
    if mode == "flirt":
        stat_add("flirt")
    elif mode.startswith("int:"):
        stat_add("interest")
    elif mode == "friend":
        stat_add("friends")


def on_chat_message(user_id: int):
    m = chat_meta.get(user_id)
    if m is not None:
        m["msgs"] += 1
    stat_add("msgs")
    task_event(user_id, "msg")


# ---------------------------------------------------------------------------
# 👑 Засновники: перші FOUNDERS_LIMIT людей, які справді поспілкувались, — значок назавжди
# ---------------------------------------------------------------------------
FOUNDERS_LIMIT = int(os.getenv("FOUNDERS_LIMIT", "100"))


def founders_count() -> int:
    return max((u.get("founder_no") or 0 for u in users_db.values()), default=0)


def founders_left() -> int:
    return max(0, FOUNDERS_LIMIT - founders_count())


def founder_promo_line() -> str:
    left = founders_left()
    if not left:
        return ""
    return (
        f"👑 Перші {FOUNDERS_LIMIT} учасників отримують значок <b>засновника</b> назавжди — "
        f"залишилось <b>{left}</b> {_plural(left, 'місце', 'місця', 'місць')}! Поспілкуйся хоча б раз, щоб забрати своє."
    )


def maybe_make_founder(user_id: int) -> bool:
    u = users_db.get(user_id)
    if u is None or u.get("founder_no") or user_id <= 0 or user_id == ADMIN_ID or user_id in banned_users:
        return False
    no = founders_count() + 1
    if no > FOUNDERS_LIMIT:
        return False
    u["founder_no"] = no
    request_save()
    _notify_later(
        user_id,
        f"👑 <b>Вітаємо! Ти — засновник ANONimchyk №{no} з {FOUNDERS_LIMIT}!</b>\n\n"
        "• Значок 👑 тепер у твоєму профілі назавжди — співрозмовники його бачать.\n"
        "• Коли вийде велике оновлення «Стиль ANONimchyk», ти автоматично отримаєш "
        "ексклюзивний 🎁 <b>Пак засновника</b>, який більше ніколи не продаватиметься.\n\n"
        "Дякуємо, що ти з нами з самого початку 💜",
    )
    return True


@dp.message(Command("founders"))
async def founders_cmd(message: types.Message):
    u = init_user(message.from_user.id)
    left = founders_left()
    if u.get("founder_no"):
        mine = f"Ти — засновник 👑 №{u['founder_no']}. Дякуємо! 💜"
    elif left:
        mine = "Щоб стати засновником — знайди співрозмовника і поспілкуйся хоча б хвилину 💬"
    else:
        mine = "Усі місця засновників уже зайняті — але попереду ще багато подарунків 🎁"
    await message.answer(
        f"👑 <b>Засновники ANONimchyk</b>\n\n"
        f"Перші {FOUNDERS_LIMIT} учасників, які справді поспілкувались у боті, отримують:\n"
        "• значок 👑 у профілі назавжди;\n"
        "• ексклюзивний 🎁 Пак засновника в день запуску «Стилю ANONimchyk».\n\n"
        f"Зайнято: <b>{founders_count()}</b> з {FOUNDERS_LIMIT} · залишилось <b>{left}</b>\n\n{mine}"
    )


def on_chat_ended(a: int, b: int):
    try:
        ma, mb = chat_meta.pop(a, None), chat_meta.pop(b, None)
        if ma is None or mb is None:
            return
        last_chat_mode[a] = ma["mode"]
        last_chat_mode[b] = mb["mode"]
        duration = time.time() - min(ma["start"], mb["start"])
        # оцінка йде в топ тижня лише після справжньої розмови: обидва писали
        rated = duration if ma["msgs"] >= 1 and mb["msgs"] >= 1 else 0.0
        last_chat_seconds[a] = last_chat_seconds[b] = rated
        if duration < REAL_CHAT_SECONDS or ma["msgs"] < 1 or mb["msgs"] < 1:
            return  # «натиснув і втік» не рахується — щоб завдання не накручували
        stat_add("real_chats")
        long_chat = duration >= LONG_CHAT_SECONDS and ma["msgs"] >= 3 and mb["msgs"] >= 3
        mode = ma["mode"]
        for x in (a, b):
            maybe_make_founder(x)
            contest_on_real_chat(x)
            task_event(x, "chat")
            if mode == "flirt":
                task_event(x, "flirt_chat")
            elif mode.startswith("int:"):
                task_event(x, "interest_chat")
            if long_chat:
                task_event(x, "long_chat")
    except Exception as e:  # noqa: BLE001
        logging.warning("Кінець чату (статистика): %s", e)


def tasks_text(user_id: int, u: dict) -> str:
    keys = ensure_tasks(user_id, u)
    mult = _task_mult(u)
    lines = ["📋 <b>Завдання на сьогодні</b>\n"]
    for k in keys:
        title, _ev, goal, reward = TASK_POOL[k]
        pay = reward * mult
        if k in u["tasks_done"]:
            lines.append(f"✅ <s>{title}</s> — +{pay:g} грн")
        else:
            prog = u["tasks_progress"].get(k, 0)
            counter = f" ({prog}/{goal})" if goal > 1 else ""
            lines.append(f"⬜ {title}{counter} — +{pay:g} грн")
    lines.append("")
    if u.get("tasks_bonus"):
        lines.append("🏆 Бонус за всі завдання отримано — ти молодець!")
    elif TASKS_ALL_BONUS:
        lines.append(f"🏆 Виконай усі — і отримай ще <b>+{TASKS_ALL_BONUS * mult:g} грн</b>")
    lines.append("💎 У тебе Premium — нагороди ×2" if mult > 1 else "💎 З Premium усі нагороди ×2")
    lines.append("")
    lines.append(
        "<i>Чат зараховується, якщо триває від 1 хв і ви обоє щось написали. "
        "Нові завдання — щодня опівночі за Києвом.</i>"
    )
    return "\n".join(lines)


@dp.message(F.text == BTN_TASKS)
@dp.message(Command("tasks"))
async def tasks_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await send_banner(message.from_user.id, "tasks")
    u = init_user(message.from_user.id)
    await message.answer(tasks_text(message.from_user.id, u))


# ---------------------------------------------------------------------------
# Рейтинг і інтереси профілю: кого кому підбирати першим
# ---------------------------------------------------------------------------
MAX_PROFILE_INTERESTS = 3
MATCH_SCORE_MAX = 4  # 2 — однаковий «рівень» рейтингу, +1 — є спільний інтерес, +1 — однаковий настрій


def match_score(u: dict, p: dict, mode: str) -> int:
    """Чим більше балів, тим краща пара. Люди з низьким рейтингом спершу потрапляють одне до одного."""
    score = 2 if is_low_rated(u) == is_low_rated(p) else 0
    if not mode.startswith("int:") and (u.get("interests") or set()) & (p.get("interests") or set()):
        score += 1
    if current_mood(u) and current_mood(u) == current_mood(p):
        score += 1  # 🎭 на одній хвилі
    return score


def rating_line(u: dict) -> str:
    up, down = u.get("rating_up", 0), u.get("rating_down", 0)
    if not up + down:
        return "ще немає оцінок"
    return f"👍 {up} · 👎 {down} ({rating_percent(u)}%)"


def interests_line(u: dict) -> str:
    keys = [k for k in INTEREST_LABELS if k in (u.get("interests") or set())]
    return ", ".join(INTEREST_LABELS[k] for k in keys) if keys else "не обрано"


async def send_interest_notes(user_id: int, u: dict, partner_id: int, p: dict):
    u_open, p_open = not u.get("hide_interests"), not p.get("hide_interests")
    common = [k for k in INTEREST_LABELS if k in (u.get("interests") or set()) & (p.get("interests") or set())]
    if common and u_open and p_open:
        note = "🧩 Спільні інтереси: <b>" + ", ".join(INTEREST_LABELS[k] for k in common) + "</b> — є з чого почати 😉"
        await safe_send(user_id, note)
        await safe_send(partner_id, note)
        return
    for me, other, is_open in ((user_id, p, p_open), (partner_id, u, u_open)):
        if is_open and other.get("interests"):
            await safe_send(me, f"🧩 Інтереси співрозмовника: {interests_line(other)}")


# ---------------------------------------------------------------------------
# 🎭 Мій настрій · 🎵 Пісня настрою · 💬 Тема дня
# ---------------------------------------------------------------------------
MOODS = {
    "happy": "😊 Гарний настрій",
    "calm": "😌 Спокійний",
    "sleepy": "🥱 Сонний",
    "sad": "😔 Сумно",
    "laugh": "😂 Хочу посміятись",
    "talk": "💬 Хочу виговоритись",
    "deep": "🌌 На глибокі розмови",
    "bored": "🙃 Нудно",
}
MOOD_TTL = 12 * 3600  # настрій «на сьогодні»: діє 12 годин
SONG_TTL = 24 * 3600  # пісня настрою: 24 години
SONG_MAX = 60
TOPIC_COOLDOWN = 20  # секунд між «новими темами» в одному чаті
_SONG_BAD = re.compile(r"(https?://|www\.|t\.me|@\w|\.com|\.ua|\.ru)", re.I)

DAILY_TOPICS = [
    "Яка пісня зараз крутиться у тебе в голові?",
    "Куди б ти поїхав(-ла) прямо зараз, якби квиток був безкоштовний?",
    "Яка найдивніша їжа, яку ти куштував(-ла)?",
    "Що тебе сьогодні порадувало, навіть якась дрібниця?",
    "Ти більше «сова» чи «жайворонок»?",
    "Який фільм ти можеш переглядати безкінечно?",
    "Якби ти міг(-ла) мати одну суперсилу — яку б обрав(-ла)?",
    "Що б ти сказав(-ла) собі п'ять років тому?",
    "Який для тебе ідеальний вихідний?",
    "Котики чи собаки — і чому?",
    "Яке хобі ти давно хочеш спробувати?",
    "Який найсмішніший випадок стався з тобою цього року?",
    "Яка пора року твоя — і за що ти її любиш?",
    "Чого ти точно не пробачиш у дружбі?",
    "Чай чи кава? І як саме ти його п'єш?",
    "Яка книга або серіал змінили твій погляд на щось?",
    "Опиши свій день трьома емодзі.",
    "Що тебе заспокоює, коли все бісить?",
    "Яка в тебе «дивна» звичка, про яку мало хто знає?",
    "Якби ти відкрив(-ла) свій заклад — що б це було?",
    "Яка мрія в тебе зараз на першому місці?",
    "Який найкращий подарунок ти колись отримував(-ла)?",
    "Що б ти робив(-ла), якби завтра не треба було на роботу чи навчання?",
    "Який звук або запах нагадує тобі дитинство?",
    "Ти віриш у кохання з першого погляду?",
    "Що тобі легше: почати розмову чи підтримувати її?",
    "Яке місто в Україні ти любиш найбільше?",
    "Що ти вмієш готувати найкраще?",
    "Чого ти навчився(-лася) за останній місяць?",
    "Яке маленьке рішення колись сильно змінило твоє життя?",
]
topic_last: dict[int, float] = {}  # user_id -> коли востаннє просив нову тему в чаті


def current_mood(u: dict) -> str | None:
    key = u.get("mood")
    if key in MOODS and time.time() - (u.get("mood_at") or 0) < MOOD_TTL:
        return key
    return None


def current_song(u: dict) -> str | None:
    song = u.get("song")
    if song and time.time() - (u.get("song_at") or 0) < SONG_TTL:
        return song
    return None


def mood_line(u: dict) -> str:
    key = current_mood(u)
    return MOODS[key] if key else "не вказано"


def topic_of_day() -> str:
    return DAILY_TOPICS[kyiv_today().toordinal() % len(DAILY_TOPICS)]


def random_topic(exclude: str | None = None) -> str:
    pool = [t for t in DAILY_TOPICS if t != exclude]
    return random.choice(pool)


def mood_text(u: dict) -> str:
    song = current_song(u)
    same = 0
    key = current_mood(u)
    if key:
        same = sum(1 for uid in queue if current_mood(users_db.get(uid, {})) == key)
    lines = [
        "🎭 <b>Мій настрій</b>\n",
        "Обери, як ти зараз. Співрозмовник побачить це на початку чату, "
        "а бот спершу шукатиме когось на одній хвилі.\n",
        f"Зараз: <b>{mood_line(u)}</b>" + (" <i>(діє 12 год)</i>" if key else ""),
        "🎵 Пісня настрою: " + (f"«{esc(song)}»" if song else "не вказано"),
    ]
    if key and same:
        lines.append(f"🔍 З таким самим настроєм зараз шукають: <b>{same}</b>")
    lines.append(f"\n💬 <b>Тема дня:</b> {topic_of_day()}")
    return "\n".join(lines)


def mood_keyboard(u: dict) -> InlineKeyboardMarkup:
    cur = current_mood(u)
    btns = [
        InlineKeyboardButton(text=("✅ " if k == cur else "") + label, callback_data=f"mood_set:{k}")
        for k, label in MOODS.items()
    ]
    rows = [btns[i : i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton(text="🎵 Пісня настрою", callback_data="mood_song")])
    if cur or current_song(u):
        rows.append([InlineKeyboardButton(text="🧹 Скинути настрій і пісню", callback_data="mood_clear")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("mood"))
async def mood_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    u = init_user(message.from_user.id)
    await message.answer(mood_text(u), reply_markup=mood_keyboard(u))


@dp.callback_query(F.data == "mood_open")
async def mood_open(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    await call.message.answer(mood_text(u), reply_markup=mood_keyboard(u))
    await call.answer()


@dp.callback_query(F.data.startswith("mood_set:"))
async def mood_set(call: types.CallbackQuery):
    key = call.data.split(":", 1)[1]
    if key not in MOODS:
        await call.answer("Невідомий настрій", show_alert=True)
        return
    u = init_user(call.from_user.id)
    u["mood"], u["mood_at"] = key, time.time()
    request_save()
    try:
        await call.message.edit_text(mood_text(u), reply_markup=mood_keyboard(u))
    except TelegramAPIError:
        pass
    await call.answer(f"Настрій: {MOODS[key]}")


@dp.callback_query(F.data == "mood_clear")
async def mood_clear(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    u["mood"], u["mood_at"], u["song"], u["song_at"] = None, 0.0, None, 0.0
    request_save()
    try:
        await call.message.edit_text(mood_text(u), reply_markup=mood_keyboard(u))
    except TelegramAPIError:
        pass
    await call.answer("Скинуто")


@dp.callback_query(F.data == "mood_song")
async def mood_song_ask(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id in active_chats:
        await call.answer("Пісню можна змінити після чату 🙂", show_alert=True)
        return
    await state.set_state(MoodStates.song)
    await call.message.answer(
        "🎵 Напиши пісню, яка зараз у тебе в настрої — виконавця і назву "
        f"(до {SONG_MAX} символів).\nНаприклад: <i>Океан Ельзи — Обійми</i>\n\n"
        "Без посилань. Скасувати: /cancel"
    )
    await call.answer()


@dp.message(MoodStates.song, F.text)
async def mood_song_save(message: types.Message, state: FSMContext):
    text = " ".join((message.text or "").split())
    if text.startswith("/"):
        await state.clear()
        await message.answer("Дію скасовано.", reply_markup=get_main_keyboard())
        return
    if len(text) < 2 or len(text) > SONG_MAX:
        await message.answer(f"Напиши від 2 до {SONG_MAX} символів, наприклад: Океан Ельзи — Обійми")
        return
    if _SONG_BAD.search(text):
        await message.answer("Без посилань і @нікнеймів, будь ласка — лише виконавець і назва пісні 🙂")
        return
    u = init_user(message.from_user.id)
    u["song"], u["song_at"] = text, time.time()
    request_save()
    await state.clear()
    await message.answer(
        "✅ Готово! Співрозмовники побачать: 🎵 «" + protect_text(esc(text)) + "»",
        reply_markup=get_main_keyboard(),
    )
    await message.answer(mood_text(u), reply_markup=mood_keyboard(u))


def vibe_note_for(me: dict, other: dict) -> str:
    """Що бачить людина на початку чату: настрій і пісня співрозмовника + тема дня."""
    lines = []
    mine, theirs = current_mood(me), current_mood(other)
    if theirs and theirs == mine:
        lines.append(f"🎭 Ви на одній хвилі: <b>{MOODS[theirs]}</b>")
    elif theirs:
        lines.append(f"🎭 Настрій співрозмовника: <b>{MOODS[theirs]}</b>")
    song = current_song(other)
    if song:
        lines.append("🎵 Співрозмовник зараз слухає: «" + protect_text(esc(song)) + "»")
    lines.append(f"💬 Тема дня: <b>{topic_of_day()}</b>")
    lines.append("<i>Інша тема для розмови: /topic</i>")
    return "\n".join(lines)


async def send_vibe_notes(user_id: int, u: dict, partner_id: int, p: dict):
    await safe_send(user_id, vibe_note_for(u, p))
    await safe_send(partner_id, vibe_note_for(p, u))


@dp.message(Command("topic"))
async def topic_cmd(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    partner_id = active_chats.get(user_id)
    if partner_id is None:
        await state.clear()
        init_user(user_id)
        await message.answer(
            f"💬 <b>Тема дня:</b> {topic_of_day()}\n\n"
            "Під час чату натисни /topic — і бот підкине вам обом нову тему для розмови 🎲"
        )
        return
    now = time.time()
    if now - max(topic_last.get(user_id, 0), topic_last.get(partner_id, 0)) < TOPIC_COOLDOWN:
        await message.answer("⏳ Тему щойно змінювали — спершу обговоріть цю 🙂")
        return
    topic_last[user_id] = now
    text = f"🎲 Нова тема для розмови: <b>{random_topic(topic_of_day())}</b>"
    await message.answer(text)
    await safe_send(partner_id, text)


async def maybe_warn_low_rating(user_id: int, u: dict):
    if is_low_rated(u) and not u.get("low_warned"):
        u["low_warned"] = True
        await safe_send(
            user_id,
            "⚠️ Співрозмовники часто ставлять тобі 👎.\n"
            "Через це бот рідше підбирає тебе іншим. Будь привітнішим — "
            "і рейтинг підросте, а з ним і кількість цікавих чатів 🙂",
        )
    elif not is_low_rated(u) and u.get("low_warned"):
        u["low_warned"] = False


def profile_interests_keyboard(u: dict, tab: str = "main"):
    chosen = u.get("interests") or set()
    topics = HOBBY_TOPICS if tab == "hobby" else ROOM_TOPICS
    buttons = [
        InlineKeyboardButton(text=("✅ " if k in chosen else "") + label, callback_data=f"pi_t_{tab}_{k}")
        for k, label in topics.items()
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    tabs = [
        InlineKeyboardButton(text=("• " if tab != "hobby" else "") + "🧩 Інтереси", callback_data="pi_tab_main"),
        InlineKeyboardButton(text=("• " if tab == "hobby" else "") + "🎯 Захоплення", callback_data="pi_tab_hobby"),
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[tabs] + rows + [[InlineKeyboardButton(text="✅ Готово", callback_data="pi_done")]]
    )


def profile_interests_text(u: dict) -> str:
    return (
        f"🧩 <b>Мої інтереси</b> (до {MAX_PROFILE_INTERESTS})\n\n"
        f"Обрано: {interests_line(u)}\n\n"
        "Бот спершу шукатиме співрозмовників зі спільними інтересами, "
        "а після з'єднання покаже, про що вам цікаво поговорити."
    )


@dp.message(Command("myinterests"))
async def my_interests_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    u = init_user(message.from_user.id)
    await message.answer(profile_interests_text(u), reply_markup=profile_interests_keyboard(u))


@dp.callback_query(F.data == "pi_open")
async def my_interests_open(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    await call.message.answer(profile_interests_text(u), reply_markup=profile_interests_keyboard(u))
    await call.answer()


@dp.callback_query(F.data.startswith("pi_tab_"))
async def my_interests_tab(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    tab = "hobby" if call.data == "pi_tab_hobby" else "main"
    try:
        await call.message.edit_reply_markup(reply_markup=profile_interests_keyboard(u, tab))
    except TelegramAPIError:
        pass
    await call.answer()


@dp.callback_query(F.data.startswith("pi_t_"))
async def my_interests_toggle(call: types.CallbackQuery):
    tab, _, key = call.data[len("pi_t_"):].partition("_")
    if key not in INTEREST_LABELS:
        await call.answer("Невідома тема", show_alert=True)
        return
    u = init_user(call.from_user.id)
    chosen = u.setdefault("interests", set())
    if key in chosen:
        chosen.discard(key)
    elif len(chosen) >= MAX_PROFILE_INTERESTS:
        await call.answer(f"Можна обрати до {MAX_PROFILE_INTERESTS}. Спершу зніми якийсь ✅", show_alert=True)
        return
    else:
        chosen.add(key)
    request_save()
    try:
        await call.message.edit_text(profile_interests_text(u), reply_markup=profile_interests_keyboard(u, tab))
    except TelegramAPIError:
        pass
    await call.answer()


@dp.callback_query(F.data == "pi_done")
async def my_interests_done(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    try:
        await call.message.edit_text(f"✅ Твої інтереси: {interests_line(u)}\n\nЗмінити: /myinterests")
    except TelegramAPIError:
        pass
    await call.answer("Збережено")


# ---------------------------------------------------------------------------
# Підказка про фільтр за статтю: якщо безкоштовний юзер довго чекає у звичайному пошуку
# ---------------------------------------------------------------------------
FILTER_NUDGE_DELAY = int(os.getenv("FILTER_NUDGE_DELAY", "40"))  # секунд очікування
FILTER_NUDGE_COOLDOWN = 24 * 3600  # не частіше разу на добу
filter_nudge_last: dict[int, float] = {}


# ---------------------------------------------------------------------------
# 🛟 Порожня черга: якщо у флірті/темі довго нікого — пропонуємо звичайний пошук
# ---------------------------------------------------------------------------
STUCK_DELAY = int(os.getenv("STUCK_DELAY", "30"))  # секунд очікування до пропозиції
stuck_offered: dict[int, float] = {}  # user_id -> коли пропонували (щоб не повторювати в тому ж пошуку)


def normal_waiting_for(user_id: int) -> int:
    """Скільки людей у звичайному пошуку, з якими цю людину реально може з'єднати."""
    u = init_user(user_id)
    n = 0
    for cid in queue:
        if cid == user_id or search_mode.get(cid, "normal") != "normal":
            continue
        p = init_user(cid)
        if is_blacklisted(u, user_id, p, cid):
            continue
        if passes_filters(u, p) and passes_filters(p, u):
            n += 1
    return n


def schedule_stuck_offer(user_id: int, mode: str):
    if mode != "flirt" and not mode.startswith("int:"):
        return
    task = asyncio.get_running_loop().create_task(stuck_offer_after_wait(user_id, mode, time.time()))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def stuck_offer_after_wait(user_id: int, mode: str, started: float):
    try:
        await asyncio.sleep(STUCK_DELAY)
        if user_id not in queue or search_mode.get(user_id, "normal") != mode:
            return  # уже знайшов співрозмовника або вийшов з пошуку
        if stuck_offered.get(user_id, 0) >= started:
            return
        stuck_offered[user_id] = time.time()
        u = init_user(user_id)
        n = normal_waiting_for(user_id)
        if mode == "flirt":
            who = "дівчат" if u.get("gender") == "Хлопець" else "хлопців"
            head = f"❤️ Зараз {who} у флірт-пошуку немає."
        else:
            topic = INTEREST_LABELS.get(mode[4:], mode[4:])
            head = f"🧩 Зараз ніхто не шукає співрозмовника на тему {topic}."
        tail = (f"У звичайному пошуку вже чекає <b>{n}</b> — з'єднаю миттєво 👇" if n
                else "Можеш перейти у звичайний пошук — там шукають усі 👇")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Перейти у звичайний пошук", callback_data="stuck_go")],
            [InlineKeyboardButton(text="⏳ Чекати далі", callback_data="stuck_wait")],
        ])
        await safe_send(user_id, f"{head}\n{tail}", reply_markup=kb)
    except Exception as e:  # noqa: BLE001
        logging.warning("Порожня черга: %s", e)


@dp.callback_query(F.data == "stuck_go")
async def stuck_go(call: types.CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    if user_id in active_chats:
        await call.answer("Ти вже в чаті 🙂")
        return
    if user_id in queue:
        queue.remove(user_id)
    search_mode.pop(user_id, None)
    await call.answer()
    await run_search(call.message, state, "normal", user_id=user_id)


@dp.callback_query(F.data == "stuck_wait")
async def stuck_wait(call: types.CallbackQuery):
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await call.answer("Добре, чекаємо далі ⏳ Щойно хтось з'явиться — з'єднаю.")



def schedule_filter_nudge(user_id: int):
    u = init_user(user_id)
    if filter_active(u, "gender_filter") or not u.get("notify_tips", True):
        return
    if time.time() - filter_nudge_last.get(user_id, 0) < FILTER_NUDGE_COOLDOWN:
        return
    asyncio.create_task(filter_nudge_after_wait(user_id))


async def filter_nudge_after_wait(user_id: int):
    try:
        await asyncio.sleep(FILTER_NUDGE_DELAY)
        u = init_user(user_id)
        if user_id not in queue or search_mode.get(user_id, "normal") != "normal":
            return  # уже знайшов співрозмовника або вийшов з пошуку
        if filter_active(u, "gender_filter"):
            return
        if time.time() - filter_nudge_last.get(user_id, 0) < FILTER_NUDGE_COOLDOWN:
            return
        filter_nudge_last[user_id] = time.time()
        if u.get("gender") == "Хлопець":
            ask = "Хочеш спілкуватися лише з дівчатами? 👧"
        elif u.get("gender") == "Дівчина":
            ask = "Хочеш спілкуватися лише з хлопцями? 👦"
        else:
            ask = "Хочеш обирати стать співрозмовника? 👫"
        price = SHOP["gender_filter"][1]
        await safe_send(
            user_id,
            f"{ask}\n\n🎯 <b>Фільтр за статтю</b> — і бот з'єднуватиме лише з тими, кого ти обереш.\n"
            f"Він входить у 💎 Premium або купується окремо — {price} грн на тиждень.\n\n"
            "<i>Пошук триває, можеш просто чекати далі ⏳</i>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💎 Premium", callback_data="prem_open")],
                    [InlineKeyboardButton(text=f"🎯 Фільтр на тиждень — {price} грн", callback_data="buy_gender_filter")],
                ]
            ),
        )
    except Exception as e:  # noqa: BLE001 — підказка ніколи не має ламати пошук
        logging.warning("Підказка про фільтр не надіслана: %s", e)


@dp.callback_query(F.data == "prem_open")
async def prem_open(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    text, kb = premium_page()
    text += f"{premium_status_text(u)}\n💰 Баланс: {u['balance']:.2f} грн\n\nОбери тариф:"
    await call.message.answer(text, reply_markup=kb)
    await call.answer()


# ---------------------------------------------------------------------------
# «Хтось шукає співрозмовника»: якщо людина довго чекає — кличемо тих, хто зараз не в чаті
# ---------------------------------------------------------------------------
WAIT_PING_DELAY = int(os.getenv("WAIT_PING_DELAY", "30"))  # секунд очікування до виклику
WAIT_PING_USER_COOLDOWN = int(os.getenv("WAIT_PING_USER_COOLDOWN", str(3 * 3600)))  # одну людину кличемо не частіше
WAIT_PING_SEEKER_COOLDOWN = 10 * 60  # за одного й того самого шукача кличемо не частіше
WAIT_PING_MAX = int(os.getenv("WAIT_PING_MAX", "40"))  # скільки людей кличемо за раз
wait_ping_seeker_last: dict[int, float] = {}


def _wait_ping_mode_ok(mode: str) -> bool:
    return mode in ("normal", "flirt") or (mode.startswith("int:") and mode[4:] in INTEREST_LABELS)


def schedule_wait_ping(user_id: int, mode: str):
    if not _wait_ping_mode_ok(mode):
        return
    if time.time() - wait_ping_seeker_last.get(user_id, 0) < WAIT_PING_SEEKER_COOLDOWN:
        return
    asyncio.create_task(wait_ping_after_wait(user_id, mode))


def wait_ping_recipients(seeker_id: int, mode: str) -> list[int]:
    """Кого можна покликати: не в чаті/черзі/кімнаті, не забанені, сумісні з шукачем і давно не кликані."""
    s = init_user(seeker_id)
    now = time.time()
    result = []
    for uid, u in users_db.items():
        if uid <= 0 or uid == seeker_id or uid in banned_users:
            continue
        if uid in active_chats or uid in queue or uid in user_room:
            continue
        if not u.get("notify_waiting", True) or u.get("gender", "Не вказано") == "Не вказано":
            continue  # вимкнув сповіщення або ще не пройшов знайомство з ботом
        if now - u.get("wait_ping_at", 0) < WAIT_PING_USER_COOLDOWN:
            continue
        if is_blacklisted(s, seeker_id, u, uid):
            continue
        if not passes_filters(s, u) or not passes_filters(u, s):
            continue
        if mode == "flirt" and (u.get("gender") == s.get("gender") or flirt_block_reason(u) is not None):
            continue
        result.append(uid)
    result.sort(key=lambda x: last_seen.get(x, 0), reverse=True)  # спершу ті, хто нещодавно заходив
    return result[:WAIT_PING_MAX]


def wait_ping_text(mode: str) -> str:
    if mode == "flirt":
        what = "❤️ Зараз хтось шукає пару для флірту"
    elif mode.startswith("int:"):
        what = f"🧩 Зараз хтось шукає співрозмовника на тему {INTEREST_LABELS.get(mode[4:], mode[4:])}"
    else:
        what = "👋 Зараз хтось шукає співрозмовника"
    return (
        f"{what} — і чекає саме на тебе!\n\n"
        "Натисни кнопку, щоб одразу з'єднатися 👇\n"
        "<i>Вимкнути такі сповіщення: ⚙️ Налаштування → 🔔 Сповіщення</i>"
    )


async def wait_ping_after_wait(user_id: int, mode: str):
    try:
        await asyncio.sleep(WAIT_PING_DELAY)
        if user_id not in queue or search_mode.get(user_id, "normal") != mode:
            return  # уже знайшов співрозмовника або вийшов з пошуку
        if time.time() - wait_ping_seeker_last.get(user_id, 0) < WAIT_PING_SEEKER_COOLDOWN:
            return
        wait_ping_seeker_last[user_id] = time.time()
        recipients = wait_ping_recipients(user_id, mode)
        if not recipients:
            return
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔍 Знайти співрозмовника", callback_data=f"wp_go:{mode}")]]
        )
        text = wait_ping_text(mode)
        sent = 0
        for uid in recipients:
            if user_id not in queue:
                break  # шукач уже знайшов пару — більше нікого не кличемо
            users_db[uid]["wait_ping_at"] = time.time()
            if await safe_send(uid, text, reply_markup=kb):
                sent += 1
            await asyncio.sleep(0.05)  # не впираємось у ліміти Telegram
        if sent:
            stat_add("wait_pings", sent)
            request_save()
            if user_id in queue:
                await safe_send(user_id, "📣 Ми покликали людей, які зараз не в чаті. Зачекай ще трохи ⏳")
    except Exception as e:  # noqa: BLE001 — сповіщення ніколи не має ламати пошук
        logging.warning("Виклик «хтось шукає» не надіслано: %s", e)


# ---------------------------------------------------------------------------
# 👀 Адміну: «хтось уже хвилину шукає співрозмовника» — щоб людина не пішла з порожнього бота
# ---------------------------------------------------------------------------
ADMIN_WAIT_ALERT_DELAY = int(os.getenv("ADMIN_WAIT_ALERT_DELAY", "60"))
ADMIN_WAIT_ALERT_COOLDOWN = 120  # не частіше ніж раз на 2 хв
admin_wait_alert_last = 0.0
admin_alerts_enabled = True


def schedule_admin_wait_alert(user_id: int, mode: str):
    if not ADMIN_ID or user_id == ADMIN_ID or not _wait_ping_mode_ok(mode):
        return
    asyncio.create_task(admin_wait_alert_after(user_id, mode))


async def admin_wait_alert_after(user_id: int, mode: str):
    global admin_wait_alert_last
    try:
        await asyncio.sleep(ADMIN_WAIT_ALERT_DELAY)
        if not admin_alerts_enabled or user_id not in queue or search_mode.get(user_id, "normal") != mode:
            return
        if ADMIN_ID in active_chats or ADMIN_ID in queue or ADMIN_ID in user_room:
            return  # адмін уже спілкується
        if time.time() - admin_wait_alert_last < ADMIN_WAIT_ALERT_COOLDOWN:
            return
        admin_wait_alert_last = time.time()
        what = {"normal": "звичайний пошук", "flirt": "флірт"}.get(mode) or (
            f"тема {INTEREST_LABELS.get(mode[4:], mode[4:])}" if mode.startswith("int:") else mode
        )
        await safe_send(
            ADMIN_ID,
            f"👀 Хтось уже хвилину шукає співрозмовника ({what}).\nПідключишся, щоб людина не пішла?",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💬 Підключитися", callback_data=f"wp_go:{mode}")],
                    [InlineKeyboardButton(text="🔕 Вимкнути такі сповіщення", callback_data="adm_alerts_off")],
                ]
            ),
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("Сповіщення адміну не надіслано: %s", e)


@dp.callback_query(F.data == "adm_alerts_off")
@admin_only
async def adm_alerts_off(call: types.CallbackQuery):
    global admin_alerts_enabled
    admin_alerts_enabled = False
    await call.message.answer("🔕 Вимкнено до перезапуску бота. Увімкнути знову: /alerts")
    await call.answer()


@dp.message(Command("alerts"))
async def adm_alerts_on(message: types.Message):
    global admin_alerts_enabled
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    admin_alerts_enabled = True
    await message.answer("🔔 Сповіщення «хтось шукає співрозмовника» увімкнено.")


@dp.callback_query(F.data.startswith("wp_go:"))
async def wait_ping_go(call: types.CallbackQuery, state: FSMContext):
    mode = call.data[len("wp_go:"):]
    if not _wait_ping_mode_ok(mode):
        mode = "normal"
    u = init_user(call.from_user.id)
    if mode == "flirt" and flirt_block_reason(u) is not None:
        mode = "normal"
    await call.answer()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await run_search(call.message, state, mode, user_id=call.from_user.id)


# ---------------------------------------------------------------------------
# Пошук за інтересами 1-на-1: з'єднуємо лише людей з однаковою темою
# ---------------------------------------------------------------------------
def interests_keyboard(tab: str = "main"):
    waiting = {}
    for uid in queue:
        mode = search_mode.get(uid, "")
        if mode.startswith("int:"):
            waiting[mode[4:]] = waiting.get(mode[4:], 0) + 1
    topics = HOBBY_TOPICS if tab == "hobby" else ROOM_TOPICS
    buttons = []
    for key, label in topics.items():
        n = waiting.get(key, 0)
        buttons.append(
            InlineKeyboardButton(text=f"{label} · чекає {n}" if n else label, callback_data=f"intr_{key}")
        )
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    hobby_wait = sum(waiting.get(k, 0) for k in HOBBY_TOPICS)
    main_wait = sum(waiting.get(k, 0) for k in ROOM_TOPICS)
    tabs = [
        InlineKeyboardButton(
            text=("• " if tab != "hobby" else "") + "🧩 Інтереси" + (f" ({main_wait})" if main_wait else ""),
            callback_data="intrtab_main",
        ),
        InlineKeyboardButton(
            text=("• " if tab == "hobby" else "") + "🎯 Захоплення" + (f" ({hobby_wait})" if hobby_wait else ""),
            callback_data="intrtab_hobby",
        ),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[tabs] + rows)


@dp.callback_query(F.data.startswith("intrtab_"))
async def interests_tab(call: types.CallbackQuery):
    tab = "hobby" if call.data == "intrtab_hobby" else "main"
    try:
        await call.message.edit_reply_markup(reply_markup=interests_keyboard(tab))
    except TelegramAPIError:
        pass
    await call.answer("🎯 Захоплення" if tab == "hobby" else "🧩 Інтереси")


@dp.message(F.text == BTN_INTERESTS)
@dp.message(Command("interests"))
async def interests_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🧩 <b>Пошук за інтересами</b>\n\n"
        "Обери тему — і ми знайдемо співрозмовника, якому цікаве те саме. "
        "Поруч із темою видно, скільки людей уже чекає.\n\n"
        "Перемикай вкладки вгорі: 🧩 Інтереси / 🎯 Захоплення.",
        reply_markup=interests_keyboard(),
    )


@dp.callback_query(F.data.startswith("intr_"))
async def interests_pick(call: types.CallbackQuery, state: FSMContext):
    key = call.data[len("intr_"):]
    if key not in INTEREST_LABELS:
        await call.answer("Невідома тема", show_alert=True)
        return
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await call.answer(INTEREST_LABELS[key])
    await run_search(call.message, state, f"int:{key}", user_id=call.from_user.id)


# ---------------------------------------------------------------------------
# Онбординг новачків: стать → вік → країна за кілька натискань
# (обробники стоять після кнопок меню — натискання меню має пріоритет)
# ---------------------------------------------------------------------------
class OnboardStates(StatesGroup):
    age = State()
    country = State()


ONBOARD_COUNTRIES = [
    ("ua", "🇺🇦 Україна", "Україна"),
    ("pl", "🇵🇱 Польща", "Польща"),
    ("de", "🇩🇪 Німеччина", "Німеччина"),
    ("cz", "🇨🇿 Чехія", "Чехія"),
]
ONBOARD_AGE_REGEX = re.compile(r"^\s*(\d{1,3})\s*$")


def profile_complete(u: dict) -> bool:
    return all(u.get(k) not in (None, "", "Не вказано") for k in ("gender", "age", "country"))


def _skip_row():
    return [InlineKeyboardButton(text="⏭ Пропустити", callback_data="ob_skip")]


async def start_onboarding(chat_id: int, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👨 Хлопець", callback_data="ob_g_m"),
                InlineKeyboardButton(text="👩 Дівчина", callback_data="ob_g_f"),
            ],
            _skip_row(),
        ]
    )
    await safe_send(
        chat_id,
        "👋 Налаштуймо профіль — це 3 кроки, ~10 секунд. Так ти знаходитимеш кращих співрозмовників.\n\n"
        "<b>Крок 1/3.</b> Хто ти?",
        reply_markup=kb,
    )


@dp.callback_query(F.data.in_({"ob_g_m", "ob_g_f"}))
async def onboard_gender(call: types.CallbackQuery, state: FSMContext):
    u = init_user(call.from_user.id)
    u["gender"] = "Хлопець" if call.data == "ob_g_m" else "Дівчина"
    refresh_default_nick(u)
    request_save()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await state.set_state(OnboardStates.age)
    await call.message.answer(
        f"✅ {u['gender']}\n\n<b>Крок 2/3.</b> Скільки тобі років? Напиши числом, наприклад <code>19</code>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[_skip_row()]),
    )
    await call.answer()


def _is_onboard_age_text(message: types.Message) -> bool:
    return bool(message.text and ONBOARD_AGE_REGEX.match(message.text))


@dp.message(OnboardStates.age, _is_onboard_age_text)
async def onboard_age(message: types.Message, state: FSMContext):
    age = int(ONBOARD_AGE_REGEX.match(message.text).group(1))
    if not (10 <= age <= 99):
        await message.answer("Вкажи вік числом від 10 до 99 (або натисни «Пропустити»).")
        return
    u = init_user(message.from_user.id)
    u["age"] = str(age)
    prev_min = u.get("min_age_seen")
    u["min_age_seen"] = age if prev_min is None else min(int(prev_min), age)
    request_save()
    await state.set_state(OnboardStates.country)
    rows = [
        [InlineKeyboardButton(text=ONBOARD_COUNTRIES[i][1], callback_data=f"ob_c_{ONBOARD_COUNTRIES[i][0]}"),
         InlineKeyboardButton(text=ONBOARD_COUNTRIES[i + 1][1], callback_data=f"ob_c_{ONBOARD_COUNTRIES[i + 1][0]}")]
        for i in range(0, len(ONBOARD_COUNTRIES) - 1, 2)
    ]
    rows.append([InlineKeyboardButton(text="🌍 Інша — напишу сам(а)", callback_data="ob_c_other")])
    rows.append(_skip_row())
    await message.answer(
        f"✅ {age}\n\n<b>Крок 3/3.</b> Звідки ти?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


async def _finish_onboarding(chat_id: int, user_id: int, state: FSMContext):
    await state.clear()
    u = init_user(user_id)
    request_save()
    await safe_send(
        chat_id,
        f"🎉 Готово! Твій профіль: {short_info(u, public=False)}\n\n"
        f"Тепер тисни «{BTN_SEARCH}» внизу — і знайомся! 🤫\n"
        "Змінити дані можна будь-коли: /edit_profile",
        reply_markup=get_main_keyboard(),
    )
    await safe_send(
        chat_id,
        "🧩 Хочеш, щоб бот підбирав співрозмовників зі спільними інтересами? Обери до 3 тем:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🧩 Обрати інтереси", callback_data="pi_open")]]
        ),
    )
    await maybe_send_safety_memo(user_id, u)


@dp.callback_query(F.data.startswith("ob_c_"))
async def onboard_country(call: types.CallbackQuery, state: FSMContext):
    key = call.data[len("ob_c_"):]
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    if key == "other":
        await state.set_state(OnboardStates.country)
        await call.message.answer(
            "Напиши свою країну або місто:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[_skip_row()])
        )
        await call.answer()
        return
    country = next((name for k, _label, name in ONBOARD_COUNTRIES if k == key), None)
    if country:
        init_user(call.from_user.id)["country"] = country
    await _finish_onboarding(call.message.chat.id, call.from_user.id, state)
    await call.answer()


@dp.message(OnboardStates.country, F.text)
async def onboard_country_text(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if text.startswith("/"):
        return
    init_user(message.from_user.id)["country"] = text[:50]
    await _finish_onboarding(message.chat.id, message.from_user.id, state)


@dp.callback_query(F.data == "ob_skip")
async def onboard_skip(call: types.CallbackQuery, state: FSMContext):
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await state.clear()
    await call.message.answer(
        "Добре! Заповнити профіль можна будь-коли: /edit_profile", reply_markup=get_main_keyboard()
    )
    await call.answer()


# ---------------------------------------------------------------------------
# Налаштування: медіа, автопошук, приватність, сповіщення
# ---------------------------------------------------------------------------
def is_blocked_media(receiver: dict | None, message: types.Message) -> bool:
    """Чи не приймає отримувач такий тип повідомлення (режим «без фото/відео»)."""
    if not media_is_safe(receiver):
        return False
    return any(getattr(message, kind, None) for kind in ("photo", "video", "video_note", "animation", "document"))


class _NoState:
    """Заглушка FSM для автопошуку співрозмовника (у нього немає власного повідомлення)."""

    async def clear(self):
        pass


class _ChatAnswer:
    """Мінімальна «відповідь у чат» для run_search, коли пошук запускає не сама людина."""

    def __init__(self, chat_id: int):
        self.chat_id = chat_id

    async def answer(self, text: str, **kwargs):
        await safe_send(self.chat_id, text, **kwargs)


async def auto_search_after_chat(user_id: int):
    u = users_db.get(user_id)
    if not u or not u.get("auto_search"):
        return
    if user_id in banned_users or user_id in active_chats or user_id in queue or user_id in user_room:
        return
    mode = last_chat_mode.get(user_id, "normal")
    if mode == "friend" or (mode == "flirt" and flirt_block_reason(u) is not None):
        mode = "normal"
    await safe_send(user_id, "🔁 Автопошук: шукаю наступного співрозмовника…\nЗупинити пошук: /stop · вимкнути автопошук: ⚙️ Налаштування")
    await run_search(_ChatAnswer(user_id), _NoState(), mode, user_id=user_id)


def privacy_keyboard(u: dict):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🎂 Приховати вік: {_onoff(u.get('hide_age'))}", callback_data="tg_hide_age")],
            [InlineKeyboardButton(text=f"🌍 Приховати країну: {_onoff(u.get('hide_country'))}", callback_data="tg_hide_country")],
            [InlineKeyboardButton(text=f"🧩 Приховати інтереси: {_onoff(u.get('hide_interests'))}", callback_data="tg_hide_interests")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="set_back")],
        ]
    )


def notify_keyboard(u: dict):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"📋 Виконані завдання: {_onoff(u.get('notify_tasks', True))}", callback_data="tg_notify_tasks")],
            [InlineKeyboardButton(text=f"💡 Підказки: {_onoff(u.get('notify_tips', True))}", callback_data="tg_notify_tips")],
            [InlineKeyboardButton(text=f"🔄 Запрошення в чат: {_onoff(u.get('allow_invites', True))}", callback_data="tg_allow_invites")],
            [InlineKeyboardButton(text=f"👋 Хтось шукає співрозмовника: {_onoff(u.get('notify_waiting', True))}", callback_data="tg_notify_waiting")],
            [InlineKeyboardButton(text=f"⏰ Година знайомств: {_onoff(u.get('notify_meet', True))}", callback_data="tg_notify_meet")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="set_back")],
        ]
    )


TOGGLES = {
    "hide_age": (False, "privacy"),
    "hide_country": (False, "privacy"),
    "hide_interests": (False, "privacy"),
    "notify_tasks": (True, "notify"),
    "notify_tips": (True, "notify"),
    "allow_invites": (True, "notify"),
    "notify_waiting": (True, "notify"),
    "notify_meet": (True, "notify"),
}


async def _set_markup(call: types.CallbackQuery, markup):
    try:
        await call.message.edit_reply_markup(reply_markup=markup)
    except TelegramAPIError:
        pass


@dp.callback_query(F.data == "set_media")
async def set_media(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    u["media_mode"] = "all" if media_is_safe(u) else "safe"
    u["media_mode_set"] = True
    request_save()
    await _set_markup(call, get_settings_keyboard(u))
    await call.answer(
        "🛡 Фото, відео, GIF і файли від співрозмовників більше не надходитимуть"
        if u["media_mode"] == "safe"
        else "📷 Тепер приймаєш усі повідомлення",
        show_alert=True,
    )


@dp.callback_query(F.data == "set_auto")
async def set_auto(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    u["auto_search"] = not u.get("auto_search")
    request_save()
    await _set_markup(call, get_settings_keyboard(u))
    await call.answer(
        "🔁 Після кожного чату бот одразу шукатиме наступного" if u["auto_search"] else "Автопошук вимкнено"
    )


@dp.callback_query(F.data == "set_privacy")
async def set_privacy(call: types.CallbackQuery):
    await _set_markup(call, privacy_keyboard(init_user(call.from_user.id)))
    await call.answer("Приховане співрозмовник побачить як 🙈. Фільтри пошуку працюють як раніше.", show_alert=True)


@dp.callback_query(F.data == "set_notify")
async def set_notify(call: types.CallbackQuery):
    await _set_markup(call, notify_keyboard(init_user(call.from_user.id)))
    await call.answer()


@dp.callback_query(F.data == "set_back")
async def set_back(call: types.CallbackQuery):
    await _set_markup(call, get_settings_keyboard(init_user(call.from_user.id)))
    await call.answer()


@dp.callback_query(F.data.startswith("tg_"))
async def toggle_setting(call: types.CallbackQuery):
    key = call.data[len("tg_"):]
    if key not in TOGGLES:
        await call.answer()
        return
    default, menu = TOGGLES[key]
    u = init_user(call.from_user.id)
    u[key] = not u.get(key, default)
    request_save()
    await _set_markup(call, privacy_keyboard(u) if menu == "privacy" else notify_keyboard(u))
    await call.answer("Збережено ✅")


# ---------------------------------------------------------------------------
# Редагування профілю кнопками (без /edit_profile)
# ---------------------------------------------------------------------------
class ProfileEditStates(StatesGroup):
    age = State()
    country = State()


def _archive_profile(u: dict):
    stamp = time.strftime("%d.%m.%Y %H:%M")
    u["archive"].append(f"{stamp} — Стать: {u['gender']}, Вік: {u['age']}, Країна: {u['country']}")
    u["archive"] = u["archive"][-50:]


def profile_edit_text(u: dict) -> str:
    return (
        "✏️ <b>Мій профіль</b>\n\n"
        f"👤 Стать: <b>{esc(u['gender'])}</b>\n"
        f"🎂 Вік: <b>{esc(u['age'])}</b>{' (🙈 прихований)' if u.get('hide_age') else ''}\n"
        f"🌍 Країна: <b>{esc(u['country'])}</b>{' (🙈 прихована)' if u.get('hide_country') else ''}\n"
        f"🧩 Інтереси: {interests_line(u)}\n\n"
        "Що змінити?"
    )


def profile_edit_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👤 Стать", callback_data="pe_gender"),
                InlineKeyboardButton(text="🎂 Вік", callback_data="pe_age"),
            ],
            [
                InlineKeyboardButton(text="🌍 Країна", callback_data="pe_country"),
                InlineKeyboardButton(text="🧩 Інтереси", callback_data="pi_open"),
            ],
        ]
    )


async def _show_profile_editor(chat_id: int, u: dict):
    await safe_send(chat_id, profile_edit_text(u), reply_markup=profile_edit_keyboard())


@dp.callback_query(F.data == "pe_menu")
async def pe_menu(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_profile_editor(call.from_user.id, init_user(call.from_user.id))
    await call.answer()


@dp.callback_query(F.data == "pe_gender")
async def pe_gender(call: types.CallbackQuery):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👦 Хлопець", callback_data="pe_g_m"),
                InlineKeyboardButton(text="👧 Дівчина", callback_data="pe_g_f"),
            ]
        ]
    )
    try:
        await call.message.edit_text("👤 Обери стать:", reply_markup=kb)
    except TelegramAPIError:
        pass
    await call.answer()


@dp.callback_query(F.data.in_({"pe_g_m", "pe_g_f"}))
async def pe_gender_set(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    new = "Хлопець" if call.data == "pe_g_m" else "Дівчина"
    if u["gender"] != new:
        _archive_profile(u)
        u["gender"] = new
        refresh_default_nick(u)
        request_save()
    try:
        await call.message.edit_text(profile_edit_text(u), reply_markup=profile_edit_keyboard())
    except TelegramAPIError:
        pass
    await call.answer("Збережено ✅")


@dp.callback_query(F.data == "pe_age")
async def pe_age(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(ProfileEditStates.age)
    await call.message.answer("🎂 Напиши свій вік числом (наприклад, 19).\nСкасувати: /cancel")
    await call.answer()


@dp.message(ProfileEditStates.age, F.text)
async def pe_age_set(message: types.Message, state: FSMContext):
    m = ONBOARD_AGE_REGEX.match(message.text or "")
    if not m or not (10 <= int(m.group(1)) <= 99):
        await message.answer("Напиши вік числом від 10 до 99. Скасувати: /cancel")
        return
    age = int(m.group(1))
    u = init_user(message.from_user.id)
    if u["age"] != str(age):
        _archive_profile(u)
        u["age"] = str(age)
        prev_min = u.get("min_age_seen")
        u["min_age_seen"] = age if prev_min is None else min(int(prev_min), age)
        request_save()
    await state.clear()
    await _show_profile_editor(message.from_user.id, u)
    await maybe_send_safety_memo(message.from_user.id, u)


@dp.callback_query(F.data == "pe_country")
async def pe_country(call: types.CallbackQuery):
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"pe_c_{key}") for key, label, _n in ONBOARD_COUNTRIES[i : i + 2]]
        for i in range(0, len(ONBOARD_COUNTRIES), 2)
    ]
    rows.append([InlineKeyboardButton(text="🌍 Інша — напишу сам(а)", callback_data="pe_c_other")])
    try:
        await call.message.edit_text("🌍 Звідки ти?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except TelegramAPIError:
        pass
    await call.answer()


def _set_country(u: dict, country: str):
    if u["country"] != country:
        _archive_profile(u)
        u["country"] = country
        request_save()


@dp.callback_query(F.data.startswith("pe_c_"))
async def pe_country_set(call: types.CallbackQuery, state: FSMContext):
    key = call.data[len("pe_c_"):]
    if key == "other":
        await state.set_state(ProfileEditStates.country)
        await call.message.answer("🌍 Напиши свою країну або місто. Скасувати: /cancel")
        await call.answer()
        return
    country = next((name for k, _label, name in ONBOARD_COUNTRIES if k == key), None)
    if country is None:
        await call.answer("Невідома країна", show_alert=True)
        return
    u = init_user(call.from_user.id)
    _set_country(u, country)
    try:
        await call.message.edit_text(profile_edit_text(u), reply_markup=profile_edit_keyboard())
    except TelegramAPIError:
        pass
    await call.answer("Збережено ✅")


@dp.message(ProfileEditStates.country, F.text)
async def pe_country_text(message: types.Message, state: FSMContext):
    country = (message.text or "").strip()[:50]
    if len(country) < 2 or LINK_REGEX.search(country):
        await message.answer("Напиши назву країни або міста. Скасувати: /cancel")
        return
    u = init_user(message.from_user.id)
    _set_country(u, country)
    await state.clear()
    await _show_profile_editor(message.from_user.id, u)


# ---------------------------------------------------------------------------
# Банери-картинки: адмін ставить їх з телефона (надсилає фото боту), file_id — у базі
# ---------------------------------------------------------------------------
BANNER_PLACES = {
    "start": "🚀 Привітання (/start)",
    "premium": "💎 Premium",
    "shop": "🏪 Магазин",
    "topup": "💳 Поповнення",
    "tasks": "📋 Завдання",
    "lottery": "🎰 Рулетка",
}
BANNER_COOLDOWN = 10 * 60  # той самий банер одній людині — не частіше ніж раз на 10 хв
banner_last_shown: dict[tuple[int, str], float] = {}


class AdminGiftStates(StatesGroup):
    target = State()
    note = State()


def admin_gift_keyboard(uid: int):
    items = [
        InlineKeyboardButton(text=f"{title}", callback_data=f"adm_gift_pick:{uid}:{key}")
        for key, (title, _price) in GIFT_CATALOG.items()
    ]
    rows = [items[i:i + 3] for i in range(0, len(items), 3)]
    rows.append([InlineKeyboardButton(text="✖️ Скасувати", callback_data="adm_gift_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _admin_gift_choose(answer, uid: int):
    u = users_db[uid]
    await answer(
        f"🎁 Подарунок для {admin_label(uid)}\n"
        f"{short_info(u, public=False)}\n\nОбери, що подарувати:",
        reply_markup=admin_gift_keyboard(uid),
    )


@dp.callback_query(F.data == "adm_gift")
@admin_only
async def adm_gift_start(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminGiftStates.target)
    await call.message.answer(
        "🎁 <b>Подарунок користувачу</b>\n\n"
        "Надішли ID людини — наш (напр. <code>ID_1012</code>) або Telegram ID.\n"
        "Знайти людину зручно в 📊 Статистика → 🆕 Нові учасники (там є кнопки 🎁).\n\nСкасувати: /cancel"
    )
    await call.answer()


@dp.message(AdminGiftStates.target)
async def adm_gift_target(message: types.Message, state: FSMContext):
    if not _is_banner_admin(message.from_user.id):
        await state.clear()
        return
    uid = resolve_user_id(message.text or "")
    if uid is None or uid not in users_db:
        await message.answer("Не знайшов такого користувача 🤔 Перевір ID (напр. ID_1012) або /cancel.")
        return
    await state.clear()
    await _admin_gift_choose(message.answer, uid)


@dp.callback_query(F.data.startswith("adm_gift_to:"))
@admin_only
async def adm_gift_to(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        uid = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer()
        return
    if uid not in users_db:
        await call.answer("Користувача не знайдено", show_alert=True)
        return
    await _admin_gift_choose(call.message.answer, uid)
    await call.answer()


@dp.callback_query(F.data.startswith("adm_gift_pick:"))
@admin_only
async def adm_gift_pick(call: types.CallbackQuery, state: FSMContext):
    try:
        _, uid, key = call.data.split(":", 2)
        uid = int(uid)
    except ValueError:
        await call.answer()
        return
    if uid not in users_db or key not in GIFT_CATALOG:
        await call.answer("Щось не так 🤔", show_alert=True)
        return
    await state.set_state(AdminGiftStates.note)
    await state.update_data(gift_uid=uid, gift_key=key)
    await call.message.answer(
        f"Дарую {GIFT_CATALOG[key][0]} для {admin_label(uid)}.\n\n"
        "✍️ Напиши коротку записку, яку людина побачить разом з подарунком "
        "(напр. «Наталіє, це ведмедик з TikTok 🧸»), або натисни кнопку нижче.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📨 Надіслати без записки", callback_data="adm_gift_send")],
                [InlineKeyboardButton(text="✖️ Скасувати", callback_data="adm_gift_cancel")],
            ]
        ),
    )
    await call.answer()


async def _admin_gift_deliver(state: FSMContext, note: str | None, reply):
    data = await state.get_data()
    await state.clear()
    uid, key = data.get("gift_uid"), data.get("gift_key")
    if uid not in users_db or key not in GIFT_CATALOG:
        await reply("Подарунок не знайдено — почни спочатку: /admin → 🎁 Подарувати подарунок")
        return
    u = users_db[uid]
    u.setdefault("gifts", {})
    u["gifts"][key] = u["gifts"].get(key, 0) + 1
    request_save()
    title = GIFT_CATALOG[key][0]
    text = f"🎁 <b>Тобі подарунок від команди ANONimchyk!</b>\n\n{esc(title)}"
    if note:
        text += f"\n\n💌 <i>{protect_text(esc(note))}</i>"
    text += (
        "\n\nВін уже у твоєму інвентарі (👤 Мій профіль). "
        "Можеш залишити собі або подарувати співрозмовнику під час чату 💜"
    )
    ok = await safe_send(uid, text)
    if ok:
        await reply(f"✅ Подаровано {title} → {admin_label(uid)}")
    else:
        await reply(
            f"⚠️ Подарунок {title} додано в інвентар {admin_label(uid)}, "
            "але повідомлення не дійшло (людина могла заблокувати бота)."
        )


@dp.callback_query(F.data == "adm_gift_send")
@admin_only
async def adm_gift_send(call: types.CallbackQuery, state: FSMContext):
    await _admin_gift_deliver(state, None, call.message.answer)
    await call.answer()


@dp.message(AdminGiftStates.note)
async def adm_gift_note(message: types.Message, state: FSMContext):
    if not _is_banner_admin(message.from_user.id):
        await state.clear()
        return
    note = (message.text or "").strip()[:1500]
    await _admin_gift_deliver(state, note or None, message.answer)


@dp.callback_query(F.data == "adm_gift_cancel")
@admin_only
async def adm_gift_cancel(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Скасовано.")
    await call.answer()


class BannerStates(StatesGroup):
    photo = State()


async def send_banner(chat_id: int, place: str, caption: str | None = None, **kwargs) -> bool:
    """Надсилає банер (якщо адмін його поставив). Повертає True, якщо картинку надіслано."""
    file_id = banners.get(place)
    if not file_id:
        return False
    now = time.time()
    if caption is None and now - banner_last_shown.get((chat_id, place), 0) < BANNER_COOLDOWN:
        return False
    try:
        await bot.send_photo(chat_id, file_id, caption=caption, **kwargs)
    except TelegramAPIError as e:
        logging.warning("Банер %s не надіслано: %s", place, e)
        return False
    banner_last_shown[(chat_id, place)] = now
    return True


def _is_banner_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID and user_id in authorized_admins


def banners_admin_keyboard():
    rows = [
        [InlineKeyboardButton(text=("✅ " if key in banners else "➕ ") + label, callback_data=f"adm_bn_{key}")]
        for key, label in BANNER_PLACES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


BANNERS_HELP = (
    "🖼 <b>Банери</b>\n\n"
    "Картинка над екраном бота. ✅ — банер стоїть, ➕ — немає.\n"
    "Обери місце і надішли картинку <b>як фото</b> (не файлом).\n"
    "Найкращий розмір: <b>1280×720</b> (горизонтальна, 16:9)."
)


@dp.callback_query(F.data == "adm_banners")
@admin_only
async def adm_banners(call: types.CallbackQuery):
    await call.message.answer(BANNERS_HELP, reply_markup=banners_admin_keyboard())
    await call.answer()


@dp.callback_query(F.data.startswith("adm_bn_"))
@admin_only
async def adm_banner_pick(call: types.CallbackQuery, state: FSMContext):
    place = call.data[len("adm_bn_"):]
    if place not in BANNER_PLACES:
        await call.answer()
        return
    await state.set_state(BannerStates.photo)
    await state.update_data(banner_place=place)
    rows = []
    if place in banners:
        rows.append([InlineKeyboardButton(text="🗑 Прибрати цей банер", callback_data=f"adm_bnrm_{place}")])
        await send_banner(call.from_user.id, place, caption="Зараз стоїть оцей 👆")
    await call.message.answer(
        f"Надішли картинку для «{BANNER_PLACES[place]}» як фото.\nСкасувати: /cancel",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
    )
    await call.answer()


@dp.callback_query(F.data.startswith("adm_bnrm_"))
@admin_only
async def adm_banner_remove(call: types.CallbackQuery, state: FSMContext):
    place = call.data[len("adm_bnrm_"):]
    banners.pop(place, None)
    request_save()
    await state.clear()
    await call.message.answer(f"🗑 Банер «{BANNER_PLACES.get(place, place)}» прибрано.", reply_markup=banners_admin_keyboard())
    await call.answer()


@dp.message(BannerStates.photo)
async def adm_banner_photo(message: types.Message, state: FSMContext):
    if not _is_banner_admin(message.from_user.id):
        await state.clear()
        return
    if not message.photo:
        await message.answer("Надішли саме картинку як фото (не файлом і не текстом). Скасувати: /cancel")
        return
    data = await state.get_data()
    place = data.get("banner_place")
    await state.clear()
    if place not in BANNER_PLACES:
        await message.answer("Щось пішло не так — обери місце ще раз у 🖼 Банери.")
        return
    banners[place] = message.photo[-1].file_id  # найбільший розмір
    request_save()
    banner_last_shown.clear()
    await message.answer(
        f"✅ Банер для «{BANNER_PLACES[place]}» встановлено! Відкрий цей екран у боті, щоб подивитись.",
        reply_markup=banners_admin_keyboard(),
    )


# ---------------------------------------------------------------------------
# Щотижневий конкурс запрошень: топ-3 за тиждень отримують Premium
# ---------------------------------------------------------------------------
# Друг зараховується лише тоді, коли поспілкувався в першому справжньому чаті (від 1 хв, обидва писали) —
# так фейкові акаунти, які просто натиснули /start, нічого не дають.
CONTEST_PRIZES = [  # (місце, днів Premium)
    ("🥇", int(os.getenv("CONTEST_PRIZE_1", "30"))),
    ("🥈", int(os.getenv("CONTEST_PRIZE_2", "14"))),
    ("🥉", int(os.getenv("CONTEST_PRIZE_3", "7"))),
]
CONTEST_MIN_INVITES = int(os.getenv("CONTEST_MIN_INVITES", "3"))  # мінімум друзів, щоб потрапити в призери
CONTEST_FOREVER_BONUS = float(os.getenv("CONTEST_FOREVER_BONUS", "100"))  # грн, якщо в переможця вже вічний Premium
CONTEST_TOP_SHOWN = 10


def contest_week_key(day: date | None = None) -> str:
    y, w, _ = (day or kyiv_today()).isocalendar()
    return f"{y}-W{w:02d}"


def contest_week_range(key: str) -> str:
    y, w = key.split("-W")
    monday = date.fromisocalendar(int(y), int(w), 1)
    sunday = monday + timedelta(days=6)
    months = ["січня", "лютого", "березня", "квітня", "травня", "червня",
              "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]
    if monday.month == sunday.month:
        return f"{monday.day}–{sunday.day} {months[sunday.month - 1]}"
    return f"{monday.day} {months[monday.month - 1]} – {sunday.day} {months[sunday.month - 1]}"


def _contest_can_write() -> bool:
    return _lease_held or not DATABASE_URL


def contest_ranking() -> list[tuple[int, int]]:
    """[(user_id, кількість)] — більше друзів вище; при рівності вище той, хто набрав раніше."""
    items = [(uid, v[0], v[1]) for uid, v in (ref_contest.get("counts") or {}).items() if v[0] > 0]
    items.sort(key=lambda t: (-t[1], t[2]))
    return [(uid, n) for uid, n, _ts in items]


async def contest_finalize(week: str):
    """Видає призи за тиждень (рівно один раз) і повідомляє переможців."""
    if week in ref_contest.setdefault("awarded", []):
        return
    ref_contest["awarded"].append(week)
    ref_contest["awarded"] = ref_contest["awarded"][-20:]
    winners = []
    ranking = [(uid, n) for uid, n in contest_ranking() if n >= CONTEST_MIN_INVITES and uid not in banned_users]
    now = time.time()
    for (medal, days), (uid, n) in zip(CONTEST_PRIZES, ranking):
        u = users_db.get(uid)
        if u is None:
            continue
        current = u["perks"].get("premium", 0)
        if current == float("inf"):
            u["balance"] += CONTEST_FOREVER_BONUS
            prize = f"+{CONTEST_FOREVER_BONUS:.0f} грн (у тебе вже вічний Premium)"
        else:
            u["perks"]["premium"] = max(now, current) + days * DAY
            prize = f"Premium на {days} днів"
        winners.append((uid, n, f"{medal} {prize}"))
        await safe_send(
            uid,
            f"🏆 <b>Ти переміг у конкурсі запрошень!</b>\n\n"
            f"Тиждень {contest_week_range(week)}: {n} {_plural(n, 'друг', 'друзі', 'друзів')} → {medal} місце.\n"
            f"Твій приз: <b>{prize}</b> 💎\n\nДякуємо, що розповідаєш про нас! Новий тиждень уже почався — /contest",
        )
    ref_contest.setdefault("history", []).append({"week": week, "winners": winners})
    ref_contest["history"] = ref_contest["history"][-10:]
    request_save()
    if ADMIN_ID:
        lines = [f"{w[2]} — {admin_label(w[0])}: {w[1]} {_plural(w[1], 'друг', 'друзі', 'друзів')}" for w in winners] or ["Призерів немає (ніхто не набрав мінімум)."]
        await safe_send(ADMIN_ID, f"🏆 Конкурс запрошень за {contest_week_range(week)} завершено:\n" + "\n".join(lines))


async def contest_tick():
    """Перехід на новий тиждень: підсумки минулого і чистий старт."""
    if not _contest_can_write():
        return
    current = contest_week_key()
    week = ref_contest.get("week")
    if week == current:
        return
    if week:
        await contest_finalize(week)
    ref_contest["week"] = current
    ref_contest["counts"] = {}
    request_save()


async def contest_loop():
    while True:
        try:
            await contest_tick()
        except Exception as e:  # noqa: BLE001
            logging.warning("Конкурс запрошень: %s", e)
        try:
            await top_tick()
        except Exception as e:  # noqa: BLE001
            logging.warning("Топ тижня: %s", e)
        try:
            await meet_tick()
        except Exception as e:  # noqa: BLE001
            logging.warning("Година знайомств: %s", e)
        try:
            await queue_cleanup_tick()
        except Exception as e:  # noqa: BLE001
            logging.warning("Черга: %s", e)
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# 🧹 Жива черга: хто чекає > 3 хв — спершу «ти тут?»; хто > 10 хв — знімаємо з пошуку
# ---------------------------------------------------------------------------
QUEUE_STALE_AFTER = int(os.getenv("QUEUE_STALE_AFTER", "180"))  # після скількох секунд питати «ти тут?»
QUEUE_MAX_WAIT = int(os.getenv("QUEUE_MAX_WAIT", "600"))  # після скількох секунд знімати з черги
HERE_PING_COOLDOWN = 120
here_pinged: dict[int, float] = {}


async def ask_still_here(user_id: int):
    """Хтось шукає, а ця людина в черзі давно — питаємо, чи вона ще тут."""
    if user_id not in queue or time.time() - here_pinged.get(user_id, 0) < HERE_PING_COOLDOWN:
        return
    here_pinged[user_id] = time.time()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Я тут — почати чат", callback_data="here_ok")]])
    await safe_send(user_id, "🔔 Знайшовся співрозмовник! Ти ще тут?\nНатисни кнопку — і одразу почнемо чат 👇", reply_markup=kb)


@dp.callback_query(F.data == "here_ok")
async def here_ok(call: types.CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    if user_id in active_chats:
        await call.answer("Ти вже в чаті 🙂")
        return
    mode = search_mode.get(user_id, "normal")
    if user_id in queue:
        queue.remove(user_id)
    await call.answer()
    await run_search(call.message, state, mode, user_id=user_id)


async def queue_cleanup_tick():
    """Раз на хвилину: хто чекає довше QUEUE_MAX_WAIT — знімаємо з пошуку і пояснюємо."""
    now = time.time()
    for uid in list(queue):
        since = queue_since.get(uid)
        if since is None:
            queue_since[uid] = now  # старі записи (до оновлення) — відлік з цієї хвилини
            continue
        if now - since < QUEUE_MAX_WAIT:
            continue
        if uid in queue:
            queue.remove(uid)
        mode = search_mode.pop(uid, "normal")
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔍 Шукати знову", callback_data=f"wp_go:{mode}")]])
        await safe_send(
            uid,
            f"😔 Поки ніхто не знайшовся — пошук зупинено, щоб ти не чекав(-ла) даремно.\n\n"
            f"Натисни, щоб шукати знову. А о {MEET_HOUR}:00 — ⏰ Година знайомств, тоді онлайн найбільше людей.",
            reply_markup=kb,
        )


# ---------------------------------------------------------------------------
# ⏰ Година знайомств: щодня 21:00–22:00 (Київ) усі заходять одночасно
# ---------------------------------------------------------------------------
MEET_HOUR = int(os.getenv("MEET_HOUR", "21"))  # початок години (за Києвом)
MEET_REMIND_MINUTES = 5  # за скільки хвилин до початку нагадувати


def meet_is_live(now: datetime | None = None) -> bool:
    now = now or datetime.now(KYIV_TZ)
    return now.hour == MEET_HOUR


def online_now() -> int:
    now = time.time()
    return sum(1 for ts in last_seen.values() if now - ts <= ONLINE_WINDOW)


def _total_chats_sum() -> int:
    return sum(u.get("total_chats", 0) for uid, u in users_db.items() if uid > 0)


def meet_status_line() -> str:
    if meet_is_live():
        return f"🔥 <b>Зараз Година знайомств</b> — до {MEET_HOUR + 1}:00 співрозмовника знайдеш найшвидше!"
    return f"⏰ Година знайомств — щодня о {MEET_HOUR}:00. Усі заходять одночасно 💬"


def meet_reminder_text() -> str:
    online = max(online_now(), 1)
    return (
        f"⏰ <b>Година знайомств</b> — сьогодні о {MEET_HOUR}:00!\n\n"
        f"Щовечора з {MEET_HOUR}:00 до {MEET_HOUR + 1}:00 усі заходять одночасно, "
        "тож співрозмовник знаходиться за лічені секунди 💬\n\n"
        f"🟢 Зараз у боті: <b>{online}</b>\n\n"
        "Почни пошук уже зараз — або повертайся о дев'ятій 👇\n"
        "<i>Вимкнути нагадування: ⚙️ Налаштування → 🔔 Сповіщення</i>"
    )


def meet_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Знайти співрозмовника", callback_data="wp_go:normal")],
            [InlineKeyboardButton(text="❤️ Флірт-пошук", callback_data="wp_go:flirt")],
        ]
    )


def meet_recipients() -> list[int]:
    result = []
    for uid, u in users_db.items():
        if uid <= 0 or uid in banned_users:
            continue
        if uid in active_chats or uid in queue or uid in user_room:
            continue  # і так уже спілкується або шукає
        if not u.get("notify_meet", True) or u.get("gender", "Не вказано") == "Не вказано":
            continue  # вимкнув нагадування або ще не пройшов знайомство з ботом
        result.append(uid)
    result.sort(key=lambda x: last_seen.get(x, 0), reverse=True)
    return result


async def _meet_broadcast(recipients: list[int]):
    text, kb = meet_reminder_text(), meet_keyboard()
    sent = 0
    for uid in recipients:
        if uid in active_chats or uid in queue:
            continue
        if await safe_send(uid, text, reply_markup=kb):
            sent += 1
        await asyncio.sleep(0.05)  # ~20 повідомлень за секунду — в межах лімітів Telegram
    meet_hour["sent"] = sent
    request_save()


async def meet_tick():
    """Раз на хвилину: нагадування о 20:55, лічимо пік онлайну, о 22:00 — підсумок адміну."""
    if not _contest_can_write():
        return
    now = datetime.now(KYIV_TZ)
    today = now.date().isoformat()
    remind_from = MEET_HOUR * 60 - MEET_REMIND_MINUTES
    minutes = now.hour * 60 + now.minute
    if remind_from <= minutes < MEET_HOUR * 60 and meet_hour.get("reminded") != today:
        meet_hour["reminded"] = today  # спершу позначаємо — щоб після перезапуску не надіслати двічі
        meet_hour["chats_start"] = None
        meet_hour["peak"] = 0
        meet_hour["sent"] = 0
        request_save()
        task = asyncio.get_running_loop().create_task(_meet_broadcast(meet_recipients()))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    if meet_is_live(now):
        if meet_hour.get("chats_start") is None:
            meet_hour["chats_start"] = _total_chats_sum()
            request_save()
        meet_hour["peak"] = max(meet_hour.get("peak", 0), online_now())
    elif now.hour == (MEET_HOUR + 1) % 24 and meet_hour.get("summary") != today and meet_hour.get("chats_start") is not None:
        meet_hour["summary"] = today
        chats = max(0, (_total_chats_sum() - meet_hour["chats_start"]) // 2)
        request_save()
        if ADMIN_ID:
            await safe_send(
                ADMIN_ID,
                "⏰ <b>Година знайомств — підсумок</b>\n\n"
                f"📨 Нагадування отримали: <b>{meet_hour.get('sent', 0)}</b>\n"
                f"🟢 Пік онлайну: <b>{meet_hour.get('peak', 0)}</b>\n"
                f"💬 Нових чатів за годину: <b>{chats}</b>",
            )


@dp.message(Command("meettest"))
async def meet_test(message: types.Message, state: FSMContext):
    """Адмін: подивитися, як виглядає нагадування (надсилається тільки тобі)."""
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(meet_reminder_text(), reply_markup=meet_keyboard())
    await message.answer(f"👆 Так виглядатиме нагадування. Отримають зараз: {len(meet_recipients())} людей.")


# ---------------------------------------------------------------------------
# 🏆 Топ тижня: найкращі співрозмовники за 👍/👎, підсумки щонеділі о 20:00 (Київ)
# ---------------------------------------------------------------------------
TOP_MIN_CHAT_SECONDS = 90  # коротші чати в топі не оцінюються
TOP_MIN_VOTES = int(os.getenv("TOP_MIN_VOTES", "20"))  # мінімум оцінок за тиждень, щоб потрапити в топ
TOP_PUBLISH_HOUR = 20  # неділя, 20:00 за Києвом
TOP_PRIZES = [("🥇", 7), ("🥈", 3), ("🥉", 3)]  # місце -> днів Premium
TOP_STICKER_FILE_ID = os.getenv("TOP_STICKER_FILE_ID", "")  # анімований стікер-нагорода (file_id)


def top_week_key(now: datetime | None = None) -> str:
    """Тиждень топу закінчується в неділю о 20:00: зсуваємо час на 4 год, і неділя 20:00 стає понеділком."""
    now = now or datetime.now(KYIV_TZ)
    shifted = (now + timedelta(hours=24 - TOP_PUBLISH_HOUR)).date()
    return contest_week_key(shifted)


def top_record_vote(rater_id: int, target_id: int, up: bool):
    """Оцінка після справжнього чату (≥ 90 с). Одна людина оцінює іншу в топі лише раз на тиждень."""
    if rater_id == target_id or target_id in banned_users or rater_id in banned_users:
        return
    current = top_week_key()
    if top_week.get("week") is None:
        top_week["week"] = current
    if top_week.get("week") != current:
        return  # тиждень щойно закінчився — підсумки ще рахуються, ця оцінка в топ не йде
    pairs = top_week.setdefault("pairs", set())
    key = f"{rater_id}:{target_id}"
    if key in pairs:
        return
    pairs.add(key)
    v = top_week.setdefault("votes", {}).setdefault(target_id, [0, 0])
    v[0 if up else 1] += 1
    request_save()


def top_ranking() -> list[tuple[int, int, int, int]]:
    """[(user_id, 👍, 👎, %)] — вище більший відсоток 👍, при рівності — більше 👍."""
    items = []
    for uid, (up, down) in (top_week.get("votes") or {}).items():
        total = up + down
        if total < TOP_MIN_VOTES or uid in banned_users or uid == ADMIN_ID or uid not in users_db:
            continue
        items.append((uid, up, down, round(up * 100 / total)))
    items.sort(key=lambda t: (-t[1] / (t[1] + t[2]), -t[1]))
    return items


async def _top_broadcast(text: str):
    for uid in list(users_db):
        if uid in banned_users or uid < 0:
            continue
        await safe_send(uid, text)
        await asyncio.sleep(0.05)  # ~20 повідомлень за секунду — в межах лімітів Telegram


async def top_publish(week: str):
    """Підсумки тижня: рівно один раз (навіть після перезапуску на Render)."""
    published = top_week.setdefault("published", [])
    if week in published:
        return
    published.append(week)
    top_week["published"] = published[-20:]
    request_save()
    now = time.time()
    winners = []
    for (medal, days), (uid, up, down, pct) in zip(TOP_PRIZES, top_ranking()):
        u = users_db[uid]
        current = u["perks"].get("premium", 0)
        if current == float("inf"):
            prize = "у тебе вже вічний Premium 💎"
        else:
            # не накопичується: просто N днів від зараз (якщо вже є довший — нічого не забираємо)
            u["perks"]["premium"] = max(current, now + days * DAY)
            prize = f"Premium на {days} {_plural(days, 'день', 'дні', 'днів')}"
        u["top_week_count"] = u.get("top_week_count", 0) + 1
        winners.append((uid, up, down, pct, medal))
        await safe_send(
            uid,
            f"🏆 <b>Ти в топі тижня!</b> {medal} місце\n\n"
            f"👍 {up} · 👎 {down} — {pct}% співрозмовників залишились задоволені.\n"
            f"Твоя нагорода: <b>{prize}</b> + значок «🏆 Був у топі» в профілі.\n\nДякуємо, що робиш чат кращим ❤️",
        )
        if TOP_STICKER_FILE_ID:
            try:
                await bot.send_sticker(uid, TOP_STICKER_FILE_ID)
            except TelegramAPIError:
                pass
    top_week.setdefault("history", []).append({"week": week, "winners": winners})
    top_week["history"] = top_week["history"][-10:]
    request_save()
    if not winners:
        if ADMIN_ID:
            await safe_send(ADMIN_ID, f"🏆 Топ тижня: ніхто не набрав {TOP_MIN_VOTES} оцінок — розсилку пропущено.")
        return
    lines = [
        f"{medal} <b>{esc(users_db[uid]['nickname'])}</b> — 👍 {pct}% ({up + down} оцінок)"
        for uid, up, down, pct, medal in winners
    ]
    text = (
        "🏆 <b>Топ співрозмовників тижня</b>\n\n" + "\n".join(lines) +
        "\n\nЇх оцінили найтепліше ❤️ Став 👍 після гарної розмови — "
        "і наступного тижня тут можеш бути ти. Деталі: /best"
    )
    task = asyncio.get_running_loop().create_task(_top_broadcast(text))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def top_tick():
    if not _contest_can_write():
        return
    current = top_week_key()
    week = top_week.get("week")
    if week == current:
        return
    if week:
        await top_publish(week)
    top_week["week"] = current
    top_week["votes"] = {}
    top_week["pairs"] = set()
    request_save()


def top_text(user_id: int) -> str:
    up, down = (top_week.get("votes") or {}).get(user_id, [0, 0])
    total = up + down
    lines = ["🏆 <b>Топ співрозмовників тижня</b>\n",
             "Після чату довше 1,5 хв співрозмовники ставлять 👍 або 👎. "
             f"Щонеділі о 20:00 трійка з найбільшим відсотком 👍 (від {TOP_MIN_VOTES} оцінок) отримує нагороди:",
             "🥇 Premium на 7 днів · 🥈🥉 Premium на 3 дні + значок «🏆 Був у топі»\n",
             f"<b>Твій тиждень:</b> 👍 {up} · 👎 {down}"]
    if total < TOP_MIN_VOTES:
        lines.append(f"До участі ще {TOP_MIN_VOTES - total} {_plural(TOP_MIN_VOTES - total, 'оцінка', 'оцінки', 'оцінок')}.")
    else:
        place = next((i for i, t in enumerate(top_ranking(), 1) if t[0] == user_id), None)
        lines.append(f"Зараз ти на <b>{place}</b> місці ({round(up * 100 / total)}% 👍).")
    last = (top_week.get("history") or [None])[-1]
    if last and last.get("winners"):
        lines.append("\n<b>Минулого тижня:</b>")
        for uid, _up, _down, pct, medal in last["winners"]:
            lines.append(f"{medal} {esc(users_db.get(uid, {}).get('nickname', '?'))} — {pct}%")
    return "\n".join(lines)


@dp.message(Command("best"))
async def top_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    init_user(message.from_user.id)
    await message.answer(top_text(message.from_user.id))


def contest_on_real_chat(user_id: int):
    """Запрошений друг провів перший справжній чат — зараховуємо його тому, хто запросив."""
    u = users_db.get(user_id)
    if not u or u.get("ref_qualified") or not u.get("referred_by"):
        return
    u["ref_qualified"] = True
    ref_id = u["referred_by"]
    if ref_id not in users_db or ref_id in banned_users:
        return
    if ref_contest.get("week") != contest_week_key():
        ref_contest["week"] = ref_contest.get("week") or contest_week_key()
    entry = ref_contest.setdefault("counts", {}).setdefault(ref_id, [0, 0.0])
    entry[0] += 1
    entry[1] = time.time()
    place = next((i for i, (uid, _n) in enumerate(contest_ranking(), 1) if uid == ref_id), None)
    _notify_later(
        ref_id,
        f"🏆 Твій друг поспілкувався в першому чаті — <b>+1 у конкурсі запрошень</b>!\n"
        f"Цього тижня: {entry[0]} · місце: {place}. Деталі: /contest",
    )
    request_save()


def _ref_link(user_id: int) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}" if BOT_USERNAME else ""


def contest_text(user_id: int) -> str:
    week = ref_contest.get("week") or contest_week_key()
    ranking = contest_ranking()
    lines = [
        f"🏆 <b>Конкурс запрошень</b> · {contest_week_range(week)}",
        "",
        "Запрошуй друзів за своїм посиланням. Друг зараховується, коли поспілкується "
        "в першому чаті (від 1 хв).",
        "",
        "<b>Призи щонеділі опівночі:</b>",
    ]
    lines += [f"{medal} Premium на {days} днів" for medal, days in CONTEST_PRIZES]
    lines.append(f"<i>Щоб потрапити в призери — мінімум {CONTEST_MIN_INVITES} друзі.</i>")
    lines.append("")
    if ranking:
        lines.append("<b>Топ тижня:</b>")
        medals = [m for m, _d in CONTEST_PRIZES]
        for i, (uid, n) in enumerate(ranking[:CONTEST_TOP_SHOWN], 1):
            mark = medals[i - 1] if i <= len(medals) else f"{i}."
            you = " ← ти" if uid == user_id else ""
            nick = esc(users_db.get(uid, {}).get("nickname", "Користувач"))
            lines.append(f"{mark} {nick} — {n}{you}")
    else:
        lines.append("Цього тижня ще ніхто не запросив друзів — стань першим! 🚀")
    mine = next(((i, n) for i, (uid, n) in enumerate(ranking, 1) if uid == user_id), None)
    lines.append("")
    if mine:
        lines.append(f"📍 Ти: <b>{mine[1]}</b> {_plural(mine[1], 'друг', 'друзі', 'друзів')} · <b>{mine[0]}</b> місце")
    else:
        lines.append("📍 Ти поки не в рейтингу — надішли посилання другу!")
    history = ref_contest.get("history") or []
    if history and history[-1].get("winners"):
        last = history[-1]
        names = ", ".join(
            f"{w[2].split()[0]} {esc(users_db.get(w[0], {}).get('nickname', '?'))}" for w in last["winners"]
        )
        lines.append(f"\n🎉 Переможці минулого тижня: {names}")
    link = _ref_link(user_id)
    if link:
        lines.append(f"\n🔗 Твоє посилання:\n<code>{esc(link)}</code>")
    return "\n".join(lines)


def contest_keyboard(user_id: int):
    link = _ref_link(user_id)
    rows = []
    if link:
        share_text = "Заходь в ANONimchyk — анонімний чат для знайомств і розмов 🎭"
        share = f"https://t.me/share/url?url={quote(link)}&text={quote(share_text)}"
        rows.append([InlineKeyboardButton(text="📤 Надіслати другу", url=share)])
    rows.append([InlineKeyboardButton(text="🔄 Оновити", callback_data="contest_open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("contest"))
async def contest_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    init_user(message.from_user.id)
    await contest_tick()
    await message.answer(contest_text(message.from_user.id), reply_markup=contest_keyboard(message.from_user.id))


@dp.callback_query(F.data == "contest_open")
async def contest_open(call: types.CallbackQuery):
    init_user(call.from_user.id)
    await contest_tick()
    text, kb = contest_text(call.from_user.id), contest_keyboard(call.from_user.id)
    if call.message.text and call.message.text.startswith("🏆 Конкурс запрошень"):
        try:
            await call.message.edit_text(text, reply_markup=kb)
        except TelegramAPIError:
            pass  # нічого не змінилось
        await call.answer("Оновлено")
        return
    await call.message.answer(text, reply_markup=kb)
    await call.answer()


# ---------------------------------------------------------------------------
# Правила, безпека, підтримка з оплат
# ---------------------------------------------------------------------------
RULES_TEXT = (
    "📜 <b>Правила ANONimchyk</b>\n\n"
    "1. <b>Повага.</b> Без образ, погроз, цькування й дискримінації.\n"
    "2. <b>Без спаму.</b> Ніякої реклами, посилань, продажів і «заробітку».\n"
    "3. <b>Інтимне — лише у ❤️ флірт-чаті (18+)</b> і тільки за взаємною згодою. "
    "Будь-який такий контент із неповнолітніми чи для неповнолітніх — бан назавжди.\n"
    "4. <b>Не вимагай</b> фото, номер, адресу, гроші чи паролі. Не публікуй чужі особисті дані.\n"
    "5. <b>Не видавай себе</b> за іншу людину чи адміністрацію, не шахрай.\n"
    "6. <b>Вказуй справжній вік.</b> Флірт-чат — лише з 18 років.\n\n"
    f"⚖️ Порушення → скарга «{BTN_REPORT}». Після кількох скарг акаунт блокується автоматично, "
    "за серйозні порушення — бан адміністратором без попередження.\n\n"
    "🤫 Бот не показує співрозмовнику твій Telegram-акаунт. Але свою анонімність бережи і сам — "
    "детальніше: /safety\n"
    "💳 Питання щодо оплат: /paysupport"
)

SAFETY_TEXT = (
    "🛡 <b>Безпека в анонімному чаті</b>\n\n"
    "• <b>Не кажи</b>, де живеш, у якій школі вчишся, своє прізвище та номер телефону.\n"
    "• <b>Не надсилай</b> фото, особливо особисті. Надіслане вже не повернеш.\n"
    "• <b>Не переходь</b> у інші месенджери чи соцмережі з незнайомцями й не погоджуйся на зустрічі.\n"
    "• <b>Ніхто не має права</b> вимагати від тебе фото, гроші, коди чи паролі.\n"
    "• Якщо співрозмовник лякає, тисне чи пише неприємне — просто завершуй чат.\n\n"
    f"🚨 <b>{BTN_REPORT}</b> — скарга, адміністратор перевірить.\n"
    f"🚫 <b>{BTN_BLACKLIST_ADD}</b> — ця людина більше ніколи тобі не трапиться.\n\n"
    "Якщо сталося щось серйозне — розкажи дорослому, якому довіряєш. "
    "В Україні безкоштовно допомагає дитяча лінія <b>116 111</b>."
)

MATCH_SAFETY_REMINDER = (
    "🛡 Пам'ятай: не кажи, де живеш, і не надсилай фото незнайомцям. "
    f"Щось не так — «{BTN_REPORT}» або «{BTN_BLACKLIST_ADD}». Більше: /safety"
)
MATCH_SAFETY_EVERY = 24 * 3600  # коротке нагадування неповнолітнім — не частіше разу на добу
_match_safety_last: dict[int, float] = {}

PAYSUPPORT_TEXT = (
    "💳 <b>Питання щодо оплат</b>\n\n"
    "Баланс у боті — внутрішня валюта: ним оплачуються Premium, подарунки та послуги в магазині. "
    "Гроші з балансу не виводяться.\n\n"
    "<b>Оплатив, а баланс не поповнився?</b>\n"
    "• Банка monobank / реквізити: зарахування може тривати до кількох хвилин, "
    "а якщо в коментарі не було коду — до перевірки адміністратором.\n"
    "• Напиши в підтримку: <b>суму, час оплати</b> і код заявки <b>P…</b> (якщо був) "
    "— і ми все перевіримо.\n\n"
    "Повернення коштів можливе у разі технічної помилки, коли оплачену послугу не було надано."
)


async def maybe_send_safety_memo(user_id: int, u: dict):
    """Повна пам'ятка неповнолітнім — один раз (після анкети чи зміни віку)."""
    if is_minor(u) and not u.get("safety_memo_shown"):
        u["safety_memo_shown"] = True
        request_save()
        await safe_send(user_id, SAFETY_TEXT)


async def send_match_safety(user_id: int, u: dict):
    if not is_minor(u):
        return
    if not u.get("safety_memo_shown"):
        await maybe_send_safety_memo(user_id, u)
        _match_safety_last[user_id] = time.time()
        return
    now = time.time()
    if now - _match_safety_last.get(user_id, 0) < MATCH_SAFETY_EVERY:
        return
    _match_safety_last[user_id] = now
    await safe_send(user_id, MATCH_SAFETY_REMINDER)


@dp.message(Command("rules", "terms"))
async def rules_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(RULES_TEXT)


@dp.message(Command("safety"))
async def safety_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(SAFETY_TEXT)


@dp.message(Command("paysupport"))
async def paysupport_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        PAYSUPPORT_TEXT,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="💬 Написати в підтримку", callback_data="support_start")]]
        ),
    )


# ---------------------------------------------------------------------------
# Пересилання повідомлень (завжди останнім!)
# ---------------------------------------------------------------------------
@dp.message()
async def relay_messages(message: types.Message):
    user_id = message.from_user.id

    room_id = user_room.get(user_id)
    if room_id is not None:
        room = rooms.get(room_id)
        if room is None:
            user_room.pop(user_id, None)
        else:
            if not message.text:
                await message.answer(
                    "📷 У кімнатах поки підтримується лише текст — це для безпеки спілкування в групі."
                )
                return
            if LINK_REGEX.search(message.text):
                await message.answer(
                    "🚫 Повідомлення з посиланнями або контактами заборонено."
                )
                return
            u = init_user(user_id)
            task_event(user_id, "room_msg")
            broadcast_text = f"<b>{esc(u['nickname'])}:</b> {protect_text(esc(message.text))}"
            for member_id in list(room["members"]):
                if member_id == user_id:
                    continue
                ok = await safe_send(member_id, broadcast_text)
                if not ok:
                    room["members"].discard(member_id)
                    user_room.pop(member_id, None)
            return

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

    if is_blocked_media(users_db.get(partner_id), message):
        await message.answer("🛡 Співрозмовник не приймає фото, відео й файли — напиши текстом 🙂")
        return

    try:
        await message.copy_to(chat_id=partner_id, protect_content=should_protect(users_db.get(user_id)))
        on_chat_message(user_id)
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
        types.BotCommand(command="rooms", description="👥 Кімнати за інтересами"),
        types.BotCommand(command="help", description="🆘 Допомога / зв'язок з адміном"),
        types.BotCommand(command="friends", description="👫 Друзі"),
        types.BotCommand(command="online", description="👥 Хто зараз онлайн"),
        types.BotCommand(command="premium", description="💎 Premium"),
        types.BotCommand(command="flirt", description="❤️ Флірт-пошук (18+)"),
        types.BotCommand(command="interests", description="🧩 Пошук за інтересами"),
        types.BotCommand(command="myinterests", description="🧩 Мої інтереси в профілі"),
        types.BotCommand(command="tasks", description="📋 Щоденні завдання"),
        types.BotCommand(command="contest", description="🏆 Конкурс запрошень"),
        types.BotCommand(command="best", description="🌟 Топ співрозмовників тижня"),
        types.BotCommand(command="mood", description="🎭 Мій настрій і пісня"),
        types.BotCommand(command="topic", description="💬 Тема для розмови"),
        types.BotCommand(command="founders", description="👑 Засновники — перші 100"),
        types.BotCommand(command="rules", description="📜 Правила"),
        types.BotCommand(command="safety", description="🛡 Безпека"),
        types.BotCommand(command="paysupport", description="💳 Питання щодо оплат"),
        types.BotCommand(command="silent", description="🔒 Захист моїх медіа (Premium)"),
    ]
    await bot.set_my_commands(default_commands, scope=types.BotCommandScopeDefault())
    DEFAULT_COMMANDS[:] = default_commands
    if LANG_RU_ENABLED:
        try:  # меню команд російською — для тих, у кого Telegram російською
            ru_commands = [types.BotCommand(command=c.command, description=tr_ru(c.description)) for c in default_commands]
            await bot.set_my_commands(ru_commands, scope=types.BotCommandScopeDefault(), language_code="ru")
        except Exception as e:  # noqa: BLE001
            logging.warning("Команди російською: %s", e)

    if ADMIN_ID:
        admin_commands = default_commands + [
            types.BotCommand(command="admin", description="🔐 Адмін-панель"),
            types.BotCommand(command="addbalance", description="💰 Поповнити баланс користувачу"),
            types.BotCommand(command="confirm", description="🏦 Підтвердити оплату за кодом"),
            types.BotCommand(command="mono", description="🫙 Стан Банки monobank"),
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
    # Дані з бази завантажуємо ДО того, як бот почне приймати повідомлення.
    await load_state_from_db()
    poll_task = asyncio.create_task(crypto_poll_loop())
    save_task = asyncio.create_task(persistence_loop())
    lease_task = asyncio.create_task(lease_heartbeat_loop())
    mono_task = asyncio.create_task(mono_poll_loop())
    contest_task = asyncio.create_task(contest_loop())
    try:
        # Кожен крок ізольований try/except, щоб тимчасова мережева помилка
        # Telegram API не вбивала весь процес (і "Application exited early" на Render).
        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except Exception as e:  # noqa: BLE001
            logging.warning("Не вдалося скинути webhook: %s", e)

        try:
            await setup_bot_commands()
        except Exception as e:  # noqa: BLE001
            logging.warning("Не вдалося встановити команди бота: %s", e)

        try:
            me = await bot.get_me()
            BOT_USERNAME = me.username or ""
        except Exception as e:  # noqa: BLE001
            logging.warning("Не вдалося отримати інформацію про бота (get_me): %s", e)

        # Якщо polling впаде (напр. TelegramConflictError через старий інстанс),
        # логуємо чітку причину і пробуємо знову, а не завершуємо процес.
        while True:
            try:
                await dp.start_polling(bot)
                break
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logging.error("Помилка під час polling, перезапуск через 5с: %s", e)
                await asyncio.sleep(5)
    finally:
        poll_task.cancel()
        save_task.cancel()
        lease_task.cancel()
        mono_task.cancel()
        contest_task.cancel()
        await save_state_to_db()  # фінальне збереження при зупинці (деплой/перезапуск)
        await release_lease()  # тепер нова копія може забрати свіжі дані
        await bot.session.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
