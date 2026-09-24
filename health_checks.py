"""Read-only dependency checks for the daily health report (health_report.py).

main() hands ``build(...)`` to ``health_report.start()``. Every check returns
(status, detail) and is strictly read-only: no chat messages, no Base/Sheet
writes, no MonitorFlow login, no browser launch, no LLM call and no KB refresh.
Network probes are single requests with a 10 s timeout; everything else reads
state the bot already keeps in memory or on disk. Details never carry secrets,
tokens or full URLs (URL paths hold document tokens, so errors keep the host only).
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse

import requests

from config import CONFIG

if TYPE_CHECKING:
    from knowledge import KnowledgeBase
    from refresher import KnowledgeRefresher
    from state import State
    from watcher import Watcher

_TIMEOUT = 10  # seconds, for every network probe
# sre_Duty.py reads the roster sheet from this host whatever LARK_DOMAIN says.
_DUTY_LARK_BASE = "https://open.larksuite.com"


# ------------------------------------------------------------------ helpers
def _span(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


_URL_ARG_RE = re.compile(r"(?i)(\burl: )\S+")
_URL_RE = re.compile(r"https?://(?:[^/\s@'\"]+@)?([^/\s:'\"]+)[^\s'\"]*")


def _scrub(text: Any) -> str:
    """One short line with every URL cut down to its host."""
    text = _URL_RE.sub(r"\1", _URL_ARG_RE.sub(r"\1…", " ".join(str(text).split())))
    return text[:160]


def _err(e: BaseException) -> str:
    return _scrub(f"{type(e).__name__}: {e}")


def _json(r: requests.Response) -> dict[str, Any]:
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"HTTP {r.status_code}, reply is not JSON") from None
    return data if isinstance(data, dict) else {}


_token_lock = threading.Lock()
# (base, app_id) -> (token, at, ms, error)
_tokens: dict[tuple[str, str], tuple[str, float, float, Exception | None]] = {}


def _tenant_token(base: str, app_id: str, app_secret: str) -> tuple[str, float]:
    """(tenant_access_token, fetch ms). Kept 60 s so the checks of one report share a fetch.
    A failure is kept too, so the other checks fail at once with the same error instead
    of queueing on the lock for another 10 s POST each and ending as 'no answer'."""
    if not app_id or not app_secret:
        raise RuntimeError("Lark app id / secret not configured")
    with _token_lock:
        hit = _tokens.get((base, app_id))
        if hit and time.time() - hit[1] < 60:
            if hit[3] is not None:
                raise hit[3]
            return hit[0], hit[2]
        t0 = time.monotonic()
        try:
            r = requests.post(
                f"{base}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
                timeout=_TIMEOUT,
            )
            ms = (time.monotonic() - t0) * 1000
            data = _json(r)
            if data.get("code") != 0 or not data.get("tenant_access_token"):
                msg = f"code {data.get('code')} {data.get('msg', '')}".strip()
                raise RuntimeError(f"tenant token refused: {msg}")
        except Exception as e:
            _tokens[(base, app_id)] = ("", time.time(), 0.0, e)
            raise
        _tokens[(base, app_id)] = (data["tenant_access_token"], time.time(), ms, None)
        return data["tenant_access_token"], ms


def _lark_get(base: str, token: str, path: str, params: dict[str, Any] | None = None) -> tuple[dict[str, Any], float]:
    t0 = time.monotonic()
    r = requests.get(
        f"{base}/open-apis{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=_TIMEOUT,
    )
    ms = (time.monotonic() - t0) * 1000
    data = _json(r)
    if data.get("code") != 0:
        raise RuntimeError(f"HTTP {r.status_code}, Lark code {data.get('code')}: {data.get('msg', '')}")
    return data.get("data") or {}, ms


# ------------------------------------------------------------------- checks
def check_lark_api() -> tuple[str, str]:
    """App credentials + API domain: one tenant-token fetch."""
    host = urlparse(CONFIG.lark_domain).hostname or "?"
    try:
        _token, ms = _tenant_token(CONFIG.lark_domain, CONFIG.lark_app_id, CONFIG.lark_app_secret)
    except Exception as e:  # noqa: BLE001
        return "fail", f"{host}: {_err(e)}"
    return "ok", f"tenant token issued in {ms:.0f} ms ({host})"


def check_watcher(watcher: Watcher | None, state: State, since: float) -> tuple[str, str]:
    """Alert poll loop: age of the last tick that finished, i.e. MonitorFlow answered."""
    interval = max(1, CONFIG.poll_interval_seconds)
    # A tick can include screenshots and the hourly catalogue scan (~2 min), so
    # allow several intervals before calling the loop stale.
    warn_after, fail_after = max(5 * interval, 300), max(15 * interval, 900)
    if watcher is None or not watcher.is_alive():
        return "fail", "alert-watcher thread is not running"
    last = getattr(watcher, "last_ok", 0.0) or 0.0
    age = time.time() - (last or since)
    what = f"last poll {_span(age)} ago" if last else f"no poll completed since start {_span(age)} ago"
    detail = f"{what} · {len(state.firing())} firing"
    if age > fail_after:
        return "fail", detail
    if age > warn_after:
        return "warn", detail
    return "ok", detail


def check_kb(knowledge: KnowledgeBase | None, refresher: KnowledgeRefresher | None) -> tuple[str | None, str]:
    """SOP knowledge base: entries loaded and the hourly refresher's last result (in memory)."""
    if not CONFIG.kb_enabled or knowledge is None:
        return None, "disabled by KB_ENABLED"
    if not CONFIG.kb_wiki_token:
        return None, "KB_WIKI_TOKEN unset, SOP sync off"
    n = len(knowledge.entries)
    base = f"{n} SOP entries (built {(knowledge.generated_at or '')[:10] or 'never'})"
    if refresher is None or not refresher.is_alive():
        return "fail", base + " · kb-refresher not running"
    st = refresher.last_status
    if st is None:
        if CONFIG.kb_file.with_suffix(".lock").exists():
            return "ok", base + " · rebuild in progress"
        return "warn", base + " · first refresh not finished yet"
    if not st.get("ok"):
        return ("warn" if n else "fail"), base + " · last refresh failed: " + _scrub(st.get("reason") or "?")
    if not n:
        return "fail", base + " · knowledge base is empty"
    return "ok", base + (" · last refresh rebuilt it" if st.get("changed") else " · last refresh ok, doc unchanged")


