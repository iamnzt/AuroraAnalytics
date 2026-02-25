"""
🌸 Flower Dashboard — Telegram Bot v3.0
Раздельные таблицы для Астаны и Алматы.
Парсит РЕАЛЬНЫЙ формат отчётов из WhatsApp.
"""

import re
import logging
import asyncio
import os
import json
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

# ============================================================
# ⚙️ НАСТРОЙКИ
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_ТОКЕН_БОТА_СЮДА")
SPREADSHEET_ASTANA = os.getenv("SPREADSHEET_ASTANA", "ВСТАВЬТЕ_ID_ТАБЛИЦЫ_АСТАНА")
SPREADSHEET_ALMATY = os.getenv("SPREADSHEET_ALMATY", "ВСТАВЬТЕ_ID_ТАБЛИЦЫ_АЛМАТЫ")
CREDENTIALS_FILE = "credentials.json"
ALLOWED_USERS = []

# ============================================================
# Google Sheets
# ============================================================
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

_client = None
def get_client():
    global _client
    if _client is None:
        google_creds_json = os.getenv("GOOGLE_CREDENTIALS")
        if google_creds_json:
            creds_dict = json.loads(google_creds_json)
            creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
        _client = gspread.authorize(creds)
    return _client

def get_sheet(city: str):
    client = get_client()
    sheet_id = SPREADSHEET_ASTANA if city == "Астана" else SPREADSHEET_ALMATY
    return client.open_by_key(sheet_id)

# ============================================================
# ПАРСЕР ЧИСЕЛ
# ============================================================

def parse_number(text: str) -> int:
    """
    Парсит число. Поддерживает:
    792 300 → 792300, 144.890 → 144890, 1.110.798 → 1110798
    """
    text = text.strip().replace(' ', '')
    if '.' in text:
        text = text.replace('.', '')
    text = re.sub(r'[^\d-]', '', text)
    try:
        return int(text)
    except ValueError:
        return 0

# ============================================================
# МАППИНГ КЛЮЧЕВЫХ СЛОВ
# ============================================================

KEY_MAPPINGS = [
    ('приход', 'leads'), ('лиды', 'leads'),
    ('оформлены', 'orders'), ('оформленные', 'orders'), ('оформлено', 'orders'),
    ('халык терминал', 'halyk_terminal'), ('халык перевод', 'halyk_transfer'),
    ('дана пей', 'other'), ('kaspi pay', 'kaspi_pay'), ('каспи пей', 'kaspi_pay'),
    ('пей', 'kaspi_pay'), ('kaspi red', 'kaspi_red'), ('каспи ред', 'kaspi_red'),
    ('ред', 'kaspi_red'), ('наличные', 'cash'), ('наличка', 'cash'), ('нал', 'cash'),
    ('халык', 'halyk_terminal'), ('жусан', 'jusan'),
    ('каспи перевод', 'kaspi_transfer'), ('перевод/иин', 'kaspi_transfer'),
    ('перевод', 'kaspi_transfer'), ('бцк', 'bcc'), ('фридом', 'freedom'),
    ('форте', 'forte'), ('международный', 'international'), ('другое', 'other'),
    ('доплаты', 'surcharge'), ('доплата', 'surcharge'),
    ('возвраты', 'returns'), ('возврат', 'returns'),
    ('общая с доплатами', 'total_with_surcharge'),
    ('за смену', 'total_with_surcharge'),
    ('общий', 'total_basic'), ('общая', 'total_basic'), ('итого', 'total_basic'),
]

PAYMENT_COLS = ['kaspi_pay','kaspi_red','cash','halyk_terminal','halyk_transfer',
    'jusan','kaspi_transfer','bcc','freedom','forte','international','other','surcharge','returns']

# ============================================================
# ПАРСЕР ОТЧЁТА МЕНЕДЖЕРА
# ============================================================

