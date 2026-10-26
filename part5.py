# ---------------------------------------------------------------------------
# Редагування профілю кнопками (без /edit_profile)
# ---------------------------------------------------------------------------
class ProfileEditStates(StatesGroup):
    age = State()
    country = State()
    bio = State()


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
        f"🧩 Інтереси: {interests_line(u)}\n"
        f"📝 Про себе: {('«' + protect_text(esc(u['bio'])) + '»') if u.get('bio') else 'не заповнено'}\n\n"
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
            [InlineKeyboardButton(text="📝 Про себе", callback_data="pe_bio")],
        ]
    )


async def _show_profile_editor(chat_id: int, u: dict):
    await safe_send(chat_id, profile_edit_text(u), reply_markup=profile_edit_keyboard())


@dp.callback_query(F.data == "pe_menu")
async def pe_menu(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_profile_editor(call.from_user.id, init_user(call.from_user.id))
    await call.answer()


BIO_MAX = 70


@dp.callback_query(F.data == "pe_bio")
async def pe_bio(call: types.CallbackQuery, state: FSMContext):
    u = init_user(call.from_user.id)
    await state.set_state(ProfileEditStates.bio)
    rows = [[InlineKeyboardButton(text="🗑 Прибрати опис", callback_data="pe_bio_del")]] if u.get("bio") else []
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="pe_menu")])
    await call.message.answer(
        f"📝 Напиши коротко про себе — до {BIO_MAX} символів. Співрозмовник побачить це на початку чату.\n\n"
        "Наприклад: «Люблю нічні прогулянки й інді-музику 🎧»\n"
        "<i>Без імені, контактів, @ніків і посилань — анонімність понад усе.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await call.answer()


@dp.callback_query(F.data == "pe_bio_del")
async def pe_bio_del(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    u = init_user(call.from_user.id)
    u.pop("bio", None)
    request_save()
    await _show_profile_editor(call.from_user.id, u)
    await call.answer("Опис прибрано")


@dp.message(ProfileEditStates.bio)
async def pe_bio_save(message: types.Message, state: FSMContext):
    text = " ".join((message.text or "").split())
    if not text:
        await message.answer("Напиши опис текстом 🙂")
        return
    if len(text) > BIO_MAX:
        await message.answer(f"Задовго — {len(text)} символів. Скороти до {BIO_MAX} 🙂")
        return
    if LINK_REGEX.search(text) or PHONE_REGEX.search(text) or "@" in text:
        await message.answer("🚫 Без контактів, @ніків і посилань — напиши інакше 🙂")
        return
    await state.clear()
    u = init_user(message.from_user.id)
    u["bio"] = text
    request_save()
    await message.answer("✅ Збережено!")
    await _show_profile_editor(message.from_user.id, u)


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
        await call.message.answer("🌍 Напиши свою країну (наприклад: Франція, США, Канада). Скасувати: /cancel")
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
    country = normalize_country(message.text or "")
    if len(country) < 2 or LINK_REGEX.search(country):
        await message.answer("Напиши назву країни. Скасувати: /cancel")
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
    "voicechat": "🎙 Голосовий чат",
    "voice": "🎙 Маска голосу",
    "flirt": "❤️ Флірт-пошук",
    "interests": "🧩 За інтересами",
    "games": "🎲 Ігри",
    "fun": "✨ Розваги",
    "confess": "🤫 Зізнання",
    "letter": "💌 Нічний лист",
    "unmask": "🎭 Маски знято",
    "friends": "👫 Друзі",
    "rooms": "👥 Кімнати",
    "halloween": "🎃 Хелловін",
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
    btns = [
        InlineKeyboardButton(text=("✅ " if key in banners else "➕ ") + label, callback_data=f"adm_bn_{key}")
        for key, label in BANNER_PLACES.items()
    ]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
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
        try:
            await launch_tick()
        except Exception as e:  # noqa: BLE001
            logging.warning("Запуск «Зняти маску»: %s", e)
        try:
            await letters_tick()
        except Exception as e:  # noqa: BLE001
            logging.warning("Нічні листи: %s", e)
        try:
            await rooms_tick()
        except Exception as e:  # noqa: BLE001
            logging.warning("Живі кімнати: %s", e)
        try:
            await comeback_tick()
        except Exception as e:  # noqa: BLE001
            logging.warning("«Ми скучили»: %s", e)
        try:
            await hw_night_tick()
        except Exception as e:  # noqa: BLE001
            logging.warning("Ніч страшилок: %s", e)
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
    await pull_from_room(user_id)
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
        if now - since < BG_SEARCH_MAX:
            if not bg_notified.get(uid):
                bg_notified[uid] = now
                await safe_send(uid, bg_search_text(), reply_markup=bg_search_kb())
            continue
        bg_notified.pop(uid, None)
        if uid in queue:
            queue.remove(uid)
        await pull_from_room(uid, quiet=True)
        mode = search_mode.pop(uid, "normal")
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔍 Шукати знову", callback_data=f"wp_go:{mode}")]])
        await safe_send(
            uid,
            f"😔 За дві години ніхто не знайшовся — пошук зупинено.\n\n"
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
    rows = [
        [InlineKeyboardButton(text="🔍 Знайти співрозмовника", callback_data="wp_go:normal")],
        [InlineKeyboardButton(text="❤️ Флірт-пошук", callback_data="wp_go:flirt")],
    ]
    extra = evening_extra_button()
    if extra:
        rows.append([extra])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def meet_recipients() -> list[int]:
    result = []
    for uid, u in users_db.items():
        if uid <= 0 or uid in banned_users:
            continue
        if uid in active_chats or uid in queue or uid in user_room:
            continue  # і так уже спілкується або шукає
        if not u.get("notify_meet", True):
            continue  # вимкнув нагадування
        if u.get("gender", "Не вказано") == "Не вказано" and not is_fresh_newbie(u):
            continue  # ще не пройшов знайомство з ботом (крім новачків — їх кличемо на першу розмову)
        result.append(uid)
    result.sort(key=lambda x: last_seen.get(x, 0), reverse=True)
    return result


async def _meet_broadcast(recipients: list[int]):
    text, kb = meet_reminder_text() + evening_extra_text(), meet_keyboard()
    sent = 0
    for uid in recipients:
        if uid in active_chats or uid in queue:
            continue
        u = users_db.get(uid) or {}
        t = text + (NEWBIE_MEET_LINE if not u.get("total_chats") else "")
        if await safe_send(uid, t, reply_markup=kb):
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
# 📍 Де я зараз: статус чату/пошуку/кімнати і швидке повернення
# ---------------------------------------------------------------------------
def chat_mode_label(mode: str) -> str:
    if mode == "flirt":
        return "❤️ Флірт-чат"
    if mode == "voice":
        return "🎙 Голосовий чат"
    if mode.startswith("int:"):
        return f"🧩 Чат за темою {INTEREST_LABELS.get(mode[4:], mode[4:])}"
    return "🔍 Анонімний чат"


def where_text(user_id: int) -> str:
    now = time.time()
    if user_id in active_chats:
        meta = chat_meta.get(user_id) or {}
        p = users_db.get(active_chats[user_id]) or {}
        mins = int((now - meta.get("start", now)) // 60)
        return (
            f"💬 <b>Ти зараз у чаті</b>: {chat_mode_label(meta.get('mode', 'normal'))}\n"
            f"👤 Співрозмовник: {short_info(p)}\n"
            f"⏱ {mins} хв · твоїх повідомлень: {meta.get('msgs', 0)}\n\n"
            "Просто пиши — повідомлення йдуть співрозмовнику. Завершити: «❌ Завершити чат»."
        )
    if user_id in queue:
        mode = search_mode.get(user_id, "normal")
        mins = int((now - queue_since.get(user_id, now)) // 60)
        return f"⏳ <b>Йде пошук</b>: {chat_mode_label(mode)} · {mins} хв\nЩойно когось знайду — одразу з'єдную."
    if user_id in user_room:
        room = rooms.get(user_room[user_id]) or {}
        title = ROOM_TOPICS.get(room.get("topic"), "кімната")
        return f"👥 <b>Ти в кімнаті</b>: {title} · учасників: {len(room.get('members') or ())}"
    return "🙂 Ти зараз не в чаті й не в пошуку. Натисни «🔍 Шукати співрозмовника» 👇"


@dp.message(F.text == BTN_BACK_CHAT)
@dp.message(F.text == BTN_BACK_ROOM)
@dp.message(Command("where", "chat"))
async def where_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    if uid in active_chats:
        kb = get_chat_keyboard()
    elif uid in user_room:
        kb = get_room_keyboard()
    else:
        kb = get_main_keyboard()
    await message.answer(where_text(uid), reply_markup=kb)

# ---------------------------------------------------------------------------
# ✨ Розваги: гороскоп, карта дня, сумісність знаків, що нового, канал зізнань
# ---------------------------------------------------------------------------
import hashlib

ZODIAC = {k: (uk, el) for k, uk, el in [('aries', '♈ Овен', 'fire'), ('taurus', '♉ Телець', 'earth'), ('gemini', '♊ Близнюки', 'air'), ('cancer', '♋ Рак', 'water'), ('leo', '♌ Лев', 'fire'), ('virgo', '♍ Діва', 'earth'), ('libra', '♎ Терези', 'air'), ('scorpio', '♏ Скорпіон', 'water'), ('sagittarius', '♐ Стрілець', 'fire'), ('capricorn', '♑ Козоріг', 'earth'), ('aquarius', '♒ Водолій', 'air'), ('pisces', '♓ Риби', 'water')]}
H_DAY = [
    'Сьогодні вдалий день, щоб почати те, що давно відкладав(-ла).',
    'Енергії багато — направ її на одну справу, а не на десять.',
    'День спокійний: ідеальний час навести лад у думках.',
    'Хтось чекає від тебе першого кроку. Можливо, варто його зробити.',
    'Невелика несподіванка підніме настрій ближче до вечора.',
    'Не поспішай із висновками — до вечора все проясниться.',
    'Сьогодні твоя сила — у словах. Говори щиро.',
    'Гарний день для нових знайомств і цікавих розмов.',
    'Дай собі відпочити — продуктивність повернеться завтра.',
    'Маленька перемога сьогодні важливіша, ніж здається.',
]
H_LOVE = [
    'У коханні: щирий комплімент зробить більше, ніж ти думаєш.',
    'У коханні: хтось думає про тебе частіше, ніж показує.',
    'У коханні: відкрийся трохи більше — і отримаєш у відповідь тепло.',
    'У коханні: не шукай ідеального — шукай свого.',
    'У коханні: ввечері можлива розмова, яка багато змінить.',
    'У коханні: зірки радять бути сміливішим(-ою).',
    'У коханні: тиша теж буває відповіддю — дай людині час.',
    'У коханні: несподіване знайомство може стати чимось більшим.',
]
H_TIP = [
    'Порада дня: випий води й посміхнись незнайомцю 🙂',
    'Порада дня: напиши тому, за ким сумуєш.',
    'Порада дня: зроби одну справу до кінця.',
    'Порада дня: прогулянка вирішить більше, ніж ти думаєш.',
    'Порада дня: скажи «ні» тому, що забирає твою енергію.',
    'Порада дня: увімкни улюблену пісню на повну.',
    'Порада дня: похвали себе — ти багато робиш.',
]
H_COLORS = ['фіолетовий', 'золотий', 'бірюзовий', 'рожевий', 'смарагдовий', 'сріблястий', 'синій', 'бордовий']
TAROT = [
    ('🃏 Блазень', 'Новий початок і легкість. Не бійся зробити крок у невідоме.'),
    ('🪄 Маг', 'Усе потрібне вже в тебе є. Дій — і вийде.'),
    ('🌙 Верховна Жриця', 'Довірся інтуїції — вона сьогодні не підведе.'),
    ('🌸 Імператриця', 'День турботи, краси й тепла. Подаруй його собі й іншим.'),
    ('👑 Імператор', 'Час взяти ситуацію під контроль і поставити межі.'),
    ('📜 Ієрофант', 'Мудра порада прийде від того, кого ти поважаєш.'),
    ('💞 Закохані', 'Важливий вибір серцем. Слухай, що відчуваєш.'),
    ('🏇 Колісниця', 'Рух уперед і перемога, якщо тримаєш курс.'),
    ('🦁 Сила', "Твоя м'якість — це теж сила. Спокій переможе."),
    ('🏮 Відлюдник', 'Побудь трохи наодинці — відповідь прийде зсередини.'),
    ('🎡 Колесо Фортуни', 'Удача повертається до тебе обличчям. Лови момент.'),
    ('⚖️ Справедливість', 'Чесність повернеться до тебе сторицею.'),
    ('🙃 Повішений', 'Подивись на ситуацію під іншим кутом — і все зміниться.'),
    ('🦋 Зміни', 'Щось старе завершується, щоб звільнити місце новому.'),
    ('🍯 Помірність', 'Баланс і терпіння — сьогодні твої найкращі друзі.'),
    ('😈 Спокуса', 'Помітиш спокусу — запитай себе, чи справді воно тобі треба.'),
    ('⚡ Вежа', 'Несподіванка струсне рутину — і це на краще.'),
    ('⭐ Зірка', 'Надія й натхнення. Мрій сміливо — сьогодні можна.'),
    ('🌕 Місяць', 'Не все таке, яким здається. Не поспішай з висновками.'),
    ('☀️ Сонце', 'Радість, тепло й успіх. Найкраща карта дня ✨'),
    ('📯 Суд', 'Час підбити підсумки й пробачити — собі теж.'),
    ('🌍 Світ', 'Завершення й гармонія. Ти саме там, де маєш бути.'),
]
COMPAT_TEXT = {k: v[0] for k, v in {'same': ('Ви говорите однією мовою стихії — легко, тепло й зрозуміло 💜', 'Вы говорите на одном языке стихии — легко, тепло и понятно 💜'), 'good': ('Ви чудово доповнюєте одне одного: де одному бракує — інший має з надлишком ✨', 'Вы отлично дополняете друг друга: где одному не хватает — у другого с избытком ✨'), 'mid': ('Цікава пара: будуть і іскри, і суперечки — нудно точно не буде 😉', 'Интересная пара: будут и искры, и споры — скучно точно не будет 😉'), 'hard': ('Різні стихії — складніше, але якщо вдасться, це буде дуже сильна пара 🔥', 'Разные стихии — сложнее, но если получится, это будет очень сильная пара 🔥')}.items()}
HOROSCOPE_CHANNEL = os.getenv("HOROSCOPE_CHANNEL", "")  # напр. @ANONimchyk_goroskop — з'явиться кнопка
NEWS_CHANNEL = os.getenv("NEWS_CHANNEL", "")  # напр. @ANONimchyk_news


def _seed(*parts) -> int:
    return int(hashlib.md5(":".join(map(str, parts)).encode()).hexdigest(), 16)


def fun_menu_kb():
    rows = [
        [InlineKeyboardButton(text="🔮 Гороскоп на сьогодні", callback_data="fun_horo")],
        [InlineKeyboardButton(text="🃏 Карта дня", callback_data="fun_tarot"),
         InlineKeyboardButton(text="💞 Сумісність знаків", callback_data="fun_compat")],
        [InlineKeyboardButton(text="📰 Що нового", callback_data="fun_news"),
         InlineKeyboardButton(text="🤫 Канал зізнань", url=confess_channel_link())],
    ]
    if HOROSCOPE_CHANNEL:
        rows.append([InlineKeyboardButton(text="🌙 Канал «ANONimchyk Гороскоп»", url=f"https://t.me/{HOROSCOPE_CHANNEL.lstrip('@')}")])
    if NEWS_CHANNEL:
        rows.append([InlineKeyboardButton(text="📣 Канал «ANONimchyk News»", url=f"https://t.me/{NEWS_CHANNEL.lstrip('@')}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


FUN_MENU_TEXT = "✨ <b>Розваги</b>\nЧим зайнятись, поки шукаєш співрозмовника 👇\n<i>Гороскоп і карти — для настрою й розваги 😉</i>"


@dp.message(F.text == BTN_FUN)
@dp.message(Command("fun"))
async def fun_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await send_banner(message.from_user.id, "fun")
    await message.answer(FUN_MENU_TEXT, reply_markup=fun_menu_kb())


@dp.callback_query(F.data == "fun_menu")
async def fun_menu_cb(call: types.CallbackQuery):
    await call.message.answer(FUN_MENU_TEXT, reply_markup=fun_menu_kb())
    await call.answer()


def zodiac_kb(prefix: str):
    keys = list(ZODIAC)
    rows = [[InlineKeyboardButton(text=ZODIAC[k][0], callback_data=f"{prefix}{k}") for k in keys[i:i + 3]]
            for i in range(0, 12, 3)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def horoscope_text(sign: str, day) -> str:
    s = _seed("horo", sign, day.isoformat())
    return (
        f"🔮 <b>{ZODIAC[sign][0]} · {day.strftime('%d.%m')}</b>\n\n"
        f"{H_DAY[s % len(H_DAY)]}\n\n"
        f"💜 {H_LOVE[(s // 7) % len(H_LOVE)]}\n\n"
        f"✨ {H_TIP[(s // 49) % len(H_TIP)]}\n\n"
        f"🍀 Щасливе число: <b>{(s // 343) % 33 + 1}</b> · колір: <b>{H_COLORS[(s // 11) % len(H_COLORS)]}</b>"
    )


def horo_after_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 Змінити знак", callback_data="fun_horo_pick"),
         InlineKeyboardButton(text="✨ Розваги", callback_data="fun_menu")],
    ])


@dp.callback_query(F.data == "fun_horo")
async def fun_horo(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    sign = u.get("zodiac")
    await call.answer()
    if sign in ZODIAC:
        await call.message.answer(horoscope_text(sign, kyiv_today()), reply_markup=horo_after_kb())
        stat_add("horoscope")
        return
    await call.message.answer("🔮 Обери свій знак — я запам'ятаю, і далі гороскоп буде в один дотик:", reply_markup=zodiac_kb("fun_z:"))


@dp.callback_query(F.data == "fun_horo_pick")
async def fun_horo_pick(call: types.CallbackQuery):
    await call.answer()
    await call.message.answer("🔮 Обери свій знак:", reply_markup=zodiac_kb("fun_z:"))


@dp.callback_query(F.data.startswith("fun_z:"))
async def fun_zodiac_set(call: types.CallbackQuery):
    sign = call.data.split(":", 1)[1]
    if sign not in ZODIAC:
        await call.answer()
        return
    u = init_user(call.from_user.id)
    u["zodiac"] = sign
    request_save()
    await call.answer(f"Запам'ятав: {ZODIAC[sign][0]}")
    await call.message.answer(horoscope_text(sign, kyiv_today()), reply_markup=horo_after_kb())
    stat_add("horoscope")


@dp.callback_query(F.data == "fun_tarot")
async def fun_tarot(call: types.CallbackQuery):
    i = _seed("tarot", call.from_user.id, kyiv_today().isoformat()) % len(TAROT)
    name, meaning = TAROT[i]
    await call.answer()
    await call.message.answer(
        f"🃏 <b>Твоя карта дня</b>\n\n<b>{name}</b>\n{meaning}\n\n<i>Завтра — нова карта 🌙</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✨ Розваги", callback_data="fun_menu")]]),
    )
    stat_add("tarot")


def compat_kind(a: str, b: str) -> str:
    ea, eb = ZODIAC[a][1], ZODIAC[b][1]
    if ea == eb:
        return "same"
    pair = {ea, eb}
    if pair in ({"fire", "air"}, {"earth", "water"}):
        return "good"
    if pair in ({"fire", "water"}, {"earth", "air"}):
        return "hard"
    return "mid"


def compat_percent(a: str, b: str) -> int:
    base = {"same": 88, "good": 80, "mid": 64, "hard": 50}[compat_kind(a, b)]
    return min(99, base + _seed("zc", *sorted((a, b))) % 10)


@dp.callback_query(F.data == "fun_compat")
async def fun_compat(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    await call.answer()
    if u.get("zodiac") in ZODIAC:
        await call.message.answer(f"💞 Твій знак: {ZODIAC[u['zodiac']][0]}. А який знак у нього / неї?", reply_markup=zodiac_kb("fun_c2:"))
    else:
        await call.message.answer("💞 Спершу обери свій знак:", reply_markup=zodiac_kb("fun_c1:"))


@dp.callback_query(F.data.startswith("fun_c1:"))
async def fun_compat_mine(call: types.CallbackQuery):
    sign = call.data.split(":", 1)[1]
    if sign not in ZODIAC:
        await call.answer()
        return
    u = init_user(call.from_user.id)
    u["zodiac"] = sign
    request_save()
    await call.answer()
    await call.message.answer(f"💞 Твій знак: {ZODIAC[sign][0]}. А який знак у нього / неї?", reply_markup=zodiac_kb("fun_c2:"))


@dp.callback_query(F.data.startswith("fun_c2:"))
async def fun_compat_result(call: types.CallbackQuery):
    other = call.data.split(":", 1)[1]
    u = init_user(call.from_user.id)
    mine = u.get("zodiac")
    if other not in ZODIAC or mine not in ZODIAC:
        await call.answer()
        return
    p = compat_percent(mine, other)
    bar = "💜" * round(p / 20) + "🤍" * (5 - round(p / 20))
    await call.answer()
    await call.message.answer(
        f"💞 <b>{ZODIAC[mine][0]} + {ZODIAC[other][0]}</b>\n\n<b>{p}%</b>  {bar}\n{COMPAT_TEXT[compat_kind(mine, other)]}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Інший знак", callback_data="fun_compat"),
             InlineKeyboardButton(text="✨ Розваги", callback_data="fun_menu")],
        ]),
    )
    stat_add("zodiac_compat")


