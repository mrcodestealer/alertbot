#!/usr/bin/env python3
"""
SRE Duty Schedule – Duty for a Given Date / Weekly Summary

Usage:
    ./sre_duty.py                # shows today's duty
    ./sre_duty.py DD/MM/YYYY     # shows duty for the specified date
    ./sre_duty.py --week         # shows this week and next week duty summary
    ./sre_duty.py -w              # same as --week
"""

import re
import sys
import csv
import os
import time
import requests
from datetime import datetime, timedelta
from calendar import monthrange
from dotenv import load_dotenv
load_dotenv()
# ================= Configuration =================
APP_ID = os.getenv("APP_ID")
APP_SECRET = os.getenv("APP_SECRET")
SPREADSHEET_TOKEN = os.getenv("OSE_SPREADSHEET_TOKEN")
# 值班数据已迁移到合并后的 wiki 工作表 AS33r7（"FINAL OSE & QA MERGE"），
# 取代旧 tab 3RIBRL。可用 OSE_SHEET_ID 覆盖；未设置或旧 tab 自动映射到 AS33r7。
_DEFAULT_OSE_SHEET_ID = "AS33r7"
_LEGACY_OSE_SHEET_IDS = frozenset({"3RIBRL", "65p5cn"})


def _resolve_ose_sheet_id() -> str:
    sid = (os.getenv("OSE_SHEET_ID") or "").strip().replace(" ", "")
    if not sid or sid in _LEGACY_OSE_SHEET_IDS:
        return _DEFAULT_OSE_SHEET_ID
    return sid


SHEET_ID = _resolve_ose_sheet_id()
DUTY_LIST_PATH = "dutyList.csv"

_SRE_TOKEN_CACHE: dict[str, object] = {"token": None, "expires_at": 0.0}
_SRE_SHEET_CACHE: dict[str, object] = {"mono": 0.0, "values": None}
_SRE_SHEET_CACHE_TTL_SEC = int(os.getenv("SRE_SHEET_CACHE_SEC", os.getenv("OSE_SHEET_CACHE_SEC", "120")))
_SRE_SHEET_SCAN_RANGE = (os.getenv("SRE_SHEET_SCAN_RANGE") or "A1:ZZ200").strip()

# Target names as they appear in column A (case‑insensitive start‑match)
TARGET_NAMES = [
    "Alex Tai", "Kelvin", "Wei Siong", "Bowei", "Jay",
    "Linus Lim", "Jeng Liang", "Misa", "Kai Xuan", "Yoon Hong",
    "Adrian","Clarence","Khai Xuan"
]

# 表格中出现的姓名形式 → CSV 中的标准姓名（用于电话查询）
TABLE_TO_CSV = {
    "WeiSiong": "Wei Siong",
    "Wei Siong": "Wei Siong",
    "Bowei": "Bo Wei",
    "BoWei": "Bo Wei",
    "Kai Xuan": "Kai Xuan",
    "KaiXuan": "Kai Xuan",
    "Yoon Hong": "Yoon Hong",
    "YoonHong": "Yoon Hong",
    "Alex Tai": "Alex Tai",
    "Kelvin": "Kelvin Er",
    "Jay": "Jay",
    "Linus Lim": "Linus Lim",
    "Jeng Liang": "Jeng Liang",
    "Misa": "Misa",
    "Adrian": "Adrian",
    "Clarence": "Clarence",
    "Khai Xuan": "Khai Xuan"
}

# SRE 现分为两个 Team（取代旧的 姓名 → 项目 映射）：值班显示按 Team 分组，
# 不再显示个人项目后缀（如 "Linus Lim FPMS"）。表格姓名拼写不一（BoWei/Bowei、
# KaiXuan/Kai Xuan、Linus/Linus Lim…），用去空格小写的 key 归一匹配。
SRE_TEAMS: list[tuple[str, list[str]]] = [
    (
        "BACKEND TEAM (FPMS, PMS, CPMS1.0, CPMS2.0/IGO, SMS, PULSAR, DOS/FLINK)",
        ["Wei Siong", "BoWei", "KaiXuan", "Linus Lim", "Jeng Liang", "Misa", "Clarence", "Khai Xuan"],
    ),
    (
        "FRONTEND TEAM (FRONTEND, POSTHOG, BI, AI/CHATBOT)",
        ["Kelvin", "Alex Tai", "YoonHong", "Jay"],
    ),
]