def parse_manager_report(text: str) -> list[dict]:
    """Парсит отчёт менеджера — многострочный формат из WhatsApp"""
    lines = text.strip().split('\n')
    lines = [l.strip() for l in lines]

    entry = {k: 0 for k in PAYMENT_COLS}
    entry.update({'date': datetime.now().strftime('%d.%m.%Y'), 'name': '', 'shift': '',
                  'leads': 0, 'orders': 0, 'total_basic': 0, 'total_with_surcharge': 0})

    shift_pat = re.compile(r'^(\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2})$')

    for line in lines:
        if not line:
            continue

        # 1. Смена: 18:30-1:00 (проверяем ПЕРВЫМ — содержит двоеточие!)
        if shift_pat.match(line):
            entry['shift'] = line
            continue

        # 2. Имя: одно слово кириллицей
        if re.match(r'^[А-ЯЁа-яё]+$', line) and not entry['name']:
            entry['name'] = line
            continue

        # 3. "За смену 2.663.298" (без двоеточия)
        za = re.match(r'^за\s+смену\s+(.+)$', line, re.IGNORECASE)
        if za:
            entry['total_with_surcharge'] = parse_number(za.group(1))
            continue

        # 4. Строка с двоеточием: ключ : значение
        kv = re.match(r'^(.+?)\s*:\s*(.+)$', line)
        if kv:
            key = kv.group(1).strip().lower()
            val = parse_number(kv.group(2))
            matched = False
            for keyword, column in KEY_MAPPINGS:
                if keyword in key:
                    if column == 'leads': entry['leads'] = val
                    elif column == 'orders': entry['orders'] = val
                    elif column == 'total_basic': entry['total_basic'] = val
                    elif column == 'total_with_surcharge': entry['total_with_surcharge'] = val
                    elif column in PAYMENT_COLS: entry[column] += val
                    matched = True
                    break
            if not matched and val > 0:
                entry['other'] += val
            continue

    if not entry['name']:
        entry['name'] = 'Неизвестно'

    return [entry]


def parse_schedule(text: str, role: str) -> list[dict]:
    results = []
    current_date = None
    for line in text.strip().split('\n'):
        line = line.strip()
        if not line: continue
        if re.match(r'^\d{1,2}\.\d{1,2}(?:\.\d{2,4})?$', line) and len(line) <= 10:
            current_date = line
            if len(current_date.split('.')) == 2:
                current_date += f'.{datetime.now().year}'
            continue
        if current_date:
            shift_type = 'Полная'
            name = line
            if any(x in line.lower() for x in ['пол-смены','пол смены','половина']):
                shift_type = 'Пол-смены'
                name = re.sub(r'\s*(пол-смены|пол смены|половина)\s*', '', line, flags=re.IGNORECASE).strip()
            if name:
                e = {'date': current_date, 'name': name.strip(), 'role': role}
                if role == 'Логист': e['shift_type'] = shift_type
                results.append(e)
    return results

# ============================================================
# Запись в Google Sheets
# ============================================================

def write_manager_report(entries: list[dict], city: str) -> int:
    ws = get_sheet(city).worksheet('Продажи')
    rows = []
    for e in entries:
        payments_sum = sum(e.get(c, 0) for c in PAYMENT_COLS[:12])
        total = e.get('total_with_surcharge', 0) or e.get('total_basic', 0) or (payments_sum + e.get('surcharge',0) - e.get('returns',0))
        row = [e['date'], e['name'], e['shift'], e['leads'], e['orders']]
        row.extend([e.get(c, 0) for c in PAYMENT_COLS])
        row.append(total)
        row.append(round(e['orders']/e['leads'], 3) if e['leads'] > 0 else 0)
        rows.append(row)
    if rows:
        ws.append_rows(rows, value_input_option='USER_ENTERED')
    return len(rows)

def write_florist_schedule(entries, city):
    ws = get_sheet(city).worksheet('Смены флористов')
    rows = [[e['date'], e['name']] for e in entries]
    if rows: ws.append_rows(rows, value_input_option='USER_ENTERED')
    return len(rows)

def write_logist_schedule(entries, city):
    ws = get_sheet(city).worksheet('Смены логистов')
    rows = [[e['date'], e['name'], e.get('shift_type','Полная')] for e in entries]
    if rows: ws.append_rows(rows, value_input_option='USER_ENTERED')
    return len(rows)

