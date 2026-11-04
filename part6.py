# ---------------------------------------------------------------------------
# 🤫 Анонімні зізнання: людина пише історію → модерація адміна → публікація в канал
# ---------------------------------------------------------------------------
CONFESS_CHANNEL = os.getenv("CONFESS_CHANNEL", "@ANONimchyk_zizn")
CONFESS_MIN, CONFESS_MAX = 30, 1500
CONFESS_COOLDOWN = int(os.getenv("CONFESS_COOLDOWN", str(3 * 3600)))  # одне зізнання на 3 години
PHONE_REGEX = re.compile(r"(\+?\d[\d\s\-()]{8,}\d)")


def confess_channel_link(msg_id: int | None = None) -> str:
    name = CONFESS_CHANNEL.lstrip("@")
    return f"https://t.me/{name}" + (f"/{msg_id}" if msg_id else "")


def confess_cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Скасувати", callback_data="conf_cancel")]])


@dp.message(F.text == BTN_CONFESS)
@dp.message(Command("confess"))
async def confess_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if user_id in banned_users:
        return
    if user_id in active_chats:
        await message.answer("🤫 Спершу заверши чат — а тоді розкажеш своє зізнання 🙂")
        return
    last = (confess_db.get("last_by") or {}).get(str(user_id), 0)
    wait = int(last + CONFESS_COOLDOWN - time.time())
    if wait > 0 and not _is_admin(user_id):
        await message.answer(f"🤫 Наступне зізнання можна надіслати через {wait // 3600} год {wait % 3600 // 60} хв 💜")
        return
    await state.set_state(ConfessStates.text)
    await send_banner(user_id, "confess")
    await message.answer(
        "🤫 <b>Анонімне зізнання</b>\n\n"
        "Напиши те, що не можеш сказати нікому. Ми опублікуємо це в каналі "
        f"<a href=\"{confess_channel_link()}\">Анонімні зізнання</a> — без імені, ніку й будь-яких даних про тебе.\n\n"
        "• без імен і деталей, за якими можна впізнати людей, — ні тебе, ні інших "
        "(адреси, телефони, @ніки, школа чи робота);\n"
        f"• від {CONFESS_MIN} до {CONFESS_MAX} символів;\n"
        "• перед публікацією зізнання перевіряє модератор.\n\n"
        "Напиши своє зізнання одним повідомленням 👇",
        reply_markup=confess_cancel_kb(),
        disable_web_page_preview=True,
    )


@dp.message(ConfessStates.text)
async def confess_got_text(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("🤫 Зізнання приймаємо лише текстом — напиши його одним повідомленням 🙂", reply_markup=confess_cancel_kb())
        return
    if len(text) < CONFESS_MIN:
        await message.answer(f"Трохи коротко 🙂 Напиши хоча б {CONFESS_MIN} символів.", reply_markup=confess_cancel_kb())
        return
    if len(text) > CONFESS_MAX:
        await message.answer(f"Задовго — максимум {CONFESS_MAX} символів (зараз {len(text)}). Скороти, будь ласка.", reply_markup=confess_cancel_kb())
        return
    if LINK_REGEX.search(text) or PHONE_REGEX.search(text):
        await message.answer("🚫 Без посилань, @ніків і телефонів — так ніхто не дізнається, хто ти 💜 Перепиши, будь ласка.", reply_markup=confess_cancel_kb())
        return
    await state.update_data(confess_text=text)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Надіслати на перевірку", callback_data="conf_send")],
            [InlineKeyboardButton(text="✏️ Переписати", callback_data="conf_again"), InlineKeyboardButton(text="❌ Скасувати", callback_data="conf_cancel")],
        ]
    )
    await message.answer(
        "Ось як це виглядатиме в каналі:\n\n"
        f"<blockquote>{esc(text)}</blockquote>\n\n"
        "Публікуємо анонімно? Натискаючи «Надіслати», ти погоджуєшся на публікацію цього тексту в каналі та в озвучених відео ANONimchyk у соцмережах.",
        reply_markup=kb,
    )


@dp.callback_query(F.data == "conf_cancel")
async def confess_cancel(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await call.message.answer("Скасовано. Повертайся, коли захочеш 💜", reply_markup=get_main_keyboard())
    await call.answer()


@dp.callback_query(F.data == "conf_again")
async def confess_again(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(ConfessStates.text)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await call.message.answer("✏️ Добре, напиши нову версію одним повідомленням 👇", reply_markup=confess_cancel_kb())
    await call.answer()


def confess_admin_kb(cid: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Опублікувати", callback_data=f"conf_ok:{cid}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"conf_no:{cid}"),
        ]]
    )


@dp.callback_query(F.data == "conf_send")
async def confess_send(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    text = data.get("confess_text")
    await state.clear()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    if not text:
        await call.answer("Зізнання не знайдено, спробуй ще раз.", show_alert=True)
        return
    user_id = call.from_user.id
    cid = confess_db.get("next_id", 1)
    confess_db["next_id"] = cid + 1
    confess_db.setdefault("items", {})[str(cid)] = {"uid": user_id, "text": text, "at": time.time(), "status": "pending"}
    confess_db.setdefault("last_by", {})[str(user_id)] = time.time()
    request_save()
    stat_add("confess")
    u = init_user(user_id)
    if ADMIN_ID:
        await safe_send(
            ADMIN_ID,
            f"🤫 <b>Нове зізнання #{cid}</b> (від {esc(u.get('custom_id', ''))})\n\n<blockquote>{esc(text)}</blockquote>",
            reply_markup=confess_admin_kb(cid),
        )
    await call.message.answer(
        "📨 Надіслано на перевірку! Щойно зізнання з'явиться в каналі — я тобі повідомлю 💜",
        reply_markup=get_main_keyboard(),
    )
    await call.answer()


def confess_post_text(no: int, text: str) -> str:
    # Посилання в тексті, а не кнопкою: під постами з кнопками Telegram не показує «Коментувати»
    link = f"https://t.me/{BOT_USERNAME or 'ANONimchykbot'}?start=zizn"
    return f"🤫 <b>Зізнання #{no}</b>\n\n{esc(text)}\n\n<i>Розкажи своє — анонімно 👉</i> <a href=\"{link}\">@{BOT_USERNAME or 'ANONimchykbot'}</a>"


def confess_post_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🤫 Зізнатись анонімно", url=f"https://t.me/{BOT_USERNAME or 'ANONimchykbot'}?start=zizn")]]
    )


@dp.callback_query(F.data.startswith("conf_ok:"))
async def confess_approve(call: types.CallbackQuery):
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    cid = call.data.split(":", 1)[1]
    item = (confess_db.get("items") or {}).get(cid)
    if not item or item.get("status") != "pending":
        await call.answer("Вже оброблено.", show_alert=True)
        return
    no = confess_db.get("pub_no", 0) + 1
    try:
        msg = await bot.send_message(CONFESS_CHANNEL, confess_post_text(no, item["text"]), disable_web_page_preview=True)
    except TelegramAPIError as e:
        await call.answer(f"Не вдалося опублікувати: {e}"[:190], show_alert=True)
        return
    confess_db["pub_no"] = no
    item.update(status="published", no=no, msg_id=getattr(msg, "message_id", None))
    request_save()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await safe_send(
        item["uid"],
        f"✅ Твоє зізнання опубліковано як <b>#{no}</b>: {confess_channel_link(item.get('msg_id'))}\n\nДякуємо за довіру 💜",
    )
    await call.answer(f"Опубліковано #{no}")


@dp.callback_query(F.data.startswith("conf_no:"))
async def confess_reject(call: types.CallbackQuery):
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    cid = call.data.split(":", 1)[1]
    item = (confess_db.get("items") or {}).get(cid)
    if not item or item.get("status") != "pending":
        await call.answer("Вже оброблено.", show_alert=True)
        return
    item["status"] = "rejected"
    item.pop("text", None)  # відхилене не зберігаємо
    request_save()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await safe_send(
        item["uid"],
        "🤫 Цього разу зізнання не пройшло модерацію — можливо, в ньому були чиїсь дані або воно порушує правила. "
        "Можеш написати нове 💜",
    )
    await call.answer("Відхилено")

# ---------------------------------------------------------------------------
# 🎭 Зняти маску: за взаємною згодою показати одне одному нік, вік і фото профілю;
# @username — окремою кнопкою, тільки якщо людина сама захоче. Вмикається в UNMASK_AT.
# ---------------------------------------------------------------------------
def _parse_unmask_at() -> datetime:
    raw = os.getenv("UNMASK_AT", "2026-10-23T20:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        dt = datetime(2026, 10, 23, 20, 0)
    return dt if dt.tzinfo else dt.replace(tzinfo=KYIV_TZ)


UNMASK_AT = _parse_unmask_at()
UNMASK_MIN_MSGS = int(os.getenv("UNMASK_MIN_MSGS", "5"))  # скільки повідомлень має написати КОЖЕН
FOUNDER_PACK_URL = os.getenv("FOUNDER_PACK_URL", "")  # посилання на стікерпак «Пак засновника»
unmask_requests: dict[int, int] = {}  # кому надіслано запит -> хто просить
unmask_done: set[frozenset] = set()  # пари, які вже зняли маски в цьому чаті


def unmask_live() -> bool:
    return datetime.now(KYIV_TZ) >= UNMASK_AT


def unmask_wait_text() -> str:
    left = int((UNMASK_AT - datetime.now(KYIV_TZ)).total_seconds())
    days, rem = divmod(max(left, 0), 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    parts = []
    if days:
        parts.append(f"{days} {_plural(days, 'день', 'дні', 'днів')}")
    if hours:
        parts.append(f"{hours} год")
    if not days:
        parts.append(f"{mins} хв")
    return (
        "🎭 <b>Зняти маску</b> відкриється в <b>п'ятницю, 23 жовтня о 20:00</b>.\n"
        f"⏳ Залишилось: {' '.join(parts)}\n\n"
        "Тоді за взаємною згодою ви зможете показати одне одному нік, вік і фото — "
        "а контакт тільки якщо самі захочете 💜"
    )


def unmask_pair_blocked(u: dict, p: dict) -> str | None:
    """Причина, чому пара не може зняти маски (None — можна)."""
    a, b = get_age_int(u), get_age_int(p)
    if a is None or b is None:
        return "🎭 Щоб зняти маску, у вас обох має бути вказаний вік у профілі."
    if (a < 18) != (b < 18):
        return "🎭 Зняти маску можна лише з однолітками — так безпечніше для всіх 💜"
    return None


def unmask_card(u: dict) -> str:
    badge = " 👑" if u.get("founder_no") else ""
    g = u.get("gender", "")
    gico = "👨" if g == "Хлопець" else "👩" if g == "Дівчина" else "🙂"
    age = u.get("age", "")
    country = u.get("country", "")
    lines = [f"{gico} <b>{esc(u.get('nickname', ''))}</b>{badge}"]
    info = ", ".join(x for x in (f"{age} р." if str(age).isdigit() else "", country if country and country != "Не вказано" else "") if x)
    if info:
        lines.append(info)
    ml = mood_line(u)
    if ml:
        lines.append(ml)
    return "\n".join(lines)


def unmask_share_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📨 Поділитися моїм @username", callback_data="unmask_share")]]
    )


@dp.message(F.text == BTN_UNMASK)
@dp.message(Command("unmask"))
async def unmask_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if not unmask_live() and not _is_admin(user_id):
        await message.answer(unmask_wait_text())
        return
    partner_id = active_chats.get(user_id)
    if partner_id is None:
        await message.answer(
            "🎭 Зняти маску можна лише під час анонімного чату — знайди співрозмовника 🙂",
            reply_markup=get_main_keyboard(),
        )
        return
    if frozenset((user_id, partner_id)) in unmask_done:
        await message.answer("🎭 Ви вже зняли маски в цьому чаті 💜")
        return
    if unmask_requests.get(partner_id) == user_id:
        await message.answer("⏳ Запит уже надіслано — чекаємо відповіді співрозмовника.")
        return
    u, p = init_user(user_id), init_user(partner_id)
    reason = unmask_pair_blocked(u, p)
    if reason:
        await message.answer(reason)
        return
    mine = (chat_meta.get(user_id) or {}).get("msgs", 0)
    theirs = (chat_meta.get(partner_id) or {}).get("msgs", 0)
    if min(mine, theirs) < UNMASK_MIN_MSGS:
        await message.answer(
            "🎭 Спершу трохи поспілкуйтесь — маску знімають, коли вже є про що говорити 🙂\n"
            f"(кожен має написати хоча б {UNMASK_MIN_MSGS} повідомлень)"
        )
        return
    if unmask_requests.get(user_id) == partner_id:  # співрозмовник уже попросив — одразу знімаємо
        unmask_requests.pop(user_id, None)
        await unmask_reveal(user_id, partner_id)
        return
    unmask_requests[partner_id] = user_id
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="🎭 Так, знімаємо", callback_data="unmask_yes"),
            InlineKeyboardButton(text="🙈 Ще ні", callback_data="unmask_no"),
        ]]
    )
    ok = await safe_send(
        partner_id,
        "🎭 <b>Співрозмовник пропонує зняти маски.</b>\n\n"
        "Ви обоє побачите нік, вік і фото профілю одне одного. "
        "Контакт (@username) — тільки якщо кожен сам захоче ним поділитися.\n\nЗнімаємо?",
        reply_markup=kb,
    )
    if ok:
        await message.answer("🎭 Запит надіслано. Маски знімуться, тільки якщо співрозмовник погодиться 💜")
    else:
        unmask_requests.pop(partner_id, None)
        await message.answer("❌ Не вдалося надіслати запит.")


