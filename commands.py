"""The /check command handler.

Flow (per the requirement):
  1. react to the triggering message with a "processing" emoji,
  2. do the work (read a fresh, live snapshot of alert state and categorise),
  3. remove the "processing" reaction and add a "done" (or "error") reaction,
  4. reply with a card showing two categories: still firing vs resolved.

/check is intentionally READ-ONLY with respect to shared state: the watcher
thread owns every write to `state` (announcements + resolution cards). If /check
also mutated `state` it would race the watcher and could silently suppress a
proactive alert push or a recovery card.
"""
from __future__ import annotations

import logging

import cards
from config import CONFIG
from lark_client import LarkClient
from monitor_client import MonitorClient
from state import State

log = logging.getLogger("alertbot.commands")


class CommandHandler:
    def __init__(self, monitor: MonitorClient, lark: LarkClient, state: State) -> None:
        self.monitor = monitor
        self.lark = lark
        self.state = state

    # ---------------------------------------------------------------- /check
    def handle_check(self, message_id: str, requested_by: str | None = None) -> None:
        processing_id = self.lark.add_reaction(message_id, CONFIG.reaction_processing)
        ok = False
        try:
            firing, resolved = self._collect()
            card = cards.check_summary_card(
                firing,
                resolved,
                requested_by=requested_by,
                severity_label=CONFIG.watch_severity,
            )
            ok = self.lark.reply_card(message_id, card, in_thread=True)
        except Exception:
            log.exception("/check failed")
            try:
                self.lark.reply_text(message_id, "⚠️ /check failed — check the bot logs.", in_thread=True)
            except Exception:
                log.exception("Failed to send /check error reply")

        # Swap reactions. Each call is guarded so one failure can't skip the
        # others or leave the "processing" reaction stuck on the message.
        try:
            if processing_id:
                self.lark.remove_reaction(message_id, processing_id)
        except Exception:
            log.exception("Failed to remove processing reaction")
        try:
            self.lark.add_reaction(message_id, CONFIG.reaction_done if ok else CONFIG.reaction_error)
        except Exception:
            log.exception("Failed to add final reaction")

    def _collect(self) -> tuple[list[dict], list[dict]]:
        """Return (still_firing, resolved) — read-only.

        * still_firing: the live firing set from the dashboard (authoritative).
        * resolved: alerts the watcher has recorded as recovered, within the
          retention window.
        """
        firing = self.monitor.list_all_alerts(
            severity=CONFIG.severity_filter, status="firing", page_size=CONFIG.monitor_page_size
        )
        firing_sorted = sorted(firing, key=lambda a: int(a.get("id", 0)), reverse=True)
        resolved = sorted(
            self.state.resolved(), key=lambda r: r.get("resolved_at") or 0, reverse=True
        )
        return firing_sorted, resolved