def write_expense(date, name, amount, city):
    get_sheet(city).worksheet('Расходы').append_row([date, name, amount, ''], value_input_option='USER_ENTERED')

def write_marketing(date, lp, l, sp, sl, rt, city):
    get_sheet(city).worksheet('Маркетинг').append_row([date,lp,l,'',sp,'',sl,'','','',rt,'',''], value_input_option='USER_ENTERED')

# ============================================================
# Telegram Bot
# ============================================================
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

class States(StatesGroup):
    choosing_city = State()
    waiting_report = State()

main_kb = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="📊 Отчёт менеджера"), KeyboardButton(text="🌺 Смены флористов")],
    [KeyboardButton(text="🚗 Смены логистов"), KeyboardButton(text="💰 Расход")],
    [KeyboardButton(text="🎯 Маркетинг"), KeyboardButton(text="📋 Помощь")],
], resize_keyboard=True)
city_kb = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="🏙 Астана"), KeyboardButton(text="🏔 Алматы")],
    [KeyboardButton(text="❌ Отмена")],
], resize_keyboard=True)
cancel_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)

def check_access(uid):
    return not ALLOWED_USERS or uid in ALLOWED_USERS

@dp.message(Command("start"))
async def cmd_start(msg: types.Message, state: FSMContext):
    if not check_access(msg.from_user.id): return
    await state.clear()
    await msg.answer("🌸 *Flower Dashboard Bot v3*\n\n📊 Отчёт менеджера\n🌺 Флористы\n🚗 Логисты\n💰 Расход\n🎯 Маркетинг", parse_mode="Markdown", reply_markup=main_kb)

@dp.message(F.text == "❌ Отмена")
async def cancel(msg: types.Message, state: FSMContext):
    await state.clear()
    await msg.answer("Отменено.", reply_markup=main_kb)

REPORT_TYPES = {"📊 Отчёт менеджера":"manager", "🌺 Смены флористов":"florist", "🚗 Смены логистов":"logist", "💰 Расход":"expense", "🎯 Маркетинг":"marketing"}

@dp.message(F.text.in_(REPORT_TYPES.keys()))
async def choose_report(msg: types.Message, state: FSMContext):
    if not check_access(msg.from_user.id): return
    await state.set_state(States.choosing_city)
    await state.update_data(report_type=REPORT_TYPES[msg.text])
    await msg.answer("Выберите город:", reply_markup=city_kb)

@dp.message(F.text == "📋 Помощь")
async def help_cmd(msg: types.Message):
    await msg.answer("📊 Менеджер — скопируйте из WhatsApp как есть\n🌺 Флористы — даты+имена\n🚗 Логисты — даты+имена\n💰 Расход — `название сумма`\n🎯 Маркетинг — `дата план факт $ продаж курс`\n\n✅ Точки в числах (144.890) — понимает\n✅ Пробелы в числах (792 300) — понимает", parse_mode="Markdown", reply_markup=main_kb)

@dp.message(States.choosing_city, F.text.in_(["🏙 Астана", "🏔 Алматы"]))
async def select_city(msg: types.Message, state: FSMContext):
    city = "Астана" if "Астана" in msg.text else "Алматы"
    data = await state.get_data()
    await state.update_data(city=city)
    await state.set_state(States.waiting_report)
    hints = {
        'manager': f"📊 *{city}*\n\nСкопируйте отчёт из WhatsApp как есть.\nДата запишется автоматически (сегодня).\nТочки в числах — ок.",
        'florist': f"🌺 *{city}*\nОтправьте расписание (даты+имена)",
        'logist': f"🚗 *{city}*\nОтправьте расписание (даты+имена)",
        'expense': f"💰 *{city}*\nФормат: `название сумма`",
        'marketing': f"🎯 *{city}*\nФормат: `дата план факт $ продаж курс`",
    }
    await msg.answer(hints.get(data['report_type'], "Отправьте данные:"), parse_mode="Markdown", reply_markup=cancel_kb)

