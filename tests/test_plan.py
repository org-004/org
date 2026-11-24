"""Plans end to end: simulated engineers, the real review process, the real alerts, the real CLI.

tests/test_graph.py writes records by hand and stands in for the review and for sending; here
nothing is stood in for but Incus (tests/fake_incus.py), the model (tests/fake_claude.py, which also
answers the review sessions) and the server behind `notify.url` (tests/fake_notify.py). A run takes
about two seconds from dispatch to its end, so each test runs as few as it can.

Nothing in a ledger moves by itself: `settle` does what the dashboard does every ten seconds, sync
and pump, until what a test waits for has happened.
"""
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_notify import Receiver  # noqa: E402
from org import dispatch, graph, manager, notify, status, sync  # noqa: E402
from org.util import read_json, write_json  # noqa: E402
from test_after import AfterCase  # noqa: E402
from test_manager_side import TURNS  # noqa: E402

# Generous: another process may be using the machine, and a deadline only decides how long a failure takes to show.
DEADLINE_S = 40
FAIL = {"structured": {"status": "fail", "msg": "The endpoint could not be built.",
                       "acceptance": [{"id": "AC1", "verdict": "fail", "evidence": "ran"}, {"id": "AC2", "verdict": "unverified", "evidence": "not run"}]},
        "files": {"$RUN/report.md": "# Report\n\nNothing was delivered: the endpoint could not be built.\n"}, "cost": 0.3}
DONE = TURNS["report"]
PAUSE = {"structured": {"verdict": "pause", "reason": "the endpoint is missing"}, "cost": 0.4}


def answer(verdict, reason, waiting=None):
    """A review session's scripted answer; `waiting` is the id of the spec that gets a note, for `continue`."""
    said = {"verdict": verdict, "reason": reason}
    if waiting:
        said["notes"] = [{"spec": waiting, "note": "Note for %s: build on the endpoint that A left." % waiting}]
    return {"structured": said, "cost": 0.4}


class PlanCase(AfterCase):
    def setUp(self):
        super().setUp()
        self.alone = []

    def extra_config(self, **sections):
        """Sections laid over the ledger's config (`notify`, `graph`, `manager`)."""
        cfg = read_json(self.ledger.config_path)
        cfg.update(sections)
        write_json(self.ledger.config_path, cfg)

    def plan(self, reports, reviews, length=2):
        """Draft and approve a chain of `length` specs, each after the one before it, with the engineers' reports
        and the reviews' answers scripted in the order they will be asked for. Returns the spec ids."""
        ids = [self.draft("Spec A")] + [self.draft("Spec " + "ABC"[i], str(i)) for i in range(1, length)]
        self.scenario(dict(TURNS, report=reports, review=reviews))
        # Each in turn, as `org approve` does it: the first starts at once, the others wait for the one before.
        for spec_id in ids:
            self.approve(spec_id)
            dispatch.dispatch(self.ledger, spec_id, log=lambda m: None)
        return ids

    def settle(self, what, test, seconds=DEADLINE_S):
        """Sync and pump until `test()` is true, and return what it returned."""
        end = time.time() + seconds
        while True:
            sync.sync_all(self.ledger)
            dispatch.pump(self.ledger)
            found = test()
            if found:
                return found
            if time.time() > end:
                self.fail("gave up waiting for %s: %s" % (what, self.picture()))
            time.sleep(0.25)

    def picture(self):
        return {r: {k: v for k, v in self.ledger.run(r).items() if k in ("state", "final", "held", "paused", "review")} for r in self.ledger.run_ids()}

    def runs(self, spec_id):
        return self.ledger.runs_for(spec_id)

    def rec(self, run_id):
        return self.ledger.run(run_id)

    def verdict(self, run_id):
        return (self.rec(run_id).get("review") or {}).get("verdict")

    def invocations(self):
        path = self.scen / "invocations.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def reviews(self):
        return [i for i in self.invocations() if i["label"].startswith("review")]

    def unpacked(self, run_id, *parts):
        """A file of a run's bundle as the simulated container holds it."""
        machine = self.rec(run_id)["machine"]["name"]
        return self.box(machine) / "home" / "org-runs" / run_id / Path(*parts)

    def pumped(self, n=3):
        for _ in range(n):
            sync.sync_all(self.ledger)
            dispatch.pump(self.ledger)
            time.sleep(0.2)

    def paused_after_a_failure(self, receiver=None, length=2):
        """A plan whose first run fails and whose review says pause, run until the pause is recorded and the rest is held."""
        if receiver:
            self.extra_config(notify={"url": receiver.url})
        ids = self.plan([FAIL, DONE, DONE], [PAUSE, answer("continue", "the second spec left what the third needs", "003-spec-c")], length)
        first = self.runs(ids[0])[0]
        self.settle("the pause", lambda: self.rec(first).get("paused") and all(self.rec(self.runs(s)[0]).get("held") for s in ids[1:]))
        return ids, first

    def org_ok(self, *argv):
        code, out, err = self.org(*argv)
        self.assertEqual(code, 0, err or out)
        return out


