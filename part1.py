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
    BufferedInputFile,
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
# Бот працює лише в особистих чатах: у групах і каналах, куди його додали адміном, він мовчить
dp.message.filter(F.chat.type == "private")
dp.callback_query.filter(F.message.chat.type == "private")

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
            CUR_UID.set(user.id)
            last_seen[user.id] = time.time()
            stats_day()["active"].add(user.id)
            known = users_db.get(user.id)
            if known is not None:
                known["last_active_day"] = today_str()
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
CUR_UID: contextvars.ContextVar = contextvars.ContextVar("cur_uid", default=None)
# Чия стать визначає закінчення (напр. завдання в грі адресоване співрозмовнику, а не отримувачу)
GENDER_FOR: contextvars.ContextVar = contextvars.ContextVar("gender_for", default=None)
_P0, _P1 = "⁣", "⁤"  # невидимі межі «не перекладати» (текст людей)

if "RU_PAIRS" not in globals():
    try:
        from lang_ru import RU_PAIRS  # коли main.py запускається напряму, а не через запускач
    except ImportError:
        RU_PAIRS = {}
        if LANG_RU_ENABLED:
            logging.warning("🌐 LANG_RU=1, але файл lang_ru.py не знайдено поруч із ботом — переклад вимкнено")

_CYR = "а-яіїєґёА-ЯІЇЄҐЁ'ʼ"
_GRN_RX = re.compile(f"(?<![{_CYR}\u2060])грн(?![{_CYR}])")  # «200\u2060грн» — гривні для виводу, лишаються гривнями
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


# ⚧ Закінчення за статтю: «поїхав(-ла)» → «поїхав» / «поїхала». Стать не вказана — лишаємо як є.
_G_UK = re.compile(f"([{_CYR}]+)\\((-ла|-лася|-ою)\\)")
_G_RU = re.compile(f"([{_CYR}]+)\\((а|ла|на)\\)")


def _g_uk(m, fem: bool) -> str:
    w, suf = m.group(1), m.group(2)
    if not fem:
        return w
    if suf == "-ла":
        if w.endswith("міг"):
            return w[:-3] + "могла"
        return (w[:-1] if w.endswith("в") else w) + "ла"
    if suf == "-лася":
        return (w[:-3] if w.endswith("вся") else w) + "лася"
    return (w[:-2] if w.endswith(("им", "ий")) else w) + "ою"


def _g_ru(m, fem: bool) -> str:
    w, suf = m.group(1), m.group(2)
    if not fem:
        return w
    if suf == "на":
        return (w[:-2] if w.endswith("ен") else w) + "на"
    return w + suf


def apply_gender(text: str, gender) -> str:
    if gender not in ("Хлопець", "Дівчина") or not isinstance(text, str) or "(" not in text:
        return text
    fem = gender == "Дівчина"
    text = _G_UK.sub(lambda m: _g_uk(m, fem), text)
    return _G_RU.sub(lambda m: _g_ru(m, fem), text)


def gender_of(uid) -> str | None:
    return (users_db.get(uid) or {}).get("gender") if isinstance(uid, int) else None


def translate_out(text, lang: str, gender=None):
    """Переклад вихідного тексту; шматки між _P0/_P1 лишаються як є. Для "uk" лише прибирає позначки."""
    if not isinstance(text, str):
        return text
    if _P0 not in text:
        return apply_gender(tr_ru(text) if lang == "ru" else text, gender)
    out = []
    for i, chunk in enumerate(re.split(f"({_P0}.*?{_P1})", text, flags=re.S)):
        if chunk.startswith(_P0) and chunk.endswith(_P1):
            out.append(chunk[1:-1])
        else:
            out.append(apply_gender(tr_ru(chunk) if lang == "ru" else chunk, gender))
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


QUIET_FROM, QUIET_TO = 23, 8


def quiet_hours_now(u: dict | None) -> bool:
    if not u or not u.get("quiet"):
        return False
    h = datetime.now(KYIV_TZ).hour
    return h >= QUIET_FROM or h < QUIET_TO


def is_quiet_for(chat_id: int) -> bool:
    """Без звуку — лише якщо людина ввімкнула тихі години, зараз ніч і вона не сидить у боті / в чаті."""
    if chat_id in active_chats or time.time() - last_seen.get(chat_id, 0) < 120:
        return False
    return quiet_hours_now(users_db.get(chat_id))