async def _send_unmask_card(to_id: int, about_id: int):
    about = init_user(about_id)
    text = "🎭 <b>Маски знято!</b> Ось хто з тобою говорив:\n\n" + unmask_card(about)
    photo_id = None
    try:
        photos = await bot.get_user_profile_photos(about_id, limit=1)
        if photos and photos.total_count and photos.photos:
            photo_id = photos.photos[0][-1].file_id
    except Exception as e:  # noqa: BLE001
        logging.info("Фото профілю %s: %s", about_id, e)
    if photo_id:
        try:
            await bot.send_photo(to_id, photo_id, caption=text, protect_content=True)
            return
        except TelegramAPIError:
            pass
    await safe_send(to_id, text + "\n\n<i>(фото профілю приховане)</i>")


async def unmask_reveal(a: int, b: int):
    if active_chats.get(a) != b:
        return
    unmask_done.add(frozenset((a, b)))
    stat_add("unmask")
    await send_banner(a, "unmask")
    await send_banner(b, "unmask")
    await _send_unmask_card(a, b)
    await _send_unmask_card(b, a)
    note = (
        "Хочеш продовжити спілкування поза ботом? Поділись контактом — тільки якщо сам цього хочеш. "
        "А можна й далі говорити тут 💜"
    )
    await safe_send(a, note, reply_markup=unmask_share_kb())
    await safe_send(b, note, reply_markup=unmask_share_kb())


@dp.callback_query(F.data == "unmask_yes")
async def unmask_yes(call: types.CallbackQuery):
    me = call.from_user.id
    requester = unmask_requests.pop(me, None)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    if requester is None or active_chats.get(me) != requester:
        await call.answer("Запит уже неактуальний.", show_alert=True)
        return
    await call.answer("🎭")
    await unmask_reveal(me, requester)


@dp.callback_query(F.data == "unmask_no")
async def unmask_no(call: types.CallbackQuery):
    me = call.from_user.id
    requester = unmask_requests.pop(me, None)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    if requester is not None:
        await safe_send(requester, "🎭 Співрозмовник поки не готовий зняти маску. Можливо, пізніше 🙂")
    await call.answer("Добре, маски лишаються 🙂")


@dp.callback_query(F.data == "unmask_share")
async def unmask_share(call: types.CallbackQuery):
    me = call.from_user.id
    partner = active_chats.get(me)
    if partner is None or frozenset((me, partner)) not in unmask_done:
        await call.answer("Чат уже завершено.", show_alert=True)
        return
    username = call.from_user.username
    if not username:
        await call.answer(
            "У тебе немає @username у Telegram. Додай його в Налаштуваннях Telegram і натисни ще раз.",
            show_alert=True,
        )
        return
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    u = init_user(me)
    await safe_send(partner, f"📨 <b>{esc(u['nickname'])}</b> ділиться контактом: @{esc(username)}")
    await call.answer("Контакт надіслано 💜")


def launch_text() -> str:
    lines = [
        "🎭 <b>Сьогодні о 20:00 ANONimchyk знімає маски!</b>",
        "",
        "Тепер у кожному чаті є кнопка «🎭 Зняти маску»:",
        "• поспілкуйтесь трохи — і запропонуйте зняти маски;",
        "• тільки якщо погодитесь обоє, ви побачите нік, вік і фото одне одного;",
        "• контакт — лише якщо сам захочеш ним поділитися.",
        "",
        "🎙 Нове: «Маска голосу» — надсилай голосові, а співрозмовник почує змінений голос. Увімкнути: /voice",
        "🤫 І канал анонімних зізнань: @ANONimchyk_zizn",
        "✨ А ще «Розваги»: гороскоп, карта дня і сумісність знаків — /fun",
        "",
        "💸 Тепер подарунки — це не тільки приємно: частина вартості кожного подарунка, "
        "який тобі дарують, стає твоїми грошима. Вивід на карту від 200 \u2060грн 💜",
        "",
        "💬 А ще в чатах: відповідь свайпом, виправлення повідомлень і кнопка «🎲 Інша тема». "
        "Додай пару слів «Про себе» в профілі — співрозмовник побачить їх на старті.",
        "",
        "⏰ А о 21:00 — Година знайомств: найбільше людей онлайн. Саме час 💜",
    ]
    return "\n".join(lines)


def founder_pack_text(no: int) -> str:
    return (
        f"👑 <b>Засновнику №{no} — обіцяний подарунок!</b>\n\n"
        "Твій ексклюзивний 🎁 <b>Пак засновника</b> — анімовані стікери ANONimchyk, "
        "які ніколи не продаватимуться:\n"
        f"{FOUNDER_PACK_URL}\n\nДякуємо, що ти з нами з самого початку 💜"
    )


def launch_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔍 Знайти співрозмовника", callback_data="wp_go:normal")]]
    )


async def launch_broadcast():
    sent = 0
    for uid, u in list(users_db.items()):
        if uid <= 0 or uid in banned_users:
            continue
        if await safe_send(uid, launch_text(), reply_markup=launch_keyboard()):
            sent += 1
            if FOUNDER_PACK_URL and u.get("founder_no"):
                await safe_send(uid, founder_pack_text(u["founder_no"]))
        await asyncio.sleep(0.05)
    launch_state["sent"] = sent
    request_save()
    if ADMIN_ID:
        await safe_send(ADMIN_ID, f"🎭 Розсилку про запуск отримали: <b>{sent}</b>")


async def launch_tick():
    """Раз на хвилину: рівно в UNMASK_AT — одна розсилка про запуск (і Пак засновника)."""
    if not _contest_can_write() or launch_state.get("done") or not unmask_live():
        return
    if (datetime.now(KYIV_TZ) - UNMASK_AT).total_seconds() > 6 * 3600:
        launch_state["done"] = True  # бот прокинувся надто пізно — не спамимо вночі
        request_save()
        return
    launch_state["done"] = True  # спершу позначаємо — щоб після перезапуску не надіслати двічі
    request_save()
    await launch_broadcast()


@dp.message(Command("launchtest"))
async def launch_test(message: types.Message):
    """Адмін: подивитись текст розсилки й Пак засновника (надсилається лише тобі)."""
    if not _is_admin(message.from_user.id):
        return
    await message.answer(launch_text(), reply_markup=launch_keyboard())
    if FOUNDER_PACK_URL:
        await message.answer(founder_pack_text(1))
    else:
        await message.answer("⚠️ FOUNDER_PACK_URL не задано в Render — Пак засновника не надішлеться.")
    await message.answer(f"Запуск: {UNMASK_AT.strftime('%d.%m %H:%M')} · розсилка вже була: {launch_state.get('done')}")

# ---------------------------------------------------------------------------
# 💸 Вивід грошей на карту: частина вартості подарунків, які тобі дарують, від WITHDRAW_MIN грн.
# Зараховуються лише подарунки після запуску оновлення і лише в межах реальних поповнень дарувальника
# (безкоштовні бонуси не перетворюються на гроші). Гроші «відлежуються» EARN_HOLD_DAYS днів,
# виплату робить адмін вручну. Номер карти не зберігається в базі — лише останні 4 цифри.
# ---------------------------------------------------------------------------
WITHDRAW_MIN_AGE = 18


def uah(x: float, digits: int = 2) -> str:
    """Сума виводу завжди в гривнях (не в ⭐) — позначка \u2060 вимикає заміну на зірки."""
    return f"{x:.{digits}f} \u2060грн"


def paid_pool(u: dict) -> float:
    """Скільки реальних поповнень людини ще може «перетворитись» на заробіток отримувачів подарунків."""
    if "paid_pool" not in u:
        u["paid_pool"] = float(u.get("topup_total") or 0.0)
    return u["paid_pool"]


def earn_from_gift(sender: dict, recipient: dict, price: float) -> float:
    if time.time() < UNMASK_AT.timestamp():
        return 0.0  # рахуємо лише з моменту запуску оновлення
    base = min(float(price), paid_pool(sender))
    if base <= 0:
        return 0.0
    sender["paid_pool"] = round(sender["paid_pool"] - base, 2)
    amt = round(base * GIFT_RECIPIENT_SHARE, 2)
    if amt <= 0:
        return 0.0
    recipient.setdefault("earn_pending", []).append([time.time(), amt])
    recipient["earned_total"] = round(recipient.get("earned_total", 0.0) + amt, 2)
    return amt


def earn_mature(u: dict) -> None:
    """Переносить гроші, що вже «відлежались», у доступні до виводу."""
    pend = u.get("earn_pending") or []
    if not pend:
        return
    edge = time.time() - EARN_HOLD_DAYS * 86400
    ready = sum(a for t, a in pend if t <= edge)
    if ready:
        u["earn_pending"] = [[t, a] for t, a in pend if t > edge]
        u["earn_ready"] = round(u.get("earn_ready", 0.0) + ready, 2)


def earn_on_hold(u: dict) -> float:
    return round(sum(a for _t, a in (u.get("earn_pending") or [])), 2)


def withdrawable(u: dict) -> float:
    earn_mature(u)
    return max(0.0, round(min(u.get("earn_ready", 0.0), u.get("balance", 0.0)), 2))


def earn_wallet_line(u: dict) -> str:
    if not u.get("earned_total"):
        return ""
    line = f"• <b>💸 Зароблено:</b> {uah(u['earned_total'])} · до виводу: <b>{uah(withdrawable(u))}</b>\n"
    hold = earn_on_hold(u)
    if hold:
        line += f"  <i>ще {uah(hold)} стануть доступні протягом {EARN_HOLD_DAYS} днів</i>\n"
    return line


