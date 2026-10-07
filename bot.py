import asyncio
import logging
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

BOT_TOKEN = "ТВІЙ_ТЕЛЕГРАМ_ТОКЕН"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Бази даних у пам'яті
users_profile = {}  # {user_id: {"gender": "...", "age": ...}}
queue = []          # Черга користувачів
active_chats = {}   # {user_id: partner_id}

# Створення станів для опитання (FSM)
class Registration(StatesGroup):
    gender = State()
    age = State()

# Клавіатури
def get_gender_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Хлопець 🧑"), KeyboardButton(text="Дівчина 👩")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Шукати співрозмовника")],
            [KeyboardButton(text="👤 Мій профіль"), KeyboardButton(text="⭐ Преміум / Фільтри")]
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

# --- 1. СТАРТ ТА РЕЄСТРАЦІЯ ---

@dp.message(CommandStart())
async def start_handler(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    # Якщо користувач вже має профіль
    if user_id in users_profile:
        await message.answer(
            "З поверненням до ANONimchyk! 🤫\nНатисни кнопку нижче, щоб розпочати пошук.",
            reply_markup=get_main_keyboard()
        )
        return

    # Якщо профілю немає — починаємо опитування
    await state.set_state(Registration.gender)
    await message.answer(
        "Привіт! Ласкаво просимо до анонімного чату ANONimchyk 🤫\n\n"
        "Перед початком давай познайомимося. **Обери свою стать:**",
        reply_markup=get_gender_keyboard(),
        parse_mode="Markdown"
    )

@dp.message(Registration.gender, F.text.in_(["Хлопець 🧑", "Дівчина 👩"]))
async def process_gender(message: types.Message, state: FSMContext):
    await state.update_data(gender=message.text)
    await state.set_state(Registration.age)
    await message.answer(
        "Чудово! Тепер **вкажи свій вік** (введи число від 12 до 99):",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )

@dp.message(Registration.gender)
async def process_gender_invalid(message: types.Message):
    await message.answer("Будь ласка, обери варіант з кнопки нижче 👇", reply_markup=get_gender_keyboard())

@dp.message(Registration.age)
async def process_age(message: types.Message, state: FSMContext):
    if not message.text.isdigit() or not (12 <= int(message.text) <= 99):
        await message.answer("Будь ласка, введи коректний вік числом (від 12 до 99):")
        return

    user_data = await state.get_data()
    users_profile[message.from_user.id] = {
        "gender": user_data["gender"],
        "age": int(message.text)
    }
    
    await state.clear()
    await message.answer(
        "Реєстрацію успішно завершено! 🎉\nТепер ти можеш шукати співрозмовників.",
        reply_markup=get_main_keyboard()
    )

# --- 2. ПОШУК ТА ЧАТ ---

@dp.message(Command("search"))
@dp.message(F.text == "🔍 Шукати співрозмовника")
async def search_partner(message: types.Message, state: FSMContext):
    user_id = message.from_user.id

    # Перевірка реєстрації
    if user_id not in users_profile:
        await start_handler(message, state)
        return

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

@dp.message(Command("stop"))
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

# --- 3. ПРОФІЛЬ ТА ПРЕМІУМ ---

@dp.message(F.text == "👤 Мій профіль")
async def show_profile(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id not in users_profile:
        await start_handler(message, state)
        return
    
    prof = users_profile[user_id]
    await message.answer(f"Твій профіль:\n• Стать: {prof['gender']}\n• Вік: {prof['age']}")

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

# --- 4. ПЕРЕСИЛАННЯ ПОВІДОМЛЕНЬ ---

@dp.message()
async def relay_messages(message: types.Message, state: FSMContext):
    user_id = message.from_user.id

    if user_id not in users_profile:
        await start_handler(message, state)
        return

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
