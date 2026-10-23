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


# ---------------------------------------------------------------------------
# Оплата через Банку monobank з автоматичним підтвердженням
# (бот раз на хвилину читає виписку Банки і шукає код платежу в коментарі)
# ---------------------------------------------------------------------------
MONO_API = "https://api.monobank.ua"
MONO_POLL_SECONDS = 65  # API дозволяє виписку не частіше ніж раз на 60 с
MONO_SEEN_LIMIT = 2000
MONO_CODE_REGEX = re.compile(r"[PР]\s?(\d{6})", re.IGNORECASE)  # латинська P або кирилична Р
mono_state = {"jar": None, "last_poll": None, "last_error": None, "jars": []}


def mono_jar_link() -> str:
    if PAY_JAR_LINK:
        return PAY_JAR_LINK
    jar = mono_state.get("jar")
    if jar and jar.get("sendId"):
        send_id = jar["sendId"]
        return send_id if send_id.startswith("http") else f"https://send.monobank.ua/{send_id}"
    return ""


async def _mono_get(path: str):
    """GET до API monobank. Повертає (статус, json або None)."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{MONO_API}{path}", headers={"X-Token": MONO_TOKEN}, timeout=aiohttp.ClientTimeout(total=20)
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:  # noqa: BLE001
                data = None
            return resp.status, data


def _choose_jar(jars: list[dict]) -> dict | None:
    if MONO_JAR_ID:
        return next((j for j in jars if j.get("id") == MONO_JAR_ID), None)
    if MONO_JAR_TITLE:
        wanted = MONO_JAR_TITLE.lower()
        matches = [j for j in jars if wanted in (j.get("title") or "").lower()]
        return matches[0] if len(matches) == 1 else None
    uah = [j for j in jars if j.get("currencyCode") in (980, None)]
    return uah[0] if len(uah) == 1 else None


async def mono_detect_jar() -> bool:
    status, data = await _mono_get("/personal/client-info")
    if status != 200 or not isinstance(data, dict):
        desc = (data or {}).get("errorDescription") if isinstance(data, dict) else None
        mono_state["last_error"] = f"client-info: HTTP {status} {desc or ''}".strip()
        return False
    jars = data.get("jars") or []
    mono_state["jars"] = jars
    jar = _choose_jar(jars)
    if jar is None:
        mono_state["last_error"] = (
            f"Не вдалося вибрати Банку (знайдено: {len(jars)}). "
            "Задай MONO_JAR_TITLE (частину назви) або MONO_JAR_ID — список: /mono"
        )
        return False
    mono_state["jar"] = jar
    mono_state["last_error"] = None
    logging.info("Monobank: використовую Банку «%s».", jar.get("title"))
    return True


def find_payment_code(comment: str) -> str | None:
    m = MONO_CODE_REGEX.search(comment or "")
    return f"P{m.group(1)}" if m else None


MONO_MATCH_WINDOW = 30 * 60  # оплата без коду: шукаємо заявку з тією ж сумою за останні 30 хв
# Надходження без коду, які чекають вибору адміна (лише в пам'яті): ключ -> {"paid", "comment"}
mono_unmatched: dict[str, dict] = {}
_mono_unmatched_seq = [0]


def _amount_candidates(paid, item_time: float) -> list[str]:
    """Неоплачені заявки з точно такою ж сумою, створені незадовго до оплати."""
    found = []
    for code, p in manual_payments.items():
        if p.get("status") != "pending" or not p.get("ts"):
            continue
        if abs(float(p["amount"]) - float(paid)) >= 0.01:
            continue
        if p["ts"] - 120 <= item_time <= p["ts"] + MONO_MATCH_WINDOW:
            found.append(code)
    return found


def _recent_pending(item_time: float, limit: int = 5) -> list[str]:
    recent = [
        (p["ts"], code)
        for code, p in manual_payments.items()
        if p.get("status") == "pending" and p.get("ts") and 0 <= item_time - p["ts"] + 120 <= 86400
    ]
    return [code for _ts, code in sorted(recent, reverse=True)[:limit]]


async def _credit_jar_payment(code: str, paid, how: str) -> str:
    p = manual_payments[code]
    expected = p["amount"]
    if abs(float(paid) - float(expected)) >= 0.01:
        p["expected_amount"] = expected
        p["amount"] = paid  # зараховуємо фактично отриману суму
    p["method"] = "jar"
    result = await _resolve_payment(code, True)
    if ADMIN_ID:
        note = "" if abs(float(paid) - float(expected)) < 0.01 else f" (очікувалось {expected} грн)"
        await safe_send(ADMIN_ID, f"🤖 {how}{note}:\n{result}")
    return result


async def process_jar_statement(items: list[dict]) -> int:
    """Обробляє операції з виписки Банки. Повертає, скільки платежів зараховано."""
    credited = 0
    changed = False
    seen = set(mono_seen_ids)
    for item in sorted(items, key=lambda i: i.get("time", 0)):
        item_id = str(item.get("id") or "")
        amount_kop = item.get("amount") or 0
        if not item_id or item_id in seen or amount_kop <= 0:
            continue
        seen.add(item_id)
        mono_seen_ids.append(item_id)
        changed = True
        paid = round(amount_kop / 100, 2)
        if float(paid).is_integer():
            paid = int(paid)
        item_time = float(item.get("time") or time.time())
        comment = item.get("comment") or ""

        # 1) код у коментарі
        code = find_payment_code(comment)
        p = manual_payments.get(code) if code else None
        if p is not None and p.get("status") == "pending":
            await _credit_jar_payment(code, paid, "Автоматично через Банку")
            credited += 1
            continue

        # 2) коду немає — шукаємо заявку з тією ж сумою
        candidates = _amount_candidates(paid, item_time)
        if len(candidates) == 1:
            await _credit_jar_payment(candidates[0], paid, "Зараховано за сумою (у коментарі не було коду)")
            credited += 1
            continue

        # 3) кілька або жодної — питаємо адміна кнопками
        if not ADMIN_ID:
            continue
        options = candidates or _recent_pending(item_time)
        text = (
            f"🫙 Надходження в Банку без коду: <b>{paid:g} грн</b>\n"
            f"Коментар: «{esc(comment) or '—'}»\n"
        )
        kb = None
        if options:
            _mono_unmatched_seq[0] += 1
            key = str(_mono_unmatched_seq[0])
            mono_unmatched[key] = {"paid": paid, "comment": comment}
            text += (
                "Кілька заявок з такою сумою — обери, кому зарахувати:"
                if candidates
                else "Заявки з такою сумою немає. Ось останні неоплачені — обери, кому зарахувати:"
            )
            rows = []
            for c in options:
                cp = manual_payments[c]
                cu = users_db.get(cp["user_id"]) or {}
                label = f"✅ {c}: {cu.get('nickname', cp['user_id'])} ({cu.get('custom_id', '?')}), заявка {cp['amount']} грн"
                rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"jp_{key}_{c}")])
            kb = InlineKeyboardMarkup(inline_keyboard=rows)
        else:
            text += "Неоплачених заявок немає. Якщо це оплата від користувача — зарахуй вручну (/addbalance)."
        await safe_send(ADMIN_ID, text, reply_markup=kb)
    if len(mono_seen_ids) > MONO_SEEN_LIMIT:
        del mono_seen_ids[: len(mono_seen_ids) - MONO_SEEN_LIMIT]
    if changed:
        request_save()
    return credited


@dp.callback_query(F.data.startswith("jp_"))
async def jar_pick(call: types.CallbackQuery):
    """Адмін вибрав, кому зарахувати надходження без коду."""
    if not _is_admin(call.from_user.id):
        await call.answer("Доступ заборонено", show_alert=True)
        return
    try:
        _prefix, key, code = call.data.split("_", 2)
    except ValueError:
        await call.answer()
        return
    entry = mono_unmatched.get(key)
    if entry is None:
        await call.answer(
            "Це надходження вже оброблено або бот перезапускався. Зарахуй вручну: /addbalance", show_alert=True
        )
        return
    p = manual_payments.get(code)
    if p is None or p.get("status") != "pending":
        await call.answer("Ця заявка вже оброблена. Обери іншу.", show_alert=True)
        return
    mono_unmatched.pop(key, None)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await _credit_jar_payment(code, entry["paid"], "Зараховано вручну з Банки")
    await call.answer("Зараховано ✅")


async def mono_poll_once():
    now = int(time.time())
    # стежимо лише за свіжими заявками (до 3 діб); старші можна підтвердити вручну
    pending = [
        p
        for p in manual_payments.values()
        if p.get("status") == "pending" and p.get("ts") and now - p["ts"] < 3 * 86400
    ]
    if not pending:
        return
    oldest = min(int(p["ts"]) for p in pending) - 300
    frm = max(oldest, now - 2682000 + 60)  # не більше 31 доби + 1 год
    status, data = await _mono_get(f"/personal/statement/{mono_state['jar']['id']}/{frm}/{now}")
    mono_state["last_poll"] = time.strftime("%d.%m.%Y %H:%M:%S")
    if status != 200 or not isinstance(data, list):
        desc = data.get("errorDescription") if isinstance(data, dict) else ""
        mono_state["last_error"] = f"statement: HTTP {status} {desc or ''}".strip()
        logging.warning("Monobank: %s", mono_state["last_error"])
        return
    mono_state["last_error"] = None
    await process_jar_statement(data)


async def mono_poll_loop():
    if not MONO_TOKEN:
        return
    await asyncio.sleep(10)
    while True:
        try:
            if mono_state.get("jar") is None:
                if not await mono_detect_jar():
                    logging.warning("Monobank: %s", mono_state["last_error"])
                    await asyncio.sleep(300)
                    continue
                await asyncio.sleep(MONO_POLL_SECONDS)  # окремий ліміт для client-info / statement
            await mono_poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            mono_state["last_error"] = str(e)
            logging.warning("Monobank: помилка перевірки Банки: %s", e)
        await asyncio.sleep(MONO_POLL_SECONDS)


@dp.callback_query(F.data.startswith("topup_jar_"))
async def topup_pay_jar(call: types.CallbackQuery):
    link = mono_jar_link()
    if not link:
        await call.answer("Цей спосіб оплати зараз недоступний", show_alert=True)
        return
    amount = _parse_topup_amount(call.data[len("topup_jar_"):])
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
        "method": "jar",
    }
    request_save()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🫙 Відкрити Банку і оплатити", url=link)],
            [InlineKeyboardButton(text="📸 Не зарахувалось? Надіслати квитанцію", callback_data=f"bank_paid_{code}")],
        ]
    )
    auto = bool(MONO_TOKEN and mono_state.get("jar"))
    await call.message.answer(
        "🫙 <b>Оплата через Банку monobank</b>\n\n"
        f"1️⃣ Натисни «Відкрити Банку» і введи суму <b>{amount} грн</b>.\n"
        f"2️⃣ У полі <b>коментар</b> обов'язково напиши код: <code>{code}</code>\n"
        "3️⃣ Оплати будь-якою карткою.\n\n"
        + (
            "🤖 Баланс поповниться <b>автоматично</b> за 1–2 хвилини після оплати."
            if auto
            else "⏳ Платіж перевіряється вручну, зазвичай протягом кількох годин."
        )
        + f"\n\n⚠️ Без коду {code} у коментарі ми не зможемо знайти твій платіж.",
        reply_markup=kb,
    )
    await call.answer()


@dp.message(Command("mono"))
async def mono_status(message: types.Message):
    """Адмін: стан підключення до Банки monobank."""
    if not _is_admin(message.from_user.id):
        return
    if not MONO_TOKEN:
        await message.answer("🫙 MONO_TOKEN не задано в Render — автопідтвердження вимкнене.")
        return
    jar = mono_state.get("jar")
    pending = sum(1 for p in manual_payments.values() if p.get("status") == "pending" and p.get("method") == "jar")
    lines = ["🫙 <b>Банка monobank</b>"]
    if jar:
        lines.append(f"Банка: «{esc(jar.get('title'))}», баланс {jar.get('balance', 0) / 100:g} грн")
        lines.append(f"Посилання: {esc(mono_jar_link())}")
    else:
        lines.append("Банку ще не вибрано.")
    lines.append(f"Оплат через Банку на перевірці: {pending}")
    lines.append(f"Остання перевірка виписки: {mono_state.get('last_poll') or '—'}")
    if mono_state.get("last_error"):
        lines.append(f"⚠️ Помилка: {esc(mono_state['last_error'])}")
    if mono_state.get("jars"):
        lines.append("\nУсі твої Банки (для MONO_JAR_TITLE / MONO_JAR_ID):")
        for j in mono_state["jars"]:
            lines.append(f"• «{esc(j.get('title'))}» — <code>{esc(j.get('id'))}</code>")
    await message.answer("\n".join(lines))


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
    for uid, u in users_db.items():
        if uid <= 0 or uid == seeker_id or uid in banned_users:
            continue
        if uid in active_chats or uid in queue or uid in user_room:
            continue
        if not u.get("notify_waiting", True) or u.get("gender", "Не вказано") == "Не вказано":
            continue  # вимкнув сповіщення або ще не пройшов знайомство з ботом
        if quiet_hours_now(u):
            continue  # 🌙 тихі години — не кличемо
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
            users_db[uid]["wait_ping_at"] = time.time()
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




# === КІНЕЦЬ part4.py ===
