"""What a run's container measured: read from a simulated Incus and simulated kernel files, kept in machine.jsonl and the record."""
from __future__ import annotations

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

from org import telemetry  # noqa: E402
from org.transport import Incus, TransportError  # noqa: E402
from test_incus import IncusCase  # noqa: E402

MIB = 1024 * 1024
# What Incus 6.0.4 says of a running container, as measured: usage_peak is left at 0.
STATE = {"status": "Running", "disk": {}, "pid": 17306, "processes": 16, "started_at": "2026-11-25T04:59:23.995237712Z",
         "memory": {"usage": 21315584, "usage_peak": 0, "total": 268435456}, "cpu": {"usage": 608788000}}


class Measuring(IncusCase):
    def a_run(self, run_id="001-measured-r1", kind="incus", **extra):
        """A run record made by hand, as dispatch leaves one, with its machine named by the backend's prefix."""
        name = "org-eng-" + run_id
        rec = {"run_id": run_id, "state": "dispatched", "remote_dir": "$HOME/org-runs/" + run_id,
               "machine": {"kind": kind, "name": name}, "events_offset": 0}
        rec.update(extra)
        self.ledger.save_run(run_id, rec)
        return run_id, name

    def lines(self, run_id):
        path = telemetry.path(self.ledger, run_id)
        return [json.loads(row) for row in path.read_text().splitlines()] if path.exists() else []

    def lists(self, name):
        """The `incus list` calls made about one container."""
        return [c for c in self.calls() if c and c[0] == "list" and "^%s$" % name in c]

    def take(self, run_id, last=False):
        rec = self.ledger.run(run_id)
        line = telemetry.take(self.ledger, run_id, rec, last=last)
        self.ledger.save_run(run_id, rec)
        return line, rec

    def later(self, seconds=61):
        """This module's clock moved on, so the minute between measurements can pass without waiting for it."""
        return mock.patch.object(telemetry, "now", return_value=time.time() + seconds)