def status_markup(chat_id: int, markup):
    """Якщо людина в чаті / пошуку / кімнаті — над головним меню з'являється мітка, як туди повернутися."""
    try:
        rows = getattr(markup, "keyboard", None)
        if not rows or rows[0][0].text != BTN_SEARCH:
            return markup
        if chat_id in active_chats:
            label = BTN_BACK_CHAT
        elif chat_id in queue:
            label = BTN_SEARCHING
        elif chat_id in user_room:
            label = BTN_BACK_ROOM
        else:
            return markup
        return markup.model_copy(update={"keyboard": [[KeyboardButton(text=label)]] + list(rows)})
    except Exception:  # noqa: BLE001
        return markup


async def i18n_request_middleware(make_request, bot_, method):
    """Перекладає все, що бот надсилає (текст, підписи, кнопки, спливаючі відповіді)."""
    try:
        chat_id = getattr(method, "chat_id", None)
        lang = user_lang(chat_id) if isinstance(chat_id, int) else (CUR_LANG.get() if LANG_RU_ENABLED else "uk")
        gender = GENDER_FOR.get() or gender_of(chat_id if isinstance(chat_id, int) else CUR_UID.get())
        upd = {}
        for name in ("text", "caption", "title", "description"):
            val = getattr(method, name, None)
            if isinstance(val, str) and (lang == "ru" or _P0 in val or (gender and "(" in val)):
                upd[name] = translate_out(val, lang, gender)
        if isinstance(chat_id, int) and hasattr(method, "disable_notification") and is_quiet_for(chat_id):
            upd["disable_notification"] = True  # 🌙 тихі години: приходить без звуку
        markup = getattr(method, "reply_markup", None)
        if isinstance(chat_id, int) and markup is not None:
            marked = status_markup(chat_id, markup)
            if marked is not markup:
                markup = marked
                upd["reply_markup"] = marked
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
            if not back and " · " in text:  # кнопки з лічильником: «👥 Комнаты по интересам · сейчас 5»
                head = _tr_back.get(text.split(" · ")[0].strip())
                back = head and head + " · " + text.split(" · ", 1)[1]
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
compat_last: dict[int, str] = {}  # 💯 сумісність щойно завершеного чату
game_state: dict[frozenset, dict] = {}  # 🎲 ігри в чаті
last_chat_mode: dict[int, str] = {}  # у якому режимі був останній чат (для автопошуку)
# Банери-картинки для екранів: місце -> file_id фото в Telegram. Ставить адмін з телефона.
banners: dict[str, str] = {}
# Щотижневий конкурс запрошень: {"week": "2026-W41", "counts": {user_id: [кількість, час останнього]},
# "awarded": [тижні, за які призи вже видано], "history": [{"week", "winners": [(user_id, n, приз)]}]}
ref_contest: dict = {"week": None, "counts": {}, "awarded": [], "history": []}
# 🏆 Топ тижня співрозмовників (за 👍/👎). Тиждень = з неділі 20:00 до неділі 20:00 за Києвом.
# {"week", "votes": {user_id: [👍, 👎]}, "pairs": {"хто:кого"}, "published": [тижні], "history": [{"week","winners"}]}
top_week: dict = {"week": None, "votes": {}, "pairs": set(), "published": [], "history": []}
letters_db: dict = {"items": [], "delivered_day": None}  # 💌 нічні листи
confess_db: dict = {"next_id": 1, "pub_no": 0, "items": {}, "last_by": {}}  # 🤫 анонімні зізнання
# 💸 Заявки на вивід: номер карти НЕ зберігаємо (лише останні 4 цифри) — повний номер бачить тільки адмін у повідомленні
withdraw_db: dict = {"next": 1, "items": {}}
launch_state: dict = {"done": False, "sent": 0}  # 🎭 розсилка про запуск «Зняти маску»
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
    # 🎃 лімітовані — у продажу лише на Хелловін (хто купив — лишаються назавжди)
    "pumpkin": ("🎃 Гарбуз", 20),
    "bat": ("🦇 Кажан", 30),
    "ghost": ("👻 Привид", 50),
}
LIMITED_GIFTS = {"pumpkin", "bat", "ghost"}
# Частка від ціни подарунка, яка йде отримувачу (решта — дохід адміна)
GIFT_RECIPIENT_SHARE = float(os.getenv("GIFT_RECIPIENT_SHARE", "0.25"))  # 0.25 = 25%
# 💸 Вивід зароблених грошей на карту: мінімум і скільки днів «відлежуються» гроші з подарунків
WITHDRAW_MIN = float(os.getenv("WITHDRAW_MIN", "200"))
EARN_HOLD_DAYS = int(os.getenv("EARN_HOLD_DAYS", "7"))

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
    keys = [k for k, (_t, p) in GIFT_CATALOG.items() if lo <= p <= hi and k not in LIMITED_GIFTS]
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
BTN_UNMASK = "🎭 Зняти маску"
BTN_ROOMS = "👥 Кімнати за інтересами"
BTN_ROOM_LEAVE = "🚪 Вийти з кімнати"
BTN_ROOM_REPORT = "🚨 Поскаржитися на учасника"
BTN_HELP = "🆘 Допомога"
BTN_CONFESS = "🤫 Зізнання"
BTN_LETTER = "💌 Нічний лист"
BTN_VOICE_SEARCH = "🎙 Голосовий чат"
BTN_GAME = "🎲 Гра"
BTN_FUN = "✨ Розваги"
BTN_BACK_CHAT = "💬 Ти в чаті — повернутися"
BTN_SEARCHING = "⏳ Йде пошук · зупинити"
BTN_MORE = "☰ Ще"
BTN_MENU_BACK = "⬅️ Головне меню"
BTN_BACK_ROOM = "👥 Ти в кімнаті — повернутися"
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


