#!/usr/bin/env python3
"""Show plans at work: three pairs of specs, B after A, each from approval to its end.

    python3 tests/demo_graph.py

Each plan has a ledger, a simulated Incus (tests/fake_incus.py) and a scripted `claude`
(tests/fake_claude.py) of its own, in a temporary directory that is removed at the end, so no
engineer, model or machine is real and nothing but 127.0.0.1 is contacted. Specs are approved
through `org approve`, as the owner does it. After that the script only does what the dashboard
does every ten seconds, sync and pump, until the plan has done what it should:

  1. A reports done, the review says continue, B starts by itself and reports done.
  2. A fails, the review says retry, the second run of A reports done, the review says continue,
     and B runs.
  3. A fails twice. The review says retry both times; the second is refused by the bound and
     becomes a pause, B is held, one message reaches the local server that stands in for
     `notify.url`, and `org release` starts B, which runs to done.

One line is printed for each plan and ends in `ok`, or in `FAILED: <what was seen>`. The exit
status is 0 only when all three ended as described.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_notify import Receiver  # noqa: E402
from org import cli, dispatch, spec, sync  # noqa: E402
from org.ledger import Ledger  # noqa: E402
from org.util import write_json  # noqa: E402
from test_manager_side import GOOD, TURNS  # noqa: E402

TEMPLATE = "org-base"
DEADLINE_S = 40
FAIL = {"structured": {"status": "fail", "msg": "The endpoint could not be built.",
                       "acceptance": [{"id": "AC1", "verdict": "fail", "evidence": "ran"}, {"id": "AC2", "verdict": "unverified", "evidence": "not run"}]},
        "files": {"$RUN/report.md": "# Report\n\nNothing was delivered.\n"}, "cost": 0.3}
DONE = TURNS["report"]


class Failed(Exception):
    """What was seen when a plan did not go as described."""


def review(verdict, reason, note_for=None):
    answer = {"verdict": verdict, "reason": reason}
    if note_for:
        answer["notes"] = [{"spec": note_for, "note": "A left its endpoint in place; build on it."}]
    return {"structured": answer, "cost": 0.4}


class World:
    """One plan's surroundings: a ledger, a simulated Incus and a scripted claude in a temporary directory."""

    def __init__(self, reports, reviews, notify_url=None):
        self.tmp = Path(tempfile.mkdtemp(prefix="org-demo-graph-"))
        tmp = self.tmp
        home = tmp / "manager-home"
        home.mkdir()
        (home / ".gitconfig").write_text("[user]\n\tname = Ada Example\n\temail = ada@example.test\n")
        secrets = tmp / "run-org"
        secrets.mkdir(mode=0o700)
        (secrets / "claude-token").write_text("sk-ant-oat01-not-a-real-token\n")
        (secrets / "github-token").write_text("github_pat_not_a_real_token\n")
        bindir = tmp / "bin"
        bindir.mkdir()
        os.symlink(ROOT / "tests" / "fake_incus.py", bindir / "incus")
        os.symlink(ROOT / "tests" / "fake_claude.py", bindir / "claude")
        (tmp / "scen").mkdir()
        (tmp / "incus" / TEMPLATE / "home").mkdir(parents=True)
        (tmp / "incus" / TEMPLATE / "state").write_text("stopped\n")
        os.environ.update(HOME=str(home), PATH=str(bindir) + os.pathsep + os.environ["PATH"], ORG_SECRETS_DIR=str(secrets),
                          ORG_CGROUP_DIR=str(tmp / "no-cgroup"), ORG_MEMINFO=str(ROOT / "tests" / "meminfo"),
                          FAKE_INCUS_ROOT=str(tmp / "incus"), FAKE_CLAUDE_SCENARIO=str(tmp / "scen" / "scenario.json"))
        self.ledger = Ledger(tmp / "ledger")
        self.ledger.init()
        config = {"machines": {"detach": "nohup", "incus": {}}, "engineer": {"retry_backoff_s": 0.05, "max_turn_retries": 1}}
        if notify_url:
            config["notify"] = {"url": notify_url}
        write_json(self.ledger.config_path, config)
        self.turns = dict(TURNS, report=reports, review=reviews)
        self.deadline = time.time() + DEADLINE_S

    def scenario(self):
        (self.tmp / "scen" / "scenario.json").write_text(json.dumps({"turns": self.turns}))

    def draft(self, title, after=None):
        spec_id = spec.new_draft(self.ledger, title, "acme")
        text = GOOD.replace("CSV export", title)
        self.ledger.draft_path(spec_id).write_text(text if after is None else text.replace("\nproject:", "\nafter: %s\nproject:" % after, 1))
        return spec_id

    def org(self, *argv):
        """The CLI in this process, as the owner runs it; returns (exit code, what it printed)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = cli.main(["--home", str(self.ledger.root)] + list(argv))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
        return code, out.getvalue()

    def approve(self, spec_id):
        code, out = self.org("approve", spec_id, "--sha", spec.draft_sha(self.ledger, spec_id)[:10])
        if code != 0:
            raise Failed("org approve %s exited %s: %s" % (spec_id, code, out.strip()[-200:]))

    def until(self, what, test):
        """What the dashboard does every ten seconds, until `test()` is true or the plan's time is up."""
        while True:
            sync.sync_all(self.ledger)
            dispatch.pump(self.ledger)
            found = test()
            if found:
                return found
            if time.time() > self.deadline:
                raise Failed("gave up waiting for %s; runs: %s" % (what, self.states()))
            time.sleep(0.3)

    def states(self):
        out = []
        for run_id in self.ledger.run_ids():
            rec = self.ledger.run(run_id)
            out.append("%s %s%s" % (run_id, rec.get("state"), (" review " + str(rec["review"].get("verdict") or "running")) if rec.get("review") else ""))
        return "; ".join(out)

    def ended(self, spec_id):
        """The run of this spec that ended well, or None while it has not: the latest run is final and its report said done."""
        runs = self.ledger.runs_for(spec_id)
        rec = self.ledger.run(runs[-1]) if runs else {}
        return runs[-1] if rec.get("final") and rec.get("state") == "done" else None

    def reviewed(self, run_id):
        return (self.ledger.run(run_id).get("review") or {}).get("verdict")

    def close(self):
        """Back to the environment as it was, nothing of this plan left running, its directory gone."""
        subprocess.run(["pkill", "-9", "-f", str(self.tmp)], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)