class Readings(Measuring):
    def test_the_kernel_files_of_a_container_in_the_default_project_are_read(self):
        run_id, name = self.a_run(alive=True)
        self.container(name, running=True, state=STATE)
        self.cgroup(name, current=300 * MIB, peak=900 * MIB, max=2048 * MIB, pids=42, oom_kill=2, cpu_usec=12_500_000)
        line, rec = self.take(run_id)
        self.assertEqual((line["memory_bytes"], line["memory_peak_bytes"], line["memory_limit_bytes"], line["processes"],
                          line["oom_kill"], line["cpu_s"], line["running"], line["runner_alive"]),
                         (300 * MIB, 900 * MIB, 2048 * MIB, 42, 2, 12.5, True, True))
        self.assertEqual(line["started_at"], STATE["started_at"])
        self.assertIsNone(line["note"])
        self.assertNotIn("last", line)
        self.assertEqual(self.lines(run_id), [line])
        self.assertEqual(self.ledger.run(run_id)["telemetry"]["samples"], 1)
        self.assertEqual(rec["oom_kill"], 2)

    def test_a_container_in_a_project_of_its_own_is_read_from_the_project_s_directory(self):
        run_id, name = self.a_run()
        self.container(name, running=True, project="user-2000")
        self.cgroup(name, project="user-2000", current=1, peak=2, max=3, pids=4, oom_kill=5, cpu_usec=6_000_000)
        self.cgroup(name, current=999, peak=999, max=999, pids=999, oom_kill=999, cpu_usec=999)  # the default project's: not this one
        line, _ = self.take(run_id)
        self.assertEqual((line["memory_bytes"], line["memory_peak_bytes"], line["memory_limit_bytes"], line["processes"],
                          line["oom_kill"], line["cpu_s"]), (1, 2, 3, 4, 5, 6.0))
        self.assertIsNone(line["note"])

    def test_a_project_that_is_not_a_plain_name_reads_no_kernel_file_and_says_so(self):
        run_id, name = self.a_run()
        self.container(name, running=True, project="../escape")
        self.cgroup(name, current=1, oom_kill=5)
        line, _ = self.take(run_id)
        self.assertIsNone(line["oom_kill"])
        self.assertIsNone(line["memory_bytes"])
        self.assertIn("not a plain name", line["note"])

    def test_incus_s_state_stands_in_where_the_kernel_s_directory_is_missing_and_a_peak_of_zero_is_not_read(self):
        run_id, name = self.a_run()
        self.container(name, running=True, state=STATE, expanded_config={"limits.memory": "2219MiB"})
        line, rec = self.take(run_id)
        self.assertEqual((line["memory_bytes"], line["cpu_s"], line["processes"], line["memory_limit_bytes"]),
                         (21315584, 0.609, 16, 2219 * MIB))
        self.assertIsNone(line["memory_peak_bytes"], "usage_peak 0 is Incus not filling it in")
        self.assertIsNone(line["oom_kill"], "kills come from the kernel's files only")
        self.assertIn("directory", line["note"])
        self.assertIsNone(rec["telemetry"]["oom_kill"])
        self.assertIsNone(rec["telemetry"]["memory_peak_bytes"])

    def test_a_peak_above_zero_from_incus_is_taken_when_the_kernel_has_none(self):
        run_id, name = self.a_run()
        self.container(name, running=True, state=dict(STATE, memory={"usage": 5, "usage_peak": 77}))
        self.cgroup(name, current=10)
        line, _ = self.take(run_id)
        self.assertEqual((line["memory_bytes"], line["memory_peak_bytes"]), (10, 77))

    def test_a_kernel_limit_of_max_falls_back_to_the_container_s_limit_and_then_to_null(self):
        run_id, name = self.a_run()
        self.container(name, running=True, expanded_config={"limits.memory": "2GiB"})
        self.cgroup(name, max="max")
        line, _ = self.take(run_id)
        self.assertEqual(line["memory_limit_bytes"], 2048 * MIB)
        self.assertNotIn("memory.max", line["note"], "the word max was read: it is not a failure")
        self.describe(name, expanded_config={"limits.memory": "a lot"})
        with self.later():
            line, _ = self.take(run_id)
        self.assertIsNone(line["memory_limit_bytes"])

    def test_one_missing_or_unparsable_file_makes_only_its_value_null_and_is_named_in_the_note(self):
        run_id, name = self.a_run()
        self.container(name, running=True)
        d = self.cgroup(name, current=100, peak=200, max=300, pids=4, oom_kill=1, cpu_usec=1_000_000)
        self.cgroup(name, peak=None)
        (d / "pids.current").write_text("-3\n")
        line, _ = self.take(run_id)
        self.assertEqual((line["memory_bytes"], line["memory_limit_bytes"], line["oom_kill"], line["cpu_s"]), (100, 300, 1, 1.0))
        self.assertIsNone(line["memory_peak_bytes"])
        self.assertIsNone(line["processes"])
        self.assertIn("memory.peak", line["note"])
        self.assertIn("pids.current", line["note"])

    def test_a_stopped_container_gives_nulls_and_a_note_and_its_state_s_zeros_are_not_taken(self):
        run_id, name = self.a_run()
        self.container(name, running=False, state={"status": "Stopped", "processes": 0, "memory": {"usage": 0}, "cpu": {"usage": 0},
                                                   "started_at": "0001-01-01T00:00:00Z"})
        line, rec = self.take(run_id)
        self.assertFalse(line["running"])
        for key in ("memory_bytes", "memory_peak_bytes", "cpu_s", "processes", "oom_kill", "started_at", "disk_bytes"):
            self.assertIsNone(line[key], key)
        self.assertIn("not running", line["note"])
        self.assertEqual(rec["telemetry"]["note"], line["note"])
        self.assertFalse(rec["telemetry"]["running"])

    def test_a_container_incus_does_not_know_gives_nulls_and_a_note(self):
        run_id, name = self.a_run()
        line, _ = self.take(run_id)
        self.assertIsNone(line["memory_bytes"])
        self.assertIn("does not know", line["note"])

    def test_incus_failing_gives_nulls_and_a_note_and_no_exception(self):
        run_id, name = self.a_run()
        self.container(name, running=True)
        self.cgroup(name, current=1, oom_kill=1)
        with mock.patch.object(Incus, "info", side_effect=TransportError("incus list failed: the daemon is down")):
            line, rec = self.take(run_id)
        self.assertIsNone(line["memory_bytes"])
        self.assertIsNone(line["oom_kill"])
        self.assertIn("Incus did not answer", line["note"])
        self.assertIn("the daemon is down", line["note"])
        self.assertEqual(rec["telemetry"]["samples"], 1)

    def test_a_name_outside_the_prefix_is_never_given_to_incus(self):
        run_id, _ = self.a_run()
        rec = self.ledger.run(run_id)
        rec["machine"]["name"] = "org-base"
        line = telemetry.take(self.ledger, run_id, rec)
        self.assertIn("not asked", line["note"])
        self.assertEqual([c for c in self.calls() if c and c[0] == "list"], [])

    def test_disk_is_taken_where_the_pool_reports_it_and_null_where_it_does_not(self):
        run_id, name = self.a_run()
        self.container(name, running=True, state=dict(STATE, disk={"root": {"usage": 4096, "total": 8192}}))
        line, rec = self.take(run_id)
        self.assertEqual((line["disk_bytes"], rec["telemetry"]["disk_bytes"]), (4096, 4096))
        self.describe(name, state=STATE)
        with self.later():
            line, rec = self.take(run_id)
        self.assertIsNone(line["disk_bytes"])
        self.assertEqual(rec["telemetry"]["disk_bytes"], 4096, "the last value seen")

    def test_numbers_that_are_not_whole_and_not_negative_are_not_taken(self):
        run_id, name = self.a_run(alive="yes")
        self.container(name, running=True, state={"processes": True, "memory": {"usage": -1, "usage_peak": "9"}, "cpu": {"usage": 1.5}})
        line, _ = self.take(run_id)
        for key in ("memory_bytes", "memory_peak_bytes", "cpu_s", "processes", "runner_alive"):
            self.assertIsNone(line[key], key)

    def test_the_last_measurement_says_it_is_the_last(self):
        run_id, name = self.a_run()
        self.container(name, running=True)
        line, _ = self.take(run_id, last=True)
        self.assertIs(line["last"], True)
        self.assertIs(self.lines(run_id)[0]["last"], True)


