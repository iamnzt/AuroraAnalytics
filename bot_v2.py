"""
🌸 Flower Dashboard — Telegram Bot v2.0
Раздельные таблицы для Астаны и Алматы.
Парсит отчёты менеджеров, флористов, логистов и расходы.

Установка:
    pip install aiogram gspread google-auth

Настройка:
    1. Создайте бота через @BotFather → получите TOKEN
    2. Создайте сервисный аккаунт Google (см. README)
    3. Загрузите ОБЕ таблицы в Google Sheets
    4. Дайте доступ сервисному аккаунту к обеим таблицам
    5. Заполните настройки ниже
"""

import re
import logging
import asyncio
import os
import json
from datetime import datetime
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

# ============================================================
# ⚙️ НАСТРОЙКИ
# Вариант 1: Впишите значения прямо сюда (для запуска на компьютере)
# Вариант 2: Задайте переменные окружения (для Railway/сервера)
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_ТОКЕН_БОТА_СЮДА")

# ID таблиц — из URL: https://docs.google.com/spreadsheets/d/ЭТОТ_ID/edit
SPREADSHEET_ASTANA = os.getenv("SPREADSHEET_ASTANA", "ВСТАВЬТЕ_ID_ТАБЛИЦЫ_АСТАНА")
SPREADSHEET_ALMATY = os.getenv("SPREADSHEET_ALMATY", "ВСТАВЬТЕ_ID_ТАБЛИЦЫ_АЛМАТЫ")

CREDENTIALS_FILE = "credentials.json"
ALLOWED_USERS = []  # Telegram ID (пустой = все)

# ============================================================
# Google Sheets
# ============================================================
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

_client = None
def get_client():
    global _client
    if _client is None:
        # Вариант 1: файл credentials.json (для компьютера)
        # Вариант 2: переменная окружения GOOGLE_CREDENTIALS (для Railway)
        google_creds_json = os.getenv("GOOGLE_CREDENTIALS")
        if google_creds_json:
            import io
            creds_dict = json.loads(google_creds_json)
            creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
        _client = gspread.authorize(creds)
    return _client

def get_sheet(city: str):
    """Возвращает нужную таблицу по городу"""
    client = get_client()
    sheet_id = SPREADSHEET_ASTANA if city == "Астана" else SPREADSHEET_ALMATY
    return client.open_by_key(sheet_id)

# ============================================================
# Маппинг ключевых слов → колонок
# ============================================================
KEY_MAP = {
    'приход': 'leads', 'лиды': 'leads', 'лидов': 'leads',
    'оформленные': 'orders', 'оформлены': 'orders', 'оформлено': 'orders',
    'пей': 'kaspi_pay', 'kaspi pay': 'kaspi_pay', 'каспи пей': 'kaspi_pay',
    'kaspi red': 'kaspi_red', 'каспи ред': 'kaspi_red', 'ред': 'kaspi_red',
    'наличные': 'cash', 'наличка': 'cash', 'нал': 'cash',
    'халык терминал': 'halyk_terminal', 'халык': 'halyk_terminal',
    'халык перевод': 'halyk_transfer',
    'жусан': 'jusan',
    'каспи перевод': 'kaspi_transfer', 'перевод/иин': 'kaspi_transfer',
    'перевод': 'kaspi_transfer',
    'бцк': 'bcc', 'фридом': 'freedom', 'форте': 'forte',
    'международный': 'international',
    'другое': 'other',
    'доплаты': 'surcharge', 'доплата': 'surcharge',
    'возвраты': 'returns', 'возврат': 'returns',
}

PAYMENT_COLS = ['kaspi_pay','kaspi_red','cash','halyk_terminal','halyk_transfer',
    'jusan','kaspi_transfer','bcc','freedom','forte','international','other','surcharge','returns']

# ============================================================
# Парсеры
# ============================================================

