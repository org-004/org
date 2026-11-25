#!/usr/bin/env python3
"""Show a finished run's container deleted by a pump, and what the machine measured kept.

    python3 tests/demo_teardown.py

A ledger made by the unit tests' own helpers (tests/test_incus.py, `IncusCase`), a simulated
Incus (tests/fake_incus.py), a scripted `claude` (tests/fake_claude.py) and kernel files of the
demonstration's own, in a temporary directory that is removed at the end. No engineer, model or
machine is real. One run is dispatched and runs to its end; the kernel's files for its container
say what container 009 of the installation said on 2026-11-24 (19 processes killed for memory)
under the limit this ledger gives. Then every command is one the owner types, printed with what
it answered:

  1. `org pump`, which takes the last measurement and deletes the container; the simulated
     Incus's record of the `delete` call is printed after it;
  2. `org status --json --no-sync`, cut down to this run's `machine`, `attach`, `telemetry`,
     `oom_kill` and `warnings`;
  3. `org review <run>` and `org peek <run>`.

The last line is `ok`, or `FAILED: <what was seen>`, and the exit status is 0 only for `ok`.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from org import dispatch  # noqa: E402
from org.util import read_json  # noqa: E402
from test_incus import IncusCase  # noqa: E402

MIB = 1024 * 1024


class Demo(IncusCase):
    def runTest(self):  # the case is only used for its helpers
        pass


def org(case, *args):
    """Run one `org` command against the demonstration's ledger, and print it with its answer."""
    print("$ org %s" % " ".join(args), flush=True)
    p = subprocess.run([sys.executable, "-m", "org", "--home", str(case.ledger.root)] + list(args), cwd=str(ROOT),
                       capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
    return p.stdout + p.stderr


def main() -> int:
    case = Demo()
    case.setUp()
    try:
        run_id, rec = dispatch.dispatch(case.ledger, case.approved(), log=lambda message: None)
        name = rec["machine"]["name"]
        limit = rec["machine"]["limits"]["memory"]
        # What the kernel says of the container while it runs, read by the first pump.
        case.cgroup(name, current=298 * MIB, peak=1900 * MIB, max=int(limit[:-3]) * MIB, pids=57, oom_kill=11, cpu_usec=611000000)
        dispatch.pump(case.ledger)
        case.wait_final(run_id)
        # And at its end, read by the pump that deletes it.
        case.cgroup(name, current=130 * MIB, peak=2219 * MIB, pids=37, oom_kill=19, cpu_usec=908000000)
        print("%s is %s and fully copied; its container %s is %s" % (run_id, case.ledger.run(run_id)["state"], name, case.state(name)))
        before = len(case.calls())
        print(org(case, "pump"))
        new = [c for c in case.calls()[before:] if c[0] in ("list", "delete")]
        print("the simulated Incus was called with:")
        for call in new:
            print("  incus " + " ".join(call))
        print("containers it still holds: %s\n" % ", ".join(sorted(p.name for p in case.root.iterdir() if (p / "home").is_dir())))

        out = org(case, "status", "--json", "--no-sync")
        view = [v for v in json.loads(out)["runs"] if v["run_id"] == run_id][0]
        print("(this run's entry, five of its keys)")
        print(json.dumps({k: view[k] for k in ("machine", "attach", "telemetry", "oom_kill", "warnings")}, indent=2, sort_keys=True) + "\n")
        review = org(case, "review", run_id)
        print(review)
        print(org(case, "peek", run_id))
        lines = [json.loads(x) for x in (case.ledger.run_dir(run_id) / "machine.jsonl").read_text().splitlines()]
        print("%s holds %d measurements; the last has \"last\": %s" % (
            (case.ledger.run_dir(run_id) / "machine.jsonl").relative_to(case.ledger.root), len(lines), json.dumps(lines[-1].get("last"))))

        rec = case.ledger.run(run_id)
        seen = []
        if ["delete", "-q", "-f", name] not in new:
            seen.append("no `incus delete` of %s was recorded" % name)
        if (case.root / name).exists():
            seen.append("the container is still there")
        if not rec.get("machine_removed_at") or view["machine"].get("removed") is not True or view["attach"] is not None:
            seen.append("the view still offers the machine: %r, attach %r" % (view["machine"], view["attach"]))
        t = view.get("telemetry") or {}
        if (t.get("oom_kill"), t.get("memory_peak_bytes"), t.get("samples")) != (19, 2219 * MIB, 2):
            seen.append("the summary is %r" % t)
        if "19 processes killed for memory" not in review or "peak 2219 MiB" not in review:
            seen.append("`org review` does not show the block")
        print("FAILED: " + "; ".join(seen) if seen else "ok")
        return 1 if seen else 0
    finally:
        case.doCleanups()


if __name__ == "__main__":
    sys.exit(main())
