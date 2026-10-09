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
        record_topup("jar" if p.get("method") == "jar" else "card", p["amount"])
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


def on_chat_message(user_id: int):
    m = chat_meta.get(user_id)
    if m is not None:
        m["msgs"] += 1
    stat_add("msgs")
    task_event(user_id, "msg")


def on_chat_ended(a: int, b: int):
    try:
        ma, mb = chat_meta.pop(a, None), chat_meta.pop(b, None)
        if ma is None or mb is None:
            return
        last_chat_mode[a] = ma["mode"]
        last_chat_mode[b] = mb["mode"]
        duration = time.time() - min(ma["start"], mb["start"])
        if duration < REAL_CHAT_SECONDS or ma["msgs"] < 1 or mb["msgs"] < 1:
            return  # «натиснув і втік» не рахується — щоб завдання не накручували
        stat_add("real_chats")
        long_chat = duration >= LONG_CHAT_SECONDS and ma["msgs"] >= 3 and mb["msgs"] >= 3
        mode = ma["mode"]
        for x in (a, b):
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
MATCH_SCORE_MAX = 3  # 2 — однаковий «рівень» рейтингу, +1 — є спільний інтерес


def match_score(u: dict, p: dict, mode: str) -> int:
    """Чим більше балів, тим краща пара. Люди з низьким рейтингом спершу потрапляють одне до одного."""
    score = 2 if is_low_rated(u) == is_low_rated(p) else 0
    if not mode.startswith("int:") and (u.get("interests") or set()) & (p.get("interests") or set()):
        score += 1
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
    if not receiver or receiver.get("media_mode") != "safe":
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
}


async def _set_markup(call: types.CallbackQuery, markup):
    try:
        await call.message.edit_reply_markup(reply_markup=markup)
    except TelegramAPIError:
        pass


@dp.callback_query(F.data == "set_media")
async def set_media(call: types.CallbackQuery):
    u = init_user(call.from_user.id)
    u["media_mode"] = "all" if u.get("media_mode") == "safe" else "safe"
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
        f"🧩 Інтереси: {interests_line(u)}\n\n"
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
        ]
    )


async def _show_profile_editor(chat_id: int, u: dict):
    await safe_send(chat_id, profile_edit_text(u), reply_markup=profile_edit_keyboard())


@dp.callback_query(F.data == "pe_menu")
async def pe_menu(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_profile_editor(call.from_user.id, init_user(call.from_user.id))
    await call.answer()


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
        await call.message.answer("🌍 Напиши свою країну або місто. Скасувати: /cancel")
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
    country = (message.text or "").strip()[:50]
    if len(country) < 2 or LINK_REGEX.search(country):
        await message.answer("Напиши назву країни або міста. Скасувати: /cancel")
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
}
BANNER_COOLDOWN = 10 * 60  # той самий банер одній людині — не частіше ніж раз на 10 хв
banner_last_shown: dict[tuple[int, str], float] = {}


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
    rows = [
        [InlineKeyboardButton(text=("✅ " if key in banners else "➕ ") + label, callback_data=f"adm_bn_{key}")]
        for key, label in BANNER_PLACES.items()
    ]
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
        await asyncio.sleep(300)


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
            broadcast_text = f"<b>{esc(u['nickname'])}:</b> {esc(message.text)}"
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
        types.BotCommand(command="silent", description="🔒 Захист моїх медіа (Premium)"),
    ]
    await bot.set_my_commands(default_commands, scope=types.BotCommandScopeDefault())

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



# === КІНЕЦЬ part4.py ===
