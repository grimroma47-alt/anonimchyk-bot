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

    welcome = (
        f"Привіт, {esc(message.from_user.first_name)}! Вітаємо в анонімному чаті! 🤫\n\n"
        f"Твій унікальний номер: <b>{u['custom_id']}</b>\n"
        "Змінити профіль можна будь-коли: ⚙️ Налаштування → ✏️ Мій профіль."
    )
    if not await send_banner(user_id, "start", caption=welcome, reply_markup=get_main_keyboard()):
        await message.answer(welcome, reply_markup=get_main_keyboard())
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
        f"• <b>Серія входів:</b> {u.get('checkin_streak', 0)} 🔥\n"
        f"• <b>Рейтинг:</b> {rating_line(u)}\n"
        f"• <b>Інтереси:</b> {interests_line(u)}\n\n"
        f"🏆 <b>Досягнення:</b> {achievements_text}\n\n"
        f"🎒 <b>Інвентар подарунків:</b> {gifts_text}\n\n"
        f"📜 <b>Архів останніх змін профілю:</b>\n{archive_text}\n\n"
        "Хочеш оновити дані? Введи /edit_profile"
    )
    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🧩 Мої інтереси", callback_data="pi_open")]]
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
    if mono_jar_link():
        buttons.append(
            [InlineKeyboardButton(text="🫙 Оплатити через Банку monobank", callback_data=f"topup_jar_{amount}")]
        )
    if PAY_CARD or PAY_IBAN:
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




# === КІНЕЦЬ part2.py ===
