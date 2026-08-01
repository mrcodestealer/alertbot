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

TEAM_LABEL = {"sre": "Platform SRE duty", "db": "DB duty"}

_BULLET_RE = re.compile(r"^\s*[•\-\*]\s*(.+?)\s*$")


class DutyLookupError(RuntimeError):
    pass


_import_lock = threading.Lock()
_sre_mod = None
_db_mod = None


def _load_modules():
    """Import the copied dutybot modules lazily (they hit the network on use)."""
    global _sre_mod, _db_mod
    with _import_lock:
        if _sre_mod is None or _db_mod is None:
            import db_duty as _db  # noqa: PLC0415
            import sre_Duty as _sre  # noqa: PLC0415

            _sre_mod, _db_mod = _sre, _db
    return _sre_mod, _db_mod


def team_for_domain(domain: str | None) -> str:
    return DOMAIN_TEAM.get((domain or "").strip().upper(), DEFAULT_TEAM)


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


def parse_names(duty_text: str) -> list[str]:
    """Extract the on-duty names from a formatted duty block."""
    names: list[str] = []
    for line in (duty_text or "").splitlines():
        name = _clean_name(line)
        if name and name not in names:
            names.append(name)
    return names


def get_duty(domain: str | None = None) -> dict[str, Any]:
    """Return {team, label, text, names, error} for the given alert Domain."""
    team = team_for_domain(domain)
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
        sre_mod, db_mod = _load_modules()
        if team == "db":
            text = db_mod.get_db_day_duty(datetime.now().date())
        else:
            text = sre_mod.get_sre_today_duty()
        result["text"] = text or ""
        result["names"] = parse_names(result["text"])
        if not result["names"]:
            log.warning("No duty names parsed for domain=%r team=%s: %r", domain, team, text[:200])
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


def mention(names: list[str]) -> str:
    """Render duty names as Lark @-mentions where an open_id is known."""
    mapping = load_openids()
    by_norm = {_norm(k): v for k, v in mapping.items()}
    out = []
    for n in names:
        oid = by_norm.get(_norm(n))
        out.append(f'<at id="{oid}"></at>' if oid else n)
    return ", ".join(out) if out else "team"