def parse_manager_report(text: str) -> list[dict]:
    """Парсит отчёт(ы) менеджера из WhatsApp"""
    results = []
    date_pat = re.compile(r'(\d{1,2}\.\d{1,2}(?:\.\d{2,4})?)')
    shift_pat = re.compile(r'(\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2})')
    kv_pat = re.compile(r'([а-яА-ЯёЁa-zA-Z\s/]+)\s*[:=]\s*([\d\s]+)', re.IGNORECASE)

    blocks = re.split(r'(?=\d{1,2}\.\d{1,2})', text.strip())

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        dm = date_pat.search(block)
        if not dm:
            continue

        date_str = dm.group(1)
        if len(date_str.split('.')) == 2:
            date_str += f'.{datetime.now().year}'

        nm = re.search(r'\d\s+([А-ЯЁа-яё]+)', block)
        name = nm.group(1) if nm else 'Неизвестно'

        sm = shift_pat.search(block)
        shift = sm.group(1) if sm else ''

        entry = {k: 0 for k in PAYMENT_COLS}
        entry.update({'date': date_str, 'name': name, 'shift': shift, 'leads': 0, 'orders': 0})

        for kv in kv_pat.finditer(block):
            key = kv.group(1).strip().lower()
            try:
                value = int(kv.group(2).replace(' ', '').strip())
            except ValueError:
                continue

            for mk, mv in KEY_MAP.items():
                if mk in key:
                    if mv == 'leads':
                        entry['leads'] = value
                    elif mv == 'orders':
                        entry['orders'] = value
                    elif mv in PAYMENT_COLS:
                        entry[mv] = value
                    break

        results.append(entry)
    return results


def parse_schedule(text: str, role: str) -> list[dict]:
    """Парсит расписание флористов/логистов"""
    results = []
    lines = text.strip().split('\n')
    current_date = None
    date_pat = re.compile(r'^(\d{1,2}\.\d{1,2}(?:\.\d{2,4})?)$')

    for line in lines:
        line = line.strip()
        if not line:
            continue

        dm = date_pat.match(line)
        if dm:
            current_date = dm.group(1)
            if len(current_date.split('.')) == 2:
                current_date += f'.{datetime.now().year}'
            continue

        if current_date:
            shift_type = 'Полная'
            name = line
            if any(x in line.lower() for x in ['пол-смены', 'пол смены', 'половина']):
                shift_type = 'Пол-смены'
                name = re.sub(r'\s*(пол-смены|пол смены|половина)\s*', '', line, flags=re.IGNORECASE).strip()

            if name:
                entry = {'date': current_date, 'name': name.strip(), 'role': role}
                if role == 'Логист':
                    entry['shift_type'] = shift_type
                results.append(entry)
    return results


# ============================================================
# Запись в Google Sheets
# ============================================================

def write_manager_report(entries: list[dict], city: str) -> int:
    ws = get_sheet(city).worksheet('Продажи')
    rows = []
    for e in entries:
        row = [e['date'], e['name'], e['shift'], e['leads'], e['orders']]
        row.extend([e.get(col, 0) for col in PAYMENT_COLS])
        # ИТОГО
        total = sum(e.get(col, 0) for col in PAYMENT_COLS[:12]) + e.get('surcharge', 0) - e.get('returns', 0)
        row.append(total)
        conv = round(e['orders'] / e['leads'], 3) if e['leads'] > 0 else 0
        row.append(conv)
        rows.append(row)
    if rows:
        ws.append_rows(rows, value_input_option='USER_ENTERED')
    return len(rows)


def write_florist_schedule(entries: list[dict], city: str) -> int:
    ws = get_sheet(city).worksheet('Смены флористов')
    rows = [[e['date'], e['name']] for e in entries]
    if rows:
        ws.append_rows(rows, value_input_option='USER_ENTERED')
    return len(rows)


def write_logist_schedule(entries: list[dict], city: str) -> int:
    ws = get_sheet(city).worksheet('Смены логистов')
    rows = [[e['date'], e['name'], e.get('shift_type', 'Полная')] for e in entries]
    if rows:
        ws.append_rows(rows, value_input_option='USER_ENTERED')
    return len(rows)


def write_expense(date: str, name: str, amount: int, city: str):
    ws = get_sheet(city).worksheet('Расходы')
    ws.append_row([date, name, amount, ''], value_input_option='USER_ENTERED')


def write_marketing(date: str, lead_plan: int, leads: int, spent_usd: float,
                    sales: int, exchange_rate: float, city: str):
    ws = get_sheet(city).worksheet('Маркетинг')
    ws.append_row([date, lead_plan, leads, '', spent_usd, '', sales, '', '', '', exchange_rate, '', ''],
                  value_input_option='USER_ENTERED')


# ============================================================
# Telegram Bot
# ============================================================
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

