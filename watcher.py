"""Background polling loop.

Every POLL_INTERVAL_SECONDS it:
  1. fetches currently-firing alerts (filtered by WATCH_SEVERITY),
  2. announces any brand-new ones (id > last_id) to the Lark alert chat,
     attaching a screenshot of the detail modal,
  3. detects alerts that were firing and have now resolved, and (optionally)
     posts a recovery card,
  4. prunes old resolved entries and persists state.
"""
from __future__ import annotations

import logging
import threading
import time

import cards
from config import CONFIG
from lark_client import LarkClient
from monitor_client import MonitorClient
from screenshot import capture_alert_detail
from state import State

log = logging.getLogger("alertbot.watcher")


class Watcher(threading.Thread):
    def __init__(self, monitor: MonitorClient, lark: LarkClient, state: State) -> None:
        super().__init__(name="alert-watcher", daemon=True)
        self.monitor = monitor
        self.lark = lark
        self.state = state
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info(
            "Watcher started: severity=%s interval=%ss chat=%s",
            CONFIG.watch_severity,
            CONFIG.poll_interval_seconds,
            CONFIG.lark_alert_chat_id or "(none — proactive push disabled)",
        )
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("Watcher tick failed")
            self._stop.wait(CONFIG.poll_interval_seconds)

    # ------------------------------------------------------------------ tick
    def _tick(self) -> None:
        firing = self.monitor.list_all_alerts(
            severity=CONFIG.severity_filter, status="firing", page_size=CONFIG.monitor_page_size
        )
        firing_ids = {int(a["id"]) for a in firing if a.get("id") is not None}
        max_id = max(firing_ids) if firing_ids else self.state.last_id

        # First run: seed the baseline so we don't replay the whole backlog.
        if not self.state.seeded and not CONFIG.announce_backlog_on_start:
            for a in firing:
                self.state.track(a, announced=False)
            self.state.last_id = max_id
            self.state.seeded = True
            self.state.save()
            log.info("Seeded baseline with %d firing alert(s); future alerts will be announced", len(firing))
            return
        self.state.seeded = True

        # 1) announce new firing alerts (oldest id first). Only advance last_id
        #    past alerts we actually delivered, so an undelivered alert is retried
        #    on the next tick instead of being lost.
        new_alerts = sorted(
            (a for a in firing if int(a["id"]) > self.state.last_id and not self.state.is_tracked(a["id"])),
            key=lambda a: int(a["id"]),
        )
        first_failure_id: int | None = None
        for alert in new_alerts:
            if self._announce_new(alert):
                self.state.track(alert, announced=True)
                self.state.save()  # persist incrementally: a restart can't re-announce
            elif first_failure_id is None:
                first_failure_id = int(alert["id"])

        if first_failure_id is None:
            self.state.last_id = max(self.state.last_id, max_id)
        else:
            # Hold last_id below the first undelivered alert so it retries; any
            # already-delivered higher-id alerts stay deduped via is_tracked().
            self.state.last_id = max(self.state.last_id, first_failure_id - 1)

        # keep statuses of already-tracked firing alerts fresh
        for a in firing:
            if self.state.is_tracked(a["id"]):
                self.state.track(a, announced=True)

        # 2) detect resolutions: tracked+firing but no longer in the firing list
        for rec in list(self.state.firing()):
            aid = rec.get("id")
            if aid is None or int(aid) in firing_ids:
                continue
            try:
                self._handle_possible_resolution(aid)
            except Exception:
                log.exception("Resolution check failed for #%s", aid)

        # 3) prune + persist
        self.state.prune(CONFIG.state_retention_hours)
        self.state.save()

    # ------------------------------------------------------------- announce
    def _announce_new(self, alert: dict) -> bool:
        """Announce a new alert. Returns True when it was delivered (or when no
        alert chat is configured, so there is nothing to deliver); False on a
        delivery failure so the caller leaves it untracked for a retry."""
        aid = alert.get("id")
        log.info("New alert #%s: %s", aid, alert.get("alert_rule"))
        if not CONFIG.lark_alert_chat_id:
            return True
        try:
            image_key = None
            shot = capture_alert_detail(aid)
            if shot:
                image_key = self.lark.upload_image(shot)
            card = cards.new_alert_card(alert, image_key=image_key)
            return self.lark.send_card(CONFIG.lark_alert_chat_id, card)
        except Exception:
            log.exception("Failed to announce alert #%s", aid)
            return False

    def _handle_possible_resolution(self, alert_id) -> None:
        try:
            detail = self.monitor.get_alert(alert_id)
        except Exception:
            log.exception("Could not fetch alert %s to confirm resolution", alert_id)
            return
        if detail.get("status") == "firing":
            # still firing (dropped off the page for another reason); refresh record
            self.state.track(detail, announced=True)
            return
        log.info("Alert #%s resolved", alert_id)
        self.state.mark_resolved(alert_id, detail)
        if CONFIG.notify_on_resolve and CONFIG.lark_alert_chat_id:
            self.lark.send_card(CONFIG.lark_alert_chat_id, cards.resolve_card(detail))
