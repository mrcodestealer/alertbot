"""Minimal Ollama client used to distil the SOP doc into structured JSON.

Talks to Ollama's /api/chat. Handles "thinking" models (strips <think> blocks)
and extracts the first JSON object/array if the model wraps its output.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any

import requests

from config import CONFIG

log = logging.getLogger("alertbot.llm")

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class LLMError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        self.base_url = (base_url or CONFIG.ollama_base_url).rstrip("/")
        self.model = model or CONFIG.ollama_model

    def available(self) -> tuple[bool, str]:
        """Check the server is reachable and report whether the model is present."""
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=10)
            r.raise_for_status()
            names = [m.get("name", "") for m in (r.json().get("models") or [])]
            if any(n == self.model or n.startswith(self.model.split(":")[0]) for n in names):
                return True, f"ok (models: {len(names)})"
            return False, f"model {self.model!r} not found; available: {names[:10]}"
        except Exception as e:  # noqa: BLE001
            return False, f"unreachable at {self.base_url}: {e}"

    def chat_json(
        self,
        system: str,
        user: str,
        *,
        images: list[bytes] | None = None,
        model: str | None = None,
        timeout: int | None = None,
    ) -> Any:
        """Send a chat request expecting a JSON reply; returns the parsed object."""
        msg: dict[str, Any] = {"role": "user", "content": user}
        if images:
            msg["images"] = [base64.b64encode(b).decode("ascii") for b in images]
        payload = {
            "model": model or self.model,
            "messages": [{"role": "system", "content": system}, msg],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }
        try:
            r = requests.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=timeout or CONFIG.ollama_timeout_seconds,
            )
        except requests.RequestException as e:
            raise LLMError(f"Ollama request failed ({self.base_url}): {e}") from e
        if r.status_code != 200:
            raise LLMError(f"Ollama HTTP {r.status_code}: {r.text[:300]}")
        content = ((r.json().get("message") or {}).get("content") or "").strip()
        if not content:
            raise LLMError("Ollama returned an empty response")
        return _parse_json(content)

    def chat_text(self, system: str, user: str, *, images: list[bytes] | None = None,
                  model: str | None = None, timeout: int | None = None) -> str:
        msg: dict[str, Any] = {"role": "user", "content": user}
        if images:
            msg["images"] = [base64.b64encode(b).decode("ascii") for b in images]
        payload = {
            "model": model or self.model,
            "messages": [{"role": "system", "content": system}, msg],
            "stream": False,
            "options": {"temperature": 0},
        }
        try:
            r = requests.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=timeout or CONFIG.ollama_timeout_seconds,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            raise LLMError(f"Ollama request failed ({self.base_url}): {e}") from e
        return _strip_think(((r.json().get("message") or {}).get("content") or "").strip())


def _strip_think(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


def _parse_json(content: str) -> Any:
    content = _strip_think(content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    # Fall back to the first balanced {...} or [...] in the text.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = content.find(opener)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(content)):
            if content[i] == opener:
                depth += 1
            elif content[i] == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(content[start : i + 1])
                    except json.JSONDecodeError:
                        break
    raise LLMError(f"Could not parse JSON from model output: {content[:300]}")
