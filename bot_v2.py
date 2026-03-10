"""
🌸 Flower Dashboard — Telegram Bot v4.0
Пошаговый ввод отчёта менеджера через кнопки.
Расходы убраны (есть отдельные боты «Чеки»).
"""

import re
import logging
import asyncio
import os
import json
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

# ============================================================
# ⚙️  НАСТРОЙКИ
# ============================================================
BOT_TOKEN           = os.getenv("BOT_TOKEN",           "ВСТАВЬТЕ_ТОКЕН_БОТА")
SPREADSHEET_ASTANA  = os.getenv("SPREADSHEET_ASTANA",  "ВСТАВЬТЕ_ID_ТАБЛИЦЫ_АСТАНА")
SPREADSHEET_ALMATY  = os.getenv("SPREADSHEET_ALMATY",  "ВСТАВЬТЕ_ID_ТАБЛИЦЫ_АЛМАТЫ")
CREDENTIALS_FILE    = "credentials.json"
ALLOWED_USERS: list[int] = []   # пусто = доступ для всех

# ============================================================
# 👤  МЕНЕДЖЕРЫ И СМЕНЫ
# ============================================================
MANAGERS = {
    "Астана": ["Камилла", "Еркежан", "Багнур", "Дилара", "Жансерик"],
    "Алматы": [],  # пустой список → ввод текстом
}
SHIFTS = ["8:00-16:30", "16:30-01:00"]

# ============================================================
# 💳  ШАГИ ОПЛАТЫ
# ============================================================
PAYMENT_STEPS = [
    ("kaspi_pay",      "💳 Каспи Пей"),
    ("kaspi_red",      "🔴 Каспи Ред"),
    ("cash",           "💵 Наличные"),
    ("halyk_terminal", "🏦 Халык терминал"),
    ("halyk_transfer", "📲 Халык перевод"),
    ("jusan",          "📱 Жусан"),
    ("kaspi_transfer", "🔄 Каспи перевод"),
    ("bcc",            "🏛 БЦК"),
    ("freedom",        "🆓 Фридом"),
    ("forte",          "💪 Форте"),
    ("international",  "🌍 Межд. карта"),
    ("other",          "❓ Другое"),
    ("surcharge",      "➕ Доплаты"),
    ("returns",        "↩️ Возвраты"),
]
PAYMENT_COLS = [p[0] for p in PAYMENT_STEPS]

# ============================================================
# 🔢  ПАРСЕР ЧИСЕЛ  (144.890 → 144890, 792 300 → 792300)
# ============================================================
def parse_number(text: str) -> int:
    text = text.strip().replace(" ", "")
    if "." in text:
        text = text.replace(".", "")
    text = re.sub(r"[^\d-]", "", text)
    try:
        return int(text)
    except ValueError:
        return 0

# ============================================================
# 🗂️  GOOGLE SHEETS
# ============================================================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
_client = None

def get_client():
    global _client
    if _client is None:
        raw = os.getenv("GOOGLE_CREDENTIALS")
        if raw:
            creds = Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
        _client = gspread.authorize(creds)
    return _client

def get_sheet(city: str):
    sid = SPREADSHEET_ASTANA if city == "Астана" else SPREADSHEET_ALMATY
    return get_client().open_by_key(sid)

# ---------- запись ----------

def write_manager_report(entry: dict, city: str) -> int:
    """Пишет строку в лист 'Продажи', возвращает итого."""
    ws = get_sheet(city).worksheet("Продажи")
    pay_sum  = sum(entry.get(c, 0) for c in PAYMENT_COLS[:12])
    surcharge = entry.get("surcharge", 0)
    returns   = entry.get("returns",   0)
    total = pay_sum + surcharge - returns
    conv  = round(entry["orders"] / entry["leads"], 3) if entry["leads"] > 0 else 0
    row = [entry["date"], entry["name"], entry["shift"],
           entry["leads"], entry["orders"]]
    row += [entry.get(c, 0) for c in PAYMENT_COLS]
    row += [total, conv]
    ws.append_row(row, value_input_option="USER_ENTERED")
    return total

def write_schedule(entries: list[dict], city: str, role: str) -> int:
    sheet_name = "Смены флористов" if role == "Флорист" else "Смены логистов"
    ws = get_sheet(city).worksheet(sheet_name)
    if role == "Логист":
        rows = [[e["date"], e["name"], e.get("shift_type", "Полная")] for e in entries]
    else:
        rows = [[e["date"], e["name"]] for e in entries]
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
    return len(rows)

