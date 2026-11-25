"""The engineer's runner, driven end to end against a scripted stand-in for the claude CLI."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from org import runner, schemas  # noqa: E402
from org.util import read_json, sha256_bytes  # noqa: E402

# The kernel's count of memory kills is read from $ORG_CGROUP_DIR: this machine's own count may be above zero, and no test may depend on it.
os.environ["ORG_CGROUP_DIR"] = os.path.join(tempfile.gettempdir(), "org-tests-no-cgroup")
# An engineer that runs this suite has a CLAUDE_CODE_SHELL_PREFIX of its own, from its runner, and a
# runner keeps one it finds: the tests of the wrapper start without one.
os.environ.pop("CLAUDE_CODE_SHELL_PREFIX", None)

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
        self.tmp = Path(tempfile.mkdtemp(prefix="org-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.run_dir = self.tmp / "run"
        (self.run_dir / ".org").mkdir(parents=True)
        (self.run_dir / "spec.md").write_text(SPEC)
        (self.run_dir / ".org" / "spec.md").write_text(SPEC)
        self.scen = self.tmp / "scen"
        self.scen.mkdir()
        os.environ["FAKE_CLAUDE_SCENARIO"] = str(self.scen / "scenario.json")
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_SCENARIO", None)
        # Tokens are handed to a machine in $ORG_SECRETS_DIR. Where this suite runs, the default
        # (/run/org) may hold real ones, which no test may read: this run's directory has none.
        was = os.environ.get("ORG_SECRETS_DIR")
        os.environ["ORG_SECRETS_DIR"] = str(self.tmp / "run-org")
        self.addCleanup(lambda: os.environ.pop("ORG_SECRETS_DIR", None) if was is None else os.environ.__setitem__("ORG_SECRETS_DIR", was))

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

    def test_the_engineer_is_told_what_its_machine_has(self):
        for facts, expected in (({"docker": "yes", "cores": "10"}, "It has 10 CPU cores. Docker works here"),
                                ({"docker": "no", "cores": "1"}, "It has 1 CPU core. Docker does not work here"),
                                ({}, "Whether Docker works here was not checked")):
            self.assertTrue(runner.machine_facts(facts).startswith(expected), (facts, runner.machine_facts(facts)))
        self.job({"plan": PLAN_TURN}, facts={"docker": "yes", "cores": "4"})
        r = runner.Runner(self.run_dir)
        r.prepare()
        self.addCleanup(lambda: (r._events.close(), r._log.close()))
        self.assertIn("Docker works here", r.system_prompt)
        self.assertNotIn("{{", r.system_prompt)

    def test_with_a_memory_limit_the_sessions_shell_commands_run_through_a_wrapper_that_marks_them(self):
        self.job({"plan": PLAN_TURN}, facts={"docker": "yes", "cores": "4", "memory_limit": "2219MiB"})
        r = runner.Runner(self.run_dir)
        r.prepare()
        self.addCleanup(lambda: (r._events.close(), r._log.close()))
        wrapper = self.run_dir.resolve() / ".org" / "shell-prefix"
        self.assertEqual(r._env()["CLAUDE_CODE_SHELL_PREFIX"], str(wrapper))
        self.assertEqual(wrapper.read_text(), runner.SHELL_PREFIX)
        self.assertTrue(os.access(wrapper, os.X_OK))
        # Claude Code runs `<the prefix> '<the command line>'`: one argument, which a shell must read.
        p = subprocess.run([str(wrapper), 'read -r line; echo "$line to $WHO"; echo "$#" >&2; cat /proc/self/oom_score_adj 2>/dev/null; exit 7'],
                           input="said", capture_output=True, text=True, env=dict(os.environ, WHO="the command"), timeout=30)
        self.assertEqual((p.returncode, p.stderr, p.stdout.splitlines()[0]), (7, "0\n", "said to the command"),
                         "the command line, its input, its environment and its exit status pass through untouched")
        mine = Path("/proc/self/oom_score_adj")
        if mine.exists():
            self.assertEqual(p.stdout.splitlines()[1:], [str(max(500, int(mine.read_text())))], "what it runs is killed first")
            # A command that is already marked higher is left as it is: the wrapper only ever raises.
            higher = str(min(1000, max(500, int(mine.read_text())) + 100))
            p = subprocess.run(["sh", "-c", 'echo "$1" > /proc/self/oom_score_adj && exec "$0" "cat /proc/self/oom_score_adj"', str(wrapper), higher],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(p.stdout.strip(), higher)
        # A user who chose a prefix of their own keeps it.
        os.environ["CLAUDE_CODE_SHELL_PREFIX"] = "/somebody/elses"
        self.addCleanup(os.environ.pop, "CLAUDE_CODE_SHELL_PREFIX", None)
        self.assertEqual(r._env()["CLAUDE_CODE_SHELL_PREFIX"], "/somebody/elses")

    def test_without_a_memory_limit_the_session_gets_no_wrapper(self):
        self.job({"plan": PLAN_TURN, "milestone:M1": DONE("M1"), "milestone:M2": DONE("M2"), "report": REPORT}, facts={"docker": "yes", "cores": "4"})
        self.assertEqual(self.go(), 0)
        self.assertEqual({c["env"]["CLAUDE_CODE_SHELL_PREFIX"] for c in self.calls()}, {None})
        self.assertFalse((self.run_dir / ".org" / "shell-prefix").exists())

    def test_the_session_is_started_with_the_wrapper_when_the_machine_has_a_limit(self):
        self.job({"plan": PLAN_TURN, "milestone:M1": DONE("M1"), "milestone:M2": DONE("M2"), "report": REPORT}, facts={"memory_limit": "5GiB"})
        self.assertEqual(self.go(), 0)
        self.assertEqual({c["env"]["CLAUDE_CODE_SHELL_PREFIX"] for c in self.calls()}, {str(self.run_dir.resolve() / ".org" / "shell-prefix")})

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

    def test_a_second_runner_refuses_to_start(self):
        # `org resume` on a run whose runner is alive must not put two engineers in one workspace.
        self.job({"plan": PLAN_TURN, "milestone:M1": dict(DONE("M1"), sleep=30)})
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        first = subprocess.Popen([sys.executable, "-m", "org", "engineer", "run", str(self.run_dir)], env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(first.wait)
        self.addCleanup(first.kill)
        deadline = time.time() + 20
        while time.time() < deadline and (self.status() or {}).get("state") != "executing":
            time.sleep(0.2)
        self.assertEqual(self.status()["state"], "executing")
        calls_before = len(self.calls())
        self.assertEqual(self.go(), 3)
        self.assertIn("already has a live runner", self.output)
        self.assertEqual(len(self.calls()), calls_before, "the second runner started no engineer")
        (self.run_dir / "STOP").write_text("")
        first.wait(timeout=30)
        self.assertEqual(self.status()["state"], "stopped")

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

    def test_the_engineer_is_told_how_much_memory_it_has_and_what_a_kill_looks_like(self):
        text = runner.machine_facts({"docker": "yes", "cores": "4", "memory_limit": "2219MiB"})
        for phrase in ("2219MiB of memory", "hard limit", "killed by the kernel", "`Killed`", "`terminated`",
                       "lower its parallelism", "not to run it again unchanged", "/sys/fs/cgroup/memory.events", "`oom_kill`"):
            self.assertIn(phrase, text)
        self.assertLess(text.index("CPU cores"), text.index("2219MiB"))
        self.assertLess(text.index("oom_kill"), text.index("Docker works here"))
        self.assertIn("5GiB of memory", runner.machine_facts({"memory_limit": "5GiB"}))

    def test_without_a_valid_memory_limit_the_engineer_is_told_exactly_what_it_was_before(self):
        base = {"docker": "yes", "cores": "4"}
        expected = runner.machine_facts(base)
        self.assertTrue(expected.startswith("It has 4 CPU cores. Docker works here"))
        for bad in (None, "", "lots", "12", "12MB", "-5GiB", "5 GiB", "5GiB\n", 2219, ["5GiB"]):
            self.assertEqual(runner.machine_facts(dict(base, memory_limit=bad)), expected, bad)

    def test_snapshot_carries_the_kernels_count_of_memory_kills(self):
        self.job({"plan": PLAN_TURN, "milestone:M1": DONE("M1"), "milestone:M2": DONE("M2"), "report": REPORT})
        self.go()
        self.assertIsNone(runner.snapshot(self.run_dir)["oom_kill"], "no memory.events, no count")
        cgroup = Path(tempfile.mkdtemp(prefix="org-cgroup-"))
        self.addCleanup(shutil.rmtree, cgroup, ignore_errors=True)
        self.addCleanup(os.environ.__setitem__, "ORG_CGROUP_DIR", os.environ["ORG_CGROUP_DIR"])
        os.environ["ORG_CGROUP_DIR"] = str(cgroup)
        for text, want in (("low 0\nhigh 0\nmax 3\noom 4\noom_kill 2\noom_group_kill 0\n", 2), ("oom_kill 0\n", 0),
                           ("low 0\noom 1\n", None), ("oom_kill many\n", None), ("", None)):
            (cgroup / "memory.events").write_text(text)
            self.assertEqual(runner.snapshot(self.run_dir)["oom_kill"], want, text)
        (cgroup / "memory.events").unlink()
        (cgroup / "memory.events").mkdir()  # unreadable as a file
        self.assertIsNone(runner.snapshot(self.run_dir)["oom_kill"])

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

    def clone(self, name="proj"):
        """A git clone in the run's workspace, with one commit that is pushed to its origin."""
        origin = self.tmp / (name + "-origin.git")
        repo = self.tmp / "work" / name
        repo.mkdir(parents=True)
        def git(*args):
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"] + list(args), check=True, capture_output=True, timeout=30)
        subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True, timeout=30)
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, timeout=30)
        git("remote", "add", "origin", str(origin))
        (repo / "base.txt").write_text("base\n")
        git("add", ".")
        git("commit", "-m", "base")
        git("push", "origin", "main")
        git("fetch", "origin")
        return repo, git

    def run_with_leftovers(self):
        self.job({"plan": PLAN_TURN, "milestone:M1": DONE("M1"), "milestone:M2": DONE("M2"), "report": REPORT})
        self.assertEqual(self.go(), 0)
        return self.status()

    def test_the_status_names_the_uncommitted_files_and_unpushed_commits_that_were_left(self):
        repo, git = self.clone()
        git("checkout", "-b", "feature-a")
        (repo / "a.txt").write_text("a\n")
        git("add", ".")
        git("commit", "-m", "Add a")
        git("checkout", "-b", "feature-b")
        (repo / "b.txt").write_text("b\n")
        git("add", ".")
        git("commit", "-m", "Add b")
        (repo / "base.txt").write_text("changed\n")
        (repo / "new file.txt").write_text("x\n")
        left = self.run_with_leftovers()["leftover"]
        self.assertEqual(len(left), 1)
        e = left[0]
        self.assertEqual(e["path"], str(repo))
        self.assertEqual(e["branch"], "feature-b")
        self.assertEqual(sorted(e["branches"]), ["feature-a", "feature-b"])
        self.assertEqual(e["commits"], ["Add b", "Add a"])
        self.assertEqual(e["commits_total"], 2)
        self.assertEqual(sorted(e["files"]), ["\"new file.txt\"", "base.txt"], "a path git quotes is kept as git printed it")
        self.assertEqual(e["files_total"], 2)

    def test_the_lists_of_what_was_left_are_capped_but_the_totals_stay_right(self):
        repo, git = self.clone()
        for i in range(25):
            git("checkout", "-b", "b%02d" % i, "main")
            (repo / ("c%02d.txt" % i)).write_text("x\n")
            git("add", ".")
            git("commit", "-m", ("long " * 60 if i == 24 else "commit %02d" % i))
        for i in range(60):
            (repo / ("u%02d.txt" % i)).write_text("x\n")
        (repo / ("d" * 100)).mkdir()
        (repo / ("d" * 100) / ("n" * 200 + ".txt")).write_text("x\n")
        e = self.run_with_leftovers()["leftover"][0]
        self.assertEqual((len(e["branches"]), len(e["commits"]), len(e["files"])), (20, 20, 50))
        self.assertEqual((e["commits_total"], e["files_total"]), (25, 61))
        self.assertEqual(max(len(c) for c in e["commits"]), 200, "a long subject is clipped to 200 characters")
        self.assertTrue(all(len(f) <= 200 for f in e["files"]))

    def test_a_clone_with_nothing_left_gives_no_leftover(self):
        self.clone()
        self.assertFalse(self.run_with_leftovers().get("leftover"))

    def test_a_clone_where_git_fails_does_not_stop_the_status_from_being_written(self):
        repo, git = self.clone()
        (repo / "a.txt").write_text("a\n")
        r = runner.Runner.__new__(runner.Runner)
        real = subprocess.run
        def broken(*a, **k):
            raise subprocess.TimeoutExpired("git", 30)
        subprocess.run = broken
        try:
            e = r.leftover_of(str(repo))
        finally:
            subprocess.run = real
        self.assertEqual(e, {"path": str(repo), "branch": "", "branches": [], "commits": [], "commits_total": 0, "files": [], "files_total": 0})
        shutil.rmtree(repo / ".git")
        self.assertEqual(r.leftover_of(str(repo))["files"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
