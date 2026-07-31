"""Build and query the alert knowledge base (monitorflow.json).

Flow:
  1. Fetch the SOP wiki doc (docs_client).
  2. If its content hash changed, ask the LLM to distil it into structured JSON.
  3. Persist to monitorflow.json.
  4. At alert time, look up the alert *locally* (no LLM call) to answer:
     is this documented? is it important? what do I do now?

Design note: an alert that is NOT in the docs is treated as IMPORTANT, per the
operational rule "if it isn't documented, don't assume it's safe".
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import CONFIG
from docs_client import LarkDocsClient
from llm_client import LLMError, OllamaClient

log = logging.getLogger("alertbot.kb")

KB_VERSION = 1

_SYSTEM_PROMPT = (
    "You are an SRE knowledge extractor. You read an on-call SOP document and turn it into "
    "strict JSON. Only use facts stated in the document. Never invent alerts or procedures. "
    "Reply with JSON only."
)

_USER_PROMPT = """\
Below is an on-call SOP document describing monitoring alerts and how to handle them.

Extract EVERY alert that the document describes and return JSON with this exact shape:

{{
  "global_rules": ["short rules that apply to all alerts in this document"],
  "entries": [
    {{
      "alert_title": "the alert name exactly as written in the document",
      "aliases": ["other names/spellings used for the same alert"],
      "keywords": ["3-6 lowercase distinctive words for matching, e.g. pod, restart"],
      "importance": "high | medium | low",
      "important": true,
      "summary": "one sentence: what this alert means",
      "working_hours_action": "what to do during working hours (empty string if not stated)",
      "non_working_hours_action": "what to do outside working hours (empty string if not stated)",
      "escalation": "who to contact / when to call, if stated",
      "ignore_conditions": ["conditions under which this alert can be ignored"],
      "notes": ["other useful details, e.g. timing thresholds"]
    }}
  ]
}}

Rules:
- "importance": high = must call/escalate immediately or doc calls it very important/urgent;
  medium = report to the group / needs follow-up; low = can be ignored or checked later.
- "important" is true unless the document clearly says the alert can be ignored or is low priority.
- Keep every string short and actionable (max ~200 chars). Use the document's own wording.
- [IMAGE:...] markers are screenshots; ignore them unless a caption near them adds a rule.
- Output JSON only, no commentary.

