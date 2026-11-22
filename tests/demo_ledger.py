#!/usr/bin/env python3
"""Write a ledger full of made-up runs in every state, for looking at the status page.

    python3 tests/demo_ledger.py /tmp/crew-demo
    crew --home /tmp/crew-demo status
    crew --home /tmp/crew-demo dash

Nothing here runs an engineer or contacts a machine: each run is marked as fully synced, so the
page and `crew status` show exactly what this file wrote. Times are relative to now.
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crew import spec  # noqa: E402
from crew.ledger import Ledger  # noqa: E402
from crew.util import write_json, write_text  # noqa: E402

NOW = time.time()
MIN, HOUR = 60.0, 3600.0
RESET = NOW + 2 * 86400 + 5 * HOUR

GOOD_DRAFT = """---
title: Per-key rate limits for the public API
project: acme
repos: gasblaster/acme-api, gasblaster/acme-gateway
delivery: pr
max_cost_usd: 60
max_hours: 8
---

## Intent

Stop one noisy integration from degrading the API for everyone else, without making well-behaved
callers think about limits at all.

## Context

The gateway has a global limit of 2,000 requests a second and no per-key accounting. Last month's
incident was a single key at 1,400 requests a second. `make test` passes on both repositories.

## Requirements

- R1 (MUST): each API key is limited to a configurable rate, default 50 requests a second.
- R2 (MUST): a limited request gets 429 with a `Retry-After` header.
- R3 (SHOULD): the dashboard shows the ten keys closest to their limit.

## Out of scope

- Billing by usage.

## Acceptance criteria

- AC1: `make test` passes in both repositories.
- AC2: `scripts/hammer.sh --key test --rps 80` reports at least 30% of responses as 429.

## Constraints

The gateway change lands before the API change. No new datastore: use the existing Redis.

## Alternatives considered

- Limiting at the load balancer: cannot see API keys.

## Open questions

