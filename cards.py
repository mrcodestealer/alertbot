"""Lark interactive card (Card JSON 1.0) builders.

Every function returns a ``dict``; callers ``json.dumps`` it into the message
``content`` field.
"""
from __future__ import annotations

from typing import Any

from config import CONFIG

# Map dashboard severity (mixed case) -> Lark header template colour.
_SEVERITY_COLOR = {
    "critical": "red",
    "warning": "orange",
    "info": "blue",
}
_SEVERITY_EMOJI = {
    "critical": "🔴",
    "warning": "🟠",
    "info": "🔵",
}

_MAX_DESC = 900  # keep cards readable / under Lark limits


def _sev_key(alert: dict[str, Any]) -> str:
    return str(alert.get("severity", "")).strip().lower()


def _color(alert: dict[str, Any]) -> str:
    return _SEVERITY_COLOR.get(_sev_key(alert), "grey")


def _emoji(alert: dict[str, Any]) -> str:
    return _SEVERITY_EMOJI.get(_sev_key(alert), "⚪")


def _clip(text: str | None, limit: int = _MAX_DESC) -> str:
    if not text:
        return "-"
    text = str(text)
    return text if len(text) <= limit else text[:limit] + " …(truncated)"


def _field(label: str, value: Any, short: bool = True) -> dict[str, Any]:
    return {
        "is_short": short,
        "text": {"tag": "lark_md", "content": f"**{label}**\n{value if value not in (None, '') else '-'}"},
    }


def _divider() -> dict[str, Any]:
    return {"tag": "hr"}


_IMPORTANCE_BADGE = {
    "high": "🚨 HIGH",
    "medium": "⚠️ MEDIUM",
    "low": "🔽 LOW",
    "unknown": "❓ UNKNOWN",
}


def sop_elements(verdict: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Build the 'what do I do' section from a knowledge-base verdict."""
    if not verdict:
        return []
    hours = "working hours / 工作时间" if verdict.get("working_hours") else "non-working hours / 非工作时间"

    if not verdict.get("in_docs"):
        content = (
            "**📕 SOP**\n"
            "⚠️ **Not found in the SOP doc — treat as IMPORTANT.**\n"
            "Please check with SRE, then add it to the wiki so next time it's known."
        )
        # No alert-specific entry, so fall back to the doc's general rules.
        rules = verdict.get("global_rules") or []
        if rules:
            content += "\n\n**General rules for this group:**\n" + "\n".join(
                f"- {_clip(r, 200)}" for r in rules
            )
        return [_divider(), {"tag": "div", "text": {"tag": "lark_md", "content": content}}]

    entry = verdict.get("entry") or {}
    badge = _IMPORTANCE_BADGE.get(str(verdict.get("importance", "medium")).lower(), "⚠️ MEDIUM")
    parts = [f"**📕 SOP** · Importance: **{badge}** · matched *{_clip(entry.get('alert_title'), 80)}*"]
    if entry.get("summary"):
        parts.append(f"_{_clip(entry.get('summary'), 250)}_")
    action = verdict.get("action")
    if action:
        parts.append(f"**▶ Now ({hours}):**\n{_clip(action, 350)}")
    if entry.get("escalation"):
        parts.append(f"**📞 Escalation:** {_clip(entry.get('escalation'), 250)}")
    if entry.get("ignore_conditions"):
        parts.append("**🔕 Can ignore when:** " + "; ".join(entry["ignore_conditions"][:3]))
    # Resolve the SOP's generic "if namespace is X" clauses against this alert.
    cond = verdict.get("condition_notes") or []
    if cond:
        parts.append("**🎯 This alert:**\n" + "\n".join(cond[:4]))
    notes = entry.get("notes") or []
    if notes:
        parts.append("**📝 Notes:** " + "; ".join(_clip(n, 150) for n in notes[:3]))

    return [_divider(), {"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(parts)}}]


def new_alert_card(
    alert: dict[str, Any],
    image_key: str | None = None,
    kb_verdict: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Card for a newly-detected firing alert."""
    title = alert.get("alert_rule") or alert.get("summary") or f"Alert #{alert.get('id')}"
    # Compact card: ID + screenshot + SOP. The alert's instance/description are
    # still parsed by the knowledge base (matching + condition checks) — they're
    # just not rendered, because the screenshot already shows them.
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"{_emoji(alert)} **{alert.get('severity', '-')}** · Alert ID `{alert.get('id', '-')}`",
            },
        },
    ]

    if image_key:
        elements.append(
            {
                "tag": "img",
                "img_key": image_key,
                "alt": {"tag": "plain_text", "content": "alert detail screenshot"},
                "mode": "fit_horizontal",
            }
        )
    else:
        # No screenshot: without it there'd be nothing identifying WHICH instance
        # fired, so fall back to the one-line instance.
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**实例 / Instance**\n{_clip(alert.get('instance'), 300)}"}}
        )

    elements.extend(sop_elements(kb_verdict))

    # "Report to SRE" button -> card.action.trigger -> posts to the SRE chat.
    elements.append(_divider())
    elements.append(
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": CONFIG.report_button_text},
                    "type": "primary",
                    "value": {
                        "action": "report_sre",
                        "alert_id": str(alert.get("id", "")),
                        "domain": alert.get("domain") or "",
                        "rule": _clip(alert.get("alert_rule") or alert.get("summary"), 120),
                        "image_key": image_key or "",
                    },
                }
            ],
        }
    )

    elements.append(_divider())
    elements.append(
        {
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": "MonitorFlow · AlertBot 🔔 new alert / 新告警"}],
        }
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🔥 {title}"},
            "template": _color(alert),
        },
        "elements": elements,
    }