class ConfessStates(StatesGroup):
    text = State()


class WithdrawStates(StatesGroup):
    amount = State()
    card = State()


# ---------------------------------------------------------------------------
# Клавіатури
# ---------------------------------------------------------------------------
def get_main_keyboard():
    # Зверху — дві головні дії на всю ширину, нижче — решта дрібнішими кнопками
    K = KeyboardButton
    return ReplyKeyboardMarkup(
        keyboard=[
            [K(text=BTN_SEARCH)],
            [K(text=BTN_VOICE_SEARCH)],
            [K(text=rooms_button_label())],
            [K(text=BTN_FLIRT), K(text=BTN_INTERESTS)],
            [K(text=BTN_PREMIUM), K(text=BTN_TOPUP), K(text=BTN_DAILY)],
            [K(text=BTN_PROFILE), K(text=BTN_WALLET), K(text=BTN_MORE)],
        ],
        resize_keyboard=True,
    )


def get_more_keyboard():
    """☰ Ще: усі інші розділи — щоб головне меню було коротким."""
    K = KeyboardButton
    return ReplyKeyboardMarkup(
        keyboard=[
            [K(text=BTN_FUN), K(text=BTN_CONFESS), K(text=BTN_LETTER)],
            [K(text=BTN_SHOP), K(text=BTN_TASKS), K(text=BTN_LOTTERY)],
            [K(text=BTN_FRIENDS), K(text=BTN_FILTERS)],
            [K(text=BTN_ONLINE), K(text=BTN_TOP), K(text=BTN_SETTINGS)],
            [K(text=BTN_HELP), K(text=BTN_MENU_BACK)],
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
            [KeyboardButton(text=BTN_UNMASK), KeyboardButton(text=BTN_GAME)],
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
    hw = halloween_active()
    order = [k for k in GIFT_CATALOG if k in LIMITED_GIFTS] + [k for k in GIFT_CATALOG if k not in LIMITED_GIFTS]
    buttons = [
        InlineKeyboardButton(
            text=("⏳ " if key in LIMITED_GIFTS else "") + f"{GIFT_CATALOG[key][0]} — {GIFT_CATALOG[key][1]} грн",
            callback_data=f"buygift_{key}",
        )
        for key in order
        if key not in LIMITED_GIFTS or hw
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
            [InlineKeyboardButton(text=f"💸 Вивести на карту (від {uah(WITHDRAW_MIN, 0)})", callback_data="wd_open")],
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
            [InlineKeyboardButton(text=f"🎙 Маска голосу: {voice_mask_label(u)}", callback_data="vm_menu")],
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
            [
                InlineKeyboardButton(text="💸 Виводи", callback_data="adm_wd"),
                InlineKeyboardButton(text="💰 Топ балансів", callback_data="adm_topbal"),
            ],
            [InlineKeyboardButton(text="🎰 Обнулити рулетку", callback_data="adm_roul_reset")],
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
    vip += " 🎃" if u.get("hw2026") else ""
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
    paid_pool(u)  # старим користувачам — з усіх попередніх поповнень
    u["paid_pool"] = round(u["paid_pool"] + amount, 2)
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
        unmask_requests.pop(user_id, None)
        unmask_requests.pop(partner_id, None)
        unmask_done.discard(frozenset((user_id, partner_id)))
        game_state.pop(frozenset((user_id, partner_id)), None)
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
    compat = compat_last.pop(chat_id, None)
    if compat:
        await safe_send(chat_id, compat)
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




# === КІНЕЦЬ part1.py ===
