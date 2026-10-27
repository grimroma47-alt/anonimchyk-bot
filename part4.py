# ---------------------------------------------------------------------------
# Статистика: щоденні лічильники для адміна
# ---------------------------------------------------------------------------
STATS_KEEP_DAYS = 62  # скільки днів історії тримаємо в базі


def _new_stats_day() -> dict:
    return {
        "new": 0,  # нових користувачів
        "active": set(),  # хто щось робив у боті (унікальні)
        "chats": 0,  # з'єднань усього
        "real_chats": 0,  # чатів від 1 хв, де обидва писали
        "flirt": 0,
        "interest": 0,
        "friends": 0,
        "msgs": 0,  # повідомлень у чатах 1-на-1
        "topup": {},  # джерело -> грн
        "topup_n": 0,  # кількість оплат
        "spent": {},  # на що витрачали баланс -> грн
        "daily_paid": 0.0,  # роздано щоденними бонусами
        "tasks_paid": 0.0,  # роздано за завдання
        "tasks_done": 0,
        "new_ids": [],  # user_id новачків за день (для списку в адмінці)
    }


def stats_day(day: str | None = None) -> dict:
    day = day or today_str()
    d = stats_days.get(day)
    if d is None:
        d = stats_days[day] = _new_stats_day()
        cutoff = (kyiv_today() - timedelta(days=STATS_KEEP_DAYS)).isoformat()
        for old in [k for k in stats_days if k < cutoff]:
            stats_days.pop(old, None)
    return d


def stat_add(key: str, n: float = 1, sub: str | None = None):
    try:
        d = stats_day()
        if sub is None:
            d[key] = d.get(key, 0) + n
        else:
            bucket = d.setdefault(key, {})
            bucket[sub] = bucket.get(sub, 0) + n
    except Exception as e:  # noqa: BLE001 — статистика ніколи не має ламати бота
        logging.warning("Статистика: %s", e)


def record_topup(source: str, amount: float):
    stat_add("topup", float(amount), sub=source)
    stat_add("topup_n")


def record_spend(category: str, amount: float):
    stat_add("spent", float(amount), sub=category)


# ---------------------------------------------------------------------------
# Щоденні завдання: 3 на день, нагорода на баланс, з Premium ×2
# ---------------------------------------------------------------------------
REAL_CHAT_SECONDS = 60  # чат зараховується, якщо тривав від 1 хв і обидва написали
LONG_CHAT_SECONDS = 300
TASKS_PER_DAY = 3
TASKS_ALL_BONUS = float(os.getenv("TASKS_ALL_BONUS", "2"))  # грн за виконання всіх завдань дня
TASKS_PREMIUM_MULT = 2

# ключ -> (назва, подія, скільки треба, нагорода грн)
TASK_POOL = {
    "daily": ("🎁 Забери щоденний бонус", "daily", 1, 1),
    "chats3": ("💬 Поспілкуйся в 3 чатах", "chat", 3, 1),
    "rate2": ("👍 Оціни 2 співрозмовників", "rate", 2, 1),
    "msgs20": ("✉️ Надішли 20 повідомлень у чатах", "msg", 20, 1),
    "longchat": ("⏱ Поспілкуйся з кимось 5 хвилин", "long_chat", 1, 2),
    "flirt1": ("❤️ Поспілкуйся у флірт-чаті", "flirt_chat", 1, 1),
    "interest1": ("🧩 Поспілкуйся за інтересами", "interest_chat", 1, 1),
    "gift1": ("🎁 Подаруй подарунок співрозмовнику", "gift", 1, 2),
    "room1": ("👥 Напиши в кімнаті за інтересами", "room_msg", 1, 1),
}


def ensure_tasks(user_id: int, u: dict) -> list[str]:
    """Видає завдання на сьогодні (однакові протягом дня, у кожного свій набір)."""
    today = today_str()
    if u.get("tasks_day") != today or not u.get("tasks_list"):
        pool = [k for k in TASK_POOL if k != "daily"]
        if flirt_block_reason(u) is not None:
            pool.remove("flirt1")  # флірт лише з 18 і з заповненим профілем
        rng = random.Random(f"{today}:{user_id}")
        u["tasks_day"] = today
        u["tasks_list"] = ["daily"] + rng.sample(pool, TASKS_PER_DAY - 1)
        u["tasks_progress"] = {}
        u["tasks_done"] = set()
        u["tasks_bonus"] = False
    return [k for k in u["tasks_list"] if k in TASK_POOL]


def _task_mult(u: dict) -> int:
    return TASKS_PREMIUM_MULT if is_premium(u) else 1


def _notify_later(user_id: int, text: str):
    try:
        asyncio.get_running_loop().create_task(safe_send(user_id, text))
    except RuntimeError:
        pass


def task_event(user_id: int, event: str, n: int = 1):
    """Зараховує дію в щоденні завдання; за виконане — нагорода і повідомлення."""
    try:
        u = users_db.get(user_id)
        if u is None or user_id in banned_users:
            return
        keys = ensure_tasks(user_id, u)
        mult = _task_mult(u)
        notes = []
        for k in keys:
            title, ev, goal, reward = TASK_POOL[k]
            if ev != event or k in u["tasks_done"]:
                continue
            done_now = u["tasks_progress"].get(k, 0) + n
            u["tasks_progress"][k] = min(done_now, goal)
            if done_now >= goal:
                u["tasks_done"].add(k)
                pay = reward * mult
                u["balance"] += pay
                stat_add("tasks_paid", pay)
                stat_add("tasks_done")
                notes.append(f"✅ Завдання виконано: {title} — <b>+{pay:g} грн</b>")
        if not notes:
            return
        done_cnt = len([k for k in keys if k in u["tasks_done"]])
        if done_cnt >= len(keys) and not u.get("tasks_bonus"):
            u["tasks_bonus"] = True
            bonus = TASKS_ALL_BONUS * mult
            if bonus:
                u["balance"] += bonus
                stat_add("tasks_paid", bonus)
                notes.append(f"🏆 Усі завдання на сьогодні виконано! Бонус <b>+{bonus:g} грн</b>")
        else:
            notes.append(f"📋 Виконано {done_cnt}/{len(keys)} — решта в «{BTN_TASKS}»")
        request_save()
        if u.get("notify_tasks", True):
            _notify_later(user_id, "\n".join(notes))
    except Exception as e:  # noqa: BLE001 — завдання ніколи не мають ламати бота
        logging.warning("Завдання: %s", e)