class WhatTheEngineerIsGiven(PlanCase):
    def test_a_retried_run_and_a_waiting_run_each_start_with_the_earlier_report_and_the_reviews_words(self):
        self.extra_config(manager={"review_model": "haiku", "review_effort": "low"})
        a, b = self.plan([FAIL, DONE, DONE], [answer("retry", "The build broke for no reason the spec explains."),
                                              answer("continue", "The second attempt left the endpoint.", "002-spec-b")])
        first = self.runs(a)[0]
        self.settle("B to be done", lambda: self.runs(b) and self.rec(self.runs(b)[0]).get("final") and self.rec(self.runs(b)[0]).get("state") == "done")
        retried, waiting = self.runs(a)[1], self.runs(b)[0]
        self.assertEqual((self.rec(first)["state"], self.rec(retried)["state"], self.rec(retried).get("retry_of")), ("failed", "done", first))
        self.assertEqual([self.verdict(first), self.verdict(retried)], ["retry", "continue"])

        # R5, a retry: the failed run's report and the review's reason.
        report = self.unpacked(retried, "earlier", first, "report.md").read_text()
        self.assertIn("Nothing was delivered", report)
        review_md = self.unpacked(retried, "earlier", first, "review.md").read_text()
        self.assertIn("another attempt at the same spec", review_md)
        self.assertIn("The review said retry: The build broke for no reason the spec explains.", review_md)
        # R5, a waiting run: the satisfying run's report and the note for this spec.
        self.assertIn("Delivered", self.unpacked(waiting, "earlier", retried, "report.md").read_text())
        review_md = self.unpacked(waiting, "earlier", retried, "review.md").read_text()
        self.assertIn("The review said continue: The second attempt left the endpoint.", review_md)
        self.assertIn("Note for 002-spec-b: build on the endpoint that A left.", review_md)
        self.assertFalse(self.unpacked(waiting, "earlier", first).exists(), "only the satisfying run travels")

        # R9: what the reviews cost, on the record and in `org review`; and the model the session was started with.
        self.assertEqual(self.rec(first)["review"]["cost_usd"], 0.4)
        self.assertEqual(self.rec(retried)["review"]["cost_usd"], 0.4)
        said = self.org_ok("review", retried)
        self.assertIn("review: continue", said)
        self.assertIn("cost $0.40", said)
        self.assertIn("note for 002-spec-b", said)
        self.assertIn("review: retry", self.org_ok("review", first))
        for call in self.reviews():
            self.assertEqual((call["opts"].get("--model"), call["opts"].get("--effort")), ("haiku", "low"))
            self.assertIn("--json-schema", call["opts"])
            self.assertEqual(call["opts"].get("--permission-mode"), "dontAsk", "no more than the manager's `ask` mode")
        self.assertEqual(len(self.reviews()), 2)

        # R17: `org graph` names each spec, its run, its state and the review's verdict.
        text = self.org_ok("graph")
        self.assertIn("Plan 001 → 002", text)
        self.assertIn("001-spec-a: %s done" % retried, text)
        self.assertIn("review: continue: The second attempt left the endpoint.", text)
        self.assertIn("002-spec-b (after 001): %s done" % waiting, text)