DOCUMENT:
---
{doc}
---
"""


# --------------------------------------------------------------------- matching
_NOISE = re.compile(
    r"\b(critical|warning|info|firing|resolved|prod|production|non-prod|nonprod|uat|qat|alert|alerts)\b",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[\[\]\(\)\{\}<>:,\|/\\\-_#>=%\"'\.]+")
_WS = re.compile(r"\s+")
# [IMAGE:<token>] markers emitted by docs_client.render_blocks
_IMAGE_MARKER = re.compile(r"\[IMAGE:[A-Za-z0-9]+\]")


def normalize(text: str) -> str:
    """Lowercase, drop severity/env noise words and punctuation, collapse spaces."""
    if not text:
        return ""
    t = str(text).lower()
    t = _PUNCT.sub(" ", t)
    t = _NOISE.sub(" ", t)
    return _WS.sub(" ", t).strip()


def _tokens(text: str) -> set[str]:
    return {w for w in normalize(text).split() if len(w) > 1}


def _score(alert_text: str, entry: dict[str, Any]) -> float:
    """0..1 similarity between an incoming alert and a KB entry."""
    a_norm = normalize(alert_text)
    if not a_norm:
        return 0.0
    a_tok = _tokens(alert_text)

    best = 0.0
    candidates = [entry.get("alert_title", "")] + list(entry.get("aliases") or [])
    for cand in candidates:
        c_norm = normalize(cand)
        if not c_norm:
            continue
        if a_norm == c_norm:
            return 1.0
        if a_norm in c_norm or c_norm in a_norm:
            best = max(best, 0.9)
        c_tok = _tokens(cand)
        if a_tok and c_tok:
            overlap = len(a_tok & c_tok) / len(a_tok | c_tok)  # Jaccard
            # Also reward covering the candidate's tokens (alert names are often
            # longer than the doc heading), but weight it below Jaccard: on its
            # own, coverage by generic words like "aliyun"/"ack" must not be
            # enough to match two genuinely different alerts.
            covered = len(a_tok & c_tok) / max(1, len(c_tok))
            best = max(best, overlap, covered * 0.7)

    kws = [k for k in (entry.get("keywords") or []) if k]
    if kws:
        hit = sum(1 for k in kws if normalize(k) and normalize(k) in a_norm)
        if hit:
            best = max(best, 0.55 + 0.1 * hit if hit >= len(kws) else 0.4 + 0.1 * hit)
    return min(best, 1.0)


def is_working_hours(now: datetime | None = None) -> bool:
    tz = timezone(timedelta(hours=CONFIG.work_timezone_offset_hours))
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    return now.weekday() in CONFIG.work_days and CONFIG.work_start_hour <= now.hour < CONFIG.work_end_hour


# ------------------------------------------------------------------ knowledge
class KnowledgeBase:
    """Thread-safe holder for monitorflow.json."""

    MATCH_THRESHOLD = 0.55

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or CONFIG.kb_file
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"version": KB_VERSION, "entries": [], "global_rules": []}
        self.load()

    # ------------------------------------------------------------- persistence
    def load(self) -> bool:
        try:
            if self._path.exists():
                with self._lock:
                    self._data = json.loads(self._path.read_text(encoding="utf-8"))
                log.info("Loaded knowledge base: %d entries (generated %s)",
                         len(self.entries), self._data.get("generated_at"))
                return True
        except Exception:
            log.exception("Failed to load %s", self._path)
        return False

    def save(self) -> None:
        with self._lock:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)
            log.info("Saved knowledge base -> %s (%d entries)", self._path, len(self.entries))

    @property
    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._data.get("entries") or []

    @property
    def global_rules(self) -> list[str]:
        with self._lock:
            return self._data.get("global_rules") or []

    @property
    def content_hash(self) -> str:
        with self._lock:
            return (self._data.get("source") or {}).get("content_hash", "")

    @property
    def generated_at(self) -> str:
        with self._lock:
            return self._data.get("generated_at", "")

    def replace(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._data = data

    # ------------------------------------------------------------------ query
    def lookup(self, alert: dict[str, Any]) -> dict[str, Any]:
        """Match an alert against the KB. Always returns a verdict dict:
        {matched, in_docs, important, importance, action, entry, score}."""
        text = " ".join(
            str(alert.get(k) or "") for k in ("alert_rule", "summary")
        ).strip()
        best, best_score = None, 0.0
        for e in self.entries:
            s = _score(text, e)
            if s > best_score:
                best, best_score = e, s

        working = is_working_hours()
        if best is None or best_score < self.MATCH_THRESHOLD:
            # Not documented -> treat as important (explicit requirement).
            return {
                "matched": False,
                "in_docs": False,
                "important": True,
                "importance": "unknown",
                "action": "",
                "entry": None,
                "score": round(best_score, 2),
                "working_hours": working,
            }

        action = (best.get("working_hours_action") if working else best.get("non_working_hours_action")) or ""
        if not action:
            action = best.get("working_hours_action") or best.get("non_working_hours_action") or ""
        return {
            "matched": True,
            "in_docs": True,
            "important": bool(best.get("important", True)),
            "importance": best.get("importance") or "medium",
            "action": action,
            "entry": best,
            "score": round(best_score, 2),
            "working_hours": working,
        }


# ----------------------------------------------------------------- build lock
class BuildInProgress(RuntimeError):
    """Another process is already rebuilding the knowledge base."""


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check. Unknown -> assume alive (fail safe)."""
    if pid <= 0:
        return False
    if os.name == "nt":  # Windows: os.kill(pid, 0) can't distinguish "gone"
        try:
            import ctypes

            # PROCESS_QUERY_LIMITED_INFORMATION
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:  # noqa: BLE001
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return True


