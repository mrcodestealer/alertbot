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
import subprocess

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

    # --------------------------------------------------------------- /deploy
    def handle_deploy(self, message_id: str, requested_by: str | None = None) -> None:
        """Run `git pull origin <branch>` and, if it succeeds, restart the
        service. Triggered only from a DM by an authorized user (gated in main).

        NOTE: this runs a FIXED command set — it never executes text from the
        message. Reactions are swapped BEFORE the restart because restarting
        kills this very process.
        """
        log.warning("DEPLOY requested by %s (dir=%s branch=%s service=%s)",
                    requested_by, CONFIG.deploy_git_dir, CONFIG.deploy_branch, CONFIG.deploy_service)
        processing_id = self.lark.add_reaction(message_id, CONFIG.reaction_processing)
        self.lark.reply_text(
            message_id,
            f"🔄 git pull origin {CONFIG.deploy_branch} in {CONFIG.deploy_git_dir} …",
        )

        code, out = -1, ""
        try:
            code, out = self._git_pull()
        except Exception as e:  # noqa: BLE001
            log.exception("git pull failed")
            out = f"git pull raised: {e}"
        self.lark.reply_text(message_id, f"git pull exit={code}\n{out[:1500]}")
        ok = code == 0

        # Swap reactions now — the restart below will terminate this process.
        try:
            if processing_id:
                self.lark.remove_reaction(message_id, processing_id)
        except Exception:
            log.exception("Failed to remove processing reaction")
        try:
            self.lark.add_reaction(message_id, CONFIG.reaction_done if ok else CONFIG.reaction_error)
        except Exception:
            log.exception("Failed to add final reaction")

        if not ok:
            self.lark.reply_text(message_id, "❌ git pull failed — skipping restart.")
            return

        self.lark.reply_text(
            message_id,
            f"♻️ Restarting `{CONFIG.deploy_service}` — I'll reconnect in a few seconds.",
        )
        try:
            self._restart_service()
        except Exception as e:  # noqa: BLE001
            log.exception("restart failed")
            self.lark.reply_text(message_id, f"⚠️ Restart command failed: {e}")

    def _git_pull(self) -> tuple[int, str]:
        r = subprocess.run(
            ["git", "-C", CONFIG.deploy_git_dir, "pull", "origin", CONFIG.deploy_branch],
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = (r.stdout + r.stderr).strip() or "(no output)"
        return r.returncode, out

    def _restart_service(self) -> None:
        # `--no-block` enqueues the restart in systemd (PID 1) and returns, so the
        # job survives this process being killed during its own restart.
        subprocess.run(
            ["systemctl", "restart", "--no-block", CONFIG.deploy_service],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