NEWS_TEXT = (
    "📰 <b>Що нового в ANONimchyk</b>\n\n"
    "🎭 <b>Зняти маску</b> — за взаємною згодою покажіть одне одному нік, вік і фото\n"
    "🎙 <b>Маска голосу</b> — 12 голосів у голосових: робот, привид, демон… (/voice)\n"
    "🎙 <b>Голосовий чат</b> — спілкування лише голосом, і голос завжди змінений\n"
    "🎲 <b>Ігри в чаті</b> — «Правда чи дія», «Що обереш?», «2 правди і 1 брехня»\n"
    "💌 <b>Нічний лист</b> — напиши незнайомцю, а вранці отримай чийсь\n"
    "🤫 <b>Анонімні зізнання</b> — і канал, де їх публікують\n"
    "💯 <b>Сумісність</b> після кожної розмови\n"
    "✨ <b>Розваги</b> — гороскоп, карта дня, сумісність знаків\n"
    "👑 <b>Пак засновника</b> — стікери для перших учасників"
)


@dp.callback_query(F.data == "fun_news")
async def fun_news(call: types.CallbackQuery):
    await call.answer()
    await call.message.answer(NEWS_TEXT, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✨ Розваги", callback_data="fun_menu")]]))


# ---------------------------------------------------------------------------
# 🎙 Голосовий чат: окремий пошук, де спілкуються лише голосовими з маскою
# ---------------------------------------------------------------------------
def voice_chat_note(u: dict) -> str:
    return (
        "🎙 <b>Голосовий чат</b>\n"
        "Тут спілкуються лише голосовими — затисни 🎤 і говори.\n"
        f"Твій голос змінює маска: <b>{voice_mask_label(u, for_voice_chat=True)}</b> — співрозмовник не почує справжнього. "
        "Змінити: /voice"
    )


@dp.message(F.text == BTN_VOICE_SEARCH)
@dp.message(Command("voicechat"))
async def voice_search_start(message: types.Message, state: FSMContext):
    if message.from_user.id not in active_chats and message.from_user.id not in queue:
        await send_banner(message.from_user.id, "voicechat")
    await run_search(message, state, "voice")


# ---------------------------------------------------------------------------
# 🎲 Ігри в чаті: Правда чи дія · Що обереш? · 2 правди і 1 брехня
# ---------------------------------------------------------------------------
TRUTHS = [
    "Яка пісня зараз найчастіше грає в тебе в навушниках?",
    "Що тебе найбільше смішить у собі?",
    "Яку дурну звичку ти ніяк не можеш кинути?",
    "Про що ти мрієш, але ще нікому не казав(-ла)?",
    "Який найсміливіший вчинок ти колись робив(-ла)?",
    "Що б ти змінив(-ла) у своєму дні, якби міг(-ла)?",
    "Чого ти боїшся, хоча розумієш, що це смішно?",
    "Яке твоє найдивніше хобі або захоплення?",
    "Яку книгу, фільм чи серіал ти можеш переглядати безкінечно?",
    "Коли ти востаннє плакав(-ла) від сміху і чому?",
    "Яку суперсилу ти б обрав(-ла)?",
    "Що тебе може миттєво підняти настрій?",
    "Що ти вмієш, чого не вміють інші?",
    "Яка найкраща порада, яку тобі давали?",
    "Яке місце в Україні ти хочеш побачити найбільше?",
    "Що б ти зробив(-ла), якби виграв(-ла) мільйон?",
    "Яка твоя перша думка зранку?",
    "За чим ти найбільше сумуєш із дитинства?",
    "Що в людях тебе чіпляє найбільше?",
    "Про що ти міг(-ла) би говорити годинами?",
]
DARES = [
    "Опиши свій настрій трьома емоджі 🙂",
    "Напиши вірш із двох рядків про цей чат.",
    "Надішли голосове з маскою голосу 🎙 і скажи «привіт» найсмішнішим тоном.",
    "Надішли стікер, який найкраще тебе описує.",
    "Розкажи анекдот — навіть якщо він поганий 😄",
    "Придумай співрозмовнику смішне прізвисько.",
    "Опиши свій ідеальний вечір одним реченням.",
    "Напиши комплімент співрозмовнику — щиро 💜",
    "Напиши назву фільму емоджі — хай співрозмовник вгадає.",
    "Склади речення, де всі слова починаються на одну літеру.",
    "Розкажи, що зараз бачиш за вікном, як диктор новин.",
    "Признайся, що ти їв(-ла) сьогодні — і оціни це від 1 до 10.",
    "Напиши, яку пісню ти б заспівав(-ла) в караоке прямо зараз.",
    "Наступні 3 повідомлення пиши лише з великої літери КОЖНЕ СЛОВО.",
    "Вигадай назву для вашої анонімної розмови, як для серіалу 🎬",
]
WOULD_RATHER = [
    ("🌊 Море", "⛰ Гори"),
    ("🌅 Світанок", "🌙 Ніч"),
    ("☕ Кава", "🍵 Чай"),
    ("🐶 Собака", "🐱 Кіт"),
    ("📚 Книга", "🎬 Фільм"),
    ("🍕 Піца", "🍣 Суші"),
    ("❄️ Зима", "☀️ Літо"),
    ("🏙 Місто", "🌾 Село"),
    ("🎧 Навушники", "🔊 Колонка"),
    ("✈️ Подорож", "🏠 Затишок вдома"),
    ("🎤 Співати", "💃 Танцювати"),
    ("🧭 Минуле", "🚀 Майбутнє"),
    ("📞 Подзвонити", "💬 Написати"),
    ("🎢 Атракціони", "🧩 Настолки"),
    ("🍫 Солодке", "🧂 Солоне"),
]


def game_menu_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔥 Правда чи дія", callback_data="gm_td")],
            [InlineKeyboardButton(text="🤔 Що обереш?", callback_data="gm_wyr")],
            [InlineKeyboardButton(text="🕵️ 2 правди і 1 брехня", callback_data="gm_2t1l")],
        ]
    )