class Summary(Measuring):
    def test_a_counter_that_went_down_is_a_restart_and_the_totals_add_up(self):
        lines = [{"at": 1, "cpu_s": 10.0, "oom_kill": 1}, {"at": 2, "cpu_s": 30.0, "oom_kill": 2},
                 {"at": 3, "cpu_s": 5.0, "oom_kill": 0}, {"at": 4, "cpu_s": 8.0, "oom_kill": 1}]
        s = telemetry.summarize(lines)
        self.assertEqual((s["samples"], s["first_at"], s["last_at"], s["restarts"], s["cpu_s"], s["oom_kill"]), (4, 1, 4, 1, 38.0, 3))

    def test_a_started_at_that_changed_is_a_restart_counted_once(self):
        lines = [{"at": 1, "cpu_s": 10.0, "started_at": "A"}, {"at": 2, "cpu_s": 20.0, "started_at": None},
                 {"at": 3, "cpu_s": 2.0, "started_at": "B"}, {"at": 4, "cpu_s": 4.0, "started_at": "B"}]
        s = telemetry.summarize(lines)
        self.assertEqual((s["restarts"], s["cpu_s"]), (1, 24.0))

    def test_a_started_at_that_changed_with_counters_that_rose_still_keeps_the_life_before(self):
        lines = [{"at": 1, "cpu_s": 10.0, "started_at": "A"}, {"at": 2, "cpu_s": 50.0, "started_at": "B"}]
        s = telemetry.summarize(lines)
        self.assertEqual((s["restarts"], s["cpu_s"]), (1, 60.0))

    def test_the_largest_peak_is_kept_and_the_last_limit_and_disk(self):
        lines = [{"at": 1, "memory_peak_bytes": 5, "memory_limit_bytes": 100, "disk_bytes": 7},
                 {"at": 2, "memory_peak_bytes": 9, "memory_limit_bytes": 200},
                 {"at": 3, "memory_peak_bytes": 3, "memory_limit_bytes": None, "disk_bytes": None, "running": False, "note": "Gone."}]
        s = telemetry.summarize(lines)
        self.assertEqual((s["memory_peak_bytes"], s["memory_limit_bytes"], s["disk_bytes"], s["running"], s["note"]),
                         (9, 200, 7, False, "Gone."))

    def test_a_counter_never_read_stays_null_not_zero(self):
        s = telemetry.summarize([{"at": 1, "cpu_s": None, "oom_kill": None}, {"at": 2}])
        self.assertIsNone(s["cpu_s"])
        self.assertIsNone(s["oom_kill"])
        self.assertIsNone(s["memory_peak_bytes"])
        self.assertEqual(s["restarts"], 0)

    def test_an_empty_file_summarizes_to_none(self):
        run_id, _ = self.a_run()
        telemetry.path(self.ledger, run_id).write_text("")
        self.assertEqual(telemetry.load(self.ledger, run_id), [])
        self.assertIsNone(telemetry.summarize(telemetry.load(self.ledger, run_id)))
        self.assertIsNone(telemetry.summarize([]))

    def test_garbage_lines_in_the_file_are_passed_over(self):
        run_id, name = self.a_run()
        self.container(name, running=True)
        self.cgroup(name, oom_kill=2)
        telemetry.path(self.ledger, run_id).write_text('not json\n[1, 2]\n"text"\n{"at": 1, "oom_kill": 1}\n{"at": 2, "oom_k')
        line, rec = self.take(run_id)
        self.assertEqual(len(telemetry.load(self.ledger, run_id)), 2)
        self.assertEqual(telemetry.load(self.ledger, run_id)[-1], line, "a line cut short before it does not swallow this one")
        self.assertEqual((rec["telemetry"]["samples"], rec["telemetry"]["oom_kill"]), (2, 2))

    def test_a_restarted_container_read_through_take_adds_its_lives(self):
        run_id, name = self.a_run()
        self.container(name, running=True)
        self.cgroup(name, cpu_usec=100_000_000, oom_kill=3, peak=500)
        self.take(run_id)
        self.cgroup(name, cpu_usec=20_000_000, oom_kill=1, peak=50)
        with self.later():
            _, rec = self.take(run_id)
        t = rec["telemetry"]
        self.assertEqual((t["restarts"], t["cpu_s"], t["oom_kill"], t["memory_peak_bytes"], t["samples"]), (1, 120.0, 4, 500, 2))
        self.assertEqual(rec["oom_kill"], 4)

    def test_the_record_s_count_of_kills_is_raised_and_never_lowered(self):
        run_id, name = self.a_run(oom_kill=1)
        self.container(name, running=True)
        self.cgroup(name, oom_kill=4)
        _, rec = self.take(run_id)
        self.assertEqual(rec["oom_kill"], 4)
        # A snapshot said more: the record keeps it, and the summary is still only what was measured.
        rec["oom_kill"] = 9
        self.ledger.save_run(run_id, rec)
        with self.later():
            _, rec = self.take(run_id)
        self.assertEqual((rec["oom_kill"], rec["telemetry"]["oom_kill"]), (9, 4))
        # A lower value written on the record by something else does not lower the summary, and is raised again.
        rec["oom_kill"] = 0
        self.ledger.save_run(run_id, rec)
        with self.later(122):
            _, rec = self.take(run_id)
        self.assertEqual((rec["oom_kill"], rec["telemetry"]["oom_kill"]), (4, 4))
        self.assertEqual(self.ledger.run(run_id)["oom_kill"], 4)


