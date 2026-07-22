#!/usr/bin/env python3
"""
kb_manager.py — Interactive Knowledge Base manager.

Commands:
  python scripts/kb_manager.py --review       Interactive review of pending suggestions
  python scripts/kb_manager.py --list         List all suggestions (pending/merged/rejected)
  python scripts/kb_manager.py --auto-merge   Apply all auto-approvable pending suggestions
  python scripts/kb_manager.py --stats        KB statistics
  python scripts/kb_manager.py --history N    Show last N merged suggestions

Review mode walks through each pending suggestion and asks:
  [a] approve and merge
  [r] reject
  [s] skip for now
  [q] quit
"""

import argparse
import os
import sys
import yaml
from datetime import datetime
from pathlib import Path

ROOT_DIR   = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))

from kb_updater import (
    load_pending_suggestions, apply_suggestions,
    _load_kb, _save_kb, _update_kb_metadata,
    _merge_suggestion, _update_suggestion_status,
    SUGGESTIONS_DIR, KB_PATH,
)


class C:
    HEADER = "\033[95m"; BLUE = "\033[94m"; CYAN = "\033[96m"
    GREEN = "\033[92m";  YELLOW = "\033[93m"; RED = "\033[91m"
    END = "\033[0m";     BOLD = "\033[1m";    DIM = "\033[2m"


SEV_ICON = {"critical": "🔴", "error": "🔴", "warning": "🟡", "info": "🔵"}
LAYER_LABEL = {
    "L0": "Identity", "L1": "Architecture", "L2": "Conventions",
    "L3": "Quality Gates", "L4": "Domain", "L5": "Integrations", "L6": "Release",
}


def _print_suggestion(s, index: int, total: int):
    print(f"\n{C.BOLD}{C.HEADER}{'─'*60}{C.END}")
    print(f"{C.BOLD}Suggestion {index}/{total}  [{s.id}]{C.END}")
    print(f"{'─'*60}")
    print(f"  Source    : {C.CYAN}{s.source}{C.END}")
    print(f"  Layer     : {C.BLUE}{s.layer} — {LAYER_LABEL.get(s.layer,'?')}{C.END}")
    print(f"  Section   : {s.section}")
    print(f"  Type      : {s.type}")
    print(f"  Confidence: {C.GREEN if s.confidence > 0.75 else C.YELLOW}{s.confidence:.0%}{C.END}")
    print(f"  Date      : {s.date}")
    print()
    print(f"  {C.BOLD}Reason:{C.END} {s.reason}")
    print()
    print(f"  {C.BOLD}Content to add:{C.END}")
    content_lines = yaml.dump(s.content, default_flow_style=False,
                               allow_unicode=True).splitlines()
    for line in content_lines:
        print(f"    {C.DIM}{line}{C.END}")
    print()
    icon = "✅" if s.auto_approvable else "⚠️ "
    print(f"  Auto-approvable: {icon} {'Yes' if s.auto_approvable else 'No — manual review required'}")


def cmd_decide(suggestion_id: str, action: str) -> int:
    """
    Non-interactive approve/reject of a single suggestion by ID.
    Used by the MCP server and by CI. Returns 0 on success, 1 on failure.
    """
    pending = load_pending_suggestions()
    target = next((s for s in pending if s.id == suggestion_id), None)
    if target is None:
        print(f"❌ No pending suggestion with id '{suggestion_id}'")
        return 1

    if action == "approve":
        kb = _load_kb()
        if _merge_suggestion(kb, target):
            _update_kb_metadata(kb, 1, datetime.now().strftime("%Y-%m-%d"))
            _save_kb(kb)
            _update_suggestion_status(target.id, "merged")
            print(f"✓ Suggestion {target.id} merged into knowledge base")
        else:
            _update_suggestion_status(target.id, "merged")
            print(f"⚠ Suggestion {target.id} already present — marked as merged")
        return 0

    if action == "reject":
        _update_suggestion_status(target.id, "rejected")
        print(f"✗ Suggestion {target.id} rejected")
        return 0

    print(f"❌ Unknown action '{action}' (use approve|reject)")
    return 1


