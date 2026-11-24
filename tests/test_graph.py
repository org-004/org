"""Plans: runs that wait for other specs, the review between them, recovery, pauses and release.

Most cases write run records by hand, as sync would have left them, and call the step of
org/graph.py they are about; a few run real engineers in the simulated Incus where the queue
itself is the point. The review process and the sending of alerts are stood in for in one
place, `Stand-ins`, so that the real org/review.py and org/notify.py can replace them later.
"""
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from org import dispatch, graph, notify, review, status  # noqa: E402
from org.runner import TERMINAL_STATES  # noqa: E402
from org.util import read_json, write_json  # noqa: E402
from test_after import AfterCase, text_of  # noqa: E402

AUTO = object()
REPORT = "# Report\n\nThe endpoint exists and is tested.\n"
STATUS_OF = {"done": "done", "partial": "partial", "failed": "fail"}


def gone_proc():
    """A process record for a process that has ended: a child started and reaped."""
    child = subprocess.Popen(["true"])
    child.wait()
    return dict(dispatch._me(), pid=child.pid)


class StandIns:
    """What tests use in place of the review process and of sending: every call is recorded, and
    what the review process "wrote" is what the test scripted."""

    def __init__(self, case):
        self.outcomes = {}  # (run id, attempt) -> what review.outcome returns
        self.procs = []  # what review.start returns, in order; alive when empty
        self.started = []  # (run id, attempt)
        self.told = []  # (function, args)
        self.failed = {}  # run id -> notify.failed's sentence
        self.start_delay = 0.0
        for name, fake in (("start", self.start), ("outcome", self.outcome)):
            self._patch(case, review, name, fake)
        for name in ("alert", "pause", "finished", "flush"):
            self._patch(case, notify, name, self.recorder(name))
        self._patch(case, notify, "failed", lambda ledger, run_id: self.failed.get(run_id))

    @staticmethod
    def _patch(case, module, name, fake):
        patcher = mock.patch.object(module, name, fake)
        patcher.start()
        case.addCleanup(patcher.stop)

    def start(self, ledger, run_id, attempt):
        if self.start_delay:
            time.sleep(self.start_delay)
        self.started.append((run_id, attempt))
        return self.procs.pop(0) if self.procs else dispatch._me()

    def outcome(self, ledger, run_id, attempt):
        return self.outcomes.get((run_id, attempt))

    def recorder(self, name):
        def record(ledger, *args):
            self.told.append((name, args))
        return record

    def calls(self, name):
        return [args for n, args in self.told if n == name]


class GraphCase(AfterCase):
    def setUp(self):
        super().setUp()
        self.fake = StandIns(self)
        self.log = []
        self.n = 0

    # ------------------------------------------------------------ the ledger, by hand
    def spec(self, title, after=None):
        spec_id = self.draft(title, after)
        self.approve(spec_id)
        return spec_id

    def settings(self, **graph_settings):
        cfg = read_json(self.ledger.config_path, {})
        cfg["graph"] = dict(cfg.get("graph") or {}, **graph_settings)
        write_json(self.ledger.config_path, cfg)

    def made(self, spec_id, state="done", status=AUTO, report=REPORT, final=None, plan=None, **fields):
        """A run record as dispatch and sync would have left it, with its mirror's report."""
        meta = self.ledger.spec_meta(spec_id)
        run_id = self.ledger.next_run_id(spec_id)
        self.n += 1
        rec = {"run_id": run_id, "spec_id": spec_id, "spec_sha": meta["sha256"], "spec_version": meta.get("version", 1),
               "title": meta.get("title"), "machine": dispatch.choose_machine(self.ledger.config(), {}, None, run_id),
               "remote_dir": dispatch.remote_dir(run_id), "created_at": time.time() + self.n * 0.001, "state": state, "events_offset": 0}
        rec.update(graph.record_fields(self.ledger, meta, plan))
        if final if final is not None else state in TERMINAL_STATES:
            rec["final"] = True
        rec.update(fields)
        self.ledger.run_dir(run_id).mkdir(parents=True)
        self.ledger.save_run(run_id, rec)
        status = STATUS_OF.get(state) if status is AUTO else status
        mirror = self.ledger.mirror_dir(run_id)
        if state not in ("queued", "dispatch_failed"):
            write_json(mirror / "status.json", {"state": state, "cost": {"usd": 1.5}})
        if status:
            write_json(mirror / "result.json", {"status": status, "msg": "as reported"})
        if status and report:
            (mirror / "report.md").write_text(report)
        return run_id

    def reviewed(self, run_id, verdict="continue", reason="the next spec still fits", notes=None):
        rec = self.ledger.run(run_id)
        # As `apply_verdict` leaves it: a `continue` is about the text of each spec that waited then.
        read = {w: self.ledger.spec_meta(w)["sha256"] for w in graph.waiters(self.ledger, rec["spec_id"])} if verdict == "continue" else {}
        rec["review"] = {"state": "done", "attempts": 1, "verdict": verdict, "reason": reason, "notes": notes or {}, "reviewed": read,
                         "cost_usd": 0.4}
        self.ledger.save_run(run_id, rec)
        return run_id

    def update(self, run_id, **fields):
        rec = self.ledger.run(run_id)
        rec.update(fields)
        self.ledger.save_run(run_id, rec)

    def answer(self, run_id, verdict="continue", reason="the next spec still fits", notes=None, attempt=1, kind="answer", **more):
        answer = {"verdict": verdict, "reason": reason}
        if notes is not None:
            answer["notes"] = [{"spec": s, "note": n} for s, n in notes.items()]
        self.fake.outcomes[(run_id, attempt)] = dict({"attempt": attempt, "kind": kind, "answer": answer if kind == "answer" else None,
                                                      "problems": [], "error": None, "cost_usd": 0.25, "at": time.time()}, **more)

    def tick(self):
        graph.tick(self.ledger, log=self.log.append)

    def rec(self, run_id):
        return self.ledger.run(run_id)

    def weekly(self, utilization, stale=False):
        write_json(self.ledger.budget_path, {"weekly": {"at": time.time(), "utilization": utilization,
                                                       "resets_at": time.time() + (-60 if stale else 86400)}})

    def pair(self):
        """A, and B after A with a queued run: the smallest plan."""
        a = self.spec("First")
        b = self.spec("Second", "1")
        return a, b, self.made(b, "queued")


