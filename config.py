"""Central configuration for AlertBot.

All values are read from environment variables (loaded from a local ``.env``
file via python-dotenv). Import ``CONFIG`` anywhere you need settings.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env sitting next to this file (works no matter the CWD systemd uses).
# override=True makes python-dotenv authoritative over values systemd may have
# already injected via EnvironmentFile — systemd keeps inline "# comments" in
# values, python-dotenv strips them, so this guarantees consistent parsing.
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on", "y"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip())
    except (TypeError, ValueError):
        return default


def _int_list(name: str, default: str, min_value: int = 1) -> list[int]:
    """Parse a comma-separated list of ints (e.g. '15,30,60'), tolerating inline
    '# comments' and whitespace. Values below min_value are dropped.
    (min_value=0 matters for WORK_DAYS, where Monday is 0.)"""
    out: list[int] = []
    for s in os.getenv(name, default).split(","):
        tok = s.split("#", 1)[0].strip()
        if tok.isdigit() and int(tok) >= min_value:
            out.append(int(tok))
    return sorted(set(out))


@dataclass
class Config:
    # --- Monitoring dashboard (MonitorFlow) ---
    # No credential defaults on purpose: a missing value must be caught by
    # validate() rather than silently falling back to a real password.
    monitor_base_url: str = os.getenv("MONITOR_BASE_URL", "https://monitor.client8.me").rstrip("/")
    monitor_username: str = os.getenv("MONITOR_USERNAME", "")
    monitor_password: str = os.getenv("MONITOR_PASSWORD", "")

    # --- Lark app credentials ---
    lark_app_id: str = os.getenv("LARK_APP_ID", "")
    lark_app_secret: str = os.getenv("LARK_APP_SECRET", "")
    lark_verification_token: str = os.getenv("LARK_VERIFICATION_TOKEN", "")
    # open.larksuite.com (international) or open.feishu.cn (China).
    lark_domain: str = os.getenv("LARK_DOMAIN", "https://open.larksuite.com").rstrip("/")

    # Chat that new-alert notifications are pushed to (oc_xxxx). Leave blank to
    # disable proactive pushes (the /check command still works everywhere).
    lark_alert_chat_id: str = os.getenv("LARK_ALERT_CHAT_ID", "")

    # --- Watcher behaviour ---
    # Severity to watch: "critical", "warning", "info", or "all".
    watch_severity: str = os.getenv("WATCH_SEVERITY", "critical").strip().lower()
    # Page size used when querying the alerts API (matches the dashboard view:
    # /alerts?page=1&page_size=50&severity=critical). The client still paginates
    # to cover everything, but requests this many per page.
    monitor_page_size: int = _int("MONITOR_PAGE_SIZE", 50)
    poll_interval_seconds: int = _int("POLL_INTERVAL_SECONDS", 60)
    # Post an FYI reminder in the alert's thread when it has been firing this long
    # (minutes). Comma-separated for multiple, e.g. "15,30,60". Empty = off.
    firing_reminder_minutes: list[int] = field(default_factory=lambda: _int_list("FIRING_REMINDER_MINUTES", "15"))
    # On first startup, announce the alerts that are already firing?
    announce_backlog_on_start: bool = _bool("ANNOUNCE_BACKLOG_ON_START", False)
    notify_on_resolve: bool = _bool("NOTIFY_ON_RESOLVE", True)
    # Drop an alert from state.json as soon as it resolves (after its resolve card
    # is posted). Keeps state.json small; makes /check's "resolved" list empty.
    clear_resolved: bool = _bool("CLEAR_RESOLVED", False)
    # How long to keep resolved alerts in the /check "resolved" section (hours).
    # Ignored when CLEAR_RESOLVED is true.
    state_retention_hours: int = _int("STATE_RETENTION_HOURS", 24)

    # --- Screenshot (Playwright) ---
    enable_screenshot: bool = _bool("ENABLE_SCREENSHOT", True)

    # --- Emoji reactions used for the /check command ---
    reaction_processing: str = os.getenv("REACTION_PROCESSING", "OnIt")
    reaction_done: str = os.getenv("REACTION_DONE", "DONE")
    reaction_error: str = os.getenv("REACTION_ERROR", "ERROR")
    check_command: str = os.getenv("CHECK_COMMAND", "/check").strip().lower()

    # --- Self-deploy via DM (git pull + restart service) ---
    # OFF by default: this executes shell commands on the server.
    deploy_enabled: bool = _bool("DEPLOY_ENABLED", False)
    deploy_branch: str = os.getenv("DEPLOY_BRANCH", "main")
    deploy_service: str = os.getenv("DEPLOY_SERVICE", "alertbot")
    # Directory the git pull runs in. Defaults to where the code lives.
    deploy_git_dir: str = os.getenv("DEPLOY_GIT_DIR", str(BASE_DIR))
    # Comma-separated Lark open_ids allowed to deploy. Empty = any DM sender
    # (a warning is logged). Get your open_id by DMing the bot "/whoami".
    # --- Duty lookup (logic copied verbatim from dutybot: sre_Duty.py / db_duty.py) ---
    # Those modules read APP_ID / APP_SECRET from the environment. This bot's own
    # Lark app can read the duty spreadsheet, so they default to LARK_APP_ID /
    # LARK_APP_SECRET — no second set of credentials needed. Set APP_ID/APP_SECRET
    # explicitly only if you ever need a different app for the sheet.
    duty_enabled: bool = _bool("DUTY_ENABLED", True)
    duty_app_id: str = os.getenv("APP_ID") or os.getenv("LARK_APP_ID", "")
    duty_app_secret: str = os.getenv("APP_SECRET") or os.getenv("LARK_APP_SECRET", "")
    # Which spreadsheet holds the roster (document identifiers, not credentials).
    ose_spreadsheet_token: str = os.getenv("OSE_SPREADSHEET_TOKEN", "O4Dfw4DVTiPpFukn801l5z3WgMd")
    ose_sheet_id: str = os.getenv("OSE_SHEET_ID", "AS33r7")
    # Chat that "Report to SRE" posts into (defaults to the alert chat).
    report_chat_id: str = os.getenv("REPORT_CHAT_ID", "") or os.getenv("LARK_ALERT_CHAT_ID", "")
    # People to never tag (leavers). Kept here rather than editing the copied
    # dutybot modules, so those stay re-copyable. Comma-separated.
    duty_exclude: list[str] = field(
        default_factory=lambda: [
            tok
            for s in os.getenv("DUTY_EXCLUDE", "").split(",")
            if (tok := s.split("#", 1)[0].strip())
        ]
    )
    # name -> open_id map collected via /secret1, used to @-tag the duty person.
    duty_openid_file: Path = field(
        default_factory=lambda: BASE_DIR / os.getenv("DUTY_OPENID_FILE", "duty_openids.json")
    )

    # --- Knowledge base (SOP doc -> monitorflow.json) ---
    kb_enabled: bool = _bool("KB_ENABLED", True)
    # Lark Wiki node token from the doc URL (…/wiki/<TOKEN>).
    kb_wiki_token: str = os.getenv("KB_WIKI_TOKEN", "")
    kb_refresh_minutes: int = _int("KB_REFRESH_MINUTES", 60)
    kb_file: Path = field(default_factory=lambda: BASE_DIR / os.getenv("KB_FILE", "monitorflow.json"))
    # Ollama (or any OpenAI-compatible /api/chat) endpoint + models.
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen3.6:35b-a3b")
    # Optional vision model for reading the doc's screenshots. Blank = skip images
    # (set to a VL model, e.g. "qwen2.5vl:7b", to have them described).
    ollama_vision_model: str = os.getenv("OLLAMA_VISION_MODEL", "")
    ollama_timeout_seconds: int = _int("OLLAMA_TIMEOUT_SECONDS", 1800)
    # Ollama's default context is only 4k, which truncates the doc and can make
    # the model return an EMPTY reply. Keep this comfortably above doc+output.
    ollama_num_ctx: int = _int("OLLAMA_NUM_CTX", 32768)
    ollama_num_predict: int = _int("OLLAMA_NUM_PREDICT", 8192)
    # Per-image timeout for the vision model, and a total budget for captioning
    # the whole doc. Captioning is a nice-to-have: when the budget is spent the
    # build carries on with text only rather than stalling for an hour.
    ollama_vision_timeout_seconds: int = _int("OLLAMA_VISION_TIMEOUT_SECONDS", 120)
    kb_caption_budget_seconds: int = _int("KB_CAPTION_BUDGET_SECONDS", 600)
    # Working-hours window used to pick which SOP guidance to show.
    work_start_hour: int = _int("WORK_START_HOUR", 9)
    work_end_hour: int = _int("WORK_END_HOUR", 18)
    # Mon=0 … Sun=6. Default Mon–Fri.
    work_days: list[int] = field(
        default_factory=lambda: _int_list("WORK_DAYS", "0,1,2,3,4", min_value=0) or [0, 1, 2, 3, 4]
    )
    work_timezone_offset_hours: int = _int("WORK_TZ_OFFSET_HOURS", 8)

    # --- /log command (read journalctl from chat) ---
    log_command_enabled: bool = _bool("LOG_COMMAND_ENABLED", True)
    log_default_lines: int = _int("LOG_DEFAULT_LINES", 40)
    log_max_lines: int = _int("LOG_MAX_LINES", 300)
    # Reuses DEPLOY_ADMIN_IDS as the authorized list (logs can be sensitive).

    deploy_admin_ids: list[str] = field(
        # Split on ',', drop any accidental inline "# comment", and trim whitespace
        # (open_ids never contain '#', so this is safe and forgiving).
        default_factory=lambda: [
            tok
            for s in os.getenv("DEPLOY_ADMIN_IDS", "").split(",")
            if (tok := s.split("#", 1)[0].strip())
        ]
    )

    # --- Misc ---
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    state_file: Path = field(default_factory=lambda: BASE_DIR / os.getenv("STATE_FILE", "state.json"))
    screenshot_dir: Path = field(default_factory=lambda: BASE_DIR / os.getenv("SCREENSHOT_DIR", "screenshots"))

    @property
    def monitor_api_base(self) -> str:
        return f"{self.monitor_base_url}/altermanager/api"

    @property
    def severity_filter(self) -> str | None:
        """Value to pass to the API ``severity`` query param (None = no filter)."""
        if self.watch_severity in {"", "all", "any"}:
            return None
        return self.watch_severity

    def validate(self) -> list[str]:
        """Return a list of human-readable configuration problems (empty = OK)."""
        problems: list[str] = []
        if not self.lark_app_id:
            problems.append("LARK_APP_ID is not set")
        if not self.lark_app_secret:
            problems.append("LARK_APP_SECRET is not set")
        if not self.monitor_username or not self.monitor_password:
            problems.append("MONITOR_USERNAME / MONITOR_PASSWORD is not set")
        return problems


CONFIG = Config()