def check_ollama() -> tuple[str | None, str]:
    """Ollama (used only by the SOP rebuild): one GET /api/tags."""
    if not CONFIG.kb_enabled or not CONFIG.kb_wiki_token:
        return None, "SOP rebuild off (KB_ENABLED / KB_WIKI_TOKEN), the only Ollama user"
    u = urlparse(CONFIG.ollama_base_url)
    host = f"{u.hostname}:{u.port}" if u.port else (u.hostname or "?")
    t0 = time.monotonic()
    try:
        r = requests.get(f"{CONFIG.ollama_base_url}/api/tags", timeout=_TIMEOUT)
        r.raise_for_status()
        names = {m.get("name", "") for m in (_json(r).get("models") or [])}
    except Exception as e:  # noqa: BLE001
        return "fail", f"{host} unreachable: {_err(e)}"
    ms = (time.monotonic() - t0) * 1000
    wanted = {"text model (OLLAMA_MODEL)": CONFIG.ollama_model}
    if CONFIG.ollama_vision_model:
        wanted["vision model (OLLAMA_VISION_MODEL)"] = CONFIG.ollama_vision_model
    missing = [label for label, m in wanted.items() if m not in names and f"{m}:latest" not in names]
    if missing:
        return "warn", f"{host} up in {ms:.0f} ms, not pulled: " + ", ".join(missing)
    return "ok", f"HTTP 200 in {ms:.0f} ms ({host}) · {' + '.join(k.split()[0] for k in wanted)} model present"


def check_duty() -> tuple[str | None, str]:
    """Duty roster sheet readable (Report button @-tags) plus local open_id coverage."""
    if not CONFIG.duty_enabled:
        return None, "disabled by DUTY_ENABLED"
    try:
        token, _ms = _tenant_token(_DUTY_LARK_BASE, CONFIG.duty_app_id, CONFIG.duty_app_secret)
        data, ms = _lark_get(_DUTY_LARK_BASE, token, f"/sheets/v2/spreadsheets/{CONFIG.ose_spreadsheet_token}/metainfo")
    except Exception as e:  # noqa: BLE001
        return "fail", "roster sheet unreadable: " + _err(e)
    detail = f"roster sheet read in {ms:.0f} ms"
    status = "ok"
    try:
        import duty  # noqa: PLC0415
        import sre_Duty  # noqa: PLC0415 - constants only; it calls Lark only when asked

        tabs = {s.get("sheetId") for s in data.get("sheets") or []}
        if sre_Duty.SHEET_ID not in tabs:
            status, detail = "fail", detail + " · duty tab missing from the spreadsheet"
        cov = duty.roster_coverage()  # local: roster constants + duty_openids.json
        names = {r["name"] for rows in cov["teams"].values() for r in rows}
        if names:
            known = len(names - set(cov["missing"]))
            detail += f" · open_ids for {known}/{len(names)} roster names"
        else:  # roster() swallows a db_duty / liveslot_duty import error and returns nothing
            status, detail = "fail", detail + " · no roster names (dutybot modules did not load)"
    except Exception as e:  # noqa: BLE001
        # e.g. sre_Duty does not import: get_duty() needs it too, so every Report-button
        # duty lookup would fail as well.
        status, detail = "fail", detail + " · roster read failed: " + _err(e)
    return status, detail


