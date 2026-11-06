@dp.message(F.text == BTN_GIFT)
async def gift_menu(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    target, kind = gift_target(user_id)
    if target is None:
        await message.answer("Дарувати можна співрозмовнику під час чату або другу в переписці.", reply_markup=get_main_keyboard())
        return
    await message.answer("🎁 Що подаруєш другу?" if kind == "friend" else "🎁 Що подаруєш співрозмовнику?",
                         reply_markup=get_gift_menu_keyboard())


@dp.callback_query(F.data == "gift_money")
async def gift_money_start(call: types.CallbackQuery, state: FSMContext):
    if gift_target(call.from_user.id)[0] is None:
        await call.answer("Чат вже завершено", show_alert=True)
        return
    await call.message.answer("Введіть суму в грн, яку хочете подарувати (або /cancel):")
    await state.set_state(GiftStates.amount)
    await call.answer()


@dp.message(GiftStates.amount, F.text)
async def gift_money_finish(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    partner_id, kind = gift_target(user_id)
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

    request_save()
    if kind == "friend":
        await message.answer(f"🎁 Подаровано {amount:.2f} грн другу!", reply_markup=_gift_kb(user_id))
        await safe_send(partner_id, f"🎁 {gift_from_label(user_id, kind)} {gift_verb(user_id)} тобі {amount:.2f} грн!")
        return
    await message.answer(
        f"🎁 Подаровано {amount:.2f} грн співрозмовнику!", reply_markup=get_chat_keyboard()
    )
    await safe_send(partner_id, f"🎁 Співрозмовник подарував вам {amount:.2f} грн!")


@dp.callback_query(F.data == "gift_catalog")
async def gift_catalog_in_chat(call: types.CallbackQuery):
    if gift_target(call.from_user.id)[0] is None:
        await call.answer("Чат вже завершено", show_alert=True)
        return
    await call.message.answer(
        "🛒 <b>Магазин подарунків</b>\n\nКупи подарунок — одразу запропоную подарувати його співрозмовнику:",
        reply_markup=get_gift_catalog_keyboard(),
    )
    await call.answer()


@dp.callback_query(F.data == "gift_inventory")
async def gift_inventory_menu(call: types.CallbackQuery):
    if gift_target(call.from_user.id)[0] is None:
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


# ---------------------------------------------------------------------------
# ✨ Особливі подарунки: ефекти, сюрпризи, листівки, ключик, колекційні
# ---------------------------------------------------------------------------
# Повноекранні ефекти Telegram (працюють лише в особистих чатах з ботом)
EFFECT_PARTY = "5046509860389126442"  # 🎉 конфеті
EFFECT_HEART = "5044134455711629726"  # ❤️ серця
EFFECT_FIRE = "5104841245755180586"  # 🔥 вогонь

# Що може випасти з 🎭 маски-сюрпризу: (подарунок, вага). Частіше — дешеве, рідко — діамант.
MASK_PRIZES = [
    ("rose", 26), ("heart", 20), ("chocolate", 16), ("rabbit", 7), ("dog", 7), ("cat", 7),
    ("cake", 7), ("bouquet", 5), ("teddy", 3), ("ring", 1.2), ("unicorn", 0.5), ("diamond", 0.3),
]
ORACLE_LINES = [
    "Сьогодні тобі напише хтось, хто зробить твій день кращим ✨",
    "Найцікавіша розмова тижня ще попереду — не вимикай сповіщення 😉",
    "Хтось думає про тебе саме зараз… і посміхається 💜",
    "Скоро ти почуєш комплімент, якого зовсім не чекаєш 🌸",
    "Сміливість — твоя суперсила цього тижня. Скажи те, що давно хотів(ла) 🦁",
    "Тебе чекає приємний сюрприз. Можливо, вже сьогодні 🎁",
    "Твоя доброта повернеться до тебе в подвійному розмірі 🍀",
    "Цього тижня варто погодитись на щось нове — не пошкодуєш 🚀",
    "Хтось запам'ятає розмову з тобою надовго 🌙",
    "Найкращі ідеї приходять уночі — запиши свою 💡",
    "Сьогодні вдалий день, щоб помиритися з кимось 🤝",
    "Зірки кажуть: більше відпочинку і менше хвилювань ⭐",
]
CARD_PHRASES = [
    "Ти класний співрозмовник 💜",
    "Дякую за розмову — було тепло 🌸",
    "З тобою цікаво навіть мовчати 😊",
    "Ти зробив(ла) мій вечір кращим ✨",
    "Посміхнись — ти цього вартий(а) 🙂",
    "Бережи себе, гаразд? 🫶",
    "Радий(а), що випадок звів нас тут 🍀",
    "Ти неймовірний(а), просто знай це 🌙",
]
FUNNY_GIFT_TEXT = {
    "sp_sock": "🧦 Тобі подарували… одну шкарпетку. Друга, кажуть, загубилась у пральці 😅",
    "sp_potato": "🥔 Картоплина! Найщиріший подарунок — з нею точно не пропадеш 😄",
    "sp_herring": "🐟 Оселедець! Пахне святом… і трохи морем 😅",
}


async def send_with_effect(chat_id: int, text: str, effect: str | None = None) -> bool:
    """Надсилає повідомлення з повноекранним ефектом; якщо ефект не спрацював — просто текстом."""
    if effect:
        u = users_db.get(chat_id)
        if u is not None and u.get("blocked_bot"):
            return False
        try:
            await bot.send_message(chat_id, text, message_effect_id=effect)
            return True
        except TelegramAPIError as e:
            logging.info("Ефект не вдався (%s), надсилаю без нього", e)
        except TypeError:
            pass
    return await safe_send(chat_id, text)


def _key_options(u: dict) -> list[tuple[str, str]]:
    """Які факти про СЕБЕ дарувальник може відкрити ключиком (тільки свої дані й тільки за бажанням)."""
    opts = []
    if u.get("city"):
        opts.append(("city", f"🏙 Моє місто ({u['city']})"))
    if str(u.get("age") or "").strip().isdigit():
        opts.append(("age", f"🎂 Мій вік ({u['age']})"))
    if u.get("interests"):
        opts.append(("int", "🧩 Мій інтерес"))
    return opts


def special_gift_payload(key: str, arg: str, sender: dict, partner: dict) -> tuple[str, str | None]:
    """Що відбувається в отримувача: додатковий текст і ефект."""
    if key == "sp_fireworks":
        return "🎆 Бах! Феєрверк на твою честь!", EFFECT_PARTY
    if key == "sp_fireheart":
        return "❤️‍🔥 Це вогняне серце — тільки для тебе!", EFFECT_HEART
    if key == "sp_mask":
        prizes, weights = zip(*MASK_PRIZES)
        prize = random.choices(prizes, weights=weights, k=1)[0]
        partner.setdefault("gifts", {})
        partner["gifts"][prize] = partner["gifts"].get(prize, 0) + 1
        p_title, p_price = GIFT_CATALOG[prize]
        effect = EFFECT_PARTY if p_price >= 35 else None
        return (
            f"🎭 Ти відкриваєш маску-сюрприз… а там <b>{esc(p_title)}</b>! "
            "Вже у твоєму інвентарі — можеш залишити собі або подарувати далі."
        ), effect
    if key == "sp_oracle":
        return f"🔮 Передбачення для тебе:\n<i>{esc(random.choice(ORACLE_LINES))}</i>", None
    if key == "sp_card":
        try:
            phrase = CARD_PHRASES[int(arg)]
        except (ValueError, IndexError):
            phrase = CARD_PHRASES[0]
        return f"💌 На листівці написано:\n<i>«{esc(phrase)}»</i>", EFFECT_HEART
    if key == "sp_key":
        if arg == "city" and sender.get("city"):
            fact = f"співрозмовник з міста <b>{esc(sender['city'])}</b>"
        elif arg == "age" and str(sender.get("age") or "").strip().isdigit():
            fact = f"співрозмовнику <b>{esc(sender['age'])}</b>"
        else:
            keys = [k for k in INTEREST_LABELS if k in (sender.get("interests") or set())]
            fact = (f"співрозмовник любить <b>{INTEREST_LABELS[random.choice(keys)]}</b>" if keys
                    else "співрозмовник любить таємниці 😉")
        return f"🗝 Ключик відкриває одну таємницю: {fact}", None
    if key in COLLECTIBLE_GIFTS:
        nums = sender.get("collect_nums", {}).get(key) or []
        num = nums.pop(0) if nums else None
        title = GIFT_CATALOG[key][0]
        label = f"{title} #{num} з {COLLECTIBLE_GIFTS[key]}" if num else title
        partner.setdefault("collection", []).append(label)
        return f"🏆 Колекційний подарунок <b>{esc(label)}</b> тепер у твоїй колекції (👤 Мій профіль)!", EFFECT_PARTY
    if key in FUNNY_GIFT_TEXT:
        return FUNNY_GIFT_TEXT[key], None
    return "", None


@dp.callback_query(F.data == "gift_info")
async def gift_info(call: types.CallbackQuery):
    await call.answer(
        "🎁 Звичайні — класичні подарунки.\n"
        "✨ Особливі — щось роблять: ефект на весь екран, сюрприз, передбачення, листівка, "
        "ключик-таємниця про тебе або колекційний номерний подарунок.",
        show_alert=True,
    )


@dp.callback_query(F.data.startswith("sendgift_"))
async def send_gift_item(call: types.CallbackQuery):
    global gift_revenue_total
    user_id = call.from_user.id
    partner_id, kind = gift_target(user_id)
    if partner_id is None:
        await call.answer("Чат вже завершено", show_alert=True)
        return

    key, _sep, arg = call.data[len("sendgift_"):].partition(":")
    item = GIFT_CATALOG.get(key)
    if item is None:
        await call.answer("Невідомий подарунок", show_alert=True)
        return

    u = init_user(user_id)
    have = u.get("gifts", {}).get(key, 0)
    if have <= 0:
        await call.answer("У тебе немає такого подарунка в інвентарі", show_alert=True)
        return

    # Листівка і ключик: спершу дарувальник обирає, що саме подарувати
    if key == "sp_card" and not arg:
        rows = [[InlineKeyboardButton(text=ph, callback_data=f"sendgift_sp_card:{i}")]
                for i, ph in enumerate(CARD_PHRASES)]
        await call.message.answer("💌 Обери напис на листівці:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await call.answer()
        return
    if key == "sp_key" and not arg:
        opts = _key_options(u)
        if not opts:
            await call.answer("🗝 Заповни профіль (місто, вік чи інтереси) — тоді буде що відкрити", show_alert=True)
            return
        rows = [[InlineKeyboardButton(text=label, callback_data=f"sendgift_sp_key:{code}")] for code, label in opts]
        await call.message.answer(
            "🗝 Який факт про себе відкриєш співрозмовнику? Інше лишиться таємницею.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await call.answer()
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
    earned = earn_from_gift(u, p, price)
    extra, effect = special_gift_payload(key, arg, u, p) if key in SPECIAL_GIFTS else ("", None)
    request_save()

    await call.message.answer(f"🎁 Подаровано: <b>{esc(title)}</b>!")
    text = (
        (f"🎁 {gift_from_label(user_id, kind)} {gift_verb(user_id)} тобі: <b>{esc(title)}</b>!\n" if kind == "friend"
         else f"🎁 Співрозмовник подарував вам: <b>{esc(title)}</b>!\n")
        + (f"{extra}\n\n" if extra else "")
        + f"На баланс нараховано {recipient_amount:.2f} грн."
        + (f"\n💸 {uah(earned)} з них можна буде вивести на карту через {EARN_HOLD_DAYS} днів." if earned else "")
    )
    await send_with_effect(partner_id, text, effect)
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
        await message.answer(f"🔐 <b>Адмін-панель</b> · версія {BOT_VERSION}", reply_markup=get_admin_keyboard())
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
    await message.answer(f"✅ Доступ надано.\n\n🔐 <b>Адмін-панель</b> · версія {BOT_VERSION}", reply_markup=get_admin_keyboard())


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
                bucket = total.setdefault(key, {})
                for sub, amount in val.items():
                    bucket[sub] = bucket.get(sub, 0) + amount
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
        f"👑 Засновників: <b>{founders_count()}</b> з {FOUNDERS_LIMIT}",
        f"🆕 Нових: <b>{t['new']}</b>",
        *[
            f"     {source_label(code)}: {n}" + (f" (спілкувались {chatted})" if n else "")
            for code, n, chatted in source_breakdown(list(dict.fromkeys(t.get("new_ids", []))))
        ],
        f"🙋 Активних: <b>{len(t['active'])}</b>",
        f"💬 Чатів: <b>{t['chats']}</b> (справжніх від 1 хв: {t['real_chats']})",
        f"     ❤️ флірт {t['flirt']} · 🧩 інтереси {t['interest']} · 👫 друзі {t['friends']}",
        f"✉️ Повідомлень: {t['msgs']}",
        f"👋 Покликано «хтось шукає»: {t.get('wait_pings', 0)}",
        f"🧭 Кімнати: входів {t.get('room_join', 0)} · фоновий пошук {t.get('bg_search', 0)} · "
        f"«Ми скучили» {t.get('comeback', 0)}" + (f" · 🎃 гарбузів {t.get('pumpkins', 0)}" if t.get('pumpkins') else ""),
        "🚨 Скарги: " + (" ".join(f"{REPORT_REASONS.get(k, k).split(' ')[0]}{n}" for k, n in t.get("reports", {}).items()) or "0"),
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
    extra = [source_label(u.get("source")), f"💬 чатів: {u.get('total_chats', 0)}"]
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
    gift_btns = [
        InlineKeyboardButton(text=f"🎁 {users_db[uid].get('custom_id', uid)}", callback_data=f"adm_gift_to:{uid}")
        for uid in chunk
    ]
    kb += [gift_btns[i:i + 3] for i in range(0, len(gift_btns), 3)]
    kb.append([InlineKeyboardButton(text="⬅️ До статистики", callback_data=f"adm_st_{period}")])
    try:
        await call.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), disable_web_page_preview=True)
    except TelegramAPIError:
        pass
    await call.answer()


@dp.callback_query(F.data == "adm_links")
@admin_only
async def adm_links(call: types.CallbackQuery):
    base = f"https://t.me/{BOT_USERNAME}?start=" if BOT_USERNAME else "https://t.me/ВАШ_БОТ?start="
    lines = [
        "🔗 <b>Посилання з мітками</b>",
        "Став різні посилання в різних місцях — і в статистиці буде видно, звідки прийшли люди.",
        "",
    ]
    for code in ("tt", "yt", "ig", "tgads"):
        lines.append(f"{SOURCE_LABELS[code]}\n<code>{base}{code}</code>\n")
    lines += [
        "💬 <b>Власна мітка</b> — для конкретного чату чи реклами в каналі:",
        f"<code>{base}chat1</code>",
        "Після <code>start=</code> пиши будь-що латиницею й цифрами (напр. <code>kanal_memy</code>) — "
        "у статистиці з'явиться окремим рядком.",
        "",
        "<i>Мітка зараховується лише новим людям — тим, хто відкрив бот уперше.</i>",
    ]
    await call.message.answer("\n".join(lines), disable_web_page_preview=True)
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
        why = REPORT_REASONS.get(r.get("reason"), "") if r.get("reason") else ""
        lines.append(
            f"#{r['id']} — {r['time']}" + (f" · <b>{why}</b>" if why else "")
            + f"\nВід {admin_label(r['from'])}\nНа {admin_label(r['on'])}\n📊 {report_history(r['on'])}\n"
        )
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
    ok = await safe_send(target_id, f"📩 <b>Відповідь від підтримки:</b>\n{protect_text(esc(message.text))}")
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
    return f"📣 <i>Реклама</i>\n\n{protect_text(esc(ad['text']))}"


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


@dp.message(Command("clearage"))
async def clear_age_history(message: types.Message):
    """Адмін: очистити історію віку людини (архів профілю і найменший вік) — для тестових акаунтів
    і випадків, коли людина справді помилилась із віком. Поточний вік у профілі лишається."""
    if not _is_admin(message.from_user.id):
        return
    arg = (message.text or "").split(maxsplit=1)
    uid = resolve_user_id(arg[1]) if len(arg) > 1 else None
    u = users_db.get(uid) if uid else None
    if not u:
        await message.answer("Напиши так: <code>/clearage ID_1234</code> (наш ID або Telegram ID)")
        return
    had = len(u.get("archive") or [])
    u["archive"] = []
    u.pop("min_age_seen", None)
    request_save()
    await message.answer(
        f"🧹 Історію віку очищено: {esc(admin_label(uid))}\n"
        f"Видалено записів архіву: {had}. Поточний вік у профілі: {esc(str(u.get('age') or '—'))}."
    )


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
MATCH_SCORE_MAX = 5  # 2 — однаковий «рівень» рейтингу, +1 — спільний інтерес, +1 — настрій, +1 — новачку «теплий» співрозмовник


def is_newbie(u: dict) -> bool:
    """Ще жодного разу ні з ким не з'єднувався."""
    return not u.get("total_chats")


def is_warm(u: dict) -> bool:
    """Досвідчений і привітний: кілька чатів, рейтинг не низький."""
    return u.get("total_chats", 0) >= 3 and not is_low_rated(u)


def match_score(u: dict, p: dict, mode: str) -> int:
    """Чим більше балів, тим краща пара. Люди з низьким рейтингом спершу потрапляють одне до одного."""
    score = 2 if is_low_rated(u) == is_low_rated(p) else 0
    if not mode.startswith("int:") and (u.get("interests") or set()) & (p.get("interests") or set()):
        score += 1
    if current_mood(u) and current_mood(u) == current_mood(p):
        score += 1  # 🎭 на одній хвилі
    if u.get("city") and u.get("city") == p.get("city"):
        score += 1  # 🏙 з одного міста
    if (is_newbie(u) and is_warm(p)) or (is_newbie(p) and is_warm(u)):
        score += 1  # 🆕 новачку — досвідченого привітного співрозмовника, щоб перший чат вдався
    return score


def rating_line(u: dict) -> str:
    up, down = u.get("rating_up", 0), u.get("rating_down", 0)
    if not up + down:
        return "ще немає оцінок"
    return f"👍 {up} · 👎 {down} ({rating_percent(u)}%)"


def interests_line(u: dict) -> str:
    keys = [k for k in INTEREST_LABELS if k in (u.get("interests") or set())]
    return ", ".join(INTEREST_LABELS[k] for k in keys) if keys else "не обрано"


# 💡 Перше повідомлення: одне відкрите питання за інтересом — щоб не думати, з чого почати
OPENERS = {
    "music": "Що ти зараз слухаєш на повторі?",
    "movies": "Який фільм чи серіал ти можеш передивлятися безкінечно?",
    "games": "У що зараз граєш?",
    "travel": "Де тобі найбільше сподобалося бувати?",
    "it": "Яка технологія чи гаджет тебе останнім часом вразили?",
    "sport": "Який спорт тобі до душі — дивитися чи займатися?",
    "books": "Яку книжку ти радиш усім?",
    "pets": "У тебе є улюбленець — кіт, пес чи хтось інший?",
    "cook": "Яку страву ти готуєш найкраще?",
    "fitness": "Як ти знаходиш мотивацію на тренування?",
    "art": "Що ти найбільше любиш малювати?",
    "photo": "Що найчастіше потрапляє тобі в кадр?",
    "instr": "На чому граєш і скільки вже вчишся?",
    "plants": "Яка рослина в тебе найпримхливіша?",
    "cars": "Яке авто — твоя мрія?",
    "yoga": "Що тобі найкраще допомагає розслабитися?",
    "craft": "Що ти останнє зробив(-ла) своїми руками?",
    "fishing": "Яка твоя найкраща рибалка?",
    "anime": "Яке аніме ти порадиш новачкові?",
    "dance": "Під яку музику тебе неможливо втримати на місці?",
}


def opener_line(keys: list) -> str:
    keys = [k for k in keys if k in OPENERS]
    if not keys:
        return ""
    return f"\n💡 Можна почати так: «{OPENERS[random.choice(keys)]}»"


async def send_interest_notes(user_id: int, u: dict, partner_id: int, p: dict):
    u_open, p_open = not u.get("hide_interests"), not p.get("hide_interests")
    common = [k for k in INTEREST_LABELS if k in (u.get("interests") or set()) & (p.get("interests") or set())]
    if common and u_open and p_open:
        note = "🧩 Спільні інтереси: <b>" + ", ".join(INTEREST_LABELS[k] for k in common) + "</b> — є з чого почати 😉"
        await safe_send(user_id, note + opener_line(common))
        await safe_send(partner_id, note + opener_line(common))
        return
    for me, other, is_open in ((user_id, p, p_open), (partner_id, u, u_open)):
        if is_open and other.get("interests"):
            keys = [k for k in INTEREST_LABELS if k in (other.get("interests") or set())]
            await safe_send(me, f"🧩 Інтереси співрозмовника: {interests_line(other)}" + opener_line(keys))


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


# === КІНЕЦЬ part3.py ===