def _sre_norm_name(name: str) -> str:
    return re.sub(r"[^0-9a-z]", "", (name or "").lower())


# 归一 key → (team_index, member_index)；短名别名（Linus/Alex）一并注册。
_SRE_TEAM_INDEX: dict[str, tuple[int, int]] = {}
for _ti, (_title, _members) in enumerate(SRE_TEAMS):
    for _mi, _m in enumerate(_members):
        _SRE_TEAM_INDEX.setdefault(_sre_norm_name(_m), (_ti, _mi))
_SRE_TEAM_INDEX.setdefault("linus", _SRE_TEAM_INDEX.get("linuslim", (0, 3)))
_SRE_TEAM_INDEX.setdefault("alex", _SRE_TEAM_INDEX.get("alextai", (1, 1)))


def _format_sre_grouped_lines(names: list[str], *, html: bool = False) -> list[str]:
    """值班名单按 Backend / Frontend Team 分组输出（无个人项目后缀）。

    未归入任何 Team 的姓名（如 Adrian）列在末尾，不带 Team 标题。
    """
    grouped: dict[int, list[tuple[int, str]]] = {}
    unassigned: list[str] = []
    for raw in names:
        hit = _SRE_TEAM_INDEX.get(_sre_norm_name(raw))
        if hit is None:
            unassigned.append(raw)
        else:
            grouped.setdefault(hit[0], []).append((hit[1], raw))
    lines: list[str] = []
    for ti, (title, _members) in enumerate(SRE_TEAMS):
        got = grouped.get(ti)
        if not got:
            continue
        if lines:
            lines.append("")
        lines.append(f"<b>{title}</b>" if html else title)
        for _mi, raw in sorted(got):
            csv_name = TABLE_TO_CSV.get(raw, raw)
            lines.append(f"• {raw} 📞{get_phone_from_dutylist(csv_name)}")
    for raw in sorted(unassigned):
        csv_name = TABLE_TO_CSV.get(raw, raw)
        lines.append(f"• {raw} 📞{get_phone_from_dutylist(csv_name)}")
    return lines

# Month name to number mapping (full and abbreviated)
MONTH_MAP = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12
}

def col_index_to_letter(col_index):
    """Convert 1‑based column index to Excel column letters."""
    letters = ''
    while col_index > 0:
        col_index -= 1
        letters = chr(65 + (col_index % 26)) + letters
        col_index //= 26
    return letters

def get_tenant_access_token():
    now = time.time()
    tok = _SRE_TOKEN_CACHE.get("token")
    exp = float(_SRE_TOKEN_CACHE.get("expires_at") or 0.0)
    if tok and now < exp:
        return tok  # type: ignore[return-value]
    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    headers = {"Content-Type": "application/json"}
    data = {"app_id": APP_ID, "app_secret": APP_SECRET}
    resp = requests.post(url, headers=headers, json=data, timeout=15)
    result = resp.json()
    if result.get("code") != 0:
        raise Exception(f"Failed to get token: {result}")
    token = result["tenant_access_token"]
    try:
        expire_sec = int(result.get("expire") or 7200)
    except (TypeError, ValueError):
        expire_sec = 7200
    _SRE_TOKEN_CACHE["token"] = token
    _SRE_TOKEN_CACHE["expires_at"] = time.time() + max(60, expire_sec - 120)
    return token

def get_sheet_metadata(token, spreadsheet_token, sheet_id):
    url = f"https://open.larksuite.com/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/metainfo"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=20)
    result = resp.json()
    if result.get("code") != 0:
        return None
    sheets = result.get("data", {}).get("sheets", [])
    for sheet in sheets:
        if sheet.get("sheetId") == sheet_id:
            return {
                "rowCount": sheet.get("rowCount"),
                "columnCount": sheet.get("columnCount")
            }
    return None

def get_range_values(token, spreadsheet_token, sheet_id, range_str):
    # Use valueRenderOption=FormattedValue to get computed numbers, not formulas
    url = f"https://open.larksuite.com/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{sheet_id}!{range_str}?valueRenderOption=FormattedValue"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=30)
    result = resp.json()
    if result.get("code") != 0:
        return None
    return result.get("data", {}).get("valueRange", {}).get("values", [])

