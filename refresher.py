"""Background thread that keeps monitorflow.json in sync with the SOP wiki doc.

Runs every KB_REFRESH_MINUTES. It only invokes the LLM when the document's
content hash has actually changed, so a normal hourly tick is a cheap API read.
"""
from __future__ import annotations

import logging
import threading

from config import CONFIG
from knowledge import KnowledgeBuilder

log = logging.getLogger("alertbot.refresher")


class KnowledgeRefresher(threading.Thread):
    def __init__(self, builder: KnowledgeBuilder) -> None:
        super().__init__(name="kb-refresher", daemon=True)
        self.builder = builder
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.last_status: dict | None = None

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def trigger(self) -> None:
        """Ask for an immediate refresh (used by the /kb command)."""
        self._wake.set()

    def run(self) -> None:
        log.info(
            "KB refresher started: every %d min (doc=%s, model=%s)",
            CONFIG.kb_refresh_minutes, CONFIG.kb_wiki_token or "(unset)", CONFIG.ollama_model,
        )
        while not self._stop.is_set():
            try:
                self.last_status = self.builder.refresh()
                log.info("KB refresh: %s", self.last_status)
            except Exception as e:  # noqa: BLE001
                log.exception("KB refresh failed")
                self.last_status = {"ok": False, "reason": str(e)[:300]}
            # Sleep until the next interval, but wake early if triggered.
            self._wake.wait(CONFIG.kb_refresh_minutes * 60)
            self._wake.clear()
