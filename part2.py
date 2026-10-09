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
    in_rooms = len(user_room)
    await message.answer(
        "👥 <b>Зараз у боті</b>\n\n"
        f"🟢 Онлайн: <b>{online}</b>\n"
        f"💬 Спілкуються в чатах: <b>{in_chats}</b>\n"
        f"🔍 Шукають співрозмовника: <b>{searching}</b>\n"
        f"🏠 У кімнатах: <b>{in_rooms}</b>\n\n"
        f"<i>Онлайн — ті, хто був активний за останні {ONLINE_WINDOW // 60} хв.</i>"
    )


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
    await state.clear()
    user_id = message.from_user.id

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
        await maybe_show_ad(user_id)


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
    # functools.wraps — щоб aiogram бачив справжні параметри функції (call, state тощо)
    # і не передавав у неї зайвих аргументів.
    @functools.wraps(func)
    async def wrapper(call: types.CallbackQuery, *args, **kwargs):
        if call.from_user.id != ADMIN_ID or call.from_user.id not in authorized_admins:
            await call.answer("Доступ заборонено", show_alert=True)
            return
        return await func(call, *args, **kwargs)

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
        f"• Активних кімнат: {len(rooms)} (учасників: {sum(len(r['members']) for r in rooms.values())})\n"
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


# === КІНЕЦЬ part2.py ===
