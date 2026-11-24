"""The end of a plan and the owner's view of alerts, with the real modules and nothing stood in but Incus, the model and the server.

Covers R16 (one `finished` message when the last run of a plan is final with nothing held: a line in
alerts.jsonl, and one POST when `notify.url` is set; none when the plan stopped on a pause) and R15
(the `alerts` row of `org doctor`, which must show where alerts go but never the secret path of the
address, and `org notify test`). It reuses the base class and helpers of tests/test_plan.py.
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_notify import Receiver  # noqa: E402
from org import doctor, notify  # noqa: E402
from test_plan import DONE, PlanCase, answer  # noqa: E402


class WhenAPlanFinishes(PlanCase):
    def lines(self, kind):
        path = self.ledger.root / "alerts.jsonl"
        found = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
        return [x for x in found if x["kind"] == kind]

    def finished_plan(self):
        """B after A, both done with the review saying continue; returns the run ids once both are final and pumps have run."""
        a, b = self.plan([DONE, DONE], [answer("continue", "A left what B needs", "002-spec-b")])
        runs = []

        def both_final():
            if self.runs(b) and self.rec(self.runs(a)[0]).get("final") and self.rec(self.runs(b)[0]).get("final"):
                runs[:] = [self.runs(a)[0], self.runs(b)[0]]
                return True

        self.settle("both runs to be final", both_final)
        self.settle("the finished alert", lambda: self.lines("finished"))
        self.pumped(4)
        return runs

    def test_one_finished_alert_and_one_message_name_each_run_and_the_total_and_no_pause_is_written(self):
        with Receiver() as receiver:
            self.extra_config(notify={"url": receiver.url})
            first, second = self.finished_plan()
            self.assertEqual([self.rec(first)["state"], self.rec(second)["state"]], ["done", "done"])
            lines = self.lines("finished")
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["run"], second)
            self.assertEqual(self.lines("pause"), [])
            self.assertEqual(len(receiver.requests), 1)
            sent = receiver.requests[0]
            self.assertEqual(sent["method"], "POST")
            self.assertTrue(sent["headers"].get("Title"))
            self.assertNotIn("\n", sent["body"].strip())
            self.assertLessEqual(len(sent["body"]), 400)
            for run_id in (first, second):
                # `clean` spaces out dots, so compare with what it makes of the id.
                self.assertIn("%s done" % notify.clean(run_id, 100), sent["body"])
            self.assertIn("spent", sent["body"])
            self.assertRegex(sent["body"], r"\$\d+\.\d\d spent")
            self.pumped(3)
            self.assertEqual(len(receiver.requests), 1, "more pumps send nothing more")
            self.assertEqual(len(self.lines("finished")), 1)

    def test_a_plan_that_stopped_on_a_pause_is_never_reported_as_finished(self):
        with Receiver() as receiver:
            self.paused_after_a_failure(receiver)
            self.pumped(5)
            self.assertEqual(self.lines("finished"), [])
            self.assertEqual([r["body"] for r in receiver.requests if "finished" in r["body"]], [])
            self.assertEqual(len(self.lines("pause")), 1)
            self.assertEqual(len(receiver.requests), 1, "only the pause was sent")

    def test_without_a_notify_url_the_finished_line_is_still_written_once_and_no_outbox_appears(self):
        self.finished_plan()
        self.assertEqual(len(self.lines("finished")), 1)
        self.assertFalse((self.ledger.root / "outbox.json").exists())


class WhatTheOwnerIsToldAboutAlerts(PlanCase):
    def alerts_row(self):
        rows = [r for r in doctor.checks(self.ledger) if r[1] == "alerts"]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def test_doctor_warns_that_pauses_show_only_in_status_and_the_dashboard_when_no_url_is_set(self):
        level, _, found, fix = self.alerts_row()
        self.assertEqual(level, "warn")
        self.assertIn("org status", found)
        self.assertIn("dashboard", found)
        self.assertIn("notify.url", fix)

    def test_doctor_shows_the_host_of_the_url_and_not_its_secret_path(self):
        with Receiver() as receiver:
            url = receiver.url.rsplit("/", 1)[0] + "/secret-topic"
            self.extra_config(notify={"url": url})
            row = self.alerts_row()
            self.assertEqual(row[0], "ok")
            self.assertIn("127.0.0.1", row[2])
            self.assertNotIn("secret-topic", " ".join(row))

    def test_notify_test_prints_the_status_and_exits_zero_and_without_a_url_says_so_and_exits_one(self):
        code, out, err = self.org("notify", "test")
        self.assertEqual(code, 1)
        self.assertIn("no notify.url is set", out + err)
        with Receiver(statuses=[200]) as receiver:
            self.extra_config(notify={"url": receiver.url})
            code, out, err = self.org("notify", "test")
            self.assertEqual(code, 0, err)
            self.assertIn("200", out)
            self.assertEqual(len(receiver.requests), 1)
            self.assertEqual(receiver.requests[0]["method"], "POST")


if __name__ == "__main__":
    unittest.main()