def report_card(
    *,
    rule: str,
    alert_id: str,
    domain: str,
    duty_label: str,
    duty_mention: str,
    image_key: str | None = None,
    duty_error: str | None = None,
    reported_by: str | None = None,
) -> dict[str, Any]:
    """Card posted to the SRE chat when someone presses 'Report to SRE'."""
    # Deliberately minimal: the alert name is the card title, then the screenshot,
    # then the ask. No metadata line, no footer.
    elements: list[dict[str, Any]] = []

    if image_key:
        elements.append(
            {
                "tag": "img",
                "img_key": image_key,
                "alt": {"tag": "plain_text", "content": "alert detail screenshot"},
                "mode": "fit_horizontal",
            }
        )
    else:
        # Without a screenshot there'd be nothing identifying the alert beyond the
        # title, so keep a single ID line in that failure case only.
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"Alert ID `{alert_id}` · Domain `{domain or '-'}`"},
            }
        )

    if duty_error:
        greeting = (
            f"⚠️ **Could not reach the {duty_label} roster** — {_clip(duty_error, 160)}\n"
            "Kindly check this alert, thank you.\n"
            "_(fix: check APP_ID / APP_SECRET / OSE_SPREADSHEET_TOKEN in .env, then `/duty`)_"
        )
    else:
        greeting = f"Hi {duty_mention} kindly check this alert thank you"
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": greeting}})

    # No footer note here on purpose: the SRE group only needs the alert and the
    # ask. Who pressed the button is still recorded in the bot's log.

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            # Title is the alert name so the SRE group sees what fired at a glance.
            "title": {"tag": "plain_text", "content": _clip(rule, 100)},
            "template": "red",
        },
        "elements": elements,
    }


def _parse_dt(value: Any):
    from datetime import datetime, timezone

    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def format_duration(seconds: float) -> str:
    """Human duration: 45s / 5m 1s / 1h 23m / 2d 3h."""
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s" if s % 60 else f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m" if (s % 3600) // 60 else f"{s // 3600}h"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def firing_duration(alert: dict[str, Any]) -> str | None:
    """How long the alert was firing: created_at -> end_time (or now)."""
    from datetime import datetime, timezone

    start = _parse_dt(alert.get("created_at"))
    if start is None:
        return None
    end = _parse_dt(alert.get("end_time")) or _parse_dt(alert.get("updated_at")) or datetime.now(timezone.utc)
    return format_duration((end - start).total_seconds())


def collapsed_reminder_card() -> dict[str, Any]:
    """Replacement for a threaded 'still firing' reminder once the alert clears.

    Deliberately just a marker: the parent card already carries the alert name,
    flap count and duration, so repeating them here would show the same text
    twice in the same thread.
    """
    return {
        "config": {"wide_screen_mode": True},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "✅ resolved / 已恢复"}}],
    }


def collapsed_card(alert: dict[str, Any]) -> dict[str, Any]:
    """Tiny card that replaces a firing alert's card once it recovers.

    Used instead of recalling the message: patching leaves no "recalled a
    message" notice, and the big card (screenshot, SOP, button) shrinks to one
    quiet line.
    """
    title = alert.get("alert_rule") or alert.get("summary") or f"Alert #{alert.get('id')}"
    lines = [f"✅ **{_clip(title, 90)}** · resolved / 已恢复"]

    try:
        count = int(alert.get("flap_count") or 0)
    except (TypeError, ValueError):
        count = 0
    if count > 0:
        unit = "time" if count == 1 else "times"
        lines.append(f"- Continue firing and resolved - {count} {unit}")

    dur = firing_duration(alert)
    if dur:
        lines.append(f"- Firing duration: {dur}")

    return {
        "config": {"wide_screen_mode": True},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}],
    }


