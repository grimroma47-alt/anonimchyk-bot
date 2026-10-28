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
            "🌐 Обери мову / Выбери язык / Choose your language:",
            reply_markup=lang_pick_kb("lang_new_"),
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


def en_available() -> bool:
    return LANG_RU_ENABLED and LANG_EN_ENABLED and bool(EN_PAIRS)


def lang_pick_kb(prefix: str):
    row = [
        InlineKeyboardButton(text=LANG_LABELS["uk"], callback_data=f"{prefix}uk"),
        InlineKeyboardButton(text=LANG_LABELS["ru"], callback_data=f"{prefix}ru"),
    ]
    rows = [row]
    if en_available():
        rows.append([InlineKeyboardButton(text=LANG_LABELS["en"], callback_data=f"{prefix}en")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def lang_from_cb(data: str) -> str:
    code = data.rsplit("_", 1)[-1]
    if code == "en" and not en_available():
        return "uk"
    return code if code in LANG_LABELS else "uk"


@dp.callback_query(F.data.in_({"lang_new_uk", "lang_new_ru", "lang_new_en"}))
async def lang_new_pick(call: types.CallbackQuery, state: FSMContext):
    u = init_user(call.from_user.id)
    u["lang"] = lang_from_cb(call.data)
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
        "🌐 Обери мову / Выбери язык / Choose your language:",
        reply_markup=lang_pick_kb("lang_set_"),
    )
    await call.answer()


@dp.callback_query(F.data.in_({"lang_set_uk", "lang_set_ru", "lang_set_en"}))
async def lang_set(call: types.CallbackQuery):
    uid = call.from_user.id
    u = init_user(uid)
    new = lang_from_cb(call.data)
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
    done = {"uk": "✅ Мову змінено: українська.", "ru": "✅ Язык изменён: русский.", "en": "✅ Language changed: English."}
    await safe_send(uid, protect_text(done.get(new, done["uk"])), reply_markup=get_main_keyboard())
    if new == "en":
        await safe_send(uid, EN_WELCOME_NOTE)


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
# ☰ Ще: решта розділів у другому меню
# ---------------------------------------------------------------------------
@dp.message(F.text == BTN_MORE)
async def more_menu(message: types.Message, state: FSMContext):
    await state.clear()
    init_user(message.from_user.id)
    await message.answer("☰ <b>Усі розділи</b> — обирай 👇", reply_markup=get_more_keyboard())


@dp.message(F.text == BTN_MENU_BACK)
async def more_back(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("🏠 Головне меню", reply_markup=get_main_keyboard())


# ---------------------------------------------------------------------------
# Профіль та архів
# ---------------------------------------------------------------------------
@dp.message(F.text.in_({BTN_PROFILE, BTN_PROFILE_OLD}))
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
        f"• <b>Настрій:</b> {mood_line(u)}\n"
        + (f"• <b>Значок:</b> 🎃 Хелловін 2026\n" if u.get("hw2026") else "")
        + (f"• <b>Гарбузи:</b> 🎃 {u.get('hw_pumpkins', 0)}/{HW_PUMPKINS_NEED}\n" if halloween_active() else "")
        + "\n"
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
@dp.message(F.text.in_({BTN_FILTERS, BTN_FILTERS_OLD}))
@dp.message(Command("filters"))
async def filters_command(message: types.Message, state: FSMContext):
    await state.clear()
    init_user(message.from_user.id)
    await message.answer(filters_text(message.from_user.id), reply_markup=get_filters_menu_keyboard(users_db[message.from_user.id]))


async def refresh_filters(call: types.CallbackQuery, note: str | None = None):
    uid = call.from_user.id
    u = init_user(uid)
    request_save()
    try:
        await call.message.edit_text(filters_text(uid), reply_markup=get_filters_menu_keyboard(u))
    except TelegramAPIError:
        pass  # той самий вміст — Telegram не дає «змінити» на ідентичне
    await call.answer(note or "Збережено ✅")


@dp.callback_query(F.data == "filters_menu")
async def filters_menu_cb(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    u = init_user(call.from_user.id)
    try:
        await call.message.edit_text(filters_text(call.from_user.id), reply_markup=get_filters_menu_keyboard(u))
    except TelegramAPIError:
        await call.message.answer(filters_text(call.from_user.id), reply_markup=get_filters_menu_keyboard(u))
    await call.answer()


@dp.callback_query(F.data.in_({"f_info_gender", "f_info_age", "f_info_country"}))
async def filters_info(call: types.CallbackQuery):
    tips = {
        "f_info_gender": "👫 Обери, кого шукати: хлопців, дівчат чи всіх. Доступно з 💎 Premium.",
        "f_info_age": "🎂 Можна обрати кілька діапазонів віку. Доступно з 💎 Premium.",
        "f_info_country": "🌍 Країна — безкоштовно. Обери прапор або «✏️ Інша країна».",
    }
    await call.answer(tips[call.data], show_alert=True)


@dp.callback_query(F.data == "f_locked")
async def filters_locked(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    price = SHOP["gender_filter"][1]
    await call.answer()
    await call.message.answer(
        "💎 <b>Фільтр за статтю й віком</b> — у Premium.\n\n"
        "З ним бот з'єднуватиме лише з тими, кого ти обереш. Або купи фільтр за статтю окремо на тиждень.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Premium", callback_data="prem_open")],
            [InlineKeyboardButton(text=f"🎯 Фільтр на тиждень — {price} грн", callback_data="buy_gender_filter")],
        ]),
    )


