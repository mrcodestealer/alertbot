#!/usr/bin/env python3
"""CLI helper for the SOP knowledge base.

  python build_kb.py check              # verify doc access + Ollama reachability
  python build_kb.py build [--force]    # build/refresh monitorflow.json
  python build_kb.py show               # print what's currently stored
  python build_kb.py test "pm2 restart" # test a lookup against the stored KB
"""
from __future__ import annotations

import json
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

from config import CONFIG  # noqa: E402
from knowledge import KnowledgeBase, KnowledgeBuilder, is_working_hours  # noqa: E402


def cmd_check() -> int:
    ok = True
    print(f"Wiki token : {CONFIG.kb_wiki_token or '(unset)'}")
    if not CONFIG.kb_wiki_token:
        print("  ✗ KB_WIKI_TOKEN is not set in .env")
        ok = False
    else:
        try:
            from docs_client import LarkDocsClient
            doc = LarkDocsClient().get_document(CONFIG.kb_wiki_token)
            print(f"  ✓ doc OK: {doc['title']!r} — {len(doc['text'])} chars, "
                  f"{len(doc['image_tokens'])} images, hash {doc['content_hash'][:12]}")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ cannot read the doc: {e}")
            ok = False

    from llm_client import OllamaClient
    llm = OllamaClient()
    avail, msg = llm.available()
    print(f"Ollama     : {llm.base_url} model={llm.model}")
    print(f"  {'✓' if avail else '✗'} {msg}")
    ok = ok and avail

    if CONFIG.ollama_vision_model:
        vis = OllamaClient(model=CONFIG.ollama_vision_model)
        v_ok, v_msg = vis.available()
        print(f"Vision     : {CONFIG.ollama_vision_model}")
        if v_ok:
            print(f"  ✓ {v_msg} — the doc's screenshots will be read")
        else:
            print(f"  ! {v_msg}")
            print(f"    Images will be SKIPPED (text still works). To enable them:")
            print(f"      ollama pull {CONFIG.ollama_vision_model}")
            print(f"    or set OLLAMA_VISION_MODEL= in .env to turn images off.")
    else:
        print("Vision     : (disabled — OLLAMA_VISION_MODEL is empty; screenshots skipped)")

    print(f"Working hrs: days={CONFIG.work_days} {CONFIG.work_start_hour}:00-{CONFIG.work_end_hour}:00 "
          f"UTC+{CONFIG.work_timezone_offset_hours} -> right now it is "
          f"{'WORKING hours' if is_working_hours() else 'NON-working hours'}")
    print(f"KB file    : {CONFIG.kb_file}")
    return 0 if ok else 1


def cmd_build(force: bool) -> int:
    kb = KnowledgeBase()
    status = KnowledgeBuilder(kb).refresh(force=force)
    print(json.dumps(status, indent=2, ensure_ascii=False))
    return 0 if status.get("ok") else 1


def cmd_show() -> int:
    kb = KnowledgeBase()
    if not kb.entries:
        print("Knowledge base is empty — run: python build_kb.py build")
        return 1
    print(f"Generated : {kb.generated_at}")
    print(f"Doc hash  : {kb.content_hash[:12]}")
    print(f"Entries   : {len(kb.entries)}\n")
    for r in kb.global_rules:
        print(f"  [global] {r}")
    print()
    for e in kb.entries:
        print(f"  [{e.get('importance','?'):<6}] {e.get('alert_title')}")
        if e.get("working_hours_action"):
            print(f"           WH : {e['working_hours_action'][:100]}")
        if e.get("non_working_hours_action"):
            print(f"           NWH: {e['non_working_hours_action'][:100]}")
    return 0


def cmd_test(query: str) -> int:
    kb = KnowledgeBase()
    v = kb.lookup({"alert_rule": query, "summary": query})
    print(json.dumps(v, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "check"
    if cmd == "check":
        return cmd_check()
    if cmd == "build":
        return cmd_build("--force" in args)
    if cmd == "show":
        return cmd_show()
    if cmd == "test":
        if len(args) < 2:
            print('usage: python build_kb.py test "alert name"')
            return 2
        return cmd_test(" ".join(args[1:]))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
