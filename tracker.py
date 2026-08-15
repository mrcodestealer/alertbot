"""Write reported alerts into the Lark Base "ALERTS TRACKER".

Triggered when someone presses "Report to OSE & SRE group". The base keeps one
TABLE PER MONTH ("August 2026"), and the Platform/DB "views" in the URLs are the
same table filtered on the `Platform` field — so the record just needs the right
Platform value to show up in the right view.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import requests

from config import CONFIG

log = logging.getLogger("alertbot.tracker")

# Alert Domain -> the Platform option in the base. Only these two are tracked;
# anything else is skipped (agreed with the team).
DOMAIN_PLATFORM = {"PLATFORM": "PLATFORM", "DB": "DB"}

ALERT_SOURCE = "MonitorFlow"


def _epoch_ms(value: Any) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


class TrackerError(RuntimeError):
    pass


class AlertsTracker:
    def __init__(self) -> None:
        self._base = f"{CONFIG.lark_domain}/open-apis"
        self._token: str | None = None
        self._token_exp = 0.0
        self._fields_cache: dict[str, list[dict]] = {}

    # ------------------------------------------------------------------ auth
    def _headers(self, json_ct: bool = True) -> dict[str, str]:
        import time

        if not self._token or time.time() >= self._token_exp:
            r = requests.post(
                f"{self._base}/auth/v3/tenant_access_token/internal",
                json={"app_id": CONFIG.lark_app_id, "app_secret": CONFIG.lark_app_secret},
                timeout=20,
            )
            data = r.json()
            if data.get("code") != 0:
                raise TrackerError(f"tenant token failed: {data.get('code')} {data.get('msg')}")
            self._token = data["tenant_access_token"]
            self._token_exp = time.time() + int(data.get("expire", 7200)) - 300
        h = {"Authorization": f"Bearer {self._token}"}
        if json_ct:
            h["Content-Type"] = "application/json"
        return h

    def _api(self, method: str, path: str, **kw) -> dict:
        r = requests.request(method, f"{self._base}{path}", headers=self._headers(), timeout=30, **kw)
        data = r.json()
        if data.get("code") != 0:
            raise TrackerError(f"{method} {path} -> {data.get('code')} {data.get('msg')}")
        return data.get("data") or {}

    # ---------------------------------------------------------------- tables
    def list_tables(self) -> list[dict]:
        return self._api("GET", f"/bitable/v1/apps/{CONFIG.tracker_app_token}/tables",
                         params={"page_size": 100}).get("items") or []

    def month_table_id(self, when: datetime | None = None) -> str:
        """Table for the given month, e.g. 'August 2026'. Created if missing."""
        when = when or datetime.now(timezone.utc).astimezone(
            timezone(__import__("datetime").timedelta(hours=CONFIG.work_timezone_offset_hours))
        )
        wanted = f"{when.strftime('%B')} {when.year}"          # "August 2026"
        tables = self.list_tables()
        for t in tables:
            if (t.get("name") or "").strip().lower() == wanted.lower():
                return t["table_id"]
        log.warning("Tracker table %r does not exist — creating it", wanted)
        return self._create_month_table(wanted)

    def _create_month_table(self, name: str) -> str:
        """Create next month's table by copying the template's field layout.

        Formula and link fields are skipped: their expressions point at the
        template's own table id and cannot be recreated through the API.
        """
        src = CONFIG.tracker_template_table or CONFIG.tracker_table_id
        fields = self._api("GET", f"/bitable/v1/apps/{CONFIG.tracker_app_token}/tables/{src}/fields",
                           params={"page_size": 100}).get("items") or []
        skip = {18, 19, 20, 21, 22, 23}  # link / formula / lookup / rollup
        payload_fields = []
        for f in fields:
            if f.get("type") in skip:
                continue
            item: dict[str, Any] = {"field_name": f.get("field_name"), "type": f.get("type")}
            if f.get("property"):
                item["property"] = f["property"]
            payload_fields.append(item)
        data = self._api(
            "POST", f"/bitable/v1/apps/{CONFIG.tracker_app_token}/tables",
            json={"table": {"name": name, "default_view_name": "Grid", "fields": payload_fields}},
        )
        tid = data.get("table_id")
        log.warning(
            "Created tracker table %r (%s) with %d field(s). Formula/link columns "
            "were not copied — add them by hand if you need them.",
            name, tid, len(payload_fields),
        )
        return tid

    def _fields(self, table_id: str) -> list[dict]:
        if table_id not in self._fields_cache:
            self._fields_cache[table_id] = self._api(
                "GET", f"/bitable/v1/apps/{CONFIG.tracker_app_token}/tables/{table_id}/fields",
                params={"page_size": 100},
            ).get("items") or []
        return self._fields_cache[table_id]

    def _option(self, table_id: str, field_name: str, wanted: str) -> str | None:
        """Exact option label for a select field, matched loosely.

        The base's Severity options include a trailing space ("Critical "), so
        compare on the stripped, lowercased text.
        """
        target = (wanted or "").strip().lower()
        for f in self._fields(table_id):
            if f.get("field_name") == field_name:
                for o in ((f.get("property") or {}).get("options") or []):
                    if (o.get("name") or "").strip().lower() == target:
                        return o.get("name")
        return None

    def harvest_duty_open_ids(self, max_tables: int = 40) -> dict[str, str]:
        """name -> open_id, read from the tracker's `SRE Duty` people fields.

        Existing records already name the duty person AND carry their open_id, so
        the tracker doubles as a directory — no contact scope and no manual
        /secret1 needed for anyone who has appeared on a past alert.
        """
        out: dict[str, str] = {}
        try:
            tables = self.list_tables()
        except Exception:
            log.debug("Tracker: could not list tables for open_id harvest", exc_info=True)
            return out
        # Every monthly table: rosters change, so scan them all (startup only).
        for t in list(reversed(tables))[:max_tables]:
            tid = t.get("table_id")
            if not tid:
                continue
            try:
                items = self._api(
                    "GET", f"/bitable/v1/apps/{CONFIG.tracker_app_token}/tables/{tid}/records",
                    params={"page_size": 500},
                ).get("items") or []
            except Exception:
                continue
            for rec in items:
                for person in (rec.get("fields") or {}).get("SRE Duty") or []:
                    uid = person.get("id")
                    name = person.get("name") or person.get("en_name")
                    if uid and name:
                        out.setdefault(name.strip(), uid)
        return out

    def add_alerts_record(
        self,
        *,
        reporter_open_id: str | None = None,
        screenshot_path: str | None = None,
        when: datetime | None = None,
    ) -> str | None:
        """Add a row to the 'Alerts Record' table for an alert with no runbook.

        Agreed field usage: Person left blank, Record Person = whoever reported
        it (the OSE on duty), Status = Pending, Has Alerts? = Yes,
        Implementation left blank for a human to write up.
        """
        table = CONFIG.alerts_record_table_id
        if not table:
            return None
        when = when or datetime.now(timezone.utc)
        fields: dict[str, Any] = {
            "Date": int(when.timestamp() * 1000),
            "Status": "Pending",
            "Has Alerts?": "Yes",
        }
        if reporter_open_id:
            fields["Record Person"] = [{"id": reporter_open_id}]
        if screenshot_path and os.path.exists(screenshot_path):
            tokenn = self.upload_image(screenshot_path)
            if tokenn:
                # The only thing identifying the alert, since Implementation is
                # left for a human to fill in.
                fields["Attachment"] = [{"file_token": tokenn}]

        data = self._api(
            "POST", f"/bitable/v1/apps/{CONFIG.tracker_app_token}/tables/{table}/records",
            json={"fields": fields},
        )
        rid = (data.get("record") or {}).get("record_id")
        log.info("Tracker: added Alerts Record row %s (no runbook)", rid)
        return rid

    # ----------------------------------------------------------------- media
    def upload_image(self, path: str) -> str | None:
        """Upload a screenshot for the Image Attachment field."""
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                r = requests.post(
                    f"{self._base}/drive/v1/medias/upload_all",
                    headers=self._headers(json_ct=False),
                    data={
                        "file_name": os.path.basename(path),
                        "parent_type": "bitable_image",
                        "parent_node": CONFIG.tracker_app_token,
                        "size": str(size),
                    },
                    files={"file": (os.path.basename(path), fh, "image/png")},
                    timeout=60,
                )
            data = r.json()
            if data.get("code") != 0:
                log.warning("Tracker image upload failed: %s %s", data.get("code"), data.get("msg"))
                return None
            return (data.get("data") or {}).get("file_token")
        except Exception:
            log.exception("Tracker image upload error for %s", path)
            return None

    # ---------------------------------------------------------------- record
    def add_alert(
        self,
        alert: dict[str, Any],
        *,
        duty_open_ids: list[str] | None = None,
        screenshot_path: str | None = None,
        when: datetime | None = None,
        has_runbook: bool | None = None,
    ) -> str | None:
        """Add one alert to this month's table. Returns the record id, or None
        if the alert's domain isn't tracked."""
        domain = (alert.get("domain") or "").strip().upper()
        platform = DOMAIN_PLATFORM.get(domain)
        if not platform:
            log.info("Tracker: skipping #%s — domain %r is not tracked", alert.get("id"), domain)
            return None

        table_id = self.month_table_id(when)
        rule = alert.get("alert_rule") or alert.get("summary") or ""

        fields: dict[str, Any] = {
            "Platform": platform,
            "Alert Source": [ALERT_SOURCE],
            # Unknown single-select values are created automatically by Lark.
            "Alert Summary": rule[:900],
            "Alert Message": (alert.get("description") or alert.get("summary") or "")[:9000],
        }
        received = _epoch_ms(alert.get("created_at"))
        if received:
            fields["Alert Received Date & Time"] = received
        if has_runbook is not None:
            # Ticked only when the SOP doc actually covers this alert.
            fields["Has Runbook?"] = bool(has_runbook)
        sev = self._option(table_id, "Severity", alert.get("severity") or "")
        if sev:
            fields["Severity"] = sev
        if duty_open_ids:
            fields["SRE Duty"] = [{"id": oid} for oid in duty_open_ids if oid]
        if screenshot_path and os.path.exists(screenshot_path):
            tokenn = self.upload_image(screenshot_path)
            if tokenn:
                fields["Image Attachment"] = [{"file_token": tokenn}]

        data = self._api(
            "POST", f"/bitable/v1/apps/{CONFIG.tracker_app_token}/tables/{table_id}/records",
            json={"fields": fields},
        )
        rid = (data.get("record") or {}).get("record_id")
        log.info("Tracker: added #%s to table %s as %s (%s)", alert.get("id"), table_id, rid, platform)
        return rid
