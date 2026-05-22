"""
Telegram-бот для бронирования коворкинга в общежитии №15 РУДН
Библиотека: aiogram 3.x
Установка: pip install aiogram aiosqlite
"""

import asyncio
import logging
import aiosqlite
from datetime import datetime, date, timedelta
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ──────────────── КОНФИГУРАЦИЯ ────────────────
TOKEN = ""          # токен от @BotFather
ADMIN_IDS = []            # Telegram ID администраторов
DB_PATH = "coworking15.db"
COWORKING_NAME = "Коворкинг общежития №15 РУДН"
TOTAL_SEATS = 24
MAX_GROUP = 6

# Временные слоты (начало → конец), коворкинг работает круглосуточно
TIME_SLOTS = [
    ("00:00", "02:00"),
    ("02:00", "04:00"),
    ("04:00", "06:00"),
    ("06:00", "08:00"),
    ("08:00", "10:00"),
    ("10:00", "12:00"),
    ("12:00", "14:00"),
    ("14:00", "16:00"),
    ("16:00", "18:00"),
    ("18:00", "20:00"),
    ("20:00", "22:00"),
    ("22:00", "00:00"),
]

logging.basicConfig(level=logging.INFO)
router = Router()


# ──────────────── СОСТОЯНИЯ FSM ────────────────
class Booking(StatesGroup):
    waiting_date = State()
    waiting_slot = State()
    waiting_seats = State()
    confirm = State()

class Registration(StatesGroup):
    waiting_student_id = State()


