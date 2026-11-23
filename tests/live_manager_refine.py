#!/usr/bin/env python3
"""Live check of the manager's refinement, against the real Claude Code. Costs under a dollar.

    python3 tests/live_manager_refine.py

Two headless turns under the settings and persona `org talk` uses, about this repository:

  1. a loose request whose premise is false (it blames a serial sync that is in fact parallel)
  2. "go with your recommendation", to see the draft, the check and the request for approval

It prints what the manager said and did each turn, and then checks the parts a program can:
the manager read the code before answering, a draft exists and passes `org spec check`, nothing
was approved, and nothing outside the desk changed. Whether the pushback is any good is for a
person to judge from the printed replies; that is the point of running it.

With nobody to answer permission prompts, anything that would ask is refused, so this also shows
the manager cannot approve its own spec.
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from org import manager, spec  # noqa: E402
from org.ledger import Ledger  # noqa: E402
from org.util import write_json  # noqa: E402

REQUEST = """`org status` is slow when a few engineers are running, because it syncs every machine one at a
time before printing. Make it fast. The project is "org" and its checkout is at {repo}."""
REPLY = """Go with what you recommend, and decide the open points yourself with sensible defaults. Keep it small:
cap it at $5 and one hour, delivery `branch`. Write the spec and show me what I would be approving."""


def turn(claude, ledger, env, session, text, first, model, effort):
    cmd = [claude, "-p", "--output-format", "stream-json", "--verbose", "--model", model, "--effort", effort,
           "--append-system-prompt", manager.persona(ledger, with_sandbox=manager.sandboxed(ledger)),
           "--settings", json.dumps(manager.settings(ledger, "talk")),
           "--permission-prompts", "none"]
    cmd += ["--session-id", session] if first else ["--resume", session]
    p = subprocess.run(cmd, input=text.encode(), capture_output=True, cwd=str(ledger.desk), env=env, timeout=1500)
    calls, result = [], {}
    for raw in p.stdout.splitlines():
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if ev.get("type") == "assistant" and not ev.get("parent_tool_use_id"):
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    inp = block.get("input") or {}
                    calls.append((block.get("name"), str(inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("query") or "")[:140]))
        elif ev.get("type") == "result":
            result = ev
    return calls, result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--keep", action="store_true", help="leave the temporary ledger in place")
    args = ap.parse_args()

    ledger = Ledger(tempfile.mkdtemp(prefix="org-refine-"))
    ledger.init()
    write_json(ledger.projects_path, {"org": {"repos": [], "path": str(ROOT)}})
    env = manager.launch_env(ledger)
    claude = shutil.which(ledger.config().get("claude_bin") or "claude", path=env["PATH"])
    if not claude:
        print("the claude CLI is not on PATH")
        return 2
    session, total = str(uuid.uuid4()), 0.0
    print("ledger: %s\n" % ledger.root)
    all_calls = []
    for n, text in enumerate([REQUEST.format(repo=ROOT), REPLY], 1):
        print("=" * 100 + "\nYOU (turn %d): %s\n" % (n, text))
        calls, result = turn(claude, ledger, env, session, text, n == 1, args.model, args.effort)
        all_calls.append(calls)
        cost = sum((m.get("costUSD") or 0) for m in (result.get("model_usage_since_last_result") or {}).values())
        total += cost
        print("MANAGER did (%d tool calls, $%.2f):" % (len(calls), cost))
        for name, what in calls:
            print("  %-10s %s" % (name, what))
        denied = result.get("permission_denials") or []
        for d in denied:
            print("  REFUSED BY THE HARNESS: %s %s" % (d.get("tool_name"), json.dumps(d.get("tool_input"))[:140]))
        print("\nMANAGER said:\n%s\n" % (result.get("result") or "(nothing)"))

    drafts = ledger.draft_ids()
    read_code = any("sync" in what and name in ("Read", "Grep", "Bash") for name, what in all_calls[0])
    errors = spec.check(ledger.draft_path(drafts[0]).read_text())[0] if drafts else ["no draft"]
    outside = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"], capture_output=True, text=True).stdout.strip()
    checks = [
        ("read the sync code before answering", read_code),
        ("wrote a draft in the desk", bool(drafts)),
        ("the draft passes `org spec check`", not errors),
        ("nothing was approved", not ledger.approved_ids()),
        ("no run was dispatched", not ledger.run_ids()),
    ]
    print("=" * 100)
    ok = True
    for name, passed in checks:
        ok = ok and passed
        print("  %s  %s" % ("pass" if passed else "FAIL", name))
    if errors and drafts:
        print("  check errors: %s" % errors)
    print("\nrepository working tree after the session (should hold only your own changes):\n%s" % (outside or "  clean"))
    if drafts:
        print("\n----- the draft -----\n%s" % ledger.draft_path(drafts[0]).read_text())
    print("total cost $%.2f" % total)
    if not args.keep:
        shutil.rmtree(ledger.root, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