class Waiting(GraphCase):
    def test_a_run_waits_with_a_clause_that_says_where_the_spec_it_names_stands(self):
        a = self.draft("First")
        b = self.spec("Second", "1")
        waiting = self.made(b, "queued")
        self.assertEqual(self.rec(waiting)["after"], [a])

        def reason():
            return graph.blocked(self.ledger, self.rec(waiting))

        self.assertEqual(reason(), "waiting for 001 (not approved yet)")
        self.approve(a)
        self.assertEqual(reason(), "waiting for 001 (no run yet)")
        first = self.made(a, "queued")
        self.assertEqual(reason(), "waiting for 001 (queued)")
        for state, words in (("dispatch_failed", "dispatch failed"), ("executing", "executing"), ("stopped", "stopped")):
            self.update(first, state=state)
            self.assertEqual(reason(), "waiting for 001 (%s)" % words)
        self.update(first, state="done", final=True)
        write_json(self.ledger.mirror_dir(first) / "result.json", {"status": "done"})
        self.assertEqual(reason(), "waiting for 001 (done, review pending)")
        self.update(first, paused={"kind": "review", "reason": "no", "at": 1})
        self.assertEqual(reason(), "waiting for 001 (paused)")
        self.update(first, paused=None)
        self.reviewed(first)
        self.assertIsNone(reason(), "satisfied: it may start")
        self.assertIsNone(graph.waiting_for(self.ledger, self.rec(waiting)))

    def test_two_specs_are_joined_with_and_and_three_with_commas_and_a_final_and(self):
        a = self.spec("First")
        self.draft("Second")
        c = self.spec("Third", "1, 2")
        self.made(a, "executing")
        rec = self.ledger.run(self.made(c, "queued"))
        self.assertEqual(graph.waiting_for(self.ledger, rec), "waiting for 001 (executing) and 002 (not approved yet)")
        d = self.spec("Fourth", "1, 2, 3")
        rec = self.ledger.run(self.made(d, "queued"))
        self.assertEqual(graph.waiting_for(self.ledger, rec),
                         "waiting for 001 (executing), 002 (not approved yet) and 003 (queued)")

    def test_approvals_may_come_in_any_order_and_only_what_is_unsatisfied_is_named(self):
        # The last spec of the chain is approved first, while the others are drafts.
        self.draft("First")
        self.draft("Second")
        c = self.spec("Third", "1, 2")
        waiting = self.made(c, "queued")
        self.assertEqual(graph.blocked(self.ledger, self.rec(waiting)), "waiting for 001 (not approved yet) and 002 (not approved yet)")
        b = self.approve("002-second")["id"]
        self.reviewed(self.made(b))
        self.assertEqual(graph.blocked(self.ledger, self.rec(waiting)), "waiting for 001 (not approved yet)")
        a = self.approve("001-first")["id"]
        self.reviewed(self.made(a, "partial"))
        self.assertIsNone(graph.blocked(self.ledger, self.rec(waiting)))

    def test_a_held_or_released_run_is_told_so_before_anything_it_waits_for(self):
        a, b, waiting = self.pair()
        self.update(waiting, held={"cause": "x", "reason": "the review said pause", "at": 1})
        self.assertEqual(graph.blocked(self.ledger, self.rec(waiting)), "held: the review said pause")
        self.update(waiting, held=None, released_at=time.time())
        self.assertIsNone(graph.blocked(self.ledger, self.rec(waiting)), "released: whatever it waited for")


class Satisfaction(GraphCase):
    def test_done_and_partial_satisfy_once_the_review_said_continue(self):
        for state in ("done", "partial"):
            with self.subTest(state=state):
                a = self.spec("First " + state)
                run_id = self.made(a, state)
                self.assertEqual(graph.satisfied(self.ledger, a), (False, "%s, review pending" % state))
                self.reviewed(run_id)
                self.assertEqual(graph.satisfied(self.ledger, a), (True, state))

    def test_a_failed_run_never_satisfies_even_with_continue_on_its_record(self):
        a = self.spec("First")
        self.reviewed(self.made(a, "failed"))
        self.assertFalse(graph.satisfied(self.ledger, a)[0])
        b = self.spec("Second")
        self.reviewed(self.made(b, "done", status="fail"))
        self.assertFalse(graph.satisfied(self.ledger, b)[0], "the report decides, not the state")

    def test_a_stopped_run_never_satisfies(self):
        a = self.spec("First")
        self.reviewed(self.made(a, "stopped", status="done"))
        self.assertEqual(graph.satisfied(self.ledger, a), (False, "stopped"))

    def test_a_run_of_an_earlier_version_does_not_satisfy_the_next_one(self):
        a = self.spec("First")
        self.reviewed(self.made(a))
        self.assertTrue(graph.satisfied(self.ledger, a)[0])
        self.ledger.draft_path(a).write_text(text_of("First").replace("without copy and paste", "without copying"))
        self.assertEqual(self.approve(a)["version"], 2)
        self.assertEqual(graph.satisfied(self.ledger, a), (False, "no run yet"))
        self.assertIsNone(graph.current_run(self.ledger, a))

    def test_a_later_run_of_the_same_version_takes_over(self):
        a = self.spec("First")
        self.reviewed(self.made(a))
        self.made(a, "executing")
        self.assertEqual(graph.satisfied(self.ledger, a), (False, "executing"))