def on_chat_started(a: int, b: int, mode: str):
    chat_log_start(a, b, mode)
    hw_mark_badge(a, b)
    now = time.time()
    for x in (a, b):
        chat_meta[x] = {"start": now, "mode": mode, "msgs": 0}
    stat_add("chats")
    if mode == "flirt":
        stat_add("flirt")
    elif mode.startswith("int:"):
        stat_add("interest")
    elif mode == "friend":
        stat_add("friends")
    elif mode == "voice":
        stat_add("voice_chats")


def on_chat_message(user_id: int):
    m = chat_meta.get(user_id)
    if m is not None:
        m["msgs"] += 1
    stat_add("msgs")
    task_event(user_id, "msg")


# ---------------------------------------------------------------------------
# 👑 Засновники: перші FOUNDERS_LIMIT людей, які справді поспілкувались, — значок назавжди
# ---------------------------------------------------------------------------
FOUNDERS_LIMIT = int(os.getenv("FOUNDERS_LIMIT", "100"))


def founders_count() -> int:
    return max((u.get("founder_no") or 0 for u in users_db.values()), default=0)


def founders_left() -> int:
    return max(0, FOUNDERS_LIMIT - founders_count())


def founder_promo_line() -> str:
    left = founders_left()
    if not left:
        return ""
    return (
        f"👑 Перші {FOUNDERS_LIMIT} учасників отримують значок <b>засновника</b> назавжди — "
        f"залишилось <b>{left}</b> {_plural(left, 'місце', 'місця', 'місць')}! Поспілкуйся хоча б раз, щоб забрати своє."
    )


def maybe_make_founder(user_id: int) -> bool:
    u = users_db.get(user_id)
    if u is None or u.get("founder_no") or user_id <= 0 or user_id == ADMIN_ID or user_id in banned_users:
        return False
    no = founders_count() + 1
    if no > FOUNDERS_LIMIT:
        return False
    u["founder_no"] = no
    request_save()
    _notify_later(
        user_id,
        f"👑 <b>Вітаємо! Ти — засновник ANONimchyk №{no} з {FOUNDERS_LIMIT}!</b>\n\n"
        "• Значок 👑 тепер у твоєму профілі назавжди — співрозмовники його бачать.\n"
        "• Коли вийде велике оновлення «Стиль ANONimchyk», ти автоматично отримаєш "
        "ексклюзивний 🎁 <b>Пак засновника</b>, який більше ніколи не продаватиметься.\n\n"
        "Дякуємо, що ти з нами з самого початку 💜",
    )
    return True


@dp.message(Command("founders"))
async def founders_cmd(message: types.Message):
    u = init_user(message.from_user.id)
    left = founders_left()
    if u.get("founder_no"):
        mine = f"Ти — засновник 👑 №{u['founder_no']}. Дякуємо! 💜"
    elif left:
        mine = "Щоб стати засновником — знайди співрозмовника і поспілкуйся хоча б хвилину 💬"
    else:
        mine = "Усі місця засновників уже зайняті — але попереду ще багато подарунків 🎁"
    await message.answer(
        f"👑 <b>Засновники ANONimchyk</b>\n\n"
        f"Перші {FOUNDERS_LIMIT} учасників, які справді поспілкувались у боті, отримують:\n"
        "• значок 👑 у профілі назавжди;\n"
        "• ексклюзивний 🎁 Пак засновника в день запуску «Стилю ANONimchyk».\n\n"
        f"Зайнято: <b>{founders_count()}</b> з {FOUNDERS_LIMIT} · залишилось <b>{left}</b>\n\n{mine}"
    )


def on_chat_ended(a: int, b: int):
    chat_log_end(a, b)
    try:
        ma, mb = chat_meta.pop(a, None), chat_meta.pop(b, None)
        if ma is None or mb is None:
            return
        last_chat_mode[a] = ma["mode"]
        last_chat_mode[b] = mb["mode"]
        duration = time.time() - min(ma["start"], mb["start"])
        # оцінка йде в топ тижня лише після справжньої розмови: обидва писали
        rated = duration if ma["msgs"] >= 1 and mb["msgs"] >= 1 else 0.0
        last_chat_seconds[a] = last_chat_seconds[b] = rated
        if ma["msgs"] >= 2 and mb["msgs"] >= 2:
            try:
                txt = compat_text(users_db.get(a) or {}, users_db.get(b) or {}, duration, min(ma["msgs"], mb["msgs"]))
                compat_last[a] = compat_last[b] = txt
            except Exception as e:  # noqa: BLE001
                logging.warning("Сумісність: %s", e)
        if duration < REAL_CHAT_SECONDS or ma["msgs"] < 1 or mb["msgs"] < 1:
            return  # «натиснув і втік» не рахується — щоб завдання не накручували
        stat_add("real_chats")
        long_chat = duration >= LONG_CHAT_SECONDS and ma["msgs"] >= 3 and mb["msgs"] >= 3
        mode = ma["mode"]
        for x in (a, b):
            maybe_make_founder(x)
            contest_on_real_chat(x)
            task_event(x, "chat")
            if mode == "flirt":
                task_event(x, "flirt_chat")
            elif mode.startswith("int:"):
                task_event(x, "interest_chat")
            if long_chat:
                task_event(x, "long_chat")
    except Exception as e:  # noqa: BLE001
        logging.warning("Кінець чату (статистика): %s", e)