def get_phone_from_dutylist(name):
    """从 dutyList.csv 中查询电话号码（三列：姓名,团队,电话）"""
    if not os.path.exists(DUTY_LIST_PATH):
        return "未找到电话号码文件"
    try:
        with open(DUTY_LIST_PATH, newline='', encoding='utf-8') as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) >= 3 and row[0].strip() == name:
                    return row[2].strip()
    except Exception:
        pass
    return "未找到电话号码"

def is_checked(cell):
    """
    Return True if the cell represents a checked checkbox or contains a check mark.
    Handles various representations: boolean True, integer 1, strings like "✓", "true", "是", etc.
    """
    if cell is None:
        return False
    if isinstance(cell, bool):
        return cell
    if isinstance(cell, (int, float)):
        return cell == 1
    if isinstance(cell, str):
        val = cell.strip().lower()
        # Common check mark representations
        return val in ('✓', '✔', '是', '1', 'true', 'yes', '勾')
    return False

def parse_month_year(text):
    """Extract (month_num, year) from a string like 'March 2026'."""
    if not isinstance(text, str):
        return None, None
    for mon_name, mon_num in MONTH_MAP.items():
        pattern = rf"{re.escape(mon_name)}\s+(\d{{4}})"
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return mon_num, int(m.group(1))
    return None, None

def extract_text_from_cell(cell):
    """将单元格内容（可能为富文本列表）转换为纯文本字符串"""
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

def _get_duty_names_for_date(target_date, values):
    """
    内部函数：给定日期和已读取的表格数据 values，返回该日值班人员列表。
    每个元素为 raw_name（表格中的原始姓名，用于查找项目映射）。
    """
    current_year = target_date.year
    current_month = target_date.month
    current_day = target_date.day

    # 定位表头列（月份/年）
    target_header_col = None
    for col_idx, cell in enumerate(values[0]):
        mon_num, year = parse_month_year(extract_text_from_cell(cell))
        if mon_num == current_month and year == current_year:
            target_header_col = col_idx
            break
    if target_header_col is None:
        return []   # 未找到月份，无人值班

    # 定位日期列
    date_col = None
    for row_idx in range(1, min(5, len(values))):
        row = values[row_idx]
        for col in range(len(row)):
            cell = extract_text_from_cell(row[col])
            try:
                day_num = int(cell.strip())
            except (ValueError, TypeError):
                continue
            if day_num == current_day:
                # 验证该列是否属于正确月份
                header = ""
                for hcol in range(col, -1, -1):
                    if hcol < len(values[0]) and values[0][hcol]:
                        header = extract_text_from_cell(values[0][hcol])
                        break
                mon_num, year = parse_month_year(header)
                if mon_num == current_month and year == current_year:
                    date_col = col
                    break
        if date_col is not None:
            break
    if date_col is None:
        return []   # 未找到日期列

    # 收集所有目标人员行号
    name_rows = {}      # target_name -> row_index
    for row_idx in range(2, len(values)):
        row = values[row_idx]
        if not row or len(row) == 0:
            continue
        cell_a = extract_text_from_cell(row[0])
        if not cell_a:
            continue
        # 锚定开头匹配（见 TARGET_NAMES 注释 “start-match”）。原来的子串匹配会让
        # "Jay" 先命中更靠上的 "Chris Jay Montecalvo [QA]" 行，导致真正的
        # "Jay (+6011...)" 行（row 90）永远不被读取 → Jay 值班天数全部漏报。
        cell_upper = cell_a.strip().upper()
        for target in TARGET_NAMES:
            if cell_upper.startswith(target.upper()):
                if target not in name_rows:
                    name_rows[target] = row_idx
                break

    # 检查哪些人当天打勾，并返回原始姓名
    checked = []  # 列表 of raw_name
    for name, row_idx in name_rows.items():
        if row_idx >= len(values):
            continue
        row = values[row_idx]
        if date_col >= len(row):
            continue
        cell = extract_text_from_cell(row[date_col])
        if is_checked(cell):
            checked.append(name)

    return checked

