import asyncio
import logging
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

BOT_TOKEN = "1871367738:AAE_pjFf44VESFaF1ecR7ElDLk7FiNfDKXk"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# База даних у пам'яті (у продакшені краще використовувати SQLite/PostgreSQL)
users_db = {}
# Формат: user_id: {
#     "custom_id": "ID_1001", "nickname": "NoName", "gender": "Не вказано",
#     "age": "Не вказано", "country": "Не вказано", "balance": 0.0,
#     "is_premium": False, "archive": []
# }

queue = []
active_chats = {}
user_counter = 1000

# FSM для реєстрації/профілю, зміни ніку та поповнення
class ProfileStates(StatesGroup):
    gender = State()
    age = State()
    country = State()

class WalletStates(StatesGroup):
    change_nick = State()
    deposit_amount = State()

# --- Клавіатури ---
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Шукати співрозмовника")],
            [KeyboardButton(text="🏪 Магазин"), KeyboardButton(text="👛 Гаманець")],
            [KeyboardButton(text="👤 Мій профіль / Архів"), KeyboardButton(text="⚙️ Налаштування")]
        ],
        resize_keyboard=True
    )

def get_chat_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="❌ Завершити чат"), KeyboardButton(text="🚨 Поскаржитися")]
        ],
        resize_keyboard=True
    )

def get_shop_keyboard():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1. Зміна нікнейму — 20 грн", callback_data="buy_nick")],
        [InlineKeyboardButton(text="2. Пріоритет у пошуку (1 день) — 30 грн", callback_data="buy_prio_1d")],
        [InlineKeyboardButton(text="3. VIP-значок у профілі — 40 грн", callback_data="buy_vip_badge")],
        [InlineKeyboardButton(text="4. Розширений чорний список — 50 грн", callback_data="buy_blacklist")],
        [InlineKeyboardButton(text="5. Доступ до фільтру за статтю (1 тиж) — 65 грн", callback_data="buy_gender_filter")],
        [InlineKeyboardButton(text="6. Доступ до фільтру за віком (1 тиж) — 65 грн", callback_data="buy_age_filter")],
        [InlineKeyboardButton(text="7. Premium на 1 тиждень — 80 грн", callback_data="buy_prem_1w")],
        [InlineKeyboardButton(text="8. Premium на 1 місяць — 180 грн", callback_data="buy_prem_1m")],
        [InlineKeyboardButton(text="9. Premium на 3 місяці — 350 грн", callback_data="buy_prem_3m")],
        [InlineKeyboardButton(text="10. Безлімітний Premium (назавжди) — 850 грн", callback_data="buy_prem_forever")]
    ])
    return keyboard

def get_wallet_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Поповнити баланс", callback_data="deposit")],
        [InlineKeyboardButton(text="✏️ Змінити ID на нікнейм (20 грн)", callback_data="change_nick_btn")]
    ])

# --- Допоміжна функція реєстрації ---
def init_user(user_id):
    global user_counter
    if user_id not in users_db:
        user_counter += 1
        users_db[user_id] = {
            "custom_id": f"ID_{user_counter}",
            "nickname": f"Користувач_{user_counter}",
            "gender": "Не вказано",
            "age": "Не вказано",
            "country": "Не вказано",
            "balance": 0.0,
            "is_premium": False,
            "archive": []  # Зберігає історію змін профілю
        }

# --- Обробники команд ---
@dp.message(CommandStart())
async def start_handler(message: types.Message):
    init_user(message.from_user.id)
    await message.answer(
        f"Привіт, {message.from_user.first_name}! Вітаємо в анонімному чаті! 🤫\n\n"
        f"Твій унікальний номер: **{users_db[message.from_user.id]['custom_id']}**\n"
        "Заповни свій профіль, щоб отримувати кращі рекомендації в пошуку.",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )

# --- Профіль та Архів ---
@dp.message(F.text == "👤 Мій профіль / Архів")
async def profile_handler(message: types.Message, state: FSMContext):
    init_user(message.from_user.id)
    u = users_db[message.from_user.id]
    
    archive_text = "\n".join(u["archive"][-5:]) if u["archive"] else "Історія порожня"
    
    text = (
        f"👤 **Твій профіль:**\n"
        f"• **ID / Нік:** {u['nickname']} ({u['custom_id']})\n"
        f"• **Стать:** {u['gender']}\n"
        f"• **Вік:** {u['age']}\n"
        f"• **Країна:** {u['country']}\n"
        f"• **Преміум:** {'Так 💎' if u['is_premium'] else 'Ні ❌'}\n\n"
        f"📜 **Архів останніх змін профілю:**\n{archive_text}\n\n"
        "Хочеш оновити свої дані? Введи /edit_profile"
    )
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("edit_profile"))
async def edit_profile_start(message: types.Message, state: FSMContext):
    await message.answer("Вкажи свою стать (Хлопець / Дівчина):")
    await state.set_state(ProfileStates.gender)

@dp.message(ProfileStates.gender)
async def process_gender(message: types.Message, state: FSMContext):
    await state.update_data(gender=message.text)
    await message.answer("Вкажи свій вік (наприклад, 18, 22 або <18):")
    await state.set_state(ProfileStates.age)

@dp.message(ProfileStates.age)
async def process_age(message: types.Message, state: FSMContext):
    await state.update_data(age=message.text)
    await message.answer("Вкажи свою країну або місто:")
    await state.set_state(ProfileStates.country)