@dp.message(States.waiting_report)
async def process_report(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    rt, city = data['report_type'], data['city']
    text = msg.text
    if not text: await msg.answer("❌ Отправьте текст"); return

    try:
        if rt == 'manager':
            entries = parse_manager_report(text)
            if not entries: await msg.answer("❌ Не распознано."); return
            count = write_manager_report(entries, city)
            for e in entries:
                total = e.get('total_with_surcharge',0) or e.get('total_basic',0) or sum(e.get(k,0) for k in PAYMENT_COLS[:12])
                await msg.answer(
                    f"✅ Записано!\n📍 {city}\n\n"
                    f"👤 {e['name']} ({e['shift']})\n"
                    f"👥 Лиды: {e['leads']} → Продажи: {e['orders']}\n\n"
                    f"💳 Пей: {e.get('kaspi_pay',0):,}\n"
                    f"💳 Ред: {e.get('kaspi_red',0):,}\n"
                    f"💵 Нал: {e.get('cash',0):,}\n"
                    f"🏦 Халык: {e.get('halyk_terminal',0):,}\n"
                    f"📲 Перевод: {e.get('kaspi_transfer',0):,}\n"
                    f"📱 Другое: {e.get('other',0):,}\n\n"
                    f"💰 ИТОГО: {total:,}₸",
                    reply_markup=main_kb)

        elif rt == 'florist':
            entries = parse_schedule(text, 'Флорист')
            if not entries: await msg.answer("❌ Не распознано."); return
            count = write_florist_schedule(entries, city)
            dates = sorted(set(e['date'] for e in entries))
            names = sorted(set(e['name'] for e in entries))
            await msg.answer(f"✅ {count} смен!\n📍 {city}\n📅 {dates[0]}—{dates[-1]}\n👤 {', '.join(names)}", reply_markup=main_kb)

        elif rt == 'logist':
            entries = parse_schedule(text, 'Логист')
            if not entries: await msg.answer("❌ Не распознано."); return
            count = write_logist_schedule(entries, city)
            dates = sorted(set(e['date'] for e in entries))
            names = sorted(set(e['name'] for e in entries))
            half = sum(1 for e in entries if e.get('shift_type')=='Пол-смены')
            await msg.answer(f"✅ {count} смен!\n📍 {city}\n📅 {dates[0]}—{dates[-1]}\n👤 {', '.join(names)}" + (f"\n⚡ Пол-смен: {half}" if half else ""), reply_markup=main_kb)

        elif rt == 'expense':
            m = re.match(r'(.+?)\s+(\d[\d\s.]*)', text.strip())
            if not m: await msg.answer("❌ Формат: `название сумма`", parse_mode="Markdown"); return
            name, amount = m.group(1).strip(), parse_number(m.group(2))
            write_expense(datetime.now().strftime('%d.%m.%Y'), name, amount, city)
            await msg.answer(f"✅ Расход: {name} — {amount:,}₸\n📍 {city}", reply_markup=main_kb)

        elif rt == 'marketing':
            count = 0
            for line in text.strip().split('\n'):
                p = line.strip().split()
                if len(p) < 6: continue
                d = p[0]
                if len(d.split('.')) == 2: d += f'.{datetime.now().year}'
                try:
                    write_marketing(d, int(p[1]), int(p[2]), float(p[3]), int(p[4]), float(p[5].replace(',','.')), city)
                    count += 1
                except: continue
            if not count: await msg.answer("❌ Формат: `дата план факт $ продаж курс`", parse_mode="Markdown"); return
            await msg.answer(f"✅ {count} строк!\n📍 {city}", reply_markup=main_kb)

    except Exception as e:
        logging.error(f"Error: {e}", exc_info=True)
        await msg.answer(f"❌ Ошибка: {str(e)}", reply_markup=main_kb)
    await state.clear()

@dp.message(Command("myid"))
async def cmd_myid(msg: types.Message):
    await msg.answer(f"ID: `{msg.from_user.id}`", parse_mode="Markdown")

async def main():
    logging.info("🌸 Flower Dashboard Bot v3.0 starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