class Verdicts(GraphCase):
    def setUp(self):
        super().setUp()
        self.settings(max_retries=100)
        dispatch.drain(self.ledger, "the tests start nothing")  # a retry is dispatched and stays queued

    def test_each_verdict_under_each_report_status_is_honoured_only_within_its_bounds(self):
        a = self.spec("First")
        b = self.spec("Second", "1")
        expected = {
            ("continue", "done"): "continue", ("continue", "partial"): "continue",
            ("continue", "fail"): "pause", ("continue", None): "pause",
            ("retry", "done"): "pause", ("retry", "partial"): "retry", ("retry", "fail"): "retry", ("retry", None): "retry",
            ("pause", "done"): "pause", ("pause", "partial"): "pause", ("pause", "fail"): "pause", ("pause", None): "pause",
        }
        for (verdict, report), want in sorted(expected.items(), key=str):
            with self.subTest(verdict=verdict, report=report):
                state = {"done": "done", "partial": "partial", "fail": "failed", None: "failed"}[report]
                run_id = self.made(a, state, status=report)
                answer = {"verdict": verdict, "reason": "because", "notes": [{"spec": b, "note": "use the endpoint"}]}
                before = set(self.ledger.run_ids())
                self.assertEqual(graph.apply_verdict(self.ledger, run_id, answer), want)
                rec = self.rec(run_id)
                self.assertEqual((rec["review"]["verdict"], rec["review"]["reason"]), (want, "because"))
                new = set(self.ledger.run_ids()) - before
                if want == "continue":
                    self.assertEqual(rec["review"]["notes"], {b: "use the endpoint"})
                    self.assertNotIn("paused", rec)
                elif want == "retry":
                    self.assertEqual(len(new), 1)
                    again = self.rec(new.pop())
                    self.assertEqual((again["retry_of"], again["state"], again["spec_version"]), (run_id, "queued", 1))
                    self.assertEqual(self.fake.calls("alert")[-1][:2], (run_id, "retry"))
                    self.assertNotIn("paused", rec)
                else:
                    self.assertEqual(new, set(), "nothing is dispatched")
                    self.assertEqual(rec["paused"]["kind"], "review")
                    self.assertIn("the review said %s: because" % verdict, rec["paused"]["reason"])
                    if verdict != "pause":
                        self.assertIn("not followed", rec["paused"]["reason"])

    def test_a_note_for_a_spec_that_does_not_wait_is_not_kept(self):
        a, b, _ = self.pair()
        run_id = self.made(a)
        graph.apply_verdict(self.ledger, run_id, {"verdict": "continue", "reason": "fine",
                                                  "notes": [{"spec": b, "note": "n"}, {"spec": "099-other", "note": "x"}]})
        self.assertEqual(self.rec(run_id)["review"]["notes"], {b: "n"})

    def test_an_answer_that_is_no_verdict_is_a_pause(self):
        a, _, _ = self.pair()
        run_id = self.made(a)
        self.assertEqual(graph.apply_verdict(self.ledger, run_id, {"verdict": "approve", "reason": "go"}), "pause")
        self.assertEqual(self.rec(run_id)["paused"]["kind"], "review")