def write_marketing(date, lp, l, sp, sl, rt, city):
    get_sheet(city).worksheet("Маркетинг").append_row(
        [date, lp, l, "", sp, "", sl, "", "", "", rt, "", ""],
        value_input_option="USER_ENTERED",
    )

# ============================================================
# ⌨️  КЛАВИАТУРЫ
# ============================================================
def kb(*rows):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
    )

main_kb   = kb(["📊 Отчёт менеджера", "🌺 Смены флористов"],
               ["🚗 Смены логистов",  "🎯 Маркетинг"],
               ["📋 Помощь"])
city_kb   = kb(["🏙 Астана", "🏔 Алматы"], ["❌ Отмена"])
cancel_kb = kb(["❌ Отмена"])
skip_kb   = kb(["0 — пропустить"], ["❌ Отмена"])

def managers_kb(city: str):
    names = MANAGERS.get(city, [])
    if not names:
        return cancel_kb
    rows = [names[i:i+3] for i in range(0, len(names), 3)]
    rows.append(["❌ Отмена"])
    return kb(*rows)

shifts_kb = kb(SHIFTS, ["❌ Отмена"])

def dates_kb():
    today = datetime.now().strftime("%d.%m.%Y")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%d.%m.%Y")
    return kb([f"📅 Сегодня ({today})", f"📅 Вчера ({yesterday})"],
              ["✏️ Другая дата"], ["❌ Отмена"])

confirm_kb = kb(["✅ Записать", "🔄 Начать заново"], ["❌ Отмена"])

# ============================================================
# 🤖  FSM СОСТОЯНИЯ
# ============================================================
class S(StatesGroup):
    # Общий шаг выбора города
    city = State()
    # Отчёт менеджера — пошагово
    m_name        = State()
    m_shift       = State()
    m_date        = State()
    m_date_custom = State()
    m_leads       = State()
    m_orders      = State()
    m_payment     = State()   # итерация по PAYMENT_STEPS через pay_index
    m_confirm     = State()
    # Текстовые сценарии (флористы, логисты, маркетинг)
    text_input    = State()

# ============================================================
# 🚀  БОТ
# ============================================================
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

def check_access(uid: int) -> bool:
    return not ALLOWED_USERS or uid in ALLOWED_USERS

# ---------- /start ----------
@dp.message(Command("start"))
async def cmd_start(msg: types.Message, state: FSMContext):
    if not check_access(msg.from_user.id):
        return
    await state.clear()
    await msg.answer(
        "🌸 *Flower Dashboard Bot v4*\n\nВыберите действие:",
        parse_mode="Markdown",
        reply_markup=main_kb,
    )

# ---------- Отмена ----------
@dp.message(F.text == "❌ Отмена")
async def cancel(msg: types.Message, state: FSMContext):
    await state.clear()
    await msg.answer("Отменено.", reply_markup=main_kb)

# ---------- Помощь ----------
@dp.message(F.text == "📋 Помощь")
async def help_cmd(msg: types.Message):
    await msg.answer(
        "📊 *Отчёт менеджера* — пошаговый ввод через кнопки\n"
        "🌺 *Флористы* — текстом: дата, затем имена\n"
        "🚗 *Логисты* — текстом: дата, затем имена\n"
        "🎯 *Маркетинг* — `дата план факт $ продаж курс`\n\n"
        "✅ Числа с точками (144.890 = 144 890) — понимает\n"
        "✅ Числа с пробелами (792 300) — понимает",
        parse_mode="Markdown",
        reply_markup=main_kb,
    )

# ---------- /myid ----------
@dp.message(Command("myid"))
async def cmd_myid(msg: types.Message):
    await msg.answer(f"ID: `{msg.from_user.id}`", parse_mode="Markdown")

# ============================================================
# ШАГИ ОТЧЁТА МЕНЕДЖЕРА
# ============================================================

ACTION_MAP = {
    "📊 Отчёт менеджера": "manager",
    "🌺 Смены флористов": "florist",
    "🚗 Смены логистов":  "logist",
    "🎯 Маркетинг":       "marketing",
}

@dp.message(F.text.in_(ACTION_MAP))
async def choose_action(msg: types.Message, state: FSMContext):
    if not check_access(msg.from_user.id):
        return
    await state.set_state(S.city)
    await state.update_data(action=ACTION_MAP[msg.text])
    await msg.answer("🏙 Выберите город:", reply_markup=city_kb)

