"""What people see of a run's machine: the view, `org status`, `org review`, `org peek`."""
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from org import status  # noqa: E402
from org.ledger import Ledger  # noqa: E402
from org.util import write_json  # noqa: E402

RUN = "001-thing-r1"
MIB = 1048576


def summary(**kw):
    t = {"samples": 37, "first_at": 1000000.0, "last_at": 1002900.0, "memory_peak_bytes": 3898 * MIB,
         "memory_limit_bytes": 4439 * MIB, "cpu_s": 908.4, "oom_kill": 19, "restarts": 0,
         "disk_bytes": int(1.2 * 1024 ** 3), "running": False, "note": None}
    t.update(kw)
    return t


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ledger = Ledger(Path(self.tmp) / "org")
        self.ledger.init()

    def run_with(self, rec=None, st=None, actuals=None, kind="incus"):
        base = {"run_id": RUN, "spec_id": "001-thing", "state": "finished", "final": True,
                "machine": {"kind": kind, "name": "org-eng-009-abcdef"}, "attach": "incus exec org-eng-009-abcdef -- bash"}
        base.update(rec or {})
        self.ledger.save_run(RUN, base)
        mirror = self.ledger.mirror_dir(RUN)
        mirror.mkdir(parents=True, exist_ok=True)
        write_json(mirror / "status.json", dict({"state": "done", "elapsed_s": 2880}, **(st or {})))
        if actuals is not None:
            write_json(mirror / "actuals.json", actuals)
            write_json(mirror / "result.json", {"status": "done", "msg": "ok"})
        return status.run_view(self.ledger, RUN, None)

    def removed(self, **more):
        rec = {"machine_removed_at": time.time() - 86400 * 2, "machine_removed_by": "teardown", "telemetry": summary()}
        rec.update(more)
        return rec


class View(Case):
    def test_the_view_of_a_removed_run_offers_no_way_in_and_carries_the_summary(self):
        v = self.run_with(self.removed())
        self.assertIsNone(v["attach"])
        self.assertTrue(v["machine"]["removed"])
        self.assertTrue(v["machine"]["removed_at"])
        self.assertEqual(v["machine"]["removed_by"], "teardown")
        self.assertTrue(v["telemetry"]["measured"])
        self.assertEqual(v["telemetry"]["oom_kill"], 19)

    def test_a_run_whose_machine_is_there_keeps_its_attach(self):
        v = self.run_with({"telemetry": summary()})
        self.assertTrue(v["attach"])
        self.assertFalse(v["machine"]["removed"])

    def test_a_docker_machine_is_not_measured_and_says_so(self):
        self.assertEqual(self.run_with(kind="docker")["telemetry"], {"measured": False, "why": "docker machines are not measured"})

    def test_an_ssh_machine_is_not_measured_and_says_so(self):
        self.assertEqual(self.run_with(kind="ssh")["telemetry"]["why"], "ssh machines are not measured")

    def test_a_local_machine_is_not_measured_and_says_so(self):
        self.assertEqual(self.run_with(kind="local")["telemetry"]["why"], "local machines are not measured")

    def test_an_incus_run_with_no_measurements_says_the_run_ended_before_org_measured(self):
        t = self.run_with()["telemetry"]
        self.assertFalse(t["measured"])
        self.assertIn("ended before org measured containers", t["why"])
        self.assertNotIn("\n", t["why"])

    def test_a_summary_of_no_samples_counts_as_not_measured(self):
        self.assertFalse(self.run_with({"telemetry": summary(samples=0)})["telemetry"]["measured"])

    def test_a_record_from_before_this_change_breaks_nothing(self):
        v = self.run_with()
        self.assertEqual(v["leftover"], [])
        self.assertTrue(status.machine_block(v))
        self.assertIn("ended before", status.machine_line(v))
        self.assertIn("machine:", status.scorecard(self.ledger, RUN))
        self.assertIn(RUN, status.render(status.collect(self.ledger)))

    def test_a_queued_run_with_no_machine_breaks_nothing(self):
        self.ledger.save_run(RUN, {"run_id": RUN, "spec_id": "001-thing", "state": "queued"})
        v = status.run_view(self.ledger, RUN, None)
        self.assertFalse(v["telemetry"]["measured"])
        status.machine_block(v), status.machine_line(v), status.render(status.collect(self.ledger))


