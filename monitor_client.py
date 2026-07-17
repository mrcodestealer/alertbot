"""HTTP client for the MonitorFlow alert dashboard (monitor.client8.me).

Handles JWT login, automatic token refresh on expiry / 401, and the three
endpoints we care about: list alerts, get one alert, and stats.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

import requests

from config import CONFIG

log = logging.getLogger("alertbot.monitor")


class MonitorClient:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._token: str | None = None
        self._token_expiry: float = 0.0  # epoch seconds
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ auth
    def login(self) -> None:
        """(Re)authenticate and cache the access token."""
        url = f"{CONFIG.monitor_api_base}/auth/login/"
        resp = self._session.post(
            url,
            json={"username": CONFIG.monitor_username, "password": CONFIG.monitor_password},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"login returned no access_token: {data}")
        self._token = token
        # expires_in is seconds (observed 43200 = 12h). Refresh 5 min early.
        expires_in = int(data.get("expires_in", 43200))
        self._token_expiry = time.time() + expires_in - 300
        log.info("Logged in to MonitorFlow as %s (token valid ~%ss)", CONFIG.monitor_username, expires_in)

    def _ensure_token(self) -> str:
        with self._lock:
            if not self._token or time.time() >= self._token_expiry:
                self.login()
            return self._token  # type: ignore[return-value]

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._ensure_token()}"}

    # --------------------------------------------------------------- request
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{CONFIG.monitor_api_base}{path}"
        for attempt in (1, 2):
            resp = self._session.get(url, params=params, headers=self._auth_headers(), timeout=30)
            if resp.status_code == 401 and attempt == 1:
                log.warning("Got 401 from %s, re-authenticating", path)
                with self._lock:
                    self._token = None  # force re-login on next _ensure_token
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"request to {path} failed after re-auth")

    # ----------------------------------------------------------------- calls
    def list_alerts(
        self,
        *,
        severity: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 200,
    ) -> list[dict[str, Any]]:
        """Return the ``results`` list from /alerts/optimized/."""
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if severity:
            params["severity"] = severity
        if status:
            params["status"] = status
        data = self._get("/alerts/optimized/", params=params)
        return data.get("results", []) if isinstance(data, dict) else []

    def list_all_alerts(
        self,
        *,
        severity: str | None = None,
        status: str | None = None,
        page_size: int = 200,
        max_pages: int = 25,
    ) -> list[dict[str, Any]]:
        """Fetch every matching alert, following pagination (capped at max_pages
        so an alert storm can't loop forever)."""
        out: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            params: dict[str, Any] = {"page": page, "page_size": page_size}
            if severity:
                params["severity"] = severity
            if status:
                params["status"] = status
            data = self._get("/alerts/optimized/", params=params)
            results = data.get("results", []) if isinstance(data, dict) else []
            out.extend(results)
            if not results or not (isinstance(data, dict) and data.get("next")):
                break
        else:
            log.warning("list_all_alerts hit max_pages=%d cap; results may be truncated", max_pages)
        return out

    def get_alert(self, alert_id: int | str) -> dict[str, Any]:
        """Full detail for a single alert (same data the eye/detail modal shows)."""
        return self._get(f"/alerts/{alert_id}/")

    def get_stats(self) -> dict[str, Any]:
        return self._get("/alerts/stats/")