@contextmanager
def build_lock(kb_path: Path, stale_seconds: int = 3600):
    """Cross-process lock so the service's refresher and a manual `build_kb.py`
    run can't rebuild at the same time — two concurrent loads of a 20GB+ model
    will thrash or OOM the box."""
    lock = kb_path.with_suffix(".lock")
    fd = None
    acquired = False
    try:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                age = stale_seconds + 1  # vanished underneath us; treat as stale
            holder = ""
            try:
                holder = lock.read_text(encoding="utf-8").strip()
            except OSError:
                pass
            # A lock whose owner died (e.g. `systemctl restart` during a build)
            # is stale immediately — don't make the next build wait it out.
            owner_dead = holder.isdigit() and not _pid_alive(int(holder))
            if age <= stale_seconds and not owner_dead:
                raise BuildInProgress(
                    f"another build is in progress (lock held {int(age)}s by pid {holder or '?'})"
                )
            log.warning(
                "Reclaiming KB build lock (%ds old, pid %s %s)",
                int(age), holder or "?", "is gone" if owner_dead else "timed out",
            )
            try:
                lock.unlink()
            except OSError:
                pass
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        fd = None
        acquired = True  # only now may we remove it on the way out
        yield
    finally:
        if fd is not None:
            os.close(fd)
        # Never delete a lock we didn't acquire — that's someone else's build.
        if acquired:
            try:
                lock.unlink()
            except OSError:
                pass


# -------------------------------------------------------------------- builder
class KnowledgeBuilder:
    """Fetches the doc and (re)builds the knowledge base via the LLM."""

    def __init__(self, kb: KnowledgeBase) -> None:
        self.kb = kb
        self.docs = LarkDocsClient()
        self.llm = OllamaClient()

    def _caption_images(self, doc: dict[str, Any]) -> tuple[str, dict[str, str]]:
        """Replace [IMAGE:token] markers with descriptions from the vision model.

        Captions are cached by image token in the KB, so unchanged screenshots are
        never re-read. Returns (enriched_text, captions).
        """
        text = doc["text"]
        tokens = doc["image_tokens"]
        if not CONFIG.ollama_vision_model or not tokens:
            # Neutralise the markers so raw image tokens never reach the model.
            return _IMAGE_MARKER.sub("[SCREENSHOT]", text), {}

        cached: dict[str, str] = dict(self.kb._data.get("image_captions") or {})  # noqa: SLF001
        vision = OllamaClient(model=CONFIG.ollama_vision_model)
        todo = [t for t in tokens if t not in cached]

        if todo:
            # Pre-flight once: if the vision model isn't installed, skip images
            # entirely instead of failing 21 times.
            ok, why = vision.available()
            if not ok:
                log.warning(
                    "Vision model %r unavailable (%s) — continuing with text only. "
                    "Install it (`ollama pull %s`), pick another via OLLAMA_VISION_MODEL, "
                    "or set OLLAMA_VISION_MODEL= to silence this.",
                    CONFIG.ollama_vision_model, why, CONFIG.ollama_vision_model,
                )
                return _IMAGE_MARKER.sub("[SCREENSHOT]", text), {}

        log.info("Captioning %d image(s) with %s (%d cached)",
                 len(todo), CONFIG.ollama_vision_model, len(tokens) - len(todo))

        failures = 0
        deadline = time.time() + CONFIG.kb_caption_budget_seconds
        for i, tok in enumerate(todo, 1):
            if time.time() > deadline:
                log.warning(
                    "Caption budget (%ds) spent after %d/%d image(s) — continuing with text only. "
                    "Raise KB_CAPTION_BUDGET_SECONDS, or set OLLAMA_VISION_MODEL= to skip images.",
                    CONFIG.kb_caption_budget_seconds, i - 1, len(todo),
                )
                break
            data = self.docs.download_image(tok)
            if not data:
                cached[tok] = ""
                continue
            try:
                t0 = time.time()
                caption = vision.chat_text(
                    "You describe screenshots from an SRE alert runbook. Be brief and factual.",
                    "Describe this screenshot in one or two sentences. If it shows an alert "
                    "message, state the alert name and any threshold or instruction visible.",
                    images=[data],
                    timeout=CONFIG.ollama_vision_timeout_seconds,
                )
                cached[tok] = " ".join(caption.split())[:400]
                failures = 0
                log.info("Captioned image %d/%d in %.0fs", i, len(todo), time.time() - t0)
            except Exception as e:  # noqa: BLE001
                cached[tok] = ""
                failures += 1
                log.warning("Captioning failed for image %s: %s", tok[:10], str(e)[:160])
                if failures >= 3:
                    log.warning(
                        "Giving up on image captioning after %d consecutive failures — "
                        "continuing with text only. The SOP text is what matters; set "
                        "OLLAMA_VISION_MODEL= to skip images entirely.", failures,
                    )
                    break

        for tok, cap in cached.items():
            text = text.replace(f"[IMAGE:{tok}]", f"[SCREENSHOT: {cap}]" if cap else "[SCREENSHOT]")
        # Any marker left over (e.g. we bailed out early) still gets neutralised,
        # so raw tokens never reach the model.
        text = _IMAGE_MARKER.sub("[SCREENSHOT]", text)
        # Keep only captions for images still in the doc.
        return text, {t: cached.get(t, "") for t in tokens if t in cached}

    def refresh(self, *, force: bool = False) -> dict[str, Any]:
        """Returns a small status dict describing what happened."""
        if not CONFIG.kb_wiki_token:
            return {"ok": False, "reason": "KB_WIKI_TOKEN is not set"}

        doc = self.docs.get_document(CONFIG.kb_wiki_token)
        if not force and doc["content_hash"] == self.kb.content_hash and self.kb.entries:
            log.info("SOP doc unchanged (hash %s) — skipping LLM rebuild", doc["content_hash"][:12])
            return {
                "ok": True, "changed": False, "skipped_llm": True,
                "entries": len(self.kb.entries), "doc_title": doc["title"],
            }

        started = time.time()
        try:
            lock_ctx = build_lock(self.kb._path)  # noqa: SLF001
            lock_ctx.__enter__()
        except BuildInProgress as e:
            log.info("Skipping rebuild: %s", e)
            return {"ok": True, "changed": False, "skipped_llm": True,
                    "reason": str(e), "entries": len(self.kb.entries)}
        try:
            doc_text, captions = self._caption_images(doc)
            log.info("SOP doc changed (or forced) — asking %s to extract %d chars…",
                     self.llm.model, len(doc_text))
            parsed = self.llm.chat_json(_SYSTEM_PROMPT, _USER_PROMPT.format(doc=doc_text))
        finally:
            lock_ctx.__exit__(None, None, None)

        entries = _clean_entries(parsed)
        if not entries:
            raise LLMError("model returned no usable entries")

        data = {
            "version": KB_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": self.llm.model,
            "source": {
                "wiki_token": CONFIG.kb_wiki_token,
                "doc_token": doc["doc_token"],
                "doc_title": doc["title"],
                "content_hash": doc["content_hash"],
                "image_count": len(doc["image_tokens"]),
                "text_chars": len(doc["text"]),
            },
            # parsed may legitimately be a bare list of entries, hence the guard.
            "global_rules": [
                str(r)[:300]
                for r in ((parsed.get("global_rules") or []) if isinstance(parsed, dict) else [])
            ][:20],
            "entries": entries,
            # Cached so unchanged screenshots are never re-read by the vision model.
            "image_captions": captions,
        }
        self.kb.replace(data)
        self.kb.save()
        took = time.time() - started
        log.info("Knowledge base rebuilt: %d entries in %.1fs", len(entries), took)
        return {
            "ok": True, "changed": True, "skipped_llm": False,
            "entries": len(entries), "seconds": round(took, 1), "doc_title": doc["title"],
        }


