"""Lark interactive card (Card JSON 1.0) builders.

Every function returns a ``dict``; callers ``json.dumps`` it into the message
``content`` field.
"""
from __future__ import annotations

from typing import Any

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
