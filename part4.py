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
    common = [k for k in INTEREST_LABELS if k in (u.get("interests") or set()) & (p.get("interests") or set())]
    if common:
        note = "🧩 Спільні інтереси: <b>" + ", ".join(INTEREST_LABELS[k] for k in common) + "</b> — є з чого почати 😉"
        await safe_send(user_id, note)
        await safe_send(partner_id, note)
        return
    for me, other in ((user_id, p), (partner_id, u)):
        if other.get("interests"):
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
    if filter_active(u, "gender_filter"):
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
        f"🎉 Готово! Твій профіль: {short_info(u)}\n\n"
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

    try:
        await message.copy_to(chat_id=partner_id, protect_content=should_protect(users_db.get(user_id)))
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
        await save_state_to_db()  # фінальне збереження при зупинці (деплой/перезапуск)
        await release_lease()  # тепер нова копія може забрати свіжі дані
        await bot.session.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())



# === КІНЕЦЬ part4.py ===