# --- Шаг 1: Город ---
@dp.message(S.city, F.text.in_(["🏙 Астана", "🏔 Алматы"]))
async def select_city(msg: types.Message, state: FSMContext):
    city   = "Астана" if "Астана" in msg.text else "Алматы"
    data   = await state.get_data()
    action = data["action"]
    await state.update_data(city=city)

    if action == "manager":
        await state.set_state(S.m_name)
        managers = MANAGERS.get(city, [])
        if managers:
            await msg.answer(
                f"👤 *Шаг 1/7* — Менеджер ({city}):",
                parse_mode="Markdown",
                reply_markup=managers_kb(city),
            )
        else:
            await msg.answer(
                f"👤 *Шаг 1/7* — Введите имя менеджера ({city}):",
                parse_mode="Markdown",
                reply_markup=cancel_kb,
            )

    elif action in ("florist", "logist"):
        role = "Флорист" if action == "florist" else "Логист"
        await state.update_data(role=role)
        await state.set_state(S.text_input)
        icon = "🌺" if action == "florist" else "🚗"
        await msg.answer(
            f"{icon} *{city}* — Смены {role.lower()}ов\n\n"
            "Формат:\n`01.06\nИмя Фамилия\nДругое Имя`\n\n"
            "_(пол-смены → допишите «пол-смены» после имени)_",
            parse_mode="Markdown",
            reply_markup=cancel_kb,
        )

    elif action == "marketing":
        await state.set_state(S.text_input)
        await msg.answer(
            f"🎯 *{city}* — Маркетинг\n\n"
            "Формат: `дата план факт $ продаж курс`\n"
            "Пример: `01.06 100 85 0.9 50 450`",
            parse_mode="Markdown",
            reply_markup=cancel_kb,
        )

# --- Шаг 2: Имя менеджера ---
@dp.message(S.m_name)
async def step_name(msg: types.Message, state: FSMContext):
    name = msg.text.strip()
    if name == "❌ Отмена":
        await cancel(msg, state)
        return
    if not name:
        await msg.answer("❌ Введите имя:")
        return
    await state.update_data(name=name)
    await state.set_state(S.m_shift)
    await msg.answer(
        f"✅ Менеджер: *{name}*\n\n⏰ *Шаг 2/7* — Выберите смену:",
        parse_mode="Markdown",
        reply_markup=shifts_kb,
    )

# --- Шаг 3: Смена ---
@dp.message(S.m_shift, F.text.in_(SHIFTS))
async def step_shift(msg: types.Message, state: FSMContext):
    await state.update_data(shift=msg.text)
    await state.set_state(S.m_date)
    await msg.answer(
        f"✅ Смена: *{msg.text}*\n\n📅 *Шаг 3/7* — Выберите дату:",
        parse_mode="Markdown",
        reply_markup=dates_kb(),
    )

# --- Шаг 4: Дата ---
@dp.message(S.m_date)
async def step_date(msg: types.Message, state: FSMContext):
    text = msg.text.strip()
    if text == "❌ Отмена":
        await cancel(msg, state)
        return
    if text == "✏️ Другая дата":
        await state.set_state(S.m_date_custom)
        await msg.answer("📅 Введите дату в формате ДД.ММ.ГГГГ:", reply_markup=cancel_kb)
        return

    if text.startswith("📅 Сегодня"):
        date = datetime.now().strftime("%d.%m.%Y")
    elif text.startswith("📅 Вчера"):
        date = (datetime.now() - timedelta(days=1)).strftime("%d.%m.%Y")
    else:
        date = datetime.now().strftime("%d.%m.%Y")

    await _after_date(msg, state, date)

@dp.message(S.m_date_custom)
async def step_date_custom(msg: types.Message, state: FSMContext):
    text = msg.text.strip()
    if text == "❌ Отмена":
        await cancel(msg, state)
        return
    if not re.match(r"^\d{1,2}\.\d{1,2}(\.\d{2,4})?$", text):
        await msg.answer("❌ Формат: ДД.ММ.ГГГГ (например: 15.06.2025)")
        return
    if len(text.split(".")) == 2:
        text += f".{datetime.now().year}"
    await _after_date(msg, state, text)

async def _after_date(msg, state, date: str):
    await state.update_data(date=date)
    await state.set_state(S.m_leads)
    await msg.answer(
        f"✅ Дата: *{date}*\n\n👥 *Шаг 4/7* — Введите количество *лидов* (приход):",
        parse_mode="Markdown",
        reply_markup=cancel_kb,
    )