@dp.message(ProfileStates.country)
async def process_country(message: types.Message, state: FSMContext):
    data = await state.get_data()
    u = users_db[message.from_user.id]
    
    # Збереження в архів
    old_info = f"Стать: {u['gender']}, Вік: {u['age']}, Країна: {u['country']}"
    u["archive"].append(old_info)
    
    # Оновлення нових даних
    u["gender"] = data["gender"]
    u["age"] = data["age"]
    u["country"] = message.text
    
    await state.clear()
    await message.answer("✅ Профіль успішно оновлено! Дані збережено в архів.", reply_markup=get_main_keyboard())

# --- Гаманець ---
@dp.message(F.text == "👛 Гаманець")
async def wallet_handler(message: types.Message):
    init_user(message.from_user.id)
    u = users_db[message.from_user.id]
    text = (
        f"👛 **Твій Гаманець**\n\n"
        f"• **Поточний баланс:** {u['balance']} грн\n"
        f"• **Ваш ID/Нік:** {u['nickname']}\n\n"
        "Тут ви можете поповнити баланс або змінити системний номер на свій нікнейм."
    )
    await message.answer(text, reply_markup=get_wallet_keyboard(), parse_mode="Markdown")

@dp.callback_query(F.data == "deposit")
async def deposit_start(call: types.CallbackQuery, state: FSMContext):
    await call.message.answer("Введіть суму в гривнях для поповнення (наприклад: 50, 100, 200):")
    await state.set_state(WalletStates.deposit_amount)
    await call.answer()

@dp.message(WalletStates.deposit_amount)
async def deposit_finish(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount <= 0:
            raise ValueError
        users_db[message.from_user.id]["balance"] += amount
        await message.answer(f"✅ Успішно! Ваш баланс поповнено на {amount} грн.", reply_markup=get_main_keyboard())
        await state.clear()
    except ValueError:
        await message.answer("Будь ласка, введіть коректне число більше 0.")

@dp.callback_query(F.data == "change_nick_btn")
async def change_nick_start(call: types.CallbackQuery, state: FSMContext):
    u = users_db[call.from_user.id]
    if u["balance"] < 20:
        await call.message.answer("❌ Недостатньо коштів! Зміна ніку коштує 20 грн. Поповніть гаманець.")
    else:
        await call.message.answer("Введіть свій новий бажаний нікнейм:")
        await state.set_state(WalletStates.change_nick)
    await call.answer()

@dp.message(WalletStates.change_nick)
async def change_nick_finish(message: types.Message, state: FSMContext):
    u = users_db[message.from_user.id]
    u["balance"] -= 20
    u["nickname"] = message.text
    await message.answer(f"🎉 Ваш нікнейм успішно змінено на: **{u['nickname']}** (Списано 20 грн).", parse_mode="Markdown")
    await state.clear()

# --- Магазин (10 товарів) ---
@dp.message(F.text == "🏪 Магазин")
async def shop_handler(message: types.Message):
    await message.answer(
        "🏪 **Магазин послуг та товарів**\n\nОберіть потрібну позицію для купівлі:",
        reply_markup=get_shop_keyboard(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("buy_"))
async def buy_item(call: types.CallbackQuery):
    prices = {
        "buy_nick": 20, "buy_prio_1d": 30, "buy_vip_badge": 40,
        "buy_blacklist": 50, "buy_gender_filter": 65, "buy_age_filter": 65,
        "buy_prem_1w": 80, "buy_prem_1m": 180, "buy_prem_3m": 350, "buy_prem_forever": 850
    }
    item_code = call.data
    price = prices.get(item_code, 0)
    u = users_db[call.from_user.id]

    if u["balance"] < price:
        await call.message.answer(f"❌ Недостатньо коштів. Ціна: {price} грн. Ваш баланс: {u['balance']} грн.")
    else:
        u["balance"] -= price
        if "prem" in item_code:
            u["is_premium"] = True
        await call.message.answer(f"🎉 Вітаємо з покупкою! Списано {price} грн. Дякуємо за підтримку!")
    await call.answer()

# --- Пошук та Чат ---
@dp.message(F.text == "🔍 Шукати співрозмовника")
async def search_partner(message: types.Message):
    user_id = message.from_user.id
    init_user(user_id)

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

        p_info = users_db[partner_id]
        u_info = users_db[user_id]

        await bot.send_message(user_id, f"Партнера знайдено! 🤫\nІнфо: {p_info['gender']}, {p_info['age']} років, {p_info['country']}", reply_markup=get_chat_keyboard())
        await bot.send_message(partner_id, f"Партнера знайдено! 🤫\nІнфо: {u_info['gender']}, {u_info['age']} років, {u_info['country']}", reply_markup=get_chat_keyboard())
    else:
        queue.append(user_id)
        await message.answer("Шукаємо співрозмовника... Зачекай ⏳")

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

# --- Пересилання повідомлень ---
@dp.message()
async def relay_messages(message: types.Message):
    user_id = message.from_user.id

    if user_id not in active_chats:
        await message.answer("Скористайтеся меню нижче:", reply_markup=get_main_keyboard())
        return

    partner_id = active_chats[user_id]
    try:
        await message.copy_to(chat_id=partner_id)
    except Exception:
        await message.answer("Не вдалося доставити повідомлення.")

async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