class BetweenGoodMeasurements(Measuring):
    GOOD = [{"at": 1.0, "cpu_s": 10.0, "oom_kill": 2, "memory_peak_bytes": 5, "running": True},
            {"at": 3.0, "cpu_s": 20.0, "oom_kill": 3, "memory_peak_bytes": 7, "running": True}]
    UNREAD = {"cpu_s": None, "oom_kill": None, "memory_peak_bytes": None, "running": None, "note": "Incus did not answer"}

    def test_a_measurement_that_failed_between_two_good_ones_is_no_restart_and_loses_nothing(self):
        s = telemetry.summarize([self.GOOD[0], dict(self.UNREAD, at=2.0), self.GOOD[1]])
        self.assertEqual((s["restarts"], s["cpu_s"], s["oom_kill"], s["memory_peak_bytes"], s["samples"]), (0, 20.0, 3, 7, 3))

    def test_last_measurements_of_a_container_already_gone_change_no_total(self):
        gone = dict(self.UNREAD, running=False, last=True, note="Incus does not know the container")
        s = telemetry.summarize(self.GOOD + [dict(gone, at=4.0), dict(gone, at=65.0)])
        self.assertEqual((s["restarts"], s["cpu_s"], s["oom_kill"], s["memory_peak_bytes"], s["samples"], s["last_at"]),
                         (0, 20.0, 3, 7, 4, 65.0))

    def test_the_one_call_is_given_the_measurement_s_own_timeout(self):
        run_id, name = self.a_run()
        self.container(name, running=True)
        with mock.patch.object(Incus, "info", return_value=None) as info:
            self.take(run_id)
        call = info.call_args
        self.assertEqual(call.args[1] if len(call.args) > 1 else call.kwargs.get("timeout"), telemetry.CALL_TIMEOUT_S)


