"""Read the SOP wiki document from Lark.

Resolves a Wiki node token -> docx document, pulls every block, and renders it
as plain markdown-ish text. Image blocks are emitted as [IMAGE:<token>] markers
and can be downloaded on demand.
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

import requests

from config import CONFIG

log = logging.getLogger("alertbot.docs")

# Lark docx block_type -> renderer hint.
_HEADING_TYPES = {3: 1, 4: 2, 5: 3, 6: 4, 7: 5, 8: 6, 9: 7, 10: 8, 11: 9}
_BULLET = 12
_ORDERED = 13
_CODE = 14
_QUOTE = 15
_TODO = 17
_CALLOUT = 19
_IMAGE = 27


class LarkDocsClient:
    def __init__(self) -> None:
        self._base = f"{CONFIG.lark_domain}/open-apis"
        self._token: str | None = None
        self._token_expiry = 0.0

    # ------------------------------------------------------------------ auth
    def _tenant_token(self) -> str:
        if self._token and time.time() < self._token_expiry:
            return self._token
        r = requests.post(
            f"{self._base}/auth/v3/tenant_access_token/internal",
            json={"app_id": CONFIG.lark_app_id, "app_secret": CONFIG.lark_app_secret},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"tenant_access_token failed: {data.get('code')} {data.get('msg')}")
        self._token = data["tenant_access_token"]
        self._token_expiry = time.time() + int(data.get("expire", 7200)) - 300
        return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._tenant_token()}"}

    # ------------------------------------------------------------------ wiki
    def resolve_wiki_node(self, wiki_token: str) -> tuple[str, str]:
        """Wiki node token -> (doc_token, title)."""
        r = requests.get(
            f"{self._base}/wiki/v2/spaces/get_node",
            headers=self._headers(),
            params={"token": wiki_token},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"wiki get_node failed: {data.get('code')} {data.get('msg')}")
        node = (data.get("data") or {}).get("node") or {}
        obj_token = node.get("obj_token")
        if not obj_token:
            raise RuntimeError(f"wiki node has no obj_token: {node}")
        if node.get("obj_type") != "docx":
            log.warning("Wiki node obj_type is %r (expected 'docx')", node.get("obj_type"))
        return obj_token, node.get("title") or ""

    # ------------------------------------------------------------------ docx
    def fetch_blocks(self, doc_token: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token = None
        while True:
            params: dict[str, Any] = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            r = requests.get(
                f"{self._base}/docx/v1/documents/{doc_token}/blocks",
                headers=self._headers(),
                params=params,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("code") != 0:
                raise RuntimeError(f"docx blocks failed: {data.get('code')} {data.get('msg')}")
            d = data.get("data") or {}
            items.extend(d.get("items") or [])
            if not d.get("has_more"):
                break
            page_token = d.get("page_token")
        return items

    def download_image(self, file_token: str) -> bytes | None:
        try:
            r = requests.get(
                f"{self._base}/drive/v1/medias/{file_token}/download",
                headers=self._headers(),
                timeout=60,
            )
            if r.status_code != 200 or r.headers.get("content-type", "").startswith("application/json"):
                log.warning("Image download failed for %s: %s %s", file_token, r.status_code, r.text[:200])
                return None
            return r.content
        except Exception:
            log.exception("Image download error for %s", file_token)
            return None

    def resolve_user_names(self, user_ids: set[str]) -> dict[str, str]:
        """open_id -> display name for people @-mentioned in the doc.

        Tries the contact API first (needs contact:user.base:readonly on the
        app); falls back to the open_id map collected via /secret1. Anything
        still unknown renders as a short stable handle.
        """
        names: dict[str, str] = {}
        ids = [u for u in user_ids if u]
        if not ids:
            return names
        try:
            for i in range(0, len(ids), 50):
                r = requests.get(
                    f"{self._base}/contact/v3/users/batch",
                    headers=self._headers(),
                    params={"user_ids": ids[i : i + 50], "user_id_type": "open_id"},
                    timeout=20,
                )
                data = r.json()
                for u in ((data.get("data") or {}).get("items") or []):
                    name = u.get("name") or u.get("en_name") or u.get("nickname")
                    if u.get("open_id") and name:
                        names[u["open_id"]] = name
        except Exception:
            log.debug("Contact lookup for doc mentions failed", exc_info=True)

        missing = [u for u in ids if u not in names]
        if missing:
            # Reuse the name -> open_id map that /secret1 builds.
            try:
                import duty  # noqa: PLC0415

                for name, oid in (duty.load_openids() or {}).items():
                    if oid in missing and name:
                        names[oid] = name
            except Exception:
                log.debug("open_id map lookup failed", exc_info=True)
        if len(names) < len(ids):
            log.info(
                "Doc mentions: resolved %d/%d name(s). Grant contact:user.base:readonly "
                "to the app (or add them via /secret1) to show real names.",
                len(names), len(ids),
            )
        return names

    # ---------------------------------------------------------------- render
    def get_document(self, wiki_token: str) -> dict[str, Any]:
        """Return {title, doc_token, text, image_tokens, content_hash}."""
        doc_token, title = self.resolve_wiki_node(wiki_token)
        blocks = self.fetch_blocks(doc_token)
        names = self.resolve_user_names(mention_ids(blocks))
        text, image_tokens = render_blocks(blocks, names)
        return {
            "title": title,
            "doc_token": doc_token,
            "text": text,
            "image_tokens": image_tokens,
            "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }


def mention_ids(blocks: list[dict[str, Any]]) -> set[str]:
    """Every open_id @-mentioned anywhere in the document."""
    ids: set[str] = set()
    for b in blocks:
        for value in b.values():
            if isinstance(value, dict) and isinstance(value.get("elements"), list):
                for el in value["elements"]:
                    if isinstance(el, dict) and "mention_user" in el:
                        uid = (el["mention_user"] or {}).get("user_id")
                        if uid:
                            ids.add(uid)
    return ids


def _mention_label(user_id: str, names: dict[str, str] | None) -> str:
    """Render an @-mention. Falls back to a short, STABLE handle per person so
    two different people never both read as a generic '@user'."""
    if names:
        name = names.get(user_id)
        if name:
            return f"@{name}"
    return f"@person-{(user_id or '')[-6:]}" if user_id else "@person"


def _element_text(block: dict[str, Any], names: dict[str, str] | None = None) -> str:
    """Concatenate the text of a block's inline elements."""
    for value in block.values():
        if isinstance(value, dict) and isinstance(value.get("elements"), list):
            out = []
            for el in value["elements"]:
                if not isinstance(el, dict):
                    continue
                if "text_run" in el:
                    out.append(el["text_run"].get("content", ""))
                elif "mention_user" in el:
                    # Lark stores mentions as their own element with no
                    # surrounding spaces, so pad them — otherwise the text reads
                    # "SRE and@person-2a9df0still need time".
                    label = _mention_label((el["mention_user"] or {}).get("user_id", ""), names)
                    if out and out[-1] and not out[-1][-1].isspace():
                        label = " " + label
                    out.append(label + " ")
                elif "equation" in el:
                    out.append(el["equation"].get("content", ""))
            return "".join(out)
    return ""


def render_blocks(blocks: list[dict[str, Any]], names: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """Render docx blocks to markdown-ish text + the list of image tokens."""
    lines: list[str] = []
    images: list[str] = []
    for b in blocks:
        bt = b.get("block_type")
        if bt == _IMAGE:
            tok = (b.get("image") or {}).get("token")
            if tok:
                images.append(tok)
                lines.append(f"[IMAGE:{tok}]")
            continue
        text = _element_text(b, names).strip()
        if not text:
            continue
        if bt in _HEADING_TYPES:
            lines.append(f"\n{'#' * (_HEADING_TYPES[bt] + 1)} {text}")
        elif bt == _BULLET:
            lines.append(f"- {text}")
        elif bt == _ORDERED:
            lines.append(f"  * {text}")
        elif bt == _CODE:
            lines.append(f"```\n{text}\n```")
        elif bt == _QUOTE or bt == _CALLOUT:
            lines.append(f"> {text}")
        elif bt == _TODO:
            lines.append(f"- [ ] {text}")
        else:
            lines.append(text)
    return "\n".join(lines).strip(), images