class Bounds(GraphCase):
    def finished(self, state="failed"):
        a, b, waiting = self.pair()
        return a, self.made(a, state), waiting

    def test_retry_is_a_pause_once_max_retries_runs_of_this_version_were_dispatched_again(self):
        a, _, _ = self.pair()
        first = self.made(a, "failed")
        second = self.made(a, "failed", retry_of=first)
        self.assertEqual(graph.apply_verdict(self.ledger, second, {"verdict": "retry", "reason": "flaky network"}), "pause")
        paused = self.rec(second)["paused"]
        self.assertEqual(paused["kind"], "retries")
        self.assertIn("graph.max_retries is 1", paused["reason"])
        self.assertEqual(graph.current_run(self.ledger, a), second, "nothing was dispatched")

    def test_retry_while_over_budget_is_a_pause_with_that_reason(self):
        a, run_id, _ = self.finished()
        self.weekly(0.9)
        self.assertEqual(graph.apply_verdict(self.ledger, run_id, {"verdict": "retry", "reason": "flaky"}), "pause")
        self.assertEqual(self.rec(run_id)["paused"]["kind"], "budget")
        self.assertIn("90% used", self.rec(run_id)["paused"]["reason"])
        self.assertEqual(graph.current_run(self.ledger, a), run_id)

    def test_a_review_is_started_once_for_a_finished_run_that_a_queued_run_waits_on(self):
        a, run_id, waiting = self.finished("done")
        self.tick()
        self.assertEqual(self.fake.started, [(run_id, 1)])
        rv = self.rec(run_id)["review"]
        self.assertEqual((rv["state"], rv["attempts"], rv["proc"]["pid"]), ("running", 1, os.getpid()))
        self.tick()
        self.assertEqual(self.fake.started, [(run_id, 1)], "a review in progress is not started again")
        self.answer(run_id, notes={"002-second": "note"})
        self.tick()
        rv = self.rec(run_id)["review"]
        self.assertEqual((rv["state"], rv["verdict"], rv["cost_usd"]), ("done", "continue", 0.25))
        self.assertEqual(rv["notes"], {"002-second": "note"})
        self.assertIsNone(graph.blocked(self.ledger, self.rec(waiting)))

    def test_an_invalid_answer_twice_is_a_pause(self):
        _, run_id, _ = self.finished()
        self.tick()
        self.answer(run_id, kind="invalid", problems=["reason is longer than 300 characters"])
        self.tick()
        self.assertEqual(self.fake.started, [(run_id, 1), (run_id, 2)])
        self.assertNotIn("paused", self.rec(run_id))
        self.answer(run_id, kind="invalid", attempt=2, problems=["notes: missing 002-second"])
        self.tick()
        rec = self.rec(run_id)
        self.assertEqual((rec["paused"]["kind"], rec["review"]["verdict"], rec["review"]["state"]), ("review_failed", "pause", "done"))
        self.assertIn("notes: missing 002-second", rec["paused"]["reason"])
        self.assertEqual(rec["review"]["cost_usd"], 0.5, "both attempts are counted")
        self.tick()
        self.assertEqual(len(self.fake.started), 2, "and no third")

    def test_a_session_that_errors_or_times_out_is_a_pause_at_once(self):
        for kind in ("error", "timeout"):
            with self.subTest(kind=kind):
                self.fake.started.clear()
                a = self.spec("First " + kind)
                b = self.spec("Second " + kind, a)
                self.made(b, "queued")
                run_id = self.made(a, "failed")
                self.tick()
                self.answer(run_id, kind=kind, error="claude exited 1" if kind == "error" else None)
                self.tick()
                self.assertEqual(self.fake.started, [(run_id, 1)], "no second attempt")
                self.assertEqual(self.rec(run_id)["paused"]["kind"], "review_failed")
                self.assertIn("claude exited 1" if kind == "error" else "graph.review_timeout_min", self.rec(run_id)["paused"]["reason"])

    def test_a_review_that_has_not_answered_past_its_timeout_is_a_pause(self):
        _, run_id, _ = self.finished()
        self.fake.procs = [dict(dispatch._me(), at=time.time() - 17 * 60)]
        self.tick()
        self.tick()
        rec = self.rec(run_id)
        self.assertEqual(rec["paused"]["kind"], "review_failed")
        self.assertIn("did not answer within graph.review_timeout_min (15 min)", rec["paused"]["reason"])

    def test_a_review_whose_process_died_is_counted_and_tried_once_more(self):
        a, run_id, waiting = self.finished("done")
        self.fake.procs = [gone_proc()]
        self.tick()
        self.tick()
        self.assertEqual(self.fake.started, [(run_id, 1), (run_id, 2)])
        self.assertIn("ended without an answer", self.rec(run_id)["review"]["last_failure"])
        self.answer(run_id, attempt=2, notes={"002-second": "n"})
        self.tick()
        self.assertEqual(self.rec(run_id)["review"]["verdict"], "continue")
        self.assertIsNone(graph.blocked(self.ledger, self.rec(waiting)))

    def test_a_review_whose_process_died_twice_is_a_pause(self):
        _, run_id, _ = self.finished()
        self.fake.procs = [gone_proc(), gone_proc()]
        for _ in range(3):
            self.tick()
        rec = self.rec(run_id)
        self.assertEqual(rec["paused"]["kind"], "review_failed")
        self.assertIn("in 2 attempts", rec["paused"]["reason"])
        self.assertEqual(len(self.fake.started), 2)

    def test_a_stopped_run_that_others_wait_on_is_paused_without_a_review(self):
        _, run_id, waiting = self.finished("stopped")
        self.tick()
        self.assertEqual(self.fake.started, [])
        self.assertEqual(self.rec(run_id)["paused"]["kind"], "stopped")
        self.assertEqual(self.rec(waiting)["held"]["cause"], run_id)

    def test_a_finished_run_nothing_queued_waits_on_is_not_reviewed(self):
        a, b, waiting = self.pair()
        self.update(waiting, state="stopped", final=True)
        self.made(a, "done")
        self.tick()
        self.assertEqual(self.fake.started, [])


class OnceOnly(GraphCase):
    def test_two_pumps_at_the_same_moment_start_one_review(self):
        _, run_id, _ = Bounds.finished(self)
        self.fake.start_delay = 0.3
        threads = [threading.Thread(target=self.tick) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.fake.started, [(run_id, 1)])

    def test_a_tick_returns_at_once_while_another_holds_the_lock(self):
        _, run_id, _ = Bounds.finished(self)
        with open(self.ledger.runs / ".graph.lock", "w") as lock:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX)
            began = time.time()
            self.tick()
            self.assertLess(time.time() - began, 1.0)
            self.assertEqual(self.fake.started, [])
        self.tick()
        self.assertEqual(self.fake.started, [(run_id, 1)])

    def test_a_step_that_fails_is_logged_and_the_others_still_run(self):
        _, run_id, _ = Bounds.finished(self)
        with mock.patch.object(graph, "recover", side_effect=RuntimeError("broken")):
            self.tick()
        self.assertTrue(any("recover failed: RuntimeError: broken" in line for line in self.log), self.log)
        self.assertEqual(self.fake.started, [(run_id, 1)])


