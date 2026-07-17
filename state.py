"""Persistent state so the bot doesn't re-announce alerts across restarts.

Stored as a small JSON file. Access is guarded by a lock because the watcher
thread and the /check command thread both touch it.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("alertbot.state")


def _summarize(alert: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields we need to render /check and resolve cards."""
    return {
        "id": alert.get("id"),
        "alert_rule": alert.get("alert_rule"),
        "summary": alert.get("summary"),
        "severity": alert.get("severity"),
        "status": alert.get("status"),
        "instance": alert.get("instance"),
        "env": alert.get("env"),
        "domain": alert.get("domain"),
        "created_at": alert.get("created_at"),
    }


class State:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self.last_id: int = 0
        self.seeded: bool = False
        # id (str) -> record dict with keys from _summarize + status/announced_at/resolved_at
        self.watched: dict[str, dict[str, Any]] = {}
        self._load()

    # --------------------------------------------------------------- persist
    def _load(self) -> None:
        if not self._path.exists():
            log.info("No state file at %s (fresh start)", self._path)
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self.last_id = int(data.get("last_id", 0))
            self.seeded = bool(data.get("seeded", False))
            self.watched = data.get("watched", {}) or {}
            log.info("Loaded state: last_id=%s, watched=%d", self.last_id, len(self.watched))
        except Exception:  # pragma: no cover - defensive
            log.exception("Failed to load state file; starting fresh")

    def save(self) -> None:
        with self._lock:
            tmp = self._path.with_suffix(".tmp")
            payload = {"last_id": self.last_id, "seeded": self.seeded, "watched": self.watched}
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)

    # ---------------------------------------------------------------- mutate
    def track(self, alert: dict[str, Any], *, announced: bool) -> None:
        """Add/refresh an alert in the watched set."""
        with self._lock:
            aid = str(alert.get("id"))
            rec = self.watched.get(aid, {})
            rec.update(_summarize(alert))
            rec["status"] = alert.get("status", rec.get("status", "firing"))
            if "announced_at" not in rec:
                rec["announced_at"] = time.time() if announced else None
            self.watched[aid] = rec

    def mark_resolved(self, alert_id: int | str, alert: dict[str, Any] | None = None) -> None:
        with self._lock:
            aid = str(alert_id)
            rec = self.watched.get(aid)
            if rec is None:
                rec = _summarize(alert) if alert else {"id": alert_id}
                self.watched[aid] = rec
            if alert:
                rec.update(_summarize(alert))
            rec["status"] = "resolved"
            rec["resolved_at"] = time.time()

    def is_tracked(self, alert_id: int | str) -> bool:
        with self._lock:
            return str(alert_id) in self.watched

    def firing(self) -> list[dict[str, Any]]:
        with self._lock:
            return [r for r in self.watched.values() if r.get("status") == "firing"]

    def resolved(self) -> list[dict[str, Any]]:
        with self._lock:
            return [r for r in self.watched.values() if r.get("status") == "resolved"]

    def prune(self, retention_hours: int) -> None:
        """Drop resolved alerts older than the retention window."""
        cutoff = time.time() - retention_hours * 3600
        with self._lock:
            drop = [
                aid
                for aid, rec in self.watched.items()
                if rec.get("status") == "resolved" and (rec.get("resolved_at") or 0) < cutoff
            ]
            for aid in drop:
                del self.watched[aid]
            if drop:
                log.debug("Pruned %d resolved alerts from state", len(drop))