class OneReviewForOneRun(PlanCase):
    def start_pumps(self, n):
        """`org pump` n times, each held at a gate until all are started, so that they begin together."""
        gate = self.tmp / "gate"
        script = 'while [ ! -e "$1" ]; do :; done; shift; exec "$@"'
        procs = [subprocess.Popen(["sh", "-c", script, "sh", str(gate), str(ROOT / "bin" / "org"), "--home", str(self.ledger.root), "pump"],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT) for _ in range(n)]
        gate.write_text("go\n")
        return procs

    def test_four_pumps_at_the_same_moment_start_one_review(self):
        a, b = self.plan([DONE], [dict(answer("continue", "ok", "002-spec-b"), sleep=2.0)])
        first = self.runs(a)[0]
        self.wait_final(first)
        for p in self.start_pumps(4):
            out, _ = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, out)
        self.settle("the verdict", lambda: self.verdict(first))
        self.pumped(2)
        self.assertEqual(len(self.reviews()), 1, "one session for one finished run")
        self.assertEqual(self.rec(first)["review"]["attempts"], 1)
        self.settle("B to start", lambda: self.runs(b) and self.rec(self.runs(b)[0]).get("state") not in ("queued", None))
        self.assertEqual(len(self.runs(b)), 1)

    def kill_the_review(self, run_id, attempt=1):
        """Wait for the review's process to be on the record and alive, then kill its whole group."""
        def pid():
            proc = (self.rec(run_id).get("review") or {}).get("proc") or {}
            return proc.get("pid") if proc.get("pid") and not dispatch._gone(proc) else None
        pid = self.settle("a review process", pid)
        # Killed too early, the process would never have started a session, and that attempt would not count in the scenario.
        self.settle("its session to start", lambda: len(self.reviews()) >= attempt)
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        return pid

    def test_a_review_killed_before_it_answered_is_one_failed_attempt_and_a_second_attempt_answers(self):
        a, b = self.plan([DONE, DONE], [{"sleep": 30}, answer("continue", "the second attempt answered", "002-spec-b")])
        first = self.runs(a)[0]
        pid = self.kill_the_review(first)
        self.settle("the second attempt's answer", lambda: self.verdict(first))
        review = self.rec(first)["review"]
        self.assertEqual((review["verdict"], review["attempts"], review["reason"]), ("continue", 2, "the second attempt answered"))
        self.assertNotEqual(review["proc"]["pid"], pid)
        self.assertEqual(len(self.reviews()), 2)
        self.assertIn("process ended without an answer", review["last_failure"])
        self.settle("B to be done", lambda: self.runs(b) and self.rec(self.runs(b)[0]).get("state") == "done")

    def test_a_review_killed_twice_is_a_pause(self):
        a, b = self.plan([DONE], [{"sleep": 30}, {"sleep": 30}])
        first = self.runs(a)[0]
        self.kill_the_review(first)
        self.settle("the second attempt", lambda: (self.rec(first).get("review") or {}).get("attempts") == 2)
        self.kill_the_review(first, 2)
        self.settle("the pause", lambda: self.rec(first).get("paused"))
        self.assertEqual(self.rec(first)["paused"]["kind"], "review_failed")
        self.assertIn("no valid answer in 2 attempts", self.rec(first)["paused"]["reason"])
        self.assertEqual((self.verdict(first), len(self.reviews())), ("pause", 2))
        self.settle("B to be held", lambda: self.rec(self.runs(b)[0]).get("held"))
        self.pumped(3)
        self.assertEqual((self.rec(self.runs(b)[0])["state"], len(self.reviews())), ("queued", 2), "nothing is started, nothing is asked again")