# --- Шаг 5: Лиды ---
@dp.message(S.m_leads)
async def step_leads(msg: types.Message, state: FSMContext):
    text = msg.text.strip()
    if text == "❌ Отмена":
        await cancel(msg, state)
        return
    n = parse_number(text)
    if n <= 0:
        await msg.answer("❌ Введите число больше 0:")
        return
    await state.update_data(leads=n)
    await state.set_state(S.m_orders)
    await msg.answer(
        f"✅ Лиды: *{n}*\n\n🛍 *Шаг 5/7* — Введите количество *оформленных заказов*:",
        parse_mode="Markdown",
        reply_markup=cancel_kb,
    )

# --- Шаг 6: Оформленные ---
@dp.message(S.m_orders)
async def step_orders(msg: types.Message, state: FSMContext):
    text = msg.text.strip()
    if text == "❌ Отмена":
        await cancel(msg, state)
        return
    n = parse_number(text)
    if n < 0:
        await msg.answer("❌ Введите 0 или больше:")
        return
    # Инициализируем оплаты нулями и начинаем итерацию
    await state.update_data(
        orders=n,
        pay_index=0,
        payments={col: 0 for col, _ in PAYMENT_STEPS},
    )
    await state.set_state(S.m_payment)
    await _ask_payment(msg, state)