def wd_pending_of(user_id: int):
    for no, w in withdraw_db.get("items", {}).items():
        if w["user_id"] == user_id and w["status"] == "pending":
            return no, w
    return None, None


def luhn_ok(num: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(num)):
        d = int(ch)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def wd_cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Скасувати", callback_data="wd_cancel")]])


@dp.callback_query(F.data == "wd_open")
async def wd_open(call: types.CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    u = init_user(user_id)
    await call.answer()
    if user_id in banned_users:
        return
    if user_id in active_chats:
        await call.message.answer("💬 Спершу заверши чат — а тоді оформимо вивід 🙂")
        return
    age = get_age_int(u)
    if age is None or age < WITHDRAW_MIN_AGE:
        await call.message.answer(
            f"💸 Вивід на карту доступний з {WITHDRAW_MIN_AGE} років.\n"
            "Гроші з подарунків лишаються на балансі — їх можна витрачати в боті 💜"
        )
        return
    no, w = wd_pending_of(user_id)
    if w:
        await call.message.answer(f"⏳ Заявка №{no} на {uah(w['amount'])} уже в обробці. Щойно виплатимо — бот напише.")
        return
    avail = withdrawable(u)
    hold = earn_on_hold(u)
    if avail < WITHDRAW_MIN:
        text = (
            "💸 <b>Вивід на карту</b>\n\n"
            "Коли тобі дарують подарунки в чаті, частина їхньої вартості стає твоїми грошима. "
            f"Вони стають доступні через {EARN_HOLD_DAYS} днів, а вивести можна від <b>{uah(WITHDRAW_MIN, 0)}</b>.\n\n"
            f"Доступно зараз: <b>{uah(avail)}</b>"
        )
        if hold:
            text += f"\nНезабаром стане доступно: {uah(hold)}"
        await call.message.answer(text)
        return
    await state.set_state(WithdrawStates.amount)
    await state.update_data(wd_avail=avail)
    await call.message.answer(
        f"💸 Доступно до виводу: <b>{uah(avail)}</b>\n\n"
        f"Напиши суму від {WITHDRAW_MIN:.0f} до {uah(avail)} або натисни кнопку 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Вивести все — {uah(avail)}", callback_data="wd_all")],
            [InlineKeyboardButton(text="✖️ Скасувати", callback_data="wd_cancel")],
        ]),
    )


async def _wd_ask_card(target: types.Message, state: FSMContext, amount: float):
    await state.update_data(wd_amount=amount)
    await state.set_state(WithdrawStates.card)
    await target.answer(
        f"💳 Сума: <b>{uah(amount)}</b>\n\n"
        "Надішли номер картки українського банку (16 цифр).\n"
        "<i>Бот одразу видалить повідомлення з номером із чату, а в базі збереже лише останні 4 цифри.</i>",
        reply_markup=wd_cancel_kb(),
    )


@dp.callback_query(F.data == "wd_all")
async def wd_all(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    if await state.get_state() != WithdrawStates.amount.state:
        return
    avail = withdrawable(init_user(call.from_user.id))
    if avail < WITHDRAW_MIN:
        await state.clear()
        await call.message.answer(f"Доступно лише {uah(avail)} — мінімум для виводу {uah(WITHDRAW_MIN, 0)}.")
        return
    await _wd_ask_card(call.message, state, avail)


@dp.callback_query(F.data == "wd_cancel")
async def wd_cancel(call: types.CallbackQuery, state: FSMContext):
    if (await state.get_state() or "").startswith("WithdrawStates"):
        await state.clear()
    await call.answer("Скасовано")
    await call.message.answer("Вивід скасовано.", reply_markup=get_main_keyboard())


@dp.message(WithdrawStates.amount)
async def wd_amount(message: types.Message, state: FSMContext):
    avail = withdrawable(init_user(message.from_user.id))
    try:
        amount = round(float((message.text or "").strip().replace(",", ".").replace("грн", "")), 2)
    except ValueError:
        await message.answer(f"Напиши лише число — наприклад, {WITHDRAW_MIN:.0f}.", reply_markup=wd_cancel_kb())
        return
    if amount < WITHDRAW_MIN or amount > avail:
        await message.answer(
            f"Сума має бути від {WITHDRAW_MIN:.0f} до {uah(avail)}.", reply_markup=wd_cancel_kb()
        )
        return
    await _wd_ask_card(message, state, amount)


@dp.message(WithdrawStates.card)
async def wd_card(message: types.Message, state: FSMContext):
    digits = re.sub(r"[\s-]", "", message.text or "")
    try:
        await message.delete()  # номер карти не лишаємо в чаті
    except TelegramAPIError:
        pass
    if not (digits.isdigit() and len(digits) == 16 and luhn_ok(digits)):
        await message.answer("⚠️ Схоже, номер з помилкою. Перевір 16 цифр і надішли ще раз.", reply_markup=wd_cancel_kb())
        return
    data = await state.get_data()
    amount = float(data.get("wd_amount") or 0)
    await state.update_data(wd_card=digits)
    await message.answer(
        f"Перевір, будь ласка:\n\n💸 Сума: <b>{uah(amount)}</b>\n💳 Картка: <b>•••• {digits[-4:]}</b>\n\n"
        "Виплата зазвичай надходить протягом 1–3 робочих днів.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Підтвердити", callback_data="wd_confirm")],
            [InlineKeyboardButton(text="✖️ Скасувати", callback_data="wd_cancel")],
        ]),
    )


@dp.callback_query(F.data == "wd_confirm")
async def wd_confirm(call: types.CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    data = await state.get_data()
    card, amount = data.get("wd_card"), float(data.get("wd_amount") or 0)
    await state.clear()
    if not card or amount <= 0:
        await call.answer("Заявка застаріла — почни ще раз з гаманця.", show_alert=True)
        return
    u = init_user(user_id)
    if wd_pending_of(user_id)[1] or amount < WITHDRAW_MIN or amount > withdrawable(u):
        await call.answer("Сума вже недоступна — відкрий гаманець ще раз.", show_alert=True)
        return
    await call.answer()
    u["balance"] = round(u["balance"] - amount, 2)
    u["earn_ready"] = round(u.get("earn_ready", 0.0) - amount, 2)
    no = str(withdraw_db.get("next", 1))
    withdraw_db["next"] = int(no) + 1
    w = {"user_id": user_id, "amount": amount, "last4": card[-4:], "created": time.time(), "status": "pending"}
    withdraw_db.setdefault("items", {})[no] = w
    request_save()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await call.message.answer(
        f"✅ Заявку №{no} на <b>{uah(amount)}</b> прийнято. Щойно гроші надійдуть на картку •••• {card[-4:]}, бот напише 💜",
        reply_markup=get_main_keyboard(),
    )
    if ADMIN_ID:
        try:
            sent = await bot.send_message(
                ADMIN_ID,
                f"💸 <b>Заявка на виплату №{no}</b>\n\n"
                f"👤 {admin_label(user_id)}\n"
                f"🎂 Вік: {esc(u.get('age'))} · зароблено всього: {uah(u.get('earned_total', 0))}\n"
                f"💰 Сума: <b>{uah(amount)}</b>\n"
                f"💳 Картка: <code>{card}</code>\n\n"
                "<i>Після переказу натисни «Виплачено» — номер картки зникне з цього повідомлення.</i>",
                protect_content=True,
                reply_markup=wd_admin_kb(no),
            )
            w["admin_msg"] = sent.message_id
        except TelegramAPIError as e:
            logging.warning("Заявка на виплату: не вдалося повідомити адміна: %s", e)


def wd_admin_kb(no: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Виплачено", callback_data=f"wd_paid:{no}"),
        InlineKeyboardButton(text="❌ Відхилити", callback_data=f"wd_rej:{no}"),
    ]])


@dp.callback_query(F.data.startswith("wd_paid:") | F.data.startswith("wd_rej:"))
async def wd_admin_decide(call: types.CallbackQuery):
    if not _is_admin(call.from_user.id):
        await call.answer("Доступ заборонено", show_alert=True)
        return
    action, no = call.data.split(":", 1)
    w = withdraw_db.get("items", {}).get(no)
    if w is None or w["status"] != "pending":
        await call.answer("Заявку вже оброблено.", show_alert=True)
        return
    paid = action == "wd_paid"
    w["status"] = "paid" if paid else "rejected"
    w["done"] = time.time()
    u = init_user(w["user_id"])
    if paid:
        u["withdrawn_total"] = round(u.get("withdrawn_total", 0.0) + w["amount"], 2)
        stat_add("withdrawn", w["amount"])
        await safe_send(w["user_id"], f"✅ Виплату <b>{uah(w['amount'])}</b> надіслано на картку •••• {w['last4']}. Дякуємо, що ти з нами 💜")
    else:
        u["balance"] = round(u["balance"] + w["amount"], 2)
        u["earn_ready"] = round(u.get("earn_ready", 0.0) + w["amount"], 2)
        await safe_send(
            w["user_id"],
            f"❌ Заявку №{no} на виплату відхилено — {uah(w['amount'])} повернуто на баланс.\n"
            "Якщо є питання — напиши в 🆘 Допомога.",
        )
    request_save()
    try:
        await call.message.edit_text(
            f"💸 Заявка №{no}: {uah(w['amount'])} · картка •••• {w['last4']}\n"
            f"👤 {admin_label(w['user_id'])}\n\n" + ("✅ Виплачено" if paid else "❌ Відхилено, гроші повернуто"),
            reply_markup=None,
        )
    except TelegramAPIError:
        pass
    await call.answer("Готово")


@dp.callback_query(F.data == "adm_wd")
@admin_only
async def adm_withdrawals(call: types.CallbackQuery):
    items = withdraw_db.get("items", {})
    pending = [(no, w) for no, w in items.items() if w["status"] == "pending"]
    paid_sum = sum(w["amount"] for w in items.values() if w["status"] == "paid")
    lines = [f"💸 <b>Виводи</b>\n\nВиплачено всього: {uah(paid_sum)}", f"На перевірці: {len(pending)}\n"]
    for no, w in pending[:20]:
        lines.append(
            f"№{no} · {uah(w['amount'])} · •••• {w['last4']} · {admin_label(w['user_id'])}"
            + ("" if w.get("admin_msg") else " · ⚠️ повідомлення з карткою не дійшло")
        )
    if pending:
        lines.append("\n<i>Повний номер картки — у повідомленні заявки вище в чаті.</i>")
    await call.message.answer("\n".join(lines))
    await call.answer()


# ---------------------------------------------------------------------------
# Адмін: топ балансів і обнулення лічильника рулетки
# ---------------------------------------------------------------------------
@dp.callback_query(F.data == "adm_topbal")
@admin_only
async def adm_top_balances(call: types.CallbackQuery):
    people = [(uid, u) for uid, u in users_db.items() if uid != ADMIN_ID]
    top = sorted(people, key=lambda x: -x[1].get("balance", 0))[:15]
    total = sum(u.get("balance", 0) for _uid, u in people)
    ready = sum(withdrawable(u) for _uid, u in people)
    lines = [
        "💰 <b>Топ балансів</b>\n",
        f"На балансах усього: <b>{total:.2f} грн</b>",
        f"З них можна вивести на карту: <b>{ready:.2f} грн</b>\n",
    ]
    for i, (uid, u) in enumerate(top, 1):
        if u.get("balance", 0) <= 0:
            break
        extra = f" · зароблено {u['earned_total']:.0f}" if u.get("earned_total") else ""
        lines.append(f"{i}. {admin_label(uid)} — <b>{u['balance']:.2f} грн</b>{extra}")
    await call.message.answer("\n".join(lines))
    await call.answer()


