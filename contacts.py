#!/usr/bin/env python3
"""
contacts — per-department contact directory.

Phone numbers come from **each team's own document** (their source of truth)
instead of ``dutyList.csv``, which drifts out of date: the FE sheet lists
``Zhi Jing → 6016-6704382`` while the CSV has no Zhi Jing at all, so the old
CSV-only lookup fuzzy-matched a different person and showed the wrong number.

Sources (each overridable via env):

    FPMS       sheet  FRGDsIAChhoc… tab 0GNrVB   col C name, col D phone
    PMS        wiki   JSKQwaZc…    tab 16pVrH    A name, B phone, E remark
    BI         sheet  EywEsYs6…    tab fClfTw    A name, B phone
    FE         sheet  BguAsEcC…    tab HoMkYJ    col M name, col N phone
    F&T        base   ER4pbXX…     table tblFMNx0E3z7U1PG  Member / Phone Number / remark
    SRE·DBA·   OSE duty sheet (AS33r7) — the number is inline in the duty-name
    Liveslot   cell, e.g. ``Jay (+6011 16392152)`` / ``Ken +60192336398``
    CPMS       handled by ``cpms_duty`` (its own "Contact List" parser)
    AI         ``ai_duty`` keeps dutyList.csv / ai_duty_phones.json

Anything not found in a department source falls back to ``dutyList.csv`` so a
lookup never gets *worse* than before. Name matching uses whole-word tokens
(``duty_list_match.resolve_phone_from_cache``) so a short name can never hijack
a longer, different one.

Usage:
    ./contacts.py            # dump every department directory
    ./contacts.py fe         # dump one department
"""

import os
import re
import sys
import time
import threading
from typing import Any, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

_BASE = "https://open.larksuite.com"

APP_ID = os.getenv("APP_ID")
APP_SECRET = os.getenv("APP_SECRET")


def _env(name: str, default: str) -> str:
    return (os.getenv(name) or default).strip() or default


# ---- source configuration (env-overridable) --------------------------------
FPMS_SHEET_TOKEN = _env("FPMS_CONTACT_SHEET_TOKEN", "FRGDsIAChhoc1WtTBfplZyqJgQf")
FPMS_CONTACT_TAB = _env("FPMS_CONTACT_SHEET_ID", "0GNrVB")

PMS_WIKI_TOKEN = _env("PMS_CONTACT_WIKI_TOKEN", "JSKQwaZc9iL0aikLCnPl57dsgAd")
PMS_CONTACT_TAB = _env("PMS_CONTACT_SHEET_ID", "16pVrH")

BI_SHEET_TOKEN = _env("BI_CONTACT_SHEET_TOKEN", "EywEsYs6vhMaGQtsbBHluy5cgGg")
BI_CONTACT_TAB = _env("BI_CONTACT_SHEET_ID", "fClfTw")

FE_SHEET_TOKEN = _env("FE_CONTACT_SHEET_TOKEN", "BguAsEcCHhdL9WtV2p1l7jC2g8X")
FE_CONTACT_TAB = _env("FE_CONTACT_SHEET_ID", "HoMkYJ")
# Columns M / N (0-based 12 / 13).
FE_NAME_COL = int(_env("FE_CONTACT_NAME_COL", "12"))
FE_PHONE_COL = int(_env("FE_CONTACT_PHONE_COL", "13"))

FT_BASE_ID = _env("FT_CONTACT_BASE_ID", "ER4pbXXTcaSKQDsdPL0lB7BDgVh")
FT_TABLE_ID = _env("FT_CONTACT_TABLE_ID", "tblFMNx0E3z7U1PG")

try:
    _CACHE_TTL = max(0, int(_env("CONTACTS_CACHE_SEC", "600")))
except ValueError:
    _CACHE_TTL = 600

_cache: dict[str, tuple[float, dict[str, dict[str, str]]]] = {}
_lock = threading.Lock()
_token_cache: dict[str, Any] = {"token": None, "exp": 0.0}
# dept → last read error, so a partial rebuild can be refused (see build_roster).
_last_error: dict[str, str] = {}


