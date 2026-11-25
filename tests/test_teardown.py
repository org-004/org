"""A finished run's machine is deleted by the next pump, against the simulated `incus` (and `docker`).

The records are written by hand where that says more plainly what is being tested than a whole
dispatch would; the code under test is the real one: dispatch, the backends, the transport.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from org import dispatch  # noqa: E402
from org.backends.incus import IncusBackend  # noqa: E402
from org.util import read_json, write_json  # noqa: E402
from test_incus import TEMPLATE, IncusCase  # noqa: E402

PREFIX = "org-eng-"


class TeardownCase(IncusCase):
    def finished(self, run_id, state="done", make=True, running=True, **fields):
        """A run as sync leaves it when it is over and fully copied, with its container (unless `make` is false)."""
        name = PREFIX + run_id
        if make:
            self.container(name, running=running)
        rec = {"run_id": run_id, "spec_id": run_id.rsplit("-r", 1)[0], "state": state, "final": True,
               "remote_dir": dispatch.remote_dir(run_id), "created_at": time.time(), "events_offset": 0,
               "machine": {"kind": "incus", "name": name, "base": TEMPLATE, "node": "vm"}}
        rec.update(fields)
        for key in [k for k, v in rec.items() if v is None]:
            rec.pop(key)
        self.ledger.run_dir(run_id).mkdir(parents=True, exist_ok=True)
        self.ledger.save_run(run_id, rec)
        return name

    def after_finish_is(self, value=None, absent=False):
        cfg = read_json(self.ledger.config_path)
        if absent:
            cfg["machines"].pop("after_finish", None)
        else:
            cfg["machines"]["after_finish"] = value
        write_json(self.ledger.config_path, cfg)

    def deletes(self, name=None):
        return [c for c in self.calls() if c[0] == "delete" and (name is None or c[-1] == name)]

    def removed(self, run_id):
        rec = self.ledger.run(run_id)
        self.assertTrue(rec.get("machine_removed_at"), rec)
        self.assertEqual(rec.get("machine_removed_by"), "teardown")
        self.assertNotIn("machine_remove_error", rec)
        return rec


class FinishedRuns(TeardownCase):
    def test_after_finish_absent_deletes_the_machine_of_a_run_in_each_terminal_state(self):
        names = {}
        for i, state in enumerate(("done", "partial", "failed", "stopped")):
            names["90%d-%s-r1" % (i, state)] = self.finished("90%d-%s-r1" % (i, state), state=state)
        self.assertEqual(dispatch.after_finish(self.ledger.config()), dispatch.AFTER_FINISH_DELETE)
        self.assertEqual(sorted(dispatch.teardown(self.ledger)), sorted(names))
        for run_id, name in names.items():
            self.assertFalse(self.box(name).exists(), run_id)
            self.removed(run_id)
            self.assertEqual(len(self.deletes(name)), 1)
        self.assertTrue((self.box(TEMPLATE) / "home").is_dir(), "the template stays")
        self.assertEqual(dispatch.teardown(self.ledger), [], "once")
        self.assertEqual(len(self.deletes()), 4)

    def test_a_run_that_left_warnings_is_deleted_all_the_same(self):
        name = self.finished("901-warned-r1", state="partial")
        mirror = self.ledger.mirror_dir("901-warned-r1")
        mirror.mkdir(parents=True)
        write_json(mirror / "status.json", {"state": "partial", "warnings": ["acme still has 2 unpushed commits on w1"]})
        self.assertEqual(dispatch.pump(self.ledger), [])
        self.assertFalse(self.box(name).exists())
        self.removed("901-warned-r1")

    def test_a_run_of_a_plan_paused_held_waited_on_or_reviewed_is_deleted_all_the_same(self):
        a = self.finished("901-paused-r1", paused={"cause": "review", "reason": "the review said pause", "at": time.time()},
                          review={"state": "running", "cost_usd": 0.0, "started_at": time.time()})
        b = self.finished("902-held-r1", state="failed", after=["901-paused"],
                          held={"cause": "901-paused-r1", "reason": "the review said pause", "at": time.time()})
        self.assertEqual(sorted(dispatch.teardown(self.ledger)), ["901-paused-r1", "902-held-r1"])
        for run_id, name in (("901-paused-r1", a), ("902-held-r1", b)):
            self.assertFalse(self.box(name).exists())
            rec = self.removed(run_id)
            self.assertTrue(rec.get("paused") or rec.get("held"), "the plan's fields are left as they were")

    def test_a_run_that_is_not_final_is_never_deleted_whatever_else_its_record_says(self):
        cases = {
            "901-unreachable-r1": dict(state="executing", final=False, reachable=False, last_error="no answer"),
            "902-dead-runner-r1": dict(state="executing", final=False, alive=False),
            "903-waiting-r1": dict(state="waiting", final=False),
            "904-paused-r1": dict(state="executing", final=False, paused={"cause": "review", "reason": "r", "at": 1}),
            "905-stopped-not-copied-r1": dict(state="stopped", final=False),
            "906-no-final-r1": dict(state="done", final=None),
            "907-final-not-terminal-r1": dict(state="executing", final=True),
            "908-queued-r1": dict(state="queued", final=None),
            "909-provisioning-r1": dict(state="provisioning", final=None),
            "910-dispatch-failed-r1": dict(state="dispatch_failed", final=None),
        }
        for run_id, fields in cases.items():
            self.finished(run_id, **fields)
        before = {r: self.ledger.run(r) for r in cases}
        self.assertEqual(dispatch.teardown(self.ledger), [])
        self.assertEqual(self.deletes(), [])
        for run_id in cases:
            self.assertTrue(self.box(PREFIX + run_id).exists(), run_id)
            self.assertEqual(self.ledger.run(run_id), before[run_id], run_id)

    def test_a_run_made_not_final_between_the_look_and_the_deletion_is_left_alone(self):
        name = self.finished("901-resumed-r1")
        real = self.ledger.run_lock

        @contextmanager
        def resumed_meanwhile(run_id, wait=0.0):
            # `org resume` got in just before the lock was asked for, and made the run not final.
            rec = self.ledger.run(run_id)
            rec.update(final=False, state="executing", resumed_at=time.time())
            self.ledger.save_run(run_id, rec)
            with real(run_id, wait) as held:
                yield held

        take = mock.Mock(return_value=None)
        with mock.patch.object(self.ledger, "run_lock", resumed_meanwhile), mock.patch.object(dispatch.telemetry, "take", take):
            self.assertEqual(dispatch.teardown(self.ledger), [])
        self.assertTrue(self.box(name).exists())
        self.assertEqual((self.deletes(), take.call_count), ([], 0))
        rec = self.ledger.run("901-resumed-r1")
        self.assertEqual((rec["final"], rec.get("machine_removed_at")), (False, None))
        # The one-run step decides from the record it reads under the lock, not from what it was handed.
        self.assertFalse(dispatch.teardown_run(self.ledger, "901-resumed-r1", self.ledger.config()))
        self.assertTrue(self.box(name).exists())

    def test_a_run_whose_lock_is_held_elsewhere_is_left_for_the_next_pump(self):
        name = self.finished("901-busy-r1")
        with self.ledger.run_lock("901-busy-r1") as held:
            self.assertTrue(held)
            self.assertEqual(dispatch.teardown(self.ledger), [])
            self.assertTrue(self.box(name).exists())
        self.assertEqual(dispatch.teardown(self.ledger), ["901-busy-r1"])
        self.assertFalse(self.box(name).exists())


class TheOrder(TeardownCase):
    def test_the_last_measurement_comes_before_the_deletion_and_the_record_after_it(self):
        name = self.finished("901-order-r1")
        seen = []

        def take(ledger, run_id, rec, last=False):
            seen.append({"run": run_id, "last": last, "deletes": len(self.deletes()), "there": self.box(name).exists(),
                         "marked": bool(ledger.run(run_id).get("machine_removed_at"))})
            rec["telemetry"] = {"samples": 1, "memory_peak_bytes": 123}
            return None

        with mock.patch.object(dispatch.telemetry, "take", side_effect=take):
            self.assertEqual(dispatch.teardown(self.ledger), ["901-order-r1"])
        self.assertEqual(seen, [{"run": "901-order-r1", "last": True, "deletes": 0, "there": True, "marked": False}])
        self.assertEqual(self.deletes(), [["delete", "-q", "-f", name]])
        rec = self.removed("901-order-r1")
        self.assertEqual(rec["telemetry"], {"samples": 1, "memory_peak_bytes": 123}, "what was measured is kept on the record")

    def test_a_measurement_that_raises_anyway_does_not_stop_the_deletion(self):
        name = self.finished("901-unmeasured-r1")
        log = []
        with mock.patch.object(dispatch.telemetry, "take", side_effect=RuntimeError("incus did not answer")):
            self.assertEqual(dispatch.teardown(self.ledger, log.append), ["901-unmeasured-r1"])
        self.assertFalse(self.box(name).exists())
        self.removed("901-unmeasured-r1")
        self.assertTrue(any("last measurement was not taken" in line and "incus did not answer" in line for line in log), log)

    def test_the_tail_of_the_runners_log_is_copied_into_the_mirror_as_runner_log_and_bounded(self):
        big = self.finished("901-big-log-r1")
        remote = self.box(big) / "home" / "org-runs" / "901-big-log-r1"
        remote.mkdir(parents=True)
        (remote / "runner.log").write_bytes(b"x" * 1_000_000 + b"\xff" + b"y" * 5_500_000 + b"\nthe last line\n")
        odd = self.finished("902-odd-log-r1")
        remote = self.box(odd) / "home" / "org-runs" / "902-odd-log-r1"
        remote.mkdir(parents=True)
        (remote / "runner.log").write_bytes(b"started\nnot utf-8: \xff\xfe\n")
        self.finished("903-no-dir-r1", remote_dir=None)
        self.finished("904-stopped-r1", running=False)
        self.assertEqual(sorted(dispatch.teardown(self.ledger)), ["901-big-log-r1", "902-odd-log-r1", "903-no-dir-r1", "904-stopped-r1"])
        text = (self.ledger.mirror_dir("901-big-log-r1") / "runner.log").read_text(encoding="utf-8")
        self.assertEqual(len(text), 5_000_000)
        self.assertTrue(text.endswith("y\nthe last line\n"))
        self.assertEqual((self.ledger.mirror_dir("902-odd-log-r1") / "runner.log").read_text(encoding="utf-8"),
                         "started\nnot utf-8: ��\n")
        for run_id in ("903-no-dir-r1", "904-stopped-r1"):
            self.assertFalse((self.ledger.mirror_dir(run_id) / "runner.log").exists(), run_id)
            self.removed(run_id)
        self.assertEqual(len(self.deletes()), 4)
        self.assertEqual([c for c in self.calls() if c[0] == "exec" and "903-no-dir" in json.dumps(c)], [],
                         "a record with no remote_dir is not asked for a log")


class WhatATickWroteMeanwhile(TeardownCase):
    """A final run's record is also written by the plan's tick, under its own lock. The deletion
    takes seconds, so it writes its own fields over the record as it is afterwards."""

    def test_what_the_plans_tick_wrote_during_the_deletion_is_kept(self):
        name = self.finished("903-reviewed-r1")

        def tick_meanwhile(ledger, run_id, rec, last=False):
            # While the container is being measured, a tick in another process marks a review started.
            there = self.ledger.run(run_id)
            there["review"] = {"state": "running", "attempts": 1}
            self.ledger.save_run(run_id, there)
            rec["telemetry"] = {"samples": 1}
            return None

        with mock.patch.object(dispatch.telemetry, "take", tick_meanwhile):
            self.assertEqual(dispatch.teardown(self.ledger), ["903-reviewed-r1"])
        rec = self.ledger.run("903-reviewed-r1")
        self.assertFalse(self.box(name).exists())
        self.assertEqual((rec.get("review"), rec.get("telemetry"), rec.get("machine_removed_by"), bool(rec.get("machine_removed_at"))),
                         ({"state": "running", "attempts": 1}, {"samples": 1}, "teardown", True))

    def test_what_the_plans_tick_wrote_during_a_deletion_that_failed_is_kept(self):
        name = self.finished("904-reviewed-r1")
        (self.box(name) / "refuse-delete").write_text("Failed to delete: the pool is busy\n")

        def tick_meanwhile(ledger, run_id, rec, last=False):
            there = self.ledger.run(run_id)
            there["review"] = {"state": "running", "attempts": 1}
            self.ledger.save_run(run_id, there)
            return None

        with mock.patch.object(dispatch.telemetry, "take", tick_meanwhile):
            self.assertEqual(dispatch.teardown(self.ledger), [])
        rec = self.ledger.run("904-reviewed-r1")
        self.assertEqual((rec.get("review"), rec["machine_remove_error"]["tries"], rec.get("machine_removed_at")),
                         ({"state": "running", "attempts": 1}, 1, None))

    def test_a_tick_that_holds_its_lock_for_long_does_not_hold_the_deletion_back_for_ever(self):
        name = self.finished("905-waited-r1")
        dispatch.graph._marker(self.ledger).write_text("{}\n")  # a plan was dispatched here once
        real, asked = dispatch.graph.writing, []

        def impatient(ledger):
            asked.append(ledger)
            return real(ledger, wait=0.1)  # the same lock, asked for a tenth of a second instead of five

        with real(self.ledger) as held:  # a tick in another process, as far as the lock can tell
            self.assertTrue(held)
            with mock.patch.object(dispatch.graph, "writing", impatient):
                self.assertEqual(dispatch.teardown(self.ledger), ["905-waited-r1"])
        self.assertEqual(len(asked), 1)
        self.assertFalse(self.box(name).exists())
        self.assertTrue(self.ledger.run("905-waited-r1").get("machine_removed_at"))


    def test_a_ledger_without_a_plan_gets_no_lock_file_of_plans_from_a_deletion(self):
        name = self.finished("906-alone-r1")
        self.assertEqual(dispatch.teardown(self.ledger), ["906-alone-r1"])
        self.assertFalse(self.box(name).exists())
        self.assertFalse((self.ledger.runs / ".graph.lock").exists())


class FailedDeletions(TeardownCase):
    def test_a_failed_deletion_is_recorded_and_tried_again_no_more_than_once_a_minute(self):
        name = self.finished("901-stuck-r1")
        (self.box(name) / "refuse-delete").write_text("Failed to delete: the storage pool is busy\n")
        log = []
        with mock.patch.object(dispatch.telemetry, "take", side_effect=lambda l, r, rec, last=False: rec.update(telemetry={"samples": 3})):
            self.assertEqual(dispatch.teardown(self.ledger, log.append), [])
        rec = self.ledger.run("901-stuck-r1")
        failed = rec["machine_remove_error"]
        self.assertIn("the storage pool is busy", failed["error"])
        self.assertEqual(failed["tries"], 1)
        self.assertLess(abs(failed["at"] - time.time()), 30)
        self.assertNotIn("machine_removed_at", rec)
        self.assertEqual(rec["telemetry"], {"samples": 3}, "what was measured is kept although the deletion failed")
        self.assertTrue(any("901-stuck-r1" in line and "storage pool is busy" in line for line in log), log)
        self.assertTrue(self.box(name).exists())
        self.assertEqual(len(self.deletes(name)), 1)
        # Within the minute it is not tried again, however often a pump looks.
        for _ in range(3):
            self.assertEqual(dispatch.teardown(self.ledger), [])
            self.assertEqual(dispatch.pump(self.ledger), [])
        self.assertEqual(len(self.deletes(name)), 1)
        # A minute later it is, and the count goes up.
        with mock.patch.object(dispatch, "now", return_value=time.time() + dispatch.REMOVE_RETRY_S + 1):
            self.assertEqual(dispatch.teardown(self.ledger), [])
        self.assertEqual(len(self.deletes(name)), 2)
        self.assertEqual(self.ledger.run("901-stuck-r1")["machine_remove_error"]["tries"], 2)
        # When Incus lets it go, the error is cleared and the run is marked.
        (self.box(name) / "refuse-delete").unlink()
        rec = self.ledger.run("901-stuck-r1")
        rec["machine_remove_error"]["at"] = time.time() - dispatch.REMOVE_RETRY_S - 1
        self.ledger.save_run("901-stuck-r1", rec)
        self.assertEqual(dispatch.teardown(self.ledger), ["901-stuck-r1"])
        self.assertEqual(len(self.deletes(name)), 3)
        self.assertFalse(self.box(name).exists())
        self.removed("901-stuck-r1")

    def test_a_failure_never_raises_out_of_pump_nor_keeps_a_queued_run_or_another_deletion_back(self):
        self.configure(slots=1)
        stuck = self.finished("901-stuck-r1")
        (self.box(stuck) / "refuse-delete").write_text("Failed to delete instance\n")
        odd = self.finished("902-odd-r1")
        fine = self.finished("903-fine-r1")
        real = IncusBackend.remove

        def remove(backend, machine, cfg):
            if machine["name"] == odd:
                raise RuntimeError("something nobody expected")
            return real(backend, machine, cfg)

        dispatch.drain(self.ledger, "for the test")
        queued, rec = dispatch.dispatch(self.ledger, self.approved(), log=lambda m: None)
        self.assertEqual(rec["state"], "queued")
        dispatch.undrain(self.ledger)
        log = []
        with mock.patch.object(IncusBackend, "remove", remove):
            self.assertEqual(dispatch.pump(self.ledger, log.append), [queued])
        self.assertEqual(self.ledger.run(queued)["state"], "dispatched")
        self.assertEqual(self.state(rec["machine"]["name"]), "running")
        self.assertIn("Failed to delete instance", self.ledger.run("901-stuck-r1")["machine_remove_error"]["error"])
        self.assertEqual(self.ledger.run("902-odd-r1")["machine_remove_error"]["error"], "something nobody expected")
        self.assertTrue(self.box(stuck).exists() and self.box(odd).exists())
        self.assertFalse(self.box(fine).exists())
        self.removed("903-fine-r1")
        self.assertTrue(any("902-odd-r1" in line for line in log), log)
        self.assertEqual(self.wait_final(queued)["state"], "done")

    def test_a_kind_config_no_longer_knows_is_recorded_as_a_failed_deletion(self):
        self.finished("901-warp-r1", machine={"kind": "warp", "name": "w1"})
        self.assertEqual(dispatch.teardown(self.ledger), [])
        failed = self.ledger.run("901-warp-r1")["machine_remove_error"]
        self.assertIn("no backend for machine kind 'warp'", failed["error"])
        self.assertEqual(failed["tries"], 1)

    def test_the_template_and_a_container_outside_the_prefix_can_never_be_named(self):
        self.container("org-cache", running=True)
        for run_id, name in (("901-template-r1", TEMPLATE), ("902-cache-r1", "org-cache")):
            self.finished(run_id, make=False, machine={"kind": "incus", "name": name, "base": TEMPLATE, "node": "vm"})
        self.assertEqual(dispatch.teardown(self.ledger), [])
        for run_id, name in (("901-template-r1", TEMPLATE), ("902-cache-r1", "org-cache")):
            rec = self.ledger.run(run_id)
            self.assertIn("org only acts on containers named org-eng-<run>", rec["machine_remove_error"]["error"])
            self.assertNotIn("machine_removed_at", rec)
            self.assertTrue((self.box(name) / "home").is_dir(), name)
        self.assertEqual([c for c in self.calls() if c[0] != "list"], [], "neither name ever reached incus")
        self.assertEqual(self.deletes(), [])


class TwoPumps(TeardownCase):
    def test_two_pumps_at_once_delete_each_machine_once_and_lose_nothing(self):
        names = {"90%d-both-r1" % i: self.finished("90%d-both-r1" % i) for i in range(1, 5)}
        barrier = threading.Barrier(2)
        results, errors = [], []

        def pumping():
            try:
                barrier.wait(10)
                results.append(dispatch.teardown(self.ledger))
            except Exception as e:  # noqa: BLE001  any exception is the test's failure
                errors.append(e)

        threads = [threading.Thread(target=pumping) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results[0] + results[1]), sorted(names), "each run was taken by one of them")
        for run_id, name in names.items():
            self.assertLessEqual(len(self.deletes(name)), 1, run_id)
            self.assertFalse(self.box(name).exists(), run_id)
            self.removed(run_id)


class WhatIsLeftAlone(TeardownCase):
    def test_a_machine_org_did_not_make_is_untouched_and_not_marked(self):
        for run_id, machine in (("901-local-r1", {"kind": "local", "name": "localhost"}),
                                ("902-ssh-r1", {"kind": "ssh", "name": "bigbox", "host": "me@bigbox"})):
            self.finished(run_id, make=False, machine=machine)
        before = {r: self.ledger.run(r) for r in ("901-local-r1", "902-ssh-r1")}
        take = mock.Mock(return_value=None)
        with mock.patch.object(dispatch.telemetry, "take", take):
            self.assertEqual(dispatch.teardown(self.ledger), [])
        self.assertEqual(take.call_count, 0)
        self.assertEqual({r: self.ledger.run(r) for r in before}, before)
        self.assertEqual(self.calls(), [])

    def test_a_container_already_gone_is_marked_removed_without_an_error(self):
        name = self.finished("901-gone-r1", make=False)
        log = []
        self.assertEqual(dispatch.teardown(self.ledger, log.append), ["901-gone-r1"])
        self.removed("901-gone-r1")
        self.assertEqual(self.deletes(), [])
        self.assertIn("901-gone-r1: %s was already gone" % name, log)

    def test_a_record_with_no_machine_or_no_machine_name_is_marked_removed_with_no_call(self):
        self.finished("901-no-machine-r1", make=False, machine=None)
        self.finished("902-no-name-r1", make=False, machine={"kind": "incus"})
        self.assertEqual(sorted(dispatch.teardown(self.ledger)), ["901-no-machine-r1", "902-no-name-r1"])
        for run_id in ("901-no-machine-r1", "902-no-name-r1"):
            self.removed(run_id)
        self.assertEqual(self.calls(), [])

    def test_a_run_stopped_while_queued_is_marked_removed_by_the_next_pump(self):
        dispatch.drain(self.ledger, "for the test")
        run_id, rec = dispatch.dispatch(self.ledger, self.approved(), log=lambda m: None)
        self.assertEqual(dispatch.stop(self.ledger, run_id), "removed from the queue")
        self.assertEqual(dispatch.pump(self.ledger), [])
        rec = self.removed(run_id)
        self.assertEqual(rec["state"], "stopped")
        self.assertEqual(self.deletes(), [])
        self.assertFalse(self.box(rec["machine"]["name"]).exists())


class TheSetting(TeardownCase):
    def test_after_finish_keep_deletes_nothing(self):
        self.after_finish_is(dispatch.AFTER_FINISH_KEEP)
        name = self.finished("901-kept-r1")
        self.assertEqual(dispatch.after_finish(self.ledger.config()), dispatch.AFTER_FINISH_KEEP)
        self.assertIsNone(dispatch.after_finish_problem(self.ledger.config()))
        self.assertEqual(dispatch.teardown(self.ledger), [])
        self.assertEqual(dispatch.pump(self.ledger), [])
        self.assertTrue(self.box(name).exists())
        self.assertEqual(self.calls(), [])
        self.assertNotIn("machine_removed_at", self.ledger.run("901-kept-r1"))

    def test_after_finish_mistyped_is_treated_as_keep_and_never_deletes(self):
        name = self.finished("901-kept-r1")
        for value in ("Delete", "delet", "DELETE", " delete", "Keep", "", 1, 0, True, False, ["delete"], {"delete": True}):
            self.after_finish_is(value)
            cfg = self.ledger.config()
            self.assertEqual(dispatch.after_finish(cfg), dispatch.AFTER_FINISH_KEEP, repr(value))
            problem = dispatch.after_finish_problem(cfg)
            self.assertIn("treated as keep", problem or "", repr(value))
            self.assertIn(json.dumps(value), problem)
            self.assertEqual(dispatch.teardown(self.ledger), [], repr(value))
        self.assertTrue(self.box(name).exists())
        self.assertEqual(self.deletes(), [])

    def test_after_finish_absent_null_or_delete_deletes(self):
        for how in ({"absent": True}, {"value": None}, {"value": dispatch.AFTER_FINISH_DELETE}):
            self.after_finish_is(**how)
            cfg = self.ledger.config()
            self.assertEqual((dispatch.after_finish(cfg), dispatch.after_finish_problem(cfg)), (dispatch.AFTER_FINISH_DELETE, None), how)
        self.assertEqual(dispatch.after_finish({}), dispatch.AFTER_FINISH_DELETE)
        self.assertEqual(dispatch.after_finish({"machines": "nonsense"}), dispatch.AFTER_FINISH_DELETE)

    def test_after_finish_keep_changed_to_delete_deletes_on_the_next_pump(self):
        self.after_finish_is(dispatch.AFTER_FINISH_KEEP)
        names = [self.finished("90%d-kept-r1" % i, state=s) for i, s in enumerate(("done", "failed"), 1)]
        self.assertEqual(dispatch.pump(self.ledger), [])
        self.assertTrue(all(self.box(n).exists() for n in names))
        self.after_finish_is(dispatch.AFTER_FINISH_DELETE)
        self.assertEqual(dispatch.pump(self.ledger), [])
        self.assertFalse(any(self.box(n).exists() for n in names))
        for i in (1, 2):
            self.removed("90%d-kept-r1" % i)

    def test_after_finish_rule_has_no_cut_off_every_run_already_final_is_deleted_by_the_first_pump_each_measured_first(self):
        runs = {"90%d-old-r1" % i: self.finished("90%d-old-r1" % i, state=s, created_at=1_700_000_000 + i)
                for i, s in enumerate(("done", "partial", "done", "failed", "stopped"), 1)}
        seen = []

        def take(ledger, run_id, rec, last=False):
            seen.append((run_id, last, self.box(runs[run_id]).exists()))

        with mock.patch.object(dispatch.telemetry, "take", side_effect=take):
            self.assertEqual(dispatch.pump(self.ledger), [])
        self.assertEqual(sorted(seen), sorted((r, True, True) for r in runs))
        for run_id, name in runs.items():
            self.assertFalse(self.box(name).exists())
            self.removed(run_id)


class Docker(TeardownCase):
    def test_a_docker_machine_is_deleted_through_its_backend(self):
        os.symlink(ROOT / "tests" / "fake_docker.py", self.bindir / "docker")
        docker = self.tmp / "docker"
        os.environ["FAKE_DOCKER_ROOT"] = str(docker)
        cfg = read_json(self.ledger.config_path)
        cfg["machines"]["plugins"] = ["org.backends.docker"]
        write_json(self.ledger.config_path, cfg)
        name = "org-eng-901-docker-r1"
        (docker / name / "home").mkdir(parents=True)
        self.finished("901-docker-r1", make=False, machine={"kind": "docker", "name": name, "node": "local", "host": None})
        self.assertEqual(dispatch.teardown(self.ledger), ["901-docker-r1"])
        self.assertFalse((docker / name).exists())
        self.removed("901-docker-r1")
        argvs = [json.loads(line)["argv"] for line in (docker / "calls.log").read_text().splitlines()]
        self.assertIn(["rm", "-f", name], argvs)
        self.assertEqual(self.calls(), [], "incus was asked for nothing")


class Afterwards(TeardownCase):
    def test_resume_refuses_a_run_whose_container_was_deleted_when_it_ended(self):
        self.finished("901-over-r1")
        self.assertEqual(dispatch.teardown(self.ledger), ["901-over-r1"])
        before = len(self.calls())
        with self.assertRaises(dispatch.DispatchError) as ctx:
            dispatch.resume(self.ledger, "901-over-r1")
        self.assertEqual(str(ctx.exception), "901-over-r1's container was deleted when the run ended, and a finished run cannot be continued")
        self.finished("902-rm-r1", machine_removed_at=time.time(), machine_removed_by="machine rm")
        with self.assertRaises(dispatch.DispatchError) as ctx:
            dispatch.resume(self.ledger, "902-rm-r1")
        self.assertIn("org machine rm", str(ctx.exception))
        self.assertEqual(self.calls()[before:], [], "incus was asked for nothing")

    def test_resume_and_machine_rm_refuse_while_another_process_holds_the_run(self):
        self.finished("901-busy-r1")

        @contextmanager
        def busy(run_id, wait=0.0):
            yield False

        with mock.patch.object(self.ledger, "run_lock", busy):
            for act in (dispatch.resume, dispatch.remove_machine):
                with self.assertRaises(dispatch.DispatchError) as ctx:
                    act(self.ledger, "901-busy-r1")
                self.assertIn("try again", str(ctx.exception))
        self.assertEqual(self.calls(), [])

    def org(self, *args):
        return subprocess.run([sys.executable, "-m", "org", "--home", str(self.ledger.root)] + list(args), capture_output=True,
                              text=True, cwd=str(ROOT), stdin=subprocess.DEVNULL, timeout=60)

    def test_machine_rm_still_works_under_keep_and_with_force_for_a_run_not_final(self):
        self.after_finish_is(dispatch.AFTER_FINISH_KEEP)
        kept = self.finished("901-kept-r1")
        p = self.org("machine", "rm", "901-kept-r1")
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "deleted " + kept), p.stderr)
        rec = self.ledger.run("901-kept-r1")
        self.assertEqual(rec["machine_removed_by"], "machine rm")
        self.assertTrue(rec["machine_removed_at"])
        going = self.finished("902-going-r1", state="executing", final=False)
        p = self.org("machine", "rm", "902-going-r1")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("pass --force", p.stderr + p.stdout)
        self.assertTrue(self.box(going).exists())
        p = self.org("machine", "rm", "902-going-r1", "--force")
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "deleted " + going), p.stderr)
        self.assertFalse(self.box(going).exists())
        self.assertEqual(self.ledger.run("902-going-r1")["machine_removed_by"], "machine rm")

    def test_machine_rm_measures_first_clears_an_earlier_failure_and_still_raises_the_backends_error(self):
        name = self.finished("901-stuck-r1")
        (self.box(name) / "refuse-delete").write_text("Failed to delete: busy\n")
        self.assertEqual(dispatch.teardown(self.ledger), [])
        self.assertEqual(self.ledger.run("901-stuck-r1")["machine_remove_error"]["tries"], 1)
        take = mock.Mock(return_value=None)
        with mock.patch.object(dispatch.telemetry, "take", take):
            with self.assertRaises(dispatch.TransportError) as ctx:
                dispatch.remove_machine(self.ledger, "901-stuck-r1")
            self.assertIn("busy", str(ctx.exception))
            (self.box(name) / "refuse-delete").unlink()
            self.assertEqual(dispatch.remove_machine(self.ledger, "901-stuck-r1"), "deleted " + name)
        self.assertEqual(take.call_count, 2)
        self.assertEqual(take.call_args.kwargs, {"last": True})
        rec = self.ledger.run("901-stuck-r1")
        self.assertEqual(rec["machine_removed_by"], "machine rm")
        self.assertNotIn("machine_remove_error", rec)
        # Asked again, it says the machine is gone and changes nothing on the record.
        self.assertEqual(dispatch.remove_machine(self.ledger, "901-stuck-r1"), name + " was already gone")
        self.assertEqual(take.call_count, 2)
        self.assertEqual(self.ledger.run("901-stuck-r1"), rec)


class Doctor(TeardownCase):
    ROW = "finished runs' machines"

    def rows(self):
        p = subprocess.run([sys.executable, "-m", "org", "--home", str(self.ledger.root), "doctor"], capture_output=True, text=True,
                           cwd=str(ROOT), stdin=subprocess.DEVNULL, timeout=60)
        rows = {m.group(2): (m.group(1), m.group(3)) for m in re.finditer(r"^(ok|warn|FAIL) +(.+?) {2,}(.*)$", p.stdout, re.M)}
        self.assertIn(self.ROW, rows, p.stdout + p.stderr)
        return rows, p.stdout

    def test_doctor_says_what_after_finish_does_with_each_value(self):
        rows, _ = self.rows()
        self.assertEqual(rows[self.ROW], ("ok", "deleted when the run ends (machines.after_finish)"))
        self.after_finish_is(dispatch.AFTER_FINISH_KEEP)
        rows, _ = self.rows()
        self.assertEqual(rows[self.ROW], ("ok", "kept until `org machine rm` (machines.after_finish keep)"))
        self.after_finish_is("Delete")
        rows, out = self.rows()
        level, found = rows[self.ROW]
        self.assertEqual(level, "warn", "nothing is deleted, so it is a warning and not a failure")
        self.assertIn('"Delete"', found)
        self.assertIn("treated as keep", found)
        self.assertIn("org config set machines.after_finish keep, or `delete`", out)


if __name__ == "__main__":
    unittest.main()
