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
import aiohttp
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
# Якщо задано DATABASE_URL (Postgres, напр. Neon) - база зберігається постійно і не зникає при перезапуску.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))

# --- Автодоставка через Fragment (вимкнена, поки FRAGMENT_AUTO != 1) ---
FRAGMENT_AUTO = os.getenv("FRAGMENT_AUTO", "0").strip() == "1"
TON_SEED = os.getenv("TON_SEED", "").strip()
TONAPI_KEY = os.getenv("TONAPI_KEY", "").strip()
FRAGMENT_COOKIES = os.getenv("FRAGMENT_COOKIES", "").strip()  # JSON з 4 cookie
AUTO_MAX_STARS = int(os.getenv("AUTO_MAX_STARS", "500"))  # більші замовлення - вручну

if not BOT_TOKEN or not ADMIN_ID:
    raise RuntimeError("Заповни BOT_TOKEN та ADMIN_ID у файлі .env")

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class _PgCursor:
    def __init__(self, cur, lastrowid=None):
        self._cur = cur
        self.lastrowid = lastrowid
        self.rowcount = cur.rowcount

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


class _PgConn:
    """Обгортка, яка робить Postgres схожим на sqlite3 (щоб решта коду не змінювалась)."""

    def __init__(self):
        import psycopg
        from psycopg.rows import dict_row
        self._conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)

    def execute(self, sql, params=()):
        sql = sql.replace("?", "%s")
        want_id = sql.lstrip().upper().startswith(("INSERT INTO REVIEWS", "INSERT INTO SUPPORT"))
        if want_id:
            sql = sql.rstrip().rstrip(";") + " RETURNING id"
        cur = self._conn.execute(sql, tuple(params))
        last = None
        if want_id:
            r = cur.fetchone()
            last = r["id"] if r else None
        return _PgCursor(cur, last)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()
        return False


def db():
    if USE_PG:
        return _PgConn()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ddl(sql):
    if USE_PG:
        return (sql.replace("__PK__", "BIGSERIAL PRIMARY KEY")
                   .replace("__BIGINT__", "BIGINT")
                   .replace("__REAL__", "DOUBLE PRECISION"))
    return (sql.replace("__PK__", "INTEGER PRIMARY KEY AUTOINCREMENT")
               .replace("__BIGINT__", "INTEGER")
               .replace("__REAL__", "REAL"))


def init_db():
    with db() as con:
        con.execute(_ddl("""
        CREATE TABLE IF NOT EXISTS users (
            user_id __BIGINT__ PRIMARY KEY,
            username TEXT,
            registered_at TEXT NOT NULL,
            stars_bought INTEGER DEFAULT 0,
            premium_months INTEGER DEFAULT 0
        )"""))
        con.execute(_ddl("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            user_id __BIGINT__ NOT NULL,
            username TEXT,
            stars INTEGER NOT NULL,
            amount_uah __REAL__ NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            telegram_charge_id TEXT,
            recipient_username TEXT,
            receipt_file_id TEXT
        )"""))
        # Міграція старої бази даних без втрати замовлень.
        if not USE_PG:
            cols = {r["name"] for r in con.execute("PRAGMA table_info(orders)").fetchall()}
            if "recipient_username" not in cols:
                con.execute("ALTER TABLE orders ADD COLUMN recipient_username TEXT")
            if "receipt_file_id" not in cols:
                con.execute("ALTER TABLE orders ADD COLUMN receipt_file_id TEXT")
        con.execute(_ddl("""
        CREATE TABLE IF NOT EXISTS reviews (
            id __PK__,
            order_id TEXT UNIQUE NOT NULL,
            user_id __BIGINT__ NOT NULL,
            username TEXT,
            comment TEXT NOT NULL,
            rating INTEGER NOT NULL,
            photo_file_id TEXT,
            created_at TEXT NOT NULL,
            stars INTEGER NOT NULL
        )"""))
        con.execute(_ddl("""
        CREATE TABLE IF NOT EXISTS support (
            id __PK__,
            user_id __BIGINT__ NOT NULL,
            username TEXT,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""))


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


_delivery_lock = asyncio.Lock()  # один платіж за раз, щоб не ламати seqno гаманця


async def notify_admin(text: str):
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
    except Exception:
        pass


async def complete_order(order_id: str, tx_id: str = "") -> bool:
    """Позначає замовлення виконаним і повідомляє покупця. Повертає False, якщо статус не підходить."""
    with db() as con:
        row = con.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if row is None or row["status"] not in ("approved_processing", "delivering", "delivery_check"):
            return False
        con.execute("UPDATE orders SET status='completed' WHERE order_id=?", (order_id,))
        con.execute("UPDATE users SET stars_bought=stars_bought+? WHERE user_id=?",
                    (row["stars"], row["user_id"]))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Залишити відгук", callback_data=f"revieworder:{order_id}")]
    ])
    await bot.send_message(
        row["user_id"],
        f"🎉 <b>Готово!</b> {row['stars']} зірок надіслано на {row['recipient_username']}\n\n"
        "✨ Дякуємо за користування FollyStars Bot!",
        parse_mode="HTML", reply_markup=kb
    )
    return True


def _set_status(order_id: str, new: str, only_from: tuple) -> bool:
    """Атомарна зміна статусу. Захищає від подвійної доставки."""
    marks = ",".join("?" * len(only_from))
    with db() as con:
        cur = con.execute(
            f"UPDATE orders SET status=? WHERE order_id=? AND status IN ({marks})",
            (new, order_id, *only_from)
        )
        return cur.rowcount == 1