def check_tracker() -> tuple[str | None, str]:
    """Alerts tracker Base readable: one GET of its table list (page_size=1)."""
    if not CONFIG.tracker_enabled:
        return None, "disabled by TRACKER_ENABLED"
    try:
        token, _ms = _tenant_token(CONFIG.lark_domain, CONFIG.lark_app_id, CONFIG.lark_app_secret)
        data, ms = _lark_get(CONFIG.lark_domain, token, f"/bitable/v1/apps/{CONFIG.tracker_app_token}/tables",
                             {"page_size": 1})
    except Exception as e:  # noqa: BLE001
        return "fail", "Base unreadable: " + _err(e)
    total = data.get("total")
    return "ok", f"Base read in {ms:.0f} ms" + (f" · {total} tables" if total is not None else "")


def _chromium_installed() -> bool | None:
    """Whether the Playwright browser cache holds a Chromium; None when it can't be located."""
    custom = os.getenv("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if custom == "0":
        return None  # browsers live inside the package
    if custom:
        root = Path(custom)
    elif sys.platform.startswith("linux"):
        root = Path(os.getenv("XDG_CACHE_HOME") or Path.home() / ".cache") / "ms-playwright"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        root = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "ms-playwright"
    try:
        return any(p.name.startswith("chromium") for p in root.iterdir())
    except OSError:
        return False


def check_screenshots(watcher: Watcher | None) -> tuple[str | None, str]:
    """Playwright installed with a Chromium, the watcher's last capture outcome, plus
    capture stats. No browser is launched."""
    if not CONFIG.enable_screenshot:
        return None, "disabled by ENABLE_SCREENSHOT"
    if importlib.util.find_spec("playwright") is None:
        return "fail", "playwright package not installed"
    now = time.time()
    try:
        shots = [p.stat() for p in CONFIG.screenshot_dir.glob("alert_*.png")]
    except OSError:
        shots = []
    parts = [f"{sum(1 for s in shots if now - s.st_mtime < 86400)} captured in 24h"]
    if shots:
        parts.append(f"last {_span(now - max(s.st_mtime for s in shots))} ago")
    parts.append(f"{len(shots)} files, {sum(s.st_size for s in shots) / 1048576:.0f} MB kept")
    if _chromium_installed() is False:
        return "warn", "no Chromium in the Playwright browser cache · " + " · ".join(parts)
    # capture_alert_detail() returns None on a login / selector / Chromium failure and
    # the alert card goes out without an image, so the files alone look fine.
    failed = getattr(watcher, "last_shot_fail", 0.0) or 0.0
    if failed > (getattr(watcher, "last_shot_ok", 0.0) or 0.0) and now - failed < 86400:
        return "warn", f"last capture failed {_span(now - failed)} ago, none worked since · " + " · ".join(parts)
    return "ok", " · ".join(parts)


def build(
    watcher: Watcher | None,
    state: State,
    knowledge: KnowledgeBase | None,
    refresher: KnowledgeRefresher | None,
) -> list[tuple[str, Callable[[], Any]]]:
    """(name, check) pairs for health_report.start(). Disabled features show as skipped."""
    since = time.time()
    return [
        ("Lark API", check_lark_api),
        ("MonitorFlow polling", lambda: check_watcher(watcher, state, since)),
        ("SOP knowledge base", lambda: check_kb(knowledge, refresher)),
        ("Ollama", check_ollama),
        ("Duty roster sheet", check_duty),
        ("Alerts tracker (Base)", check_tracker),
        ("Screenshots", lambda: check_screenshots(watcher)),
    ]