# ---- Lark plumbing ---------------------------------------------------------
def _token() -> str:
    now = time.time()
    if _token_cache["token"] and now < float(_token_cache["exp"] or 0):
        return str(_token_cache["token"])
    r = requests.post(
        f"{_BASE}/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
        timeout=15,
    ).json()
    if r.get("code") != 0:
        raise RuntimeError(f"token failed: {r}")
    _token_cache["token"] = r["tenant_access_token"]
    try:
        exp = int(r.get("expire") or 7200)
    except (TypeError, ValueError):
        exp = 7200
    _token_cache["exp"] = time.time() + max(60, exp - 120)
    return str(_token_cache["token"])


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token()}"}


class ContactSourceError(RuntimeError):
    """A contact document could not be read (permission, rate limit, renamed tab…).

    Raised rather than returning an empty list: an empty directory is
    indistinguishable from "the team has no contacts", which silently pushed
    every phone lookup onto the roster fallback and let the scheduled rebuild
    delete a whole team from dutyList.json.
    """


def _wiki_sheet_token(wiki_token: str) -> Optional[str]:
    r = requests.get(
        f"{_BASE}/open-apis/wiki/v2/spaces/get_node",
        headers=_headers(),
        params={"token": wiki_token},
        timeout=20,
    ).json()
    if r.get("code") != 0:
        raise ContactSourceError(
            f"wiki {wiki_token}: code={r.get('code')} msg={r.get('msg')}"
        )
    return ((r.get("data") or {}).get("node") or {}).get("obj_token")


def _sheet_values(sheet_token: str, tab: str, rng: str = "A1:Z200") -> list[list[Any]]:
    url = (
        f"{_BASE}/open-apis/sheets/v2/spreadsheets/{sheet_token}/values/{tab}!{rng}"
        "?valueRenderOption=FormattedValue"
    )
    r = requests.get(url, headers=_headers(), timeout=30).json()
    if r.get("code") != 0:
        raise ContactSourceError(
            f"sheet {sheet_token}!{tab}: code={r.get('code')} msg={r.get('msg')}"
        )
    return (r.get("data") or {}).get("valueRange", {}).get("values", []) or []