None
"""


def milestone(mid, title, est_min, est_usd, started=None, took=None, outcome=None, cost=None):
    m = {"id": mid, "title": title, "est_minutes": est_min, "est_cost_usd": est_usd, "started_at": None, "ended_at": None,
         "outcome": outcome, "cost_usd": cost}
    if started is not None:
        m["started_at"] = NOW - started
        if took is not None:
            m["ended_at"] = NOW - started + took
    return m


def run(ledger, run_id, title, state, started_ago, machine, **kw):
    spec_id = run_id.rsplit("-r", 1)[0]
    ms = kw.get("milestones", [])
    finished = kw.get("finished_ago")
    cost = kw.get("cost", 0.0)
    status = {
        "run_id": run_id, "spec_id": spec_id, "title": title, "state": state, "phase": kw.get("phase", state),
        "detail": kw.get("detail", ""), "started_at": NOW - started_ago,
        "finished_at": (NOW - finished) if finished is not None else None,
        "elapsed_s": started_ago - (finished or 0), "heartbeat_at": NOW, "pid": 4242, "model": kw.get("model", "fable"),
        "cost": {"usd": cost, "confirmed_usd": cost, "live_usd": 0.0, "cap_usd": kw.get("cap"), "by_model": {}},
        "estimate": kw.get("estimate"),
        "forecast": {"eta_at": NOW + kw["eta_in"], "cost_usd": kw.get("heading_for"), "basis": kw.get("basis", "the plan")} if "eta_in" in kw else None,
        "progress": {"done": len([m for m in ms if m["ended_at"]]), "total": len(ms),
                     "current": next((m["title"] for m in ms if m["started_at"] and not m["ended_at"]), None), "milestones": ms},
        "activity": {"at": NOW - kw.get("activity_ago", 5), "text": kw["activity"], "sub": False} if kw.get("activity") else None,
        "agents_running": len(kw.get("agents", [])), "agents": kw.get("agents", []),
        "rate_limit": {"status": "allowed", "type": "seven_day", "resets_at": RESET, "at": NOW - 40,
                       "windows": {"seven_day": {"utilization": 0.62, "resets_at": RESET, "status": "allowed"}}},
        "waiting_until": (NOW + kw["resumes_in"]) if "resumes_in" in kw else None,
        "caps": {}, "result": kw.get("result"), "end_reason": kw.get("end_reason"), "warnings": kw.get("warnings", []),
        "turns": 3, "retries": kw.get("retries", 0),
    }
    mirror = ledger.mirror_dir(run_id)
    write_json(mirror / "status.json", status)
    if kw.get("plan"):
        write_text(mirror / "plan.frozen.md", kw["plan"])
        write_text(mirror / "plan.md", kw["plan"])
    if kw.get("report"):
        write_text(mirror / "report.md", kw["report"])
    record = {"run_id": run_id, "spec_id": spec_id, "spec_version": 1, "title": title, "machine": machine, "state": state,
              "remote_dir": "$HOME/crew-runs/" + run_id, "dispatched_at": NOW - started_ago - 20, "last_sync_at": NOW,
              "reachable": True, "alive": finished is None, "heartbeat_age_s": 3.0, "final": True, "events_offset": 0}
    record.update(kw.get("record", {}))
    ledger.save_run(run_id, record)


def build(root) -> Ledger:
    ledger = Ledger(root)
    ledger.init()
    write_json(ledger.config_path, {"budget": {"usd_per_weekly_pct": 4.0}})
    write_json(ledger.budget_path, {"weekly": {"at": NOW - 40, "utilization": 0.62, "resets_at": RESET, "status": "allowed"}})
    orb = lambda name: {"kind": "orb", "name": "eng-" + name}  # noqa: E731
    plan = "# Plan\n\n## Approach\n\nSettle the export interface first, then fan out.\n\n## Milestones\n\n| id | title | minutes | cost |\n|---|---|---|---|\n| M1 | Interfaces | 15 | $1.20 |\n| M2 | API endpoint | 25 | $2.40 |\n\n## Risks\n\n- The row serializer is shared with the PDF export: change it once, in M1.\n"

    run(ledger, "007-csv-export-r1", "CSV export for the table view", "executing", 72 * MIN, orb("007-csv-export-r1"),
        detail="milestone 3/5: Web button and download flow", cost=6.20, cap=40.0, estimate={"wall_minutes": 100, "cost_usd": 9.0},
        eta_in=28 * MIN, heading_for=9.8, basis="pace of 2 finished milestone(s)", plan=plan,
        activity="Bash: bazel test //web/export/... --test_output=errors", activity_ago=12,
        agents=["web: button and menu wiring", "web: download progress toast", "api: content-disposition tests"],
        milestones=[milestone("M1", "Interfaces and shared row serializer", 15, 1.2, 60 * MIN, 17 * MIN, "done", 1.35),
                    milestone("M2", "API endpoint with streaming", 25, 2.4, 43 * MIN, 31 * MIN, "done", 3.1),
                    milestone("M3", "Web button and download flow", 30, 3.0, 12 * MIN),
                    milestone("M4", "Filter-aware export", 20, 1.6), milestone("M5", "Integration tests and PRs", 10, 0.8)])
    run(ledger, "008-auth-refactor-r1", "Move session handling behind an interface", "planning", 8 * MIN, orb("008-auth-refactor-r1"),
        detail="researching and planning", cost=0.90, cap=120.0, activity="Read: services/auth/session.py", activity_ago=3)
    run(ledger, "009-search-index-r1", "Incremental search index rebuilds", "waiting", 3 * HOUR, {"kind": "ssh", "name": "bigbox"},
        phase="executing", detail="usage limit reached; resuming when it resets", cost=31.4, cap=80.0, estimate={"wall_minutes": 240, "cost_usd": 44.0},
        resumes_in=2 * HOUR + 14 * MIN, model="opus",
        milestones=[milestone("M1", "Change-log reader", 60, 10, 170 * MIN, 70 * MIN, "done", 12.9),
                    milestone("M2", "Segment merge", 90, 18, 100 * MIN), milestone("M3", "Backfill", 60, 11), milestone("M4", "Cutover", 30, 5)])
    run(ledger, "010-flaky-tests-r1", "Deflake the checkout test suite", "executing", 40 * MIN, orb("010-flaky-tests-r1"),
        detail="milestone 1/2: Reproduce and classify", cost=3.1, estimate={"wall_minutes": 90, "cost_usd": 7.0}, eta_in=55 * MIN,
        activity="Bash: for i in $(seq 50); do pytest tests/checkout -x -q; done", activity_ago=9 * MIN,
        milestones=[milestone("M1", "Reproduce and classify", 40, 3.0, 30 * MIN), milestone("M2", "Fix and verify 200 clean runs", 50, 4.0)],
        record={"reachable": False, "last_error": "orb:eng-010-flaky-tests-r1: no answer within 45s"})
    run(ledger, "006-readme-refresh-r1", "Rewrite the README around the new CLI", "done", 5 * HOUR, orb("006-readme-refresh-r1"),
        finished_ago=5 * HOUR - 22 * MIN, cost=1.10, estimate={"wall_minutes": 30, "cost_usd": 2.0}, model="sonnet", plan=plan,
        report="# Report\n\n## Outcome\n\ndone. The README now leads with install and the three commands people use.\n\n## Forecast against actual\n\nPlanned 30 minutes and $2.00; took 22 minutes and $1.10.\n",
        milestones=[milestone("M1", "Outline from the CLI help", 8, 0.5, 290 * MIN, 6 * MIN, "done", 0.3),
                    milestone("M2", "Write and check every command", 15, 1.0, 284 * MIN, 11 * MIN, "done", 0.6),
                    milestone("M3", "Open the pull request", 5, 0.3, 273 * MIN, 3 * MIN, "done", 0.2)],
        result={"status": "done", "msg": "README rewritten; every command in it was run and its output checked. PR gasblaster/acme-cli#41.",
                "acceptance": [{"id": "AC%d" % i, "verdict": "pass", "evidence": "ran"} for i in (1, 2, 3)]})
    run(ledger, "005-payments-webhooks-r1", "Retry and dead-letter queue for payment webhooks", "partial", 26 * HOUR, orb("005-payments-webhooks-r1"),
        finished_ago=22 * HOUR, cost=38.6, cap=40.0, estimate={"wall_minutes": 150, "cost_usd": 24.0}, plan=plan, report="# Report\n\n## Outcome\n\npartial.\n",
        milestones=[milestone("M1", "Retry policy and schema", 30, 5, 26 * HOUR, 41 * MIN, "done", 7.9),
                    milestone("M2", "Worker and backoff", 50, 9, 25 * HOUR, 95 * MIN, "done", 16.2),
                    milestone("M3", "Dead-letter queue and replay tool", 40, 7, 23.4 * HOUR, 70 * MIN, "partial", 12.8),
                    milestone("M4", "Load test at 500 events a second", 30, 3)],
        warnings=["/home/eng/work/005-payments-webhooks-r1/acme-api still has 0 uncommitted file(s) and 2 unpushed commit(s)"],
        end_reason="the cost cap of $40.00 was reached",
        result={"status": "partial", "msg": "Retries and the dead-letter queue are delivered and tested; the replay tool is unfinished and the load test was not run. The cost cap was reached during M3.",
                "acceptance": [{"id": "AC1", "verdict": "pass", "evidence": ""}, {"id": "AC2", "verdict": "pass", "evidence": ""},
                               {"id": "AC3", "verdict": "fail", "evidence": ""}, {"id": "AC4", "verdict": "unverified", "evidence": ""}]})
    run(ledger, "004-ios-widgets-r1", "Home-screen widgets for the iOS app", "failed", 30 * HOUR, {"kind": "ssh", "name": "bigbox"},
        finished_ago=30 * HOUR - 14 * MIN, cost=2.3, cap=50.0,
        end_reason="the plan's verdict was infeasible: building for iOS needs Xcode, and the machine is Linux",
        result={"status": "fail", "msg": "Infeasible on this machine: WidgetKit targets need Xcode on macOS, and no macOS machine is configured. Nothing was changed.",
                "acceptance": [{"id": "AC1", "verdict": "unverified", "evidence": ""}]})

    ready = spec.new_draft(ledger, "Per-key rate limits for the public API", "acme")
    ledger.draft_path(ready).write_text(GOOD_DRAFT)
    spec.new_draft(ledger, "Make onboarding feel faster")
    return ledger


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    print("demo ledger written to %s" % build(sys.argv[1]).root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