class Recovery(GraphCase):
    def running(self, spec_id, **fields):
        return self.made(spec_id, "executing", final=False, **dict({"reachable": True, "alive": True}, **fields))

    def setUp(self):
        super().setUp()
        self.resumed = []
        patcher = mock.patch.object(dispatch, "resume", lambda ledger, run_id, log=None: self.resumed.append(run_id) or "nohup")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_runner_dead_for_dead_after_min_is_resumed_and_counted(self):
        a, _, _ = self.pair()
        run_id = self.running(a, alive=False)
        self.tick()
        self.assertEqual(self.resumed, [])
        since = self.rec(run_id)["dead_since"]
        self.assertAlmostEqual(since, time.time(), delta=5)
        self.update(run_id, dead_since=time.time() - 4 * 60)
        self.tick()
        self.assertEqual(self.resumed, [], "not yet five minutes")
        self.update(run_id, dead_since=time.time() - 5 * 60 - 1)
        self.tick()
        self.assertEqual(self.resumed, [run_id])
        rec = self.rec(run_id)
        self.assertEqual((rec["resumes"], "dead_since" in rec), (1, False))
        self.assertEqual(self.fake.calls("alert")[0][:2], (run_id, "resume"))
        self.assertEqual(self.fake.calls("pause"), [], "no pause and no message for a recovery within its bound")

    def test_a_runner_that_came_back_loses_its_dead_since(self):
        a, _, _ = self.pair()
        run_id = self.running(a, dead_since=time.time() - 60)
        self.tick()
        self.assertNotIn("dead_since", self.rec(run_id))

    def test_past_max_resumes_the_run_is_paused_and_what_waits_on_it_held(self):
        a, _, waiting = self.pair()
        run_id = self.running(a, alive=False, dead_since=time.time() - 600, resumes=2)
        self.tick()
        self.assertEqual(self.resumed, [])
        rec = self.rec(run_id)
        self.assertEqual(rec["paused"]["kind"], "resumes")
        self.assertIn("graph.max_resumes is 2", rec["paused"]["reason"])
        self.assertEqual(self.rec(waiting)["held"]["cause"], run_id)
        self.assertEqual(self.fake.calls("pause"), [(run_id, rec["paused"]["reason"], 1)])

    def test_a_run_whose_machine_is_stopped_is_resumed_at_once(self):
        a, _, _ = self.pair()
        run_id = self.running(a, reachable=False)
        self.container(self.rec(run_id)["machine"]["name"])  # stopped
        self.assertTrue(graph.machine_stopped(self.ledger, self.rec(run_id)))
        self.tick()
        self.assertEqual(self.resumed, [run_id])
        self.assertIn("is stopped", self.fake.calls("alert")[0][2])

    def test_an_unreachable_run_whose_machine_runs_or_is_unknown_is_left_alone(self):
        a, _, _ = self.pair()
        run_id = self.running(a, reachable=False)
        self.assertFalse(graph.machine_stopped(self.ledger, self.rec(run_id)), "no such container: not stopped")
        self.container(self.rec(run_id)["machine"]["name"], running=True)
        self.assertFalse(graph.machine_stopped(self.ledger, self.rec(run_id)))
        self.assertFalse(graph.machine_stopped(self.ledger, {"machine": {"kind": "ssh", "name": "box"}}))
        self.tick()
        self.assertEqual(self.resumed, [])

    def test_a_stopped_machine_past_max_resumes_is_a_pause(self):
        a, _, _ = self.pair()
        self.settings(max_resumes=0)
        run_id = self.running(a, reachable=False)
        self.container(self.rec(run_id)["machine"]["name"])
        self.tick()
        self.assertEqual((self.resumed, self.rec(run_id)["paused"]["kind"]), ([], "resumes"))

    def test_a_run_that_never_got_a_machine_is_dispatched_again_once(self):
        a, _, waiting = self.pair()
        dispatch.drain(self.ledger)
        run_id = self.made(a, "dispatch_failed", error="incus copy failed")
        self.tick()
        again = graph.current_run(self.ledger, a)
        self.assertNotEqual(again, run_id)
        self.assertEqual((self.rec(again)["redispatch_of"], self.rec(again)["state"]), (run_id, "queued"))
        self.assertEqual(self.fake.calls("alert")[0][:2], (run_id, "redispatch"))
        self.update(again, state="dispatch_failed", error="incus copy failed again")
        self.tick()
        self.assertEqual(graph.current_run(self.ledger, a), again, "not a third time")
        self.assertEqual(self.rec(again)["paused"]["kind"], "dispatch")
        self.assertEqual(self.rec(waiting)["held"]["cause"], again)

    def test_a_run_of_no_plan_is_never_recovered(self):
        self.pair()
        lone = self.spec("Alone")
        dead = self.running(lone, alive=False, dead_since=time.time() - 3600)
        failed = self.made(self.spec("Alone too"), "dispatch_failed")
        self.tick()
        self.assertEqual(self.resumed, [])
        self.assertEqual(len(self.ledger.run_ids()), 3, "nothing was dispatched again")
        for run_id in (dead, failed):
            self.assertNotIn("paused", self.rec(run_id))


class Budget(GraphCase):
    def test_the_limit_counts_only_a_reading_that_is_fresh_and_at_or_above_the_threshold(self):
        self.assertIsNone(graph.over_budget(self.ledger), "no reading")
        self.weekly(0.79)
        self.assertIsNone(graph.over_budget(self.ledger))
        self.weekly(0.80)
        self.assertEqual(graph.over_budget(self.ledger), "the weekly limit is 80% used, at or above graph.pause_above_weekly_pct (80%)")
        self.weekly(0.95, stale=True)
        self.assertIsNone(graph.over_budget(self.ledger), "a reading from a window that has reset says nothing")
        self.settings(pause_above_weekly_pct=50)
        self.weekly(0.6)
        self.assertIn("(50%)", graph.over_budget(self.ledger))

    def test_nothing_starts_by_itself_at_or_above_the_threshold_and_that_is_a_pause(self):
        a, b, waiting = self.pair()
        self.reviewed(self.made(a))
        self.weekly(0.85)
        self.assertIn("85% used", graph.blocked(self.ledger, self.rec(waiting)))
        dispatch.pump(self.ledger)
        rec = self.rec(waiting)
        self.assertEqual((rec["state"], rec["paused"]["kind"], rec["held"]["cause"]), ("queued", "budget", waiting))
        self.assertTrue(rec["queue_reason"].startswith("held: it could start, and was not started because the weekly limit is 85%"))
        self.assertEqual(self.fake.calls("pause"), [(waiting, rec["paused"]["reason"], 1)])
        self.weekly(0.1)
        dispatch.pump(self.ledger)
        self.assertEqual(self.rec(waiting)["state"], "queued", "a pause stays until the person releases it")

    def test_no_review_and_no_resume_is_started_above_the_threshold(self):
        a, _, _ = self.pair()
        finished = self.made(a, "failed")
        self.weekly(0.9)
        with mock.patch.object(dispatch, "resume") as resume:
            other = self.spec("Third")
            self.spec("Fourth", other)
            self.made(self.ledger.approved_ids()[-1], "queued")
            dead = self.made(other, "executing", final=False, alive=False, reachable=True, dead_since=time.time() - 3600)
            self.tick()
            resume.assert_not_called()
        self.assertEqual(self.fake.started, [])
        self.assertEqual(self.rec(finished)["paused"]["kind"], "budget")
        self.assertEqual(self.rec(dead)["paused"]["kind"], "budget")

    def test_a_stale_or_missing_reading_lets_the_plan_go_on(self):
        a, b, waiting = self.pair()
        self.reviewed(self.made(a))
        self.weekly(0.99, stale=True)
        self.tick()
        self.assertNotIn("paused", self.rec(waiting))
        self.assertIsNone(graph.blocked(self.ledger, self.rec(waiting)))