def resolve_card(alert: dict[str, Any]) -> dict[str, Any]:
    """Card sent when a previously-firing alert has recovered."""
    title = alert.get("alert_rule") or alert.get("summary") or f"Alert #{alert.get('id')}"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"✅ Resolved / 已恢复: {title}"},
            "template": "green",
        },
        "elements": [
            {
                "tag": "div",
                "fields": [
                    _field("Severity", alert.get("severity", "-")),
                    _field("Alert ID", alert.get("id", "-")),
                    _field("Domain", alert.get("domain", "-")),
                    _field("Env", alert.get("env", "-")),
                ],
            },
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**Instance / 实例**\n{_clip(alert.get('instance'), 300)}"}},
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": "MonitorFlow · AlertBot ✅ resolved / 已恢复"}],
            },
        ],
    }


def firing_reminder_card(alert: dict[str, Any], minutes: int) -> dict[str, Any]:
    """Short FYI posted in an alert's thread when it's still firing after N minutes."""
    title = alert.get("alert_rule") or alert.get("summary") or f"Alert #{alert.get('id')}"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"⏰ FYI: still firing for {minutes} min"},
            "template": "orange",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**{title}** (`#{alert.get('id')}`) has been **firing for ~{minutes} minutes** "
                        f"and has not recovered yet.\n**Instance / 实例**\n{_clip(alert.get('instance'), 200)}"
                    ),
                },
            }
        ],
    }


def kb_status_card(kb: Any) -> dict[str, Any]:
    """Status of the SOP knowledge base (monitorflow.json)."""
    src = getattr(kb, "_data", {}).get("source") or {}
    entries = kb.entries
    lines = [
        f"**Entries:** {len(entries)}",
        f"**Doc:** {src.get('doc_title') or '-'}",
        f"**Last built:** {kb.generated_at or 'never'}",
        f"**Model:** {getattr(kb, '_data', {}).get('model') or '-'}",
        f"**Doc hash:** `{(kb.content_hash or '-')[:12]}`",
    ]
    rules = kb.global_rules
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}
    ]
    if rules:
        elements.append(_divider())
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**Global rules**\n" + "\n".join(f"- {_clip(r, 200)}" for r in rules[:6])},
        })
    if entries:
        listing = "\n".join(
            f"- {_IMPORTANCE_BADGE.get(str(e.get('importance','medium')).lower(),'')} {_clip(e.get('alert_title'), 70)}"
            for e in entries[:20]
        )
        if len(entries) > 20:
            listing += f"\n… +{len(entries) - 20} more"
        elements.append(_divider())
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**Documented alerts**\n" + listing}})
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": "/kb refresh to rebuild · /kb <alert name> to test a lookup"}],
    })
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "📕 SOP Knowledge Base"},
                   "template": "blue" if entries else "grey"},
        "elements": elements,
    }


def kb_lookup_card(query: str, verdict: dict[str, Any]) -> dict[str, Any]:
    """Preview what the bot would attach for a given alert name."""
    matched = verdict.get("in_docs")
    elements = [
        {"tag": "div", "text": {"tag": "lark_md",
                                "content": f"**Query:** {_clip(query, 150)}\n**Match score:** {verdict.get('score')}"}}
    ]
    elements.extend(sop_elements(verdict))
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🔎 SOP lookup"},
            "template": "green" if matched else "orange",
        },
        "elements": elements,
    }


def _alert_line(alert: dict[str, Any]) -> str:
    sev = _emoji(alert)
    aid = alert.get("id", "?")
    rule = alert.get("alert_rule") or alert.get("summary") or "-"
    inst = _clip(alert.get("instance"), 90)
    return f"{sev} `#{aid}` **{rule}**\n   {inst}"


def _sop_alert_line(item: dict[str, Any]) -> str:
    """One line for a firing alert in the /check card, with its SOP verdict.

    /check answers "which alert NAMES are/aren't documented", so it shows the
    rule name (with a count when several instances are firing) and no alert IDs.
    """
    alert = item["alert"]
    verdict = item.get("verdict") or {}
    entry = verdict.get("entry") or {}
    sev = _emoji(alert)
    rule = alert.get("alert_rule") or alert.get("summary") or "-"
    count = item.get("count") or 1
    firing = item.get("firing")
    line = f"{sev} **{rule}**" + (f" ×{count}" if count > 1 else "")
    if firing:
        line += f" · 🔥{firing} firing"
    elif item.get("historic"):
        line += " · 🕘 seen earlier"
    elif firing == 0:
        line += " · ✅ all resolved"
    if verdict.get("in_docs"):
        badge = _IMPORTANCE_BADGE.get(str(verdict.get("importance", "medium")).lower(), "")
        line += f"\n   {badge} — {_clip(verdict.get('action') or entry.get('summary'), 120)}"
    else:
        line += f"\n   ❓ not documented → treat as **IMPORTANT**"
    return line


