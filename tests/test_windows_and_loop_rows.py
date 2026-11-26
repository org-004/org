"""Every weekly window, the forecast beside a run's outcome, and the loop's line and doctor row."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from org import budget, doctor, graph, loop, status  # noqa: E402
from org.ledger import Ledger  # noqa: E402
from org.stream import Meter  # noqa: E402
from org.util import read_json, write_json  # noqa: E402

TWO_WINDOWS = {"type": "rate_limit_event", "rate_limit_info": {
    "status": "allowed", "rateLimitType": "seven_day",
    "unifiedWindows": {
        "seven_day": {"utilization": 0.40, "resetsAt": time.time() + 86400, "status": "allowed"},
        "seven_day_overage_included": {"utilization": 0.81, "resetsAt": time.time() + 86400, "status": "allowed"}}}}


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.ledger = Ledger(self.dir)
        self.ledger.init()
        for name, value in (("pool", lambda ledger: []),):
            p = mock.patch.object(status.dispatch, name, value)
            p.start()
            self.addCleanup(p.stop)

    def config(self, **cfg):
        write_json(self.ledger.config_path, cfg)

    def sample_two_windows(self):
        meter = Meter({})
        meter.feed(json.loads(json.dumps(TWO_WINDOWS)))
        budget.record_sample(self.ledger, {"rate_limit": meter.rate_limit}, "001-x-r1")


class EveryWeeklyWindow(Case):
    def test_a_stream_with_two_windows_leaves_every_weekly_window_in_the_ledger(self):
        self.sample_two_windows()
        wins = budget.windows(self.ledger)
        self.assertEqual(sorted(wins), ["seven_day", "seven_day_overage_included"])
        self.assertEqual(wins["seven_day_overage_included"]["utilization"], 0.81)
        self.assertFalse(wins["seven_day"]["stale"])
        self.assertEqual(budget.weekly(self.ledger)["utilization"], 0.40, "weekly stays the seven_day reading")

    def test_org_status_shows_every_weekly_window_and_the_json_carries_them_all(self):
        self.sample_two_windows()
        state = status.collect(self.ledger)
        text = status.render(state, width=160)
        first = text.splitlines()[0]
        self.assertIn("seven_day 40%", first)
        self.assertIn("seven_day_overage_included 81%", first)
        self.assertEqual(sorted(json.loads(json.dumps(state))["budget"]["windows"]), ["seven_day", "seven_day_overage_included"])

    def test_every_weekly_window_holds_the_brake_when_only_the_second_is_at_81_percent_and_it_is_named(self):
        self.sample_two_windows()
        self.assertEqual(graph.over_budget(self.ledger),
                         "the weekly limit seven_day_overage_included is 81% used, at or above graph.pause_above_weekly_pct (80%)")

    def test_every_weekly_window_that_has_reset_is_stale_and_holds_no_brake(self):
        self.sample_two_windows()
        book = read_json(self.ledger.budget_path)
        book["windows"]["seven_day_overage_included"]["resets_at"] = time.time() - 5
        write_json(self.ledger.budget_path, book)
        self.assertIsNone(graph.over_budget(self.ledger))


class Forecast(Case):
    def make_run(self, version, header):
        sdir = self.ledger.spec_dir("001-x")
        sdir.mkdir(parents=True)
        (sdir / "spec.md").write_text("---\ntitle: X\n%s---\n\nbody\n" % header)
        write_json(sdir / "meta.json", {"title": "X", "version": version})
        self.ledger.save_run("001-x-r1", {"spec_id": "001-x", "spec_version": version, "state": "done"})
        (self.ledger.mirror_dir("001-x-r1")).mkdir(parents=True, exist_ok=True)

    def test_status_json_carries_the_forecast_of_the_specs_header_for_every_run(self):
        self.make_run(1, "expect_usd: 12.5\nexpect_hours: 3\n")
        view = status.run_view(self.ledger, "001-x-r1", None)
        self.assertEqual((view["expect_usd"], view["expect_hours"]), (12.5, 3.0))

    def test_a_spec_without_a_forecast_gives_null_in_status_json(self):
        self.make_run(1, "")
        view = status.run_view(self.ledger, "001-x-r1", None)
        self.assertEqual((view["expect_usd"], view["expect_hours"]), (None, None))

    def test_the_review_page_shows_the_forecast_beside_the_plan_and_what_the_run_took(self):
        self.make_run(1, "expect_usd: 10\nexpect_hours: 2\n")
        mirror = self.ledger.mirror_dir("001-x-r1")
        write_json(mirror / "actuals.json", {"finished_at": 1, "after_planning": {"est_minutes": 30, "actual_minutes": 40},
                                             "total": {"cost_usd": 15.0, "elapsed_minutes": 180}})
        write_json(mirror / "result.json", {"status": "done"})
        text = status.scorecard(self.ledger, "001-x-r1")
        row = next(line for line in text.splitlines() if line.startswith("forecast in the spec"))
        self.assertIn("$10", row)
        self.assertIn("2 h", row)
        self.assertIn("1.50x", row)


class LoopRows(Case):
    def pumped(self, at):
        p = self.ledger.root / "loop"
        p.mkdir(exist_ok=True)
        write_json(p / "pumped.json", {"at": at})
        # `loop.pumped_at` may be unwritten in this tree: the doctor is tested against the file.
        read = lambda ledger: (read_json(ledger.root / "loop" / "pumped.json", {}) or {}).get("at")  # noqa: E731
        patcher = mock.patch.object(loop, "pumped_at", read)
        patcher.start()
        self.addCleanup(patcher.stop)

    def row(self):
        return next(r for r in doctor.checks(self.ledger) if r[1] == "loop")

    def test_org_status_shows_the_loops_line_under_the_pool_and_json_carries_the_dict(self):
        with mock.patch.object(loop, "summary", lambda ledger: {"enabled": False, "line": "loop: off"}):
            state = status.collect(self.ledger)
        self.assertEqual(state["loop"]["line"], "loop: off")
        self.assertIn("loop: off", status.render(state, width=160).splitlines())

    def test_a_loop_status_that_fails_is_shown_as_unknown_and_json_carries_the_error(self):
        with mock.patch.object(loop, "summary", mock.Mock(side_effect=ValueError("bad state"))):
            state = status.collect(self.ledger)
        self.assertEqual(state["loop"]["error"], "bad state")
        self.assertIn("loop: unknown (bad state)", status.render(state, width=160))

    def test_the_doctor_row_says_off_when_the_loop_is_off(self):
        self.assertEqual(self.row()[0:3], ("ok", "loop", "off"))

    def test_the_doctor_warns_when_the_loop_is_on_and_notify_url_is_not_set(self):
        self.config(loop={"enabled": True})
        self.pumped(time.time())
        level, _, found, _ = self.row()
        self.assertEqual(level, "warn")
        self.assertIn("notify.url is not set", found)

    def test_the_doctor_names_each_setting_the_manager_turn_is_missing(self):
        self.config(loop={"enabled": True}, notify={"url": "http://127.0.0.1:9/x"})
        self.pumped(time.time())
        level, _, found, _ = self.row()
        self.assertEqual(level, "warn")
        self.assertIn("manager.direct is not true", found)

    def test_the_doctor_warns_when_nothing_has_pumped_for_ten_minutes(self):
        self.config(loop={"enabled": True}, notify={"url": "http://127.0.0.1:9/x"})
        self.pumped(time.time() - 700)
        self.assertIn("is the dashboard's service running?", self.row()[2])

    def test_the_doctor_warns_when_the_loop_is_on_and_nothing_has_ever_pumped(self):
        self.config(loop={"enabled": True}, notify={"url": "http://127.0.0.1:9/x"})
        with mock.patch.object(loop, "pumped_at", lambda ledger: None):
            self.assertIn("is the dashboard's service running?", self.row()[2])

    def test_a_loop_that_pumped_a_minute_ago_adds_no_pumping_warning(self):
        self.config(loop={"enabled": True}, notify={"url": "http://127.0.0.1:9/x"})
        self.pumped(time.time() - 60)
        self.assertNotIn("service running", self.row()[2])


if __name__ == "__main__":
    unittest.main()
