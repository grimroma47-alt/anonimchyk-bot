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
        await asyncio.sleep(300)


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
            broadcast_text = f"<b>{esc(u['nickname'])}:</b> {esc(message.text)}"
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
        types.BotCommand(command="rules", description="📜 Правила"),
        types.BotCommand(command="safety", description="🛡 Безпека"),
        types.BotCommand(command="paysupport", description="💳 Питання щодо оплат"),
        types.BotCommand(command="silent", description="🔒 Захист моїх медіа (Premium)"),
    ]
    await bot.set_my_commands(default_commands, scope=types.BotCommandScopeDefault())

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



# === КІНЕЦЬ part5.py ===