def _load_sre_sheet_values() -> tuple[list | None, str | None, bool]:
    """Fetch SRE sheet; returns ``(values, error, cache_hit)``."""
    now = time.monotonic()
    cached = _SRE_SHEET_CACHE.get("values")
    if (
        _SRE_SHEET_CACHE_TTL_SEC > 0
        and isinstance(cached, list)
        and now - float(_SRE_SHEET_CACHE.get("mono") or 0.0) < _SRE_SHEET_CACHE_TTL_SEC
    ):
        return cached, None, True

    try:
        token = get_tenant_access_token()
    except Exception as e:
        return None, f"Failed to get access token: {e}", False

    scan_range = _SRE_SHEET_SCAN_RANGE
    if not scan_range.upper().startswith("A1:"):
        props = get_sheet_metadata(token, SPREADSHEET_TOKEN, SHEET_ID)
        if not props:
            return None, "Cannot retrieve sheet metadata", False
        max_row = props.get("rowCount", 200)
        scan_range = f"A1:ZZ{max_row}"

    values = get_range_values(token, SPREADSHEET_TOKEN, SHEET_ID, scan_range)
    if values is None:
        return None, "Failed to read sheet data", False
    if len(values) < 2:
        return None, "Sheet has fewer than 2 rows.", False
    _SRE_SHEET_CACHE["values"] = values
    _SRE_SHEET_CACHE["mono"] = now
    return values, None, False


def _format_sre_duty_body(target_date, checked: list[str]) -> str:
    if not checked:
        return f"📅 {target_date.strftime('%d/%m/%Y')} – no SRE duty assigned."
    lines = [f"📅 SRE Duty – {target_date.strftime('%d/%m/%Y')}"]
    lines.extend(_format_sre_grouped_lines(checked))
    return "\n".join(lines)


def get_sre_duty_bulk(dates: list) -> dict:
    """One sheet read for many dates (dutyai week cards — avoids N× Lark API)."""
    from datetime import date as date_cls

    t0 = time.perf_counter()
    unique = sorted({d for d in dates if isinstance(d, date_cls)})
    if not unique:
        return {}
    values, err, cache_hit = _load_sre_sheet_values()
    if values is None:
        msg = f"❌ {err or 'Failed to read sheet data'}"
        return {d: msg for d in unique}
    out: dict = {}
    for d in unique:
        checked = _get_duty_names_for_date(d, values)
        out[d] = _format_sre_duty_body(d, checked)
    ms = (time.perf_counter() - t0) * 1000
    print(
        f"[sre_Duty] bulk lookup {len(unique)} day(s) in {ms:.0f}ms "
        f"(cache={'hit' if cache_hit else 'miss'})",
        flush=True,
    )
    return out


# ---------- 对外接口 ----------
def get_sre_duty(target_date):
    """返回指定日期的值班信息（格式化字符串）"""
    values, err, _cache_hit = _load_sre_sheet_values()
    if values is None:
        return f"❌ {err or 'Failed to read sheet data'}"
    checked = _get_duty_names_for_date(target_date, values)
    return _format_sre_duty_body(target_date, checked)

# （旧的 get_sre_week_duty 首个实现已删除：它构建了字符串却从未 return，
#   一直被文件末尾的同名函数遮蔽。现只保留 sretwoweek 版本。）

def parse_date_arg(arg):
    """Parse a string in DD/MM/YYYY format and return a date object."""
    try:
        return datetime.strptime(arg, "%d/%m/%Y").date()
    except ValueError:
        return None

def get_sre_today_duty():
    """Return today's SRE duty (no arguments)."""
    return get_sre_duty(datetime.now().date())

