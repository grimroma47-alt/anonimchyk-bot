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
        comment = item.get("comment") or ""
        code = find_payment_code(comment)
        p = manual_payments.get(code) if code else None
        if p is None or p.get("status") != "pending":
            if ADMIN_ID:
                await safe_send(
                    ADMIN_ID,
                    f"🫙 Надходження в Банку без відомого коду: <b>{paid:g} грн</b>\n"
                    f"Коментар: «{esc(comment) or '—'}»\n"
                    "Якщо це оплата від користувача — зарахуй вручну (/addbalance або /confirm).",
                )
            continue
        expected = p["amount"]
        if abs(paid - expected) >= 0.01:
            p["expected_amount"] = expected
            p["amount"] = paid  # зараховуємо фактично отриману суму
        p["method"] = "jar"
        result = await _resolve_payment(code, True)
        credited += 1
        if ADMIN_ID:
            note = "" if abs(paid - expected) < 0.01 else f" (очікувалось {expected} грн)"
            await safe_send(ADMIN_ID, f"🤖 Автоматично через Банку{note}:\n{result}")
    if len(mono_seen_ids) > MONO_SEEN_LIMIT:
        del mono_seen_ids[: len(mono_seen_ids) - MONO_SEEN_LIMIT]
    if changed:
        request_save()
    return credited


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


# === КІНЕЦЬ part3.py ===
