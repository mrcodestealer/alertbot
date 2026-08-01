#!/usr/bin/env python3
"""
DB Duty Schedule – Weekly Summary (3 weeks by default)

Usage:
    ./db_duty.py                # shows next 3 weeks of DB duty (current, next, next+1)
    ./db_duty.py DD/MM/YYYY     # shows duty for the specified date
    ./db_duty.py --week         # shows this week's duty summary (single week)
    ./db_duty.py --week-detail   # shows day-by-day duty for the current week
    ./db_duty.py --week-detail DD/MM/YYYY  # day-by-day for the week containing the given date
    ./db_duty.py --debug         # enables debug output
"""

import re
import sys
import csv
import os
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()
# ================= Configuration =================
APP_ID = os.getenv("APP_ID")
APP_SECRET = os.getenv("APP_SECRET")
SPREADSHEET_TOKEN = os.getenv("OSE_SPREADSHEET_TOKEN")
DUTY_LIST_PATH = "dutyList.csv"          # optional fallback

# 值班数据已迁移到合并后的 wiki 工作表 AS33r7（"FINAL OSE & QA MERGE"），
# 取代按年分表的 OSE{year}（旧 tab 3RIBRL）。可用 OSE_SHEET_ID 覆盖；旧 tab 自动映射到 AS33r7。
_DEFAULT_OSE_SHEET_ID = "AS33r7"
_LEGACY_OSE_SHEET_IDS = frozenset({"3RIBRL", "65p5cn"})


def _resolve_ose_sheet_id() -> str:
    sid = (os.getenv("OSE_SHEET_ID") or "").strip().replace(" ", "")
    if not sid or sid in _LEGACY_OSE_SHEET_IDS:
        return _DEFAULT_OSE_SHEET_ID
    return sid


OSE_SHEET_ID = _resolve_ose_sheet_id()

# DB team members and their CLEAN phone numbers (no extra text)
TARGET_DUTY = [
    {"name": "Kah Zheng", "phone": "+60169294328"},
    {"name": "Ken", "phone": "+60192336398"},
    {"name": "Ziyang", "phone": "+60102398909"},
    {"name": "Monlong", "phone": "+60104237748"},
]

# Month name to number mapping (full and abbreviated)
MONTH_MAP = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12
}

# ---------- Debug flag ----------
DEBUG = False

def debug_print(*args, **kwargs):
    if DEBUG:
        print("[DEBUG]", *args, file=sys.stderr, **kwargs)

# ---------- Helper functions ----------
def get_tenant_access_token():
    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    headers = {"Content-Type": "application/json"}
    data = {"app_id": APP_ID, "app_secret": APP_SECRET}
    resp = requests.post(url, headers=headers, json=data)
    result = resp.json()
    if result.get("code") != 0:
        raise Exception(f"Failed to get token: {result}")
    return result["tenant_access_token"]

def get_all_sheets_metadata(token):
    url = f"https://open.larksuite.com/open-apis/sheets/v2/spreadsheets/{SPREADSHEET_TOKEN}/metainfo"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers)
    result = resp.json()
    if result.get("code") != 0:
        return None
    return result.get("data", {}).get("sheets", [])

def get_sheet_id_by_year(year):
    """Return the OSE duty tab ID (merged 'FINAL OSE & QA MERGE' = ``AS33r7``).

    ``year`` is kept for signature/back-compat; duty now lives in a single merged
    tab (override via ``OSE_SHEET_ID``; legacy ``3RIBRL``/``65p5cn`` map to it).
    """
    debug_print(f"Using OSE duty sheet id: {OSE_SHEET_ID}")
    return OSE_SHEET_ID

def get_range_values_for_sheet(token, sheet_id, range_str):
    url = f"https://open.larksuite.com/open-apis/sheets/v2/spreadsheets/{SPREADSHEET_TOKEN}/values/{sheet_id}!{range_str}?valueRenderOption=FormattedValue"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers)
    result = resp.json()
    if result.get("code") != 0:
        return None
    return result.get("data", {}).get("valueRange", {}).get("values", [])

def get_sheet_metadata_by_id(token, sheet_id):
    sheets = get_all_sheets_metadata(token)
    if not sheets:
        return None
    for sheet in sheets:
        if sheet.get("sheetId") == sheet_id:
            return {"rowCount": sheet.get("rowCount"), "columnCount": sheet.get("columnCount")}
    return None

def is_checked(cell):
    if cell is None:
        return False
    if isinstance(cell, bool):
        return cell
    if isinstance(cell, (int, float)):
        return cell == 1
    if isinstance(cell, str):
        val = cell.strip().lower()
        return val in ('✓', '✔', '是', '1', 'true', 'yes', '勾')
    return False