def _clean_entries(parsed: Any) -> list[dict[str, Any]]:
    """Validate/normalise the model's output into KB entries."""
    if isinstance(parsed, list):
        raw = parsed
    elif isinstance(parsed, dict):
        raw = parsed.get("entries") or parsed.get("alerts") or []
    else:
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = str(item.get("alert_title") or item.get("title") or "").strip()
        if not title:
            continue
        key = normalize(title)
        if not key or key in seen:
            continue
        seen.add(key)

        importance = str(item.get("importance") or "medium").strip().lower()
        if importance not in {"high", "medium", "low"}:
            importance = "medium"
        important = item.get("important")
        important = bool(important) if isinstance(important, bool) else importance != "low"

        def _slist(value, limit=8, size=300):
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list):
                return []
            return [str(v).strip()[:size] for v in value if str(v).strip()][:limit]

        out.append({
            "alert_title": title[:200],
            "aliases": _slist(item.get("aliases"), 6, 200),
            "keywords": [k.lower() for k in _slist(item.get("keywords"), 8, 40)],
            "in_docs": True,
            "important": important,
            "importance": importance,
            "summary": str(item.get("summary") or "").strip()[:400],
            "working_hours_action": str(item.get("working_hours_action") or "").strip()[:400],
            "non_working_hours_action": str(item.get("non_working_hours_action") or "").strip()[:400],
            "escalation": str(item.get("escalation") or "").strip()[:300],
            "ignore_conditions": _slist(item.get("ignore_conditions"), 6),
            "notes": _slist(item.get("notes"), 8),
        })
    return out