def roul_reset_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Так, обнулити", callback_data="adm_roul_reset_yes"),
        InlineKeyboardButton(text="✖️ Ні", callback_data="adm_roul_reset_no"),
    ]])


@dp.callback_query(F.data == "adm_roul_reset")
@admin_only
async def adm_roulette_reset(call: types.CallbackQuery):
    await call.message.answer(
        f"🎰 Дохід з рулетки зараз: {lottery_revenue_total:.0f} грн.\nОбнулити лічильник (напр. після тестів)?",
        reply_markup=roul_reset_kb(),
    )
    await call.answer()


@dp.callback_query(F.data.in_({"adm_roul_reset_yes", "adm_roul_reset_no"}))
@admin_only
async def adm_roulette_reset_do(call: types.CallbackQuery):
    global lottery_revenue_total
    if call.data.endswith("_yes"):
        lottery_revenue_total = 0.0
        request_save()
        text = "✅ Лічильник рулетки обнулено."
    else:
        text = "Залишено як є."
    try:
        await call.message.edit_text(text, reply_markup=None)
    except TelegramAPIError:
        pass
    await call.answer()


BOT_STARTED_AT = time.time()


@dp.message(Command("version"))
async def version_cmd(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    up = int(time.time() - BOT_STARTED_AT)
    blocked = sum(1 for u in users_db.values() if u.get("blocked_bot"))
    await message.answer(
        f"🤖 Версія бота: <b>{BOT_VERSION}</b>\n⏱ Працює без перезапуску: {up // 3600} год {up % 3600 // 60} хв\n"
        f"{calls_status()}\n🚫 Заблокували бота: {blocked}"
    )


@dp.message(Command("resetroulette"))
async def reset_roulette_cmd(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    await message.answer(
        f"🎰 Дохід з рулетки зараз: {lottery_revenue_total:.0f} грн.\nОбнулити лічильник (напр. після тестів)?",
        reply_markup=roul_reset_kb(),
    )


# ---------------------------------------------------------------------------
# 👥 Живі кімнати: велика кнопка з лічильником, «поки шукаємо — зазирни в кімнату»,
# пошук у фоні (до 2 год) і теми від бота, коли в кімнаті тихо
# ---------------------------------------------------------------------------
BG_SEARCH_MAX = int(os.getenv("BG_SEARCH_MAX", str(2 * 3600)))  # скільки триває пошук у фоні від старту
ROOM_QUIET_TOPIC = int(os.getenv("ROOM_QUIET_TOPIC", "300"))  # через скільки секунд тиші бот підкидає тему
bg_notified: dict[int, float] = {}  # кому вже сказали, що пошук іде у фоні

ROOM_QUESTIONS = [
    "Яка найдивніша річ сталася з вами цього тижня?",
    "Що вас точно підбадьорить, коли все йде не так?",
    "Яку пісню ви б поставили на повтор прямо зараз?",
    "Яке місце в Україні (чи у світі) варто побачити кожному?",
    "Що ви вмієте, чого ніхто не очікує?",
    "Яка ваша найкраща порада тому, кому зараз важко?",
    "Котики, собаки чи ні те ні інше? Аргументуйте 😄",
    "Який фільм ви переглянули б удруге просто зараз?",
    "Що б ви зробили з вільним днем без телефона?",
    "Яка ваша суперсила в компанії друзів?",
    "Чай чи кава — і в який час доби?",
    "Яку звичку ви хочете позбутися, а яку завести?",
]


HW_ROOM = {"spooky": "🎃 Страшилки"}  # тимчасова кімната на Хелловін


def room_topics_now() -> dict:
    return {**HW_ROOM, **ROOM_TOPICS} if halloween_active() else ROOM_TOPICS


def rooms_online() -> int:
    return sum(len(r["members"]) for r in rooms.values())


def rooms_button_label() -> str:
    n = rooms_online()
    return f"{BTN_ROOMS} · {n}" if n >= 2 else BTN_ROOMS


def rooms_intro_text(user_id: int) -> str:
    text = (
        "👥 <b>Кімнати за інтересами</b>\n\n"
        f"Обери тему — потрапиш у групу до {ROOM_CAPACITY} людей, які говорять про те саме. "
        "У кімнатах поки підтримується лише текст — це для безпеки спілкування в групі."
    )
    if user_id in queue:
        text += "\n\n🔔 Пошук співрозмовника не зупиниться — щойно когось знайду, покличу тебе."
    return text


def rooms_open_kb():
    n = rooms_online()
    label = f"👥 Зайти в кімнату · там {n}" if n else "👥 Зайти в кімнату"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data="rooms_open")]])


async def offer_rooms_while_waiting(user_id: int, mode: str):
    """Поки черга порожня — пропонуємо кімнату, де вже хтось є (пошук триває)."""
    if mode == "voice" or user_id in user_room or rooms_online() < 1:
        return
    await safe_send(
        user_id,
        f"👥 Поки шукаємо — зазирни в кімнату: там зараз <b>{rooms_online()}</b>. Пошук при цьому не зупиниться 🔔",
        reply_markup=rooms_open_kb(),
    )


@dp.callback_query(F.data == "rooms_open")
async def rooms_open_cb(call: types.CallbackQuery):
    user_id = call.from_user.id
    await call.answer()
    if user_id in banned_users:
        return
    if user_id in active_chats:
        await call.message.answer("Спочатку заверши приватний чат.", reply_markup=get_chat_keyboard())
        return
    if user_id in user_room:
        await call.message.answer("Ти вже в кімнаті 🙂", reply_markup=get_room_keyboard())
        return
    await call.message.answer(rooms_intro_text(user_id), reply_markup=get_room_topics_keyboard())


async def pull_from_room(user_id: int, quiet: bool = False):
    """Знайшовся співрозмовник — людина виходить з кімнати в приватний чат (інших попереджаємо)."""
    room_id = user_room.get(user_id)
    if room_id is None:
        return
    room = rooms.get(room_id)
    remove_from_room(user_id)
    if quiet or not room:
        return
    nick = esc((users_db.get(user_id) or {}).get("nickname", "Хтось"))
    for member_id in list(room["members"]):
        await safe_send(member_id, f"🚪 <b>{nick}</b> пішов у приватний чат.")


def bg_search_text() -> str:
    return (
        "🔔 Поки нікого немає — я шукаю <b>у фоні</b> ще до двох годин.\n\n"
        "Займайся своїми справами: щойно хтось з'явиться, я напишу, і ви одразу почнете чат."
        + (f"\n\n👥 А поки можна поговорити в кімнаті — там зараз {rooms_online()}." if rooms_online() else "")
    )


