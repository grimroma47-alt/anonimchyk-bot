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
    pool = TRUTHS if truth else DARES
    choices = [i for i in range(len(pool)) if (truth, i) not in used] or list(range(len(pool)))
    i = random.choice(choices)
    used.add((truth, i))
    await call.answer()
    head = "😇 <b>Правда</b>" if truth else "😈 <b>Дія</b>"
    await safe_send(call.from_user.id, f"{head} — твоє завдання:\n\n{pool[i]}", reply_markup=td_kb())
    await safe_send(partner, f"{head} — завдання для співрозмовника:\n\n{pool[i]}\n\n<i>Тепер твоя черга 👇</i>", reply_markup=td_kb())


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


# ---------------------------------------------------------------------------
# 💌 Нічний лист: пишеш анонімного листа — вранці він прилітає іншому автору листа
# ---------------------------------------------------------------------------
LETTER_MIN, LETTER_MAX = 20, 1000
LETTER_HOUR = int(os.getenv("LETTER_HOUR", "9"))  # о котрій доставляємо (Київ)
LETTER_MAX_AGE_DAYS = 3


class LetterStates(StatesGroup):
    text = State()


def _letter_adult(u: dict) -> bool | None:
    a = get_age_int(u)
    return None if a is None else a >= 18


@dp.message(F.text == BTN_LETTER)
@dp.message(Command("letter"))
async def letter_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if user_id in banned_users:
        return
    u = init_user(user_id)
    if _letter_adult(u) is None:
        await message.answer("💌 Щоб писати нічні листи, вкажи вік у профілі: /edit_profile")
        return
    today = kyiv_today().isoformat()
    if any(it["uid"] == user_id and it.get("day") == today for it in letters_db.get("items", [])) and not _is_admin(user_id):
        await message.answer("💌 Сьогодні ти вже написав(-ла) лист. Завтра вранці він знайде свого читача ✨")
        return
    await state.set_state(LetterStates.text)
    await message.answer(
        "💌 <b>Нічний лист</b>\n\n"
        "Напиши лист незнайомцю — про що завгодно: що на душі, порада, побажання, історія дня.\n"
        f"Завтра о {LETTER_HOUR}:00 він прилетить людині, яка теж написала лист, — а тобі прийде чийсь 💜\n\n"
        "• без імен, контактів і посилань;\n"
        f"• від {LETTER_MIN} до {LETTER_MAX} символів.\n\nНапиши лист одним повідомленням 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Скасувати", callback_data="letter_cancel")]]),
    )


@dp.callback_query(F.data == "letter_cancel")
async def letter_cancel(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await call.message.answer("Скасовано 💜", reply_markup=get_main_keyboard())
    await call.answer()


@dp.message(LetterStates.text)
async def letter_got(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Скасувати", callback_data="letter_cancel")]])
    if not text:
        await message.answer("💌 Лист приймаємо лише текстом 🙂", reply_markup=cancel_kb)
        return
    if len(text) < LETTER_MIN or len(text) > LETTER_MAX:
        await message.answer(f"💌 Від {LETTER_MIN} до {LETTER_MAX} символів, будь ласка (зараз {len(text)}).", reply_markup=cancel_kb)
        return
    if LINK_REGEX.search(text) or PHONE_REGEX.search(text):
        await message.answer("🚫 Без посилань, @ніків і телефонів — лист має лишатися анонімним 💜", reply_markup=cancel_kb)
        return
    await state.clear()
    u = init_user(message.from_user.id)
    letters_db.setdefault("items", []).append({
        "uid": message.from_user.id, "text": text, "at": time.time(), "day": kyiv_today().isoformat(),
        "adult": _letter_adult(u), "lang": u.get("lang", "uk"),
    })
    request_save()
    stat_add("letters")
    await message.answer(
        f"💌 Лист запечатано! Завтра о {LETTER_HOUR}:00 він полетить до незнайомця, а тобі прийде чийсь ✨",
        reply_markup=get_main_keyboard(),
    )


def letter_read_kb(author_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="💜 Дякую за лист", callback_data=f"letter_like:{author_id}"),
            InlineKeyboardButton(text="🚨 Поскаржитися", callback_data=f"letter_rep:{author_id}"),
        ]]
    )


