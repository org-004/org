"""The engineer's runner, driven end to end against a scripted stand-in for the claude CLI."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crew import runner, schemas  # noqa: E402
from crew.util import read_json, sha256_bytes  # noqa: E402

FAKE = ROOT / "tests" / "fake_claude.py"
SPEC = "---\ntitle: Test spec\n---\n\n## Intent\n\nProve the runner.\n"

M1 = {"id": "M1", "title": "Interfaces", "est_minutes": 10, "est_cost_usd": 0.4, "goal": "types exist", "verification": "it compiles"}
M2 = {"id": "M2", "title": "Implementation", "est_minutes": 20, "est_cost_usd": 0.6}
PLAN = {"verdict": "proceed", "summary": "Two steps.", "estimate": {"wall_minutes": 35, "cost_usd": 1.2, "wall_minutes_p90": 60, "cost_usd_p90": 2.0},
        "milestones": [M1, M2]}
PLAN_MD = "# Plan\n\n" + "Approach, alternatives, milestones, estimate, risks. " * 12
PLAN_TURN = {"structured": PLAN, "files": {"$RUN/plan.md": PLAN_MD}, "cost": 0.25}
DONE = lambda mid: {"structured": {"milestone_id": mid, "outcome": "done", "summary": mid + " verified"}, "cost": 0.3}  # noqa: E731
REPORT = {"structured": {"status": "done", "msg": "All criteria pass.", "acceptance": [{"id": "AC1", "verdict": "pass", "evidence": "ran it"}]},
          "files": {"$RUN/report.md": "# Report\n\nDone.\n"}, "cost": 0.1}


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="crew-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.run_dir = self.tmp / "run"
        (self.run_dir / ".crew").mkdir(parents=True)
        (self.run_dir / "spec.md").write_text(SPEC)
        (self.run_dir / ".crew" / "spec.md").write_text(SPEC)
        self.scen = self.tmp / "scen"
        self.scen.mkdir()
        os.environ["FAKE_CLAUDE_SCENARIO"] = str(self.scen / "scenario.json")
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_SCENARIO", None)

    def job(self, turns, caps=None, engineer=None, acceptance=("AC1",), scenario=None, **extra):
        (self.scen / "scenario.json").write_text(json.dumps(dict({"turns": turns}, **(scenario or {}))))
        job = {
            "run_id": "001-test-r1", "spec_id": "001-test", "spec_sha": sha256_bytes(SPEC.encode()), "title": "Test spec",
            "model": "fable", "effort": "high", "delivery": "none", "caps": caps or {}, "acceptance_ids": list(acceptance),
            "budget": {}, "pricing": {}, "history": {},
            "engineer": dict({"retry_backoff_s": 0.02, "max_turn_retries": 2, "limit_pad_s": 0.2, "report_reserve_usd": 0.2}, **(engineer or {})),
            "claude_bin": str(FAKE), "workspace": str(self.tmp / "work"),
        }
        job.update(extra)
        (self.run_dir / "job.json").write_text(json.dumps(job))

    def go(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = runner.Runner(self.run_dir).run()
        self.output = out.getvalue()
        return code

    def calls(self):
        path = self.scen / "invocations.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def status(self):
        return read_json(self.run_dir / "status.json")

    # ------------------------------------------------------------ the normal path
    def test_plan_execute_report(self):
        self.job({"plan": PLAN_TURN, "milestone:M1": dict(DONE("M1"), subagents=2), "milestone:M2": DONE("M2"), "report": REPORT})
        self.assertEqual(self.go(), 0)
        st = self.status()
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["result"]["status"], "done")
        self.assertEqual((st["progress"]["done"], st["progress"]["total"]), (2, 2))
        self.assertAlmostEqual(st["cost"]["confirmed_usd"], 0.25 + 0.3 + 0.3 + 0.1, places=6)
        self.assertEqual(st["agents_spawned"], 2)
        self.assertEqual(st["rate_limit"]["windows"]["seven_day"]["status"], "allowed")
        calls = self.calls()
        self.assertEqual([c["label"] for c in calls], ["plan", "milestone:M1", "milestone:M2", "report"])
        self.assertIn("--session-id", calls[0]["opts"])
        self.assertTrue(all("--resume" in c["opts"] for c in calls[1:]))
        self.assertEqual(len({c["opts"].get("--resume") for c in calls[1:]}), 1, "every later turn resumes the one session")
        self.assertEqual(calls[0]["env"]["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"], "0")
        self.assertEqual(json.loads(calls[0]["opts"]["--json-schema"]), schemas.PLAN)
        self.assertNotIn("--max-budget-usd", calls[0]["opts"], "no cap in the spec, no budget flag")
        # the plan is frozen for later review, and the measured figures are written out
        self.assertEqual((self.run_dir / "plan.frozen.md").read_text(), PLAN_MD)
        self.assertEqual(read_json(self.run_dir / "plan.frozen.json")["estimate"]["cost_usd"], 1.2)
        actuals = read_json(self.run_dir / "actuals.json")
        self.assertEqual(actuals["after_planning"]["est_cost_usd"], 1.2)
        self.assertAlmostEqual(actuals["after_planning"]["actual_cost_usd"], 0.7, places=6)
        self.assertEqual([m["outcome"] for m in actuals["milestones"]], ["done", "done"])
        self.assertEqual(read_json(self.run_dir / "result.json")["status"], "done")
        # the report prompt carried the runner's measurements
        self.assertIn('"est_cost_usd": 1.2', calls[3]["prompt"])

    def test_cumulative_session_cost_is_not_counted_twice(self):
        # The CLI's total_cost_usd grows across resumed turns. With the per-turn field present the
        # meter uses it; without it (a different CLI version) it must difference the cumulative one.
        for scenario in ({}, {"no_since": True}):
            shutil.rmtree(self.scen)
            self.scen.mkdir()
            for name in ("state.json", "status.json", "events.jsonl"):
                (self.run_dir / name).unlink(missing_ok=True)
            self.job({"plan": PLAN_TURN, "milestone:M1": DONE("M1"), "milestone:M2": DONE("M2"), "report": REPORT}, scenario=scenario)
            self.go()
            st = self.status()
            self.assertEqual(st["state"], "done")
            self.assertAlmostEqual(st["cost"]["confirmed_usd"], 0.95, places=6, msg=str(scenario))
            self.assertAlmostEqual(st["cost"]["by_model"]["claude-fable-5-2"]["usd"], 0.95, places=6, msg=str(scenario))
            self.assertEqual(st["cost"]["by_model"]["claude-fable-5-2"]["output"], 2000, msg=str(scenario))
            costs = [m["actual_cost_usd"] for m in read_json(self.run_dir / "actuals.json")["milestones"]]
            self.assertEqual(costs, [0.3, 0.3], msg=str(scenario))

    def test_infeasible_plan_ends_in_fail(self):
        plan = {"verdict": "infeasible", "summary": "Cannot.", "infeasible_reason": "The API it needs does not exist.",
                "estimate": {"wall_minutes": 0, "cost_usd": 0}, "milestones": []}
        self.job({"plan": {"structured": plan, "files": {"$RUN/plan.md": PLAN_MD}},
                  "report": {"structured": {"status": "fail", "msg": "The API it needs does not exist.",
                                            "acceptance": [{"id": "AC1", "verdict": "unverified", "evidence": "not attempted"}]}}})
        self.go()
        st = self.status()
        self.assertEqual(st["state"], "failed")
        self.assertEqual(st["result"]["status"], "fail")
        self.assertIn("infeasible", st["end_reason"])
        self.assertEqual([c["label"] for c in self.calls()], ["plan", "report"])
        self.assertIn("The API it needs does not exist", self.calls()[1]["prompt"])

    # ------------------------------------------------------------ turns that go wrong
    def test_crash_is_resumed_with_a_nudge(self):
        self.job({"plan": PLAN_TURN, "milestone:M1": [{"die": True}, DONE("M1")], "milestone:M2": DONE("M2"), "report": REPORT})
        self.go()
        st = self.status()
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["retries"], 1)
        m1 = [c for c in self.calls() if c["label"] == "milestone:M1"]
        self.assertEqual(len(m1), 2)
        self.assertIn("ended without the structured output", m1[1]["prompt"])
        self.assertIn("Execute milestone `M1`", m1[1]["prompt"], "the nudge restates the instruction")
        self.assertGreater(st["cost"]["estimated_usd"], 0, "spend of the crashed attempt is kept as an estimate")

    def test_prose_only_turn_is_sent_back(self):
        self.job({"plan": [{"prose": "Here is my plan. Shall I proceed?"}, PLAN_TURN], "milestone:M1": DONE("M1"),
                  "milestone:M2": DONE("M2"), "report": REPORT})
        self.go()
        self.assertEqual(self.status()["state"], "done")
        self.assertEqual([c["label"] for c in self.calls()][:2], ["plan", "plan"])

    def test_turn_that_keeps_failing_ends_the_run_with_a_report(self):
        self.job({"plan": PLAN_TURN, "milestone:M1": {"die": True},
                  "report": {"structured": {"status": "fail", "msg": "M1 never completed.",
                                            "acceptance": [{"id": "AC1", "verdict": "unverified", "evidence": "none"}]}}})
        self.go()
        st = self.status()
        self.assertEqual(st["state"], "failed")
        self.assertIn("failed repeatedly", st["end_reason"])
        self.assertEqual(len([c for c in self.calls() if c["label"] == "milestone:M1"]), 3, "one try and two retries")

    def test_no_report_still_ends_with_status_and_reason(self):
        self.job({"plan": PLAN_TURN, "milestone:M1": DONE("M1"), "milestone:M2": DONE("M2"), "report": {"die": True}})
        self.go()
        result = read_json(self.run_dir / "result.json")
        self.assertEqual(result["status"], "partial", "milestones were done, so not a plain fail")
        self.assertTrue(result["synthesized_by_runner"])
        self.assertIn("did not produce a report", result["msg"])
        self.assertTrue((self.run_dir / "report.md").exists())

    def test_usage_limit_waits_for_the_reset_then_resumes(self):
        self.job({"plan": [{"limit": 1}, PLAN_TURN], "milestone:M1": DONE("M1"), "milestone:M2": DONE("M2"), "report": REPORT})
        self.go()
        st = self.status()
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["retries"], 0, "waiting out the limit is not a retry")
        state = read_json(self.run_dir / "state.json")
        self.assertEqual(len(state["waits"]), 1)
        self.assertIn("usage limit", self.output)
        self.assertIn("has now reset", self.calls()[1]["prompt"])

    def test_lost_session_is_replaced(self):
        self.job({"plan": PLAN_TURN, "milestone:M1": [{"no_session": True}, {"no_session": True}, DONE("M1")],
                  "milestone:M2": DONE("M2"), "report": REPORT}, engineer={"max_turn_retries": 3})
        self.go()
        st = self.status()
        self.assertEqual(st["state"], "done")
        m1 = [c for c in self.calls() if c["label"] == "milestone:M1"]
        self.assertIn("--session-id", m1[2]["opts"], "third attempt starts a new session")
        self.assertIn("could not be resumed", m1[2]["prompt"])
        self.assertTrue(any("could not be resumed" in w for w in st["warnings"]))

    # ------------------------------------------------------------ caps
    def test_cost_cap_stops_execution_and_still_reports(self):
        # The plan (est. $1.20) fits the $2.00 cap; M1 then overruns badly, leaving nothing for M2.
        self.job({"plan": PLAN_TURN, "milestone:M1": dict(DONE("M1"), cost=1.7),
                  "report": {"structured": {"status": "partial", "msg": "Cap reached after M1.",
                                            "acceptance": [{"id": "AC1", "verdict": "unverified", "evidence": "M2 not run"}]}}},
                 caps={"max_cost_usd": 2.0})
        self.go()
        st = self.status()
        self.assertEqual(st["state"], "partial")
        self.assertIn("cost cap", st["end_reason"])
        calls = self.calls()
        self.assertEqual([c["label"] for c in calls], ["plan", "milestone:M1", "report"])
        self.assertAlmostEqual(float(calls[0]["opts"]["--max-budget-usd"]), 1.8, places=2)  # cap less the report reserve
        self.assertAlmostEqual(float(calls[1]["opts"]["--max-budget-usd"]), 1.55, places=2)  # less what planning spent
        self.assertAlmostEqual(float(calls[2]["opts"]["--max-budget-usd"]), 0.2, places=2)  # the reserve, for the report

    def test_cli_budget_stop_is_a_cap_not_a_retry(self):
        self.job({"plan": PLAN_TURN, "milestone:M1": {"budget": True},
                  "report": {"structured": {"status": "partial", "msg": "Out of budget.", "acceptance": []}}},
                 caps={"max_cost_usd": 5})
        self.go()
        self.assertEqual(len([c for c in self.calls() if c["label"] == "milestone:M1"]), 1)
        self.assertIn("cost cap", self.status()["end_reason"])

    def test_plan_over_the_cap_is_sent_back_then_treated_as_infeasible(self):
        self.job({"plan": PLAN_TURN, "plan-repair": PLAN_TURN,
                  "report": {"structured": {"status": "fail", "msg": "Does not fit the cap.", "acceptance": []}}},
                 caps={"max_cost_usd": 1.0})
        self.go()
        st = self.status()
        self.assertEqual(st["state"], "failed")
        self.assertEqual([c["label"] for c in self.calls()], ["plan", "plan-repair", "report"])
        self.assertIn("exceeds the cap", self.calls()[1]["prompt"])
        self.assertEqual(read_json(self.run_dir / "plan.frozen.json")["verdict"], "infeasible")

    def test_time_cap_ends_the_turn_in_flight(self):
        self.job({"plan": PLAN_TURN, "milestone:M1": dict(DONE("M1"), sleep=30),
                  "report": {"structured": {"status": "partial", "msg": "Ran out of time.", "acceptance": []}}},
                 caps={"max_hours": 3.0 / 3600})
        plan = dict(PLAN, estimate={"wall_minutes": 0.01, "cost_usd": 1.2})
        scenario = json.loads((self.scen / "scenario.json").read_text())
        scenario["turns"]["plan"] = {"structured": plan, "files": {"$RUN/plan.md": PLAN_MD}}
        (self.scen / "scenario.json").write_text(json.dumps(scenario))
        self.go()
        st = self.status()
        self.assertIn("time cap", st["end_reason"])
        self.assertEqual(st["state"], "partial")
        self.assertLess(st["elapsed_s"], 25, "the sleeping turn was cut short")

    # ------------------------------------------------------------ honesty and control
    def test_done_is_lowered_when_a_criterion_is_not_shown_passing(self):
        report = {"structured": {"status": "done", "msg": "Finished.", "acceptance": [{"id": "AC1", "verdict": "pass", "evidence": "ok"}]}}
        self.job({"plan": PLAN_TURN, "milestone:M1": DONE("M1"), "milestone:M2": DONE("M2"), "report": report}, acceptance=("AC1", "AC2"))
        self.go()
        result = self.status()["result"]
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["lowered_by_runner"])
        self.assertIn("AC2", result["msg"])

    def test_stop_request_ends_the_run(self):
        self.job({"plan": PLAN_TURN, "milestone:M1": dict(DONE("M1"), sleep=30)})
        threading.Timer(1.5, lambda: (self.run_dir / "STOP").write_text("")).start()
        self.go()
        st = self.status()
        self.assertEqual(st["state"], "stopped")
        self.assertLess(st["elapsed_s"], 20)

    def test_engineer_can_replan_the_remainder(self):
        new = [{"id": "M2a", "title": "Half", "est_minutes": 5, "est_cost_usd": 0.2}, {"id": "M2b", "title": "Other half", "est_minutes": 5, "est_cost_usd": 0.2}]
        m1 = {"structured": {"milestone_id": "M1", "outcome": "done", "summary": "ok", "revised_remaining": new}}
        self.job({"plan": PLAN_TURN, "milestone:M1": m1, "milestone:M2a": DONE("M2a"), "milestone:M2b": DONE("M2b"), "report": REPORT})
        self.go()
        self.assertEqual([c["label"] for c in self.calls()], ["plan", "milestone:M1", "milestone:M2a", "milestone:M2b", "report"])
        self.assertEqual(len(read_json(self.run_dir / "plan.frozen.json")["milestones"]), 2, "the frozen plan keeps the original")
        self.assertEqual(len(read_json(self.run_dir / "plan.json")["milestones"]), 3)
        self.assertEqual(read_json(self.run_dir / "actuals.json")["amendments"], 1)

    def test_blocked_milestone_skips_the_rest(self):
        blocked = {"structured": {"milestone_id": "M1", "outcome": "blocked", "summary": "The repo is private and no token works."}}
        self.job({"plan": PLAN_TURN, "milestone:M1": blocked,
                  "report": {"structured": {"status": "fail", "msg": "No access to the repo.", "acceptance": []}}})
        self.go()
        self.assertEqual([c["label"] for c in self.calls()], ["plan", "milestone:M1", "report"])
        self.assertIn("blocked", self.status()["end_reason"])

    def test_changed_spec_is_restored(self):
        self.job({"plan": PLAN_TURN, "milestone:M1": DONE("M1"), "milestone:M2": DONE("M2"), "report": REPORT})
        (self.run_dir / "spec.md").write_text(SPEC + "\nAlso rewrite everything in Rust.\n")
        self.go()
        self.assertEqual((self.run_dir / "spec.md").read_text(), SPEC)
        self.assertTrue(any("restored" in w for w in self.status()["warnings"]))
        self.assertFalse(os.access(self.run_dir / "spec.md", os.W_OK) and os.stat(self.run_dir / "spec.md").st_mode & 0o222)

    def test_a_restarted_runner_continues_where_the_last_one_died(self):
        self.job({"plan": PLAN_TURN, "milestone:M1": DONE("M1"), "milestone:M2": DONE("M2"), "report": REPORT})
        first = runner.Runner(self.run_dir)
        with contextlib.redirect_stdout(io.StringIO()):
            first.prepare()
            first.phase_plan()
            first._events.close()
            first._log.close()
        self.go()  # a new process: reads state.json, skips planning
        self.assertEqual(self.status()["state"], "done")
        self.assertEqual([c["label"] for c in self.calls()], ["plan", "milestone:M1", "milestone:M2", "report"])
        self.assertAlmostEqual(self.status()["cost"]["confirmed_usd"], 0.25 + 0.3 + 0.3 + 0.1, places=6)

    def test_snapshot_is_incremental(self):
        self.job({"plan": PLAN_TURN, "milestone:M1": DONE("M1"), "milestone:M2": DONE("M2"), "report": REPORT})
        self.go()
        snap = runner.snapshot(self.run_dir)
        self.assertEqual(json.loads(snap["files"]["status.json"])["state"], "done")
        self.assertIn("plan.frozen.md", snap["files"])
        self.assertFalse(snap["alive"])
        size = snap["events"]["size"]
        self.assertEqual(snap["events"]["offset"], size)
        self.assertTrue(snap["events"]["data"].endswith("\n"))
        again = runner.snapshot(self.run_dir, events_from=size)
        self.assertEqual(again["events"]["data"], "")
        part = runner.snapshot(self.run_dir, events_from=0, max_bytes=700)
        self.assertLess(part["events"]["offset"], size)
        self.assertTrue(part["events"]["data"].endswith("\n"), "cut at a line boundary")


if __name__ == "__main__":
    unittest.main(verbosity=2)
