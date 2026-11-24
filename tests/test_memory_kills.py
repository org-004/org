"""The count of memory kills, from a simulated container's cgroup to the run record and `org status`."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from org import dispatch, status, sync  # noqa: E402
from test_incus import IncusCase  # noqa: E402


class Canned:
    """A machine that answers every snapshot with the same document."""

    def __init__(self, snap):
        self.snap = snap

    def run(self, script, timeout=None):
        return mock.Mock(stdout=json.dumps(self.snap).encode())


class MemoryKills(IncusCase):
    def a_run(self, events=None):
        """Dispatch a run, give its container a memory.events with `events` if any, and let it finish."""
        run_id, rec = dispatch.dispatch(self.ledger, self.approved(), log=lambda *_: None)
        if events is not None:
            cgroup = self.box(rec["machine"]["name"]) / "home" / ".cgroup"
            cgroup.mkdir()
            (cgroup / "memory.events").write_text(events)
        return run_id, self.wait_final(run_id)

    def test_a_count_of_zero_is_carried_from_the_snapshot_to_the_view_and_prints_no_line(self):
        run_id, rec = self.a_run("low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n")
        self.assertEqual(rec["oom_kill"], 0)
        view = status.collect(self.ledger)["runs"][0]
        self.assertEqual(view["oom_kill"], 0)
        self.assertNotIn("killed for memory", status.render(status.collect(self.ledger), width=200))

    def test_a_count_above_zero_reaches_the_record_and_the_status_line(self):
        run_id, rec = self.a_run("oom 3\noom_kill 3\n")
        self.assertEqual(rec["oom_kill"], 3)
        self.assertEqual(self.ledger.run(run_id)["oom_kill"], 3)
        state = status.collect(self.ledger)
        self.assertEqual(state["runs"][0]["oom_kill"], 3)
        self.assertIn("  ! %s: 3 processes were killed for memory in its container" % run_id, status.render(state, width=200))

    def test_a_machine_with_no_count_leaves_the_record_without_one_and_breaks_nothing(self):
        run_id, rec = self.a_run()
        self.assertEqual(rec["state"], "done")
        self.assertIsNone(rec["oom_kill"])
        state = status.collect(self.ledger)
        self.assertIsNone(state["runs"][0]["oom_kill"])
        self.assertNotIn("killed for memory", status.render(state, width=200))

    def test_sync_stores_only_a_whole_number_that_is_not_negative(self):
        run_id = "001-tampered-r1"
        self.ledger.run_dir(run_id).mkdir(parents=True)
        self.ledger.save_run(run_id, {"run_id": run_id, "state": "dispatched", "remote_dir": "$HOME/org-runs/" + run_id,
                                      "machine": {"kind": "incus", "name": "org-eng-" + run_id}, "events_offset": 0})
        base = {"ok": True, "now": 1.0, "files": {}, "events": {"offset": 0, "size": 0, "data": ""}}
        # In this order: what is not a count leaves none on record, and once there is one it never
        # goes down, because a container that was restarted counts from zero and the kills still happened.
        cases = [({}, None), ({"oom_kill": "7"}, None), ({"oom_kill": -1}, None), ({"oom_kill": True}, None), ({"oom_kill": 2.0}, None),
                 ({"oom_kill": None}, None), ({"oom_kill": [1]}, None), ({"oom_kill": 0}, 0), ({"oom_kill": 7}, 7), ({"oom_kill": 0}, 7),
                 ({}, 7), ({"oom_kill": "9"}, 7), ({"oom_kill": 9}, 9)]
        for extra, want in cases:
            with mock.patch.object(sync, "for_machine", return_value=Canned(dict(base, **extra))):
                rec = sync.sync_run(self.ledger, run_id)
            self.assertEqual(rec["oom_kill"], want, extra)
            self.assertEqual(self.ledger.run(run_id)["oom_kill"], want, extra)


if __name__ == "__main__":
    unittest.main()