# --- Шаг 7: Оплаты (итерация) ---
async def _ask_payment(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    idx  = data.get("pay_index", 0)

    if idx >= len(PAYMENT_STEPS):
        # Все оплаты введены → подтверждение
        await state.set_state(S.m_confirm)
        await _show_confirm(msg, state)
        return

    col, label = PAYMENT_STEPS[idx]
    progress   = f"{idx + 1}/{len(PAYMENT_STEPS)}"
    await msg.answer(
        f"💰 *Шаг 6/7* — Оплаты [{progress}]\n\n*{label}*\n_(введите сумму или нажмите «0 — пропустить»)_",
        parse_mode="Markdown",
        reply_markup=skip_kb,
    )

@dp.message(S.m_payment)
async def step_payment(msg: types.Message, state: FSMContext):
    text = msg.text.strip()
    if text == "❌ Отмена":
        await cancel(msg, state)
        return

    data = await state.get_data()
    idx  = data.get("pay_index", 0)
    col, label = PAYMENT_STEPS[idx]

    amount = 0 if text == "0 — пропустить" else parse_number(text)
    if amount < 0:
        await msg.answer("❌ Введите 0 или положительное число:")
        return

    payments = data.get("payments", {})
    payments[col] = amount
    await state.update_data(payments=payments, pay_index=idx + 1)
    await _ask_payment(msg, state)

# --- Шаг 8: Подтверждение ---
async def _show_confirm(msg: types.Message, state: FSMContext):
    data     = await state.get_data()
    payments = data.get("payments", {})

    pay_sum   = sum(payments.get(c, 0) for c in PAYMENT_COLS[:12])
    surcharge = payments.get("surcharge", 0)
    returns   = payments.get("returns",   0)
    total     = pay_sum + surcharge - returns

    lines = []
    for col, label in PAYMENT_STEPS:
        v = payments.get(col, 0)
        if v > 0:
            lines.append(f"  {label}: {v:,}₸")

    conv = round(data["orders"] / data["leads"], 1) if data["leads"] > 0 else 0

    text = (
        f"📋 *Шаг 7/7 — Проверьте данные:*\n\n"
        f"📍 {data['city']}\n"
        f"👤 {data['name']}\n"
        f"⏰ {data['shift']}\n"
        f"📅 {data['date']}\n\n"
        f"👥 Лиды: *{data['leads']}*   |   Продажи: *{data['orders']}*   |   Конверсия: *{conv}%*\n"
    )
    if lines:
        text += "\n💳 *Оплаты:*\n" + "\n".join(lines)
    text += f"\n\n💰 *ИТОГО: {total:,}₸*"

    await msg.answer(text, parse_mode="Markdown", reply_markup=confirm_kb)

@dp.message(S.m_confirm)
async def step_confirm(msg: types.Message, state: FSMContext):
    text = msg.text.strip()

    if text == "❌ Отмена":
        await cancel(msg, state)
        return

    if text == "🔄 Начать заново":
        await state.clear()
        await msg.answer("🔄 Начнём заново.", reply_markup=main_kb)
        return

    if text == "✅ Записать":
        data     = await state.get_data()
        payments = data.get("payments", {})
        entry    = {
            "date":   data["date"],
            "name":   data["name"],
            "shift":  data["shift"],
            "leads":  data["leads"],
            "orders": data["orders"],
        }
        entry.update(payments)

        try:
            total = write_manager_report(entry, data["city"])
            conv  = round(data["orders"] / data["leads"], 1) if data["leads"] > 0 else 0
            await msg.answer(
                f"✅ *Записано!*\n\n"
                f"📍 {data['city']}  |  👤 {data['name']}\n"
                f"⏰ {data['shift']}  |  📅 {data['date']}\n\n"
                f"👥 {data['leads']} лидов → {data['orders']} продаж ({conv}%)\n"
                f"💰 *{total:,}₸*",
                parse_mode="Markdown",
                reply_markup=main_kb,
            )
        except Exception as e:
            logging.error(f"Write error: {e}", exc_info=True)
            await msg.answer(f"❌ Ошибка записи: {e}", reply_markup=main_kb)

        await state.clear()
        return

    await msg.answer("Нажмите ✅ Записать, 🔄 Начать заново или ❌ Отмена")

# ============================================================
# ТЕКСТОВЫЕ СЦЕНАРИИ (флористы, логисты, маркетинг)
# ============================================================

def parse_schedule(text: str, role: str) -> list[dict]:
    results, current_date = [], None
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if re.match(r"^\d{1,2}\.\d{1,2}(?:\.\d{2,4})?$", line) and len(line) <= 10:
            current_date = line
            if len(current_date.split(".")) == 2:
                current_date += f".{datetime.now().year}"
            continue
        if current_date:
            shift_type = "Полная"
            name = line
            if any(x in line.lower() for x in ["пол-смены", "пол смены", "половина"]):
                shift_type = "Пол-смены"
                name = re.sub(r"\s*(пол-смены|пол смены|половина)\s*", "", line,
                              flags=re.IGNORECASE).strip()
            if name:
                e = {"date": current_date, "name": name, "role": role}
                if role == "Логист":
                    e["shift_type"] = shift_type
                results.append(e)
    return results

@dp.message(S.text_input)
async def process_text_input(msg: types.Message, state: FSMContext):
    text = msg.text.strip()
    if text == "❌ Отмена":
        await cancel(msg, state)
        return

    data   = await state.get_data()
    action = data["action"]
    city   = data["city"]

    try:
        if action == "florist":
            entries = parse_schedule(text, "Флорист")
            if not entries:
                await msg.answer("❌ Не распознано.")
                return
            count = write_schedule(entries, city, "Флорист")
            dates = sorted({e["date"] for e in entries})
            names = sorted({e["name"] for e in entries})
            await msg.answer(
                f"✅ {count} смен записано!\n📍 {city}\n"
                f"📅 {dates[0]}—{dates[-1]}\n👤 {', '.join(names)}",
                reply_markup=main_kb,
            )

        elif action == "logist":
            entries = parse_schedule(text, "Логист")
            if not entries:
                await msg.answer("❌ Не распознано.")
                return
            count = write_schedule(entries, city, "Логист")
            dates = sorted({e["date"] for e in entries})
            names = sorted({e["name"] for e in entries})
            half  = sum(1 for e in entries if e.get("shift_type") == "Пол-смены")
            extra = f"\n⚡ Пол-смен: {half}" if half else ""
            await msg.answer(
                f"✅ {count} смен записано!\n📍 {city}\n"
                f"📅 {dates[0]}—{dates[-1]}\n👤 {', '.join(names)}{extra}",
                reply_markup=main_kb,
            )

        elif action == "marketing":
            count = 0
            for line in text.strip().split("\n"):
                p = line.strip().split()
                if len(p) < 6:
                    continue
                d = p[0]
                if len(d.split(".")) == 2:
                    d += f".{datetime.now().year}"
                try:
                    write_marketing(d, int(p[1]), int(p[2]),
                                    float(p[3]), int(p[4]),
                                    float(p[5].replace(",", ".")), city)
                    count += 1
                except Exception:
                    continue
            if not count:
                await msg.answer(
                    "❌ Формат: `дата план факт $ продаж курс`",
                    parse_mode="Markdown",
                )
                return
            await msg.answer(f"✅ {count} строк!\n📍 {city}", reply_markup=main_kb)

    except Exception as e:
        logging.error(f"Error in text_input: {e}", exc_info=True)
        await msg.answer(f"❌ Ошибка: {e}", reply_markup=main_kb)

    await state.clear()

# ============================================================
# 🏁  ЗАПУСК
# ============================================================
async def main():
    logging.info("🌸 Flower Dashboard Bot v4.0 starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
