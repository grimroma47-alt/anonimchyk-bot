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
    await send_banner(user_id, "letter")
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
    if key in HALLOWEEN_VOICES and halloween_active():
        return False  # 🎃 на Хелловін — безкоштовно
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
        sent = await bot.send_voice(
            partner_id,
            BufferedInputFile(masked, filename="voice.ogg"),
            caption="🎙 <i>голос змінено маскою</i>",
            protect_content=should_protect(u),
            **reply_kwargs(message, partner_id),
        )
        remember_pair_msg(user_id, getattr(message, "message_id", None), partner_id, getattr(sent, "message_id", None))
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
        lock = "💎" if voice_locked(u, key) else ("🎃" if key in HALLOWEEN_VOICES and halloween_active() else "")
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
    await send_banner(message.from_user.id, "voice")
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


def rooms_online() -> int:
    return sum(len(r["members"]) for r in rooms.values())


def rooms_button_label() -> str:
    n = rooms_online()
    return f"{BTN_ROOMS} · зараз {n}" if n >= 2 else BTN_ROOMS


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
        "🎲 «Страшна правда чи дія» — у грі під час чату\n\n"
        f"🌙 <b>{HW_NIGHT.day}.{HW_NIGHT.month:02d} · {HW_NIGHT.hour}:00 — Ніч страшилок!</b> "
        "Заходь, розкажи страшну історію анонімно. Усі, хто поспілкується "
        f"{HW_NIGHT.day} жовтня, отримають значок 🎃 у профілі назавжди."
    )


def hw_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Знайти співрозмовника", callback_data="wp_go:normal")],
        [InlineKeyboardButton(text="👻 Маска голосу", callback_data="vm_menu")],
    ])


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
    except TelegramAPIError as e:
        logging.info("Редагування не передано: %s", e)


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
        types.BotCommand(command="fun", description="✨ Розваги: гороскоп, карта дня"),
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
