"""Who is on duty right now, per alert Domain.

The heavy lifting is done by ``sre_Duty.py`` and ``db_duty.py``, copied verbatim
from the dutybot project (do not edit them here — re-copy if dutybot changes).
This module only:
  * makes sure those modules see the environment variables they expect,
  * maps an alert's Domain to the right duty roster,
  * extracts plain names out of their formatted output, and
  * resolves names to Lark open_ids (collected via /secret1) for @-mentions.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime
from typing import Any

from config import CONFIG

log = logging.getLogger("alertbot.duty")

# The copied modules call os.getenv("APP_ID") / ("APP_SECRET") / ("OSE_*") at
# import time, so populate those names before importing them.
for _key, _val in (
    ("APP_ID", CONFIG.duty_app_id),
    ("APP_SECRET", CONFIG.duty_app_secret),
    ("OSE_SPREADSHEET_TOKEN", CONFIG.ose_spreadsheet_token),
    ("OSE_SHEET_ID", CONFIG.ose_sheet_id),
):
    if _val and not os.getenv(_key):
        os.environ[_key] = _val

# Domain (from the MonitorFlow alert) -> duty roster.
# Domains seen in the dashboard: PLATFORM, DB, Network, EGAME, LIVEGAME,
# LIVESLOTS, Unknown. Anything not listed falls back to DEFAULT_TEAM.
DOMAIN_TEAM = {
    "PLATFORM": "sre",
    "DB": "db",
}
DEFAULT_TEAM = "sre"

# Some alerts belong to a team regardless of their Domain — a LiveSlots node
# alert is filed under PLATFORM but should page the LiveSlot duty. Matched
# against the alert's rule + instance + description. Checked before the Domain.
KEYWORD_TEAM: list[tuple[str, str]] = [
    ("liveslot", "liveslot"),
]

TEAM_LABEL = {
    "sre": "Platform SRE duty (Backend Team)",
    "db": "DB duty",
    "liveslot": "LiveSlot duty",
}

# Only this section of the SRE roster is tagged for platform alerts — the
# frontend team does not handle them. Blank = tag everyone on the roster.
SRE_SECTION = os.getenv("SRE_DUTY_SECTION", "BACKEND").strip()

_BULLET_RE = re.compile(r"^\s*[•\-\*]\s*(.+?)\s*$")


class DutyLookupError(RuntimeError):
    pass


_import_lock = threading.Lock()
_sre_mod = None
_db_mod = None
_ls_mod = None


def _load_modules():
    """Import the copied dutybot modules lazily (they hit the network on use)."""
    global _sre_mod, _db_mod, _ls_mod
    with _import_lock:
        if _sre_mod is None or _db_mod is None or _ls_mod is None:
            import db_duty as _db  # noqa: PLC0415
            import liveslot_duty as _ls  # noqa: PLC0415
            import sre_Duty as _sre  # noqa: PLC0415

            _sre_mod, _db_mod, _ls_mod = _sre, _db, _ls
    return _sre_mod, _db_mod, _ls_mod


def team_for_domain(domain: str | None) -> str:
    return DOMAIN_TEAM.get((domain or "").strip().upper(), DEFAULT_TEAM)


def report_button_text(team: str) -> str:
    """Label for the report button, so it names the group it will post to."""
    if team == "liveslot":
        return CONFIG.report_button_text_liveslot
    return CONFIG.report_button_text


def report_chat_for(team: str) -> str:
    """Chat a report goes to. LiveSlots can have its own; otherwise the default."""
    if team == "liveslot" and CONFIG.report_chat_id_liveslot:
        return CONFIG.report_chat_id_liveslot
    return CONFIG.report_chat_id or CONFIG.lark_alert_chat_id


def alert_content(alert: dict[str, Any] | None) -> str:
    """The text searched for team keywords: rule + instance + description."""
    if not alert:
        return ""
    return " ".join(
        str(alert.get(k) or "")
        for k in ("alert_rule", "summary", "instance", "description")
    )


def team_for_alert(domain: str | None, content: str = "") -> str:
    """Route by content keyword first (e.g. anything LiveSlots), then Domain."""
    text = (content or "").lower()
    for keyword, team in KEYWORD_TEAM:
        if keyword in text:
            return team
    return team_for_domain(domain)


def _clean_name(line: str) -> str | None:
    """Pull a person's name out of a duty bullet line.

    sre_Duty: "• Alex Tai 📞60123456789"
    db_duty : "• Kah Zheng DB (Phone: +60169294328)"
    """
    m = _BULLET_RE.match(line)
    if not m:
        return None
    name = m.group(1)
    name = name.split("📞")[0]           # drop phone (SRE format)
    name = re.sub(r"\(Phone:.*?\)", "", name)  # drop phone (DB format)
    name = re.sub(r"\bDB\b\s*$", "", name.strip())  # trailing team marker
    name = name.strip(" -–—·:")
    return name or None


def parse_names(duty_text: str, *, only_section: str | None = None) -> list[str]:
    """Extract the on-duty names from a formatted duty block.

    sre_Duty groups its output under team headings::

        📅 SRE Duty – 01/08/2026
        BACKEND TEAM (FPMS, PMS, ...)
        • Wei Siong 📞601...
        • Clarence 📞601...

        FRONTEND TEAM (FRONTEND, POSTHOG, ...)
        • Alex Tai 📞601...

    ``only_section`` keeps just the names under a heading starting with that
    prefix (e.g. "BACKEND"), so platform alerts don't tag the frontend team.
    Names listed with no heading are skipped when a section filter is active —
    better to under-tag than to page the wrong team.
    """
    names: list[str] = []
    current: str | None = None
    want = (only_section or "").strip().upper()
    for line in (duty_text or "").splitlines():
        name = _clean_name(line)
        if name is None:
            stripped = line.strip()
            # A non-bullet, non-empty line that isn't the date title is a heading.
            if stripped and not stripped.startswith("📅"):
                current = stripped
            continue
        if want and not (current or "").upper().startswith(want):
            continue
        if name not in names:
            names.append(name)
    return names


def get_duty(domain: str | None = None, content: str = "") -> dict[str, Any]:
    """Return {team, label, text, names, error} for an alert.

    ``content`` is the alert's text (rule/instance/description); a keyword in it
    can override the Domain — e.g. a LiveSlots node alert pages LiveSlot duty
    even though its Domain is PLATFORM.
    """
    team = team_for_alert(domain, content)
    result: dict[str, Any] = {
        "team": team,
        "label": TEAM_LABEL.get(team, team.upper()),
        "text": "",
        "names": [],
        "error": None,
    }
    if not CONFIG.duty_enabled:
        result["error"] = "duty lookup disabled (DUTY_ENABLED=false)"
        return result
    try:
        sre_mod, db_mod, ls_mod = _load_modules()
        if team == "db":
            text = db_mod.get_db_day_duty(datetime.now().date())
            section = None  # DB roster has no team sections
        elif team == "liveslot":
            text = ls_mod.get_day_duty(datetime.now().date())
            section = None  # LiveSlot roster has no team sections
        else:
            text = sre_mod.get_sre_today_duty()
            section = SRE_SECTION or None
        result["text"] = text or ""
        # sre_Duty/db_duty report failures by RETURNING a string (e.g.
        # "❌ Failed to read sheet data"), not by raising — so detect that,
        # otherwise it looks like "nobody on duty".
        stripped = (text or "").strip()
        if not stripped:
            result["error"] = "duty lookup returned nothing"
            return result
        if stripped.startswith("❌") or "Failed to read sheet" in stripped:
            result["error"] = stripped.lstrip("❌ ").strip()[:200]
            return result

        parsed = parse_names(result["text"], only_section=section)
        dropped = [n for n in parsed if is_excluded(n)]
        if dropped:
            log.info("Excluding %s from duty (DUTY_EXCLUDE)", ", ".join(dropped))
        result["names"] = [n for n in parsed if not is_excluded(n)]
        result["excluded"] = dropped
        if not result["names"]:
            log.warning("No duty names parsed for domain=%r team=%s: %r", domain, team, text[:300])
            result["error"] = (
                f"no {'BACKEND ' if section else ''}duty names found in today's roster"
            )
    except Exception as e:  # noqa: BLE001
        log.exception("Duty lookup failed for domain=%r", domain)
        result["error"] = str(e)[:200]
    return result


# --------------------------------------------------------------- open_id map
def load_openids() -> dict[str, str]:
    try:
        if CONFIG.duty_openid_file.exists():
            return json.loads(CONFIG.duty_openid_file.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Could not read %s", CONFIG.duty_openid_file)
    return {}


def save_openids(mapping: dict[str, str]) -> None:
    tmp = CONFIG.duty_openid_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG.duty_openid_file)


def remember_openids(pairs: dict[str, str]) -> dict[str, str]:
    """Merge newly-seen name -> open_id pairs into the stored map."""
    mapping = load_openids()
    mapping.update({k.strip(): v for k, v in pairs.items() if k and v})
    save_openids(mapping)
    return mapping


def _norm(name: str) -> str:
    return re.sub(r"\s+", "", (name or "").lower())


def is_excluded(name: str) -> bool:
    """True for people who must never be tagged (e.g. left the company).

    Configured via DUTY_EXCLUDE so the copied dutybot modules stay untouched and
    can be re-copied when dutybot changes.
    """
    n = _norm(name)
    return bool(n) and any(_norm(x) == n for x in CONFIG.duty_exclude)


def resolve_openid(name: str, mapping: dict[str, str] | None = None) -> str | None:
    """Find the open_id for a duty name.

    The roster spells names as in the spreadsheet ("Kai Xuan") while /secret1
    stores Lark display names ("KaiXuan Ng"), so fall back to a containment
    match. Requires >=4 chars to avoid short-name collisions.
    """
    mapping = load_openids() if mapping is None else mapping
    target = _norm(name)
    if not target:
        return None
    by_norm = {_norm(k): v for k, v in mapping.items()}
    if target in by_norm:
        return by_norm[target]
    if len(target) >= 4:
        for key, oid in by_norm.items():
            if len(key) >= 4 and (target in key or key in target):
                return oid
    return None


def mention(names: list[str]) -> str:
    """Render duty names as Lark @-mentions where an open_id is known."""
    mapping = load_openids()
    out = []
    for n in names:
        oid = resolve_openid(n, mapping)
        out.append(f'<at id="{oid}"></at>' if oid else n)
    # Space-separated, not comma-separated: consecutive @-mentions read better
    # in Lark without punctuation between them.
    return " ".join(out) if out else "team"


def roster() -> dict[str, list[str]]:
    """Everyone who can ever appear on duty, read from the copied dutybot
    modules — so this list follows the source of truth, not a hardcoded copy.

    Only the BACKEND section of the SRE roster is included: the frontend team
    isn't tagged for platform alerts.
    """
    out: dict[str, list[str]] = {"sre_backend": [], "db": [], "liveslot": []}
    try:
        sre_mod, db_mod, ls_mod = _load_modules()
        for title, members in getattr(sre_mod, "SRE_TEAMS", []):
            if (SRE_SECTION or "BACKEND").upper() in str(title).upper():
                out["sre_backend"] = [m for m in members if not is_excluded(m)]
        out["db"] = [
            m.get("name", "")
            for m in getattr(db_mod, "TARGET_DUTY", [])
            if m.get("name") and not is_excluded(m.get("name", ""))
        ]
        # liveslot_duty uses {"display", "lookup"}; the sheet shows "display".
        out["liveslot"] = [
            m.get("display") or m.get("lookup") or ""
            for m in getattr(ls_mod, "TARGET_DUTY", [])
            if (m.get("display") or m.get("lookup")) and not is_excluded(m.get("display") or "")
        ]
    except Exception:
        log.exception("Could not read duty rosters")
    return out


def roster_coverage() -> dict[str, Any]:
    """Check every roster member against the saved open_id map."""
    mapping = load_openids()
    teams = roster()
    result: dict[str, Any] = {"teams": {}, "missing": []}
    for team, names in teams.items():
        rows = []
        for n in names:
            oid = resolve_openid(n, mapping)
            rows.append({"name": n, "open_id": oid})
            if not oid:
                result["missing"].append(n)
        result["teams"][team] = rows
    result["saved"] = len(mapping)
    return result


def duty_status(domain: str | None = None) -> dict[str, Any]:
    """Duty for a domain plus whether each name resolves to an open_id.
    Used by /duty — deliberately does NOT emit @-tags, so checking it doesn't
    ping anyone."""
    info = get_duty(domain)
    mapping = load_openids()
    info["resolved"] = [
        {"name": n, "open_id": resolve_openid(n, mapping)} for n in info.get("names") or []
    ]
    return info