def letters_match(items: list[dict]) -> list[tuple[dict, int]]:
    """Кожному листу — читач з іншого листа тієї ж групи (вік 18+/до 18, мова). Повертає [(лист, кому)]."""
    out = []
    groups: dict[tuple, list[dict]] = {}
    for it in items:
        groups.setdefault((it.get("adult"), it.get("lang", "uk")), []).append(it)
    for grp in groups.values():
        authors = list(dict.fromkeys(it["uid"] for it in grp))
        if len(authors) < 2:
            continue
        random.shuffle(authors)
        nxt = {authors[k]: authors[(k + 1) % len(authors)] for k in range(len(authors))}  # коло: ніхто не отримає свій
        for it in grp:
            out.append((it, nxt[it["uid"]]))
    return out


async def letters_deliver() -> int:
    items = [it for it in letters_db.get("items", []) if it.get("uid") not in banned_users]
    pairs = letters_match(items)
    sent_ids = set()
    n = 0
    for it, to in pairs:
        ok = await safe_send(
            to,
            "💌 <b>Тобі нічний лист від незнайомця</b>\n\n"
            f"<blockquote>{esc(it['text'])}</blockquote>\n\n"
            "Хочеш і собі написати? /letter",
            reply_markup=letter_read_kb(it["uid"]),
        )
        sent_ids.add(id(it))
        n += ok
        await asyncio.sleep(0.05)
    now = time.time()
    # недоставлені (не знайшлося пари) лишаються, але не довше LETTER_MAX_AGE_DAYS
    letters_db["items"] = [
        it for it in letters_db.get("items", [])
        if id(it) not in sent_ids and now - it.get("at", now) < LETTER_MAX_AGE_DAYS * 86400
    ]
    request_save()
    return n


async def letters_tick():
    if not _contest_can_write():
        return
    now = datetime.now(KYIV_TZ)
    today = now.date().isoformat()
    if now.hour == LETTER_HOUR and letters_db.get("delivered_day") != today:
        letters_db["delivered_day"] = today
        request_save()
        n = await letters_deliver()
        if ADMIN_ID and n:
            await safe_send(ADMIN_ID, f"💌 Нічних листів доставлено: <b>{n}</b>")


@dp.callback_query(F.data.startswith("letter_like:"))
async def letter_like(call: types.CallbackQuery):
    try:
        author = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer()
        return
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await safe_send(author, "💜 Твій нічний лист знайшов свого читача — і йому сподобалось ✨")
    await call.answer("Передали автору 💜")


@dp.callback_query(F.data.startswith("letter_rep:"))
async def letter_report(call: types.CallbackQuery):
    try:
        author = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer()
        return
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    if ADMIN_ID:
        await safe_send(
            ADMIN_ID,
            f"🚨 Скарга на нічний лист від {esc(admin_label(author))}:\n\n<blockquote>{esc((call.message.text or '')[:900])}</blockquote>",
        )
    await call.answer("Дякуємо, модератор перевірить 🛡", show_alert=True)