class States(StatesGroup):
    choosing_city = State()
    waiting_report = State()
    waiting_marketing = State()

# Клавиатуры
main_kb = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="📊 Отчёт менеджера"), KeyboardButton(text="🌺 Смены флористов")],
    [KeyboardButton(text="🚗 Смены логистов"), KeyboardButton(text="💰 Расход")],
    [KeyboardButton(text="🎯 Маркетинг"), KeyboardButton(text="📋 Помощь")],
], resize_keyboard=True)

city_kb = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="🏙 Астана"), KeyboardButton(text="🏔 Алматы")],
    [KeyboardButton(text="❌ Отмена")],
], resize_keyboard=True)

cancel_kb = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="❌ Отмена")],
], resize_keyboard=True)


def check_access(uid):
    return not ALLOWED_USERS or uid in ALLOWED_USERS


@dp.message(Command("start"))
async def cmd_start(msg: types.Message, state: FSMContext):
    if not check_access(msg.from_user.id): return
    await state.clear()
    await msg.answer(
        "🌸 *Flower Dashboard Bot v2.0*\n\n"
        "Каждый город записывается в отдельную таблицу!\n\n"
        "📊 Отчёт менеджера\n"
        "🌺 Смены флористов\n"
        "🚗 Смены логистов\n"
        "💰 Расход\n"
        "🎯 Маркетинг — данные по таргету",
        parse_mode="Markdown", reply_markup=main_kb
    )


@dp.message(F.text == "❌ Отмена")
async def cancel(msg: types.Message, state: FSMContext):
    await state.clear()
    await msg.answer("Отменено.", reply_markup=main_kb)


# --- Обработчики кнопок ---

REPORT_TYPES = {
    "📊 Отчёт менеджера": "manager",
    "🌺 Смены флористов": "florist",
    "🚗 Смены логистов": "logist",
    "💰 Расход": "expense",
    "🎯 Маркетинг": "marketing",
}

@dp.message(F.text.in_(REPORT_TYPES.keys()))
async def choose_report(msg: types.Message, state: FSMContext):
    if not check_access(msg.from_user.id): return
    rt = REPORT_TYPES[msg.text]
    await state.set_state(States.choosing_city)
    await state.update_data(report_type=rt)
    await msg.answer("Выберите город:", reply_markup=city_kb)


@dp.message(F.text == "📋 Помощь")
async def help_cmd(msg: types.Message):
    await msg.answer(
        "🌸 *Flower Dashboard Bot v2.0*\n\n"
        "📊 *Отчёт менеджера* — скопируйте из WhatsApp\n"
        "🌺 *Флористы* — расписание: даты и имена\n"
        "🚗 *Логисты* — расписание (+ пол-смены)\n"
        "💰 *Расход* — формат: `название сумма`\n"
        "🎯 *Маркетинг* — формат:\n"
        "`дата план факт потрачено$ продаж курс`\n\n"
        "Каждый город → отдельная таблица!\n"
        "/myid — узнать Telegram ID",
        parse_mode="Markdown", reply_markup=main_kb
    )


# --- Выбор города ---

@dp.message(States.choosing_city, F.text.in_(["🏙 Астана", "🏔 Алматы"]))
async def select_city(msg: types.Message, state: FSMContext):
    city = "Астана" if "Астана" in msg.text else "Алматы"
    data = await state.get_data()
    rt = data['report_type']
    await state.update_data(city=city)
    await state.set_state(States.waiting_report)

    hints = {
        'manager': f"📊 *{city}*\n\nСкопируйте отчёт менеджера из WhatsApp:\n`23.02 Камилла 16:30-01:00 Приход:74 Оформленные:52 Пей:959805...`",
        'florist': f"🌺 *{city}*\n\nРасписание флористов:\n`23.02\nЗангар\nНурай\n24.02\nЗангар\nНурай`",
        'logist': f"🚗 *{city}*\n\nРасписание логистов:\n`19.01\nБану\nЕркежан\n20.01\nАлема пол-смены`",
        'expense': f"💰 *{city}*\n\nФормат: `название сумма`\nПример: `цветы 200000`",
        'marketing': f"🎯 *{city}*\n\nФормат (каждая строка = один день):\n`дата план факт потрачено$ продаж курс`\n\nПример:\n`01.11 150 115 350 92 525`\n`02.11 150 122 350 89 528`",
    }
    await msg.answer(hints.get(rt, "Отправьте данные:"), parse_mode="Markdown", reply_markup=cancel_kb)