def expect(what, got, want):
    if got != want:
        raise Failed("%s was %r, not %r" % (what, got, want))


def plan_1():
    w = World(reports=[DONE], reviews=[review("continue", "A left what B needs.", "002-spec-b")])
    try:
        a, b = w.draft("Spec A"), w.draft("Spec B", "1")
        w.scenario()
        w.approve(a)
        w.approve(b)
        expect("B right after approval", w.ledger.run(w.ledger.runs_for(b)[0]).get("state"), "queued")
        w.until("B to be done", lambda: w.ended(b))
        run_a, run_b = w.ledger.runs_for(a), w.ledger.runs_for(b)
        expect("runs of A and of B", (len(run_a), len(run_b)), (1, 1))
        expect("A's review", w.reviewed(run_a[0]), "continue")
        expect("B's report", w.ledger.run(run_b[0]).get("state"), "done")
    finally:
        w.close()


def plan_2():
    w = World(reports=[FAIL, DONE], reviews=[review("retry", "The failure looks like a fluke; a fresh attempt may cure it."),
                                             review("continue", "The second attempt left what B needs.", "002-spec-b")])
    try:
        a, b = w.draft("Spec A"), w.draft("Spec B", "1")
        w.scenario()
        w.approve(a)
        w.approve(b)
        w.until("B to be done", lambda: w.ended(b))
        run_a = w.ledger.runs_for(a)
        expect("runs of A", len(run_a), 2)
        first, second = (w.ledger.run(r) for r in run_a)
        expect("the first run of A", (first["state"], w.reviewed(run_a[0])), ("failed", "retry"))
        expect("the second run of A", (second["state"], second.get("retry_of"), w.reviewed(run_a[1])), ("done", run_a[0], "continue"))
        expect("runs of B", len(w.ledger.runs_for(b)), 1)
    finally:
        w.close()


def plan_3():
    with Receiver() as receiver:
        w = World(reports=[FAIL, FAIL, DONE], reviews=[review("retry", "A fresh attempt may cure it."), review("retry", "Once more, then.")],
                  notify_url=receiver.url)
        try:
            a, b = w.draft("Spec A"), w.draft("Spec B", "1")
            w.scenario()
            w.approve(a)
            w.approve(b)
            run_b = w.ledger.runs_for(b)[0]
            w.until("B to be held", lambda: w.ledger.run(run_b).get("held"))
            run_a = w.ledger.runs_for(a)
            expect("runs of A", len(run_a), 2)
            expect("reviews of A", [w.reviewed(r) for r in run_a], ["retry", "pause"])
            w.until("the message to arrive", lambda: receiver.requests)
            # A few more pumps: a message told once is sent once, and a held run is not started.
            for _ in range(4):
                dispatch.pump(w.ledger)
                time.sleep(0.2)
            expect("messages sent", len(receiver.requests), 1)
            body = receiver.requests[0]["body"]
            if not body.startswith("org: %s paused: " % run_a[1]) or "\n" in body or "1 run(s) held" not in body:
                raise Failed("the message was %r" % body)
            expect("B while held", w.ledger.run(run_b).get("state"), "queued")
            code, out = w.org("release", run_b)
            expect("exit status of org release", code, 0)
            w.until("B to be done", lambda: w.ended(b))
            expect("messages sent in all", len(receiver.requests), 1)
        finally:
            w.close()


# What each plan is expected to do, in the words of its line.
PLANS = ((plan_1, "plan 1: B after A, A done, review continue, B started by itself and done"),
         (plan_2, "plan 2: B after A, A failed, review retry, second run of A done, review continue, B done"),
         (plan_3, "plan 3: B after A, A failed twice, retry refused, B held, 1 message sent, org release, B started and done"))


def main() -> int:
    old_env = dict(os.environ)
    code = 0
    for plan, words in PLANS:
        try:
            plan()
            line = words + ": ok"
        except Failed as e:
            line, code = "%s: FAILED: %s" % (words, e), 1
        except Exception as e:  # noqa: BLE001  a plan that breaks is a failed plan; the others still run
            line, code = "%s: FAILED: %s: %s" % (words, type(e).__name__, e), 1
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        print(line, flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