@dp.message(Command("letterdeliver"))
async def letter_admin_deliver(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    n = await letters_deliver()
    await message.answer(f"💌 Доставлено зараз: {n}. У черзі лишилось: {len(letters_db.get('items', []))}")


# ---------------------------------------------------------------------------
# 🎙 Маска голосу: голосові в анонімному чаті надходять співрозмовнику зміненим голосом.
# Якщо змінити не вдалося — оригінал НЕ надсилаємо (анонімність понад усе).
# Поза чатом надіслане боту голосове повертається як 🎧 прослуховування.
# ---------------------------------------------------------------------------
VOICE_PRESETS = {  # ключ -> (назва, фільтр ffmpeg)
    "low": ("🐻 Низький", "rubberband=pitch=0.8"),
    "high": ("🐭 Високий", "rubberband=pitch=1.25"),
    "female": ("👩 Жіночий", "rubberband=pitch=1.5:formant=shifted,highpass=f=160"),
    "male": ("👨 Чоловічий", "rubberband=pitch=0.72,lowpass=f=6000"),
    "robot": ("🤖 Робот", "afftfilt=real='hypot(re,im)*sin(0)':imag='hypot(re,im)*cos(0)':win_size=512:overlap=0.75,chorus=0.6:0.8:30:0.4:0.25:2"),
    "radio": ("📻 Рація", "highpass=f=350,lowpass=f=3200,acrusher=bits=8:mix=0.25:mode=log,volume=1.6"),
    "ghost": ("👻 Привид", "rubberband=pitch=0.88,aecho=0.8:0.85:70|140:0.45|0.3"),
    "cartoon": ("🐿 Мультик", "rubberband=pitch=1.75"),
    "monster": ("👾 Монстр", "rubberband=pitch=0.58,flanger=delay=2:depth=3,lowpass=f=5000"),
    "alien": ("👽 Прибулець", "rubberband=pitch=1.35,vibrato=f=7:d=0.35,aphaser=type=t:speed=0.6"),
    "cave": ("🕳 Печера", "aecho=0.8:0.9:180|360|540:0.5|0.35|0.2,lowpass=f=5000"),
    "demon": ("😈 Демон", "rubberband=pitch=0.5,aecho=0.8:0.8:40:0.5,lowpass=f=4000,volume=1.4"),
}
VOICE_PITCH_MIN, VOICE_PITCH_MAX = -8, 8  # «Свій тон» у півтонах
VOICE_MAX_SECONDS = int(os.getenv("VOICE_MAX_SECONDS", "120"))
_ffmpeg_path: str | None = None


VOICE_FREE = {"low", "high", "robot", "radio"}  # безкоштовні завжди; решта — Premium з PREMIUM_VOICES_AT


def _parse_dt_env(name: str, default: str) -> datetime:
    raw = os.getenv(name, default)
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        dt = datetime.fromisoformat(default)
    return dt if dt.tzinfo else dt.replace(tzinfo=KYIV_TZ)


PREMIUM_VOICES_AT = _parse_dt_env("PREMIUM_VOICES_AT", "2026-10-30T00:00")


def voice_locked(u: dict, key: str) -> bool:
    """Чи закритий голос для цієї людини (Premium-голоси вмикаються з PREMIUM_VOICES_AT)."""
    if key in VOICE_FREE or key not in VOICE_PRESETS:
        return False
    return datetime.now(KYIV_TZ) >= PREMIUM_VOICES_AT and not is_premium(u)


def voice_mask_active(u: dict) -> bool:
    m = u.get("voice_mask")
    return m in VOICE_PRESETS or (m == "custom" and int(u.get("voice_pitch") or 0) != 0)


def voice_mask_label(u: dict, for_voice_chat: bool = False) -> str:
    m = u.get("voice_mask")
    if for_voice_chat and not voice_mask_active(u):
        return VOICE_PRESETS["low"][0]
    if m in VOICE_PRESETS and voice_locked(u, m):
        return VOICE_PRESETS["low"][0]
    if m in VOICE_PRESETS:
        return VOICE_PRESETS[m][0]
    if m == "custom" and int(u.get("voice_pitch") or 0):
        return f"🎚 Свій тон ({int(u['voice_pitch']):+d})"
    return "вимк"


def voice_filter(u: dict, force: bool = False) -> str | None:
    m = u.get("voice_mask")
    if m in VOICE_PRESETS and voice_locked(u, m):
        return VOICE_PRESETS["low"][1]  # Premium закінчився — м'яко переходимо на безкоштовний голос
    if m in VOICE_PRESETS:
        return VOICE_PRESETS[m][1]
    if m == "custom":
        n = int(u.get("voice_pitch") or 0)
        if n:
            return f"rubberband=pitch={2 ** (n / 12):.4f}"
    return VOICE_PRESETS["low"][1] if force else None


def ffmpeg_exe() -> str | None:
    global _ffmpeg_path
    if _ffmpeg_path:
        return _ffmpeg_path
    import shutil
    path = shutil.which("ffmpeg")
    if not path:
        try:
            import imageio_ffmpeg  # pip install imageio-ffmpeg — сам привозить ffmpeg
            path = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as e:  # noqa: BLE001
            logging.warning("🎙 ffmpeg не знайдено: %s", e)
            path = None
    _ffmpeg_path = path
    return path


async def mask_voice_bytes(data: bytes, af: str) -> bytes | None:
    exe = ffmpeg_exe()
    if not exe or not af:
        return None
    proc = await asyncio.create_subprocess_exec(
        exe, "-v", "error", "-i", "pipe:0", "-ac", "1", "-ar", "48000", "-af", af,
        "-c:a", "libopus", "-b:a", "48k", "-f", "ogg", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(data), timeout=40)
    except asyncio.TimeoutError:
        proc.kill()
        return None
    if proc.returncode != 0 or len(out) < 200:
        logging.warning("🎙 ffmpeg: %s", (err or b"")[:300])
        return None
    return out


async def _masked_from_message(message: types.Message, u: dict, force: bool = False) -> bytes | None:
    try:
        buf = await bot.download(message.voice)
        raw = buf.read() if buf else b""
        return await mask_voice_bytes(raw, voice_filter(u, force)) if raw else None
    except Exception as e:  # noqa: BLE001
        logging.warning("🎙 Маска голосу: %s", e)
        return None


async def relay_masked_voice(message: types.Message, user_id: int, partner_id: int, force: bool = False):
    u = users_db.get(user_id) or {}
    voice = message.voice
    if voice.duration and voice.duration > VOICE_MAX_SECONDS:
        await message.answer(f"🎙 З маскою голосу — до {VOICE_MAX_SECONDS // 60} хв. Запиши коротше 🙂")
        return
    masked = await _masked_from_message(message, u, force)
    if not masked:
        await message.answer(
            "🎙 Не вдалося змінити голос — повідомлення НЕ надіслано, твій голос у безпеці 🛡\n"
            "Спробуй ще раз або вимкни маску: /voice"
        )
        return
    try:
        await bot.send_voice(
            partner_id,
            BufferedInputFile(masked, filename="voice.ogg"),
            caption="🎙 <i>голос змінено маскою</i>",
            protect_content=should_protect(u),
        )
        on_chat_message(user_id)
        stat_add("voice_mask")
    except TelegramAPIError:
        end_chat(user_id)
        await message.answer("Не вдалося доставити повідомлення, чат завершено.", reply_markup=get_main_keyboard())


async def voice_preview(message: types.Message, u: dict):
    voice = message.voice
    if voice.duration and voice.duration > 30:
        await message.answer("🎧 Для прослуховування надішли голосове до 30 секунд 🙂")
        return
    masked = await _masked_from_message(message, u)
    if not masked:
        await message.answer("🎧 Не вдалося обробити голос, спробуй ще раз.")
        return
    await bot.send_voice(
        message.from_user.id,
        BufferedInputFile(masked, filename="preview.ogg"),
        caption=f"🎧 Так тебе почують з маскою «{voice_mask_label(u)}». Змінити: /voice",
    )


def voice_mask_kb(u: dict):
    cur = u.get("voice_mask") or "off"
    def b(key, label):
        lock = "💎" if voice_locked(u, key) else ""
        return InlineKeyboardButton(text=("✅ " if cur == key else "") + label + lock, callback_data=f"vm_set:{key}")
    keys = list(VOICE_PRESETS)
    rows = [[b("off", "🎙 Мій голос")]]
    for i in range(0, len(keys), 3):
        rows.append([b(k, VOICE_PRESETS[k][0]) for k in keys[i:i + 3]])
    n = int(u.get("voice_pitch") or 0)
    rows.append([
        InlineKeyboardButton(text="➖", callback_data="vm_pitch:-1"),
        InlineKeyboardButton(text=("✅ " if cur == "custom" else "") + f"🎚 Свій тон: {n:+d}", callback_data="vm_set:custom"),
        InlineKeyboardButton(text="➕", callback_data="vm_pitch:1"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def voice_mask_text() -> str:
    return (
        "🎙 <b>Маска голосу</b>\n\n"
        "Надсилай голосові в анонімному чаті — співрозмовник почує змінений голос, а не твій справжній.\n"
        "Якщо змінити не вийде, бот оригінал не надішле 🛡\n\n"
        "🎚 <b>Свій тон</b> — підкрути ➖/➕ як подобається.\n"
        "🎧 <b>Прослухати</b>: поза чатом надішли боту голосове — він поверне, як тебе почують.\n\nОбери маску:"
    )


@dp.message(Command("voice"))
async def voice_mask_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    u = init_user(message.from_user.id)
    await message.answer(voice_mask_text(), reply_markup=voice_mask_kb(u))


@dp.callback_query(F.data == "vm_menu")
async def voice_mask_menu(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    await call.message.answer(voice_mask_text(), reply_markup=voice_mask_kb(u))
    await call.answer()


@dp.callback_query(F.data.startswith("vm_set:"))
async def voice_mask_set(call: types.CallbackQuery):
    key = call.data.split(":", 1)[1]
    if key not in VOICE_PRESETS and key not in ("off", "custom"):
        await call.answer()
        return
    u = init_user(call.from_user.id)
    if voice_locked(u, key):
        await call.answer("💎 Цей голос — у Premium. Безкоштовні: низький, високий, робот, рація і «Свій тон».", show_alert=True)
        await call.message.answer(
            "💎 Усі 12 голосів — з Premium.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Premium", callback_data="prem_open")]]),
        )
        return
    if key == "custom" and not int(u.get("voice_pitch") or 0):
        u["voice_pitch"] = -3  # стартова точка, щоб одразу було чутно
    u["voice_mask"] = None if key == "off" else key
    request_save()
    try:
        await call.message.edit_reply_markup(reply_markup=voice_mask_kb(u))
    except TelegramAPIError:
        pass
    await call.answer("🎙 Маску вимкнено" if key == "off" else f"🎙 Маска: {voice_mask_label(u)}")


@dp.callback_query(F.data.startswith("vm_pitch:"))
async def voice_mask_pitch(call: types.CallbackQuery):
    step = 1 if call.data.endswith(":1") else -1
    u = init_user(call.from_user.id)
    n = max(VOICE_PITCH_MIN, min(VOICE_PITCH_MAX, int(u.get("voice_pitch") or 0) + step))
    u["voice_pitch"] = n
    u["voice_mask"] = "custom" if n else None
    request_save()
    try:
        await call.message.edit_reply_markup(reply_markup=voice_mask_kb(u))
    except TelegramAPIError:
        pass
    await call.answer(f"🎚 Тон: {n:+d}" if n else "🎙 Тон 0 — твій звичайний голос")

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
    await message.answer(
        "🤫 <b>Анонімне зізнання</b>\n\n"
        "Напиши те, що не можеш сказати нікому. Ми опублікуємо це в каналі "
        f"<a href=\"{confess_channel_link()}\">Анонімні зізнання</a> — без імені, ніку й будь-яких даних про тебе.\n\n"
        "• не вказуй імена, адреси, телефони й @ніки — ні свої, ні чужі;\n"
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
    return f"🤫 <b>Зізнання #{no}</b>\n\n{esc(text)}\n\n<i>Розкажи своє — анонімно 👇</i>"


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
        msg = await bot.send_message(CONFESS_CHANNEL, confess_post_text(no, item["text"]), reply_markup=confess_post_kb())
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
        types.BotCommand(command="voice", description="🎙 Маска голосу"),
        types.BotCommand(command="game", description="🎲 Гра в чаті"),
        types.BotCommand(command="where", description="📍 Де я зараз"),
        types.BotCommand(command="letter", description="💌 Нічний лист"),
        types.BotCommand(command="confess", description="🤫 Анонімне зізнання"),
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



# === КІНЕЦЬ part6.py ===