def tasks_text(user_id: int, u: dict) -> str:
    keys = ensure_tasks(user_id, u)
    mult = _task_mult(u)
    lines = ["📋 <b>Завдання на сьогодні</b>\n"]
    for k in keys:
        title, _ev, goal, reward = TASK_POOL[k]
        pay = reward * mult
        if k in u["tasks_done"]:
            lines.append(f"✅ <s>{title}</s> — +{pay:g} грн")
        else:
            prog = u["tasks_progress"].get(k, 0)
            counter = f" ({prog}/{goal})" if goal > 1 else ""
            lines.append(f"⬜ {title}{counter} — +{pay:g} грн")
    lines.append("")
    if u.get("tasks_bonus"):
        lines.append("🏆 Бонус за всі завдання отримано — ти молодець!")
    elif TASKS_ALL_BONUS:
        lines.append(f"🏆 Виконай усі — і отримай ще <b>+{TASKS_ALL_BONUS * mult:g} грн</b>")
    lines.append("💎 У тебе Premium — нагороди ×2" if mult > 1 else "💎 З Premium усі нагороди ×2")
    lines.append("")
    lines.append(
        "<i>Чат зараховується, якщо триває від 1 хв і ви обоє щось написали. "
        "Нові завдання — щодня опівночі за Києвом.</i>"
    )
    return "\n".join(lines)


@dp.message(F.text == BTN_TASKS)
@dp.message(Command("tasks"))
async def tasks_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await send_banner(message.from_user.id, "tasks")
    u = init_user(message.from_user.id)
    await message.answer(tasks_text(message.from_user.id, u))


# ---------------------------------------------------------------------------
# Рейтинг і інтереси профілю: кого кому підбирати першим
# ---------------------------------------------------------------------------
MAX_PROFILE_INTERESTS = 3
MATCH_SCORE_MAX = 4  # 2 — однаковий «рівень» рейтингу, +1 — є спільний інтерес, +1 — однаковий настрій


def match_score(u: dict, p: dict, mode: str) -> int:
    """Чим більше балів, тим краща пара. Люди з низьким рейтингом спершу потрапляють одне до одного."""
    score = 2 if is_low_rated(u) == is_low_rated(p) else 0
    if not mode.startswith("int:") and (u.get("interests") or set()) & (p.get("interests") or set()):
        score += 1
    if current_mood(u) and current_mood(u) == current_mood(p):
        score += 1  # 🎭 на одній хвилі
    return score


def rating_line(u: dict) -> str:
    up, down = u.get("rating_up", 0), u.get("rating_down", 0)
    if not up + down:
        return "ще немає оцінок"
    return f"👍 {up} · 👎 {down} ({rating_percent(u)}%)"


def interests_line(u: dict) -> str:
    keys = [k for k in INTEREST_LABELS if k in (u.get("interests") or set())]
    return ", ".join(INTEREST_LABELS[k] for k in keys) if keys else "не обрано"


async def send_interest_notes(user_id: int, u: dict, partner_id: int, p: dict):
    u_open, p_open = not u.get("hide_interests"), not p.get("hide_interests")
    common = [k for k in INTEREST_LABELS if k in (u.get("interests") or set()) & (p.get("interests") or set())]
    if common and u_open and p_open:
        note = "🧩 Спільні інтереси: <b>" + ", ".join(INTEREST_LABELS[k] for k in common) + "</b> — є з чого почати 😉"
        await safe_send(user_id, note)
        await safe_send(partner_id, note)
        return
    for me, other, is_open in ((user_id, p, p_open), (partner_id, u, u_open)):
        if is_open and other.get("interests"):
            await safe_send(me, f"🧩 Інтереси співрозмовника: {interests_line(other)}")


# ---------------------------------------------------------------------------
# 🎭 Мій настрій · 🎵 Пісня настрою · 💬 Тема дня
# ---------------------------------------------------------------------------
MOODS = {
    "happy": "😊 Гарний настрій",
    "calm": "😌 Спокійний",
    "sleepy": "🥱 Сонний",
    "sad": "😔 Сумно",
    "laugh": "😂 Хочу посміятись",
    "talk": "💬 Хочу виговоритись",
    "deep": "🌌 На глибокі розмови",
    "bored": "🙃 Нудно",
}
MOOD_TTL = 12 * 3600  # настрій «на сьогодні»: діє 12 годин
SONG_TTL = 24 * 3600  # пісня настрою: 24 години
SONG_MAX = 60
TOPIC_COOLDOWN = 20  # секунд між «новими темами» в одному чаті
_SONG_BAD = re.compile(r"(https?://|www\.|t\.me|@\w|\.com|\.ua|\.ru)", re.I)

DAILY_TOPICS = [
    "Яка пісня зараз крутиться у тебе в голові?",
    "Куди б ти поїхав(-ла) прямо зараз, якби квиток був безкоштовний?",
    "Яка найдивніша їжа, яку ти куштував(-ла)?",
    "Що тебе сьогодні порадувало, навіть якась дрібниця?",
    "Ти більше «сова» чи «жайворонок»?",
    "Який фільм ти можеш переглядати безкінечно?",
    "Якби ти міг(-ла) мати одну суперсилу — яку б обрав(-ла)?",
    "Що б ти сказав(-ла) собі п'ять років тому?",
    "Який для тебе ідеальний вихідний?",
    "Котики чи собаки — і чому?",
    "Яке хобі ти давно хочеш спробувати?",
    "Який найсмішніший випадок стався з тобою цього року?",
    "Яка пора року твоя — і за що ти її любиш?",
    "Чого ти точно не пробачиш у дружбі?",
    "Чай чи кава? І як саме ти його п'єш?",
    "Яка книга або серіал змінили твій погляд на щось?",
    "Опиши свій день трьома емодзі.",
    "Що тебе заспокоює, коли все бісить?",
    "Яка в тебе «дивна» звичка, про яку мало хто знає?",
    "Якби ти відкрив(-ла) свій заклад — що б це було?",
    "Яка мрія в тебе зараз на першому місці?",
    "Який найкращий подарунок ти колись отримував(-ла)?",
    "Що б ти робив(-ла), якби завтра не треба було на роботу чи навчання?",
    "Який звук або запах нагадує тобі дитинство?",
    "Ти віриш у кохання з першого погляду?",
    "Що тобі легше: почати розмову чи підтримувати її?",
    "Яке місто в Україні ти любиш найбільше?",
    "Що ти вмієш готувати найкраще?",
    "Чого ти навчився(-лася) за останній місяць?",
    "Яке маленьке рішення колись сильно змінило твоє життя?",
]
topic_last: dict[int, float] = {}  # user_id -> коли востаннє просив нову тему в чаті