def sre_check(month=None, year=None):
    """
    检查 SRE 值班表中指定月份（默认为当前月份）是否有空缺。
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

    try:
        token = get_tenant_access_token()
    except Exception as e:
        return f"❌ Failed to get access token: {e}"

    props = get_sheet_metadata(token, SPREADSHEET_TOKEN, SHEET_ID)
    if not props:
        return "❌ Cannot retrieve sheet metadata"
    max_row = props.get("rowCount", 200)
    scan_range = f"A1:ZZ{max_row}"
    values = get_range_values(token, SPREADSHEET_TOKEN, SHEET_ID, scan_range)
    if values is None:
        return "❌ Failed to read sheet data"
    if len(values) < 2:
        return "Sheet has fewer than 2 rows."

    missing = []
    for day in range(1, days_in_month + 1):
        target_date = datetime(year, month, day).date()
        checked = _get_duty_names_for_date(target_date, values)
        if not checked:
            missing.append(day)

    month_name = datetime(year, month, 1).strftime("%B %Y")
    if not missing:
        return f"✅ All days in {month_name} have duty assigned."
    else:
        missing_str = ", ".join(str(d) for d in missing)
        return f"⚠️ {month_name} missing duty date：{missing_str}"

def srethisweek():
    """Display duty for the current week only."""
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())

    # Need to read sheet data again inside this function
    try:
        token = get_tenant_access_token()
    except Exception as e:
        return f"❌ Failed to get access token: {e}"

    props = get_sheet_metadata(token, SPREADSHEET_TOKEN, SHEET_ID)
    if not props:
        return "❌ Cannot retrieve sheet metadata"
    max_row = props.get("rowCount", 200)
    scan_range = f"A1:ZZ{max_row}"
    values = get_range_values(token, SPREADSHEET_TOKEN, SHEET_ID, scan_range)
    if values is None:
        return "❌ Failed to read sheet data"
    if len(values) < 2:
        return "Sheet has fewer than 2 rows."

    week_names = set()
    for i in range(7):
        day = monday + timedelta(days=i)
        day_checked = _get_duty_names_for_date(day, values)
        week_names.update(day_checked)
    week_names = sorted(week_names)

    heading = f"📅 <b>SRE Duty this week – {monday.strftime('%d/%m/%Y')}</b>"
    if not week_names:
        return f"{heading} – no duty"

    lines = [heading]
    lines.extend(_format_sre_grouped_lines(week_names, html=True))
    return "\n".join(lines)

def sretwoweek():
    """Display duty for the current week and the following week."""
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    next_monday = monday + timedelta(days=7)

    try:
        token = get_tenant_access_token()
    except Exception as e:
        return f"❌ Failed to get access token: {e}"

    props = get_sheet_metadata(token, SPREADSHEET_TOKEN, SHEET_ID)
    if not props:
        return "❌ Cannot retrieve sheet metadata"
    max_row = props.get("rowCount", 200)
    scan_range = f"A1:ZZ{max_row}"
    values = get_range_values(token, SPREADSHEET_TOKEN, SHEET_ID, scan_range)
    if values is None:
        return "❌ Failed to read sheet data"
    if len(values) < 2:
        return "Sheet has fewer than 2 rows."

    def week_summary(start_monday, title):
        week_names = set()
        for i in range(7):
            day = start_monday + timedelta(days=i)
            day_checked = _get_duty_names_for_date(day, values)
            week_names.update(day_checked)
        lines = [f"📅 <b>{title}</b>"]
        if not week_names:
            lines.append("– no duty")
        else:
            lines.extend(_format_sre_grouped_lines(sorted(week_names), html=True))
        return "\n".join(lines)

    # 注意：title 不再包含 "📅 " 前缀
    this_week_str = week_summary(monday, f"SRE Duty this week – {monday.strftime('%d/%m/%Y')}")
    next_week_str = week_summary(next_monday, f"SRE Duty next week – {next_monday.strftime('%d/%m/%Y')}")
    return f"{this_week_str}\n\n{next_week_str}"

# Update the existing get_sre_week_duty to use the new two‑week implementation
# (keeping the original name for backward compatibility)
def get_sre_week_duty():
    """Return this week and next week duty summary (same as sretwoweek)."""
    return sretwoweek()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg in ("--week", "-w"):
            print(get_sre_week_duty())
        elif arg == "--thisweek":
            print(srethisweek())
        elif arg == "--twoweek":
            print(sretwoweek())
        else:
            user_date = parse_date_arg(arg)
            if user_date is None:
                print(f"❌ Invalid date format. Please use DD/MM/YYYY (e.g., 03/02/2026)")
                sys.exit(1)
            print(get_sre_duty(user_date))
    else:
        print(get_sre_duty(datetime.now().date()))