def bg_search_kb():
    rows = []
    if rooms_online():
        rows.append([InlineKeyboardButton(text="👥 Зайти в кімнату", callback_data="rooms_open")])
    rows.append([InlineKeyboardButton(text="⏹ Зупинити пошук", callback_data="bg_stop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "bg_stop")
async def bg_stop(call: types.CallbackQuery):
    user_id = call.from_user.id
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    if user_id in queue:
        queue.remove(user_id)
        bg_notified.pop(user_id, None)
        await call.answer("Пошук зупинено")
        await call.message.answer("Пошук зупинено.", reply_markup=get_main_keyboard() if user_id not in user_room else get_room_keyboard())
    else:
        await call.answer("Пошук уже не активний")


async def rooms_tick():
    """Раз на хвилину: якщо в кімнаті з 2+ людьми 5 хв тиша — бот підкидає питання для всіх."""
    now = time.time()
    for room in list(rooms.values()):
        members = list(room["members"])
        if len(members) < 2:
            continue
        last = max(room.get("last_msg", now), room.get("last_topic", 0))
        if now - last < ROOM_QUIET_TOPIC:
            continue
        room["last_topic"] = now
        q = random.choice([x for x in ROOM_QUESTIONS if x != room.get("last_q")])
        room["last_q"] = q
        for member_id in members:
            await safe_send(member_id, f"🎲 <b>Питання для всіх:</b> {q}")


# ---------------------------------------------------------------------------
# 💡 «А ти знав?» і 🎁 рефералка — разом із вечірнім нагадуванням о 20:55 (щоб не слати зайвого):
# підказка через день, у неділю — запрошення друзів за Premium. Новачкам — окремий рядок.
# ---------------------------------------------------------------------------
NEWBIE_MEET_LINE = "\n\n💜 Ти ще ні з ким не поговорив(-ла) — о 21:00 найкращий шанс знайти співрозмовника за секунди!"
TIPS = [
    ("🎙 Голосове можна надіслати з маскою — співрозмовник почує робота, привида чи демона, а не тебе.", "🎙 Спробувати маску голосу", "vm_menu"),
    ("🎭 Якщо розмова зайшла, можна разом зняти маски й побачити одне одного — тільки якщо захочете обоє.", "🔍 Знайти співрозмовника", "wp_go:normal"),
    ("🎲 У чаті є «Правда чи дія» і «Що обереш?» — ідеально, коли не знаєш, про що говорити.", "🔍 Знайти співрозмовника", "wp_go:normal"),
    ("💌 Напиши нічний лист — а вранці він прилетить незнайомцю, і тобі — чийсь.", "💌 Написати лист", "tip_cmd:letter"),
    ("🔮 Щодня новий гороскоп і карта дня Таро.", "✨ Відкрити Розваги", "fun_menu"),
    ("🤫 Можна анонімно розповісти те, що не скажеш нікому, — у каналі зізнань.", "🤫 Написати зізнання", "tip_cmd:confess"),
    ("👥 Є кімнати за інтересами — групові чати, де завжди хтось є.", "👥 Відкрити кімнати", "rooms_open"),
    ("💸 Частину вартості подарунків, які тобі дарують, можна вивести на карту (18+).", "🔍 Знайти співрозмовника", "wp_go:normal"),
]


def evening_extra() -> tuple[str, str, str] | None:
    """Що додати до вечірнього нагадування сьогодні: (текст, кнопка, callback) або None."""
    today = kyiv_today()
    if halloween_active():
        return (
            "🎃 <b>Тиждень Хелловіну</b>: маски голосу «Привид» і «Демон» безкоштовно, лімітовані подарунки "
            f"🎃🦇👻 і «Страшна правда чи дія». А {HW_NIGHT.day}.{HW_NIGHT.month:02d} · {HW_NIGHT.hour}:00 — Ніч страшилок!",
            "🎃 Що на Хелловін?",
            "hw_open",
        )
    if today.weekday() == 6:  # неділя — рефералка
        return (
            f"🎁 <b>Premium безкоштовно</b>: запроси {REFERRAL_PREMIUM_EVERY} друзів — отримаєш "
            f"{REFERRAL_PREMIUM_DAYS} днів Premium (фільтр за статтю й віком, усі маски голосу, без реклами). "
            "А топ-3 тижня отримують Premium за конкурсом запрошень!",
            "🎁 Моє посилання для друзів",
            "prem_free",
        )
    if today.toordinal() % 2:
        return None
    text, btn, cb = TIPS[(today.toordinal() // 2) % len(TIPS)]
    return (f"💡 <b>А ти знав?</b> {text}", btn, cb)


def evening_extra_text() -> str:
    x = evening_extra()
    return f"\n\n{x[0]}" if x else ""


def evening_extra_button():
    x = evening_extra()
    return InlineKeyboardButton(text=x[1], callback_data=x[2]) if x else None


@dp.callback_query(F.data.startswith("tip_cmd:"))
async def tip_cmd(call: types.CallbackQuery):
    what = call.data.split(":", 1)[1]
    await call.answer()
    if what == "letter":
        await call.message.answer("💌 Щоб написати нічний лист, натисни /letter")
    elif what == "confess":
        await call.message.answer("🤫 Щоб написати анонімне зізнання, натисни /confess")


def is_fresh_newbie(u: dict) -> bool:
    joined = u.get("joined_at") or 0
    return not u.get("total_chats") and joined and time.time() - joined < 14 * 86400


# ---------------------------------------------------------------------------
# 🌙 Теплий старт: новачкам, які ще ні з ким не поговорили, — одне «живе» запрошення під час
# Години знайомств (о 21:10), коли в боті справді багато людей. Лише раз на людину.
# ---------------------------------------------------------------------------
WARM_START_MINUTE = 10
WARM_START_MAX_AGE = 3 * 86400  # лише ті, хто прийшов за останні 3 дні
WARM_START_MIN_ONLINE = 3  # якщо в боті майже нікого — не кличемо, щоб не розчарувати
warm_state: dict = {"day": None}


def warm_start_recipients() -> list[int]:
    now = time.time()
    out = []
    for uid, u in users_db.items():
        if uid <= 0 or uid in banned_users or uid in active_chats or uid in queue or uid in user_room:
            continue
        if u.get("total_chats") or u.get("warm_start_sent") or not u.get("notify_meet", True):
            continue
        joined = u.get("joined_at") or 0
        if not joined or now - joined > WARM_START_MAX_AGE:
            continue
        out.append(uid)
    return out


async def warm_start_tick():
    now = datetime.now(KYIV_TZ)
    today = now.date().isoformat()
    if now.hour != MEET_HOUR or now.minute < WARM_START_MINUTE or warm_state.get("day") == today:
        return
    if not _contest_can_write():
        return
    warm_state["day"] = today
    online = online_now()
    if online < WARM_START_MIN_ONLINE:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔍 Знайти співрозмовника", callback_data="wp_go:normal")]])
    sent = 0
    for uid in warm_start_recipients():
        users_db[uid]["warm_start_sent"] = True
        if await safe_send(
            uid,
            f"🔥 <b>Зараз Година знайомств</b> — у боті <b>{online}</b> людей онлайн.\n\n"
            "Ти ще ні з ким не поговорив(-ла) — саме час: натисни, і за кілька секунд я з'єднаю тебе "
            "з кимось анонімно 💬",
            reply_markup=kb,
        ):
            sent += 1
        await asyncio.sleep(0.05)
    if sent:
        stat_add("warm_start", sent)
    request_save()


# ---------------------------------------------------------------------------
# 👋 «Ми скучили»: одне повідомлення тим, хто не заходив 4+ дні (не частіше ніж раз на 2 тижні)
# ---------------------------------------------------------------------------
COMEBACK_AFTER_DAYS = int(os.getenv("COMEBACK_AFTER_DAYS", "4"))
COMEBACK_EVERY_DAYS = 14
COMEBACK_HOUR = 19  # о 19:xx за Києвом — до вечірнього піку
comeback_state: dict = {"day": None}


def comeback_text() -> str:
    lines = [
        "👋 <b>Давно не бачились!</b>",
        "",
        "За цей час у ANONimchyk:",
        "🎭 у чаті можна разом зняти маски — тільки якщо захочете обоє",
        "🎙 голосові з маскою — твій голос ніхто не впізнає",
        "👥 кімнати, де завжди хтось є",
    ]
    if halloween_active():
        lines.append("🎃 <b>Тиждень Хелловіну</b>: страшні маски голосу безкоштовно й лімітовані подарунки")
    lines += ["", f"⏰ Найбільше людей онлайн щодня о {MEET_HOUR}:00. Заходь 💜"]
    return "\n".join(lines)


async def comeback_tick():
    now = datetime.now(KYIV_TZ)
    today = now.date()
    if now.hour != COMEBACK_HOUR or comeback_state.get("day") == today.isoformat() or not _contest_can_write():
        return
    comeback_state["day"] = today.isoformat()
    edge = (today - timedelta(days=COMEBACK_AFTER_DAYS)).isoformat()
    again = (today - timedelta(days=COMEBACK_EVERY_DAYS)).isoformat()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔍 Знайти співрозмовника", callback_data="wp_go:normal")]])
    sent = 0
    for uid, u in list(users_db.items()):
        if uid <= 0 or uid in banned_users or uid in active_chats or uid in queue or uid in user_room:
            continue
        if not u.get("notify_tips", True):
            continue
        last = u.get("last_active_day")
        if not last:
            u["last_active_day"] = today.isoformat()  # старі користувачі: відлік з сьогодні, без розсилки
            continue
        if last > edge or (u.get("comeback_day") or "0") > again:
            continue
        u["comeback_day"] = today.isoformat()
        if await safe_send(uid, comeback_text(), reply_markup=kb):
            sent += 1
        await asyncio.sleep(0.05)
    if sent:
        stat_add("comeback", sent)
    request_save()


# ---------------------------------------------------------------------------
# 🎃 Хелловін: тиждень масок (27.10–01.11) — страшні маски голосу безкоштовно, лімітовані подарунки,
# «Страшна правда чи дія», Ніч страшилок 31.10 о 23:00 і значок 🎃 за чат 31 жовтня
# ---------------------------------------------------------------------------
HW_FROM = _parse_dt_env("HALLOWEEN_FROM", "2026-10-27T00:00")
HW_TO = _parse_dt_env("HALLOWEEN_TO", "2026-11-02T00:00")
HW_NIGHT = _parse_dt_env("HALLOWEEN_NIGHT", "2026-10-31T23:00")  # Ніч страшилок триває годину
HW_BADGE_DAY = HW_NIGHT.date().isoformat()  # хто поспілкувався цього дня — отримує значок 🎃
HW_NIGHT_REMIND_MIN = 5
HALLOWEEN_VOICES = {"ghost", "demon"}

SPOOKY_TRUTHS = [
    "Чого ти боявся(-лася) в дитинстві найбільше?",
    "Розкажи найстрашніший сон, який пам'ятаєш.",
    "Ти віриш у привидів? Чому?",
    "Що найдивніше траплялося з тобою вночі?",
    "У який фільм жахів ти ніколи не подивишся вдруге?",
    "Чи був у тебе момент, коли здавалося, що вдома хтось є?",
    "Який звук уночі лякає тебе найбільше?",
    "Куди б ти ніколи не пішов (не пішла) сам(а) вночі?",
]
SPOOKY_DARES = [
    "Напиши страшну історію рівно в три речення 👻",
    "Надішли голосове моторошним шепотом (маска «Привид» безкоштовна до 1.11) 🎙",
    "Опиши свій ідеальний хелловінський костюм одним реченням 🎃",
    "Вигадай назву для фільму жахів про ваш чат 🎬",
    "Надішли найстрашніший стікер, який у тебе є 😱",
    "Розкажи міську легенду зі свого міста (або вигадай її) 🏚",
]


def halloween_active(now: datetime | None = None) -> bool:
    now = now or datetime.now(KYIV_TZ)
    return HW_FROM <= now < HW_TO


def hw_night_live(now: datetime | None = None) -> bool:
    now = now or datetime.now(KYIV_TZ)
    return HW_NIGHT <= now < HW_NIGHT + timedelta(hours=1)


def hw_text() -> str:
    return (
        "🎃 <b>Тиждень Хелловіну в ANONimchyk</b> · до 1 листопада\n\n"
        "👻 Маски голосу <b>«Привид»</b> і <b>«Демон»</b> — безкоштовно для всіх: /voice\n"
        "🎁 Лімітовані подарунки 🎃 Гарбуз · 🦇 Кажан · 👻 Привид — тільки цього тижня\n"
        "🎲 «Страшна правда чи дія» — у грі під час чату\n"
        f"🎃 <b>Полювання на гарбузи</b>: після кожного справжнього чату є шанс знайти гарбуз. "
        f"Збери {HW_PUMPKINS_NEED} — отримаєш Premium на добу!\n\n"
        f"🌙 <b>{HW_NIGHT.day}.{HW_NIGHT.month:02d} · {HW_NIGHT.hour}:00 — Ніч страшилок!</b> "
        "Заходь, розкажи страшну історію анонімно. Усі, хто поспілкується "
        f"{HW_NIGHT.day} жовтня, отримають значок 🎃 у профілі назавжди."
    )


def hw_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Знайти співрозмовника", callback_data="wp_go:normal")],
        [InlineKeyboardButton(text="👻 Маска голосу", callback_data="vm_menu")],
    ])


@dp.message(F.text == BTN_HALLOWEEN)
@dp.message(Command("halloween"))
async def hw_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    init_user(message.from_user.id)
    await send_banner(message.from_user.id, "halloween")
    if not halloween_active():
        await message.answer("🎃 Хелловінський тиждень уже завершився — до наступного року! 👻")
        return
    await message.answer(hw_text(), reply_markup=hw_kb())


@dp.callback_query(F.data == "hw_open")
async def hw_open(call: types.CallbackQuery):
    await call.answer()
    await call.message.answer(hw_text() if halloween_active() else "🎃 Хелловін уже минув 👻", reply_markup=hw_kb())


def hw_mark_badge(*uids: int):
    if datetime.now(KYIV_TZ).date().isoformat() != HW_BADGE_DAY:
        return
    for x in uids:
        u = users_db.get(x)
        if u is not None and not u.get("hw2026"):
            u["hw2026"] = True


HW_PUMPKINS_NEED = 3
HW_PUMPKIN_CHANCE = float(os.getenv("HW_PUMPKIN_CHANCE", "0.35"))
HW_PUMPKIN_PREMIUM_MAX = 2  # скільки разів за Хелловін можна виграти Premium гарбузами


async def hw_pumpkin_roll(user_id: int):
    u = users_db.get(user_id)
    if u is None or u.get("hw_prem_got", 0) >= HW_PUMPKIN_PREMIUM_MAX or random.random() > HW_PUMPKIN_CHANCE:
        return
    n = u.get("hw_pumpkins", 0) + 1
    if n >= HW_PUMPKINS_NEED:
        u["hw_pumpkins"] = 0
        u["hw_prem_got"] = u.get("hw_prem_got", 0) + 1
        current = u["perks"].get("premium", 0)
        if current != float("inf"):
            u["perks"]["premium"] = max(time.time(), current) + DAY
        request_save()
        await safe_send(user_id, "🎃🎃🎃 <b>Три гарбузи!</b> Premium на добу вже активовано — фільтри й усі маски голосу твої 💜")
        return
    u["hw_pumpkins"] = n
    stat_add("pumpkins")
    request_save()
    await safe_send(
        user_id,
        f"🎃 Ти знайшов(-ла) гарбуз! <b>{n}/{HW_PUMPKINS_NEED}</b>\n"
        f"Збери {HW_PUMPKINS_NEED} — і отримаєш Premium на добу. Гарбузи ховаються в справжніх розмовах 😉",
    )


async def hw_night_tick():
    """31.10 о 22:55 — запрошення на Ніч страшилок (тим, хто отримує нагадування про Годину знайомств)."""
    now = datetime.now(KYIV_TZ)
    start = HW_NIGHT - timedelta(minutes=HW_NIGHT_REMIND_MIN)
    if not (start <= now < HW_NIGHT) or launch_state.get("hw_night") or not _contest_can_write():
        return
    launch_state["hw_night"] = True
    request_save()
    text = (
        f"🌙 <b>Ніч страшилок — о {HW_NIGHT.hour}:00!</b> 👻\n\n"
        "Заходь, знайди співрозмовника й розкажіть одне одному найстрашнішу історію — анонімно.\n"
        "Маска голосу «Привид» безкоштовна — розкажи моторошним голосом 🎙\n\n"
        "🎃 Усі, хто поспілкується сьогодні, отримають значок у профілі назавжди!"
    )
    kb = hw_kb()
    for uid in meet_recipients():
        if uid in active_chats or uid in queue:
            continue
        await safe_send(uid, text, reply_markup=kb)
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# 🚨 Скарги з причиною і доказом від постраждалого (як у великих месенджерах):
# під час чату в пам'яті лише останні 10 повідомлень кожного співрозмовника (текст; медіа — позначкою),
# після чату вони стираються. Модератор отримує їх ТІЛЬКИ якщо той, хто скаржиться, сам погодився.
# Самі повідомлення в базі не зберігаються — лише в повідомленні адміну.
# ---------------------------------------------------------------------------
EVIDENCE_KEEP = 10
MINOR_FAST_BAN = 2  # стільки скарг «👶» від різних людей — бан одразу, до перевірки адміном
recent_msgs: dict[int, list] = {}  # user_id -> [(час, текст)] його останні повідомлення в поточному чаті
report_pending: dict[int, dict] = {}  # хто скаржиться -> {"on", "msgs", "ts", "reason"}

REPORT_REASONS = {
    "insult": "🤬 Образи, погрози",
    "nsfw": "🔞 Непристойності",
    "spam": "📢 Спам, реклама, шахрайство",
    "minor": "👶 Неповнолітній / пристає до неповнолітніх",
    "other": "❓ Інше",
}


def msg_mark(message) -> str:
    text = getattr(message, "text", None)
    if text is not None:
        return text[:500]
    if getattr(message, "photo", None):
        mark = "[фото]"
    elif getattr(message, "voice", None):
        mark = "[голосове]"
    elif getattr(message, "video_note", None):
        mark = "[кружечок]"
    elif getattr(message, "video", None):
        mark = "[відео]"
    elif getattr(message, "animation", None):
        mark = "[GIF]"
    elif getattr(message, "sticker", None):
        mark = "[стікер]"
    else:
        mark = "[файл]"
    cap = getattr(message, "caption", None)
    return mark + (f" {cap[:300]}" if cap else "")


def remember_recent(user_id: int, text: str):
    buf = recent_msgs.setdefault(user_id, [])
    buf.append((time.time(), text))
    del buf[:-EVIDENCE_KEEP]


def forget_recent(*uids: int):
    for x in uids:
        recent_msgs.pop(x, None)


def report_reason_kb():
    rows = [[InlineKeyboardButton(text=label, callback_data=f"rep_r:{k}")] for k, label in REPORT_REASONS.items()]
    rows.append([InlineKeyboardButton(text="✖️ Скасувати", callback_data="rep_r:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def report_history(target: int) -> str:
    mine = [r for r in reports if r.get("on") == target]
    if not mine:
        return "перша скарга"
    by = {}
    for r in mine:
        k = REPORT_REASONS.get(r.get("reason"), "без причини").split(" ")[0]
        by[k] = by.get(k, 0) + 1
    u = users_db.get(target) or {}
    return f"усього {len(mine)}: " + " ".join(f"{k}×{n}" for k, n in by.items()) + f" · {rating_line(u)}"


@dp.callback_query(F.data.startswith("rep_r:"))
async def report_reason_pick(call: types.CallbackQuery):
    user_id = call.from_user.id
    reason = call.data.split(":", 1)[1]
    pend = report_pending.get(user_id)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    if reason == "cancel" or pend is None or time.time() - pend["ts"] > 600:
        report_pending.pop(user_id, None)
        await call.answer("Скасовано" if reason == "cancel" else "Скарга застаріла")
        return
    pend["reason"] = reason if reason in REPORT_REASONS else "other"
    await call.answer()
    if pend["msgs"]:
        await call.message.answer(
            "📎 Додати до скарги останні повідомлення співрозмовника як доказ?\n"
            "<i>Їх побачить лише модератор. Фото й відео не передаються.</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Так, додати", callback_data="rep_e:1"),
                InlineKeyboardButton(text="Ні", callback_data="rep_e:0"),
            ]]),
        )
        return
    await submit_report(call.message, user_id, with_evidence=False)


@dp.callback_query(F.data.startswith("rep_e:"))
async def report_evidence_pick(call: types.CallbackQuery):
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await call.answer()
    if call.from_user.id not in report_pending:
        return
    await submit_report(call.message, call.from_user.id, with_evidence=call.data.endswith("1"))


async def submit_report(target_msg, user_id: int, with_evidence: bool):
    global report_counter
    pend = report_pending.pop(user_id, None)
    if pend is None:
        return
    partner_id, reason = pend["on"], pend.get("reason", "other")
    report_counter += 1
    reports.append({
        "id": report_counter, "from": user_id, "on": partner_id,
        "time": time.strftime("%d.%m.%Y %H:%M"), "status": "нова", "reason": reason,
    })
    stat_add("reports", 1, reason)
    p = init_user(partner_id)
    p["reports_received"] = p.get("reports_received", 0) + 1
    minor_from = {r["from"] for r in reports if r.get("on") == partner_id and r.get("reason") == "minor"}
    auto_banned = False
    if partner_id not in banned_users and (
        p["reports_received"] >= AUTO_BAN_REPORTS or len(minor_from) >= MINOR_FAST_BAN
    ):
        banned_users.add(partner_id)
        register_auto_ban(p)
        auto_banned = True
        if active_chats.get(partner_id) not in (None, user_id):
            other = end_chat(partner_id)
            await safe_send(other, "Співрозмовник завершив чат.", reply_markup=get_main_keyboard())
        if partner_id in queue:
            queue.remove(partner_id)
        remove_from_room(partner_id)
        ban_text, ban_kb = banned_notice(partner_id)
        await safe_send(partner_id, ban_text, reply_markup=ban_kb)
    request_save()
    if ADMIN_ID:
        urgent = "‼️ <b>ТЕРМІНОВО</b> · " if reason in ("minor", "nsfw") else ""
        text = (
            f"{urgent}🚨 Скарга #{report_counter}: <b>{REPORT_REASONS.get(reason, reason)}</b>\n"
            f"Від: {admin_label(user_id)}\nНа: {admin_label(partner_id)}\n"
            f"📊 На цю людину: {report_history(partner_id)}"
            + ("\n\n⛔ Автобан спрацював." if auto_banned else "")
        )
        if with_evidence and pend["msgs"]:
            ev = "\n".join(
                f"{datetime.fromtimestamp(ts, KYIV_TZ).strftime('%H:%M')} {protect_text(esc(t))}" for ts, t in pend["msgs"]
            )
            text += f"\n\n📎 <b>Останні повідомлення порушника</b> (додав той, хто скаржиться):\n{ev}"
        await safe_send(ADMIN_ID, text[:4000], protect_content=True)
    was_chat = active_chats.get(user_id) == partner_id
    if was_chat:
        end_chat(user_id)
        await safe_send(partner_id, "Співрозмовник завершив чат.", reply_markup=get_main_keyboard())
    await target_msg.answer(
        "🚨 Скаргу надіслано" + (", чат завершено" if was_chat else "") + ". Дякуємо, що допомагаєш робити бот безпечнішим!",
        reply_markup=get_main_keyboard(),
    )
    if was_chat:
        await auto_search_after_chat(user_id)
        await auto_search_after_chat(partner_id)


# ---------------------------------------------------------------------------
# ↩️ Відповіді й ✏️ редагування в анонімному чаті: свайп-відповідь приходить співрозмовнику
# як відповідь на те саме повідомлення, а виправлене повідомлення виправляється і в нього.
# Пам'ятаємо пари (мій чат, id) ↔ (чат співрозмовника, id) — лише в пам'яті, з обмеженням розміру.
# ---------------------------------------------------------------------------
PAIR_MSG_LIMIT = 40000
pair_msgs: dict[tuple[int, int], tuple[int, int]] = {}


def remember_pair_msg(user_id: int, my_id, partner_id: int, their_id) -> None:
    if not my_id or not their_id:
        return
    while len(pair_msgs) >= PAIR_MSG_LIMIT:
        pair_msgs.pop(next(iter(pair_msgs)), None)
    pair_msgs[(user_id, my_id)] = (partner_id, their_id)
    pair_msgs[(partner_id, their_id)] = (user_id, my_id)
    # ↩️ «Ви зупинились тут»: пам'ятаємо лише id останнього повідомлення (не текст)
    last_chat_msg[user_id] = (partner_id, my_id, "me")
    last_chat_msg[partner_id] = (user_id, their_id, "them")


# user_id -> (partner_id, message_id у чаті user_id, хто писав: "me"/"them") — лише в пам'яті
last_chat_msg: dict[int, tuple[int, int, str]] = {}


def resume_kwargs(user_id: int) -> tuple[dict, str]:
    """Цитата останнього повідомлення поточного чату + підпис «Ви зупинились тут»."""
    hit = last_chat_msg.get(user_id)
    partner_id = active_chats.get(user_id)
    if not hit or partner_id is None or hit[0] != partner_id:
        return {}, ""
    line = (
        "↩️ <b>Ви зупинились тут</b> ☝️ — співрозмовник чекає на твою відповідь 💬"
        if hit[2] == "them" else
        "↩️ <b>Ви зупинились тут</b> ☝️ — останнім писав(-ла) ти, чекаємо відповідь 🙂"
    )
    return {"reply_parameters": types.ReplyParameters(message_id=hit[1], allow_sending_without_reply=False)}, line


def reply_kwargs(message, partner_id: int) -> dict:
    """Якщо людина відповідає свайпом — співрозмовник побачить відповідь на те саме повідомлення."""
    r = getattr(message, "reply_to_message", None)
    if r is None:
        return {}
    hit = pair_msgs.get((message.from_user.id, getattr(r, "message_id", None)))
    if not hit or hit[0] != partner_id:  # повідомлення з попереднього чату — без цитати
        return {}
    return {"reply_parameters": types.ReplyParameters(message_id=hit[1], allow_sending_without_reply=True)}


@dp.edited_message()
async def relay_edit(message: types.Message):
    if message.chat.type != "private":
        return
    user_id = message.from_user.id
    partner_id = active_chats.get(user_id)
    hit = pair_msgs.get((user_id, message.message_id))
    if partner_id is None or not hit or hit[0] != partner_id:
        return
    new_text = message.text if message.text is not None else message.caption
    if new_text is None:
        return
    if LINK_REGEX.search(new_text):
        await message.answer("🚫 Виправлення з посиланням або контактом не надіслано співрозмовнику.")
        return
    try:
        if message.text is not None:
            await bot.edit_message_text(
                chat_id=partner_id, message_id=hit[1], text=protect_text(new_text),
                entities=message.entities, parse_mode=None,
            )
        else:
            await bot.edit_message_caption(
                chat_id=partner_id, message_id=hit[1], caption=protect_text(new_text),
                caption_entities=message.caption_entities, parse_mode=None,
            )
        remember_recent(user_id, "[виправлено] " + new_text[:500])
    except TelegramAPIError as e:
        logging.info("Редагування не передано: %s", e)


# ---------------------------------------------------------------------------
# 📞 Анонімні дзвінки (LiveKit Cloud). Бот створює одноразову кімнату на двох і надсилає обом посилання
# на сторінку дзвінка, яку віддає наш же вебсервер. Весь звук і відео йдуть через сервер LiveKit,
# тож співрозмовники не бачать IP одне одного; Telegram-акаунти теж ніхто не бачить.
# Умови: обом 18+, Premium хоча б в одного, згода обох. Камера за замовчуванням вимкнена. До 15 хв.
# Змінні Render: LIVEKIT_URL (wss://…livekit.cloud), LIVEKIT_API_KEY, LIVEKIT_API_SECRET.
# ---------------------------------------------------------------------------
import base64
import hashlib as _hashlib_calls
import hmac
import json as _json_calls
import secrets

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "").strip().strip("\"'").strip()
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "").strip().strip("\"'").strip()
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "").strip().strip("\"'").strip()
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().strip("\"'").strip().rstrip("/")
CALLS_ON = bool(LIVEKIT_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET and PUBLIC_URL)