@dp.callback_query(F.data == "f_gender_menu")
@dp.callback_query(F.data == "f_age_menu")
async def f_old_submenu(call: types.CallbackQuery):
    await refresh_filters(call, " ")


@dp.callback_query(F.data.startswith("fg_"))
async def fg_set(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    if not filter_active(u, "gender_filter"):
        await filters_locked(call)
        return
    value = call.data[len("fg_"):]
    u["filter_gender"] = value if value in ("Хлопець", "Дівчина") else None
    await refresh_filters(call)


@dp.callback_query(F.data.startswith("fa_"))
async def fa_toggle(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    if not filter_active(u, "age_filter"):
        await filters_locked(call)
        return
    value = call.data[len("fa_"):]
    allowed = {r[0] for r in age_ranges_for(u)}
    u["filter_age_ranges"] = set(u.get("filter_age_ranges") or set())
    if value == "reset":
        u["filter_age_ranges"] = set()
    elif value not in allowed:
        await call.answer("Цей діапазон недоступний", show_alert=True)
        return
    elif value in u["filter_age_ranges"]:
        u["filter_age_ranges"].discard(value)
    else:
        u["filter_age_ranges"].add(value)
    await refresh_filters(call)


@dp.callback_query(F.data.startswith("fc_"))
async def fc_set(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    value = call.data[len("fc_"):]
    if value == "any":
        u["filter_country"] = None
    elif value.isdigit() and int(value) < len(FILTER_COUNTRIES):
        name = FILTER_COUNTRIES[int(value)][1]
        u["filter_country"] = None if u.get("filter_country") == name else name
    await refresh_filters(call)


@dp.callback_query(F.data == "f_country_menu")
async def f_country_menu(call: types.CallbackQuery, state: FSMContext):
    await call.message.answer(
        "🌍 Напиши країну, з якої шукати співрозмовника (наприклад: Франція, США, Канада).\n"
        "Щоб скинути — напиши «скинути». Скасувати: /cancel"
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
    else:
        u["filter_country"] = normalize_country(text)[:50]
    request_save()
    await message.answer(filters_text(message.from_user.id), reply_markup=get_filters_menu_keyboard(u))


@dp.callback_query(F.data == "f_reset")
async def f_reset(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    u["filter_gender"] = None
    u["filter_age_ranges"] = set()
    u["filter_country"] = None
    await refresh_filters(call, "Скинуто ✅")


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
@dp.message(F.text.in_({BTN_DAILY, BTN_DAILY_OLD}))
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
        cheap_keys = [k for k, (_, p) in GIFT_CATALOG.items() if p <= DAILY_GIFT_MAX_PRICE and k not in LIMITED_GIFTS]
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
        + earn_wallet_line(u)
        + f"• <b>Нік:</b> {esc(u['nickname'])}"
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
    if perk in FREE_FILTERS:
        await call.answer("Цей фільтр тепер безкоштовний — налаштуй у «🎯 Фільтри» 🙂", show_alert=True)
        return
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
    if key in ("gender_filter", "age_filter"):
        await call.message.answer(filters_text(call.from_user.id), reply_markup=get_filters_menu_keyboard(u))
    await call.answer()


@dp.callback_query(F.data.startswith("buygift_"))
async def buy_gift_item(call: types.CallbackQuery):
    """Купівля подарунка про запас (в інвентар), без прив'язки до співрозмовника."""
    key = call.data[len("buygift_"):]
    item = GIFT_CATALOG.get(key)
    if item is None:
        await call.answer("Невідомий подарунок", show_alert=True)
        return
    if key in LIMITED_GIFTS and not halloween_active():
        await call.answer("🎃 Цей подарунок був лімітованим — продаж завершено", show_alert=True)
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
    ua_only = user_lang(user_id) == "uk"  # Банка й переказ на картку — лише для українців (гривня)
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


@dp.message(F.text.in_({BTN_TOPUP, BTN_TOPUP_OLD}))
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
        f"Запрошено: {done}. До наступного Premium: {left}.",
        reply_markup=contest_keyboard(call.from_user.id),
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
@dp.message(F.text.startswith(BTN_ROOMS) | (F.text == BTN_ROOMS_OLD))
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

    await send_banner(user_id, "rooms")
    await message.answer(rooms_intro_text(user_id), reply_markup=get_room_topics_keyboard())


@dp.callback_query(F.data.startswith("room_join_"))
async def room_join(call: types.CallbackQuery):
    user_id = call.from_user.id

    if user_id in banned_users:
        await call.answer("⛔ Вас заблоковано в цьому боті.", show_alert=True)
        return
    if user_id in user_room:
        await call.answer("Ти вже в кімнаті. Спочатку вийди з неї.", show_alert=True)
        return
    if user_id in active_chats:
        await call.answer("Спочатку заверши приватний чат.", show_alert=True)
        return

    topic_key = call.data[len("room_join_"):]
    if topic_key not in room_topics_now():
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

    room.setdefault("last_msg", time.time())
    stat_add("room_join")
    searching = user_id in queue
    await call.message.answer(
        f"✅ Ти приєднався до кімнати: <b>{esc(room_topics_now().get(topic_key, topic_key))}</b> "
        f"({len(room['members'])}/{ROOM_CAPACITY} 👥)\n\n"
        "Просто пиши текстом — повідомлення побачать усі учасники кімнати."
        + ("\n\n🔔 Пошук співрозмовника триває — щойно когось знайду, покличу тебе в приватний чат." if searching else ""),
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


@dp.message(F.text.in_({BTN_FLIRT, BTN_FLIRT_OLD}))
@dp.message(Command("flirt"))
async def flirt_search(message: types.Message, state: FSMContext):
    u = init_user(message.from_user.id)
    reason = flirt_block_reason(u)
    if reason and message.from_user.id not in banned_users:
        await state.clear()
        await message.answer(reason)
        return
    await send_banner(message.from_user.id, "flirt")
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
        if now_ts - queue_since.get(candidate_id, now_ts) > QUEUE_STALE_AFTER and candidate_id not in user_room:
            stale.append(candidate_id)  # хто в кімнаті — точно поруч, його не перепитуємо
            continue
        score = match_score(u, p_candidate, mode)
        if best is None or score > best[0]:
            best = (score, i)
            if score >= MATCH_SCORE_MAX:
                break
    match_index = best[1] if best else None

    if match_index is not None:
        partner_id = queue.pop(match_index)
        await pull_from_room(partner_id)
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
        elif mode == "voice":
            await message.answer(voice_chat_note(u))
            await safe_send(partner_id, voice_chat_note(p))
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
        bg_notified.pop(user_id, None)
        for sid in stale:
            await ask_still_here(sid)
        if mode == "flirt":
            looking_for = "дівчину" if u.get("gender") == "Хлопець" else "хлопця"
            wait_text = f"❤️ Шукаємо {looking_for} для флірту... Зачекай ⏳"
        elif mode == "voice":
            wait_text = (
                "🎙 Шукаємо співрозмовника для голосового чату... Зачекай ⏳\n"
                f"Твій голос змінить маска: <b>{voice_mask_label(u, for_voice_chat=True)}</b> (змінити: /voice)"
            )
        elif mode.startswith("int:"):
            wait_text = (
                f"🧩 Шукаємо співрозмовника за темою {INTEREST_LABELS.get(mode[4:], mode[4:])}... Зачекай ⏳\n"
                f"Якщо довго нікого немає — натисни «{BTN_STOP}» і спробуй звичайний пошук."
            )
        else:
            wait_text = "Шукаємо співрозмовника... Зачекай ⏳"
            if is_newbie(u):
                wait_text += NEWBIE_WAIT_LINE
            else:
                schedule_filter_nudge(user_id)
        if not current_mood(u) and mode != "flirt":
            wait_text += "\n\n🎭 Поки чекаєш — вкажи свій настрій: /mood. Бот спершу шукатиме когось на одній хвилі."
        wait_text += "\n\n🔔 Можеш закрити бот — щойно хтось знайдеться, я напишу."
        await message.answer(wait_text, reply_markup=get_main_keyboard())
        await offer_rooms_while_waiting(user_id, mode)
        await maybe_show_ad(user_id)
        schedule_stuck_offer(user_id, mode)
        schedule_wait_ping(user_id, mode)
        schedule_admin_wait_alert(user_id, mode)


@dp.message(F.text == BTN_STOP)
@dp.message(F.text == BTN_SEARCHING)
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




# === КІНЕЦЬ part2.py ===
