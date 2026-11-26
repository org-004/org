"""The runner's time cap is measured on the monotonic clock, so the wall clock cannot end a run."""
import contextlib
import io
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from org import runner  # noqa: E402
from org.util import write_json  # noqa: E402
import test_runner  # noqa: E402

DONE, PLAN_TURN, REPORT = test_runner.DONE, test_runner.PLAN_TURN, test_runner.REPORT


class MonotonicCap(unittest.TestCase):
    # The runner case's helpers, without the many tests it carries, which would run again here.
    setUp, job, go, status = (test_runner.RunnerCase.setUp, test_runner.RunnerCase.job,
                              test_runner.RunnerCase.go, test_runner.RunnerCase.status)

    def test_a_wall_clock_set_three_hours_forward_in_the_middle_of_a_run_does_not_end_a_run_capped_at_two_hours(self):
        self.job({"plan": PLAN_TURN, "milestone:M1": dict(DONE("M1"), sleep=1.5), "milestone:M2": DONE("M2"), "report": REPORT},
                 caps={"max_hours": 2.0})
        shift = [0.0]
        wall = lambda: time.time() + shift[0]  # noqa: E731
        threading.Timer(0.8, lambda: shift.__setitem__(0, 3 * 3600.0)).start()
        with mock.patch.object(runner, "now", wall):
            self.go()
        st = self.status()
        self.assertEqual(shift[0], 3 * 3600.0, "the clock was moved during the run")
        self.assertEqual(st["state"], "done", st.get("end_reason"))
        self.assertNotIn("time cap", st.get("end_reason") or "")

    def test_a_wall_clock_jump_does_not_make_a_talking_turn_silent(self):
        # The silence watchdog is on the monotonic clock too: a jump of three hours, far past the
        # minute of silence allowed here, ends nothing.
        # M1 is quiet for 3.5 s, longer than the 2 s the runner waits for a line before it looks
        # at the clocks, so it looks at least once after the jump with nothing heard since.
        self.job({"plan": PLAN_TURN, "milestone:M1": dict(DONE("M1"), sleep=3.5), "report": REPORT},
                 engineer={"silence_timeout_min": 1.0})
        shift = [0.0]
        threading.Timer(1.0, lambda: shift.__setitem__(0, 3 * 3600.0)).start()
        with mock.patch.object(runner, "now", lambda: time.time() + shift[0]):
            self.go()
        self.assertEqual(self.status()["state"], "done", self.status().get("end_reason"))

    def test_a_turn_that_says_nothing_for_longer_than_the_silence_allowed_is_ended(self):
        self.job({"plan": PLAN_TURN, "milestone:M1": dict(DONE("M1"), sleep=30), "report": REPORT},
                 engineer={"silence_timeout_min": 2 / 60.0, "max_turn_retries": 0})
        started = time.monotonic()
        self.go()
        self.assertLess(time.monotonic() - started, 25, "the silent turn was ended, not waited out")
        self.assertIn("no output for too long", self.status().get("end_reason") or "")

    def test_a_resumed_runner_carries_the_time_already_used_and_not_the_gap(self):
        self.job({"plan": PLAN_TURN}, caps={"max_hours": 2.0})
        first = runner.Runner(self.run_dir)
        state = dict(first.state, used_s=7000.0, started_at=time.time() - 30 * 86400)  # a month of wall time, 7000 s of work
        write_json(self.run_dir / "state.json", state)
        again = runner.Runner(self.run_dir)
        self.assertGreaterEqual(again.used_s(), 7000.0)
        self.assertLess(again.used_s(), 7100.0)
        self.assertAlmostEqual(again.time_left(), 200.0, delta=100)
        self.assertAlmostEqual(again.deadline(), time.time() + again.time_left(), delta=1)

    def test_a_runner_that_was_resumed_past_its_cap_is_out_of_time(self):
        self.job({"plan": PLAN_TURN}, caps={"max_hours": 1.0})
        first = runner.Runner(self.run_dir)
        write_json(self.run_dir / "state.json", dict(first.state, used_s=3601.0))
        self.assertLess(runner.Runner(self.run_dir).time_left(), 0)

    def test_saving_the_state_writes_the_time_used_so_far(self):
        self.job({"plan": PLAN_TURN}, caps={"max_hours": 2.0})
        r = runner.Runner(self.run_dir)
        r.save_state()
        self.assertGreaterEqual(json.loads((self.run_dir / "state.json").read_text())["used_s"], 0.0)


if __name__ == "__main__":
    unittest.main()