def cmd_review(auto_approve_threshold: float = 0.0):
    """Interactive review of pending suggestions."""
    pending = load_pending_suggestions()
    if not pending:
        print(f"\n{C.GREEN}✓ No pending knowledge base suggestions.{C.END}\n")
        return

    print(f"\n{C.BOLD}{C.HEADER}{'='*60}{C.END}")
    print(f"{C.BOLD}{C.HEADER}  📚 Knowledge Base Review — {len(pending)} pending{C.END}")
    print(f"{C.BOLD}{C.HEADER}{'='*60}{C.END}")
    print(f"\n  Commands: {C.BOLD}[a]{C.END}pprove  "
          f"{C.BOLD}[r]{C.END}eject  "
          f"{C.BOLD}[s]{C.END}kip  "
          f"{C.BOLD}[q]{C.END}uit\n")

    kb = _load_kb()
    merged_count = 0
    today = datetime.now().strftime("%Y-%m-%d")

    for i, s in enumerate(pending, 1):
        _print_suggestion(s, i, len(pending))

        while True:
            try:
                choice = input(f"  → ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                choice = "q"

            if choice in ("a", "approve"):
                if _merge_suggestion(kb, s):
                    merged_count += 1
                    _update_suggestion_status(s.id, "merged")
                    print(f"  {C.GREEN}✓ Merged into knowledge base{C.END}")
                else:
                    _update_suggestion_status(s.id, "merged")
                    print(f"  {C.YELLOW}⚠ Already present — marked as merged{C.END}")
                break

            elif choice in ("r", "reject"):
                _update_suggestion_status(s.id, "rejected")
                print(f"  {C.RED}✗ Rejected{C.END}")
                break

            elif choice in ("s", "skip", ""):
                print(f"  {C.DIM}Skipped{C.END}")
                break

            elif choice in ("q", "quit"):
                if merged_count > 0:
                    _update_kb_metadata(kb, merged_count, today)
                    _save_kb(kb)
                    print(f"\n{C.GREEN}✓ {merged_count} suggestion(s) merged into KB{C.END}")
                print(f"\n{C.YELLOW}Review paused. Run again to continue.{C.END}\n")
                return

            else:
                print(f"  Unknown command. Use: a / r / s / q")

    if merged_count > 0:
        _update_kb_metadata(kb, merged_count, today)
        _save_kb(kb)

    remaining = load_pending_suggestions()
    print(f"\n{C.BOLD}{C.GREEN}{'='*60}{C.END}")
    print(f"{C.BOLD}{C.GREEN}  Review complete:{C.END}")
    print(f"    Merged  : {merged_count}")
    print(f"    Remaining pending: {len(remaining)}")
    print(f"{C.BOLD}{C.GREEN}{'='*60}{C.END}\n")


def cmd_list(status_filter: str = None):
    """List all suggestions."""
    if not SUGGESTIONS_DIR.exists():
        print("No suggestions directory found.")
        return

    files = sorted(SUGGESTIONS_DIR.glob("*.yaml"), reverse=True)
    if not files:
        print("No suggestions found.")
        return

    by_status = {}
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            status = data.get("status", "pending")
            if status_filter and status != status_filter:
                continue
            by_status.setdefault(status, []).append(data)
        except Exception:
            continue

    status_order = ["pending", "merged", "rejected"]
    status_icons = {"pending": "⏳", "merged": "✅", "rejected": "❌"}

    for status in status_order:
        items = by_status.get(status, [])
        if not items:
            continue
        print(f"\n{C.BOLD}{status_icons.get(status,'')} {status.upper()} ({len(items)}){C.END}")
        for d in items:
            conf = d.get("confidence", 0)
            conf_str = f"{conf:.0%}"
            layer = d.get("layer","?")
            section = d.get("section","?")
            reason = d.get("reason","")[:60]
            source = d.get("source","?")
            print(f"  {d['id']}  [{layer}/{section}]  {conf_str}  {source}")
            print(f"    {C.DIM}{reason}...{C.END}")


def cmd_auto_merge():
    """Apply all auto-approvable pending suggestions."""
    pending = load_pending_suggestions()
    auto = [s for s in pending if s.auto_approvable]

    if not auto:
        print(f"\n{C.YELLOW}No auto-approvable suggestions pending.{C.END}\n")
        return

    print(f"\n{C.CYAN}Auto-merging {len(auto)} suggestion(s)...{C.END}\n")
    result = apply_suggestions(auto, auto_only=True)
    print(f"  {C.GREEN}✓ Merged : {result['merged']}{C.END}")
    print(f"  {C.DIM}Skipped (already present): {result['skipped']}{C.END}")
    queued = [s for s in pending if not s.auto_approvable]
    if queued:
        print(f"  {C.YELLOW}⏳ Still pending (manual review): {len(queued)}{C.END}")
        print(f"     Run: python scripts/kb_manager.py --review\n")


def cmd_stats():
    """Show KB statistics and suggestion history."""
    kb = _load_kb()

    print(f"\n{C.BOLD}📚 Knowledge Base Statistics{C.END}\n")

    # KB content
    proj = kb.get("project", {})
    print(f"  Project : {proj.get('name','(not set)')}")
    meta = kb.get("metadata", {})
    print(f"  Version : {meta.get('version','?')}")
    print(f"  Updated : {meta.get('last_updated','?')}")
    print()

    conv = kb.get("conventions", {})
    total_rules = sum(
        len(v.get("rules", [])) for v in conv.values()
        if isinstance(v, dict)
    )
    print(f"  Conventions : {len(conv)} sections, {total_rules} rules")

    gates = kb.get("quality_gates", {})
    total_gates = sum(len(v) for v in gates.values() if isinstance(v, list))
    print(f"  Quality gates: {total_gates} total", end="  ")
    for sev, items in gates.items():
        if isinstance(items, list) and items:
            print(f"| {sev}: {len(items)}", end=" ")
    print()

    domain_areas = kb.get("domain", {}).get("business_rules", [])
    print(f"  Domain rules: {len(domain_areas)} areas")

    integrations = kb.get("integrations", [])
    print(f"  Integrations: {len(integrations)}")
    print()

    # Suggestion stats
    if SUGGESTIONS_DIR.exists():
        files = list(SUGGESTIONS_DIR.glob("*.yaml"))
        stats = {"pending": 0, "merged": 0, "rejected": 0}
        sources = {}
        for path in files:
            try:
                with open(path, encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                stats[data.get("status","pending")] = stats.get(data.get("status","pending"),0) + 1
                src = data.get("source","?")
                sources[src] = sources.get(src, 0) + 1
            except Exception:
                continue

        print(f"  Suggestions — pending: {stats['pending']}, "
              f"merged: {stats['merged']}, rejected: {stats['rejected']}")
        if sources:
            src_str = ", ".join(f"{k}: {v}" for k,v in sources.items())
            print(f"  Sources — {src_str}")

    # Changelog
    changelog = meta.get("changelog", [])
    if changelog:
        print(f"\n  {C.BOLD}Recent changelog:{C.END}")
        for entry in reversed(changelog[-5:]):
            print(f"    {entry.get('date','?')}  {entry.get('change','')}")
    print()


def cmd_history(n: int = 10):
    """Show the last N merged suggestions."""
    if not SUGGESTIONS_DIR.exists():
        print("No suggestions directory.")
        return

    merged = []
    for path in sorted(SUGGESTIONS_DIR.glob("*.yaml"), reverse=True):
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if data.get("status") == "merged":
                merged.append(data)
                if len(merged) >= n:
                    break
        except Exception:
            continue

    if not merged:
        print("No merged suggestions found.")
        return

    print(f"\n{C.BOLD}📜 Last {len(merged)} merged KB suggestions{C.END}\n")
    for d in merged:
        merged_at = d.get("merged_at","?")[:10] if d.get("merged_at") else d.get("date","?")
        print(f"  {merged_at}  [{d.get('layer','?')}/{d.get('section','?')}]  "
              f"{d.get('source','?')}  {d.get('reason','')[:70]}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Git Daily Review — KB Manager")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--review",     action="store_true", help="Interactive review of pending suggestions")
    group.add_argument("--list",       action="store_true", help="List all suggestions")
    group.add_argument("--auto-merge", action="store_true", help="Auto-merge all approvable suggestions")
    group.add_argument("--stats",      action="store_true", help="KB statistics")
    group.add_argument("--approve",    metavar="ID", help="Approve and merge a pending suggestion by ID (non-interactive)")
    group.add_argument("--reject",     metavar="ID", help="Reject a pending suggestion by ID (non-interactive)")
    group.add_argument("--history",    type=int, metavar="N", nargs="?", const=10,
                       help="Show last N merged suggestions (default: 10)")
    parser.add_argument("--status", help="Filter --list by status (pending/merged/rejected)")
    args = parser.parse_args()

    if args.approve:
        sys.exit(cmd_decide(args.approve, "approve"))
    elif args.reject:
        sys.exit(cmd_decide(args.reject, "reject"))
    elif args.review:
        cmd_review()
    elif args.list:
        cmd_list(args.status)
    elif args.auto_merge:
        cmd_auto_merge()
    elif args.stats:
        cmd_stats()
    elif args.history is not None:
        cmd_history(args.history)


if __name__ == "__main__":
    main()