class WhenTheReviewDoesNotAnswer(PlanCase):
    def paused_for(self, reviews, **graph_config):
        if graph_config:
            self.extra_config(graph=graph_config)
        a, b = self.plan([DONE], reviews)
        first = self.runs(a)[0]
        self.settle("the pause", lambda: self.rec(first).get("paused"))
        self.settle("B to be held", lambda: self.rec(self.runs(b)[0]).get("held"))
        self.pumped(3)
        self.assertEqual(self.rec(self.runs(b)[0])["state"], "queued")
        self.assertEqual(self.rec(first)["paused"]["kind"], "review_failed")
        self.assertEqual(self.verdict(first), "pause")
        return first

    def test_a_session_that_dies_is_a_pause(self):
        first = self.paused_for([{"die": True}])
        self.assertIn("review session failed", self.rec(first)["paused"]["reason"])
        self.assertLessEqual(len(self.reviews()), 2)

    def test_prose_with_no_structured_answer_twice_is_a_pause_after_two_sessions_and_no_third(self):
        first = self.paused_for([{"prose": "I think it is fine, shall I continue?"}])
        self.assertEqual(len(self.reviews()), 2)
        self.assertEqual(self.rec(first)["review"]["attempts"], 2)
        self.pumped(3)
        self.assertEqual(len(self.reviews()), 2, "a third session is never started")

    def test_an_answer_that_fails_validation_twice_is_a_pause_after_two_sessions_and_no_third(self):
        # `continue` must carry a note for each waiting spec; this one carries none.
        first = self.paused_for([answer("continue", "all well")])
        self.assertEqual(len(self.reviews()), 2)
        self.assertIn("notes: no note for waiting spec '002-spec-b'", self.rec(first)["paused"]["reason"])
        self.pumped(3)
        self.assertEqual(len(self.reviews()), 2)

    def test_a_session_longer_than_the_timeout_is_a_pause(self):
        first = self.paused_for([{"sleep": 30}], review_timeout_min=0.03)
        self.assertIn("graph.review_timeout_min", self.rec(first)["paused"]["reason"])
        self.assertEqual(len(self.reviews()), 1)


class WhenARunFailsInAChain(PlanCase):
    def test_a_pause_holds_the_whole_chain_tells_the_owner_once_and_a_release_starts_it(self):
        with Receiver() as receiver:
            (a, b, c), first = self.paused_after_a_failure(receiver, length=3)
            second, third = self.runs(b)[0], self.runs(c)[0]
            reason = self.rec(first)["paused"]["reason"]
            self.assertEqual(self.rec(first)["paused"]["kind"], "review")
            self.assertIn("the endpoint is missing", reason)
            for run_id in (second, third):
                self.assertEqual((self.rec(run_id)["state"], self.rec(run_id)["held"]["cause"], self.rec(run_id)["held"]["reason"]), ("queued", first, reason))

            # R12: the held reason and a `!` line for the cause, in the text and in the JSON.
            text = self.org_ok("status", "--no-sync")
            self.assertIn("! %s: held: %s" % (second, reason), text)
            self.assertIn("! %s: held: %s" % (third, reason), text)
            self.assertIn("! %s: paused: %s. 2 run(s) held" % (first, reason), text)
            views = {v["run_id"]: v for v in json.loads(self.org_ok("status", "--json", "--no-sync"))["runs"]}
            self.assertEqual(views[second]["held"]["reason"], reason)
            self.assertEqual(views[third]["held"]["cause"], first)
            self.assertEqual(views[first]["paused"]["reason"], reason)
            self.assertIn("paused: %s. 2 run(s) held" % reason, views[first]["warnings"])
            self.assertTrue(any(w.startswith("held: " + reason) for w in views[second]["warnings"]))

            # R14: one line in alerts.jsonl, one POST whose body is one line, sent once however often pump runs.
            self.pumped(3)
            lines = [json.loads(x) for x in (self.ledger.root / "alerts.jsonl").read_text().splitlines()]
            pauses = [x for x in lines if x["kind"] == "pause"]
            self.assertEqual([(x["run"], x["reason"]) for x in pauses], [(first, reason)])
            self.assertEqual(len(receiver.requests), 1)
            sent = receiver.requests[0]
            self.assertEqual(sent["method"], "POST")
            self.assertEqual(sent["body"], "org: %s paused: %s. 2 run(s) held. org status" % (first, notify.clean(reason, 300)))
            self.assertEqual(sent["headers"]["Title"], notify.TITLE)
            self.assertNotIn("\n", sent["body"])

            # R13: a release starts the held run, the one behind it waits for it as usual.
            code, _, err = self.org("release", first)
            self.assertEqual(code, 1, "the run that caused the pause is not queued")
            self.assertIn("is not queued", err)
            self.assertIn("released %s" % second, self.org_ok("release", second))
            self.settle("the released run to be done", lambda: self.rec(second).get("state") == "done")
            self.assertIsNone(self.rec(second).get("held"))
            self.assertEqual(self.rec(third)["state"] == "queued", True)
            code, _, err = self.org("release", second)
            self.assertEqual(code, 1)
            self.assertIn("is not queued (it is done)", err)
            self.settle("the third to be done", lambda: self.rec(third).get("state") == "done")
            self.assertEqual(self.verdict(second), "continue")
            self.assertEqual(len(receiver.requests), 1, "nothing more to tell")

    def test_a_queued_run_that_is_not_held_cannot_be_released(self):
        a, b = self.plan([DONE], [{"sleep": 30}])
        code, _, err = self.org("release", self.runs(b)[0])
        self.assertEqual(code, 1)
        self.assertIn("is not held, so there is nothing to release", err)
        self.assertEqual(self.rec(self.runs(b)[0])["state"], "queued")

    def test_a_server_that_fails_twice_is_tried_on_each_pump_until_the_third_and_then_not_again(self):
        with Receiver(statuses=[500, 500, 200]) as receiver:
            _, first = self.paused_after_a_failure(receiver)
            seen = [len(receiver.requests)]
            while len(receiver.requests) < 3 and len(seen) < 8:
                dispatch.pump(self.ledger)
                seen.append(len(receiver.requests))
            self.assertEqual(len(receiver.requests), 3, seen)
            self.assertTrue(all(b - a <= 1 for a, b in zip(seen, seen[1:])), "one try per pump: %s" % seen)
            self.pumped(4)
            self.assertEqual(len(receiver.requests), 3, "delivered, and so not sent again")
            self.assertEqual(len({r["body"] for r in receiver.requests}), 1)
            self.assertEqual(read_json(self.ledger.root / "outbox.json")["pending"], [])
            self.assertNotIn("could not be sent", self.org_ok("status", "--no-sync"))

    def test_a_server_that_always_fails_is_tried_three_times_and_then_shown_as_a_failure(self):
        with Receiver(default=500) as receiver:
            _, first = self.paused_after_a_failure(receiver)
            self.settle("three tries", lambda: len(receiver.requests) >= 3)
            self.pumped(5)
            self.assertEqual(len(receiver.requests), 3)
            text = self.org_ok("status", "--no-sync")
            self.assertIn("! %s: the alert for this pause could not be sent to notify.url after 3 tries (HTTP 500)" % first, text)
            self.assertEqual(len(read_json(self.ledger.root / "outbox.json")["failed"]), 1)
            # It stood in nobody's way: the pause itself is as it was.
            self.assertTrue(self.rec(first)["paused"])


