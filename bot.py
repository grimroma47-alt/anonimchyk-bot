import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiohttp import web

# Токен береться із Environment Variables у Render або вкажіть вручну:
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ТВІЙ_ТЕЛЕГРАМ_ТОКЕН")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

queue = []
active_chats = {}

def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Шукати співрозмовника")],
            [KeyboardButton(text="⭐ Преміум / Фільтри")]
        ],
        resize_keyboard=True
    )

def get_chat_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="❌ Завершити чат")]
        ],
        resize_keyboard=True
    )

@dp.message(CommandStart())
async def start_handler(message: types.Message):
    await message.answer(
        "Привіт! Ласкаво просимо до анонімного чату ANONimchyk 🤫\n\n"
        "Тут ти можеш спілкуватися 1 на 1 та обмінюватися фото/відео.\n"
        "Натисни кнопку нижче, щоб розпочати пошук!",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )

@dp.message(F.text == "🔍 Шукати співрозмовника")
async def search_partner(message: types.Message):
    user_id = message.from_user.id

    if user_id in active_chats:
        await message.answer("Ти вже перебуваєш у чаті!")
        return

    if user_id in queue:
        await message.answer("Ти вже в черзі пошуку. Зачекай трохи...")
        return

    if queue:
        partner_id = queue.pop(0)
        active_chats[user_id] = partner_id
        active_chats[partner_id] = user_id

        await bot.send_message(user_id, "Партнера знайдено! Приємного спілкування 🤫", reply_markup=get_chat_keyboard())
        await bot.send_message(partner_id, "Партнера знайдено! Приємного спілкування 🤫", reply_markup=get_chat_keyboard())
    else:
        queue.append(user_id)
        await message.answer("Шукаємо співрозмовника... Зачекай ⏳", reply_markup=ReplyKeyboardRemove())

@dp.message(F.text == "❌ Завершити чат")
async def stop_chat(message: types.Message):
    user_id = message.from_user.id

    if user_id in queue:
        queue.remove(user_id)
        await message.answer("Пошук зупинено.", reply_markup=get_main_keyboard())
        return

    if user_id not in active_chats:
        await message.answer("Ти зараз не в чаті.", reply_markup=get_main_keyboard())
        return

    partner_id = active_chats.pop(user_id)
    if partner_id in active_chats:
        del active_chats[partner_id]

    await message.answer("Чат завершено.", reply_markup=get_main_keyboard())
    await bot.send_message(partner_id, "Співрозмовник завершив чат.", reply_markup=get_main_keyboard())

@dp.message(F.text == "⭐ Преміум / Фільтри")
async def premium_menu(message: types.Message):
    await message.answer(
        "💎 **Преміум підписка** дозволяє обирати стать та вік співрозмовника!\n\n"
        "Обери тариф:\n"
        "1️⃣ **1 тиждень** — $2.99\n"
        "2️⃣ **1 місяць** — $7.99\n"
        "3️⃣ **1 рік** — $39.99",
        parse_mode="Markdown"
    )

@dp.message()
async def relay_messages(message: types.Message):
    user_id = message.from_user.id

    if user_id not in active_chats:
        await message.answer("Щоб відправляти повідомлення, спочатку знайди співрозмовника!", reply_markup=get_main_keyboard())
        return

    partner_id = active_chats[user_id]

    try:
        await message.copy_to(chat_id=partner_id)
    except Exception:
        await message.answer("Не вдалося доставити повідомлення. Співрозмовник міг заблокувати бота.")

# Простий веб-сервер для того, щоб Render бачив, що сервіс працює
async def handle(request):
    return web.Response(text="Bot is running!")

async def main():
    logging.basicConfig(level=logging.INFO)
    
    # Запускаємо веб-сервер для Render на призначеному порту
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

    # Запускаємо бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
