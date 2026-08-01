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
import re
import subprocess

import cards
from config import CONFIG
from lark_client import LarkClient
from monitor_client import MonitorClient
from state import State

log = logging.getLogger("alertbot.commands")

# --- log redaction -----------------------------------------------------------
# Journal lines can contain credentials (the Lark ws URL carries access_key and
# ticket). Never forward those to a chat.
_SECRET_QS_RE = re.compile(
    r"((?:access_key|ticket|token|secret|password|passwd|pwd|authorization|api_key)"
    r"\s*[=:]\s*)([^&\s\"',]+)",
    re.IGNORECASE,
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")


def redact(text: str) -> str:
    """Mask credentials that may appear in log output."""
    if not text:
        return text
    text = _SECRET_QS_RE.sub(lambda m: f"{m.group(1)}***REDACTED***", text)
    text = _JWT_RE.sub("***JWT-REDACTED***", text)
    # Also mask the exact configured secrets, wherever they appear.
    for secret in (CONFIG.lark_app_secret, CONFIG.monitor_password, CONFIG.lark_verification_token):
        if secret and len(secret) >= 6:
            text = text.replace(secret, "***REDACTED***")
    return text


def _build_matcher(pattern: str):
    """Case-insensitive grep-like matcher. Tries regex; falls back to a literal
    substring match if the pattern isn't valid regex."""
    pattern = pattern[:200]
    try:
        rx = re.compile(pattern, re.IGNORECASE)
        return lambda line: rx.search(line) is not None
    except re.error:
        needle = pattern.lower()
        return lambda line: needle in line.lower()


class CommandHandler:
    def __init__(self, monitor: MonitorClient, lark: LarkClient, state: State, kb=None) -> None:
        self.monitor = monitor
        self.lark = lark
        self.state = state
        self.kb = kb  # KnowledgeBase | None

    # ------------------------------------------------------------------- /kb
    def handle_kb(self, message_id: str, text: str, refresher=None,
                  requested_by: str | None = None) -> None:
        """/kb            -> knowledge-base status
        /kb refresh    -> force a rebuild from the wiki doc
        /kb <alert name> -> show what the bot would say for that alert
        """
        parts = text.strip().split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        try:
            if self.kb is None:
                self.lark.reply_text(message_id, "Knowledge base is disabled (KB_ENABLED=false).")
                return

            if arg.lower() in ("refresh", "reload", "update", "sync"):
                if refresher is None:
                    self.lark.reply_text(message_id, "Refresher isn't running (KB_WIKI_TOKEN unset?).")
                    return
                self.lark.reply_text(
                    message_id,
                    "🔄 Refreshing the knowledge base from the wiki doc — this can take a "
                    "while if the doc changed (the model has to re-read it).",
                )
                refresher.trigger()
                return

            if arg:  # treat as an alert name to test
                v = self.kb.lookup({"alert_rule": arg, "summary": arg})
                self.lark.reply_card(message_id, cards.kb_lookup_card(arg, v), in_thread=True)
                return

            self.lark.reply_card(message_id, cards.kb_status_card(self.kb), in_thread=True)
        except Exception as e:  # noqa: BLE001
            log.exception("/kb failed")
            self.lark.reply_text(message_id, f"⚠️ /kb failed: {e}")

    # ---------------------------------------------------------------- /check
    def handle_check(self, message_id: str, requested_by: str | None = None) -> None:
        processing_id = self.lark.add_reaction(message_id, CONFIG.reaction_processing)
        ok = False
        try:
            firing, resolved = self._collect()
            if self.kb is not None and self.kb.entries:
                undoc, doc, idle = self._split_by_sop(firing)
                card = cards.check_sop_card(
                    undoc, doc, idle, resolved,
                    requested_by=requested_by,
                    severity_label=CONFIG.watch_severity,
                )
            else:
                # No knowledge base -> fall back to the plain firing/resolved view.
                card = cards.check_summary_card(
                    firing, resolved,
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

    def _split_by_sop(self, firing: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
        """Partition by SOP coverage:
          1. firing but NOT in the doc, 2. firing and in the doc,
          3. documented alerts that are not firing right now.
        """
        undocumented, documented = [], []
        matched_titles: set[str] = set()
        for alert in firing:
            verdict = self.kb.lookup(alert)
            item = {"alert": alert, "verdict": verdict}
            if verdict.get("in_docs"):
                documented.append(item)
                title = (verdict.get("entry") or {}).get("alert_title")
                if title:
                    matched_titles.add(title)
            else:
                undocumented.append(item)

        idle = [e for e in self.kb.entries if e.get("alert_title") not in matched_titles]
        # Most important first within each firing group.
        rank = {"high": 0, "medium": 1, "low": 2, "unknown": 0}
        documented.sort(key=lambda i: rank.get(str(i["verdict"].get("importance")).lower(), 3))
        idle.sort(key=lambda e: rank.get(str(e.get("importance")).lower(), 3))
        return undocumented, documented, idle

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

    # ------------------------------------------------------------------ /log
    def handle_log(self, message_id: str, lines: int, pattern: str | None,
                   requested_by: str | None = None) -> None:
        """Reply with the tail of `journalctl -u <service>`, optionally filtered.

        The pattern is applied in Python (never passed to a shell), and output is
        redacted before it leaves the server.
        """
        lines = max(1, min(lines, CONFIG.log_max_lines))
        log.info("/log by %s (lines=%d pattern=%r)", requested_by, lines, pattern)
        processing_id = self.lark.add_reaction(message_id, CONFIG.reaction_processing)
        ok = False
        try:
            raw = self._journal(lines if not pattern else CONFIG.log_max_lines)
            out_lines = raw.splitlines()

            if pattern:
                matcher = _build_matcher(pattern)
                out_lines = [ln for ln in out_lines if matcher(ln)]
                out_lines = out_lines[-lines:]  # keep the newest N matches

            header = (
                f"📜 journalctl -u {CONFIG.deploy_service} "
                f"({len(out_lines)} line(s)"
                + (f", filter={pattern!r}" if pattern else "")
                + ")"
            )
            body = "\n".join(out_lines) if out_lines else "(no matching lines)"
            body = redact(body)
            # Keep the message well under Lark's limit; keep the newest content.
            limit = 3000
            if len(body) > limit:
                body = "…(truncated)\n" + body[-limit:]
            self.lark.reply_text(message_id, f"{header}\n```\n{body}\n```", in_thread=True)
            ok = True
        except Exception as e:  # noqa: BLE001
            log.exception("/log failed")
            self.lark.reply_text(message_id, f"⚠️ /log failed: {e}", in_thread=True)

        try:
            if processing_id:
                self.lark.remove_reaction(message_id, processing_id)
        except Exception:
            log.exception("Failed to remove processing reaction")
        try:
            self.lark.add_reaction(message_id, CONFIG.reaction_done if ok else CONFIG.reaction_error)
        except Exception:
            log.exception("Failed to add final reaction")

    def _journal(self, lines: int) -> str:
        r = subprocess.run(
            ["journalctl", "-u", CONFIG.deploy_service, "-n", str(lines),
             "--no-pager", "--output", "short-iso"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[:300] or f"journalctl rc={r.returncode}")
        return r.stdout

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
            # Note: if we're mid-restart our own process may be terminated here;
            # that path is handled inside _restart_service (SIGTERM == success).
            log.exception("restart failed")
            self.lark.reply_text(
                message_id,
                f"⚠️ Restart request failed: {e}\n"
                f"Run manually on the server: systemctl restart {CONFIG.deploy_service}",
            )

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
        """Ask systemd to restart this service.

        Restarting ourselves is inherently racy: `systemctl restart` makes systemd
        SIGTERM the whole service cgroup, which includes the `systemctl` child we
        just spawned. So we:
          1. prefer `systemd-run`, which hands the job to PID 1 in its own
             transient unit — outside our cgroup, so it can't be killed with us;
          2. fall back to plain `systemctl restart --no-block`;
          3. treat "killed by SIGTERM" as SUCCESS — that signal *is* our own
             restart arriving, not a failure.
        """
        cmds = [
            ["systemd-run", "--collect", "--no-block", "--unit",
             f"alertbot-redeploy-{CONFIG.deploy_service}",
             "systemctl", "restart", CONFIG.deploy_service],
            ["systemctl", "restart", "--no-block", CONFIG.deploy_service],
        ]
        last_err = None
        for cmd in cmds:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            except FileNotFoundError as e:
                last_err = e
                continue  # systemd-run not present; try the fallback
            except subprocess.TimeoutExpired as e:
                last_err = e
                continue
            if r.returncode == 0:
                return
            # Negative return code == killed by a signal. -15 (SIGTERM) means our
            # own restart already reached us, i.e. the request succeeded.
            if r.returncode == -15:
                log.info("systemctl was SIGTERMed by our own restart — treating as success")
                return
            last_err = RuntimeError(
                f"{' '.join(cmd)} -> rc={r.returncode} {(r.stdout + r.stderr).strip()[:300]}"
            )
        if last_err:
            raise last_err if isinstance(last_err, Exception) else RuntimeError(str(last_err))