class Warnings(Case):
    STILL = "/work/repo still has 2 uncommitted file(s) and 3 unpushed commit(s)"

    def test_a_removed_run_shows_the_runner_warning_as_what_was_destroyed(self):
        w = self.run_with(self.removed(), {"warnings": [self.STILL, "something else"]})["warnings"]
        self.assertEqual(w[0], "/work/repo: 2 uncommitted file(s) and 3 unpushed commit(s) were deleted with the machine")
        self.assertNotIn("still has", " ".join(w))
        self.assertEqual(w[1], "something else")

    def test_a_run_whose_machine_is_there_keeps_the_runner_warning_as_written(self):
        self.assertEqual(self.run_with({}, {"warnings": [self.STILL]})["warnings"], [self.STILL])

    def test_a_failing_deletion_is_a_warning_in_the_view_and_in_org_status(self):
        err = {"error": "incus: storage busy", "at": time.time(), "tries": 3}
        v = self.run_with({"machine_remove_error": err})
        self.assertEqual(len(v["warnings"]), 1)
        self.assertIn("its machine could not be deleted (3 tries, last ", v["warnings"][0])
        self.assertIn("storage busy", v["warnings"][0])
        text = status.render(status.collect(self.ledger), width=200)
        self.assertIn("! %s: its machine could not be deleted" % RUN, text)

    def test_the_failing_deletion_warning_is_gone_once_the_machine_is_removed(self):
        err = {"error": "incus: storage busy", "at": time.time(), "tries": 3}
        v = self.run_with(self.removed(machine_remove_error=err))
        self.assertEqual(v["warnings"], [])
        self.assertNotIn("could not be deleted", status.render(status.collect(self.ledger), width=200))


class Block(Case):
    def block(self, **kw):
        return "\n".join(status.machine_block(self.run_with(self.removed(telemetry=summary(**kw)))))

    def test_the_block_for_a_run_that_peaked_at_ten_gibibytes_and_killed_nothing(self):
        b = self.block(memory_peak_bytes=10504634368, memory_limit_bytes=None, cpu_s=19504.0, oom_kill=0, samples=5)
        self.assertIn("peak 10018 MiB of a not read limit", b)
        self.assertIn("19504 s", b)
        self.assertIn("0 processes killed for memory", b)
        self.assertIn("restarts  0", b)

    def test_the_block_for_a_run_with_nineteen_kills_and_a_limit_of_4439_mib(self):
        b = status.machine_block(self.run_with(self.removed()))
        self.assertTrue(b[0].startswith("machine: incus org-eng-009-abcdef · deleted "))
        self.assertTrue(b[0].endswith("when the run ended"))
        text = "\n".join(b)
        self.assertIn("peak 3898 MiB of a 4439 MiB limit", text)
        self.assertIn("19 processes killed for memory", text)
        self.assertIn("908 s, 0.32 cores on average over 48m", text)
        self.assertIn("disk      1.2 GiB at the last measurement", text)
        self.assertIn("measured  37 times, ", text)

    def test_a_null_value_prints_not_read_and_never_zero(self):
        b = self.block(memory_peak_bytes=None, memory_limit_bytes=None, cpu_s=None, oom_kill=None, restarts=None, disk_bytes=None)
        for word in ("peak not read of a not read limit", "cpu       not read", "kills     not read", "restarts  not read",
                     "not reported by the storage pool"):
            self.assertIn(word, b)

    def test_the_average_is_left_out_when_the_elapsed_time_is_missing_or_zero(self):
        for elapsed in (None, 0):
            v = self.run_with(self.removed(), {"elapsed_s": elapsed})
            self.assertNotIn("cores", "\n".join(status.machine_block(v)))

    def test_a_note_from_the_last_measurement_is_one_more_line(self):
        self.assertIn("note      memory.events was missing", self.block(note="memory.events was missing"))

    def test_a_machine_that_is_still_there_says_so(self):
        v = self.run_with({"telemetry": summary()})
        self.assertIn("still there", status.machine_block(v)[0])

    def test_a_machine_removed_by_a_person_says_org_machine_rm(self):
        v = self.run_with(self.removed(machine_removed_by="machine rm"))
        self.assertIn("with `org machine rm`", status.machine_block(v)[0])

    def test_without_measurements_the_block_is_the_first_line_and_the_reason(self):
        b = status.machine_block(self.run_with(kind="docker"))
        self.assertEqual(len(b), 2)
        self.assertIn("docker machines are not measured", b[1])


