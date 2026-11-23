"""The manager's side: specs, approval, dispatch to a (simulated) OrbStack machine, sync, status."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from org import budget, dispatch, spec, status, sync  # noqa: E402
from org.ledger import Ledger  # noqa: E402
from org.transport import Ssh, TransportError  # noqa: E402
from org.util import read_json, write_json  # noqa: E402

GOOD = """---
title: CSV export
project: acme
repos: acme/web, acme/api
delivery: pr
max_cost_usd: 40
max_hours: 6
---

## Intent

People need their table data in a spreadsheet without copy and paste.

## Context

The table view lives in acme-web; the rows come from /api/rows in acme-api.

## Requirements

- R1 (MUST): the table view has an Export button that downloads the current rows as CSV.
- R2 (SHOULD): the export respects the active filter.

## Out of scope

- Excel formats.

## Acceptance criteria

- AC1: `npm test -- export` passes in acme-web.
- AC2: `curl -s localhost:3000/api/rows.csv | head -1` prints the header row.

## Constraints

One pull request per repository against main.

## Alternatives considered

- Client-side only export: loses rows beyond the first page.

## Open questions

None
"""

M1 = {"id": "M1", "title": "API endpoint", "est_minutes": 10, "est_cost_usd": 2.0}
PLAN = {"verdict": "proceed", "summary": "Endpoint then button.", "estimate": {"wall_minutes": 20, "cost_usd": 5.0}, "milestones": [M1]}
PLAN_MD = "# Plan\n\n" + "Approach, alternatives, milestones, estimate, risks. " * 12
TURNS = {
    "plan": {"structured": PLAN, "files": {"$RUN/plan.md": PLAN_MD}, "cost": 0.5},
    "milestone:M1": {"structured": {"milestone_id": "M1", "outcome": "done", "summary": "endpoint verified"}, "cost": 2.5},
    "report": {"structured": {"status": "done", "msg": "Both criteria pass.",
                              "acceptance": [{"id": "AC1", "verdict": "pass", "evidence": "ran"}, {"id": "AC2", "verdict": "pass", "evidence": "ran"}],
                              "artifacts": [{"repo": "acme/api", "branch": "org/001-csv-export", "pr_url": "https://example.test/pr/1"}]},
               "files": {"$RUN/report.md": "# Report\n\nDelivered.\n"}, "cost": 0.3},
}


class SpecChecks(unittest.TestCase):
    def test_good_spec_passes(self):
        errors, warnings = spec.check(GOOD)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        meta, body = spec.parse(GOOD)
        self.assertEqual(meta["repos"], ["acme/web", "acme/api"])
        self.assertEqual(meta["max_cost_usd"], 40.0)
        self.assertEqual(spec.acceptance_ids(body), ["AC1", "AC2"])

    def test_untouched_template_is_not_approvable(self):
        errors, _ = spec.check(spec.TEMPLATE.format(title="X", project=""))
        self.assertTrue(any("template text" in e for e in errors))

    def test_what_blocks_approval(self):
        cases = {
            "open questions remain": GOOD.replace("## Open questions\n\nNone", "## Open questions\n\n- Which button colour?"),
            "acceptance criteria need ids": GOOD.replace("- AC1:", "- first:").replace("- AC2:", "- second:"),
            "requirements need ids": GOOD.replace("- R1 (MUST):", "-").replace("- R2 (SHOULD):", "-"),
            "'## Intent' is missing": GOOD.replace("## Intent", "## Motivation"),
            "max_cost_usd must be a number": GOOD.replace("max_cost_usd: 40", "max_cost_usd: forty"),
            "delivery must be one of": GOOD.replace("delivery: pr", "delivery: yolo"),
            "machine must be": GOOD.replace("delivery: pr", "delivery: pr\nmachine: the big one"),
            "title is required": GOOD.replace("title: CSV export\n", ""),
        }
        for expected, text in cases.items():
            errors, _ = spec.check(text)
            self.assertTrue(any(expected in e for e in errors), "%r not reported in %r" % (expected, errors))

    def test_a_repo_must_be_reachable_from_the_engineers_machine(self):
        local_path = GOOD.replace("repos: acme/web, acme/api", "repos: /Users/me/src/acme-web")
        errors, _ = spec.check(local_path)
        self.assertTrue(any("path on this machine" in e and "orb" in e for e in errors), errors)
        self.assertTrue(any("path on this machine" in e for e in spec.check(local_path.replace("delivery: pr", "delivery: pr\nmachine: ssh:gpu"))[0]))
        # on the same machine a path is a perfectly good repository
        self.assertEqual(spec.check(local_path.replace("delivery: pr", "delivery: pr\nmachine: local"))[0], [])
        self.assertEqual(spec.check(local_path, default_machine="local")[0], [])

    def test_a_placeholder_repo_is_not_ready(self):
        for name in ("OWNER/org", "owner/name", "<org>/acme-api", "your-org/acme", "TBD"):
            errors, _ = spec.check(GOOD.replace("repos: acme/web, acme/api", "repos: " + name))
            self.assertTrue(any("placeholder" in e for e in errors), name)
        for name in ("acme/web", "https://github.com/acme/web.git", "git@github.com:acme/web.git", "ownerly/repository"):
            errors, _ = spec.check(GOOD.replace("repos: acme/web, acme/api", "repos: " + name))
            self.assertEqual(errors, [], name)

    def test_effort_is_checked(self):
        self.assertEqual(spec.check(GOOD.replace("delivery: pr", "delivery: pr\neffort: xhigh"))[0], [])
        self.assertTrue(any("effort must be one of" in e for e in spec.check(GOOD.replace("delivery: pr", "delivery: pr\neffort: heroic"))[0]))

    def test_warnings_do_not_block(self):
        text = GOOD.replace("max_cost_usd: 40\nmax_hours: 6\n", "").replace("(MUST)", "(SHOULD)")
        errors, warnings = spec.check(text)
        self.assertEqual(errors, [])
        self.assertTrue(any("unbounded" in w for w in warnings))
        self.assertTrue(any("MUST" in w for w in warnings))


class ManagerSide(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="org-mgr-"))
        self.addCleanup(self._cleanup)
        self.old_env = dict(os.environ)
        # A home of our own: nothing here reads or copies the real login token.
        home = self.tmp / "mac-home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "oauth-token").write_text("sk-ant-oat01-not-a-real-token\n")
        bindir = self.tmp / "bin"
        bindir.mkdir()
        os.symlink(ROOT / "tests" / "fake_orb.py", bindir / "orb")
        os.symlink(ROOT / "tests" / "fake_claude.py", bindir / "claude")
        self.scen = self.tmp / "scen"
        self.scen.mkdir()
        os.environ.update(HOME=str(home), PATH=str(bindir) + os.pathsep + self.old_env["PATH"],
                          FAKE_ORB_ROOT=str(self.tmp / "orb"), FAKE_CLAUDE_SCENARIO=str(self.scen / "scenario.json"))
        self.scenario(TURNS)
        self.ledger = Ledger(self.tmp / "ledger")
        self.ledger.init()
        write_json(self.ledger.config_path, {"machines": {"default": "orb", "detach": "nohup"},
                                             "engineer": {"retry_backoff_s": 0.05, "max_turn_retries": 1}})

    def _cleanup(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        subprocess.run(["pkill", "-f", str(self.tmp)], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scenario(self, turns):
        (self.scen / "scenario.json").write_text(json.dumps({"turns": turns}))

    def approved(self, text=GOOD):
        spec_id = spec.new_draft(self.ledger, "CSV export", "acme")
        self.ledger.draft_path(spec_id).write_text(text)
        spec.approve(self.ledger, spec_id, spec.draft_sha(self.ledger, spec_id)[:10])
        return spec_id

    def wait_final(self, run_id, seconds=40):
        end = time.time() + seconds
        while time.time() < end:
            rec = sync.sync_run(self.ledger, run_id)
            if rec.get("final"):
                return rec
            time.sleep(0.3)
        self.fail("run %s did not finish: %s" % (run_id, self.ledger.run(run_id)))

    def machine_home(self, run_id):
        return self.tmp / "orb" / ("eng-" + run_id) / "home"

    # ------------------------------------------------------------ approval
    def test_approval_is_bound_to_the_text(self):
        spec_id = spec.new_draft(self.ledger, "CSV export", "acme")
        self.assertEqual(spec_id, "001-csv-export")
        draft = self.ledger.draft_path(spec_id)
        draft.write_text(GOOD)
        sha = spec.draft_sha(self.ledger, spec_id)
        with self.assertRaises(spec.ApprovalError):
            spec.approve(self.ledger, spec_id, "deadbeefdead")
        with self.assertRaises(spec.ApprovalError):
            spec.approve(self.ledger, spec_id, sha[:4])  # too short to mean anything
        draft.write_text(GOOD.replace("One pull request per repository", "Exactly one pull request per repository"))
        with self.assertRaises(spec.ApprovalError) as ctx:
            spec.approve(self.ledger, spec_id, sha[:12])  # the text moved after it was read
        self.assertIn("changed since", str(ctx.exception))
        meta = spec.approve(self.ledger, spec_id, spec.draft_sha(self.ledger, spec_id)[:12])
        self.assertEqual(meta["version"], 1)
        frozen = self.ledger.spec_dir(spec_id) / "spec.md"
        self.assertEqual(frozen.stat().st_mode & 0o222, 0, "the approved spec is read-only")
        self.assertEqual(spec.state_of(self.ledger, spec_id), "approved")
        # a later edit is a revision, and approving it keeps the earlier version
        draft.write_text(GOOD.replace("Excel formats.", "Excel formats. PDF."))
        self.assertEqual(spec.state_of(self.ledger, spec_id), "revised")
        meta2 = spec.approve(self.ledger, spec_id, spec.draft_sha(self.ledger, spec_id)[:12])
        self.assertEqual(meta2["version"], 2)
        self.assertTrue((self.ledger.spec_dir(spec_id) / "spec.v1.md").exists())

    def test_a_spec_can_be_named_by_its_draft_path(self):
        spec_id = spec.new_draft(self.ledger, "CSV export", "acme")
        self.assertEqual(self.ledger.resolve_spec(str(self.ledger.draft_path(spec_id))), spec_id)
        self.assertEqual(self.ledger.resolve_spec(spec_id + ".md"), spec_id)
        self.assertEqual(self.ledger.resolve_spec("1"), spec_id)

    def test_status_does_not_wait_long_for_a_machine(self):
        seen = []
        original = status.sync_all
        status.sync_all = lambda ledger, **kw: seen.append(kw) or []
        self.addCleanup(setattr, status, "sync_all", original)
        status.collect(self.ledger, do_sync=True)
        write_json(self.ledger.config_path, {"status": {"sync_timeout_s": 2}})
        status.collect(self.ledger, do_sync=True)
        status.collect(self.ledger)
        self.assertEqual(seen, [{"timeout": 8.0, "slices": 1}, {"timeout": 2.0, "slices": 1}], "and none at all without do_sync")

    def test_draft_that_fails_its_checks_cannot_be_approved(self):
        spec_id = spec.new_draft(self.ledger, "Vague idea")
        with self.assertRaises(spec.ApprovalError) as ctx:
            spec.approve(self.ledger, spec_id, spec.draft_sha(self.ledger, spec_id)[:12])
        self.assertIn("does not pass its checks", str(ctx.exception))
        self.assertIsNone(self.ledger.spec_meta(spec_id))

    # ------------------------------------------------------------ dispatch, sync, status
    def test_dispatch_to_an_orb_machine_and_follow_it_to_the_end(self):
        spec_id = self.approved()
        log = []
        run_id, rec = dispatch.dispatch(self.ledger, spec_id, log=log.append)
        self.assertEqual(run_id, "001-csv-export-r1")
        self.assertEqual(rec["machine"], dict(rec["machine"], kind="orb", name="eng-001-csv-export-r1", base="base"))
        self.assertEqual(rec["state"], "dispatched")
        calls = (self.tmp / "orb" / "calls.log").read_text().splitlines()
        self.assertEqual(calls[:2], ["clone base eng-001-csv-export-r1", "start eng-001-csv-export-r1"])
        home = self.machine_home(run_id)
        token = home / ".claude" / "oauth-token"
        self.assertEqual(token.read_text().strip(), "sk-ant-oat01-not-a-real-token")
        self.assertEqual(token.stat().st_mode & 0o077, 0, "the token is private to the machine's user")
        remote = home / "org-runs" / run_id
        self.assertTrue((remote / ".org" / "org" / "runner.py").exists())
        self.assertEqual((remote / "spec.md").read_text(), GOOD)
        job = read_json(remote / "job.json")
        self.assertEqual(job["caps"], {"max_cost_usd": 40.0, "max_hours": 6.0})
        self.assertEqual(job["acceptance_ids"], ["AC1", "AC2"])
        self.assertEqual(job["delivery"], "pr")

        self.wait_final(run_id)
        mirror = self.ledger.mirror_dir(run_id)
        self.assertEqual(read_json(mirror / "status.json")["state"], "done")
        for name in ("plan.frozen.md", "plan.frozen.json", "report.md", "result.json", "actuals.json", "events.jsonl"):
            self.assertTrue((mirror / name).exists(), name)
        events = (mirror / "events.jsonl").read_text().splitlines()
        self.assertEqual(sum(1 for e in events if json.loads(e).get("type") == "result"), 3, "one result per turn, none duplicated")
        self.assertTrue((home / "work" / run_id).is_dir(), "the workspace is under the machine's home, not ours")

        state = status.collect(self.ledger)
        view = state["runs"][0]
        self.assertEqual((view["state"], view["health"], view["terminal"]), ("done", "ok", True))
        self.assertAlmostEqual(view["cost_usd"], 3.3, places=6)
        self.assertEqual(view["progress"]["done"], 1)
        self.assertIsNotNone(state["budget"]["weekly"])
        self.assertEqual(state["drafts"], [], "an approved, unchanged draft is not pending")
        text = status.render(state, width=200)
        self.assertIn("001-csv-export-r1", text)
        self.assertIn("done ✓", text)
        self.assertIn("Both criteria pass.", text)
        card = status.scorecard(self.ledger, run_id)
        self.assertIn("cost after planning", card)
        self.assertIn("AC1 pass, AC2 pass", card)

        # a finished, fully copied run is not asked again
        before = len((self.tmp / "orb" / "calls.log").read_text().splitlines())
        sync.sync_all(self.ledger)
        self.assertEqual(len((self.tmp / "orb" / "calls.log").read_text().splitlines()), before)

        # the next run of any spec is told how this estimate turned out
        run2, _ = dispatch.dispatch(self.ledger, spec_id, log=log.append)
        self.assertEqual(run2, "001-csv-export-r2")
        job2 = read_json(self.machine_home(run2) / "org-runs" / run2 / "job.json")
        self.assertEqual(len(job2["history"]["runs"]), 1)
        self.assertAlmostEqual(job2["history"]["cost_ratio"], 2.8 / 5.0, places=2)
        plan_prompt = None
        self.wait_final(run2)
        for line in (self.scen / "invocations.jsonl").read_text().splitlines():
            call = json.loads(line)
            if call["label"] == "plan":
                plan_prompt = call["prompt"]
        self.assertIn("Actual cost was 0.6x the estimate", plan_prompt)
        self.assertIn("at most $40.00 for the whole run", plan_prompt)

    def test_two_engineers_at_once_on_separate_machines(self):
        a, b = self.approved(), self.approved(GOOD.replace("CSV export", "JSON export"))
        slow = dict(TURNS, **{"milestone:M1": dict(TURNS["milestone:M1"], sleep=1.5)})
        self.scenario(slow)
        run_a, _ = dispatch.dispatch(self.ledger, a, log=lambda m: None)
        run_b, _ = dispatch.dispatch(self.ledger, b, log=lambda m: None)
        self.assertNotEqual(self.machine_home(run_a), self.machine_home(run_b))
        time.sleep(1.0)
        sync.sync_all(self.ledger)
        live = [v for v in status.collect(self.ledger)["runs"] if not v["terminal"]]
        self.assertEqual(len(live), 2, "both are in flight at the same time")
        self.wait_final(run_a)
        self.wait_final(run_b)
        self.assertEqual({v["state"] for v in status.collect(self.ledger)["runs"]}, {"done"})

    def test_stop_then_delete_the_machine(self):
        spec_id = self.approved()
        self.scenario(dict(TURNS, **{"milestone:M1": dict(TURNS["milestone:M1"], sleep=60)}))
        run_id, _ = dispatch.dispatch(self.ledger, spec_id, log=lambda m: None)
        end = time.time() + 20
        while time.time() < end:
            sync.sync_run(self.ledger, run_id)
            st = read_json(self.ledger.mirror_dir(run_id) / "status.json") or {}
            if st.get("state") == "executing":
                break
            time.sleep(0.3)
        self.assertEqual(st.get("state"), "executing")
        view = status.run_view(self.ledger, run_id, None)
        self.assertEqual(view["health"], "ok")
        self.assertIn("milestone 1/1: API endpoint", view["detail"] or "")
        self.assertIn("signalled", dispatch.stop(self.ledger, run_id))
        self.wait_final(run_id)
        self.assertEqual(read_json(self.ledger.mirror_dir(run_id) / "status.json")["state"], "stopped")
        self.assertIn("deleted", dispatch.remove_machine(self.ledger, run_id))
        self.assertFalse(self.machine_home(run_id).exists())
        self.assertTrue((self.ledger.mirror_dir(run_id) / "status.json").exists(), "the record outlives the machine")

    def test_a_dead_runner_shows_as_dead_and_can_be_resumed(self):
        spec_id = self.approved()
        self.scenario(dict(TURNS, **{"milestone:M1": dict(TURNS["milestone:M1"], sleep=60)}))
        run_id, _ = dispatch.dispatch(self.ledger, spec_id, log=lambda m: None)
        end = time.time() + 20
        st = {}
        while time.time() < end and st.get("state") != "executing":
            sync.sync_run(self.ledger, run_id)
            st = read_json(self.ledger.mirror_dir(run_id) / "status.json") or {}
            time.sleep(0.3)
        os.kill(st["pid"], 9)  # the runner itself dies; no status update is written
        time.sleep(0.5)
        sync.sync_run(self.ledger, run_id)
        self.assertEqual(status.run_view(self.ledger, run_id, None)["health"], "dead")
        subprocess.run(["pkill", "-f", str(self.tmp)], capture_output=True)  # the orphaned engineer process
        self.scenario(TURNS)
        dispatch.resume(self.ledger, run_id)
        self.wait_final(run_id)
        self.assertEqual(read_json(self.ledger.mirror_dir(run_id) / "status.json")["state"], "done")
        labels = [json.loads(line)["label"] for line in (self.scen / "invocations.jsonl").read_text().splitlines()]
        self.assertEqual(labels.count("plan"), 1, "the plan was not redone")

    def test_dispatch_failures_are_recorded(self):
        spec_id = self.approved()
        os.environ["FAKE_ORB_BASE"] = "some-other-template"
        with self.assertRaises(dispatch.DispatchError):
            dispatch.dispatch(self.ledger, spec_id, log=lambda m: None)
        rec = self.ledger.run("001-csv-export-r1")
        self.assertEqual(rec["state"], "dispatch_failed")
        self.assertIn("orb clone", rec["error"])
        view = status.collect(self.ledger)["runs"][0]
        self.assertEqual(view["state"], "dispatch_failed")
        self.assertIn("orb clone", view["detail"])

    def test_an_existing_run_directory_on_a_shared_host_is_not_overwritten(self):
        shared = self.tmp / "shared-host-home"
        shared.mkdir()
        cfg = {"machines": {"default": "local", "detach": "nohup", "local": {"enabled": True, "home": str(shared)}},
               "engineer": {"retry_backoff_s": 0.05}}
        write_json(self.ledger.config_path, cfg)
        spec_id = self.approved()
        run_id, _ = dispatch.dispatch(self.ledger, spec_id, log=lambda m: None)
        self.wait_final(run_id)
        # A second ledger, same host, same first spec: the run id collides.
        other = Ledger(self.tmp / "other-ledger")
        other.init()
        write_json(other.config_path, cfg)
        other_spec = spec.new_draft(other, "CSV export", "acme")
        other.draft_path(other_spec).write_text(GOOD)
        spec.approve(other, other_spec, spec.draft_sha(other, other_spec)[:10])
        report_before = (shared / "org-runs" / run_id / "report.md").read_text()
        with self.assertRaises(dispatch.DispatchError) as ctx:
            dispatch.dispatch(other, other_spec, log=lambda m: None)
        self.assertIn("already has a run directory", str(ctx.exception))
        self.assertEqual((shared / "org-runs" / run_id / "report.md").read_text(), report_before)
        self.assertEqual(other.run(run_id)["state"], "dispatch_failed")

    def test_unreachable_machine_is_reported_not_fatal(self):
        spec_id = self.approved()
        self.scenario(dict(TURNS, **{"milestone:M1": dict(TURNS["milestone:M1"], sleep=60)}))
        run_id, _ = dispatch.dispatch(self.ledger, spec_id, log=lambda m: None)
        time.sleep(0.5)
        sync.sync_run(self.ledger, run_id)
        shutil.move(str(self.tmp / "orb" / ("eng-" + run_id)), str(self.tmp / "orb-gone"))
        rec = sync.sync_run(self.ledger, run_id)
        self.assertFalse(rec["reachable"])
        view = status.run_view(self.ledger, run_id, None)
        self.assertEqual(view["health"], "unreachable")
        self.assertIn("unreachable", status.render(status.collect(self.ledger), width=200))

    def test_machine_selection(self):
        cfg = self.ledger.config()
        with self.assertRaises(dispatch.DispatchError) as ctx:
            dispatch.choose_machine(cfg, {}, "local", "001-x-r1")
        self.assertIn("disabled", str(ctx.exception))
        with self.assertRaises(dispatch.DispatchError):
            dispatch.choose_machine(cfg, {}, "ssh:nowhere", "001-x-r1")
        cfg["machines"]["ssh"] = {"gpu": {"host": "me@gpu.example.test", "options": ["-p", "2222"]}}
        m = dispatch.choose_machine(cfg, {"machine": "ssh:gpu"}, None, "001-x-r1")
        self.assertEqual((m["kind"], m["host"], m["push_token"]), ("ssh", "me@gpu.example.test", False))
        argv = Ssh("gpu", m["host"], m["options"]).argv('echo "$HOME"')
        self.assertEqual(argv[:1] + argv[-2:], ["ssh", "me@gpu.example.test", "bash -c 'echo \"$HOME\"'"])
        self.assertIn("2222", argv)
        self.assertEqual(dispatch.choose_machine(cfg, {"machine": "ssh:gpu"}, "orb", "001-x-r1")["kind"], "orb", "the flag outranks the spec")

    def test_weekly_rate_is_learned_from_finished_runs(self):
        self.assertEqual(budget.usd_per_pct(self.ledger)["source"], "unknown")
        for i, (start, end, cost) in enumerate([(0.20, 0.30, 30.0), (0.30, 0.45, 30.0), (0.50, 0.505, 9.0)]):
            mirror = self.ledger.mirror_dir("00%d-x-r1" % i)
            mirror.mkdir(parents=True)
            write_json(mirror.parent / "run.json", {"run_id": "00%d-x-r1" % i})
            write_json(mirror / "result.json", {"status": "done"})
            write_json(mirror / "actuals.json", {"finished_at": 1000 + i, "total": {"cost_usd": cost},
                                                 "weekly": {"utilization_start": start, "utilization_end": end}})
        rate = budget.usd_per_pct(self.ledger)
        # 30/10 = $3.00 per point and 30/15 = $2.00: other usage only inflates the movement, so the
        # larger figure is nearer the truth. The third run moved the meter too little to count.
        self.assertEqual((rate["source"], rate["runs"]), ("calibrated", 2))
        self.assertAlmostEqual(rate["value"], 3.0, places=6)
        write_json(self.ledger.config_path, {"budget": {"usd_per_weekly_pct": 4.5}})
        self.assertEqual(budget.usd_per_pct(self.ledger), {"value": 4.5, "source": "config", "runs": 0})

    def test_cli_round_trip(self):
        def org(*args, ok=True):
            p = subprocess.run([sys.executable, "-m", "org", "--home", str(self.ledger.root)] + list(args),
                               capture_output=True, text=True, cwd=str(ROOT), timeout=60)
            if ok:
                self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
            return p

        out = org("spec", "new", "CSV export", "--project", "acme").stdout.split()
        spec_id, path = out[0], out[1]
        self.assertEqual(org("spec", "check", spec_id, ok=False).returncode, 1)
        Path(path).write_text(GOOD)
        check = org("spec", "check", "1").stdout
        self.assertIn("ready to approve", check)
        sha = check.split("sha256 ")[1].split()[0]
        self.assertIn("draft", org("status", "--no-sync").stdout)
        self.assertEqual(org("approve", spec_id, "--sha", "0000000000", ok=False).returncode, 1)
        approve = org("approve", spec_id, "--sha", sha).stdout
        self.assertIn("dispatched 001-csv-export-r1", approve)
        self.wait_final("001-csv-export-r1")
        self.assertIn("done ✓", org("status").stdout)
        self.assertEqual(json.loads(org("status", "--json").stdout)["runs"][0]["state"], "done")
        self.assertIn("# Plan", org("plan", "001").stdout)
        self.assertIn("Delivered.", org("report", "1").stdout)
        self.assertIn("cost after planning", org("review", "001-csv-export-r1").stdout)
        peek = org("peek", "1").stdout
        self.assertIn("result:    done — Both criteria pass.", peek)
        self.assertIn("turn ended", peek)
        self.assertIn("Bash: echo working on plan", org("logs", "1", "--all").stdout)
        self.assertIn("eng-001-csv-export-r1", org("machine", "ls").stdout)
        # approving the same text again must not start a second engineer
        again = org("approve", spec_id, "--sha", sha).stdout
        self.assertIn("already approved", again)
        self.assertIn("nothing dispatched", again)
        self.assertEqual(self.ledger.runs_for(spec_id), ["001-csv-export-r1"])
        self.assertIn("dispatched 001-csv-export-r2", org("dispatch", spec_id).stdout, "asking for another run is explicit")
        self.wait_final("001-csv-export-r2")
        self.assertIn("deleted", org("machine", "rm", "001-csv-export-r1").stdout)
        self.assertIn("v1", org("spec", "ls").stdout)
        self.assertIn(str(self.ledger.draft_path(spec_id)), org("spec", "check", str(self.ledger.draft_path(spec_id))).stdout)


class ScratchClone(unittest.TestCase):
    """`org clone`: a repository in the manager's scratch space, by name and nothing else."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="org-clone-", dir="/tmp")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ledger = Ledger(self.tmp / "ledger")
        # GitHub, for this test, is a directory: git rewrites the URL, org never sees the difference.
        self.remotes = self.tmp / "github"
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text('[user]\n\tname = T\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n'
                             '[url "%s/"]\n\tinsteadOf = https://github.com/\n' % self.remotes)
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=str(gitconfig), GIT_CONFIG_NOSYSTEM="1", HOME=str(self.tmp))
        self.remote = self.remotes / "acme" / "web.git"
        self.remote.mkdir(parents=True)
        self.git(self.remote, "init", "-q")
        self.commit("first")

    def git(self, where, *args):
        p = subprocess.run(["git", "-C", str(where)] + list(args), capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def commit(self, text):
        (self.remote / "README").write_text(text + "\n")
        self.git(self.remote, "add", "README")
        self.git(self.remote, "commit", "-q", "-m", text)

    def org(self, *args):
        return subprocess.run([sys.executable, "-m", "org", "--home", str(self.ledger.root)] + list(args),
                              capture_output=True, text=True, cwd=str(ROOT), env=self.env, timeout=60)

    def test_a_repository_arrives_in_scratch_and_is_fetched_the_next_time(self):
        copy = self.ledger.scratch / "web"
        p = self.org("clone", "acme/web")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.startswith(str(copy) + "  main  "), p.stdout)
        self.assertIn("first", p.stdout)
        self.assertEqual((copy / "README").read_text(), "first\n")
        self.assertEqual(self.git(copy, "config", "--get", "remote.origin.url"), "https://github.com/acme/web.git")
        self.assertEqual(self.git(copy, "config", "--get", "remote.origin.pushurl"), "no-push://scratch-copy",
                         "a scratch copy has nowhere to push")

        self.commit("second")
        p = self.org("clone", "acme/web")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("fetched; the checked-out branch is 1 commit behind", p.stdout)
        self.assertEqual((copy / "README").read_text(), "first\n", "the working tree is the manager's to move")
        self.git(copy, "merge", "-q", "--ff-only")
        # The same repository, named by its URL, is the same copy.
        p = self.org("clone", "https://github.com/acme/web")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("fetched; up to date", p.stdout)

    def test_only_a_repository_can_be_named(self):
        for bad in ("../web", "/tmp/web", str(self.remote), "acme/web --upload-pack=x", "acme/web;id", "ext::sh -c id",
                    "file:///tmp/web", "acme/.git", "acme"):
            p = self.org("clone", bad)
            self.assertEqual(p.returncode, 1, bad)
            self.assertIn("is not a repository", p.stderr, bad)
        self.assertNotEqual(self.org("clone", "--upload-pack=x", "acme/web").returncode, 0, "an option is not a repository either")
        self.assertEqual(list(self.ledger.scratch.iterdir()) if self.ledger.scratch.exists() else [], [])

    def test_what_is_already_there_is_not_replaced_and_a_failed_clone_leaves_nothing(self):
        (self.ledger.scratch / "web").mkdir(parents=True)
        (self.ledger.scratch / "web" / "notes").write_text("mine\n")
        p = self.org("clone", "acme/web")
        self.assertEqual(p.returncode, 1)
        self.assertIn("is already there and is not a clone of https://github.com/acme/web.git", p.stderr)
        self.assertEqual((self.ledger.scratch / "web" / "notes").read_text(), "mine\n")
        p = self.org("clone", "acme/missing")
        self.assertEqual(p.returncode, 1)
        self.assertIn("could not clone https://github.com/acme/missing.git", p.stderr)
        self.assertFalse((self.ledger.scratch / "missing").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
