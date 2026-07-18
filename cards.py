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


def new_alert_card(alert: dict[str, Any], image_key: str | None = None) -> dict[str, Any]:
    """Card for a newly-detected firing alert."""
    title = alert.get("alert_rule") or alert.get("summary") or f"Alert #{alert.get('id')}"
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "fields": [
                _field("Severity", f"{_emoji(alert)} {alert.get('severity', '-')}"),
                _field("Status / 状态", alert.get("status", "-")),
                _field("Domain", alert.get("domain", "-")),
                _field("Env", alert.get("env", "-")),
                _field("Alert ID", alert.get("id", "-")),
                _field("Created / 创建时间", alert.get("created_at", "-")),
            ],
        },
        _divider(),
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**Instance / 实例**\n{_clip(alert.get('instance'), 300)}"}},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**Description / 详细描述**\n{_clip(alert.get('description'))}"}},
    ]

    if image_key:
        elements.append(_divider())
        elements.append(
            {
                "tag": "img",
                "img_key": image_key,
                "alt": {"tag": "plain_text", "content": "alert detail screenshot"},
                "mode": "fit_horizontal",
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