def _game_pair(user_id: int) -> tuple[int, frozenset] | None:
    partner = active_chats.get(user_id)
    if partner is None:
        return None
    return partner, frozenset((user_id, partner))


@dp.message(F.text == BTN_GAME)
@dp.message(Command("game"))
async def game_open(message: types.Message, state: FSMContext):
    await state.clear()
    if _game_pair(message.from_user.id) is None:
        await message.answer("🎲 Ігри доступні під час анонімного чату — знайди співрозмовника 🙂", reply_markup=get_main_keyboard())
        return
    await send_banner(message.from_user.id, "games")
    await message.answer("🎲 <b>Зіграємо?</b> Обери гру — вона з'явиться в обох:", reply_markup=game_menu_kb())


async def _both(user_id: int, partner: int, text: str, kb=None):
    await safe_send(user_id, text, reply_markup=kb)
    await safe_send(partner, text, reply_markup=kb)


def td_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="😇 Правда", callback_data="td_t"),
            InlineKeyboardButton(text="😈 Дія", callback_data="td_d"),
        ]]
    )


@dp.callback_query(F.data == "gm_td")
async def game_td(call: types.CallbackQuery):
    pair = _game_pair(call.from_user.id)
    if pair is None:
        await call.answer("Чат уже завершено.", show_alert=True)
        return
    partner, _ = pair
    await call.answer()
    await _both(call.from_user.id, partner,
                "🔥 <b>Правда чи дія</b>\nХто першим натисне — той і відповідає. Потім по черзі 😉", td_kb())
    stat_add("games")