class Line(Case):
    def test_the_line_for_a_removed_measured_run(self):
        line = status.machine_line(self.run_with(self.removed()))
        self.assertTrue(line.startswith("peak 3898 MiB of 4439 MiB · cpu 908 s · 19 killed for memory · 0 restarts · deleted "))

    def test_the_line_for_a_run_still_there_has_no_deletion(self):
        self.assertNotIn("deleted", status.machine_line(self.run_with({"telemetry": summary()})))

    def test_the_line_with_nulls_says_not_read(self):
        line = status.machine_line(self.run_with({"telemetry": summary(memory_peak_bytes=None, cpu_s=None)}))
        self.assertIn("peak not read of 4439 MiB · cpu not read", line)

    def test_the_line_for_runs_not_measured_is_the_reason(self):
        self.assertEqual(status.machine_line(self.run_with(kind="ssh")), "ssh machines are not measured")
        self.assertIn("ended before org measured", status.machine_line(self.run_with()))


class Leftover(Case):
    def test_the_leftover_list_is_cut_down_to_what_can_be_trusted(self):
        good = {"path": "/w/a", "branch": "feat", "branches": ["feat", 7, "x" * 500], "commits": ["c%d" % i for i in range(30)],
                "commits_total": 30, "files": ["f%d" % i for i in range(80)], "files_total": 80, "/etc/passwd": "x"}
        raw = [good, "text", 4, None, ["a"]] + [{"path": "/w/%d" % i} for i in range(40)]
        left = self.run_with({}, {"leftover": raw})["leftover"]
        self.assertEqual(len(left), 20)
        first = left[0]
        self.assertEqual(set(first), {"path", "branch", "branches", "commits", "commits_total", "files", "files_total"})
        self.assertEqual(first["branches"], ["feat", "x" * 200])
        self.assertEqual(len(first["commits"]), 20)
        self.assertEqual(len(first["files"]), 50)
        self.assertEqual((first["commits_total"], first["files_total"]), (30, 80))

    def test_wrong_types_are_dropped_and_long_strings_clipped(self):
        left = self.run_with({}, {"leftover": [{"path": "p" * 999, "branch": 5, "branches": "main", "commits": [1, 2],
                                                "files": {"a": 1}, "commits_total": "9", "files_total": True}]})["leftover"]
        self.assertEqual(left[0]["path"], "p" * 200)
        self.assertEqual((left[0]["branch"], left[0]["branches"], left[0]["commits"], left[0]["files"]), ("", [], [], []))
        self.assertEqual((left[0]["commits_total"], left[0]["files_total"]), (0, 0))

    def test_leftover_that_is_not_a_list_is_nothing(self):
        self.assertEqual(self.run_with({}, {"leftover": {"path": "x"}})["leftover"], [])

    def test_the_scorecard_says_what_was_left_on_a_machine_that_is_gone(self):
        left = [{"path": "/w/a", "branch": "feat", "branches": ["feat", "wip"], "commits": ["Fix the thing"], "commits_total": 3,
                 "files": ["src/a.py"], "files_total": 1}]
        self.run_with(self.removed(), {"leftover": left}, actuals={})
        text = status.scorecard(self.ledger, RUN)
        self.assertIn("deleted with the machine", text)
        self.assertIn("/w/a  branch feat, wip", text)
        self.assertIn("commit  Fix the thing", text)
        self.assertIn("and 2 more commit(s)", text)
        self.assertIn("file    src/a.py", text)


class Scorecard(Case):
    ACTUALS = {"after_planning": {}, "planning": {}, "total": {"elapsed_minutes": 48, "cost_usd": 1.0}, "milestones": []}

    def test_the_scorecard_ends_with_the_block_after_a_blank_line(self):
        self.run_with(self.removed(), actuals=self.ACTUALS)
        text = status.scorecard(self.ledger, RUN)
        self.assertIn("\n\nmachine: incus", text)
        self.assertIn("peak 3898 MiB of a 4439 MiB limit", text)

    def test_the_scorecard_of_a_run_without_actuals_keeps_its_sentence_and_has_the_block(self):
        self.run_with(self.removed())
        text = status.scorecard(self.ledger, RUN)
        self.assertIn("has not finished (no actuals.json", text)
        self.assertIn("\n\nmachine: incus", text)
        self.assertIn("19 processes killed for memory", text)


if __name__ == "__main__":
    unittest.main()