def _bitable_records(app_token: str, table_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    page = ""
    for _ in range(10):
        params: dict[str, Any] = {"page_size": 200}
        if page:
            params["page_token"] = page
        r = requests.get(
            f"{_BASE}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            headers=_headers(),
            params=params,
            timeout=30,
        ).json()
        if r.get("code") != 0:
            raise ContactSourceError(
                f"bitable {app_token}/{table_id}: code={r.get('code')} msg={r.get('msg')}"
            )
        data = r.get("data") or {}
        out.extend(data.get("items") or [])
        page = data.get("page_token") or ""
        if not data.get("has_more"):
            break
    return out


# ---- cell / value helpers -------------------------------------------------
def _text(v: Any) -> str:
    """Flatten a Lark cell (string, rich-text list, mention dict) to text."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)):
        return str(v).strip()
    if isinstance(v, list):
        parts = []
        for it in v:
            t = _text(it)
            if t:
                parts.append(t)
        return " ".join(parts).strip()
    if isinstance(v, dict):
        for k in ("text", "name", "en_name", "value"):
            t = _text(v.get(k))
            if t:
                return t
        return ""
    return str(v).strip()


# Space/dash/bracket separators are allowed INSIDE one number, but never a line
# break: a cell holding two numbers ("9958927239\n9175302318") must not be
# glued into one 20-digit value.
_PHONE_RE = re.compile(r"\+?\d[\d\-()  ]{5,}\d")


def clean_phone(raw: str) -> str:
    """First phone-looking run in ``raw`` → ``+``/digits only ('' if none)."""
    s = _text(raw)
    if not s:
        return ""
    m = _PHONE_RE.search(s)
    if not m:
        return ""
    tok = m.group(0)
    plus = tok.strip().startswith("+")
    digits = re.sub(r"\D", "", tok)
    if len(digits) < 7:
        return ""
    return ("+" + digits) if plus else digits


# Labels in the duty sheet that are not people ("Duty Phone (MY): +60…").
_NON_PERSON_RE = re.compile(
    r"(?i)^(?:duty\b|non[\s-]?working|working\s*hour|remark|contact\s*list|"
    r"team\b|backend\s*team|frontend\s*team|sre\b|dba\b|ote\b|if\s|kindly\b)"
)


def strip_name(raw: str) -> str:
    """Roster label → bare name: drop inline numbers, roles and bracket notes."""
    s = _text(raw)
    if not s:
        return ""
    s = s.splitlines()[0]
    if _NON_PERSON_RE.match(s.strip()):
        return ""  # a section label, not a person
    s = re.sub(r"\(([^)]*\d[^)]*)\)", " ", s)      # "(+6011 16392152)"
    s = re.sub(r"\[[^\]]*\]", " ", s)               # "[call if can't reach…]"
    s = re.sub(r"\+?\d[\d\s\-]{5,}\d", " ", s)      # bare inline number
    # "(Manager)" / "( Manager )" / "(Team Lead )" — spacing inside varies.
    s = re.sub(r"\(\s*(?:manager|team\s*lead|senior|supervisor)\s*\)", " ", s, flags=re.I)
    s = re.sub(r"\b(?:call|whatsapp|whatapps?|phone)\b.*$", " ", s, flags=re.I)
    # Unbalanced brackets are left behind by cells like
    # "Jin (60125855200 (Whatsapp & Call))" — drop any stray bracket / marker.
    s = re.sub(r"[()\[\]*]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -–—:,&/")
    if not s or _NON_PERSON_RE.match(s):
        return ""
    return s


# ---- department providers -------------------------------------------------
def _dir_two_col(
    sheet_token: str, tab: str, name_col: int, phone_col: int, *, remark_col: int = -1
) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for row in _sheet_values(sheet_token, tab):
        row = row or []

        def col(i: int) -> str:
            return _text(row[i]) if 0 <= i < len(row) else ""

        name = strip_name(col(name_col))
        phone = clean_phone(col(phone_col))
        if not name or not phone:
            continue
        low = name.lower()
        if low in ("name", "member", "负责人", "姓名"):
            continue
        entry = {"name": name, "phone": phone}
        if remark_col >= 0:
            rem = _text(col(remark_col))
            if rem:
                entry["remark"] = rem
        out.setdefault(name, entry)
    return out


def _dir_fpms() -> dict[str, dict[str, str]]:
    # 排班表: col C = 负责人 (name), col D = 联系方式 (phone)
    return _dir_two_col(FPMS_SHEET_TOKEN, FPMS_CONTACT_TAB, 2, 3)


def _dir_pms() -> dict[str, dict[str, str]]:
    tok = _wiki_sheet_token(PMS_WIKI_TOKEN)
    if not tok:
        return {}
    return _dir_two_col(tok, PMS_CONTACT_TAB, 0, 1, remark_col=4)


def _dir_bi() -> dict[str, dict[str, str]]:
    return _dir_two_col(BI_SHEET_TOKEN, BI_CONTACT_TAB, 0, 1)


def _dir_fe() -> dict[str, dict[str, str]]:
    return _dir_two_col(FE_SHEET_TOKEN, FE_CONTACT_TAB, FE_NAME_COL, FE_PHONE_COL)


def _dir_ft() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for rec in _bitable_records(FT_BASE_ID, FT_TABLE_ID):
        f = rec.get("fields") or {}
        name = strip_name(_text(f.get("Member")))
        phone = clean_phone(_text(f.get("Phone Number")))
        if not name or not phone:
            continue
        entry = {"name": name, "phone": phone}
        rem = _text(f.get("remark"))
        if rem:
            entry["remark"] = rem
        out.setdefault(name, entry)
    return out


GAME_WIKI_TOKEN = _env("GAME_CONTACT_WIKI_TOKEN", "P7ATwNGfci7idPkqlm4l4YUGgFn")
GAME_CONTACT_TAB = _env("GAME_CONTACT_SHEET_ID", "fea5bc")

# Column headers → department label (the user's naming: "Product Manager" is
# recorded as "Game Product Manager", "Game Operation" stays "Game Operation").
_GAME_PM_RE = re.compile(r"(?i)product\s*manager|产品负责人")
_GAME_OPS_RE = re.compile(r"(?i)game\s*operation|游戏运营")
_GAME_HEADER_RE = re.compile(r"(?i)负责人|person\s+in\s+charge|studio\s*manager|department|部门")


def _dir_game() -> dict[str, dict[str, str]]:
    """Game / OM / CRD / OS / Studio contacts from the Emergency-Contact sheet.

    Layout is several stacked sections of ``name | phone`` column pairs. The
    department comes from the column header when it names a role (Product
    Manager / Game Operation) and otherwise from the section's own Department
    column (OM, CRD-CS, C & P, OS, Studio…). ``@`` prefixes are stripped —
    plain names only. Read-only: nothing is ever written back to this sheet.
    """
    tok = _wiki_sheet_token(GAME_WIKI_TOKEN) or GAME_WIKI_TOKEN
    rows = _sheet_values(tok, GAME_CONTACT_TAB, "A1:J120")
    out: dict[str, dict[str, str]] = {}
    col_dept: dict[int, str] = {}
    section_dept = ""

    def cell(row: list[Any], i: int) -> str:
        return _text(row[i]) if 0 <= i < len(row) else ""

    for row in rows:
        row = row or []
        joined = " ".join(_text(c) for c in row)
        if _GAME_HEADER_RE.search(joined) and not _PHONE_RE.search(joined):
            # New section header — remember which columns are which role.
            col_dept = {}
            for i, c in enumerate(row):
                h = _text(c)
                if _GAME_PM_RE.search(h):
                    col_dept[i] = "Game Product Manager"
                elif _GAME_OPS_RE.search(h):
                    col_dept[i] = "Game Operation"
            section_dept = ""
            continue
        # A department cell in column A carries down the section's rows.
        a = cell(row, 0)
        if a and not _PHONE_RE.search(a):
            first = a.splitlines()[0].strip()
            if first and not _GAME_PM_RE.search(first) and len(first) <= 32:
                section_dept = first
        for i in range(len(row) - 1):
            raw_name = cell(row, i)
            raw_phone = cell(row, i + 1)
            if not raw_name or "@" not in raw_name or not clean_phone(raw_phone):
                continue
            # A cell may hold two @mentions ("@Angel Leong @Sherlyn Ong") with one
            # number per line next to it — pair them positionally when counts match.
            names = [
                n for n in (strip_name(p) for p in raw_name.split("@") if p.strip())
                if n and len(n) <= 40
            ]
            phones = [p for p in (clean_phone(ln) for ln in raw_phone.splitlines()) if p]
            if not names or not phones:
                continue
            dept = col_dept.get(i) or section_dept or "GAME"
            for idx, name in enumerate(names):
                phone = phones[idx] if idx < len(phones) else phones[0]
                out.setdefault(
                    name, {"name": name, "phone": phone, "department": dept}
                )
    return out


# Section headings in the OSE sheet's contact block → roster department label.
_OSE_SECTION_LABELS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"(?i)^sre\s*platform"), "PLATFORM SRE"),
    (re.compile(r"(?i)^sre\s*game"), "GAME"),
    (re.compile(r"(?i)^dba\b"), "DB"),
    (re.compile(r"(?i)^ote\b"), "OTE"),
    (re.compile(r"(?i)^live\s*slot"), "LIVESLOT"),
)


# "Project Handler (Emergency Contact List)" — every tab EXCEPT the OLD one.
# Used to fill GAPS only: people who are not in the duty roster yet. It never
# overrides a number that a team's own duty document already provides.
PROJECT_HANDLER_WIKI = _env("PROJECT_HANDLER_WIKI_TOKEN", "XiDnwK7nliWpQwkcEoplxwPRgSZ")
PROJECT_HANDLER_TABS: tuple[tuple[str, str], ...] = (
    ("043420", "Dev Side"),
    ("LsBLIW", "SRE - Platform"),
    ("KvLH1E", "SRE & PO - Game"),
    ("EzdPmU", "OSE & QA"),
    ("IEK4wI", "Others"),
)
# zHKZDn ("Project Handler List (OLD)") is deliberately excluded.
PROJECT_HANDLER_SKIP_TABS = frozenset({"zHKZDn"})

_PH_NAME_HDR_RE = re.compile(r"(?i)^\s*name\s*$")
_PH_PHONE_HDR_RE = re.compile(r"(?i)contact\s*(?:number|no)")
# Section labels / titles that share the Name column with real people.
_PH_NOT_A_PERSON_RE = re.compile(
    r"(?i)\b(?:duty|shift|list|team|management|manager\s*$|department|排班|"
    r"handler|project|group|dept)\b|&|排班表"
)


def _dir_project_handler() -> dict[str, dict[str, str]]:
    """Contacts from the Project Handler doc (gap-fill source).

    Each tab has a header row naming a ``Name`` column and a ``Contact Number``
    column; column A carries the section/role, which becomes the department.
    """
    tok = _wiki_sheet_token(PROJECT_HANDLER_WIKI) or PROJECT_HANDLER_WIKI
    out: dict[str, dict[str, str]] = {}
    for tab, label in PROJECT_HANDLER_TABS:
        if tab in PROJECT_HANDLER_SKIP_TABS:
            continue
        try:
            rows = _sheet_values(tok, tab, "A1:J200")
        except ContactSourceError as exc:
            print(f"⚠️ contacts: project-handler tab {tab} unreadable — {exc}", flush=True)
            continue
        name_col = phone_col = -1
        section = ""
        for row in rows:
            row = row or []

            def cell(i: int) -> str:
                return _text(row[i]) if 0 <= i < len(row) else ""

            # Header row: locate the Name / Contact Number columns for this tab.
            hdr_name = next(
                (i for i, c in enumerate(row) if _PH_NAME_HDR_RE.match(_text(c))), -1
            )
            hdr_phone = next(
                (i for i, c in enumerate(row) if _PH_PHONE_HDR_RE.search(_text(c))), -1
            )
            if hdr_name >= 0 and hdr_phone >= 0:
                name_col, phone_col = hdr_name, hdr_phone
                continue
            if name_col < 0 or phone_col < 0:
                continue
            a = cell(0)
            if a and not _PHONE_RE.search(a):
                first = a.splitlines()[0].strip()
                if first and len(first) <= 40:
                    section = first
            raw_name = cell(name_col)
            phone = clean_phone(cell(phone_col))
            if not raw_name or not phone:
                continue
            # Drop ALL parenthesised notes first — this doc annotates people with
            # roles and project lists ("Alex Tai (FPMS/NT/SMS/FGS)", "David (HOD)"),
            # which must not become part of the name. Do it before splitting so a
            # project list inside brackets isn't mistaken for a second person.
            cleaned = re.sub(r"\([^)]*\)", " ", raw_name)
            # "Jong Kwan Yung / Yang" — keep each alias as a person.
            for part in re.split(r"\s*/\s*", cleaned):
                name = strip_name(part)
                if not name or len(name) > 40:
                    continue
                if _PH_NOT_A_PERSON_RE.search(name) or len(name) < 3:
                    continue
                out.setdefault(
                    name,
                    {"name": name, "phone": phone, "department": section or label},
                )
    return out


# "Missing Contact" Bitable — the last resort for ``/s`` when nobody in any duty
# document or the roster matches. People are added here manually.
MISSING_CONTACT_BASE = _env("MISSING_CONTACT_BASE_ID", "CpdEbEofwaYyyEsSjlElKNxzgec")
MISSING_CONTACT_TABLE = _env("MISSING_CONTACT_TABLE_ID", "tblUYQZSVE8PW8Hh")


def missing_contacts() -> list[dict[str, str]]:
    """Rows of the manually-maintained "Missing Contact" table."""
    out: list[dict[str, str]] = []
    for rec in _bitable_records(MISSING_CONTACT_BASE, MISSING_CONTACT_TABLE):
        f = rec.get("fields") or {}
        name = strip_name(_text(f.get("Name")))
        if not name:
            continue
        out.append(
            {
                "name": name,
                "department": _text(f.get("Department")),
                "phone": clean_phone(_text(f.get("Contact Number")))
                or _text(f.get("Contact Number")),
                "note": _text(f.get("Text")),
                "source": "missing_contact",
            }
        )
    return out


def search_missing_contacts(query: str) -> list[dict[str, str]]:
    """Name search over the "Missing Contact" table (substring, case-insensitive)."""
    q = re.sub(r"\s+", "", (query or "").strip().lower())
    if not q:
        return []
    try:
        rows = missing_contacts()
    except Exception as exc:
        print(f"⚠️ contacts: Missing Contact table unreadable — {exc}", flush=True)
        return []
    hits = []
    for r in rows:
        nm = re.sub(r"\s+", "", r["name"].lower())
        if q in nm or nm in q:
            hits.append(r)
    return hits


def _dir_ose_inline() -> dict[str, dict[str, str]]:
    """SRE / DBA / Liveslot: number is inline in the duty-name cell of AS33r7.

    The sheet stacks these teams in one column, so the current section heading
    ("SRE PLATFORM", "DBA", "OTE", …) is tracked and stored as the person's
    department — otherwise every inline contact would inherit whichever
    department happened to be looked up last.
    """
    out: dict[str, dict[str, str]] = {}
    try:
        import ose_Duty as od

        values, _err = od._get_cached_ose_sheet_values()
    except Exception:
        values = None
    section = ""
    for row in values or []:
        raw = _text((row or [None])[0] if row else "")
        if not raw:
            continue
        phone = clean_phone(raw)
        if not phone:
            head = raw.splitlines()[0].strip()
            for pat, label in _OSE_SECTION_LABELS:
                if pat.match(head):
                    section = label
                    break
            continue
        name = strip_name(raw)
        if not name or len(name) > 40:
            continue
        entry = {"name": name, "phone": phone}
        if section:
            entry["department"] = section
        if re.search(r"(?i)\b(call|whatsapp|whatapps?)\b", raw):
            entry["remark"] = re.sub(r"\s+", " ", raw).strip()
        out.setdefault(name, entry)
    return out


_PROVIDERS: dict[str, Any] = {
    "fpms": _dir_fpms,
    "pms": _dir_pms,
    "bi": _dir_bi,
    "fe": _dir_fe,
    "ft": _dir_ft,
    "sre": _dir_ose_inline,
    "db": _dir_ose_inline,
    "dba": _dir_ose_inline,
    "liveslot": _dir_ose_inline,
    "game": _dir_game,
}

DEPARTMENTS: tuple[str, ...] = (
    "fpms", "pms", "bi", "fe", "ft", "sre", "db", "liveslot", "game",
)


def _norm_dept(dept: str) -> str:
    d = (dept or "").strip().lower().lstrip("/")
    return "db" if d == "dba" else d


def directory(dept: str, *, refresh: bool = False) -> dict[str, dict[str, str]]:
    """``{name: {"name", "phone", "remark"?}}`` from a department's own document."""
    key = _norm_dept(dept)
    provider = _PROVIDERS.get(key)
    if not provider:
        return {}
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if not refresh and hit and _CACHE_TTL > 0 and now - hit[0] < _CACHE_TTL:
            return hit[1]
    try:
        data = provider() or {}
        _last_error.pop(key, None)
    except Exception as exc:
        # Loud, and remembered: build_roster() must not persist a roster that is
        # missing a team because their document was briefly unreadable.
        print(f"⚠️ contacts: {key} contact document unreadable — {exc}", flush=True)
        _last_error[key] = str(exc)
        return {}
    if data:
        with _lock:
            _cache[key] = (now, data)
    else:
        print(f"⚠️ contacts: {key} contact document returned no usable rows", flush=True)
    return data


def invalidate(dept: Optional[str] = None) -> None:
    with _lock:
        if dept:
            _cache.pop(_norm_dept(dept), None)
        else:
            _cache.clear()


def _lookup(dept: str, name: str) -> Optional[dict[str, str]]:
    d = directory(dept)
    if not d:
        return None
    import duty_list_match as dlm

    cache = {n: n for n in d}  # reuse the safe token matcher to pick the name
    hit = dlm.resolve_phone_from_cache(strip_name(name) or name, cache)
    return d.get(hit) if hit else None


def _roster_fallback(dept: str, name: str) -> Optional[str]:
    """Roster fallback for a name missing from its department's own document.

    Department-aware on purpose. A duty name is matched **within its own
    department first** (loose token matching is safe there), and only then
    against other departments — where an **exact/normalised name match is
    required**. Without that split, loose matching across departments hands out
    the wrong person's number: FPMS's "David" would grab FE's "David Chin", and
    "Kelvin" (FE) / "Kelvin Er" (PLATFORM SRE) or "Jason" (FE) / "Jason Wong"
    (GAME) would cross over.
    """
    nm = strip_name(name) or name
    try:
        import duty_list_match as dlm

        rows = dlm.load_duty_list()
    except Exception:
        return None

    key = _norm_dept(dept)
    same: dict[str, str] = {}
    try:
        import leavewfh as lw

        for r in rows:
            rn, rd = str(r.get("name") or "").strip(), str(r.get("department") or "")
            if rn and lw.department_matches_command(rd, key):
                same.setdefault(rn, str(r.get("phone") or ""))
    except Exception:
        same = {}
    if same:
        try:
            hit = dlm.resolve_phone_from_cache(nm, same)
        except Exception:
            hit = None
        if hit:
            return hit

    # Other departments: exact / normalised name only — never a token-subset.
    try:
        target = dlm.normalize_name(nm)
    except Exception:
        return None
    if not target:
        return None
    for r in rows:
        rn = str(r.get("name") or "").strip()
        if rn and dlm.normalize_name(rn) == target:
            phone = str(r.get("phone") or "").strip()
            if phone:
                return phone
    return None


def get_phone(dept: str, name: str, *, fallback_csv: bool = True) -> Optional[str]:
    """Phone for ``name`` from the department's document; roster as last resort."""
    if not name:
        return None
    entry = _lookup(dept, name)
    if entry and entry.get("phone"):
        return entry["phone"]
    if not fallback_csv:
        return None
    return _roster_fallback(dept, name)


def get_remark(dept: str, name: str) -> Optional[str]:
    entry = _lookup(dept, name)
    return (entry or {}).get("remark") or None


if __name__ == "__main__":
    wanted = [a.lower() for a in sys.argv[1:]] or list(DEPARTMENTS)
    for dept in wanted:
        d = directory(dept)
        print(f"=== {dept.upper()} — {len(d)} contact(s) ===")
        for name, e in sorted(d.items()):
            rem = f"   ({e['remark'][:44]})" if e.get("remark") else ""
            print(f"  {name:<32} {e['phone']}{rem}")
        print()


# ================= Roster JSON (dutyList.json — used by /s) =================
# ``/s`` used to read dutyList.csv directly. The CSV drifts out of date, so the
# roster is now a JSON file built from every department's own contact document,
# with the CSV as the base layer for people no document lists (e.g. AI, PO,
# managers). Department documents win on conflicts, because they are maintained
# by the teams themselves.
ROSTER_JSON_PATH = _env("DUTY_LIST_JSON", "dutyList.json")

# Department key → label written into the roster for that source.
_ROSTER_DEPT_LABEL: dict[str, str] = {
    "fpms": "FPMS",
    "pms": "PMS",
    "bi": "BI",
    "fe": "FE",
    "ft": "F&T",
}


def _roster_json_path() -> str:
    p = ROSTER_JSON_PATH
    if os.path.isabs(p):
        return p
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), p)


