#!/usr/bin/env python3
"""End-to-end smoke test with a real engineer. Spends real tokens (about a dollar on sonnet).

    python3 tests/e2e.py                      an engineer on this machine (use inside a box)
    python3 tests/e2e.py --machine orb        from the Mac: clone `base`, run there
    python3 tests/e2e.py --machine ssh:gpu    a host from ~/.crew/config.json

It approves a small, self-contained spec (build a tiny Python package in a repository the engineer
creates on its own machine, so nothing touches GitHub), dispatches it, follows the run to the end
and prints the plan-against-outcome scorecard. Exit status 0 means the engineer reported `done`
with every acceptance criterion passing.

The ledger is a temporary directory unless --home is given, so this never mixes with real work.
"""
import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crew import dispatch, spec, status, sync  # noqa: E402
from crew.ledger import Ledger  # noqa: E402
from crew.util import fmt_clock, fmt_dur, fmt_usd, read_json, read_text, write_json  # noqa: E402

SPEC = """---
title: textkit smoke test
project: textkit
delivery: branch
machine: {machine}
model: {model}
effort: {effort}
max_cost_usd: {max_cost}
max_hours: 1
---

## Intent

Prove the crew pipeline end to end on a job small enough to finish in minutes: a new, tiny Python
package with two text helpers, built in a repository that lives entirely on the engineer's machine.
The helpers matter less than the path: plan, execute, verify, push, report.

## Context

Nothing exists yet. There is no GitHub repository and none should be created. The "remote" is a
bare git repository the engineer creates on its own machine at `~/crew-e2e/{spec_id}/textkit.git`.

## Requirements

- R1 (MUST): create the bare repository `~/crew-e2e/{spec_id}/textkit.git` and a working clone of it
  under the workspace. Its `master` branch holds one initial commit containing only a README.md.
- R2 (MUST): on a branch named `crew/{spec_id}`, add a Python package `textkit` (standard library
  only) exporting `squeeze(text)`, which collapses every run of whitespace to one space and strips
  the ends, and `slugify(text)`, which lowercases the text and replaces every run of characters
  other than a-z and 0-9 with a single hyphen, with no leading or trailing hyphen.
- R3 (MUST): `slugify` returns the empty string for empty input and for input with no letters or
  digits.
- R4 (MUST): unit tests under `tests/` cover R2 and R3 and pass with `python3 -m unittest -q`.
- R5 (MUST): the branch `crew/{spec_id}` is pushed to the bare repository; `master` there is unchanged.
- R6 (SHOULD): `slugify` folds accented Latin letters to their base letter first, so `Crème Brûlée`
  becomes `creme-brulee`.

## Out of scope

- Any GitHub repository, pull request or release.
- Packaging metadata (pyproject.toml), type checking, linting configuration.
- Transliteration of non-Latin scripts.

## Acceptance criteria

Each is run in a fresh clone of `~/crew-e2e/{spec_id}/textkit.git` with `crew/{spec_id}` checked out.

- AC1: `python3 -m unittest -q` exits 0 and runs at least four tests.
- AC2: `python3 -c "from textkit import slugify; print(slugify('Hello, World!'))"` prints `hello-world`.
- AC3: `python3 -c "from textkit import slugify; print(repr(slugify('!!!')))"` prints `''`.
- AC4: `python3 -c "from textkit import squeeze; print(squeeze('  a \\t b  '))"` prints `a b`.
- AC5: `git log --oneline master` in that clone shows exactly one commit.

## Constraints

Standard library only, Python 3.9 or later. No network access is needed. Keep it small: this is a
smoke test, and the plan should be one or two milestones.

## Alternatives considered

- Using an existing GitHub repository: rejected, a smoke test should not leave branches behind.

## Open questions

None
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--machine", default="local", help="local, orb or ssh:<name> (default local)")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--effort", default="low")
    ap.add_argument("--max-cost", type=float, default=5.0)
    ap.add_argument("--base", default=None, help="OrbStack template machine to clone (default from ~/.crew/config.json, else base)")
    ap.add_argument("--home", default=None, help="ledger directory to use (default: a new temporary one)")
    ap.add_argument("--timeout", type=int, default=3600, help="seconds to wait for the run")
    args = ap.parse_args()

    ledger = Ledger(args.home or tempfile.mkdtemp(prefix="crew-e2e-"))
    ledger.init()
    real = read_json(Path.home() / ".crew" / "config.json", {}) or {}
    machines = dict(real.get("machines") or {})
    machines["default"] = args.machine
    machines["local"] = {"enabled": args.machine == "local"}
    if args.base:
        machines["orb"] = dict(machines.get("orb") or {}, base=args.base)
    write_json(ledger.config_path, {"machines": machines})
    print("ledger:  %s" % ledger.root)

    spec_id = spec.new_draft(ledger, "textkit smoke test", "textkit")
    ledger.draft_path(spec_id).write_text(SPEC.format(machine=args.machine, model=args.model, effort=args.effort,
                                                       max_cost=args.max_cost, spec_id=spec_id))
    errors, warnings = spec.check(read_text(ledger.draft_path(spec_id)))
    if errors:
        print("the smoke spec does not pass its own checks:", errors)
        return 2
    meta = spec.approve(ledger, spec_id, spec.draft_sha(ledger, spec_id)[:12])
    print("spec:    %s approved (sha256 %s)" % (spec_id, meta["sha256"][:12]))
    try:
        run_id, rec = dispatch.dispatch(ledger, spec_id, log=lambda m: print("         " + m))
    except dispatch.DispatchError as e:
        print("dispatch failed: %s" % e)
        return 2
    print("run:     %s" % run_id)
    if rec.get("attach"):
        print("watch:   %s" % rec["attach"])
    print("also:    crew --home %s status --watch" % ledger.root)
    print()

    last, end = None, time.time() + args.timeout
    while time.time() < end:
        rec = sync.sync_run(ledger, run_id)
        v = status.run_view(ledger, run_id, None)
        p = v["progress"]
        line = "%-10s %-28s %s  %s  eta %s  %s" % (
            status.state_label(v), (v.get("detail") or "")[:28], fmt_dur(v["elapsed_s"]), fmt_usd(v["cost_usd"]),
            fmt_clock(v["eta_at"]) if v["eta_at"] else "—", (v.get("activity") or "")[:70])
        if line != last:
            print(time.strftime("%H:%M:%S ") + line, flush=True)
            last = line
        if rec.get("final"):
            break
        if v["health"] == "dead":
            print("the runner died; see %s" % (ledger.mirror_dir(run_id) / "runner.log.tail"))
            return 1
        time.sleep(5)
    else:
        print("timed out after %ds; the run continues on its machine" % args.timeout)
        return 1

    print()
    print(status.scorecard(ledger, run_id))
    result = read_json(ledger.mirror_dir(run_id) / "result.json") or {}
    st = read_json(ledger.mirror_dir(run_id) / "status.json") or {}
    for w in st.get("warnings") or []:
        print("warning: " + w)
    return 0 if result.get("status") == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