def extract_text_from_cell(cell):
    if cell is None:
        return ""
    if isinstance(cell, str):
        return cell
    if isinstance(cell, list):
        parts = []
        for item in cell:
            if isinstance(item, dict) and 'text' in item:
                parts.append(item['text'])
            elif isinstance(item, str):
                parts.append(item)
        return ''.join(parts)
    return str(cell)

def parse_month_year(text):
    if not isinstance(text, str):
        return None, None
    for mon_name, mon_num in MONTH_MAP.items():
        pattern = rf'\b{re.escape(mon_name)}\b[^\d]*(\d{{4}})'
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return mon_num, int(m.group(1))
    return None, None

def get_month_for_column(col_idx, headers):
    for c in range(col_idx, -1, -1):
        if c < len(headers):
            header_text = extract_text_from_cell(headers[c])
            if header_text:
                mon_num, year = parse_month_year(header_text)
                if mon_num is not None:
                    debug_print(f"Column {c} header '{header_text}' -> month {mon_num}, year {year}")
                    return mon_num, year
                else:
                    debug_print(f"Column {c} header '{header_text}' – no month/year pattern found")
    return None, None

# DBA section header (matches "DB", "DBA", "DBA Team", optional trailing colon).
_DBA_HEADER_RE = re.compile(r'^\s*DBA?\s*(?:team)?\s*:?\s*$', re.IGNORECASE)
# A phone number inside a column-A cell, e.g. "+60169294328" or "+6011 16392152".
_PHONE_RE = re.compile(r'\+?\d[\d\-\s]{6,}\d')


def _parse_member_cell(cell_text):
    """Parse a DBA person cell like ``"Kah Zheng +60169294328 (note)"``.

    Returns ``(name, phone)`` or ``None`` when the cell has no phone number
    (i.e. it's a section header / note rather than a person entry).
    """
    text = (cell_text or "").strip()
    if not text:
        return None
    m = _PHONE_RE.search(text)
    if not m:
        return None
    # Name = everything before the phone; keep only the first line, trim punctuation.
    name = text[:m.start()].splitlines()[0].strip(" \t-:,") if text[:m.start()].strip() else ""
    if not name:
        return None
    raw = m.group(0).strip()
    digits = re.sub(r'\D', '', raw)
    if not digits:
        return None
    phone = ("+" + digits) if raw.startswith("+") else digits
    return name, phone


def find_dba_members(values):
    """Return DBA on-call members as ``[(row_idx, name, phone), ...]``.

    Reads the sheet's ``DBA`` section dynamically (name + phone from column A)
    so it stays correct as the roster changes. Falls back to the legacy
    ``TARGET_DUTY`` name scan if the ``DBA`` header can't be located.
    """
    header_idx = None
    for row_idx in range(len(values)):
        row = values[row_idx]
        if not row:
            continue
        if _DBA_HEADER_RE.match(extract_text_from_cell(row[0]).strip()):
            header_idx = row_idx
            debug_print(f"Found DBA section header at row {row_idx}")
            break

    members = []
    if header_idx is not None:
        for row_idx in range(header_idx + 1, len(values)):
            row = values[row_idx]
            cell_a = extract_text_from_cell(row[0]).strip() if row else ""
            if not cell_a:
                continue  # spacer row between members
            parsed = _parse_member_cell(cell_a)
            if parsed is None:
                # A labelled row with no phone marks the next section → stop.
                debug_print(f"DBA section ends at row {row_idx}: {cell_a!r}")
                break
            name, phone = parsed
            members.append((row_idx, name, phone))
            debug_print(f"DBA member '{name}' ({phone}) at row {row_idx}")

    if members:
        return members

    # Fallback: legacy hardcoded-name scan (keeps working if the header changes).
    debug_print("DBA section not found; falling back to TARGET_DUTY name scan")
    for row_idx in range(len(values)):
        row = values[row_idx]
        if not row:
            continue
        cell_a = extract_text_from_cell(row[0])
        if not cell_a:
            continue
        for target in TARGET_DUTY:
            if re.search(rf'\b{re.escape(target["name"])}\b', cell_a, re.IGNORECASE):
                members.append((row_idx, target["name"], target["phone"]))
                debug_print(f"Found target '{target['name']}' at row {row_idx}")
                break
    return members