@dp.callback_query(F.data.in_({"td_t", "td_d"}))
async def game_td_pick(call: types.CallbackQuery):
    pair = _game_pair(call.from_user.id)
    if pair is None:
        await call.answer("Чат уже завершено.", show_alert=True)
        return
    partner, key = pair
    st = game_state.setdefault(key, {})
    used = st.setdefault("td_used", set())
    truth = call.data == "td_t"
    spooky = halloween_active() and random.random() < 0.5  # 🎃 на Хелловін — половина завдань страшні
    pool = (SPOOKY_TRUTHS if truth else SPOOKY_DARES) if spooky else (TRUTHS if truth else DARES)
    kind = ("s" if spooky else "") + ("t" if truth else "d")
    choices = [i for i in range(len(pool)) if (kind, i) not in used] or list(range(len(pool)))
    i = random.choice(choices)
    used.add((kind, i))
    await call.answer()
    head = "😇 <b>Правда</b>" if truth else "😈 <b>Дія</b>"
    await safe_send(call.from_user.id, f"{head} — твоє завдання:\n\n{pool[i]}", reply_markup=td_kb())
    tok = GENDER_FOR.set(gender_of(call.from_user.id) or "?")
    try:
        await safe_send(partner, f"{head} — завдання для співрозмовника:\n\n{pool[i]}\n\n<i>Тепер твоя черга 👇</i>", reply_markup=td_kb())
    finally:
        GENDER_FOR.reset(tok)


