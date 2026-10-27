"""
In-memory anonymous chat logger for an aiogram 3 bot.

This module deliberately does not use a database or disk storage.
It stores Telegram source message IDs in RAM and copies messages to the
admin only when the admin opens a chat. The bot must still have access to
the original private messages in the sender's chat with the bot.

Integration expectations:
- Call chat_logger.start_chat(a, b, mode) from on_chat_started(a, b, mode).
- Call chat_logger.log_message(message, user_id, partner_id) after a message
  has successfully been relayed.
- Call chat_logger.end_chat(a, b) from on_chat_ended(a, b).
- Register the admin handlers shown in the integration notes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

log = logging.getLogger(__name__)

KYIV_TZ = ZoneInfo("Europe/Kyiv")

# Conservative defaults; tune only if your hosting RAM budget allows it.
MAX_CHATS = 150
MAX_MESSAGES_PER_CHAT = 80
MAX_TOTAL_MESSAGES = 5000
MAX_MEDIA_MESSAGES_PER_CHAT = 30
MAX_MESSAGE_AGE_SECONDS = 24 * 60 * 60
ADMIN_PAGE_SIZE = 8


@dataclass
class LoggedMessage:
    sender_id: int
    source_chat_id: int
    source_message_id: int
    timestamp: float
    content_type: str
    caption: str = ""


@dataclass
class LoggedChat:
    chat_id: str
    users: tuple[int, int]
    mode: str = "anonymous"
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    messages: list[LoggedMessage] = field(default_factory=list)


class InMemoryChatLogger:
    def __init__(self, admin_id: int) -> None:
        self.admin_id = int(admin_id)
        self.chats: OrderedDict[str, LoggedChat] = OrderedDict()
        self.active_by_user: dict[int, str] = {}
        self._message_count = 0
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None

    def _new_chat_key(self, a: int, b: int) -> str:
        # Stable for the pair while still allowing repeated sessions to be distinct.
        return f"{min(int(a), int(b))}:{max(int(a), int(b))}:{time.time_ns()}"

    async def start(self) -> None:
        if self._cleanup_task and not self._cleanup_task.done():
            return
        self._cleanup_task = asyncio.create_task(self._midnight_cleanup_loop())

    async def close(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
        async with self._lock:
            self.chats.clear()
            self.active_by_user.clear()
            self._message_count = 0

    async def start_chat(self, a: int, b: int, mode: str = "anonymous") -> None:
        if int(a) == int(b):
            return
        async with self._lock:
            # If a user is still mapped to an older session, detach only the mapping.
            key = self._new_chat_key(a, b)
            chat = LoggedChat(chat_id=key, users=(int(a), int(b)), mode=str(mode))
            self.chats[key] = chat
            self.active_by_user[int(a)] = key
            self.active_by_user[int(b)] = key
            self._trim_chats_locked()

    async def log_message(self, message: Message, user_id: int, partner_id: int) -> None:
        if int(user_id) == int(partner_id):
            return
        async with self._lock:
            key = self.active_by_user.get(int(user_id))
            chat = self.chats.get(key) if key else None
            # If a start callback was missed, create a minimal session.
            if chat is None or int(partner_id) not in chat.users:
                key = self._new_chat_key(user_id, partner_id)
                chat = LoggedChat(chat_id=key, users=(int(user_id), int(partner_id)))
                self.chats[key] = chat
                self.active_by_user[int(user_id)] = key
                self.active_by_user[int(partner_id)] = key

            content_type = getattr(message, "content_type", "unknown")
            item = LoggedMessage(
                sender_id=int(user_id),
                source_chat_id=int(message.chat.id),
                source_message_id=int(message.message_id),
                timestamp=time.time(),
                content_type=str(content_type),
                caption=(message.caption or "")[:500],
            )
            chat.messages.append(item)
            self._message_count += 1

            # Per-chat limits: evict oldest entries, then global limits.
            while len(chat.messages) > MAX_MESSAGES_PER_CHAT:
                chat.messages.pop(0)
                self._message_count -= 1
            media_count = sum(1 for m in chat.messages if m.content_type not in ("text", "unknown"))
            while media_count > MAX_MEDIA_MESSAGES_PER_CHAT:
                for idx, old in enumerate(chat.messages):
                    if old.content_type not in ("text", "unknown"):
                        chat.messages.pop(idx)
                        self._message_count -= 1
                        media_count -= 1
                        break
            self._trim_messages_locked()
            self._trim_chats_locked()

    async def end_chat(self, a: int, b: int) -> None:
        async with self._lock:
            key_a = self.active_by_user.pop(int(a), None)
            key_b = self.active_by_user.pop(int(b), None)
            key = key_a if key_a == key_b else (key_a or key_b)
            chat = self.chats.get(key) if key else None
            if chat:
                chat.ended_at = time.time()

    async def list_chats(self, page: int = 0) -> tuple[list[LoggedChat], int]:
        async with self._lock:
            self._expire_old_locked()
            all_chats = list(reversed(self.chats.values()))
            total_pages = max(1, (len(all_chats) + ADMIN_PAGE_SIZE - 1) // ADMIN_PAGE_SIZE)
            page = max(0, min(int(page), total_pages - 1))
            start = page * ADMIN_PAGE_SIZE
            return all_chats[start:start + ADMIN_PAGE_SIZE], total_pages

    async def get_chat(self, key: str) -> LoggedChat | None:
        async with self._lock:
            self._expire_old_locked()
            return self.chats.get(key)

    def _trim_messages_locked(self) -> None:
        while self._message_count > MAX_TOTAL_MESSAGES:
            oldest_chat = next(iter(self.chats.values()), None)
            if oldest_chat is None:
                self._message_count = 0
                return
            if oldest_chat.messages:
                oldest_chat.messages.pop(0)
                self._message_count -= 1
            else:
                self.chats.pop(oldest_chat.chat_id, None)

    def _trim_chats_locked(self) -> None:
        while len(self.chats) > MAX_CHATS:
            key, chat = self.chats.popitem(last=False)
            self._message_count -= len(chat.messages)
            for uid in chat.users:
                if self.active_by_user.get(uid) == key:
                    self.active_by_user.pop(uid, None)

    def _expire_old_locked(self) -> None:
        cutoff = time.time() - MAX_MESSAGE_AGE_SECONDS
        for key in list(self.chats.keys()):
            chat = self.chats[key]
            old_len = len(chat.messages)
            chat.messages[:] = [m for m in chat.messages if m.timestamp >= cutoff]
            self._message_count -= old_len - len(chat.messages)
            if chat.ended_at is not None and chat.ended_at < cutoff and not chat.messages:
                self.chats.pop(key, None)
                for uid in chat.users:
                    if self.active_by_user.get(uid) == key:
                        self.active_by_user.pop(uid, None)

    async def clear(self) -> None:
        async with self._lock:
            self.chats.clear()
            self.active_by_user.clear()
            self._message_count = 0

    async def _midnight_cleanup_loop(self) -> None:
        while True:
            now = datetime.now(KYIV_TZ)
            tomorrow = (now + timedelta(days=1)).date()
            midnight = datetime.combine(tomorrow, dt_time.min, tzinfo=KYIV_TZ)
            await asyncio.sleep(max(1, (midnight - now).total_seconds()))
            await self.clear()


def build_admin_router(logger: InMemoryChatLogger, admin_id: int) -> Router:
    """Create a router with admin-only callbacks. Include it in the Dispatcher."""
    router = Router(name="in_memory_chat_logger_admin")
    admin_id = int(admin_id)

    def is_admin(user_id: int | None) -> bool:
        return user_id is not None and int(user_id) == admin_id

    def list_keyboard(chats: list[LoggedChat], page: int, total_pages: int) -> InlineKeyboardMarkup:
        rows = []
        for chat in chats:
            a, b = chat.users
            status = "🟢" if chat.ended_at is None else "⚪"
            rows.append([InlineKeyboardButton(
                text=f"{status} {a} ↔ {b} · {len(chat.messages)} пов.",
                callback_data=f"cl_open:{chat.chat_id}",
            )])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"cl_page:{page-1}"))
        nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="cl_noop"))
        if page + 1 < total_pages:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"cl_page:{page+1}"))
        rows.append(nav)
        rows.append([InlineKeyboardButton(text="🧹 Очистити журнал", callback_data="cl_clear")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def show_list(target: Message | CallbackQuery, page: int = 0) -> None:
        chats, total_pages = await logger.list_chats(page)
        body = "💬 <b>Журнал анонімних чатів</b>\n"
        body += f"Чатів у пам’яті: {len(logger.chats)}\n\n"
        if not chats:
            body += "Журнал порожній."
        markup = list_keyboard(chats, page, total_pages)
        if isinstance(target, CallbackQuery):
            if target.message:
                await target.message.edit_text(body, reply_markup=markup, parse_mode="HTML")
                await target.answer()
        else:
            await target.answer(body, reply_markup=markup, parse_mode="HTML")

    @router.callback_query(F.data == "adm_chats")
    async def open_chat_log(call: CallbackQuery) -> None:
        if not is_admin(call.from_user.id):
            await call.answer("⛔ Доступ заборонено.", show_alert=True)
            return
        await show_list(call, 0)

    @router.callback_query(F.data.startswith("cl_page:"))
    async def page_chat_log(call: CallbackQuery) -> None:
        if not is_admin(call.from_user.id):
            await call.answer("⛔ Доступ заборонено.", show_alert=True)
            return
        try:
            page = int((call.data or "").split(":", 1)[1])
        except (ValueError, IndexError):
            await call.answer("Некоректна сторінка.", show_alert=True)
            return
        await show_list(call, page)

    @router.callback_query(F.data == "cl_noop")
    async def noop(call: CallbackQuery) -> None:
        if not is_admin(call.from_user.id):
            await call.answer("⛔ Доступ заборонено.", show_alert=True)
            return
        await call.answer()

    @router.callback_query(F.data == "cl_clear")
    async def clear_chat_log(call: CallbackQuery) -> None:
        if not is_admin(call.from_user.id):
            await call.answer("⛔ Доступ заборонено.", show_alert=True)
            return
        await logger.clear()
        if call.message:
            await call.message.edit_text("🧹 Журнал очищено. Дані видалені з оперативної пам’яті.")
        await call.answer("Очищено.")

    @router.callback_query(F.data.startswith("cl_open:"))
    async def inspect_chat(call: CallbackQuery, bot: Bot) -> None:
        if not is_admin(call.from_user.id):
            await call.answer("⛔ Доступ заборонено.", show_alert=True)
            return
        key = (call.data or "").split(":", 1)[1]
        chat = await logger.get_chat(key)
        if chat is None:
            await call.answer("Цей чат уже видалено.", show_alert=True)
            return
        a, b = chat.users
        status = "активний" if chat.ended_at is None else "завершений"
        await call.answer("Відкриваю журнал…")
        if call.message:
            await call.message.answer(
                f"📂 <b>Анонімний чат</b>\nУчасники: <code>{a}</code> ↔ <code>{b}</code>\n"
                f"Статус: {status}\nПовідомлень: {len(chat.messages)}",
                parse_mode="HTML",
            )
        # Copy original messages in order. If Telegram no longer permits a copy,
        # report it without crashing the bot.
        for item in list(chat.messages):
            try:
                await bot.copy_message(
                    chat_id=admin_id,
                    from_chat_id=item.source_chat_id,
                    message_id=item.source_message_id,
                )
            except TelegramAPIError as exc:
                log.info("Could not copy logged message %s: %s", item.source_message_id, exc)
                try:
                    await bot.send_message(
                        admin_id,
                        f"⚠️ Не вдалося показати повідомлення (тип: {item.content_type}, "
                        f"ID користувача: {item.sender_id}).",
                    )
                except TelegramAPIError:
                    pass
        if call.message:
            await call.message.answer(
                "Кінець журналу цього чату.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="⬅️ До списку чатів", callback_data="adm_chats")
                ]]),
            )

    return router
