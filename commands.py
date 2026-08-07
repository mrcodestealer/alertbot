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
    # A doc entry scoring at least this against a firing alert (but below the
    # match threshold) is flagged in /check as a probable name mismatch.
    NEAR_MISS_MIN = 0.30

    def __init__(self, monitor: MonitorClient, lark: LarkClient, state: State, kb=None) -> None:
        self.monitor = monitor
        self.lark = lark
        self.state = state
        self.kb = kb  # KnowledgeBase | None

    # --------------------------------------------------- "Report to SRE" button
    def handle_report(self, value: dict, reporter: str | None = None) -> None:
        """Card-button action: post the alert to the SRE chat, tagging the duty
        person for the alert's Domain."""
        import duty as duty_mod  # local import: pulls in the copied dutybot modules

        alert_id = str(value.get("alert_id") or "")
        domain = value.get("domain") or ""
        rule = value.get("rule") or f"Alert #{alert_id}"
        image_key = value.get("image_key") or None

        chat = CONFIG.report_chat_id or CONFIG.lark_alert_chat_id
        if not chat:
            log.error("Report requested but no REPORT_CHAT_ID / LARK_ALERT_CHAT_ID configured")
            return

        # Pull the alert once: used for the "already firing N minutes" line and
        # for the tracker record.
        detail: dict = {}
        try:
            if self.monitor is not None:
                detail = self.monitor.get_alert(alert_id) or {}
        except Exception:
            log.warning("Report: could not fetch #%s detail", alert_id)

        firing_minutes = None
        created = cards._parse_dt(detail.get("created_at")) if detail else None
        if created is not None:
            from datetime import datetime, timezone  # noqa: PLC0415

            firing_minutes = int((datetime.now(timezone.utc) - created).total_seconds() // 60)
            if firing_minutes < 1:
                firing_minutes = None

        # Route by alert content first (e.g. anything LiveSlots pages LiveSlot
        # duty), falling back to the Domain.
        content = duty_mod.alert_content(detail) or rule
        info = duty_mod.get_duty(domain, content)
        log.info(
            "Report alert #%s domain=%s firing=%smin -> chat=%s team=%s names=%s error=%s by=%s",
            alert_id, domain, firing_minutes, chat, info["label"], info["names"],
            info.get("error"), reporter,
        )
        if info.get("error"):
            log.error("Duty lookup failed for report of #%s: %s", alert_id, info["error"])
        duty_mention = duty_mod.mention(info["names"])
        card = cards.report_card(
            rule=rule,
            alert_id=alert_id,
            domain=domain,
            duty_label=info["label"],
            duty_mention=duty_mention,
            image_key=image_key,
            duty_error=info.get("error"),
            reported_by=reporter,
            firing_minutes=firing_minutes,
        )
        msg_id = self.lark.send_card(chat, card)
        if msg_id:
            log.info("Report card for #%s posted to %s (message_id=%s)", alert_id, chat, msg_id)
            # Remember it so the watcher can reply here when the alert recovers.
            if self.state is not None:
                self.state.set_report(alert_id, msg_id, duty_mention, rule)
                self.state.save()
            self._file_in_tracker(alert_id, domain, rule, info, detail)
        else:
            log.error(
                "FAILED to post report card for #%s to chat %s — is the bot a member of that chat?",
                alert_id, chat,
            )

    def _file_in_tracker(self, alert_id: str, domain: str, rule: str, duty_info: dict,
                         detail: dict | None = None) -> None:
        """Also file the reported alert in the Lark Base alerts tracker.

        Best-effort: the report card has already been posted, so a tracker
        failure must never surface as a failed report.
        """
        if not CONFIG.tracker_enabled:
            return
        try:
            import duty as duty_mod  # noqa: PLC0415
            from tracker import AlertsTracker  # noqa: PLC0415

            # Full detail (description, created_at) comes from the dashboard;
            # the button payload only carries the essentials.
            alert = {**(detail or {}), "domain": domain} if detail else {
                "id": alert_id, "alert_rule": rule, "domain": domain
            }

            mapping = duty_mod.load_openids()
            open_ids = [
                oid for oid in (duty_mod.resolve_openid(n, mapping) for n in duty_info.get("names") or [])
                if oid
            ]
            shot = CONFIG.screenshot_dir / f"alert_{alert_id}.png"
            rid = AlertsTracker().add_alert(
                alert,
                duty_open_ids=open_ids,
                screenshot_path=str(shot) if shot.exists() else None,
            )
            if rid:
                log.info("Tracker: filed #%s as record %s", alert_id, rid)
        except Exception:
            log.exception("Tracker: failed to file #%s (report card was still sent)", alert_id)

    # ----------------------------------------------------------------- /duty
    def handle_duty(self, message_id: str, arg: str = "") -> None:
        """Show today's duty per team and whether each name has an open_id.

        Shows the raw open_id rather than an @-mention on purpose — checking the
        roster shouldn't ping the whole duty team.
        """
        import duty as duty_mod  # local import

        # "/duty all" -> check the WHOLE roster, not just who is on duty today.
        if arg.strip().lower() in ("all", "roster", "check"):
            cov = duty_mod.roster_coverage()
            labels = {"sre_backend": "SRE Backend Team", "db": "DB Team", "liveslot": "LiveSlot Team"}
            blocks = []
            for team, rows in cov["teams"].items():
                if not rows:
                    continue
                lines = [f"【{labels.get(team, team)}】"]
                for r in rows:
                    lines.append(
                        f"  ✅ {r['name']}" if r["open_id"] else f"  ❌ {r['name']} — missing open_id"
                    )
                blocks.append("\n".join(lines))
            missing = cov["missing"]
            tail = (
                "\n\n❌ Missing (" + str(len(missing)) + "): " + ", ".join(missing)
                + "\nRun: /secret1 " + " ".join(f"@{m}" for m in missing[:6])
                if missing
                else "\n\n✅ Everyone on the roster has an open_id."
            )
            self.lark.reply_text(
                message_id,
                f"Duty roster coverage ({cov['saved']} open_id(s) saved):\n"
                + "\n\n".join(blocks) + tail,
            )
            return

        domains = [arg.strip().upper()] if arg.strip() else ["PLATFORM", "DB", "LIVESLOTS"]
        blocks: list[str] = []
        missing = False
        for dom in domains:
            info = duty_mod.duty_status(dom)  # one sheet read per domain
            head = f"【{dom}】→ {info['label']}"
            if info.get("error"):
                blocks.append(f"{head}\n  ⚠️ {info['error']}")
                continue
            if not info.get("resolved"):
                blocks.append(f"{head}\n  (nobody on duty / could not parse names)")
                continue
            lines = [head]
            for r in info["resolved"]:
                if r["open_id"]:
                    lines.append(f"  ✅ {r['name']} → {r['open_id']}")
                else:
                    missing = True
                    lines.append(f"  ⚠️ {r['name']} → no open_id (will show as plain text)")
            blocks.append("\n".join(lines))
        tail = (
            "\n\nSome names have no open_id — run: /secret1 @thatperson"
            if missing
            else "\n\nAll duty names resolve to an open_id ✅"
        )
        self.lark.reply_text(message_id, "Duty today:\n" + "\n\n".join(blocks) + tail)

    # -------------------------------------------------------------- /secret1
    def handle_secret1(self, message_id: str, mentions: list) -> None:
        """Reply with the open_id of every @-mentioned person and remember them,
        so duty names can be @-tagged in report cards later."""
        import duty as duty_mod  # local import

        people: list[tuple[str, str]] = []
        for m in mentions or []:
            name = getattr(m, "name", None) or ""
            oid = getattr(getattr(m, "id", None), "open_id", None) or ""
            if oid:
                people.append((name, oid))

        if not people:
            self.lark.reply_text(
                message_id,
                "Usage: /secret1 @person [@person2 …] — I'll reply with their open_id.",
            )
            return

        duty_mod.remember_openids({n: o for n, o in people if n})
        lines = [f"{n or '(unknown)'} → {o}" for n, o in people]
        self.lark.reply_text(
            message_id,
            "open_id(s):\n" + "\n".join(lines) + "\n\n(saved — these will be used to @-tag duty)",
        )

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
            if self.kb is not None and self.kb.entries:
                alerts = self._collect_all()
                undoc, doc, idle = self._split_by_sop(alerts)
                card = cards.check_sop_card(
                    undoc, doc, idle, None,
                    requested_by=requested_by,
                    severity_label=CONFIG.watch_severity,
                    scanned=len(alerts),
                )
            else:
                firing, resolved = self._collect()
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
        # Group by alert NAME: /check is about which names are documented, so
        # several instances of the same rule collapse into one line with a count.
        undocumented, documented = [], []
        matched_titles: set[str] = set()
        seen: dict[str, dict] = {}
        for alert in firing:
            is_firing = str(alert.get("status", "")).lower() == "firing"
            name = (alert.get("alert_rule") or alert.get("summary") or "").strip().lower()
            if name and name in seen:
                item = seen[name]
                item["count"] += 1
                if is_firing:
                    item["firing"] += 1
                    item["alert"] = alert  # prefer a firing example
                continue
            verdict = self.kb.lookup(alert)
            item = {"alert": alert, "verdict": verdict, "count": 1, "firing": 1 if is_firing else 0}
            if name:
                seen[name] = item
            if verdict.get("in_docs"):
                documented.append(item)
                title = (verdict.get("entry") or {}).get("alert_title")
                if title:
                    matched_titles.add(title)
            else:
                undocumented.append(item)

        # Names recorded on earlier runs but absent from this scan window still
        # count for documentation coverage — that's the point of the catalogue.
        if self.state is not None:
            for key, rec in (self.state.seen_rules or {}).items():
                if key in seen:
                    continue
                pseudo = {
                    "alert_rule": rec.get("name") or key,
                    "severity": rec.get("severity") or "CRITICAL",
                    "domain": rec.get("domain") or "",
                    "summary": "", "instance": "", "description": "",
                    "status": "resolved",
                }
                verdict = self.kb.lookup(pseudo)
                item = {"alert": pseudo, "verdict": verdict,
                        "count": int(rec.get("count") or 1), "firing": 0, "historic": True}
                seen[key] = item
                if verdict.get("in_docs"):
                    documented.append(item)
                    title = (verdict.get("entry") or {}).get("alert_title")
                    if title:
                        matched_titles.add(title)
                else:
                    undocumented.append(item)

        # Category 3: documented alerts with no matching firing alert. Annotate
        # any that ALMOST matched something currently firing — that's the whole
        # point of this section: catching a name mismatch between the doc and
        # the dashboard.
        from knowledge import match_score, shares_distinctive_token  # noqa: PLC0415

        idle = []
        for e in self.kb.entries:
            if e.get("alert_title") in matched_titles:
                continue
            e = dict(e)  # don't mutate the stored KB
            best_s, best_a = 0.0, None
            for item in undocumented:
                # Require a meaningful shared word, otherwise vendor boilerplate
                # ("aliyun", "ack") flags every entry as a mismatch.
                if not shares_distinctive_token(item["alert"], e):
                    continue
                s = match_score(item["alert"], e)
                if s > best_s:
                    best_s, best_a = s, item["alert"]
            if best_a is not None and self.NEAR_MISS_MIN <= best_s < self.kb.MATCH_THRESHOLD:
                e["_near"] = {
                    "score": round(best_s, 2),
                    "id": best_a.get("id"),
                    "rule": best_a.get("alert_rule") or best_a.get("summary") or "",
                }
            idle.append(e)

        rank = {"high": 0, "medium": 1, "low": 2, "unknown": 0}
        # Currently-firing names first, then the most frequent.
        for lst in (undocumented, documented):
            lst.sort(key=lambda i: (0 if i.get("firing") else 1, -i.get("count", 0)))
        documented.sort(key=lambda i: (0 if i.get("firing") else 1,
                                       rank.get(str(i["verdict"].get("importance")).lower(), 3)))
        # Suspected mismatches first — they need action; the rest are just quiet.
        idle.sort(key=lambda e: (0 if e.get("_near") else 1,
                                 rank.get(str(e.get("importance")).lower(), 3)))
        return undocumented, documented, idle

    def _collect_all(self) -> list[dict]:
        """Recent alerts of the watched severity, firing AND resolved.

        /check answers "which alert names are documented", and a name matters
        whether or not it happens to be firing at this instant — so scan the
        recent history rather than only the live firing set.
        """
        alerts = self.monitor.list_all_alerts(
            severity=CONFIG.severity_filter,
            page_size=CONFIG.monitor_page_size,
            max_pages=CONFIG.check_lookback_pages,
        )
        # Add every name to the lasting catalogue so coverage isn't limited to
        # what happens to be in this scan window.
        if self.state is not None:
            new = self.state.record_rules(alerts)
            self.state.prune_seen_rules()
            self.state.save()
            if new:
                log.info("/check recorded %d new alert name(s)", new)
        return alerts

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