def current_mood(u: dict) -> str | None:
    key = u.get("mood")
    if key in MOODS and time.time() - (u.get("mood_at") or 0) < MOOD_TTL:
        return key
    return None


def current_song(u: dict) -> str | None:
    song = u.get("song")
    if song and time.time() - (u.get("song_at") or 0) < SONG_TTL:
        return song
    return None


def mood_line(u: dict) -> str:
    key = current_mood(u)
    return MOODS[key] if key else "не вказано"


def topic_of_day() -> str:
    return DAILY_TOPICS[kyiv_today().toordinal() % len(DAILY_TOPICS)]


def random_topic(exclude: str | None = None) -> str:
    pool = [t for t in DAILY_TOPICS if t != exclude]
    return random.choice(pool)


def mood_text(u: dict) -> str:
    song = current_song(u)
    same = 0
    key = current_mood(u)
    if key:
        same = sum(1 for uid in queue if current_mood(users_db.get(uid, {})) == key)
    lines = [
        "🎭 <b>Мій настрій</b>\n",
        "Обери, як ти зараз. Співрозмовник побачить це на початку чату, "
        "а бот спершу шукатиме когось на одній хвилі.\n",
        f"Зараз: <b>{mood_line(u)}</b>" + (" <i>(діє 12 год)</i>" if key else ""),
        "🎵 Пісня настрою: " + (f"«{esc(song)}»" if song else "не вказано"),
    ]
    if key and same:
        lines.append(f"🔍 З таким самим настроєм зараз шукають: <b>{same}</b>")
    lines.append(f"\n💬 <b>Тема дня:</b> {topic_of_day()}")
    return "\n".join(lines)


