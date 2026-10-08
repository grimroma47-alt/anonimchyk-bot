import asyncio
import logging
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

# Вставте токен бота від @BotFather:
BOT_TOKEN = "1871367738:AAHHWt3e1WE5p_nV4RfhtbD_Bo4-j192X88"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Сховище для черги та активних чатів (у пам'яті)
queue = []          # Черга користувачів, які шукають пару
active_chats = {}   # Активні чати: {user_id: partner_id}

# Клавіатури
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

    # Якщо в черзі хтось є — з'єднуємо
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

# Пересилання усіх типів повідомлень (текст, фото, відео, voice)
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

async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