class Due(Measuring):
    def test_one_incus_call_per_measurement_and_none_within_the_minute(self):
        run_id, name = self.a_run()
        self.container(name, running=True)
        self.cgroup(name, oom_kill=0)
        self.assertEqual(telemetry.measure_due(self.ledger), [run_id])
        self.assertEqual(len(self.lists(name)), 1)
        self.assertEqual(len([c for c in self.calls() if c and c[0] != "list"]), 0, "nothing but the one list")
        self.assertEqual(telemetry.measure_due(self.ledger), [])
        self.assertEqual(len(self.lists(name)), 1)
        self.assertEqual(len(self.lines(run_id)), 1)
        with self.later():
            self.assertEqual(telemetry.measure_due(self.ledger), [run_id])
        self.assertEqual(len(self.lists(name)), 2)
        self.assertEqual(self.ledger.run(run_id)["telemetry"]["samples"], 2)

    def test_dueness_comes_from_the_file_not_from_the_record(self):
        run_id, name = self.a_run()
        self.container(name, running=True)
        self.assertEqual(telemetry.measure_due(self.ledger), [run_id])
        rec = self.ledger.run(run_id)
        rec.pop("telemetry")  # as though the write of the record had been lost to another writer
        self.ledger.save_run(run_id, rec)
        self.assertEqual(telemetry.measure_due(self.ledger), [])
        self.assertEqual(len(self.lists(name)), 1)

    def test_final_queued_and_removed_machine_runs_are_not_measured(self):
        cases = [self.a_run("001-final-r1", final=True, state="done"), self.a_run("002-queued-r1", state="queued"),
                 self.a_run("003-removed-r1", state="done", machine_removed_at=1.0),
                 self.a_run("004-provisioning-r1", state="provisioning"), self.a_run("005-failed-r1", state="dispatch_failed")]
        for run_id, name in cases:
            self.container(name, running=True)
        self.assertEqual(telemetry.measure_due(self.ledger), [])
        for run_id, name in cases:
            self.assertFalse(telemetry.path(self.ledger, run_id).exists(), run_id)
        self.assertEqual([c for c in self.calls() if c and c[0] == "list"], [])

    def test_a_docker_or_local_machine_is_not_measured_and_gets_no_file(self):
        docker, _ = self.a_run("001-docker-r1", kind="docker")
        local, _ = self.a_run("002-local-r1", kind="local")
        self.assertEqual(telemetry.measure_due(self.ledger), [])
        for run_id in (docker, local):
            rec = self.ledger.run(run_id)
            self.assertIsNone(telemetry.take(self.ledger, run_id, rec, last=True))
            self.assertNotIn("telemetry", rec)
            self.assertFalse(telemetry.path(self.ledger, run_id).exists())
        self.assertEqual([c for c in self.calls() if c and c[0] == "list"], [])

    def test_a_run_whose_lock_another_holds_is_left_for_the_next_pump(self):
        run_id, name = self.a_run()
        self.container(name, running=True)
        with self.ledger.run_lock(run_id) as held:
            self.assertTrue(held)
            self.assertEqual(telemetry.measure_due(self.ledger), [])
        self.assertFalse(telemetry.path(self.ledger, run_id).exists())
        self.assertEqual(self.lists(name), [])
        self.assertEqual(telemetry.measure_due(self.ledger), [run_id])

    def test_two_pumps_measuring_at_once_write_one_line(self):
        run_id, name = self.a_run()
        self.container(name, running=True)
        real, gate = Incus.info, threading.Barrier(2, timeout=5)

        def slow(container, timeout=60):
            time.sleep(0.3)  # long enough that the second pump arrives while the first is measuring
            return real(container, timeout)

        results = []

        def pump():
            try:
                gate.wait()
            except threading.BrokenBarrierError:
                pass
            results.append(telemetry.measure_due(self.ledger))

        with mock.patch.object(Incus, "info", side_effect=slow):
            threads = [threading.Thread(target=pump) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(sorted(results), [[], [run_id]])
        self.assertEqual(len(self.lines(run_id)), 1)
        self.assertEqual(self.ledger.run(run_id)["telemetry"]["samples"], 1)

    def test_several_runs_are_measured_side_by_side(self):
        names = [self.a_run("00%d-many-r1" % i) for i in range(1, 5)]
        for _, name in names:
            self.container(name, running=True)
        real = Incus.info

        def slow(container, timeout=60):
            time.sleep(0.5)
            return real(container, timeout)

        with mock.patch.object(Incus, "info", side_effect=slow):
            started = time.time()
            self.assertEqual(sorted(telemetry.measure_due(self.ledger)), sorted(r for r, _ in names))
            self.assertLess(time.time() - started, 1.95, "four calls of half a second each, waited for side by side")


class NeverRaises(Measuring):
    def test_an_odd_record_neither_raises_nor_writes_a_measurement(self):
        run_id, _ = self.a_run()
        for rec in ({}, {"machine": None}, {"machine": "org-eng-x"}, {"machine": {"kind": "incus"}}, {"machine": {"kind": "incus", "name": 7}}):
            rec = dict(rec)
            line = telemetry.take(self.ledger, run_id, rec)
            if line is not None:
                self.assertIsNotNone(line["note"], rec)
                self.assertIsNone(line["memory_bytes"])
        rid = "002-odd-r1"
        self.ledger.save_run(rid, {"run_id": rid, "state": "dispatched"})  # machine missing
        self.ledger.save_run("003-odd-r1", {"run_id": "003-odd-r1", "state": "dispatched", "machine": {"kind": "incus"}})
        (self.ledger.run_dir("004-odd-r1")).mkdir(parents=True)
        (self.ledger.run_dir("004-odd-r1") / "run.json").write_text("[1, 2]")
        logged = []
        measured = telemetry.measure_due(self.ledger, logged.append)
        self.assertNotIn(rid, measured)
        self.assertFalse(telemetry.path(self.ledger, rid).exists())

    def test_a_file_that_cannot_be_written_costs_the_measurement_and_raises_nothing(self):
        run_id, name = self.a_run()
        self.container(name, running=True)
        telemetry.path(self.ledger, run_id).mkdir()  # nothing can be appended to a directory, whoever runs the test
        rec = self.ledger.run(run_id)
        self.assertIsNone(telemetry.take(self.ledger, run_id, rec))
        self.assertNotIn("telemetry", rec)
        self.assertEqual(telemetry.measure_due(self.ledger), [])

    def test_a_lock_that_cannot_be_opened_or_a_runs_directory_that_cannot_be_read_raises_nothing(self):
        run_id, name = self.a_run()
        self.container(name, running=True)
        (self.ledger.run_dir(run_id) / ".sync.lock").mkdir()
        logged = []
        self.assertEqual(telemetry.measure_due(self.ledger, logged.append), [])
        self.assertTrue(logged)
        with mock.patch.object(type(self.ledger), "run_ids", side_effect=OSError("the runs directory is unreadable")):
            self.assertEqual(telemetry.measure_due(self.ledger), [])


if __name__ == "__main__":
    unittest.main()
