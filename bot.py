import asyncio
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, CopyTextButton,
    ReplyKeyboardMarkup, KeyboardButton, LabeledPrice, PreCheckoutQuery
)
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
REVIEWS_URL = os.getenv("REVIEWS_URL", "https://t.me/").strip()
REVIEW_CHANNEL_ID = os.getenv("REVIEW_CHANNEL_ID", "").strip()
CARD_NUMBER = os.getenv("CARD_NUMBER", "5168752160601400").strip()
CARD_OWNER = os.getenv("CARD_OWNER", "Денис В.").strip()
RATE_UAH = 0.78
DB_PATH = os.getenv("DB_PATH", "folly_stars.sqlite3")

if not BOT_TOKEN or not ADMIN_ID:
    raise RuntimeError("Заповни BOT_TOKEN та ADMIN_ID у файлі .env")

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            registered_at TEXT NOT NULL,
            stars_bought INTEGER DEFAULT 0,
            premium_months INTEGER DEFAULT 0
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            username TEXT,
            stars INTEGER NOT NULL,
            amount_uah REAL NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            telegram_charge_id TEXT,
            recipient_username TEXT,
            receipt_file_id TEXT
        )""")
        # Міграція старої бази даних без втрати замовлень.
        cols = {r["name"] for r in con.execute("PRAGMA table_info(orders)").fetchall()}
        if "recipient_username" not in cols:
            con.execute("ALTER TABLE orders ADD COLUMN recipient_username TEXT")
        if "receipt_file_id" not in cols:
            con.execute("ALTER TABLE orders ADD COLUMN receipt_file_id TEXT")
        con.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            comment TEXT NOT NULL,
            rating INTEGER NOT NULL,
            photo_file_id TEXT,
            created_at TEXT NOT NULL,
            stars INTEGER NOT NULL
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS support (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")


def now():
    return datetime.now(timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M")


def ensure_user(user):
    with db() as con:
        row = con.execute("SELECT user_id FROM users WHERE user_id=?", (user.id,)).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO users(user_id, username, registered_at) VALUES(?,?,?)",
                (user.id, user.username, now())
            )
        else:
            con.execute("UPDATE users SET username=? WHERE user_id=?", (user.username, user.id))


def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⭐ Купити Stars"), KeyboardButton(text="👤 Мій профіль")],
            [KeyboardButton(text="🎁 Подарунки"), KeyboardButton(text="🧮 Порахувати")],
            [KeyboardButton(text="🛡️ Відгуки"), KeyboardButton(text="💎 Telegram Premium")],
            [KeyboardButton(text="☎️ Служба підтримки")]
        ],
        resize_keyboard=True
    )


def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back")]
    ])


def admin_order_kb(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Схвалити", callback_data=f"approve:{order_id}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"reject:{order_id}")
        ]
    ])


class Flow(StatesGroup):
    calc_stars = State()
    calc_uah = State()
    recipient = State()
    custom_stars = State()
    receipt = State()
    review_photo = State()
    review_text = State()
    support_message = State()


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    ensure_user(message.from_user)
    await message.answer(
        "⭐ <b>Folly Stars</b>\n\nВітаємо у магазині Telegram Stars!\nОберіть потрібний розділ нижче.",
        reply_markup=main_menu(), parse_mode="HTML"
    )


@dp.message(F.text == "🧮 Порахувати")
async def calculator(message: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Зірки → Гривні", callback_data="calc:stars")],
        [InlineKeyboardButton(text="💵 Гривні → Зірки", callback_data="calc:uah")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back")]
    ])
    await message.answer("🧮 <b>Порахувати вартість</b>\n\n💰 Оберіть тип конвертації:", reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "calc:stars")
async def calc_stars_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.calc_stars)
    await call.message.edit_text("🧮 Введіть кількість зірок:", reply_markup=back_kb())
    await call.answer()


@dp.callback_query(F.data == "calc:uah")
async def calc_uah_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.calc_uah)
    await call.message.edit_text("🧮 Введіть суму в гривнях:", reply_markup=back_kb())
    await call.answer()


@dp.message(Flow.calc_stars)
async def calc_stars_result(message: Message, state: FSMContext):
    try:
        amount = int(message.text.strip())
        if amount <= 0 or amount > 10_000_000:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("Будь ласка, введіть ціле число більше нуля.")
        return
    total = amount * RATE_UAH
    await state.clear()
    await message.answer(
        f"🧮 <b>Результат конвертації:</b>\n\n⭐️ {amount:,} зірок = {total:,.2f} грн\n\n💎 Курс: 0.78 грн/⭐️".replace(",", " "),
        reply_markup=back_kb(), parse_mode="HTML"
    )


@dp.message(Flow.calc_uah)
async def calc_uah_result(message: Message, state: FSMContext):
    try:
        amount = float(message.text.strip().replace(",", "."))
        if amount <= 0 or amount > 100_000_000:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("Будь ласка, введіть коректну суму в гривнях.")
        return
    stars = int(amount / RATE_UAH)
    await state.clear()
    await message.answer(
        f"🧮 <b>Результат конвертації:</b>\n\n💵 {amount:.2f} грн = {stars:,} зірок ⭐️\n\n💎 Курс: 0.78 грн/⭐️".replace(",", " "),
        reply_markup=back_kb(), parse_mode="HTML"
    )


@dp.message(F.text == "👤 Мій профіль")
async def profile(message: Message):
    ensure_user(message.from_user)
    with db() as con:
        row = con.execute("SELECT * FROM users WHERE user_id=?", (message.from_user.id,)).fetchone()
    username = f"@{row['username']}" if row["username"] else "Не вказано"
    text = (
        "👤 <b>Ваш профіль 🌟</b>\n\n"
        f"🆔 ID: <code>{row['user_id']}</code> 🔑\n\n"
        f"👤 Username: {username} 📛\n\n"
        f"🗓 Дата реєстрації: {row['registered_at']} ⏰\n\n"
        "⭐️ Рейтинг: 0 ✨\n\n"
        "🏆 Статус: 🥈 Активний Клієнт\n\n"
        "📊 Статистика покупок: 📈\n\n"
        f"⭐️ Зірок куплено: {row['stars_bought']} ✨\n\n"
        f"💎 Місяців Premium: {row['premium_months']} 🚀"
    )
    await message.answer(text, reply_markup=back_kb(), parse_mode="HTML")


@dp.message(F.text == "🛡️ Відгуки")
async def reviews(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Переглянути відгуки", url=REVIEWS_URL)],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back")]
    ])
    await message.answer(
        "💬 <b>Відгуки наших клієнтів</b>\n\n"
        "📢 Всі відгуки публікуються в нашому каналі!\n\n"
        "🌟 Переглянь що кажуть інші про наш сервіс",
        reply_markup=kb, parse_mode="HTML"
    )


@dp.message(F.text == "🎁 Подарунки")
@dp.message(F.text == "💎 Telegram Premium")
async def unavailable(message: Message):
    await message.answer("⏳ Цей розділ тимчасово недоступний.", reply_markup=back_kb())


@dp.message(F.text == "☎️ Служба підтримки")
async def support_start(message: Message, state: FSMContext):
    await state.set_state(Flow.support_message)
    await message.answer("✍️ Опишіть вашу проблему або питання:", reply_markup=back_kb())


@dp.message(Flow.support_message)
async def support_receive(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Надішліть, будь ласка, текстове повідомлення.")
        return
    ensure_user(message.from_user)
    with db() as con:
        cur = con.execute(
            "INSERT INTO support(user_id, username, message, created_at) VALUES(?,?,?,?)",
            (message.from_user.id, message.from_user.username, message.text, now())
        )
        ticket = cur.lastrowid
    username = f"@{message.from_user.username}" if message.from_user.username else "без username"
    await bot.send_message(
        ADMIN_ID,
        f"📩 <b>Нове звернення #{ticket}</b>\nКористувач: {username}\nID: <code>{message.from_user.id}</code>\n\n{message.text}",
        parse_mode="HTML"
    )
    await state.clear()
    await message.answer(
        "✅ <b>Запит надіслано!</b>\n\n⏳ Очікуйте відповіді від нашої команди підтримки",
        reply_markup=main_menu(), parse_mode="HTML"
    )


def stars_amount_kb(recipient: str = ""):
    suffix = f":{recipient}" if recipient else ""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐️ 50 зірок - 39 грн", callback_data=f"stars:50{suffix}")],
        [InlineKeyboardButton(text="⭐️ 100 зірок - 78 грн", callback_data=f"stars:100{suffix}")],
        [InlineKeyboardButton(text="⭐️ 200 зірок - 156 грн", callback_data=f"stars:200{suffix}")],
        [InlineKeyboardButton(text="⭐️ 500 зірок - 390 грн", callback_data=f"stars:500{suffix}")],
        [InlineKeyboardButton(text="🎁 Своя кількість (50+)", callback_data=f"stars:custom{suffix}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back")]
    ])


async def show_star_options(message: Message, state: FSMContext):
    data = await state.get_data()
    recipient = data.get("recipient_username", "")
    await state.set_state(Flow.custom_stars)
    await message.answer(
        f"⭐️ Оберіть кількість зірок для {recipient or 'невідомого отримувача'}.",
        reply_markup=stars_amount_kb(recipient)
    )


@dp.message(F.text == "⭐ Купити Stars")
async def buy_start(message: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Другу", callback_data="recipient:friend")],
        [InlineKeyboardButton(text="👤 Собі", callback_data="recipient:self")]
    ])
    await message.answer("⭐️ Для кого купуємо Telegram Stars?", reply_markup=kb)


@dp.callback_query(F.data == "recipient:friend")
async def recipient_friend(call: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.recipient)
    await call.message.answer(
        "👥 Вкажіть @username друга\n\n"
        "🖊 Приклад: @follystarsbot\n"
        "⚠️ Обов'язково з символом @"
    )
    await call.answer()


@dp.callback_query(F.data == "recipient:self")
async def recipient_self(call: CallbackQuery, state: FSMContext):
    username = call.from_user.username
    if not username:
        await call.answer("Спочатку встановіть username у налаштуваннях Telegram.", show_alert=True)
        return
    await state.update_data(recipient_username=f"@{username}")
    await show_star_options(call.message, state)
    await call.answer()


@dp.message(Flow.recipient)
async def recipient_receive(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if not re.fullmatch(r"@[A-Za-z0-9_]{5,32}", value):
        await message.answer("❌ Введіть коректний @username. Він має починатися з @.")
        return
    await state.update_data(recipient_username=value)
    await show_star_options(message, state)


async def create_order(message: Message, state: FSMContext, stars: int, buyer=None):
    buyer = buyer or message.from_user
    data = await state.get_data()
    recipient = data.get("recipient_username")
    if not recipient:
        await state.clear()
        await message.answer("Не вдалося визначити отримувача. Почніть оформлення ще раз.")
        return
    amount_uah = round(stars * RATE_UAH, 2)
    order_id = uuid.uuid4().hex[:10].upper()
    with db() as con:
        con.execute(
            """INSERT INTO orders
            (order_id,user_id,username,stars,amount_uah,status,created_at,recipient_username)
            VALUES(?,?,?,?,?,?,?,?)""",
            (order_id, buyer.id, buyer.username, stars,
             amount_uah, "awaiting_receipt", now(), recipient)
        )
    await state.clear()
    await message.answer(
        "⭐️ <b>Замовлення створено!</b>\n\n"
        f"👤 Отримувач: {recipient}\n"
        f"⭐️ Кількість: {stars} зірок\n"
        f"💰 Вартість: {amount_uah:.2f} грн\n\n"
        "💳 <b>Оберіть спосіб оплати:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏦 Банк України", callback_data=f"paybank:{order_id}")]
        ])
    )


@dp.callback_query(F.data.startswith("stars:"))
async def choose_stars(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":", 2)
    value = parts[1]
    if len(parts) > 2 and parts[2]:
        # отримувач зашитий у кнопці, тому працює навіть після перезапуску бота
        await state.update_data(recipient_username=parts[2])
    if value == "custom":
        await state.set_state(Flow.custom_stars)
        await call.message.answer("✍️ Введіть кількість зірок (мінімум 50):")
        await call.answer()
        return
    await create_order(call.message, state, int(value), buyer=call.from_user)
    await call.answer()


@dp.message(Flow.custom_stars)
async def custom_stars_receive(message: Message, state: FSMContext):
    try:
        stars = int((message.text or "").strip())
        if stars < 50 or stars > 100_000:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введіть ціле число від 50 до 100000.")
        return
    await create_order(message, state, stars)


@dp.callback_query(F.data.startswith("paybank:"))
async def pay_bank(call: CallbackQuery, state: FSMContext):
    order_id = call.data.split(":", 1)[1]
    with db() as con:
        row = con.execute("SELECT * FROM orders WHERE order_id=? AND user_id=?",
                          (order_id, call.from_user.id)).fetchone()
    if row is None or row["status"] != "awaiting_receipt":
        await call.answer("Замовлення не знайдено або вже оплачено.", show_alert=True)
        return
    await state.set_state(Flow.receipt)
    await state.update_data(receipt_order_id=order_id)
    await call.message.answer(
        "🏦 <b>Банк України</b>\n\n"
        f"💳 Номер карти: <code>{CARD_NUMBER}</code>\n"
        f"👤 Отримувач: {CARD_OWNER}\n"
        f"💰 Сума: {row['amount_uah']:.2f} грн\n\n"
        f"🗃 Товар: ⭐️ {row['stars']} зірок\n"
        f"👤 Отримувач: {row['recipient_username']}\n\n"
        f"📋 ID замовлення: <code>{order_id}</code>\n\n"
        "📸 Після оплати надішліть скріншот чека в цей чат!\n\n"
        "⏰ <b>ВАЖЛИВО:</b> Після відправки чека очікуйте підтвердження! ✅",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="📋 Скопіювати номер картки",
                copy_text=CopyTextButton(text=CARD_NUMBER.replace(" ", ""))
            )]
        ])
    )
    await call.answer()


@dp.message(Flow.receipt, F.photo)
async def receive_receipt(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("receipt_order_id")
    photo_id = message.photo[-1].file_id
    with db() as con:
        row = con.execute("SELECT * FROM orders WHERE order_id=? AND user_id=?",
                          (order_id, message.from_user.id)).fetchone()
        if row is None or row["status"] != "awaiting_receipt":
            await message.answer("Замовлення не знайдено. Зверніться до підтримки.")
            await state.clear()
            return
        con.execute("UPDATE orders SET status='under_review', receipt_file_id=? WHERE order_id=?",
                    (photo_id, order_id))
    await state.clear()
    await message.answer(
        f"✅ <b>Чек прийнято!</b>\n\n✍️ Замовлення: <code>{order_id}</code>\n"
        "⏳ Статус: На розгляді\n\n⌛ Час обробки: 15-70 хвилин",
        parse_mode="HTML", reply_markup=main_menu()
    )
    buyer = f"@{row['username']}" if row["username"] else "без username"
    caption = (
        f"🆕 <b>Нове замовлення на перевірку</b>\n"
        f"📋 ID: <code>{order_id}</code>\n"
        f"🆔 Покупець: {buyer} (ID {row['user_id']})\n"
        f"👤 Отримувач: {row['recipient_username']}\n"
        f"⭐️ Кількість: {row['stars']} зірок\n"
        f"💰 Сума: {row['amount_uah']:.2f} грн"
    )
    await bot.send_photo(ADMIN_ID, photo_id, caption=caption,
                         parse_mode="HTML", reply_markup=admin_order_kb(order_id))


@dp.message(Flow.receipt)
async def receipt_not_photo(message: Message):
    await message.answer("📸 Будь ласка, надішліть скріншот чека саме фотографією.")


@dp.callback_query(F.data.startswith("approve:"))
async def approve_order(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Немає доступу.", show_alert=True)
        return
    order_id = call.data.split(":", 1)[1]
    with db() as con:
        row = con.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if row is None or row["status"] != "under_review":
            await call.answer("Замовлення не знайдено або вже оброблено.", show_alert=True)
            return
        con.execute("UPDATE orders SET status='approved_processing' WHERE order_id=?", (order_id,))
    await bot.send_message(
        row["user_id"],
        f"✅ <b>Замовлення схвалено!</b>\n\n✍️ ID: <code>{order_id}</code>\n\n"
        f"🗃 Товар: {row['stars']} зірок\n\n"
        "🚀 Ваше замовлення вже обробляється!",
        parse_mode="HTML"
    )
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Замовлення схвалено. Виконайте доставку через Fragment.")
    await call.message.answer(
        f"⚠️ Замовлення {order_id} схвалено. Доставка через Fragment поки виконується вручну. "
        "Після фактичної доставки позначте замовлення виконаним у базі."
    )


@dp.callback_query(F.data.startswith("reject:"))
async def reject_order(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Немає доступу.", show_alert=True)
        return
    order_id = call.data.split(":", 1)[1]
    with db() as con:
        row = con.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if row is None or row["status"] != "under_review":
            await call.answer("Замовлення не знайдено або вже оброблено.", show_alert=True)
            return
        con.execute("UPDATE orders SET status='rejected' WHERE order_id=?", (order_id,))
    await bot.send_message(row["user_id"], f"❌ <b>Замовлення {order_id} відхилено.</b>\nЗверніться до служби підтримки.", parse_mode="HTML")
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Замовлення відхилено.")


@dp.callback_query(F.data == "back")
async def back(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer("⭐ Folly Stars\nОберіть потрібний розділ:", reply_markup=main_menu())
    await call.answer()


@dp.message(Command("done"))
async def admin_done(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Використання: /done ORDER_ID")
        return
    order_id = parts[1].upper()
    with db() as con:
        row = con.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if row is None or row["status"] != "approved_processing":
            await message.answer("Замовлення не знайдено або воно не в обробці.")
            return
        con.execute("UPDATE orders SET status='completed' WHERE order_id=?", (order_id,))
        con.execute("UPDATE users SET stars_bought=stars_bought+? WHERE user_id=?", (row["stars"], row["user_id"]))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Залишити відгук", callback_data=f"revieworder:{order_id}")]
    ])
    await bot.send_message(
        row["user_id"],
        f"🎉 <b>Готово!</b> {row['stars']} зірок надіслано на {row['recipient_username']}\n\n"
        "✨ Дякуємо за користування FollyStars Bot!",
        parse_mode="HTML", reply_markup=kb
    )
    await message.answer(f"✅ Замовлення {order_id} позначено як виконане.")


@dp.callback_query(F.data.startswith("revieworder:"))
async def review_start(call: CallbackQuery, state: FSMContext):
    order_id = call.data.split(":", 1)[1]
    with db() as con:
        row = con.execute("SELECT * FROM orders WHERE order_id=? AND user_id=? AND status='completed'",
                          (order_id, call.from_user.id)).fetchone()
        exists = con.execute("SELECT id FROM reviews WHERE order_id=?", (order_id,)).fetchone()
    if row is None or exists:
        await call.answer("Відгук для цього замовлення вже залишено або замовлення не знайдено.", show_alert=True)
        return
    await state.set_state(Flow.review_photo)
    await state.update_data(review_order_id=order_id, review_photo_id=None)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пропустити", callback_data="review:skipphoto")]
    ])
    await call.message.answer("📷 Надішліть скріншот подарунка (або натисніть «Пропустити»)", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "review:skipphoto")
async def review_skip_photo(call: CallbackQuery, state: FSMContext):
    if await state.get_state() != Flow.review_photo:
        await call.answer()
        return
    await state.set_state(Flow.review_text)
    await call.message.answer("✏️ Тепер напишіть текст відгуку:")
    await call.answer()


@dp.message(Flow.review_photo, F.photo)
async def review_receive_photo(message: Message, state: FSMContext):
    await state.update_data(review_photo_id=message.photo[-1].file_id)
    await state.set_state(Flow.review_text)
    await message.answer("✏️ Тепер напишіть текст відгуку:")


@dp.message(Flow.review_photo)
async def review_photo_required(message: Message):
    await message.answer("Надішліть фото або натисніть кнопку «Пропустити».")


@dp.message(Flow.review_text)
async def review_receive_text(message: Message, state: FSMContext):
    comment = (message.text or "").strip()
    if not comment:
        await message.answer("Будь ласка, напишіть текстовий відгук.")
        return
    if len(comment) > 500:
        await message.answer("Відгук задовгий. Скоротіть його до 500 символів.")
        return
    await state.update_data(review_comment=comment)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"⭐️ {n}", callback_data=f"rating:{n}") for n in range(1, 6)
    ]])
    await state.set_state(Flow.review_text)
    await message.answer("⭐️ Оцініть наш сервіс від 1 до 5 зірок:", reply_markup=kb)


@dp.callback_query(F.data.startswith("rating:"))
async def review_publish(call: CallbackQuery, state: FSMContext):
    if await state.get_state() != Flow.review_text:
        await call.answer("Спочатку напишіть відгук.", show_alert=True)
        return
    rating = int(call.data.split(":", 1)[1])
    data = await state.get_data()
    order_id = data.get("review_order_id")
    comment = data.get("review_comment")
    photo_id = data.get("review_photo_id")
    with db() as con:
        row = con.execute("SELECT * FROM orders WHERE order_id=? AND user_id=? AND status='completed'",
                          (order_id, call.from_user.id)).fetchone()
        if row is None:
            await call.answer("Замовлення не знайдено.", show_alert=True)
            await state.clear()
            return
        cur = con.execute(
            """INSERT INTO reviews(order_id,user_id,username,comment,rating,photo_file_id,created_at,stars)
            VALUES(?,?,?,?,?,?,?,?)""",
            (order_id, call.from_user.id, call.from_user.username, comment, rating, photo_id, now(), row["stars"])
        )
        review_no = cur.lastrowid
        total_stars = con.execute("SELECT COALESCE(SUM(stars),0) AS total FROM orders WHERE status='completed'").fetchone()["total"]
    await state.clear()
    await call.message.answer(
        f"🎉 <b>Відгук успішно опубліковано!</b>\n\n"
        f"Ваша оцінка: {'⭐️' * rating}\n\nДякуємо за вашу думку!",
        parse_mode="HTML"
    )
    if REVIEW_CHANNEL_ID:
        uname = f"@{call.from_user.username}" if call.from_user.username else "без username"
        post = (
            f"📊 <b>Відгук №{review_no}</b>\n\n"
            f"🆔 Клієнт: {uname}\n"
            f"📝 Коментар: {comment}\n"
            f"📅 Дата: {datetime.now().strftime('%d.%m.%Y')}\n\n"
            f"🌟 Оцінка: {'⭐️' * rating}\n\n"
            f"💫 Виведено: {row['stars']} ⭐️\n"
            f"📊 Всього виведено: {total_stars} ⭐️\n\n"
            "🎯 FollyStars Bot - ваш надійний партнер!"
        )
        if photo_id:
            await bot.send_photo(REVIEW_CHANNEL_ID, photo_id, caption=post, parse_mode="HTML")
        else:
            await bot.send_message(REVIEW_CHANNEL_ID, post, parse_mode="HTML")
    await call.answer("Дякуємо за відгук!")


async def health(request):
    return web.Response(text="ok")


async def main():
    init_db()
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