class Chain(GraphCase):
    """C after B after A: A's pause holds both, and says so wherever runs are shown."""

    def chain(self):
        a = self.spec("First")
        b = self.spec("Second", "1")
        c = self.spec("Third", "2")
        run_b, run_c = self.made(b, "queued"), self.made(c, "queued")
        run_a = self.made(a, "failed")
        return (a, b, c), (run_a, run_b, run_c)

    def test_a_pause_holds_every_run_that_waits_on_it_directly_or_through_others(self):
        (a, b, c), (run_a, run_b, run_c) = self.chain()
        self.tick()
        self.answer(run_a, verdict="pause", reason="the schema changed under 002")
        self.tick()
        reason = "the review said pause: the schema changed under 002"
        self.assertEqual(self.rec(run_a)["paused"]["reason"], reason)
        for run_id in (run_b, run_c):
            self.assertEqual((self.rec(run_id)["held"]["cause"], self.rec(run_id)["held"]["reason"]), (run_a, reason))
        self.assertEqual(self.fake.calls("pause"), [(run_a, reason, 2)])
        self.assertIsNotNone(self.rec(run_a)["paused"]["alerted_at"])
        self.tick()
        self.assertEqual(len(self.fake.calls("pause")), 1, "told once")

        self.assertEqual(dispatch.pump(self.ledger), [], "a held run does not start")
        self.assertEqual(self.rec(run_c)["queue_reason"], "held: " + reason)
        state = status.collect(self.ledger)
        views = {v["run_id"]: v for v in state["runs"]}
        held_line = "held: %s. `org release %s` starts it anyway"
        for run_id in (run_b, run_c):
            self.assertEqual(views[run_id]["held"]["reason"], reason)
            self.assertIn(held_line % (reason, run_id), views[run_id]["warnings"])
        self.assertEqual(views[run_a]["paused"]["kind"], "review")
        self.assertIn("paused: %s. 2 run(s) held" % reason, views[run_a]["warnings"])
        self.assertNotIn("proc", views[run_a]["review"])
        json.dumps(state)  # what `org status --json` prints
        code, out, _ = self.org("status")
        self.assertEqual(code, 0)
        self.assertIn("! %s: paused: %s. 2 run(s) held" % (run_a, reason), out)
        self.assertIn("! %s: held: %s" % (run_c, reason), out)
        code, out, _ = self.org("status", "--json")
        doc = json.loads(out)
        self.assertEqual({v["run_id"]: (v.get("held") or {}).get("cause") for v in doc["runs"]}, {run_a: None, run_b: run_a, run_c: run_a})

    def test_a_send_that_failed_is_a_warning_on_the_run_that_paused(self):
        (a, b, c), (run_a, run_b, run_c) = self.chain()
        graph.pause(self.ledger, run_a, "review", "no")
        self.fake.failed[run_a] = "the alert for this pause could not be sent: HTTP 500"
        view = status.run_view(self.ledger, run_a, None)
        self.assertIn("the alert for this pause could not be sent: HTTP 500", view["warnings"])

    def test_a_pause_lifts_when_the_person_dispatches_that_spec_again(self):
        (a, b, c), (run_a, run_b, run_c) = self.chain()
        graph.pause(self.ledger, run_a, "review", "the review said pause: no")
        self.tick()
        self.assertEqual(self.rec(run_c)["held"]["cause"], run_a)
        graph.pause(self.ledger, run_a, "review", "a second reason")
        self.assertEqual(self.rec(run_a)["paused"]["reason"], "the review said pause: no", "the first reason stays")
        dispatch.drain(self.ledger)
        again, _ = dispatch.dispatch(self.ledger, a, log=lambda m: None)
        self.tick()
        for run_id in (run_b, run_c):
            self.assertNotIn("held", self.rec(run_id))
        self.assertEqual(graph.blocked(self.ledger, self.rec(run_b)), "waiting for 001 (queued)")
        self.assertEqual(graph.blocked(self.ledger, self.rec(run_c)), "waiting for 002 (queued)")


