#!/usr/bin/env python3
"""CLI helper for the SOP knowledge base.

  python build_kb.py check              # verify doc access + Ollama reachability
  python build_kb.py build [--force]    # build/refresh monitorflow.json
  python build_kb.py show               # print what's currently stored
  python build_kb.py test "pm2 restart" # test a lookup against the stored KB
  python build_kb.py coverage           # which real alerts are / aren't documented
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
    # Show the alert names that were extracted — "doc_title" above is the wiki
    # page's name, these are the alerts the bot can now recognise.
    if status.get("ok") and kb.entries:
        print(f"\nExtracted {len(kb.entries)} alert(s):")
        for e in kb.entries:
            print(f"  [{e.get('importance','?'):<6}] {e.get('alert_title')}")
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


def cmd_coverage(limit_pages: int = 4) -> int:
    """Compare the KB against alerts actually seen on the dashboard.

    Shows which real alerts are documented, which are not (so you know what to
    add to the wiki), and flags weak matches worth double-checking.
    """
    from collections import Counter

    from monitor_client import MonitorClient

    kb = KnowledgeBase()
    if not kb.entries:
        print("Knowledge base is empty — run: python build_kb.py build")
        return 1

    mc = MonitorClient()
    mc.login()
    rules: Counter = Counter()
    for a in mc.list_all_alerts(severity=CONFIG.severity_filter, page_size=200, max_pages=limit_pages):
        rules[a.get("alert_rule") or "?"] += 1

    matched, weak, missing = [], [], []
    for rule, cnt in rules.most_common():
        v = kb.lookup({"alert_rule": rule, "summary": rule})
        title = (v.get("entry") or {}).get("alert_title", "")
        if not v["in_docs"]:
            missing.append((cnt, rule))
        elif v["score"] < 0.75:
            weak.append((cnt, rule, v["score"], title))
        else:
            matched.append((cnt, rule, v["score"], title))

    total = sum(rules.values()) or 1
    covered = sum(c for c, *_ in matched) + sum(c for c, *_ in weak)
    print(f"Alert rules seen : {len(rules)} distinct ({total} occurrences)")
    print(f"KB entries       : {len(kb.entries)}")
    print(f"Coverage         : {covered}/{total} occurrences ({100*covered/total:.0f}%)\n")

    print(f"=== DOCUMENTED ({len(matched)}) ===")
    for c, rule, s, title in matched:
        print(f"  x{c:<5} {rule[:52]:<54} -> {title[:40]}")

    if weak:
        print(f"\n=== WEAK MATCHES ({len(weak)}) — verify these are really the same alert ===")
        for c, rule, s, title in weak:
            print(f"  x{c:<5} [{s}] {rule[:46]:<48} -> {title[:40]}")

    print(f"\n=== NOT IN THE DOC ({len(missing)}) — treated as IMPORTANT; add the frequent ones to the wiki ===")
    for c, rule in missing:
        print(f"  x{c:<5} {rule[:80]}")
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
    if cmd == "coverage":
        return cmd_coverage()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