def calls_status() -> str:
    """Для адміна: чи працюють дзвінки і якої змінної бракує (лише назви, без значень)."""
    missing = [n for n, v in (("LIVEKIT_URL", LIVEKIT_URL), ("LIVEKIT_API_KEY", LIVEKIT_API_KEY),
                              ("LIVEKIT_API_SECRET", LIVEKIT_API_SECRET), ("PUBLIC_URL", PUBLIC_URL)) if not v]
    if missing:
        return "📞 Дзвінки: вимкнено — немає " + ", ".join(missing)
    bad = []
    if not LIVEKIT_URL.startswith(("wss://", "ws://", "https://")):
        bad.append("LIVEKIT_URL має починатися з wss://")
    if not PUBLIC_URL.startswith("https://"):
        bad.append("PUBLIC_URL має починатися з https://")
    if any(c in (LIVEKIT_URL + LIVEKIT_API_KEY + LIVEKIT_API_SECRET + PUBLIC_URL) for c in "\"' "):
        bad.append("у значеннях є лапки або пробіли")
    return "📞 Дзвінки: увімкнено" + (" ⚠️ " + "; ".join(bad) if bad else " ✅")
CALL_MINUTES = int(os.getenv("CALL_MINUTES", "15"))
CALL_DAILY_MAX = int(os.getenv("CALL_DAILY_MAX", "6"))  # скільки запрошень на дзвінок людина може надіслати за день
CALL_COOLDOWN = 60
CALL_INVITE_TTL = 120
CALL_MIN_AGE = 18

