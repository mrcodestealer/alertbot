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
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


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
    poll_interval_seconds: int = _int("POLL_INTERVAL_SECONDS", 60)
    # On first startup, announce the alerts that are already firing?
    announce_backlog_on_start: bool = _bool("ANNOUNCE_BACKLOG_ON_START", False)
    notify_on_resolve: bool = _bool("NOTIFY_ON_RESOLVE", True)
    # How long to keep resolved alerts in the /check "resolved" section (hours).
    state_retention_hours: int = _int("STATE_RETENTION_HOURS", 24)

    # --- Screenshot (Playwright) ---
    enable_screenshot: bool = _bool("ENABLE_SCREENSHOT", True)

    # --- Emoji reactions used for the /check command ---
    reaction_processing: str = os.getenv("REACTION_PROCESSING", "OnIt")
    reaction_done: str = os.getenv("REACTION_DONE", "DONE")
    reaction_error: str = os.getenv("REACTION_ERROR", "ERROR")
    check_command: str = os.getenv("CHECK_COMMAND", "/check").strip().lower()

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
