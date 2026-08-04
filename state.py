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
        # Cumulative fire count (累计告警). Only the list endpoint returns it,
        # so keep it here for the resolve card to use.
        "alert_count": alert.get("alert_count"),
        "end_time": alert.get("end_time"),
    }


class State:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self.last_id: int = 0
        self.seeded: bool = False
        # id (str) -> record dict with keys from _summarize + status/announced_at/resolved_at
        self.watched: dict[str, dict[str, Any]] = {}
        # alert rule -> {"count": n, "last_resolved": epoch}. Tracks an alert
        # that keeps firing and resolving; keyed by RULE NAME because every
        # firing gets a brand-new alert id.
        self.flaps: dict[str, dict[str, Any]] = {}
        # Catalogue of every alert NAME ever seen -> {name,count,first_seen,
        # last_seen,severity,domain}. Lets /check report doc coverage over all
        # history, not just the current scan window.
        self.seen_rules: dict[str, dict[str, Any]] = {}
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
            self.flaps = data.get("flaps", {}) or {}
            self.seen_rules = data.get("seen_rules", {}) or {}
            log.info("Loaded state: last_id=%s, watched=%d", self.last_id, len(self.watched))
        except Exception:  # pragma: no cover - defensive
            log.exception("Failed to load state file; starting fresh")

    def save(self) -> None:
        with self._lock:
            tmp = self._path.with_suffix(".tmp")
            payload = {"last_id": self.last_id, "seeded": self.seeded,
                       "watched": self.watched, "flaps": self.flaps,
                       "seen_rules": self.seen_rules}
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

    def set_firing_message_id(self, alert_id: int | str, message_id: str) -> None:
        """Remember the Lark message_id of an alert's firing card, so the resolve
        card can be threaded under it."""
        with self._lock:
            rec = self.watched.get(str(alert_id))
            if rec is not None:
                rec["firing_message_id"] = message_id
                self.add_message_id(alert_id, message_id)

    def set_image_key(self, alert_id: int | str, image_key: str) -> None:
        """Remember the uploaded screenshot so the card can be re-rendered
        in place (the 'still firing' update) without re-capturing it."""
        with self._lock:
            rec = self.watched.get(str(alert_id))
            if rec is not None:
                rec["image_key"] = image_key

    def add_message_id(self, alert_id: int | str, message_id: str) -> None:
        """Track every message posted for this alert (card + threaded replies)
        so they can all be removed together when it resolves."""
        if not message_id:
            return
        with self._lock:
            rec = self.watched.get(str(alert_id))
            if rec is not None:
                ids = rec.setdefault("message_ids", [])
                if message_id not in ids:
                    ids.append(message_id)

    def get_message_ids(self, alert_id: int | str) -> list[str]:
        with self._lock:
            rec = self.watched.get(str(alert_id))
            return list(rec.get("message_ids") or []) if rec else []

    def get_firing_message_id(self, alert_id: int | str) -> str | None:
        with self._lock:
            rec = self.watched.get(str(alert_id))
            return rec.get("firing_message_id") if rec else None

    def add_reminded(self, alert_id: int | str, minutes: int) -> None:
        """Record that the N-minute 'still firing' reminder was sent for an alert."""
        with self._lock:
            rec = self.watched.get(str(alert_id))
            if rec is not None:
                sent = rec.setdefault("reminded", [])
                if minutes not in sent:
                    sent.append(minutes)

    def firing(self) -> list[dict[str, Any]]:
        with self._lock:
            return [r for r in self.watched.values() if r.get("status") == "firing"]

    def resolved(self) -> list[dict[str, Any]]:
        with self._lock:
            return [r for r in self.watched.values() if r.get("status") == "resolved"]

    def record_rules(self, alerts: list[dict[str, Any]], now: float | None = None) -> int:
        """Remember every alert NAME we've seen, with how often and when.

        This builds a lasting catalogue so /check can report documentation
        coverage across everything ever observed, instead of only the alerts
        that happen to be in the current scan window.
        Returns the number of names that were new.
        """
        now = time.time() if now is None else now
        new = 0
        with self._lock:
            for a in alerts:
                name = (a.get("alert_rule") or a.get("summary") or "").strip()
                if not name:
                    continue
                key = " ".join(name.split()).lower()
                rec = self.seen_rules.get(key)
                if rec is None:
                    rec = {"name": name, "count": 0, "first_seen": now,
                           "severity": a.get("severity"), "domain": a.get("domain")}
                    self.seen_rules[key] = rec
                    new += 1
                rec["count"] = int(rec.get("count", 0)) + 1
                rec["last_seen"] = now
                if a.get("severity"):
                    rec["severity"] = a.get("severity")
                if a.get("domain"):
                    rec["domain"] = a.get("domain")
        return new

    def prune_seen_rules(self, max_entries: int = 1000) -> None:
        """Cap the catalogue, dropping the least recently seen names."""
        with self._lock:
            if len(self.seen_rules) <= max_entries:
                return
            ordered = sorted(self.seen_rules.items(),
                             key=lambda kv: kv[1].get("last_seen") or 0, reverse=True)
            self.seen_rules = dict(ordered[:max_entries])

    def record_flap(self, rule: str, window_seconds: int, now: float | None = None) -> int:
        """Count consecutive fire→resolve cycles for an alert rule.

        Called each time the rule resolves. If it last resolved within
        ``window_seconds`` the counter increments (it keeps firing and
        resolving); if it stayed quiet longer than that, the counter resets and
        this counts as a fresh incident.

        Keyed by rule NAME, not alert id — every firing gets a new id.
        """
        key = " ".join((rule or "").split()).lower()
        if not key:
            return 1
        now = time.time() if now is None else now
        with self._lock:
            rec = self.flaps.get(key)
            last = (rec or {}).get("last_resolved") or 0
            if rec and (now - last) <= window_seconds:
                count = int(rec.get("count", 1)) + 1
            else:
                count = 1
            self.flaps[key] = {"count": count, "last_resolved": now}
            return count

    def prune_flaps(self, window_seconds: int, now: float | None = None) -> None:
        """Drop flap counters that have gone quiet (older than 4x the window)."""
        now = time.time() if now is None else now
        cutoff = now - max(window_seconds * 4, 3600)
        with self._lock:
            stale = [k for k, v in self.flaps.items() if (v.get("last_resolved") or 0) < cutoff]
            for k in stale:
                del self.flaps[k]

    def forget(self, alert_id: int | str) -> None:
        """Remove an alert from state entirely."""
        with self._lock:
            self.watched.pop(str(alert_id), None)

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