# --- Обработка данных ---

@dp.message(States.waiting_report)
async def process_report(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    rt, city = data['report_type'], data['city']
    text = msg.text

    if not text:
        await msg.answer("❌ Отправьте текстовое сообщение")
        return

    try:
        if rt == 'manager':
            entries = parse_manager_report(text)
            if not entries:
                await msg.answer("❌ Не удалось распознать. Проверьте формат."); return
            count = write_manager_report(entries, city)
            lines = "\n".join([f"  • {e['name']} ({e['shift']}) — {sum(e.get(k,0) for k in PAYMENT_COLS[:12])+e.get('surcharge',0)-e.get('returns',0):,}₸" for e in entries])
            await msg.answer(f"✅ Записано {count} отчёт(ов)!\n📍 {city}\n\n{lines}", reply_markup=main_kb)

        elif rt == 'florist':
            entries = parse_schedule(text, 'Флорист')
            if not entries:
                await msg.answer("❌ Не удалось распознать."); return
            count = write_florist_schedule(entries, city)
            dates = sorted(set(e['date'] for e in entries))
            names = sorted(set(e['name'] for e in entries))
            await msg.answer(f"✅ {count} смен флористов!\n📍 {city}\n📅 {dates[0]}—{dates[-1]}\n👤 {', '.join(names)}", reply_markup=main_kb)

        elif rt == 'logist':
            entries = parse_schedule(text, 'Логист')
            if not entries:
                await msg.answer("❌ Не удалось распознать."); return
            count = write_logist_schedule(entries, city)
            dates = sorted(set(e['date'] for e in entries))
            names = sorted(set(e['name'] for e in entries))
            half = sum(1 for e in entries if e.get('shift_type') == 'Пол-смены')
            extra = f"\n⚡ Пол-смен: {half}" if half else ""
            await msg.answer(f"✅ {count} смен логистов!\n📍 {city}\n📅 {dates[0]}—{dates[-1]}\n👤 {', '.join(names)}{extra}", reply_markup=main_kb)

        elif rt == 'expense':
            match = re.match(r'(.+?)\s+(\d[\d\s]*)', text.strip())
            if not match:
                await msg.answer("❌ Формат: `название сумма`", parse_mode="Markdown"); return
            name = match.group(1).strip()
            amount = int(match.group(2).replace(' ', ''))
            date_str = datetime.now().strftime('%d.%m.%Y')
            write_expense(date_str, name, amount, city)
            await msg.answer(f"✅ Расход записан!\n📍 {city} | {date_str}\n💰 {name}: {amount:,}₸\n⚠️ Поставьте категорию в таблице", reply_markup=main_kb)

        elif rt == 'marketing':
            # Парсим строки: дата план факт потрачено$ продаж курс
            lines = text.strip().split('\n')
            count = 0
            for line in lines:
                parts = line.strip().split()
                if len(parts) < 6:
                    continue
                date_str = parts[0]
                if len(date_str.split('.')) == 2:
                    date_str += f'.{datetime.now().year}'
                try:
                    lead_plan = int(parts[1])
                    leads = int(parts[2])
                    spent = float(parts[3])
                    sales = int(parts[4])
                    rate = float(parts[5].replace(',', '.'))
                except (ValueError, IndexError):
                    continue
                write_marketing(date_str, lead_plan, leads, spent, sales, rate, city)
                count += 1

            if count == 0:
                await msg.answer("❌ Не распознано. Формат строки:\n`дата план факт потрачено$ продаж курс`", parse_mode="Markdown"); return
            await msg.answer(f"✅ Записано {count} строк маркетинга!\n📍 {city}", reply_markup=main_kb)

    except Exception as e:
        logging.error(f"Error: {e}")
        await msg.answer(f"❌ Ошибка: {str(e)}", reply_markup=main_kb)

    await state.clear()


@dp.message(Command("myid"))
async def cmd_myid(msg: types.Message):
    await msg.answer(f"Ваш Telegram ID: `{msg.from_user.id}`", parse_mode="Markdown")


# ============================================================
# Запуск
# ============================================================
async def main():
    logging.info("🌸 Flower Dashboard Bot v2.0 starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
