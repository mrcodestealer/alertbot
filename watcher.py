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
from datetime import datetime, timezone

import cards
from config import CONFIG
from lark_client import LarkClient
from monitor_client import MonitorClient
from screenshot import capture_alert_detail
from state import State

log = logging.getLogger("alertbot.watcher")


def _parse_dt(value) -> datetime | None:
    """Parse an ISO-8601 timestamp (e.g. '2026-07-18T02:02:50.955347+08:00')
    into an aware UTC datetime; None if it can't be parsed."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class Watcher(threading.Thread):
    def __init__(self, monitor: MonitorClient, lark: LarkClient, state: State, kb=None) -> None:
        super().__init__(name="alert-watcher", daemon=True)
        self.monitor = monitor
        self.lark = lark
        self.state = state
        self.kb = kb  # KnowledgeBase | None
        self._stop = threading.Event()
        # Run the first deep catalogue scan shortly after startup.
        self._last_catalogue = 0.0

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
        # Catalogue the names we see, so /check knows about them even if they
        # later drop out of its scan window.
        self.state.record_rules(firing)
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
            delivered, msg_id, image_key = self._announce_new(alert)
            if delivered:
                self.state.track(alert, announced=True)
                if msg_id:
                    self.state.set_firing_message_id(alert["id"], msg_id)
                if image_key:
                    # Kept so the "still firing" update can re-render the same
                    # card without re-capturing the screenshot.
                    self.state.set_image_key(alert["id"], image_key)
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

        # 2b) FYI reminders for alerts still firing past the configured thresholds
        self._check_firing_reminders()

        # 2c) periodically catalogue alert names from deep history
        self._refresh_catalogue()

        # 3) prune + persist
        self.state.prune(CONFIG.state_retention_hours)
        self.state.save()

    # ------------------------------------------------------------- announce
    def _announce_new(self, alert: dict) -> tuple[bool, str | None, str | None]:
        """Announce a new alert. Returns (delivered, message_id, image_key).
        The image key is returned rather than stored here because the alert is
        only added to state after this call."""
        aid = alert.get("id")
        log.info("New alert #%s: %s", aid, alert.get("alert_rule"))
        if not CONFIG.lark_alert_chat_id:
            return True, None, None
        try:
            # Local SOP lookup — no LLM call here, so this stays fast.
            verdict = None
            if self.kb is not None:
                try:
                    verdict = self.kb.lookup(alert)
                    log.info(
                        "SOP for #%s: in_docs=%s importance=%s score=%s",
                        aid, verdict.get("in_docs"), verdict.get("importance"), verdict.get("score"),
                    )
                except Exception:
                    log.exception("KB lookup failed for #%s", aid)

            image_key = None
            shot = capture_alert_detail(aid)
            if shot:
                image_key = self.lark.upload_image(shot)
            card = cards.new_alert_card(
                alert, image_key=image_key, kb_verdict=verdict,
                button_text=self._button_text(alert),
            )
            msg_id = self.lark.send_card(CONFIG.lark_alert_chat_id, card)
            return (msg_id is not None), msg_id, image_key
        except Exception:
            log.exception("Failed to announce alert #%s", aid)
            return False, None, None

    def _button_text(self, alert: dict) -> str:
        """Report-button label naming the group the alert will be sent to."""
        try:
            import duty as duty_mod  # noqa: PLC0415

            team = duty_mod.team_for_alert(alert.get("domain"), duty_mod.alert_content(alert))
            return duty_mod.report_button_text(team)
        except Exception:
            log.debug("Could not pick a report button label", exc_info=True)
            return CONFIG.report_button_text

    # -------------------------------------------------------- name catalogue
    def _refresh_catalogue(self) -> None:
        """Deep-scan alert history to catalogue every alert NAME.

        Runs here rather than inside /check because 60 pages x 200 rows takes
        ~2 minutes — fine in the background, far too slow for a chat command.
        /check then reports coverage from the catalogue instantly.
        """
        if CONFIG.catalogue_pages <= 0:
            return
        due = self._last_catalogue + CONFIG.catalogue_refresh_minutes * 60
        if time.time() < due:
            return
        self._last_catalogue = time.time()  # set first: a failure shouldn't retry every tick
        try:
            started = time.time()
            alerts = self.monitor.list_all_alerts(
                severity=CONFIG.severity_filter,
                page_size=CONFIG.monitor_page_size,
                max_pages=CONFIG.catalogue_pages,
            )
            new = self.state.record_rules(alerts)
            self.state.prune_seen_rules()
            self.state.save()
            log.info(
                "Catalogue scan: %d alerts over %d page(s) in %.0fs — %d new name(s), %d known",
                len(alerts), CONFIG.catalogue_pages, time.time() - started, new,
                len(self.state.seen_rules),
            )
        except Exception:
            log.exception("Catalogue scan failed")

    # ------------------------------------------------------ firing reminders
    def _check_firing_reminders(self) -> None:
        """Re-post an alert every FIRING_REPEAT_MINUTES while it stays firing.

        A NEW message is sent (rather than updating the old card) so the chat
        actually notifies people again; the card is titled "(Firing 30 minutes)"
        so it's clear how long it's been going. Earlier cards are left as they
        are — the newest one is the live view.
        """
        interval = CONFIG.firing_repeat_minutes
        if interval <= 0 or not CONFIG.lark_alert_chat_id:
            return
        now = datetime.now(timezone.utc)
        for rec in self.state.firing():
            if not rec.get("firing_message_id"):
                continue  # never announced
            created = _parse_dt(rec.get("created_at"))
            if created is None:
                continue
            age_min = (now - created).total_seconds() / 60.0
            milestone = int(age_min // interval) * interval
            if milestone < interval:
                continue
            already = set(rec.get("reminded", []))
            if milestone in already:
                continue
            aid = rec.get("id")
            # Only the newest milestone: after downtime, don't replay 15/30/45.
            log.info("Alert #%s still firing at %d min — re-posting", aid, milestone)

            verdict = None
            if self.kb is not None:
                try:
                    verdict = self.kb.lookup(rec)
                except Exception:
                    log.exception("KB lookup failed for #%s", aid)
            card = cards.new_alert_card(
                rec,
                image_key=rec.get("image_key"),
                kb_verdict=verdict,
                firing_minutes=milestone,
                button_text=self._button_text(rec),
            )
            new_id = self.lark.send_card(CONFIG.lark_alert_chat_id, card)
            if new_id:
                # Track it so it can be collapsed when the alert resolves, and
                # mark every milestone up to now as done.
                self.state.add_message_id(aid, new_id)
                for m in range(interval, milestone + 1, interval):
                    self.state.add_reminded(aid, m)
                self.state.save()  # persist so a crash can't re-post it
            else:
                log.warning("Could not re-post alert #%s at %d min", aid, milestone)

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
        firing_msg_id = self.state.get_firing_message_id(alert_id)
        posted_ids = self.state.get_message_ids(alert_id)
        self.state.mark_resolved(alert_id, detail)

        # If someone pressed "Report to SRE" for this alert, reply to that very
        # message in the SRE group so the people who were tagged get the update.
        report = self.state.pop_report(alert_id)
        if report and report.get("message_id"):
            rule = report.get("rule") or summary.get("alert_rule") or ""
            try:
                if self.lark.reply_card(
                    report["message_id"],
                    cards.report_resolved_card(rule, report.get("mention") or "team"),
                    in_thread=False,
                ):
                    log.info("Replied 'resolved' to the SRE report for #%s", alert_id)
                else:
                    log.warning("Could not reply to the SRE report for #%s", alert_id)
            except Exception:
                log.exception("Failed replying to the SRE report for #%s", alert_id)
        self.state.prune_reports()

        action = CONFIG.resolve_action

        summary = dict(detail)
        # How many times this rule has fired AND resolved in quick succession.
        # Counted per rule name, because each firing gets a new alert id; the
        # counter resets once the rule stays quiet past the window.
        rule = summary.get("alert_rule") or summary.get("summary") or ""
        window = CONFIG.flap_window_minutes * 60
        summary["flap_count"] = self.state.record_flap(rule, window)
        self.state.prune_flaps(window)

        # "collapse": rewrite the cards in place. Leaves no "recalled" notice.
        if action == "collapse" and posted_ids:
            # The alert may have been re-posted every N minutes while firing.
            # The newest card carries the full resolved summary; the earlier
            # ones shrink to a marker so they stop saying "still firing".
            latest = posted_ids[-1]
            small = cards.collapsed_card(summary)
            if self.lark.patch_card(latest, small):
                marker = cards.collapsed_reminder_card()
                for mid in posted_ids:
                    if mid != latest:
                        self.lark.patch_card(mid, marker)
                log.info("Alert #%s resolved — card collapsed in place", alert_id)
                if CONFIG.clear_resolved:
                    self.state.forget(alert_id)
                return
            log.warning("Could not collapse card for #%s — falling back to a resolve card", alert_id)

        # "delete": recall the messages (Lark shows a 'recalled' tombstone).
        if action == "delete" and posted_ids:
            removed = 0
            # Newest first so threaded replies go before their parent.
            for mid in reversed(posted_ids):
                if self.lark.delete_message(mid):
                    removed += 1
            log.info("Alert #%s resolved — removed %d/%d message(s)", alert_id, removed, len(posted_ids))
            if removed:
                if CONFIG.clear_resolved:
                    self.state.forget(alert_id)
                return  # nothing left to thread a resolve card under
            log.warning(
                "Could not remove any message for #%s (too old to recall?) — "
                "falling back to a resolve card", alert_id,
            )

        if CONFIG.notify_on_resolve and CONFIG.lark_alert_chat_id:
            card = cards.resolve_card(detail)
            sent = False
            if firing_msg_id:
                # Thread the recovery card under the original firing message.
                sent = self.lark.reply_card(firing_msg_id, card, in_thread=True)
                if not sent:
                    log.warning("Threaded resolve reply failed for #%s; sending standalone", alert_id)
            if not sent:
                self.lark.send_card(CONFIG.lark_alert_chat_id, card)

        # Once resolved (and its card is posted), optionally drop it from state.
        if CONFIG.clear_resolved:
            self.state.forget(alert_id)