def mood_keyboard(u: dict) -> InlineKeyboardMarkup:
    cur = current_mood(u)
    btns = [
        InlineKeyboardButton(text=("✅ " if k == cur else "") + label, callback_data=f"mood_set:{k}")
        for k, label in MOODS.items()
    ]
    rows = [btns[i : i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton(text="🎵 Пісня настрою", callback_data="mood_song")])
    if cur or current_song(u):
        rows.append([InlineKeyboardButton(text="🧹 Скинути настрій і пісню", callback_data="mood_clear")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("mood"))
async def mood_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    u = init_user(message.from_user.id)
    await message.answer(mood_text(u), reply_markup=mood_keyboard(u))


@dp.callback_query(F.data == "mood_open")
async def mood_open(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    await call.message.answer(mood_text(u), reply_markup=mood_keyboard(u))
    await call.answer()


@dp.callback_query(F.data.startswith("mood_set:"))
async def mood_set(call: types.CallbackQuery):
    key = call.data.split(":", 1)[1]
    if key not in MOODS:
        await call.answer("Невідомий настрій", show_alert=True)
        return
    u = init_user(call.from_user.id)
    u["mood"], u["mood_at"] = key, time.time()
    request_save()
    try:
        await call.message.edit_text(mood_text(u), reply_markup=mood_keyboard(u))
    except TelegramAPIError:
        pass
    await call.answer(f"Настрій: {MOODS[key]}")


@dp.callback_query(F.data == "mood_clear")
async def mood_clear(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    u["mood"], u["mood_at"], u["song"], u["song_at"] = None, 0.0, None, 0.0
    request_save()
    try:
        await call.message.edit_text(mood_text(u), reply_markup=mood_keyboard(u))
    except TelegramAPIError:
        pass
    await call.answer("Скинуто")


@dp.callback_query(F.data == "mood_song")
async def mood_song_ask(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id in active_chats:
        await call.answer("Пісню можна змінити після чату 🙂", show_alert=True)
        return
    await state.set_state(MoodStates.song)
    await call.message.answer(
        "🎵 Напиши пісню, яка зараз у тебе в настрої — виконавця і назву "
        f"(до {SONG_MAX} символів).\nНаприклад: <i>Океан Ельзи — Обійми</i>\n\n"
        "Без посилань. Скасувати: /cancel"
    )
    await call.answer()


@dp.message(MoodStates.song, F.text)
async def mood_song_save(message: types.Message, state: FSMContext):
    text = " ".join((message.text or "").split())
    if text.startswith("/"):
        await state.clear()
        await message.answer("Дію скасовано.", reply_markup=get_main_keyboard())
        return
    if len(text) < 2 or len(text) > SONG_MAX:
        await message.answer(f"Напиши від 2 до {SONG_MAX} символів, наприклад: Океан Ельзи — Обійми")
        return
    if _SONG_BAD.search(text):
        await message.answer("Без посилань і @нікнеймів, будь ласка — лише виконавець і назва пісні 🙂")
        return
    u = init_user(message.from_user.id)
    u["song"], u["song_at"] = text, time.time()
    request_save()
    await state.clear()
    await message.answer(
        "✅ Готово! Співрозмовники побачать: 🎵 «" + protect_text(esc(text)) + "»",
        reply_markup=get_main_keyboard(),
    )
    await message.answer(mood_text(u), reply_markup=mood_keyboard(u))


def vibe_note_for(me: dict, other: dict) -> str:
    """Що бачить людина на початку чату: настрій і пісня співрозмовника + тема дня."""
    lines = []
    mine, theirs = current_mood(me), current_mood(other)
    if theirs and theirs == mine:
        lines.append(f"🎭 Ви на одній хвилі: <b>{MOODS[theirs]}</b>")
    elif theirs:
        lines.append(f"🎭 Настрій співрозмовника: <b>{MOODS[theirs]}</b>")
    song = current_song(other)
    if song:
        lines.append("🎵 Співрозмовник зараз слухає: «" + protect_text(esc(song)) + "»")
    if other.get("bio"):
        lines.append("📝 Про співрозмовника: «" + protect_text(esc(other["bio"])) + "»")
    lines.append(f"💬 Тема дня: <b>{topic_of_day()}</b>")
    if hw_night_live():
        lines.append("🌙 <b>Зараз Ніч страшилок</b> — розкажіть одне одному найстрашнішу історію 👻")
    return "\n".join(lines)


def topic_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎲 Інша тема", callback_data="topic_new")]])


async def send_vibe_notes(user_id: int, u: dict, partner_id: int, p: dict):
    await safe_send(user_id, vibe_note_for(u, p), reply_markup=topic_kb())
    await safe_send(partner_id, vibe_note_for(p, u), reply_markup=topic_kb())


@dp.message(Command("topic"))
async def topic_cmd(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    partner_id = active_chats.get(user_id)
    if partner_id is None:
        await state.clear()
        init_user(user_id)
        await message.answer(
            f"💬 <b>Тема дня:</b> {topic_of_day()}\n\n"
            "Під час чату натисни /topic — і бот підкине вам обом нову тему для розмови 🎲"
        )
        return
    err = await new_topic_for_pair(user_id, partner_id)
    if err:
        await message.answer(err)


async def new_topic_for_pair(user_id: int, partner_id: int) -> str | None:
    """Підкидає обом нову тему. Повертає текст помилки, якщо тему щойно змінювали."""
    now = time.time()
    if now - max(topic_last.get(user_id, 0), topic_last.get(partner_id, 0)) < TOPIC_COOLDOWN:
        return "⏳ Тему щойно змінювали — спершу обговоріть цю 🙂"
    topic_last[user_id] = now
    text = f"🎲 Нова тема для розмови: <b>{random_topic(topic_of_day())}</b>"
    await safe_send(user_id, text, reply_markup=topic_kb())
    await safe_send(partner_id, text, reply_markup=topic_kb())
    return None


@dp.callback_query(F.data == "topic_new")
async def topic_new_cb(call: types.CallbackQuery):
    user_id = call.from_user.id
    partner_id = active_chats.get(user_id)
    if partner_id is None:
        await call.answer("Чат уже завершено — нова тема буде в наступному 🙂", show_alert=True)
        return
    err = await new_topic_for_pair(user_id, partner_id)
    await call.answer(err or "")


async def maybe_warn_low_rating(user_id: int, u: dict):
    if is_low_rated(u) and not u.get("low_warned"):
        u["low_warned"] = True
        await safe_send(
            user_id,
            "⚠️ Співрозмовники часто ставлять тобі 👎.\n"
            "Через це бот рідше підбирає тебе іншим. Будь привітнішим — "
            "і рейтинг підросте, а з ним і кількість цікавих чатів 🙂",
        )
    elif not is_low_rated(u) and u.get("low_warned"):
        u["low_warned"] = False


def profile_interests_keyboard(u: dict, tab: str = "main"):
    chosen = u.get("interests") or set()
    topics = HOBBY_TOPICS if tab == "hobby" else ROOM_TOPICS
    buttons = [
        InlineKeyboardButton(text=("✅ " if k in chosen else "") + label, callback_data=f"pi_t_{tab}_{k}")
        for k, label in topics.items()
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    tabs = [
        InlineKeyboardButton(text=("• " if tab != "hobby" else "") + "🧩 Інтереси", callback_data="pi_tab_main"),
        InlineKeyboardButton(text=("• " if tab == "hobby" else "") + "🎯 Захоплення", callback_data="pi_tab_hobby"),
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[tabs] + rows + [[InlineKeyboardButton(text="✅ Готово", callback_data="pi_done")]]
    )


def profile_interests_text(u: dict) -> str:
    return (
        f"🧩 <b>Мої інтереси</b> (до {MAX_PROFILE_INTERESTS})\n\n"
        f"Обрано: {interests_line(u)}\n\n"
        "Бот спершу шукатиме співрозмовників зі спільними інтересами, "
        "а після з'єднання покаже, про що вам цікаво поговорити."
    )


@dp.message(Command("myinterests"))
async def my_interests_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    u = init_user(message.from_user.id)
    await message.answer(profile_interests_text(u), reply_markup=profile_interests_keyboard(u))


@dp.callback_query(F.data == "pi_open")
async def my_interests_open(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    await call.message.answer(profile_interests_text(u), reply_markup=profile_interests_keyboard(u))
    await call.answer()


@dp.callback_query(F.data.startswith("pi_tab_"))
async def my_interests_tab(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    tab = "hobby" if call.data == "pi_tab_hobby" else "main"
    try:
        await call.message.edit_reply_markup(reply_markup=profile_interests_keyboard(u, tab))
    except TelegramAPIError:
        pass
    await call.answer()


@dp.callback_query(F.data.startswith("pi_t_"))
async def my_interests_toggle(call: types.CallbackQuery):
    tab, _, key = call.data[len("pi_t_"):].partition("_")
    if key not in INTEREST_LABELS:
        await call.answer("Невідома тема", show_alert=True)
        return
    u = init_user(call.from_user.id)
    chosen = u.setdefault("interests", set())
    if key in chosen:
        chosen.discard(key)
    elif len(chosen) >= MAX_PROFILE_INTERESTS:
        await call.answer(f"Можна обрати до {MAX_PROFILE_INTERESTS}. Спершу зніми якийсь ✅", show_alert=True)
        return
    else:
        chosen.add(key)
    request_save()
    try:
        await call.message.edit_text(profile_interests_text(u), reply_markup=profile_interests_keyboard(u, tab))
    except TelegramAPIError:
        pass
    await call.answer()


@dp.callback_query(F.data == "pi_done")
async def my_interests_done(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    try:
        await call.message.edit_text(f"✅ Твої інтереси: {interests_line(u)}\n\nЗмінити: /myinterests")
    except TelegramAPIError:
        pass
    await call.answer("Збережено")


# ---------------------------------------------------------------------------
# Підказка про фільтр за статтю: якщо безкоштовний юзер довго чекає у звичайному пошуку
# ---------------------------------------------------------------------------
FILTER_NUDGE_DELAY = int(os.getenv("FILTER_NUDGE_DELAY", "40"))  # секунд очікування
FILTER_NUDGE_COOLDOWN = 24 * 3600  # не частіше разу на добу
filter_nudge_last: dict[int, float] = {}


# ---------------------------------------------------------------------------
# 🛟 Порожня черга: якщо у флірті/темі довго нікого — пропонуємо звичайний пошук
# ---------------------------------------------------------------------------
STUCK_DELAY = int(os.getenv("STUCK_DELAY", "30"))  # секунд очікування до пропозиції
stuck_offered: dict[int, float] = {}  # user_id -> коли пропонували (щоб не повторювати в тому ж пошуку)


def normal_waiting_for(user_id: int) -> int:
    """Скільки людей у звичайному пошуку, з якими цю людину реально може з'єднати."""
    u = init_user(user_id)
    n = 0
    for cid in queue:
        if cid == user_id or search_mode.get(cid, "normal") != "normal":
            continue
        p = init_user(cid)
        if is_blacklisted(u, user_id, p, cid):
            continue
        if passes_filters(u, p) and passes_filters(p, u):
            n += 1
    return n


def schedule_stuck_offer(user_id: int, mode: str):
    if mode not in ("flirt", "voice") and not mode.startswith("int:"):
        return
    task = asyncio.get_running_loop().create_task(stuck_offer_after_wait(user_id, mode, time.time()))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def stuck_offer_after_wait(user_id: int, mode: str, started: float):
    try:
        await asyncio.sleep(STUCK_DELAY)
        if user_id not in queue or search_mode.get(user_id, "normal") != mode:
            return  # уже знайшов співрозмовника або вийшов з пошуку
        if stuck_offered.get(user_id, 0) >= started:
            return
        stuck_offered[user_id] = time.time()
        u = init_user(user_id)
        n = normal_waiting_for(user_id)
        if mode == "flirt":
            who = "дівчат" if u.get("gender") == "Хлопець" else "хлопців"
            head = f"❤️ Зараз {who} у флірт-пошуку немає."
        elif mode == "voice":
            head = "🎙 Зараз у голосовому чаті більше нікого немає."
        else:
            topic = INTEREST_LABELS.get(mode[4:], mode[4:])
            head = f"🧩 Зараз ніхто не шукає співрозмовника на тему {topic}."
        tail = (f"У звичайному пошуку вже чекає <b>{n}</b> — з'єднаю миттєво 👇" if n
                else "Можеш перейти у звичайний пошук — там шукають усі 👇")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Перейти у звичайний пошук", callback_data="stuck_go")],
            [InlineKeyboardButton(text="⏳ Чекати далі", callback_data="stuck_wait")],
        ])
        await safe_send(user_id, f"{head}\n{tail}", reply_markup=kb)
    except Exception as e:  # noqa: BLE001
        logging.warning("Порожня черга: %s", e)


@dp.callback_query(F.data == "stuck_go")
async def stuck_go(call: types.CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    if user_id in active_chats:
        await call.answer("Ти вже в чаті 🙂")
        return
    if user_id in queue:
        queue.remove(user_id)
    search_mode.pop(user_id, None)
    await call.answer()
    await run_search(call.message, state, "normal", user_id=user_id)


@dp.callback_query(F.data == "stuck_wait")
async def stuck_wait(call: types.CallbackQuery):
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await call.answer("Добре, чекаємо далі ⏳ Щойно хтось з'явиться — з'єднаю.")



def schedule_filter_nudge(user_id: int):
    u = init_user(user_id)
    if filter_active(u, "gender_filter") or not u.get("notify_tips", True):
        return
    if time.time() - filter_nudge_last.get(user_id, 0) < FILTER_NUDGE_COOLDOWN:
        return
    asyncio.create_task(filter_nudge_after_wait(user_id))


async def filter_nudge_after_wait(user_id: int):
    try:
        await asyncio.sleep(FILTER_NUDGE_DELAY)
        u = init_user(user_id)
        if user_id not in queue or search_mode.get(user_id, "normal") != "normal":
            return  # уже знайшов співрозмовника або вийшов з пошуку
        if filter_active(u, "gender_filter"):
            return
        if time.time() - filter_nudge_last.get(user_id, 0) < FILTER_NUDGE_COOLDOWN:
            return
        filter_nudge_last[user_id] = time.time()
        if u.get("gender") == "Хлопець":
            ask = "Хочеш спілкуватися лише з дівчатами? 👧"
        elif u.get("gender") == "Дівчина":
            ask = "Хочеш спілкуватися лише з хлопцями? 👦"
        else:
            ask = "Хочеш обирати стать співрозмовника? 👫"
        price = SHOP["gender_filter"][1]
        await safe_send(
            user_id,
            f"{ask}\n\n🎯 <b>Фільтр за статтю</b> — і бот з'єднуватиме лише з тими, кого ти обереш.\n"
            f"Він входить у 💎 Premium або купується окремо — {price} грн на тиждень.\n\n"
            "<i>Пошук триває, можеш просто чекати далі ⏳</i>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💎 Premium", callback_data="prem_open")],
                    [InlineKeyboardButton(text=f"🎯 Фільтр на тиждень — {price} грн", callback_data="buy_gender_filter")],
                ]
            ),
        )
    except Exception as e:  # noqa: BLE001 — підказка ніколи не має ламати пошук
        logging.warning("Підказка про фільтр не надіслана: %s", e)


@dp.callback_query(F.data == "prem_open")
async def prem_open(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    text, kb = premium_page()
    text += f"{premium_status_text(u)}\n💰 Баланс: {u['balance']:.2f} грн\n\nОбери тариф:"
    await call.message.answer(text, reply_markup=kb)
    await call.answer()


# ---------------------------------------------------------------------------
# «Хтось шукає співрозмовника»: якщо людина довго чекає — кличемо тих, хто зараз не в чаті
# ---------------------------------------------------------------------------
WAIT_PING_DELAY = int(os.getenv("WAIT_PING_DELAY", "30"))  # секунд очікування до виклику
WAIT_PING_USER_COOLDOWN = int(os.getenv("WAIT_PING_USER_COOLDOWN", str(3 * 3600)))  # одну людину кличемо не частіше
WAIT_PING_SEEKER_COOLDOWN = 10 * 60  # за одного й того самого шукача кличемо не частіше
WAIT_PING_MAX = int(os.getenv("WAIT_PING_MAX", "40"))  # скільки людей кличемо за раз
WAIT_PING_DAILY_MAX = int(os.getenv("WAIT_PING_DAILY_MAX", "2"))  # одну людину кличемо не більше N разів на день
WAIT_PING_NIGHT_END = 8  # з 00:00 до 08:00 (Київ) «хтось шукає» не надсилаємо
wait_ping_seeker_last: dict[int, float] = {}


def _wait_ping_mode_ok(mode: str) -> bool:
    return mode in ("normal", "flirt", "voice") or (mode.startswith("int:") and mode[4:] in INTEREST_LABELS)


def schedule_wait_ping(user_id: int, mode: str):
    if not _wait_ping_mode_ok(mode):
        return
    if time.time() - wait_ping_seeker_last.get(user_id, 0) < WAIT_PING_SEEKER_COOLDOWN:
        return
    asyncio.create_task(wait_ping_after_wait(user_id, mode))


def wait_ping_recipients(seeker_id: int, mode: str) -> list[int]:
    """Кого можна покликати: не в чаті/черзі/кімнаті, не забанені, сумісні з шукачем і давно не кликані."""
    s = init_user(seeker_id)
    now = time.time()
    result = []
    if datetime.now(KYIV_TZ).hour < WAIT_PING_NIGHT_END:
        return result  # уночі нікого не будимо
    for uid, u in users_db.items():
        if uid <= 0 or uid == seeker_id or uid in banned_users:
            continue
        if uid in active_chats or uid in queue or uid in user_room:
            continue
        if not u.get("notify_waiting", True) or u.get("gender", "Не вказано") == "Не вказано":
            continue  # вимкнув сповіщення або ще не пройшов знайомство з ботом
        if quiet_hours_now(u):
            continue  # 🌙 тихі години — не кличемо
        if u.get("wait_ping_day") == today_str() and u.get("wait_ping_n", 0) >= WAIT_PING_DAILY_MAX:
            continue  # не більше WAIT_PING_DAILY_MAX разів на день
        if now - u.get("wait_ping_at", 0) < WAIT_PING_USER_COOLDOWN:
            continue
        if is_blacklisted(s, seeker_id, u, uid):
            continue
        if not passes_filters(s, u) or not passes_filters(u, s):
            continue
        if mode == "flirt" and (u.get("gender") == s.get("gender") or flirt_block_reason(u) is not None):
            continue
        result.append(uid)
    result.sort(key=lambda x: last_seen.get(x, 0), reverse=True)  # спершу ті, хто нещодавно заходив
    return result[:WAIT_PING_MAX]


def wait_ping_text(mode: str) -> str:
    if mode == "flirt":
        what = "❤️ Зараз хтось шукає пару для флірту"
    elif mode.startswith("int:"):
        what = f"🧩 Зараз хтось шукає співрозмовника на тему {INTEREST_LABELS.get(mode[4:], mode[4:])}"
    else:
        what = "👋 Зараз хтось шукає співрозмовника"
    return (
        f"{what} — і чекає саме на тебе!\n\n"
        "Натисни кнопку, щоб одразу з'єднатися 👇\n"
        "<i>Вимкнути такі сповіщення: ⚙️ Налаштування → 🔔 Сповіщення</i>"
    )


async def wait_ping_after_wait(user_id: int, mode: str):
    try:
        await asyncio.sleep(WAIT_PING_DELAY)
        if user_id not in queue or search_mode.get(user_id, "normal") != mode:
            return  # уже знайшов співрозмовника або вийшов з пошуку
        if time.time() - wait_ping_seeker_last.get(user_id, 0) < WAIT_PING_SEEKER_COOLDOWN:
            return
        wait_ping_seeker_last[user_id] = time.time()
        recipients = wait_ping_recipients(user_id, mode)
        if not recipients:
            return
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔍 Знайти співрозмовника", callback_data=f"wp_go:{mode}")]]
        )
        text = wait_ping_text(mode)
        sent = 0
        for uid in recipients:
            if user_id not in queue:
                break  # шукач уже знайшов пару — більше нікого не кличемо
            pu = users_db[uid]
            pu["wait_ping_at"] = time.time()
            if pu.get("wait_ping_day") != today_str():
                pu["wait_ping_day"], pu["wait_ping_n"] = today_str(), 0
            pu["wait_ping_n"] = pu.get("wait_ping_n", 0) + 1
            if await safe_send(uid, text, reply_markup=kb):
                sent += 1
            await asyncio.sleep(0.05)  # не впираємось у ліміти Telegram
        if sent:
            stat_add("wait_pings", sent)
            request_save()
            if user_id in queue:
                await safe_send(user_id, "📣 Ми покликали людей, які зараз не в чаті. Зачекай ще трохи ⏳")
    except Exception as e:  # noqa: BLE001 — сповіщення ніколи не має ламати пошук
        logging.warning("Виклик «хтось шукає» не надіслано: %s", e)


# ---------------------------------------------------------------------------
# 👀 Адміну: «хтось уже хвилину шукає співрозмовника» — щоб людина не пішла з порожнього бота
# ---------------------------------------------------------------------------
ADMIN_WAIT_ALERT_DELAY = int(os.getenv("ADMIN_WAIT_ALERT_DELAY", "60"))
ADMIN_WAIT_ALERT_COOLDOWN = 120  # не частіше ніж раз на 2 хв
admin_wait_alert_last = 0.0
admin_alerts_enabled = True


def schedule_admin_wait_alert(user_id: int, mode: str):
    if not ADMIN_ID or user_id == ADMIN_ID or not _wait_ping_mode_ok(mode):
        return
    asyncio.create_task(admin_wait_alert_after(user_id, mode))


async def admin_wait_alert_after(user_id: int, mode: str):
    global admin_wait_alert_last
    try:
        await asyncio.sleep(ADMIN_WAIT_ALERT_DELAY)
        if not admin_alerts_enabled or user_id not in queue or search_mode.get(user_id, "normal") != mode:
            return
        if ADMIN_ID in active_chats or ADMIN_ID in queue or ADMIN_ID in user_room:
            return  # адмін уже спілкується
        if time.time() - admin_wait_alert_last < ADMIN_WAIT_ALERT_COOLDOWN:
            return
        admin_wait_alert_last = time.time()
        what = {"normal": "звичайний пошук", "flirt": "флірт"}.get(mode) or (
            f"тема {INTEREST_LABELS.get(mode[4:], mode[4:])}" if mode.startswith("int:") else mode
        )
        await safe_send(
            ADMIN_ID,
            f"👀 Хтось уже хвилину шукає співрозмовника ({what}).\nПідключишся, щоб людина не пішла?",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💬 Підключитися", callback_data=f"wp_go:{mode}")],
                    [InlineKeyboardButton(text="🔕 Вимкнути такі сповіщення", callback_data="adm_alerts_off")],
                ]
            ),
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("Сповіщення адміну не надіслано: %s", e)


@dp.callback_query(F.data == "adm_alerts_off")
@admin_only
async def adm_alerts_off(call: types.CallbackQuery):
    global admin_alerts_enabled
    admin_alerts_enabled = False
    await call.message.answer("🔕 Вимкнено до перезапуску бота. Увімкнути знову: /alerts")
    await call.answer()


@dp.message(Command("alerts"))
async def adm_alerts_on(message: types.Message):
    global admin_alerts_enabled
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    admin_alerts_enabled = True
    await message.answer("🔔 Сповіщення «хтось шукає співрозмовника» увімкнено.")


@dp.callback_query(F.data.startswith("wp_go:"))
async def wait_ping_go(call: types.CallbackQuery, state: FSMContext):
    mode = call.data[len("wp_go:"):]
    if not _wait_ping_mode_ok(mode):
        mode = "normal"
    u = init_user(call.from_user.id)
    if mode == "flirt" and flirt_block_reason(u) is not None:
        mode = "normal"
    await call.answer()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await run_search(call.message, state, mode, user_id=call.from_user.id)


# ---------------------------------------------------------------------------
# Пошук за інтересами 1-на-1: з'єднуємо лише людей з однаковою темою
# ---------------------------------------------------------------------------
def interests_keyboard(tab: str = "main"):
    waiting = {}
    for uid in queue:
        mode = search_mode.get(uid, "")
        if mode.startswith("int:"):
            waiting[mode[4:]] = waiting.get(mode[4:], 0) + 1
    topics = HOBBY_TOPICS if tab == "hobby" else ROOM_TOPICS
    buttons = []
    for key, label in topics.items():
        n = waiting.get(key, 0)
        buttons.append(
            InlineKeyboardButton(text=f"{label} · чекає {n}" if n else label, callback_data=f"intr_{key}")
        )
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    hobby_wait = sum(waiting.get(k, 0) for k in HOBBY_TOPICS)
    main_wait = sum(waiting.get(k, 0) for k in ROOM_TOPICS)
    tabs = [
        InlineKeyboardButton(
            text=("• " if tab != "hobby" else "") + "🧩 Інтереси" + (f" ({main_wait})" if main_wait else ""),
            callback_data="intrtab_main",
        ),
        InlineKeyboardButton(
            text=("• " if tab == "hobby" else "") + "🎯 Захоплення" + (f" ({hobby_wait})" if hobby_wait else ""),
            callback_data="intrtab_hobby",
        ),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[tabs] + rows)


@dp.callback_query(F.data.startswith("intrtab_"))
async def interests_tab(call: types.CallbackQuery):
    tab = "hobby" if call.data == "intrtab_hobby" else "main"
    try:
        await call.message.edit_reply_markup(reply_markup=interests_keyboard(tab))
    except TelegramAPIError:
        pass
    await call.answer("🎯 Захоплення" if tab == "hobby" else "🧩 Інтереси")


@dp.message(F.text == BTN_INTERESTS)
@dp.message(Command("interests"))
async def interests_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await send_banner(message.from_user.id, "interests")
    await message.answer(
        "🧩 <b>Пошук за інтересами</b>\n\n"
        "Обери тему — і ми знайдемо співрозмовника, якому цікаве те саме. "
        "Поруч із темою видно, скільки людей уже чекає.\n\n"
        "Перемикай вкладки вгорі: 🧩 Інтереси / 🎯 Захоплення.",
        reply_markup=interests_keyboard(),
    )


@dp.callback_query(F.data.startswith("intr_"))
async def interests_pick(call: types.CallbackQuery, state: FSMContext):
    key = call.data[len("intr_"):]
    if key not in INTEREST_LABELS:
        await call.answer("Невідома тема", show_alert=True)
        return
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await call.answer(INTEREST_LABELS[key])
    await run_search(call.message, state, f"int:{key}", user_id=call.from_user.id)


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
    ("gb", "🇬🇧 Велика Британія", "Велика Британія"),
    ("it", "🇮🇹 Італія", "Італія"),
    ("es", "🇪🇸 Іспанія", "Іспанія"),
    ("nl", "🇳🇱 Нідерланди", "Нідерланди"),
]
# Якщо замість країни написали українське місто/область — записуємо «Україна» (щоб фільтр за країною працював)
UA_PLACES = (
    "київ", "львів", "харків", "одес", "дніпр", "запоріж", "вінниц", "полтав", "чернігів", "черкас", "житомир",
    "суми", "сум", "рівн", "луцьк", "тернопіль", "хмельниц", "івано", "франківськ", "ужгород", "чернівц",
    "кропивницьк", "миколаїв", "херсон", "кривий", "біла церква", "краматорськ", "кременчук", "бровар", "ірпін",
    "буча", "обл", "україн", "украин", "ukraine", "киев", "львов", "харьков", "одесс", "днепр",
)


def normalize_country(text: str) -> str:
    t = (text or "").strip()[:50]
    low = t.lower()
    for _k, _label, name in ONBOARD_COUNTRIES:
        if low == name.lower():
            return name
    if any(p in low for p in UA_PLACES):
        return "Україна"
    return t
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
    refresh_default_nick(u)
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
            "Напиши свою країну (наприклад: Франція, США, Канада):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[_skip_row()])
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
    init_user(message.from_user.id)["country"] = normalize_country(text)
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
            [InlineKeyboardButton(text=f"👋 Хтось шукає співрозмовника: {_onoff(u.get('notify_waiting', True))}", callback_data="tg_notify_waiting")],
            [InlineKeyboardButton(text=f"⏰ Година знайомств: {_onoff(u.get('notify_meet', True))}", callback_data="tg_notify_meet")],
            [InlineKeyboardButton(text=f"🌙 Тихі години 23:00–08:00: {_onoff(u.get('quiet', False))}", callback_data="tg_quiet")],
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
    "notify_waiting": (True, "notify"),
    "notify_meet": (True, "notify"),
    "quiet": (False, "notify"),
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




# === КІНЕЦЬ part4.py ===