def get_date_column(target_date, values):
    """Return the column index where the day number for target_date is located, or None."""
    current_year = target_date.year
    current_month = target_date.month
    current_day = target_date.day

    # Find the row that contains day numbers (search rows 0..10)
    day_row_idx = None
    day_row_candidates = []
    for row_idx in range(min(10, len(values))):
        row = values[row_idx]
        if not row:
            continue
        numeric_count = 0
        for cell in row:
            try:
                num = int(extract_text_from_cell(cell).strip())
                if 1 <= num <= 31:
                    numeric_count += 1
            except (ValueError, TypeError):
                continue
        if numeric_count > 2:
            day_row_candidates.append((row_idx, numeric_count))
    if not day_row_candidates:
        return None
    day_row_candidates.sort(key=lambda x: x[1], reverse=True)
    day_row_idx = day_row_candidates[0][0]

    day_row = values[day_row_idx]
    for col in range(len(day_row)):
        cell_text = extract_text_from_cell(day_row[col])
        try:
            day_num = int(cell_text.strip())
        except (ValueError, TypeError):
            continue
        if day_num == current_day:
            col_month, col_year = get_month_for_column(col, values[0] if len(values) > 0 else [])
            if col_month == current_month and col_year == current_year:
                return col
    return None

def get_duty_for_date(target_date, values, members):
    """Return list of ``(name, phone)`` for DBA members checked on target_date."""
    date_col = get_date_column(target_date, values)
    if date_col is None:
        return []
    checked = []
    for row_idx, name, phone in members:
        if row_idx >= len(values):
            continue
        row = values[row_idx]
        if date_col >= len(row):
            continue
        cell = extract_text_from_cell(row[date_col])
        if is_checked(cell):
            checked.append((name, phone))
    return checked

def get_values_and_targets_for_year(year):
    """Fetch sheet data and target rows for a given year."""
    sheet_id = get_sheet_id_by_year(year)
    if not sheet_id:
        return None, None, f"❌ Sheet OSE{year} not found."

    try:
        token = get_tenant_access_token()
    except Exception as e:
        return None, None, f"❌ Failed to get access token: {e}"

    props = get_sheet_metadata_by_id(token, sheet_id)
    if not props:
        return None, None, f"❌ Cannot retrieve sheet metadata for OSE{year}."

    max_row = props.get("rowCount", 200)
    scan_range = f"A1:ZZ{max_row}"
    values = get_range_values_for_sheet(token, sheet_id, scan_range)
    if values is None:
        return None, None, f"❌ Failed to read sheet data for OSE{year}."
    if len(values) < 2:
        return None, None, f"Sheet OSE{year} has fewer than 2 rows."

    members = find_dba_members(values)
    return values, members, None

def get_week_summary_from_data(week_start, values, members):
    """Return week summary using pre‑fetched data."""
    duty = set()  # set of (name, phone)
    for i in range(7):
        day = week_start + timedelta(days=i)
        duty.update(get_duty_for_date(day, values, members))

    week_end = week_start + timedelta(days=6)
    title = f"📅 DB Duty – {week_start.strftime('%B %d, %Y Monday')} – {week_end.strftime('%B %d, %Y Sunday')}"
    if not duty:
        return f"{title}\n• no duty"

    lines = [title]
    for name, phone in sorted(duty):
        lines.append(f"• {name} 📞 {phone or '未找到电话号码'}")
    return "\n".join(lines)

def get_three_weeks_summary():
    """Return summaries for the next three weeks (current, next, next+1)."""
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())   # Monday of current week

    # Determine the year of the Monday (assumes all three weeks are in same year)
    year = monday.year
    values, members, error = get_values_and_targets_for_year(year)
    if error:
        return error
    if not members:
        return "❌ No DB duty section found in sheet."

    results = []
    for i in range(3):
        week_start = monday + timedelta(days=7*i)
        results.append(get_week_summary_from_data(week_start, values, members))
    return "\n\n".join(results)

def get_db_day_duty(target_date):
    """Return duty for a single date."""
    values, members, error = get_values_and_targets_for_year(target_date.year)
    if error:
        return error
    if not members:
        return f"📅 {target_date.strftime('%d/%m/%Y')} – no DB duty section found."

    checked = get_duty_for_date(target_date, values, members)
    if not checked:
        return f"📅 {target_date.strftime('%d/%m/%Y')} – no db duty assigned."

    lines = [f"📅 DB Duty – {target_date.strftime('%d/%m/%Y')}"]
    for name, phone in sorted(set(checked)):
        lines.append(f"• {name} DB (Phone: {phone or '未找到电话号码'})")
    return "\n".join(lines)