class Release(GraphCase):
    def test_release_starts_a_held_run_whatever_it_waited_for_and_what_follows_waits_for_it(self):
        (a, b, c), (run_a, run_b, run_c) = Chain.chain(self)
        graph.pause(self.ledger, run_a, "review", "the review said pause: no")
        self.tick()
        code, out, _ = self.org("release", run_b)
        self.assertEqual(code, 0)
        self.assertIn("released %s: it starts when a slot is free" % run_b, out)
        rec = self.rec(run_b)
        self.assertEqual(("held" in rec, "paused" in rec, bool(rec["released_at"])), (False, False, True))
        self.tick()
        self.assertNotIn("held", self.rec(run_b), "a released run is not held again")
        self.assertNotIn("held", self.rec(run_c), "the path through a released run does not hold")
        self.assertEqual(graph.blocked(self.ledger, self.rec(run_c)), "waiting for 002 (queued)")
        self.weekly(0.95)
        self.assertEqual(dispatch.pump(self.ledger), [run_b], "it starts, whatever it waited for and whatever the weekly limit")
        self.assertEqual(graph.blocked(self.ledger, self.rec(run_c)), "waiting for 002 (dispatched)")
        self.assertIn("did not satisfy 001-first (paused): the person released this run",
                      dict(graph.bundle_files(self.ledger, run_b))["earlier/%s/review.md" % run_a].decode())

    def test_a_run_that_is_not_held_is_refused_with_a_sentence(self):
        a, b, waiting = self.pair()
        with self.assertRaises(graph.GraphError) as refused:
            graph.release(self.ledger, waiting)
        self.assertIn("is not held, so there is nothing to release: it is queued (waiting for 001 (no run yet))", str(refused.exception))
        finished = self.made(a)
        code, _, err = self.org("release", finished)
        self.assertEqual(code, 1)
        self.assertIn("is not queued (it is done)", err)
        self.assertNotIn("Traceback", err)

    def test_org_stop_ends_a_held_run_without_it_ever_starting(self):
        (a, b, c), (run_a, run_b, run_c) = Chain.chain(self)
        graph.pause(self.ledger, run_a, "review", "no")
        self.tick()
        code, out, _ = self.org("stop", run_c)
        self.assertEqual((code, out.strip()), (0, "removed from the queue"))
        rec = self.rec(run_c)
        self.assertEqual((rec["state"], rec["final"]), ("stopped", True))
        self.assertEqual(dispatch.pump(self.ledger), [])
        self.assertFalse(self.box(rec["machine"]["name"]).exists())


class WhatTravels(GraphCase):
    def test_a_run_after_another_carries_its_report_and_the_reviews_note(self):
        a, b, waiting = self.pair()
        first = self.reviewed(self.made(a, "partial"), reason="the API is there, the button is not", notes={b: "build the button on /api/rows.csv"})
        files = dict(graph.bundle_files(self.ledger, waiting))
        self.assertEqual(sorted(files), ["earlier/%s/report.md" % first, "earlier/%s/review.md" % first])
        self.assertEqual(files["earlier/%s/report.md" % first].decode(), REPORT)
        said = files["earlier/%s/review.md" % first].decode()
        for words in ("names %s in `after`" % a, "It ended partial; its report's status is partial.",
                      "The review said continue: the API is there, the button is not", "build the button on /api/rows.csv"):
            self.assertIn(words, said)
        self.assertNotIn("did not satisfy", said)
        # And they are in the tar that travels, at those names in the run directory.
        meta = self.ledger.spec_meta(b)
        job = {"run_id": waiting, "project": None}
        with tarfile.open(fileobj=io.BytesIO(dispatch.build_bundle(self.ledger, meta, job))) as tar:
            names = tar.getnames()
        self.assertIn("earlier/%s/report.md" % first, names)
        self.assertIn("earlier/%s/review.md" % first, names)

    def test_a_retry_carries_the_failed_run_and_says_it_is_another_attempt(self):
        a, _, _ = self.pair()
        failed = self.reviewed(self.made(a, "failed"), verdict="retry", reason="the clone timed out")
        again = self.made(a, "queued", plan={"retry_of": failed})
        files = dict(graph.bundle_files(self.ledger, again))
        said = files["earlier/%s/review.md" % failed].decode()
        self.assertIn("This run is another attempt at the same spec, %s" % a, said.splitlines()[2])
        self.assertIn("The review said retry: the clone timed out", said)
        self.assertIn("earlier/%s/report.md" % failed, files)

    def test_a_run_of_no_plan_carries_nothing_more(self):
        lone = self.made(self.spec("Alone"), "queued")
        self.assertEqual(graph.bundle_files(self.ledger, lone), [])


class Telling(GraphCase):
    def test_a_plan_that_finished_with_nothing_held_is_told_once_with_what_it_spent(self):
        a, b, waiting = self.pair()
        first = self.reviewed(self.made(a))
        self.update(waiting, state="done", final=True)
        write_json(self.ledger.mirror_dir(waiting) / "status.json", {"state": "done", "cost": {"usd": 2.0}})
        self.tick()
        self.assertEqual(self.fake.calls("finished"), [(waiting, [(first, "done"), (waiting, "done")], 3.9)])
        self.tick()
        self.assertEqual(len(self.fake.calls("finished")), 1)
        self.assertIn(",".join([first, waiting]), read_json(self.ledger.root / "graph.json")["finished"])
        self.assertTrue(self.fake.calls("flush"), "what waits to be sent is sent at every tick")

    def test_a_plan_with_a_pause_is_not_told_as_finished(self):
        a, b, waiting = self.pair()
        first = self.made(a, "failed")
        self.update(waiting, state="stopped", final=True)
        graph.pause(self.ledger, first, "review", "no")
        self.tick()
        self.assertEqual(self.fake.calls("finished"), [])