def wyr_kb(i: int):
    a, b = WOULD_RATHER[i]
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=a, callback_data=f"wyr:{i}:0"),
            InlineKeyboardButton(text=b, callback_data=f"wyr:{i}:1"),
        ]]
    )


async def _wyr_new(user_id: int, partner: int, key: frozenset):
    st = game_state.setdefault(key, {})
    used = st.setdefault("wyr_used", set())
    choices = [i for i in range(len(WOULD_RATHER)) if i not in used] or list(range(len(WOULD_RATHER)))
    i = random.choice(choices)
    used.add(i)
    st["wyr"] = {"i": i, "votes": {}}
    await _both(user_id, partner, "🤔 <b>Що обереш?</b>\nОбирайте — відповіді відкриються, коли проголосуєте обоє 🤫", wyr_kb(i))


@dp.callback_query(F.data == "gm_wyr")
async def game_wyr(call: types.CallbackQuery):
    pair = _game_pair(call.from_user.id)
    if pair is None:
        await call.answer("Чат уже завершено.", show_alert=True)
        return
    await call.answer()
    await _wyr_new(call.from_user.id, pair[0], pair[1])
    stat_add("games")


@dp.callback_query(F.data.startswith("wyr:"))
async def game_wyr_vote(call: types.CallbackQuery):
    pair = _game_pair(call.from_user.id)
    if pair is None:
        await call.answer("Чат уже завершено.", show_alert=True)
        return
    partner, key = pair
    try:
        _, si, sc = call.data.split(":")
        i, c = int(si), int(sc)
    except ValueError:
        await call.answer()
        return
    cur = (game_state.get(key) or {}).get("wyr")
    if not cur or cur["i"] != i:
        await call.answer("Це питання вже закрите 🙂")
        return
    if call.from_user.id in cur["votes"]:
        await call.answer("Ти вже обрав(-ла) — чекаємо співрозмовника ⏳")
        return
    cur["votes"][call.from_user.id] = c
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    if len(cur["votes"]) < 2:
        await call.answer(f"Ти обрав(-ла): {WOULD_RATHER[i][c]} — чекаємо співрозмовника ⏳")
        return
    await call.answer()
    mine, theirs = cur["votes"][call.from_user.id], cur["votes"].get(partner, -1)
    same = mine == theirs
    verdict = "💜 <b>Ви обрали однаково!</b>" if same else "🙃 <b>Ви обрали різне</b> — є про що посперечатися 😄"
    more = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➡️ Ще питання", callback_data="gm_wyr")]])
    for me, other in ((call.from_user.id, partner), (partner, call.from_user.id)):
        await safe_send(
            me,
            f"{verdict}\nТи: {WOULD_RATHER[i][cur['votes'].get(me, 0)]} · Співрозмовник: {WOULD_RATHER[i][cur['votes'].get(other, 0)]}",
            reply_markup=more,
        )