class WhatTheOwnerCanDo(PlanCase):
    def test_releasing_is_a_command_that_asks_the_person_in_every_manager_mode(self):
        for mode in ("talk", "ask"):
            perms = manager.settings(self.ledger, mode)["permissions"]
            self.assertIn("Bash(org release *)", perms["ask"], mode)
            self.assertNotIn("Bash(org release *)", perms["allow"], mode)
            self.assertNotIn("Bash(org release)", perms["allow"], mode)

    def test_notify_test_prints_the_http_status_and_exits_zero(self):
        with Receiver(statuses=[200]) as receiver:
            self.extra_config(notify={"url": receiver.url})
            code, out, _ = self.org("notify", "test")
            self.assertEqual((code, out.strip()), (0, "sent: HTTP 200"))
            self.assertEqual(len(receiver.requests), 1)
            self.assertEqual(receiver.requests[0]["body"], notify.TEST_TEXT)
        with Receiver(statuses=[404]) as receiver:
            self.extra_config(notify={"url": receiver.url})
            code, out, _ = self.org("notify", "test")
            self.assertEqual(code, 1)
            self.assertIn("HTTP 404", out)


class WithoutAPlan(PlanCase):
    def test_a_ledger_with_no_plan_leaves_no_trace_of_one_after_a_whole_run_and_several_pumps(self):
        run_id, _ = dispatch.dispatch(self.ledger, self.approved(), log=lambda m: None)
        self.wait_final(run_id)
        self.pumped(4)
        status.collect(self.ledger, do_sync=True)
        self.org_ok("status")
        for name in ("graph.json", "alerts.jsonl", "outbox.json"):
            self.assertFalse((self.ledger.root / name).exists(), name)
        self.assertEqual(self.reviews(), [])
        self.assertFalse(graph.has_plan(self.ledger))
        self.assertFalse((self.ledger.run_dir(run_id) / "review.json").exists())
        self.assertNotIn("review", self.rec(run_id))


if __name__ == "__main__":
    unittest.main()