async def auto_deliver(order_id: str):
    if not FRAGMENT_AUTO:
        return
    if not (TON_SEED and TONAPI_KEY and FRAGMENT_COOKIES):
        await notify_admin(f"⚠️ Автодоставка увімкнена, але не задані TON_SEED / TONAPI_KEY / FRAGMENT_COOKIES. "
                           f"Замовлення <code>{order_id}</code> - вручну, потім /done {order_id}")
        return
    async with _delivery_lock:
        # approved_processing -> delivering (тільки один раз)
        if not _set_status(order_id, "delivering", ("approved_processing",)):
            return
        with db() as con:
            row = con.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        try:
            import json as _json
            from pyfragment import (
                FragmentClient, FragmentError, UserNotFoundError, WalletError,
                CookieError, ConfigurationError,
            )
            from pyfragment.enums import PaymentMethod
            cookies = _json.loads(FRAGMENT_COOKIES)
        except Exception as e:
            _set_status(order_id, "approved_processing", ("delivering",))
            await notify_admin(f"⚠️ Автодоставка не запустилась (налаштування/бібліотека): <code>{e}</code>\n"
                               f"Замовлення <code>{order_id}</code> - вручну, потім /done {order_id}")
            return
        try:
            async with FragmentClient(seed=TON_SEED, api_key=TONAPI_KEY, cookies=cookies) as client:
                result = await client.purchase_stars(
                    row["recipient_username"], amount=row["stars"], payment_method=PaymentMethod.GRAM
                )
            tx = str(getattr(result, "transaction_id", ""))
        except (UserNotFoundError, WalletError, CookieError, ConfigurationError) as e:
            # нічого не відправлено - безпечно повернути в чергу
            _set_status(order_id, "approved_processing", ("delivering",))
            hint = {
                "UserNotFoundError": "Username не знайдено на Fragment. Уточніть у покупця.",
                "WalletError": "Недостатньо коштів на гаманці або проблема з гаманцем.",
                "CookieError": "Cookies Fragment застаріли - оновіть FRAGMENT_COOKIES.",
            }.get(type(e).__name__, "Невірні параметри.")
            await notify_admin(
                f"❌ Автодоставка <code>{order_id}</code> не виконана: {hint}\n<code>{e}</code>\n\n"
                f"Після виправлення: /retry {order_id}\nАбо вручну і /done {order_id}"
            )
            return
        except Exception as e:
            # невідомий стан: транзакція могла піти. Автоповтор заборонено.
            _set_status(order_id, "delivery_check", ("delivering",))
            await notify_admin(
                f"🚨 <b>Перевірте гаманець!</b> Замовлення <code>{order_id}</code>: помилка під час відправки, "
                f"невідомо, чи пішли зірки.\n<code>{type(e).__name__}: {e}</code>\n\n"
                f"Якщо зірки ДІЙШЛИ: /done {order_id}\nЯкщо НЕ дійшли: /reset {order_id}, потім /retry {order_id}"
            )
            return
        await complete_order(order_id, tx)
        await notify_admin(f"✅ Автодоставка <code>{order_id}</code>: {row['stars']} зірок -> "
                           f"{row['recipient_username']}\ntx: <code>{tx}</code>")


@dp.message(Command("retry"))
async def admin_retry(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Використання: /retry ORDER_ID")
        return
    order_id = parts[1].upper()
    with db() as con:
        row = con.execute("SELECT status FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if row is None or row["status"] != "approved_processing":
        await message.answer("Замовлення не знайдено або воно не в статусі очікування доставки.")
        return
    await message.answer(f"🔁 Повторюю автодоставку {order_id}...")
    asyncio.create_task(auto_deliver(order_id))


@dp.message(Command("reset"))
async def admin_reset(message: Message):
    """Повертає замовлення зі статусу delivery_check в чергу, коли ви ПЕРЕВІРИЛИ, що зірки не дійшли."""
    if message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Використання: /reset ORDER_ID")
        return
    order_id = parts[1].upper()
    if _set_status(order_id, "approved_processing", ("delivery_check",)):
        await message.answer(f"Готово. Тепер /retry {order_id}")
    else:
        await message.answer("Замовлення не в статусі перевірки.")


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
    if FRAGMENT_AUTO and row["stars"] <= AUTO_MAX_STARS:
        await call.answer("Схвалено. Запускаю автодоставку.")
        asyncio.create_task(auto_deliver(order_id))
    else:
        await call.answer("Замовлення схвалено. Виконайте доставку вручну.")
        reason = "" if not FRAGMENT_AUTO else f" (більше ліміту {AUTO_MAX_STARS} зірок)"
        await call.message.answer(
            f"⚠️ Замовлення {order_id} схвалено. Доставка вручну{reason}. "
            f"Після доставки виконайте /done {order_id}"
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
    if await complete_order(order_id):
        await message.answer(f"✅ Замовлення {order_id} позначено як виконане.")
    else:
        await message.answer("Замовлення не знайдено або воно не в обробці.")


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


async def keep_alive():
    """Раз на 10 хв стукає на власну публічну адресу, щоб Render не присипляв сервіс."""
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        return
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            await asyncio.sleep(600)
            try:
                async with session.get(url) as resp:
                    await resp.read()
            except Exception:
                pass


async def main():
    init_db()
    with db() as con:
        stuck = con.execute("SELECT order_id FROM orders WHERE status='delivering'").fetchall()
        con.execute("UPDATE orders SET status='delivery_check' WHERE status='delivering'")
    for r in stuck:
        await notify_admin(f"🚨 Бот перезапустився під час доставки <code>{r['order_id']}</code>. "
                           f"Перевірте гаманець: /done {r['order_id']} якщо зірки дійшли, "
                           f"інакше /reset {r['order_id']} і /retry {r['order_id']}")
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    keepalive_task = asyncio.create_task(keep_alive())
    try:
        await dp.start_polling(bot)
    finally:
        keepalive_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
