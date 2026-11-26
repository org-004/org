#!/usr/bin/env python3
"""Show the manager's loop (org/loop.py) at work, in five scenes.

    python3 tests/demo_loop.py

Each scene has a ledger, a scripted `claude` (tests/fake_claude.py: the manager's turns and the
engineers alike), engineers on the `local` machine and a local server that stands in for
`notify.url`, of its own, in a temporary directory that is removed at the end. Nothing real runs
and nothing but 127.0.0.1 is contacted. The loop's own process, `org loop-run`, is real: a turn is
a process that runs the scripted session, which writes files and runs `org` commands as a session
would, with the environment of a turn (ORG_ACTOR=manager).

The script does what the dashboard's service does every ten seconds, sync and pump, and it sets the
clock: the loop is given the script's clock, and a turn is told the offset to the real one, so that
the times a turn writes agree with it. Only the loop's reading of the time is the script's;
everything else (the budget reading's staleness, the engineers) runs on the real clock.

  1. The loop is on. The first pump starts a `bootstrap` turn, which writes a draft with a forecast
     and approves it; the run ends `done`; two ticks `loop.settle_s` apart start a `settled` turn
     that is told the run with its forecast and actual, and answers `wake_in_min` 60 and a `tell`.
     One message reaches the server; a `wake` turn starts when the clock is moved 61 minutes.
  2. Three runs: one lives, one ends `done` beside it and starts no turn; then one fails and a
     `trouble` turn starts while the first still runs.
  3. Two turns in a row give no answer: the loop pauses itself and sends one message; no third turn
     however far the clock moves; `org loop resume` and the next pump starts one.
  4. The weekly reading is at 81%: no turn and one message; `org approve` as the manager is refused
     and dispatches nothing; the reading goes stale and a turn starts.
  5. The day's turns reach `loop.day_max_usd`: none starts until the clock passes midnight; and a
     wake time that passed while the clock stood still for eight hours starts a turn at the
     first tick.

One line is printed for each scene and ends in `ok`, or in `FAILED: <what was seen>`. The exit
status is 0 only when all five ended as described.
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_notify import Receiver  # noqa: E402
from org import cli, dispatch, loop, spec, sync  # noqa: E402
from org.ledger import Ledger  # noqa: E402
from org.util import write_json  # noqa: E402
from test_manager_side import GOOD, M1, PLAN, PLAN_MD, TURNS  # noqa: E402

DEADLINE_S = 40
MIN = 60.0
FAIL = {"structured": {"status": "fail", "msg": "The endpoint could not be built.",
                       "acceptance": [{"id": "AC1", "verdict": "fail", "evidence": "ran"}, {"id": "AC2", "verdict": "unverified", "evidence": "not run"}]},
        "files": {"$RUN/report.md": "# Report\n\nNothing was delivered.\n"}, "cost": 0.3}
DONE = TURNS["report"]


class Failed(Exception):
    """What was seen when a scene did not go as described."""


def answer(did="Looked at the runs; nothing needed doing.", wake=None, tell=None, **more):
    return dict({"structured": {"did": [did], "wake_in_min": wake, "tell": tell}, "cost": 0.3}, **more)


def plan_of(milestone):
    return {"structured": dict(PLAN, milestones=[dict(M1, id=milestone)]), "files": {"$RUN/plan.md": PLAN_MD}, "cost": 0.5}


def milestone_of(**more):
    return dict(TURNS["milestone:M1"], **more)


def expect(what, got, want):
    if got != want:
        raise Failed("%s was %r, not %r" % (what, got, want))


class World:
    """One scene's surroundings: a ledger, a scripted claude and a stand-in for notify.url, in a temporary directory,
    with the loop on and a clock that only the script moves."""

    def __init__(self, turns, receiver, loop_cfg=None):
        self.tmp = Path(tempfile.mkdtemp(prefix="org-demo-loop-"))
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
        os.symlink(ROOT / "tests" / "fake_claude.py", bindir / "claude")
        self.scen = tmp / "scen"
        self.scen.mkdir()
        os.environ.update(HOME=str(home), PATH=str(bindir) + os.pathsep + os.environ["PATH"], ORG_SECRETS_DIR=str(secrets),
                          ORG_CGROUP_DIR=str(tmp / "no-cgroup"), ORG_MEMINFO=str(ROOT / "tests" / "meminfo"),
                          FAKE_CLAUDE_SCENARIO=str(self.scen / "scenario.json"))
        for name in ("ORG_HOME", "ORG_ACTOR"):
            os.environ.pop(name, None)
        self.receiver = receiver
        self.ledger = Ledger(tmp / "ledger")
        self.ledger.init()
        write_json(self.ledger.config_path, {
            "claude_bin": str(bindir / "claude"),
            "machines": {"default": "local", "detach": "nohup", "local": {"enabled": True}},
            "engineer": {"retry_backoff_s": 0.05, "max_turn_retries": 1},
            "manager": {"direct": True, "permission_mode": "bypassPermissions"},
            "notify": {"url": receiver.url},
            "loop": dict({"enabled": True}, **(loop_cfg or {})),
        })
        self.turns = turns
        self.scenario()
        # Ten in the morning, local time, today: far from midnight, which only scene 5 crosses.
        self.now = loop._midnight(time.time()) + 10 * 3600
        self.deadline = time.time() + DEADLINE_S

    # ------------------------------------------------------------ the script's clock and the dashboard's work
    def clock(self):
        return self.now

    def scenario(self):
        (self.scen / "scenario.json").write_text(json.dumps({"turns": self.turns}))

    def pump(self):
        """What the dashboard does every ten seconds: sync, then `dispatch.pump`, whose `loop.tick` is given the
        script's clock. `pump` calls `loop.tick(ledger, log)` without one, so it is wrapped here, once, and
        nothing else of `loop.tick` or `pump` is replaced: the same code runs as in the service."""
        real_tick = loop.tick
        with mock.patch.object(loop, "tick", lambda ledger, log=lambda m: None, clock=None: real_tick(ledger, log, clock=self.clock)):
            sync.sync_all(self.ledger)
            dispatch.pump(self.ledger)

    def tick(self, minutes=0.0):
        """Move the clock, then pump; returns what the loop's state says was started by it (the turn's reason or None)."""
        before = self.state().get("turns") or 0
        self.now += minutes * MIN
        self.pump()
        state = self.state()
        return (state.get("running") or {}).get("why") if (state.get("turns") or 0) > before else None

    def until(self, what, test):
        while True:
            self.pump()
            found = test()
            if found:
                return found
            if time.time() > self.deadline:
                raise Failed("gave up waiting for %s; %s" % (what, self.describe()))
            time.sleep(0.3)

    # ------------------------------------------------------------ what there is to see
    def state(self):
        return loop.read_state(self.ledger)

    def lines(self):
        return loop.turns(self.ledger)

    def describe(self):
        runs = "; ".join("%s %s" % (r, self.ledger.run(r).get("state")) for r in self.ledger.run_ids())
        st = self.state()
        return "turns started %s, lines %s, running %s, paused %s; runs: %s" % (
            st.get("turns"), [(l["turn"], l["outcome"]) for l in self.lines()], bool(st.get("running")), bool(st.get("paused")), runs or "none")

    def finish_turn(self, n, outcome="ok"):
        """Pump until turn `n`'s line is in turns.jsonl and the loop has taken it in; returns the line."""
        def taken():
            line = next((l for l in self.lines() if l["turn"] == n), None)
            return line if line and not self.state().get("running") else None
        line = self.until("turn %d to end" % n, taken)
        if line["outcome"] != outcome:
            raise Failed("turn %d ended %s (%s), not %s" % (n, line["outcome"], line.get("reason"), outcome))
        return line

    def prompt_of(self, label):
        path = self.scen / "invocations.jsonl"
        found = [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []
        return [i["prompt"] for i in found if i["label"] == label]

    def messages(self, settle=1.0):
        """What reached the stand-in for notify.url, after what is queued has had time to leave."""
        end = time.time() + settle
        while time.time() < end:
            self.pump()
            time.sleep(0.2)
        return [r["body"] for r in self.receiver.requests]

    def cli(self, *argv, actor=None):
        """The CLI as a process, as a person or a session runs it; returns (exit code, output)."""
        env = dict(os.environ)
        env.pop("ORG_ACTOR", None)
        if actor:
            env["ORG_ACTOR"] = actor
        done = subprocess.run([str(ROOT / "bin" / "org"), "--home", str(self.ledger.root)] + list(argv), env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        return done.returncode, done.stdout.decode("utf-8", "replace")

    def org(self, *argv):
        """The CLI in this process; returns the exit code."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = cli.main(["--home", str(self.ledger.root)] + list(argv))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
        if code:
            raise Failed("org %s exited %s: %s" % (" ".join(argv), code, out.getvalue().strip()[-200:]))

    def draft(self, title, expect_line=""):
        spec_id = spec.new_draft(self.ledger, title, "acme")
        text = GOOD.replace("CSV export", title)
        if expect_line:
            text = text.replace("max_hours: 6\n", "max_hours: 6\n" + expect_line, 1)
        self.ledger.draft_path(spec_id).write_text(text)
        return spec_id

    def bootstrap(self):
        """The first turn of a loop, which does nothing here (scripted as turn 1), started and taken in; the gap passed."""
        expect("the first pump", self.tick(), "bootstrap")
        line = self.finish_turn(1)
        self.now = max(self.now, line["ended_at"]) + (loop.setting(self.ledger, "gap_min") + 1) * MIN

    def close(self):
        """Nothing of this scene left running, its directory gone."""
        subprocess.run(["pkill", "-9", "-f", str(self.tmp)], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------- the scenes


def scene_1(receiver):
    spec_id = "001-exporter"
    turns = dict(TURNS)
    w = World(turns, receiver, {"settle_s": 600})
    try:
        path = w.ledger.draft_path(spec_id)
        text = GOOD.replace("CSV export", "Exporter").replace("max_hours: 6\n", "max_hours: 6\nexpect_usd: 12\nexpect_hours: 2\n", 1)
        # The manager's own commands: `org` is on the turn's PATH, and ORG_ACTOR=manager is in its environment.
        turns["turn:1"] = answer("Wrote the draft of the exporter, with its forecast, and approved it.", files={str(path): text},
                                 commands=[["org", "--home", str(w.ledger.root), "approve", spec_id]])
        turns["turn:2"] = answer("Read the exporter's report.", wake=60, tell="The exporter is done; I will look again in an hour.")
        turns["turn:3"] = answer("Looked again; nothing else to do.")
        w.scenario()
        expect("the first pump", w.tick(), "bootstrap")
        line = w.finish_turn(1)
        runs = w.ledger.runs_for(spec_id)
        expect("runs of the spec after the bootstrap turn", len(runs), 1)
        expect("the run's machine", (w.ledger.run(runs[0]).get("machine") or {}).get("kind"), "local")
        run_id = runs[0]
        # Only synced, not pumped, while it runs: a tick that saw the end now would be the first of the two.
        end = time.time() + DEADLINE_S
        while not w.ledger.run(run_id).get("final") and time.time() < end:
            sync.sync_all(w.ledger)
            time.sleep(0.3)
        expect("the run's state", w.ledger.run(run_id).get("state"), "done")
        # The end was first seen by the pump that took in turn 1's line; `settle_s` (here longer than the gap) must stand
        # between that tick and the one that starts the turn, however many ticks come between.
        settle = loop.setting(w.ledger, "settle_s")
        w.now = max(w.now, line["ended_at"]) + (loop.setting(w.ledger, "gap_min") + 1) * MIN
        expect("the first tick after the gap", w.tick(), None)
        expect("a tick less than settle_s after the first", w.tick(2), None)
        expect("a tick settle_s after the first", w.tick(settle / MIN), "settled")
        line = w.finish_turn(2)
        prompt = w.prompt_of("turn:2")
        expect("invocations of turn 2", len(prompt), 1)
        prompt = prompt[0]
        if run_id not in prompt or "forecast: $12, 2 hours" not in prompt:
            raise Failed("the message of the settled turn does not name %s with its forecast: %r" % (run_id, prompt[:300]))
        took = re.search(r"- what it took: (\$[0-9.]+), ([0-9.]+) hours", prompt)
        if not took:
            raise Failed("the message of the settled turn gives no actual for %s: %r" % (run_id, re.findall(r"- what it took:[^\n]*", prompt)))
        expect("turn 2's answer", (line["answer"]["wake_in_min"], bool(line["answer"]["tell"])), (60, True))
        sent = w.messages()
        expect("messages sent", len(sent), 1)
        if "The exporter is done" not in sent[0]:
            raise Failed("the message was %r" % sent[0])
        w.now = max(w.now, line["ended_at"])
        expect("a tick 59 minutes after turn 2", w.tick(59), None)
        expect("a tick 61 minutes after turn 2", w.tick(2), "wake")
        w.finish_turn(3)
        expect("messages sent in all", len(w.messages(0.5)), 1)
        return
    finally:
        w.close()


def scene_2(receiver):
    # Three runs of three specs, started one after the other; the plan each engineer is given names its milestone,
    # so what the milestone does is what that run does (M1 lives, M2 ends done, M3's report fails).
    turns = {"plan": [plan_of("M1"), plan_of("M2"), plan_of("M3")],
             "milestone:M1": milestone_of(sleep=600), "milestone:M2": milestone_of(), "milestone:M3": milestone_of(),
             "report": [DONE, FAIL], "turn:1": answer(), "turn:2": answer("Looked at the failed run; the other still runs.")}
    w = World(turns, receiver)
    try:
        w.bootstrap()
        settle = loop.setting(w.ledger, "settle_s") / MIN
        planned = lambda n: (json.loads((w.scen / "counts.json").read_text()) if (w.scen / "counts.json").exists() else {}).get("plan", 0) >= n  # noqa: E731
        specs = [w.draft("Live"), w.draft("Done"), w.draft("Failing")]
        w.org("approve", specs[0])
        w.until("the live run's engineer to plan", lambda: planned(1))
        w.org("approve", specs[1])
        run_live, run_done = w.ledger.runs_for(specs[0])[0], w.ledger.runs_for(specs[1])[0]
        w.until("the second run to be done", lambda: w.ledger.run(run_done).get("final"))
        expect("the second run", w.ledger.run(run_done).get("state"), "done")
        if w.ledger.run(run_live).get("final"):
            raise Failed("the first run ended as well: %s" % w.describe())
        # A run that ended `done` beside a live one: however often it is looked at, no turn.
        started = [w.tick(), w.tick(settle + 1), w.tick(settle + 1), w.tick(60)]
        expect("turns started beside a live run that ended done", started, [None] * 4)
        expect("turns", w.state().get("turns"), 1)
        w.org("approve", specs[2])
        run_fail = w.ledger.runs_for(specs[2])[0]
        w.until("the third run to end", lambda: w.ledger.run(run_fail).get("final"))
        expect("the third run", w.ledger.run(run_fail).get("state"), "failed")
        expect("a tick that first sees the failure", w.tick(), None)
        expect("the tick settle_s later", w.tick(settle + 1), "trouble")
        if w.ledger.run(run_live).get("final"):
            raise Failed("the first run ended before the turn was started: %s" % w.describe())
        w.finish_turn(2)
        prompt = (w.prompt_of("turn:2") or [""])[0]
        if run_fail not in prompt:
            raise Failed("the trouble turn's message does not name %s" % run_fail)
        if w.ledger.run(run_live).get("final"):
            raise Failed("the first run ended during the turn: %s" % w.describe())
        return
    finally:
        w.close()


def scene_3(receiver):
    turns = {"turn:1": {"prose": "I looked around. Shall I go on?", "cost": 0.2}, "turn:2": {"die": True},
             "turn:3": answer("Back at work after `org loop resume`.")}
    w = World(turns, receiver)
    try:
        expect("the first pump", w.tick(), "bootstrap")
        w.finish_turn(1, "failed")
        expect("failures after the first", w.state().get("failures"), 1)
        w.now = max(w.now, w.lines()[-1]["ended_at"]) + (loop.setting(w.ledger, "gap_min") + 1) * MIN
        expect("the tick after the gap", w.tick(), "retry")
        w.finish_turn(2, "failed")
        paused = w.state().get("paused")
        if not paused or paused.get("by") != "loop":
            raise Failed("the loop did not pause itself after two turns without an answer: %s" % w.describe())
        sent = w.messages()
        expect("messages sent", len(sent), 1)
        if "two turns in a row failed" not in sent[0]:
            raise Failed("the message was %r" % sent[0])
        started = [w.tick(minutes) for minutes in (10, 60, 24 * 60, 24 * 60, 7 * 24 * 60)]
        expect("turns started while paused, however far the clock moved", started, [None] * 5)
        expect("turns", w.state().get("turns"), 2)
        expect("messages sent while paused", len(w.messages(0.5)), 1)
        code, out = w.cli("loop", "resume")
        expect("exit status of org loop resume", code, 0)
        if w.state().get("paused"):
            raise Failed("org loop resume left the loop paused")
        expect("the first pump after resume", w.tick(), "retry")
        w.finish_turn(3)
        expect("messages sent in all", len(w.messages(0.5)), 1)
        return
    finally:
        w.close()


def reading(utilization, resets_in_s):
    """budget.json as `budget._record` writes it, for a reading taken now that resets `resets_in_s` seconds from the real now."""
    sample = {"at": time.time(), "utilization": utilization, "resets_at": time.time() + resets_in_s, "status": "allowed", "run_id": "demo"}
    return {"weekly": sample, "windows": {"seven_day": sample}, "samples": [sample]}


def scene_4(receiver):
    turns = {"turn:1": answer("The first turn, once the reading went stale.")}
    w = World(turns, receiver)
    try:
        # `budget.weekly` judges staleness on the real clock, so the reading's reset is in the real future.
        write_json(w.ledger.budget_path, reading(0.81, 3 * 24 * 3600))
        started = [w.tick(), w.tick(60), w.tick(24 * 60)]
        expect("turns started at 81%", started, [None] * 3)
        expect("turns", w.state().get("turns"), None)
        sent = w.messages()
        expect("messages sent", len(sent), 1)
        if "weekly limit" not in sent[0] or "no turn of the manager starts" not in sent[0]:
            raise Failed("the message was %r" % sent[0])
        spec_id = w.draft("Exporter", "expect_usd: 12\nexpect_hours: 2\n")
        code, out = w.cli("approve", spec_id, actor="manager")
        if code == 0:
            raise Failed("org approve as the manager exited 0 at 81%%: %s" % out.strip()[-200:])
        if "weekly limit" not in out:
            raise Failed("org approve as the manager was refused, but not for the limit: %s" % out.strip()[-200:])
        expect("runs after the refused approve", w.ledger.run_ids(), [])
        expect("the refused approve left the spec approved", w.ledger.spec_meta(spec_id), None)
        expect("messages sent after the refusal", len(w.messages(0.5)), 1)
        # The window resets, as far as the real clock can tell: its reading is stale and no longer holds anything back.
        write_json(w.ledger.budget_path, reading(0.81, -3600))
        expect("a tick once the reading is stale", w.tick(), "bootstrap")
        w.finish_turn(1)
        expect("messages sent in all", len(w.messages(0.5)), 1)
        return
    finally:
        w.close()


def scene_5(receiver):
    turns = {"turn:1": answer("The first turn; it cost the day's whole allowance.", wake=15, cost=1.5),
             "turn:2": answer("The first turn of the next day.", wake=15, cost=0.1),
             "turn:3": answer("The turn the wake time started, after eight hours.", cost=0.1)}
    w = World(turns, receiver, {"day_max_usd": 1.0})
    try:
        expect("the first pump", w.tick(), "bootstrap")
        line = w.finish_turn(1)
        if loop.day_cost(w.ledger, w.now) < 1.0:
            raise Failed("the turn cost %s, which does not reach loop.day_max_usd" % loop.day_cost(w.ledger, w.now))
        midnight = loop._midnight(w.now) + 24 * 3600
        w.now = max(w.now, line["ended_at"]) + 16 * MIN  # its wake time has come (15 minutes), and the gap has passed
        for hours in (0, 1, 6):
            expect("a tick on the same day at %s" % time.strftime("%H:%M", time.localtime(w.now + hours * 3600)), w.tick(hours * 60), None)
        w.now = midnight - 30 * MIN
        expect("a tick half an hour before midnight", w.tick(), None)
        expect("turns", w.state().get("turns"), 1)
        expect("day_told", w.state().get("day_told"), loop.local_day(w.now))
        expect("what `org loop status` says", loop.status(w.ledger, w.clock)["day_reached"], True)
        expect("a tick just after midnight", w.tick(31), "wake")
        line = w.finish_turn(2)
        expect("turn 2 started on", loop.local_day(line["started_at"]), loop.local_day(midnight))
        # The wake time passed while the clock stood still: no tick for eight hours, then the first one.
        w.now = line["ended_at"] + 8 * 3600
        expect("the first tick eight hours after", w.tick(), "wake")
        w.finish_turn(3)
        return
    finally:
        w.close()


# What each scene is expected to do, in the words of its line.
SCENES = ((scene_1, "scene 1: bootstrap writes and approves a draft, the run is done, a settled turn is told its forecast and actual, "
                    "one message, a wake turn 61 minutes later"),
          (scene_2, "scene 2: a run done beside a live one starts no turn; a failure beside it starts a trouble turn"),
          (scene_3, "scene 3: two turns without an answer pause the loop with one message, no third turn, org loop resume starts one"),
          (scene_4, "scene 4: weekly reading at 81%: no turn, one message, org approve as the manager refused; stale, a turn starts"),
          (scene_5, "scene 5: day_max_usd reached, no turn until midnight; a wake time passed in eight hours starts a turn at the first tick"))


def main() -> int:
    old_env = dict(os.environ)
    code = 0
    for scene, words in SCENES:
        try:
            with Receiver() as receiver:
                scene(receiver)
            line = words + ": ok"
        except Failed as e:
            line, code = "%s: FAILED: %s" % (words, e), 1
        except Exception as e:  # noqa: BLE001  a scene that breaks is a failed scene; the others still run
            line, code = "%s: FAILED: %s: %s" % (words, type(e).__name__, e), 1
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        print(line, flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