@dp.callback_query(F.data == "gm_2t1l")
async def game_2t1l(call: types.CallbackQuery):
    pair = _game_pair(call.from_user.id)
    if pair is None:
        await call.answer("Чат уже завершено.", show_alert=True)
        return
    await call.answer()
    await safe_send(
        call.from_user.id,
        "🕵️ <b>2 правди і 1 брехня</b>\nТи починаєш: напиши 3 факти про себе — два правдиві, один вигаданий. "
        "Співрозмовник має вгадати брехню. Потім мінятесь 😉\n<i>Без імен, адрес і контактів 🙂</i>",
    )
    await safe_send(
        pair[0],
        "🕵️ <b>2 правди і 1 брехня</b>\nСпіврозмовник зараз напише 3 факти про себе — один із них брехня. "
        "Вгадай, який! Потім твоя черга 😉",
    )
    stat_add("games")


# ---------------------------------------------------------------------------
# 💯 Сумісність після чату
# ---------------------------------------------------------------------------
def compat_text(u: dict, p: dict, duration: float, min_msgs: int) -> str:
    score = 35
    reasons = []
    common = sorted((u.get("interests") or set()) & (p.get("interests") or set()))
    if common:
        score += min(30, 15 * len(common))
        labels = ", ".join(INTEREST_LABELS.get(k, k) for k in common[:3])
        reasons.append(f"🧩 спільне: {labels}")
    mu, mp = current_mood(u), current_mood(p)
    if mu and mu == mp:
        score += 12
        reasons.append(f"🎭 однаковий настрій: {MOODS[mu]}")
    a, b = get_age_int(u), get_age_int(p)
    if a is not None and b is not None and abs(a - b) <= 3:
        score += 8
        reasons.append("🎂 майже однолітки")
    if duration >= 600 or min_msgs >= 15:
        score += 14
        reasons.append("💬 довга розмова — вам було цікаво")
    elif duration >= 180 or min_msgs >= 6:
        score += 7
        reasons.append("💬 жива розмова")
    score += random.randint(0, 4)
    score = max(40, min(99, score))
    bar = "💜" * round(score / 20) + "🤍" * (5 - round(score / 20))
    lines = [f"💯 <b>Ваша сумісність: {score}%</b>  {bar}"]
    lines += [f"• {r}" for r in reasons]
    if score >= 80:
        lines.append("\nСхоже, ви на одній хвилі ✨ Додай у друзі, щоб не загубитись 🤝")
    return "\n".join(lines)




# === КІНЕЦЬ part5.py ===