def get_db_week_detail(week_start=None):
    """Return a day-by-day breakdown for the week."""
    if week_start is None:
        today = datetime.now().date()
        week_start = today - timedelta(days=today.weekday())
    else:
        week_start = week_start - timedelta(days=week_start.weekday())

    values, members, error = get_values_and_targets_for_year(week_start.year)
    if error:
        return error
    if not members:
        return "❌ No DB duty section found in sheet."

    lines = [f"📅 DB Duty details – week starting {week_start.strftime('%d/%m/%Y')}"]
    missing_days = []
    for i in range(7):
        day = week_start + timedelta(days=i)
        checked = get_duty_for_date(day, values, members)
        if not checked:
            missing_days.append(day.strftime('%A %d/%m/%Y'))
            lines.append(f"  {day.strftime('%A %d/%m/%Y')}: ❌ no duty")
        else:
            duty_list = [f"{name} DB (Phone: {phone or '未找到电话号码'})"
                         for name, phone in sorted(set(checked))]
            lines.append(f"  {day.strftime('%A %d/%m/%Y')}: ✅ " + ", ".join(duty_list))
    if missing_days:
        lines.append(f"\n⚠️ Missing duty on: {', '.join(missing_days)}")
    else:
        lines.append("\n✅ All days have duty assigned.")
    return "\n".join(lines)

def parse_date_arg(arg):
    try:
        return datetime.strptime(arg, "%d/%m/%Y").date()
    except ValueError:
        return None

def list_sheet_ids():
    try:
        token = get_tenant_access_token()
        sheets = get_all_sheets_metadata(token)
        if not sheets:
            print("No sheets found.")
            return
        print("Available sheets:")
        for sheet in sheets:
            print(f"  Title: {sheet.get('title')}, ID: {sheet.get('sheetId')}")
    except Exception as e:
        print("Error listing sheets:", e)
        
def db_check(month=None, year=None):
    """
    检查 DB 值班表中指定月份（默认为当前月份）是否有空缺。
    返回字符串，格式与其他 check 命令一致。
    """
    if year is None:
        year = datetime.now().year
    if month is None:
        month = datetime.now().month

    # 计算该月的总天数
    if month == 12:
        next_month_first = datetime(year + 1, 1, 1).date()
    else:
        next_month_first = datetime(year, month + 1, 1).date()
    days_in_month = (next_month_first - datetime(year, month, 1).date()).days

    # 获取该年份的表格数据
    values, members, error = get_values_and_targets_for_year(year)
    if error:
        return error
    if not members:
        return f"❌ 在 OSE{year} 工作表中未找到 DB 值班人员区域。"

    missing = []
    for day in range(1, days_in_month + 1):
        target_date = datetime(year, month, day).date()
        checked = get_duty_for_date(target_date, values, members)
        if not checked:
            missing.append(day)

    month_name = datetime(year, month, 1).strftime("%B %Y")
    if not missing:
        return f"✅ All days in {month_name} have duty assigned."
    else:
        missing_str = ", ".join(str(d) for d in missing)
        return f"⚠️ {month_name} 缺少值班的日期：{missing_str}"

if __name__ == "__main__":
    if "--debug" in sys.argv:
        DEBUG = True
        sys.argv.remove("--debug")
        list_sheet_ids()
        print("\n" + "="*50 + "\n")

    if len(sys.argv) > 1:
        if sys.argv[1] in ("--week", "-w"):
            # Show a single week summary
            if len(sys.argv) > 2:
                user_date = parse_date_arg(sys.argv[2])
                if user_date:
                    monday = user_date - timedelta(days=user_date.weekday())
                    values, members, error = get_values_and_targets_for_year(monday.year)
                    if error:
                        print(error)
                    else:
                        print(get_week_summary_from_data(monday, values, members))
                else:
                    print(get_three_weeks_summary())  # fallback
            else:
                # default: show current week
                today = datetime.now().date()
                monday = today - timedelta(days=today.weekday())
                values, members, error = get_values_and_targets_for_year(monday.year)
                if error:
                    print(error)
                else:
                    print(get_week_summary_from_data(monday, values, members))
        elif sys.argv[1] == "--week-detail":
            if len(sys.argv) > 2:
                user_date = parse_date_arg(sys.argv[2])
                if user_date:
                    print(get_db_week_detail(user_date))
                else:
                    print(get_db_week_detail())
            else:
                print(get_db_week_detail())
        else:
            user_date = parse_date_arg(sys.argv[1])
            if user_date is None:
                print("❌ Invalid date format. Please use DD/MM/YYYY (e.g., 03/02/2026)")
                sys.exit(1)
            print(get_db_day_duty(user_date))
    else:
        # Default: show three weeks
        print(get_three_weeks_summary())