call_requests: dict[int, tuple[int, float]] = {}  # кому -> (від кого, коли)
call_tickets: dict[str, dict] = {}  # id посилання -> {"room","token","end","lang"}
call_last: dict[int, float] = {}
call_peers: dict[int, dict[int, float]] = {}  # з ким людина нещодавно мала дзвінок (для скарги)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def livekit_jwt(claims: dict, ttl: int) -> str:
    now = int(time.time())
    payload = {"iss": LIVEKIT_API_KEY, "nbf": now - 10, "exp": now + ttl, **claims}
    head = _b64url(_json_calls.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64url(_json_calls.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
    sig = hmac.new(LIVEKIT_API_SECRET.encode(), f"{head}.{body}".encode(), _hashlib_calls.sha256).digest()
    return f"{head}.{body}.{_b64url(sig)}"


def _livekit_http() -> str:
    u = LIVEKIT_URL
    if u.startswith("wss://"):
        u = "https://" + u[6:]
    elif u.startswith("ws://"):
        u = "http://" + u[5:]
    return u.rstrip("/")


async def livekit_api(method: str, body: dict) -> bool:
    token = livekit_jwt({"video": {"roomCreate": True, "roomList": True}}, 60)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.post(
                f"{_livekit_http()}/twirp/livekit.RoomService/{method}",
                json=body, headers={"Authorization": f"Bearer {token}"},
            ) as r:
                if r.status != 200:
                    logging.warning("LiveKit %s: HTTP %s %s", method, r.status, (await r.text())[:200])
                return r.status == 200
    except Exception as e:  # noqa: BLE001
        logging.warning("LiveKit %s: %s", method, e)
        return False


def call_block_reason(u: dict, p: dict) -> str | None:
    """Чому дзвінок неможливий (або None — можна)."""
    for x in (u, p):
        age = get_age_int(x)
        low = min_age_ever(x)
        if age is None or age < CALL_MIN_AGE or (low is not None and low < CALL_MIN_AGE):
            return "self" if x is u else "partner"
    if not (is_premium(u) or is_premium(p)):
        return "premium"
    return None


CALL_PAGE_TEXT = {
    "uk": {"title": "Анонімний дзвінок", "join": "📞 Приєднатися", "wait": "Чекаємо співрозмовника…",
           "on": "Співрозмовник на зв'язку", "left": "Співрозмовник вийшов", "ended": "Дзвінок завершено",
           "mic": "🎙 Мікрофон", "cam": "📷 Камера", "end": "Завершити", "note":
           "Твій Telegram і номер ніхто не бачить. Камера вимкнена — вмикаєш лише ти. Не показуй документи й особисте. Якщо щось не так — завершуй дзвінок і поскаржся в боті.",
           "err": "Не вдалося підключитися. Дозволь доступ до мікрофона або відкрий посилання в Chrome / Safari.",
           "gone": "Посилання вже недійсне — дзвінок завершився."},
    "ru": {"title": "Анонимный звонок", "join": "📞 Присоединиться", "wait": "Ждём собеседника…",
           "on": "Собеседник на связи", "left": "Собеседник вышел", "ended": "Звонок завершён",
           "mic": "🎙 Микрофон", "cam": "📷 Камера", "end": "Завершить", "note":
           "Твой Telegram и номер никто не видит. Камера выключена — включаешь только ты. Не показывай документы и личное. Если что-то не так — завершай звонок и пожалуйся в боте.",
           "err": "Не удалось подключиться. Разреши доступ к микрофону или открой ссылку в Chrome / Safari.",
           "gone": "Ссылка уже недействительна — звонок завершился."},
    "en": {"title": "Anonymous call", "join": "📞 Join", "wait": "Waiting for your partner…",
           "on": "Your partner is here", "left": "Your partner left", "ended": "Call ended",
           "mic": "🎙 Mic", "cam": "📷 Camera", "end": "End", "note":
           "No one sees your Telegram or number. The camera is off — only you can turn it on. Don't show documents or anything personal. If something's wrong — end the call and report in the bot.",
           "err": "Couldn't connect. Allow microphone access or open the link in Chrome / Safari.",
           "gone": "This link is no longer valid — the call has ended."},
}

CALL_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>__TITLE__ · ANONimchyk</title>
<style>
:root{--bg:#0c0618;--card:#1a1030;--acc:#7c3aed;--txt:#f4efff;--mut:#b7a6e0}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:radial-gradient(900px 900px at 50% 30%,#3a1a6e,#120826 60%,#08040f);color:var(--txt);font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:520px;margin:0 auto;min-height:100%;display:flex;flex-direction:column;align-items:center;padding:24px 16px;gap:16px}
h1{font-size:22px;margin:8px 0 0}.st{color:var(--mut);font-size:15px;min-height:20px;text-align:center}
.timer{font-variant-numeric:tabular-nums;font-size:15px;color:#ffd9a8}
.stage{position:relative;width:100%;aspect-ratio:3/4;border-radius:24px;overflow:hidden;background:#140c26;display:flex;align-items:center;justify-content:center;box-shadow:0 0 60px rgba(124,58,237,.25)}
.mask{font-size:96px;opacity:.9}#remote video{width:100%;height:100%;object-fit:cover}
#me{position:absolute;right:10px;bottom:10px;width:28%;aspect-ratio:3/4;border-radius:14px;overflow:hidden;background:#000;display:none}#me video{width:100%;height:100%;object-fit:cover;transform:scaleX(-1)}
.row{display:flex;gap:10px;width:100%}button{flex:1;border:0;border-radius:16px;padding:16px 6px;font-size:15px;font-weight:600;color:#fff;background:var(--card);white-space:nowrap}
button.on{background:var(--acc)}button.end{background:#c0264b}button.big{background:var(--acc);font-size:19px;padding:18px}
.note{font-size:13px;color:var(--mut);text-align:center;line-height:1.45}.hide{display:none!important}
</style></head><body><div class="wrap">
<h1>🎭 __TITLE__</h1><div class="timer" id="tm"></div><div class="st" id="st"></div>
<div class="stage"><div id="remote" class="hide"></div><div class="mask" id="mk">🎭</div><div id="me"></div></div>
<button class="big" id="join">__JOIN__</button>
<div class="row hide" id="ctl"><button id="mic" class="on">__MIC__</button><button id="cam">__CAM__</button><button class="end" id="end">__END__</button></div>
<div class="note">__NOTE__</div></div>
<script src="https://cdn.jsdelivr.net/npm/livekit-client@2.5.7/dist/livekit-client.umd.min.js"></script>
<script>
const T=__TEXTS__,URL_=__URL__,TOKEN=__TOKEN__,END=__END_TS__;
const $=id=>document.getElementById(id);let room=null,timerId=null;
function st(t){$('st').textContent=t}
function tick(){const s=Math.max(0,Math.round(END-Date.now()/1000));$('tm').textContent=String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0');if(s<=0)finish()}
function finish(){if(timerId)clearInterval(timerId);if(room)room.disconnect();st(T.ended);$('ctl').classList.add('hide');$('join').classList.add('hide')}
tick();timerId=setInterval(tick,1000);
$('join').onclick=async()=>{
 const L=window.LivekitClient;if(!L){st(T.err);return}
 $('join').disabled=true;
 room=new L.Room({adaptiveStream:true,dynacast:true});
 room.on(L.RoomEvent.TrackSubscribed,(track)=>{const el=track.attach();if(track.kind==='video'){$('remote').innerHTML='';$('remote').appendChild(el);$('remote').classList.remove('hide');$('mk').classList.add('hide')}else{el.style.display='none';document.body.appendChild(el)}});
 room.on(L.RoomEvent.TrackUnsubscribed,(track)=>{track.detach().forEach(e=>e.remove());if(track.kind==='video'){$('remote').classList.add('hide');$('mk').classList.remove('hide')}});
 room.on(L.RoomEvent.ParticipantConnected,()=>st(T.on));
 room.on(L.RoomEvent.ParticipantDisconnected,()=>st(T.left));
 room.on(L.RoomEvent.Disconnected,()=>{st(T.ended);$('ctl').classList.add('hide')});
 room.on(L.RoomEvent.LocalTrackPublished,(pub)=>{if(pub.track&&pub.track.kind==='video'&&pub.source===L.Track.Source.Camera){const el=pub.track.attach();$('me').innerHTML='';$('me').appendChild(el);$('me').style.display='block'}});
 room.on(L.RoomEvent.LocalTrackUnpublished,(pub)=>{if(pub.track&&pub.track.kind==='video'){$('me').style.display='none';$('me').innerHTML=''}});
 try{await room.connect(URL_,TOKEN);await room.startAudio();await room.localParticipant.setMicrophoneEnabled(true);
  $('join').classList.add('hide');$('ctl').classList.remove('hide');st(room.remoteParticipants.size?T.on:T.wait)}
 catch(e){console.log(e);st(T.err);$('join').disabled=false}
};
$('mic').onclick=async()=>{const on=!room.localParticipant.isMicrophoneEnabled;await room.localParticipant.setMicrophoneEnabled(on);$('mic').classList.toggle('on',on)};
$('cam').onclick=async()=>{const on=!room.localParticipant.isCameraEnabled;try{await room.localParticipant.setCameraEnabled(on);$('cam').classList.toggle('on',on);if(!on){$('me').style.display='none';$('me').innerHTML=''}}catch(e){st(T.err);$('me').style.display='none';$('me').innerHTML=''}};
$('end').onclick=finish;
</script></body></html>"""


def call_page_html(ticket: dict | None) -> str:
    lang = (ticket or {}).get("lang", "uk")
    t = CALL_PAGE_TEXT.get(lang, CALL_PAGE_TEXT["uk"])
    if not ticket or time.time() > ticket["end"]:
        return (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                f"</head><body style='background:#0c0618;color:#f4efff;font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center;padding:16px'>"
                f"<div style='font-size:18px'>🎭<br><br>{html.escape(t['gone'])}</div></body></html>")
    page = CALL_PAGE
    for k, v in (("__TITLE__", t["title"]), ("__JOIN__", t["join"]), ("__MIC__", t["mic"]), ("__CAM__", t["cam"]),
                 ("__END__", t["end"]), ("__NOTE__", t["note"])):
        page = page.replace(k, html.escape(v))
    return (page.replace("__TEXTS__", _json_calls.dumps(t, ensure_ascii=False))
            .replace("__URL__", _json_calls.dumps(LIVEKIT_URL)).replace("__TOKEN__", _json_calls.dumps(ticket["token"]))
            .replace("__END_TS__", str(int(ticket["end"]))))


async def handle_call_page(request: web.Request):
    tid = request.match_info.get("tid", "")
    return web.Response(
        text=call_page_html(call_tickets.get(tid)), content_type="text/html",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def _call_cleanup():
    now = time.time()
    for k in [k for k, v in call_tickets.items() if now > v["end"] + 600]:
        call_tickets.pop(k, None)


@dp.message(F.text == BTN_CALL)
@dp.message(Command("call"))
async def call_start(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    pid, kind = gift_target(uid)
    if pid is None:
        await message.answer("📞 Дзвінок можна запропонувати під час чату або другу в переписці 🙂")
        return
    if not CALLS_ON:
        await message.answer("📞 Дзвінки зовсім скоро з'являться 🙂")
        return
    u, p = init_user(uid), init_user(pid)
    why = call_block_reason(u, p)
    if why == "self":
        await message.answer(f"📞 Дзвінки доступні лише з {CALL_MIN_AGE} років.")
        return
    if why == "partner":
        await message.answer("📞 З цим співрозмовником дзвінок недоступний.")
        return
    if why == "premium":
        await message.answer(
            "📞 <b>Анонімні дзвінки</b> — у 💎 Premium.\n\n"
            "Достатньо, щоб Premium був хоча б в одного з вас. Ваші Telegram-акаунти й номери залишаються прихованими.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Premium", callback_data="prem_open")]]),
        )
        return
    now = time.time()
    if now - call_last.get(uid, 0) < CALL_COOLDOWN:
        await message.answer("📞 Запрошення вже надіслано — зачекай хвилинку 🙂")
        return
    day = u.setdefault("call_day", {"d": None, "n": 0})
    if day.get("d") != today_str():
        day.update(d=today_str(), n=0)
    if day["n"] >= CALL_DAILY_MAX:
        await message.answer("📞 На сьогодні ліміт запрошень на дзвінок вичерпано. Завтра — знову 🙂")
        return
    day["n"] += 1
    call_last[uid] = now
    call_requests[pid] = (uid, now, kind)
    who = f"Друг {esc(u.get('nickname') or '')}" if kind == "friend" else "Співрозмовник"
    ok = await safe_send(
        pid,
        f"📞 <b>{who} пропонує анонімний дзвінок.</b>\n\n"
        f"Посилання відкриється в браузері. Твій Telegram і номер ніхто не побачить, камера вимкнена — вмикаєш лише ти сам(а). До {CALL_MINUTES} хв.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Прийняти", callback_data=f"call_yes:{uid}"),
            InlineKeyboardButton(text="❌ Ні", callback_data=f"call_no:{uid}"),
        ]]),
    )
    if ok:
        await message.answer("📞 Запрошення на дзвінок надіслано — чекаємо відповіді.")
    else:
        call_requests.pop(pid, None)
        await message.answer("❌ Не вдалося надіслати запрошення.")


@dp.callback_query(F.data.startswith("call_no:"))
async def call_no(call: types.CallbackQuery):
    me = call.from_user.id
    req = call_requests.pop(me, None)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await call.answer("Відхилено")
    if req:
        await safe_send(req[0], "📞 Співрозмовник поки не хоче дзвонити — нічого страшного, продовжуйте в чаті 🙂")


@dp.callback_query(F.data.startswith("call_yes:"))
async def call_yes(call: types.CallbackQuery):
    me = call.from_user.id
    try:
        caller = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer()
        return
    req = call_requests.pop(me, None)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    kind = req[2] if req and len(req) > 2 else "chat"
    still = (active_chats.get(me) == caller) if kind == "chat" else (
        caller in (init_user(me).get("friends") or set()) and me in (init_user(caller).get("friends") or set()))
    if not req or req[0] != caller or time.time() - req[1] > CALL_INVITE_TTL or not still:
        await call.answer("Запрошення вже неактуальне", show_alert=True)
        return
    u, p = init_user(me), init_user(caller)
    if call_block_reason(u, p):
        await call.answer("Дзвінок зараз недоступний", show_alert=True)
        return
    await call.answer()
    room = "c_" + secrets.token_hex(8)
    if not await livekit_api("CreateRoom", {"name": room, "empty_timeout": 120, "departure_timeout": 30, "max_participants": 2}):
        for x in (me, caller):
            await safe_send(x, "❌ Не вдалося створити дзвінок. Спробуйте трохи пізніше.")
        return
    end = time.time() + CALL_MINUTES * 60
    _call_cleanup()
    for x, other in ((me, caller), (caller, me)):
        tid = secrets.token_urlsafe(18)
        token = livekit_jwt({
            "sub": "p_" + secrets.token_hex(6), "name": "🎭",
            "video": {"room": room, "roomJoin": True, "canPublish": True, "canSubscribe": True, "canPublishData": False},
        }, CALL_MINUTES * 60 + 120)
        call_tickets[tid] = {"room": room, "token": token, "end": end, "lang": user_lang(x)}
        call_peers.setdefault(x, {})[other] = time.time()
        await safe_send(
            x,
            f"📞 <b>Дзвінок готовий!</b> Натисни кнопку — відкриється сторінка дзвінка.\n\n"
            f"⏱ До {CALL_MINUTES} хв · 📷 камера вимкнена, поки ти сам(а) не ввімкнеш.\n"
            "<i>Якщо мікрофон не працює — відкрий посилання в Chrome чи Safari.</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📞 Відкрити дзвінок", url=f"{PUBLIC_URL}/call/{tid}")]]),
        )
    stat_add("calls")
    task = asyncio.get_running_loop().create_task(_call_finish(room, me, caller, CALL_MINUTES * 60 + 20))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _call_finish(room: str, a: int, b: int, delay: float):
    await asyncio.sleep(delay)
    await livekit_api("DeleteRoom", {"room": room})
    for x, other in ((a, b), (b, a)):
        await safe_send(
            x, "📞 Дзвінок завершено. Якщо співрозмовник поводився погано — поскаржся, модератор перевірить.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚨 Поскаржитися на дзвінок", callback_data=f"call_rep:{other}")]]),
        )


@dp.callback_query(F.data.startswith("call_rep:"))
async def call_report(call: types.CallbackQuery):
    me = call.from_user.id
    try:
        other = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer()
        return
    if time.time() - (call_peers.get(me) or {}).get(other, 0) > 6 * 3600:
        await call.answer("Скарга на цей дзвінок уже недоступна", show_alert=True)
        return
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await call.answer()
    report_pending[me] = {"on": other, "msgs": [], "ts": time.time(), "call": True}
    await call.message.answer("🚨 Що сталося під час дзвінка? Обери причину:", reply_markup=report_reason_kb())


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
            room["last_msg"] = time.time()
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
        pu = users_db.get(user_id) or {}
        if getattr(message, "voice", None) and voice_mask_active(pu):
            await voice_preview(message, pu)
            return
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

    in_voice_chat = (chat_meta.get(user_id) or {}).get("mode") == "voice"
    if in_voice_chat and not getattr(message, "voice", None) and not getattr(message, "sticker", None):
        await message.answer("🎙 Це голосовий чат — тут тільки голосові (і стікери). Затисни 🎤 і говори 🙂")
        return
    if getattr(message, "voice", None) and (in_voice_chat or voice_mask_active(users_db.get(user_id) or {})):
        await relay_masked_voice(message, user_id, partner_id, force=in_voice_chat)
        return

    try:
        sent = await message.copy_to(
            chat_id=partner_id, protect_content=should_protect(users_db.get(user_id)), **reply_kwargs(message, partner_id)
        )
        remember_pair_msg(user_id, getattr(message, "message_id", None), partner_id, getattr(sent, "message_id", None))
        remember_recent(user_id, msg_mark(message))
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
    app.router.add_get("/call/{tid}", handle_call_page)
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
        types.BotCommand(command="search", description="🔍 Шукати співрозмовника"),
        types.BotCommand(command="next", description="⏭ Наступний співрозмовник"),
        types.BotCommand(command="stop", description="❌ Завершити чат"),
        types.BotCommand(command="call", description="📞 Дзвінок співрозмовнику (18+, Premium)"),
        types.BotCommand(command="friends", description="👫 Друзі"),
        types.BotCommand(command="filters", description="🎯 Фільтри пошуку"),
        types.BotCommand(command="interests", description="🧩 Пошук за інтересами"),
        types.BotCommand(command="voice", description="🎙 Маска голосу"),
        types.BotCommand(command="premium", description="💎 Premium"),
        types.BotCommand(command="confess", description="🤫 Анонімне зізнання"),
        types.BotCommand(command="edit_profile", description="✏️ Мій профіль"),
        types.BotCommand(command="help", description="🆘 Допомога"),
        types.BotCommand(command="rules", description="📜 Правила"),
    ]
    await bot.set_my_commands(default_commands, scope=types.BotCommandScopeDefault())
    DEFAULT_COMMANDS[:] = default_commands
    if LANG_RU_ENABLED:
        try:  # меню команд російською — для тих, у кого Telegram російською
            ru_commands = [types.BotCommand(command=c.command, description=tr_ru(c.description)) for c in default_commands]
            await bot.set_my_commands(ru_commands, scope=types.BotCommandScopeDefault(), language_code="ru")
            if en_available():
                en_commands = [types.BotCommand(command=c.command, description=tr_lang(c.description, "en")) for c in default_commands]
                await bot.set_my_commands(en_commands, scope=types.BotCommandScopeDefault(), language_code="en")
        except Exception as e:  # noqa: BLE001
            logging.warning("Команди російською/англійською: %s", e)

    if ADMIN_ID:
        admin_commands = default_commands + [
            types.BotCommand(command="admin", description="🔐 Адмін-панель"),
            types.BotCommand(command="addbalance", description="💰 Поповнити баланс користувачу"),
            types.BotCommand(command="confirm", description="🏦 Підтвердити оплату за кодом"),
            types.BotCommand(command="mono", description="🫙 Стан Банки monobank"),
            types.BotCommand(command="clearage", description="🧹 Очистити історію віку (ID)"),
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
            logging.info(calls_status())
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



# === КІНЕЦЬ part6.py ===