# ──────────────── БАЗА ДАННЫХ ────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                tg_id     INTEGER PRIMARY KEY,
                full_name TEXT,
                username  TEXT,
                student_id TEXT,
                verified  INTEGER DEFAULT 0,
                reg_date  TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id      INTEGER,
                book_date  TEXT,
                slot_start TEXT,
                slot_end   TEXT,
                seats      INTEGER DEFAULT 1,
                status     TEXT DEFAULT 'active',
                created_at TEXT,
                FOREIGN KEY(tg_id) REFERENCES users(tg_id)
            )
        """)
        await db.commit()


async def get_user(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)) as cur:
            return await cur.fetchone()


async def count_booked(book_date: str, slot_start: str, slot_end: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(SUM(seats),0) FROM bookings "
            "WHERE book_date=? AND slot_start=? AND slot_end=? AND status='active'",
            (book_date, slot_start, slot_end)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# ──────────────── КЛАВИАТУРЫ ────────────────
def kb_main(is_verified: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if is_verified:
        builder.button(text="📅 Забронировать", callback_data="start_book")
        builder.button(text="📋 Мои брони",     callback_data="my_bookings")
        builder.button(text="🔍 Свободные места", callback_data="check_available")
    else:
        builder.button(text="🎓 Пройти верификацию", callback_data="register")
    builder.button(text="ℹ️ О боте", callback_data="show_info")
    builder.adjust(1)
    return builder.as_markup()


def kb_dates() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    today = date.today()
    for i in range(5):
        d = today + timedelta(days=i)
        label = "Сегодня" if i == 0 else ("Завтра" if i == 1 else d.strftime("%d.%m (%a)"))
        builder.button(text=label, callback_data=f"date_{d.isoformat()}")
    builder.button(text="❌ Отмена", callback_data="cancel_action")
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()


async def kb_slots(chosen_date: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for s, e in TIME_SLOTS:
        booked = await count_booked(chosen_date, s, e)
        free = TOTAL_SEATS - booked
        emoji = "🟢" if free >= 4 else ("🟡" if free > 0 else "🔴")
        label = f"{emoji} {s}–{e}  ({free} мест)"
        builder.button(text=label, callback_data=f"slot_{s}_{e}")
    builder.button(text="❌ Отмена", callback_data="cancel_action")
    builder.adjust(1)
    return builder.as_markup()


def kb_seats(free: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    max_seats = min(free, MAX_GROUP)
    for n in range(1, max_seats + 1):
        builder.button(text=str(n), callback_data=f"seats_{n}")
    builder.button(text="❌ Отмена", callback_data="cancel_action")
    builder.adjust(3)
    return builder.as_markup()


# ──────────────── ХЕНДЛЕРЫ ────────────────
INFO_TEXT = (
    "ℹ️ <b>О боте РУДН-КВ</b>\n\n"
    "Бот для бронирования рабочих мест в коворкинге общежития №15 РУДН.\n\n"
    "🏠 <b>Адрес:</b> Общежитие №15 РУДН, Москва\n"
    "💺 <b>Мест:</b> 24\n"
    "🕐 <b>Режим работы:</b> круглосуточно, 24/7\n"
    "📆 <b>Доступна запись</b> на 5 дней вперёд\n"
    "👥 <b>Групповое бронирование:</b> до 6 мест за раз\n\n"
    "📌 <b>Как пользоваться:</b>\n"
    "1. Пройдите верификацию — введите номер студенческого билета\n"
    "2. После подтверждения администратором (до 2 часов) вы сможете бронировать\n"
    "3. Выберите дату, временной слот и количество мест\n"
    "4. Покажите номер брони на входе в коворкинг\n\n"
    "🤖 <b>Команды:</b>\n"
    "/start — главное меню\n"
    "/info — эта справка\n\n"
    "❓ По вопросам обращайтесь к администратору коворкинга."
)


@router.message(CommandStart())
async def cmd_start(msg: Message):
    user = await get_user(msg.from_user.id)
    if not user:
        await msg.answer(
            f"👋 Добро пожаловать в бот бронирования!\n"
            f"<b>{COWORKING_NAME}</b>\n\n"
            f"Для бронирования необходима верификация студенческого билета РУДН.\n\n"
            f"💡 Нажмите <b>ℹ️ О боте</b>, чтобы узнать подробнее.",
            parse_mode="HTML",
            reply_markup=kb_main(False)
        )
    else:
        verified = bool(user["verified"])
        status = "✅ верифицирован" if verified else "⏳ ожидает верификации"
        await msg.answer(
            f"👋 С возвращением, <b>{user['full_name']}</b>!\n"
            f"Статус: {status}\n\n"
            f"<b>{COWORKING_NAME}</b>\n"
            f"💡 /info — справка о боте",
            parse_mode="HTML",
            reply_markup=kb_main(verified)
        )


@router.message(Command("info"))
async def cmd_info(msg: Message):
    await msg.answer(INFO_TEXT, parse_mode="HTML")


@router.callback_query(F.data == "show_info")
async def cb_show_info(call: CallbackQuery):
    await call.message.answer(INFO_TEXT, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "register")
async def cb_register(call: CallbackQuery, state: FSMContext):
    await call.message.answer(
        "🎓 Введите ваш <b>номер студенческого билета</b> РУДН:\n"
        "<i>Пример: 123456</i>",
        parse_mode="HTML"
    )
    await state.set_state(Registration.waiting_student_id)
    await call.answer()


@router.message(Registration.waiting_student_id)
async def reg_student_id(msg: Message, state: FSMContext):
    sid = msg.text.strip()
    if not sid.isdigit() or len(sid) < 4:
        await msg.answer("❌ Некорректный номер. Введите числовой номер студенческого билета.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO users (tg_id, full_name, username, student_id, verified, reg_date) "
            "VALUES (?,?,?,?,0,?)",
            (msg.from_user.id,
             msg.from_user.full_name,
             msg.from_user.username or "",
             sid,
             datetime.now().isoformat())
        )
        await db.commit()
    await state.clear()
    # Уведомить администраторов
    bot: Bot = msg.bot
    for admin_id in ADMIN_IDS:
        try:
            builder = InlineKeyboardBuilder()
            builder.button(text="✅ Подтвердить", callback_data=f"verify_{msg.from_user.id}")
            builder.button(text="❌ Отклонить",  callback_data=f"reject_{msg.from_user.id}")
            builder.adjust(2)
            await bot.send_message(
                admin_id,
                f"🔔 Новая заявка на верификацию:\n"
                f"👤 {msg.from_user.full_name} (@{msg.from_user.username})\n"
                f"🎓 Студенческий: <b>{sid}</b>\n"
                f"🆔 TG ID: {msg.from_user.id}",
                parse_mode="HTML",
                reply_markup=builder.as_markup()
            )
        except Exception:
            pass
    await msg.answer(
        "✅ Заявка отправлена администратору!\n"
        "Верификация обычно занимает до 2 часов.\n"
        "После подтверждения вы получите уведомление."
    )


@router.callback_query(F.data.startswith("verify_"))
async def cb_verify(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет доступа.", show_alert=True)
        return
    tg_id = int(call.data.split("_")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET verified=1 WHERE tg_id=?", (tg_id,))
        await db.commit()
    await call.bot.send_message(
        tg_id,
        "🎉 Ваш студенческий билет подтверждён!\n"
        "Теперь вы можете бронировать места в коворкинге.",
        reply_markup=kb_main(True)
    )
    await call.message.edit_text(call.message.text + "\n\n✅ Верифицирован")
    await call.answer("Пользователь верифицирован!")


@router.callback_query(F.data.startswith("reject_"))
async def cb_reject(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет доступа.", show_alert=True)
        return
    tg_id = int(call.data.split("_")[1])
    await call.bot.send_message(
        tg_id,
        "❌ Верификация не пройдена.\n"
        "Обратитесь к администратору коворкинга."
    )
    await call.message.edit_text(call.message.text + "\n\n❌ Отклонён")
    await call.answer("Отклонено.")


@router.callback_query(F.data == "check_available")
async def cb_available(call: CallbackQuery):
    today = date.today().isoformat()
    lines = [f"📊 <b>Доступность на сегодня</b> ({today})\n<b>{COWORKING_NAME}</b>\n"]
    for s, e in TIME_SLOTS:
        booked = await count_booked(today, s, e)
        free = TOTAL_SEATS - booked
        bar = "🟢" * (free // 4) + "⬜" * ((TOTAL_SEATS - free) // 4)
        lines.append(f"{s}–{e}: <b>{free}</b> из {TOTAL_SEATS} мест  {bar}")
    await call.message.answer("\n".join(lines), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "start_book")
async def cb_start_book(call: CallbackQuery, state: FSMContext):
    user = await get_user(call.from_user.id)
    if not user or not user["verified"]:
        await call.answer("Сначала пройдите верификацию!", show_alert=True)
        return
    await call.message.answer("📅 Выберите дату бронирования:", reply_markup=kb_dates())
    await state.set_state(Booking.waiting_date)
    await call.answer()


@router.callback_query(Booking.waiting_date, F.data.startswith("date_"))
async def cb_date(call: CallbackQuery, state: FSMContext):
    chosen_date = call.data.split("_")[1]
    await state.update_data(date=chosen_date)
    await call.message.answer(
        f"🕐 Выберите временной слот ({chosen_date}):",
        reply_markup=await kb_slots(chosen_date)
    )
    await state.set_state(Booking.waiting_slot)
    await call.answer()


@router.callback_query(Booking.waiting_slot, F.data.startswith("slot_"))
async def cb_slot(call: CallbackQuery, state: FSMContext):
    _, s, e = call.data.split("_")
    data = await state.get_data()
    booked = await count_booked(data["date"], s, e)
    free = TOTAL_SEATS - booked
    if free == 0:
        await call.answer("Нет свободных мест на этот слот!", show_alert=True)
        return
    await state.update_data(slot_start=s, slot_end=e, free=free)
    await call.message.answer(
        f"👥 Сколько мест забронировать?\n"
        f"Доступно: <b>{free}</b> (максимум {MAX_GROUP} в одной брони)",
        parse_mode="HTML",
        reply_markup=kb_seats(free)
    )
    await state.set_state(Booking.waiting_seats)
    await call.answer()


@router.callback_query(Booking.waiting_seats, F.data.startswith("seats_"))
async def cb_seats(call: CallbackQuery, state: FSMContext):
    seats = int(call.data.split("_")[1])
    data = await state.update_data(seats=seats)
    data = await state.get_data()
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data="confirm_book")
    builder.button(text="❌ Отмена",      callback_data="cancel_action")
    builder.adjust(2)
    await call.message.answer(
        f"📋 <b>Подтверждение бронирования</b>\n\n"
        f"📍 {COWORKING_NAME}\n"
        f"📅 Дата: {data['date']}\n"
        f"🕐 Время: {data['slot_start']} – {data['slot_end']}\n"
        f"👥 Количество мест: {seats}",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )
    await state.set_state(Booking.confirm)
    await call.answer()


@router.callback_query(Booking.confirm, F.data == "confirm_book")
async def cb_confirm(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    booked_now = await count_booked(data["date"], data["slot_start"], data["slot_end"])
    if TOTAL_SEATS - booked_now < data["seats"]:
        await call.message.answer("😔 Извините, места только что заняли. Попробуйте другой слот.")
        await state.clear()
        await call.answer()
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO bookings (tg_id, book_date, slot_start, slot_end, seats, status, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (call.from_user.id, data["date"], data["slot_start"], data["slot_end"],
             data["seats"], "active", datetime.now().isoformat())
        )
        await db.commit()
        async with db.execute(
            "SELECT last_insert_rowid()"
        ) as cur:
            row = await cur.fetchone()
            booking_id = row[0]
    await state.clear()
    await call.message.answer(
        f"🎉 <b>Бронирование подтверждено!</b>\n\n"
        f"🔑 Номер брони: #{booking_id}\n"
        f"📍 {COWORKING_NAME}\n"
        f"📅 {data['date']}   🕐 {data['slot_start']}–{data['slot_end']}\n"
        f"👥 Мест: {data['seats']}\n\n"
        f"⚠️ <b>При входе в коворкинг предъявите студенческий билет РУДН</b> "
        f"и назовите номер брони <b>#{booking_id}</b>.\n\n"
        f"Посмотреть и отменить брони: кнопка «📋 Мои брони».",
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data == "my_bookings")
async def cb_my_bookings(call: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM bookings WHERE tg_id=? AND status='active' AND book_date>=? ORDER BY book_date, slot_start",
            (call.from_user.id, date.today().isoformat())
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        await call.message.answer("У вас нет активных бронирований.")
        await call.answer()
        return
    builder = InlineKeyboardBuilder()
    text = "📋 <b>Ваши активные брони:</b>\n\n"
    for r in rows:
        text += f"🔑 #{r['id']}  📅 {r['book_date']}  🕐 {r['slot_start']}–{r['slot_end']}  👥 {r['seats']} мест\n"
        builder.button(text=f"❌ Отменить #{r['id']}", callback_data=f"cancel_book_{r['id']}")
    builder.adjust(1)
    await call.message.answer(text, parse_mode="HTML", reply_markup=builder.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("cancel_book_"))
async def cb_cancel_booking(call: CallbackQuery):
    bid = int(call.data.split("_")[2])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE bookings SET status='cancelled' WHERE id=? AND tg_id=?",
            (bid, call.from_user.id)
        )
        await db.commit()
    await call.message.answer(f"✅ Бронирование #{bid} отменено.")
    await call.answer()


@router.callback_query(F.data == "cancel_action")
async def cb_cancel_action(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Действие отменено.", reply_markup=kb_main(True))
    await call.answer()


# Команды администратора
@router.message(Command("stats"))
async def cmd_stats(msg: Message):
    if msg.from_user.id not in ADMIN_IDS:
        return
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Общая статистика
        async with db.execute("SELECT COUNT(*) FROM users WHERE verified=1") as cur:
            verified_users = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE verified=0") as cur:
            pending_users = (await cur.fetchone())[0]
        # Брони на ближайшие 7 дней по датам
        dates_ahead = [(date.today() + timedelta(days=i)).isoformat() for i in range(7)]
        # Все активные брони на ближайшие 7 дней с именами
        async with db.execute(
            """SELECT b.id, b.book_date, b.slot_start, b.slot_end, b.seats,
                      u.full_name, u.username, u.student_id
               FROM bookings b JOIN users u ON b.tg_id = u.tg_id
               WHERE b.status='active' AND b.book_date >= ?
               ORDER BY b.book_date, b.slot_start""",
            (today,)
        ) as cur:
            all_bookings = await cur.fetchall()

    # Формируем ответ
    lines = [f"📊 <b>Статистика коворкинга</b>\n",
             f"👤 Верифицированных студентов: <b>{verified_users}</b>",
             f"⏳ Ожидают верификации: <b>{pending_users}</b>\n"]

    if not all_bookings:
        lines.append("📭 Активных бронирований нет.")
    else:
        # Группируем по датам
        from collections import defaultdict
        by_date = defaultdict(list)
        for r in all_bookings:
            by_date[r['book_date']].append(r)

        total_seats_all = 0
        for d in sorted(by_date.keys()):
            day_bookings = by_date[d]
            day_seats = sum(r['seats'] for r in day_bookings)
            total_slots = TOTAL_SEATS * len(TIME_SLOTS)
            total_booked_slots = sum(r['seats'] for r in day_bookings)
            label = " (сегодня)" if d == today else (" (завтра)" if d == (date.today() + timedelta(days=1)).isoformat() else "")
            lines.append(f"\n📅 <b>{d}{label}</b> — забронировано мест: <b>{day_seats}</b>")

            # По слотам
            by_slot = defaultdict(list)
            for r in day_bookings:
                by_slot[(r['slot_start'], r['slot_end'])].append(r)
            for (s, e) in sorted(by_slot.keys()):
                slot_bookings = by_slot[(s, e)]
                slot_seats = sum(r['seats'] for r in slot_bookings)
                free = TOTAL_SEATS - slot_seats
                bar = "🟢" if free >= 8 else ("🟡" if free > 0 else "🔴")
                lines.append(f"  {bar} <b>{s}–{e}</b>: занято <b>{slot_seats}</b>, свободно <b>{free}</b>")
                for r in slot_bookings:
                    uname = f"@{r['username']}" if r['username'] else "—"
                    lines.append(f"      · #{r['id']} {r['full_name']} ({uname}) | билет: {r['student_id']} | {r['seats']} мест")
            total_seats_all += day_seats

        lines.append(f"\n💺 Всего активных бронирований: <b>{len(all_bookings)}</b> ({total_seats_all} мест)")

    await msg.answer("\n".join(lines), parse_mode="HTML")


# ──────────────── ЗАПУСК ────────────────
async def main():
    await init_db()
    bot = Bot(token=TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="info",  description="Информация о коворкинге и боте"),
    ])
    print(f"Бот запущен. Коворкинг: {COWORKING_NAME}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
