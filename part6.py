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
        chatlog_record(user_id, message)
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
        types.BotCommand(command="where", description="📍 Де я?"),
        types.BotCommand(command="restart", description="🔄 Перезапустити (якщо щось зависло)"),
        types.BotCommand(command="call", description="📞 Дзвінок співрозмовнику (18+, Premium)"),
        types.BotCommand(command="friends", description="👫 Друзі"),
        types.BotCommand(command="filters", description="🎯 Фільтри пошуку"),
        types.BotCommand(command="interests", description="🧩 Пошук за інтересами"),
        types.BotCommand(command="voice", description="🎙 Маска голосу"),
        types.BotCommand(command="balance", description="💰 Мій баланс"),
        types.BotCommand(command="gifts", description="🎁 Магазин подарунків"),
        types.BotCommand(command="invite", description="🤝 Запросити друга"),
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
    chatlog_task = asyncio.create_task(chatlog_reset_loop())
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
        chatlog_task.cancel()
        await save_state_to_db()  # фінальне збереження при зупинці (деплой/перезапуск)
        await release_lease()  # тепер нова копія може забрати свіжі дані
        await bot.session.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())


# === КІНЕЦЬ part6.py ===