class Showing(GraphCase):
    def test_org_graph_prints_each_plan_in_order_with_runs_states_reasons_and_verdicts(self):
        (a, b, c), (run_a, run_b, run_c) = Chain.chain(self)
        self.reviewed(run_a, verdict="pause", reason="the schema changed")
        graph.pause(self.ledger, run_a, "review", "the review said pause: the schema changed")
        self.tick()
        self.spec("Alone")
        code, out, _ = self.org("graph")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "Plan 001 → 002 → 003")
        self.assertIn("  001-first: %s failed, paused: the review said pause: the schema changed" % run_a, lines)
        self.assertIn("    review: pause: the schema changed", lines)
        self.assertIn("  002-second (after 001): %s queued, held: the review said pause: the schema changed" % run_b, lines)
        self.assertIn("  003-third (after 002): %s queued, held: the review said pause: the schema changed" % run_c, lines)
        self.assertNotIn("004", out, "a spec in no plan is in no plan")

    def test_org_graph_lists_the_run_a_retry_replaced_with_the_verdict_that_replaced_it(self):
        a, b, run_b = self.pair()
        first = self.reviewed(self.made(a, "failed"), verdict="retry", reason="the network dropped")
        second = self.reviewed(self.made(a, "done", plan={"retry_of": first}))
        lines = self.org("graph")[1].splitlines()
        at = lines.index("  001-first: %s done" % second)
        self.assertEqual(lines[at + 1:at + 3], ["    before it: %s failed; review: retry: the network dropped" % first,
                                                "    review: continue: the next spec still fits"])

    def test_plans_are_ordered_and_a_waiting_run_says_what_it_waits_for(self):
        a = self.spec("First")
        b = self.spec("Second")
        c = self.spec("Third", "2, 1")
        self.spec("Fourth", "3")
        self.made(c, "queued")
        self.assertEqual(graph.plans(self.ledger), [[a, b, c, "004-fourth"]])
        self.assertIn("queued, waiting for 002 (no run yet) and 001 (no run yet)", graph.render(self.ledger))

    def test_org_review_adds_the_verdict_the_reason_the_notes_and_the_cost(self):
        a, b, _ = self.pair()
        run_id = self.made(a, "done")
        self.reviewed(run_id, verdict="pause", reason="not what 002 expects", notes={})
        rec = self.rec(run_id)
        rec["review"].update(answer={"verdict": "retry", "reason": "x"}, attempts=2)
        self.ledger.save_run(run_id, rec)
        said = graph.review_text(self.ledger, run_id)
        for words in ("review: pause", "the session answered retry", "reason: not what 002 expects", "attempts 2 · cost $0.40"):
            self.assertIn(words, said)
        self.assertEqual(graph.review_text(self.ledger, self.made(a, "done")), "")


class NoPlan(GraphCase):
    def test_a_ledger_without_a_plan_is_left_untouched(self):
        lone = self.spec("Alone")
        run_id = self.made(lone, "failed")
        queued = self.made(lone, "queued")
        self.assertFalse(graph.has_plan(self.ledger))
        before = sorted(p.relative_to(self.ledger.root) for p in self.ledger.root.rglob("*"))
        self.tick()
        view = {"run_id": run_id, "warnings": []}
        self.assertIs(graph.annotate(self.ledger, self.rec(run_id), view), view)
        self.assertEqual(view, {"run_id": run_id, "warnings": []})
        self.assertIsNone(graph.blocked(self.ledger, self.rec(queued)))
        self.assertEqual(graph.bundle_files(self.ledger, queued), [])
        self.assertEqual(graph.plans(self.ledger), [])
        self.assertEqual(sorted(p.relative_to(self.ledger.root) for p in self.ledger.root.rglob("*")), before)
        self.assertEqual((self.fake.started, self.fake.told), ([], []))


class RealRuns(GraphCase):
    """Engineers in the simulated Incus: the queue starts a waiting run by itself."""

    def test_a_waiting_run_starts_by_pump_once_the_spec_it_names_is_satisfied(self):
        a = self.spec("First")
        b = self.spec("Second", "1")
        run_a, rec = dispatch.dispatch(self.ledger, a, log=lambda m: None)
        self.assertEqual(rec["state"], "dispatched")
        run_b, rec = dispatch.dispatch(self.ledger, b, log=lambda m: None)
        self.assertEqual((rec["state"], rec["after"]), ("queued", [a]))
        self.assertEqual(rec["queue_reason"], "waiting for 001 (dispatched)")
        self.wait_final(run_a)
        self.assertEqual(dispatch.pump(self.ledger), [], "the review comes first")
        self.assertEqual(self.fake.started, [(run_a, 1)])
        self.assertEqual(self.rec(run_b)["queue_reason"], "waiting for 001 (done, review pending)")
        self.answer(run_a, notes={b: "the endpoint is /api/rows.csv"})
        self.assertEqual(dispatch.pump(self.ledger), [run_b])
        self.assertEqual(self.wait_final(run_b)["state"], "done")
        home = self.box(self.rec(run_b)["machine"]["name"]) / "home" / "org-runs" / run_b / "earlier" / run_a
        self.assertEqual((home / "report.md").read_text(), (self.ledger.mirror_dir(run_a) / "report.md").read_text())
        self.assertIn("the endpoint is /api/rows.csv", (home / "review.md").read_text())
        dispatch.pump(self.ledger)
        self.assertEqual([args[:2] for args in self.fake.calls("finished")], [(run_b, [(run_a, "done"), (run_b, "done")])])

    def test_two_specs_after_the_same_one_run_side_by_side(self):
        a = self.spec("First")
        b = self.spec("Second", "1")
        c = self.spec("Third", "1")
        run_a, _ = dispatch.dispatch(self.ledger, a, log=lambda m: None)
        run_b, _ = dispatch.dispatch(self.ledger, b, log=lambda m: None)
        run_c, _ = dispatch.dispatch(self.ledger, c, log=lambda m: None)
        self.wait_final(run_a)
        dispatch.pump(self.ledger)
        self.answer(run_a, notes={b: "b", c: "c"})
        self.assertEqual(dispatch.pump(self.ledger), [run_b, run_c])
        self.assertEqual([p["used"] for p in dispatch.pool(self.ledger)], [2])
        self.assertEqual((self.wait_final(run_b)["state"], self.wait_final(run_c)["state"]), ("done", "done"))


if __name__ == "__main__":
    unittest.main()