def _idle_line(entry: dict[str, Any]) -> str:
    """A documented alert that isn't currently matched to anything firing.

    If it nearly matched a firing alert, flag it — that's a likely name mismatch
    between the SOP doc and the dashboard, which is what this section is for.
    """
    badge = _IMPORTANCE_BADGE.get(str(entry.get("importance", "medium")).lower(), "")
    line = f"{badge} {_clip(entry.get('alert_title'), 70)}"
    near = entry.get("_near")
    if near:
        line += (
            f"\n   ⚠️ possible name mismatch → firing as "
            f"**{_clip(near['rule'], 60)}** (score {near['score']})"
        )
    return line


def check_sop_card(
    firing_undocumented: list[dict[str, Any]],
    firing_documented: list[dict[str, Any]],
    idle_documented: list[dict[str, Any]],
    resolved: list[dict[str, Any]] | None = None,
    *,
    requested_by: str | None = None,
    severity_label: str = "all",
    scanned: int | None = None,
) -> dict[str, Any]:
    """/check summary grouped by SOP coverage:
    1. firing but NOT in the SOP doc, 2. firing and found in the doc,
    3. in the doc but not firing right now."""
    elements: list[dict[str, Any]] = []

    # Cap per section. Lark rejects an over-large card outright, so this is a
    # safety net rather than a display preference — raise CHECK_MAX_PER_SECTION
    # if you have more entries than this.
    cap = max(1, CONFIG.check_max_per_section)

    def section(title: str, body_lines: list[str], empty: str, sep: str = "\n\n") -> None:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": title}})
        if body_lines:
            body = sep.join(body_lines[:cap])
            if len(body_lines) > cap:
                body += f"{sep}… +{len(body_lines) - cap} more"
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": body}})
        else:
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"_{empty}_"}})

    # 1) firing, undocumented — most urgent
    section(
        f"**⚠️ 不在文档 / NOT in SOP doc ({len(firing_undocumented)} alert name(s))**",
        [_sop_alert_line(i) for i in firing_undocumented],
        "None — every alert seen is documented 🎉",
    )
    elements.append(_divider())

    # 2) firing, documented
    section(
        f"**📕 已记录 / Found in SOP doc ({len(firing_documented)} alert name(s))**",
        [_sop_alert_line(i) for i in firing_documented],
        "None seen recently",
    )
    elements.append(_divider())

    # 3) documented but quiet
    section(
        f"**📗 文档已记录 · 未出现 / In SOP doc — no alert seen ({len(idle_documented)})**",
        [_idle_line(e) for e in idle_documented],
        "SOP doc is empty — run /kb refresh",
        sep="\n",  # one line each, keep it compact
    )

    if resolved:
        elements.append(_divider())
        section(
            f"**✅ 已恢复 / Recently resolved ({len(resolved)})**",
            [_alert_line(a) for a in resolved],
            "None recently",
        )

    note = f"MonitorFlow · AlertBot /check · severity={severity_label}"
    if scanned:
        note += f" · scanned {scanned} recent alerts (firing + resolved)"
    if requested_by:
        note += f" · by {requested_by}"
    elements.append(_divider())
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": note}]})

    header_color = "red" if firing_undocumented else ("orange" if firing_documented else "green")
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "📋 Alert Check / 告警检查"},
            "template": header_color,
        },
        "elements": elements,
    }


def check_summary_card(
    firing: list[dict[str, Any]],
    resolved: list[dict[str, Any]],
    *,
    requested_by: str | None = None,
    severity_label: str = "all",
) -> dict[str, Any]:
    """Two-category summary for the /check command:
    1) alerts still firing, 2) alerts that have resolved."""
    elements: list[dict[str, Any]] = []

    # ---- Category 1: still firing ----
    elements.append(
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**🔥 仍在告警 / Still Firing ({len(firing)})**"},
        }
    )
    if firing:
        body = "\n\n".join(_alert_line(a) for a in firing[:20])
        if len(firing) > 20:
            body += f"\n\n… +{len(firing) - 20} more"
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": body}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "_None — all clear 🎉_"}})

    elements.append(_divider())

    # ---- Category 2: resolved ----
    elements.append(
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**✅ 已恢复 / Resolved ({len(resolved)})**"},
        }
    )
    if resolved:
        body = "\n\n".join(_alert_line(a) for a in resolved[:20])
        if len(resolved) > 20:
            body += f"\n\n… +{len(resolved) - 20} more"
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": body}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "_None recently_"}})

    note = f"MonitorFlow · AlertBot /check · severity={severity_label}"
    if scanned:
        note += f" · scanned {scanned} recent alerts (firing + resolved)"
    if requested_by:
        note += f" · by {requested_by}"
    elements.append(_divider())
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": note}]})

    header_color = "red" if firing else "green"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "📋 Alert Check / 告警检查"},
            "template": header_color,
        },
        "elements": elements,
    }