def build_roster(
    *, include_csv: bool = True, failures: Optional[list[str]] = None
) -> list[dict[str, str]]:
    """Merge every contact source into ``[{name, department, phone}, …]``.

    Any department whose document could not be read is appended to ``failures``
    (when a list is passed) so the caller can refuse to persist a roster that is
    silently missing a whole team.
    """
    merged: dict[tuple[str, str], dict[str, str]] = {}

    def _nkey(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").strip()).lower()

    def _tight(s: str) -> str:
        """Identity key: lowercase, no spaces or punctuation ("Jun Meng"=="JunMeng")."""
        return re.sub(r"[^0-9a-z一-鿿]", "", (s or "").lower())

    def put(
        name: str, dept: str, phone: str, *, overwrite: bool, hint_dept: bool = False
    ) -> None:
        """Insert/refresh one roster row, keyed on **(name, department)**.

        Keying on the name alone silently corrupted rows: the OSE duty sheet
        writes bare first names, so its ``Kelvin (+60125989338)`` (Kelvin **Er**,
        PLATFORM SRE) and ``Jason (60123035820)`` (Jason **Wong**, GAME)
        overwrote the phones of FE's *different* Kelvin and Jason. Two same-named
        people in different departments must coexist as separate rows.

        ``hint_dept`` marks a source that knows the phone but not reliably the
        department (the OSE sheet stacks several teams in one column). Such a
        source may only refresh a row whose department already matches — it must
        never claim a name that another department already owns.
        """
        nm = (name or "").strip()
        ph = (phone or "").strip()
        if not nm or not ph:
            return
        nk, dk = _nkey(nm), _nkey(dept)
        if hint_dept:
            tight_nm, digits = _tight(nm), re.sub(r"\D", "", ph)
            same = [
                k
                for k, v in merged.items()
                if _tight(k[0]) == tight_nm
                or (digits and re.sub(r"\D", "", v.get("phone") or "") == digits)
            ]
            if same:
                if (nk, dk) in merged:
                    merged[(nk, dk)]["phone"] = ph  # same person, refresh number
                # Name owned by another department — never overwrite it.
                return
        key = (nk, dk)
        if key in merged and not overwrite:
            return
        merged[key] = {"name": nm, "department": (dept or "").strip(), "phone": ph}

    if include_csv:
        try:
            import duty_list_match as dlm

            for r in dlm.load_duty_list_csv():
                put(r.get("name", ""), r.get("department", ""), r.get("phone", ""), overwrite=False)
        except Exception as exc:
            print(f"⚠️ contacts: CSV base layer unavailable: {exc!r}", flush=True)

    # The OSE duty sheet is one stacked column, so its section label is a hint
    # rather than the truth — keep any department the roster already knows.
    _inline = {"sre", "db", "dba", "liveslot"}
    _last_error.clear()
    for dept in DEPARTMENTS:
        label = _ROSTER_DEPT_LABEL.get(dept, "")
        try:
            for _n, e in (directory(dept) or {}).items():
                put(
                    e.get("name", ""),
                    e.get("department") or label or dept.upper(),
                    e.get("phone", ""),
                    overwrite=True,
                    hint_dept=dept in _inline,
                )
        except Exception as exc:
            print(f"⚠️ contacts: roster skip {dept}: {exc!r}", flush=True)
            if failures is not None:
                failures.append(dept)

    # Project Handler doc — GAP FILL ONLY: add people who are not in the roster
    # yet (any department). Never overrides a number a team's own doc supplied.
    try:
        # Identity ignores spacing/punctuation AND matches on the phone number:
        # the roster spells one person "JunMeng" while this doc writes
        # "Jun Meng", and Aaron / Aaron Wong share 60177212270. Comparing raw
        # names added them twice, and the duplicate carried a different
        # department — which is how FE's Jun Meng showed up under "Other".
        existing_names = {_tight(k[0]) for k in merged}
        existing_phones = {
            re.sub(r"\D", "", r.get("phone") or "") for r in merged.values()
        }
        existing_phones.discard("")
        for e in (_dir_project_handler() or {}).values():
            nm = (e.get("name") or "").strip()
            ph = (e.get("phone") or "").strip()
            if not nm or not ph:
                continue
            if _tight(nm) in existing_names:
                continue
            if re.sub(r"\D", "", ph) in existing_phones:
                continue
            merged[(_nkey(nm), _nkey(e.get("department") or "Other"))] = {
                "name": nm,
                "department": (e.get("department") or "Other").strip(),
                "phone": ph,
            }
    except Exception as exc:
        print(f"⚠️ contacts: project-handler gap-fill skipped: {exc}", flush=True)
        if failures is not None:
            failures.append("project_handler")

    # Same person under several department labels with the SAME number (e.g.
    # Jason Wong listed as GAME and again as OM) → keep the first, drop the rest.
    by_person: dict[tuple[str, str], tuple[str, str]] = {}
    for key, row in list(merged.items()):
        ident = (_tight(key[0]), re.sub(r"\D", "", row.get("phone") or ""))
        if not ident[1]:
            continue
        if ident in by_person and by_person[ident] != key:
            merged.pop(key, None)
        else:
            by_person.setdefault(ident, key)

    if failures is not None:
        for dept, err in _last_error.items():
            if dept not in failures:
                failures.append(dept)
        if failures:
            print(
                f"⚠️ contacts: roster built WITHOUT {', '.join(sorted(set(failures)))} "
                "(document unreadable)",
                flush=True,
            )
    rows = sorted(merged.values(), key=lambda r: r["name"].lower())
    return rows


def write_roster_json(rows: Optional[list[dict[str, str]]] = None) -> str:
    """Write ``dutyList.json`` atomically; returns the path."""
    import json as _json

    if rows is None:
        rows = build_roster()
    path = _roster_json_path()
    tmp = f"{path}.{os.getpid()}.tmp"
    payload = {"updated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "contacts": rows}
    with open(tmp, "w", encoding="utf-8") as f:
        _json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def load_roster_json() -> list[dict[str, str]]:
    """Read ``dutyList.json`` (``[]`` when absent/unreadable)."""
    import json as _json

    path = _roster_json_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = _json.load(f)
    except Exception as exc:
        print(f"⚠️ contacts: {path} unreadable: {exc!r}", flush=True)
        return []
    rows = data.get("contacts") if isinstance(data, dict) else data
    out: list[dict[str, str]] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        nm = str(r.get("name") or "").strip()
        if nm:
            out.append(
                {
                    "name": nm,
                    "department": str(r.get("department") or "").strip(),
                    "phone": str(r.get("phone") or "").strip(),
                }
            )
    return out
