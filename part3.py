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
    record_spend("lottery", LOTTERY_COST)

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
    for rid, r in rooms.items():
        if r["topic"] == topic_key and len(r["members"]) < ROOM_CAPACITY:
            target_room_id = rid
            break
    if target_room_id is None:
        room_counter += 1
        target_room_id = f"room_{room_counter}"
        rooms[target_room_id] = {"topic": topic_key, "members": set()}

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
        await send_match_safety(user_id, u)
        await send_match_safety(partner_id, p)
    else:
        if has_perk(u, "priority") or is_premium(u):
            queue.insert(0, user_id)
        else:
            queue.append(user_id)
        search_mode[user_id] = mode
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
        await message.answer(wait_text)
        await maybe_show_ad(user_id)
        schedule_wait_ping(user_id, mode)


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
                target_id, f"{header}\n{esc(message.text)}", reply_markup=kb, protect_content=protect
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
        f"🆕 Нових: <b>{t['new']}</b>",
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
    extra = [f"💬 чатів: {u.get('total_chats', 0)}"]
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
    kb.append([InlineKeyboardButton(text="⬅️ До статистики", callback_data=f"adm_st_{period}")])
    try:
        await call.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), disable_web_page_preview=True)
    except TelegramAPIError:
        pass
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
    ok = await safe_send(target_id, f"📩 <b>Відповідь від підтримки:</b>\n{esc(message.text)}")
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
    return f"📣 <i>Реклама</i>\n\n{esc(ad['text'])}"


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




# === КІНЕЦЬ part3.py ===
