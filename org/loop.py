"""The manager's loop: org wakes a headless session of the manager when there is something to decide.

`tick` is called by `dispatch.pump` after `graph.tick`, every ten seconds while the dashboard's
service runs (which `org up` starts), and by `org status` and `org sync`. With `loop.enabled`
false, the default, it does nothing at all. When it is on, it decides whether a turn is due (R8 of
spec 016), and if so starts one: `org loop-run <n>`, a process of its own started through a
launcher as a review is, that runs one headless session of the manager under the settings of a
direct chat that asks nothing (R1), with the turn's extra deny rules (R2), and writes one line
to `loop/turns.jsonl` when it ends.

Files, all under `<ledger>/loop/`, all written by org's code only (a session is told so and
given no rule for it: a turn runs as a direct chat runs, R1, and could as well change the
configuration; what bounds the loop is `day_max_usd` and the brake, which are counted here):

  state.json     owned by `tick`, `pause` and `resume`, under `.lock`. The turn process never
                 writes it. Shape (every key optional; a missing file is the state of a loop that
                 has never run):
                   {"turns": int,                 how many turns have been started, ever
                    "running": null | {"turn": n, "pid": int, "boot": str, "started_at": wall,
                                       "why": reason, "runs": [run ids concerned],
                                       "detail": str, "log": file name},
                    "reaped": int,                the newest turn whose line `tick` has taken in
                    "failures": int,              failed turns in a row (an `ok` sets it to 0)
                    "last_failure": null | str,   why the last failed turn failed, for the next
                    "paused": null | {"reason": str, "at": wall, "by": "loop" | "hand"},
                    "seen": {run id: state},      runs whose end (or hold) a turn has been told of
                    "pending": {reason: {"since": wall, "runs": [...]}},  first of two ticks
                    "brake_told": null | resets_at of the reading the brake message was sent for,
                    "day_told": null | "YYYY-MM-DD", the local day `day_max_usd` was reached}
  turns.jsonl    one line per turn, appended by the turn process when it ends, or by `tick` for
                 a turn whose process ended without one. See `turn_line`.
  turn-<n>.log   the turn's raw stream and stderr, beside it.
  turn-<n>.md    the message the turn was given, written by the turn process before it starts
                 the session, for whoever reads the log later.
  pumped.json    {"at": wall}: stamped by `tick` on every call while the loop is on (R22).

Every time here is a wall-clock time, as a person reads it, except the limits of a running turn,
which the turn process measures with `time.monotonic()` (R24). A wall time that jumped while the
machine slept only makes something due sooner: it never stops a limit from being enforced.

Nothing here may raise out of `tick`: the loop's trouble never keeps a queued run from starting.
"""
from __future__ import annotations

from typing import Callable, List, Optional

from .ledger import Ledger

# The first words of every turn's message, as `review.OPENING` is for a review: tests/fake_claude.py
# knows a turn by them.
OPENING = "A turn of the manager's loop"

# Why a turn is started (R8), in the order `due` tries them.
REASONS = ("bootstrap", "retry", "trouble", "settled", "wake")

# The outcome of a turn, in its line.
OUTCOMES = ("ok", "failed", "stopped")

# What a turn may never run, beside what a direct chat may never run (R2). Each command is denied
# bare and with arguments, in each form a session could type it (`org`, `python3 -m org`, the
# path of bin/org): see `manager.turn_settings`.
TURN_DENY_SHELL = ["ssh", "scp"]
TURN_DENY_ORG = ["login", "down", "node add", "node rm"]

# The message R9 sends when the loop pauses itself.
PAUSED_TITLE = "org: the manager's loop paused"

Clock = Callable[[], float]


# ---------------------------------------------------------------- settings and paths


def setting(ledger: Ledger, key: str):
    """`loop.<key>` from the configuration, falling back to `DEFAULT_CONFIG["loop"][key]` when it is
    missing or not of the default's type (a number for a number; a bool for `enabled`)."""
    from .ledger import DEFAULT_CONFIG

    default = DEFAULT_CONFIG["loop"][key]
    value = (ledger.config().get("loop") or {}).get(key, default)
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return value


def enabled(ledger: Ledger) -> bool:
    """`loop.enabled` is exactly true."""
    return (ledger.config().get("loop") or {}).get("enabled") is True


def loop_dir(ledger: Ledger):
    return ledger.root / "loop"


def state_path(ledger: Ledger):
    return loop_dir(ledger) / "state.json"


def turns_path(ledger: Ledger):
    return loop_dir(ledger) / "turns.jsonl"


def log_path(ledger: Ledger, turn: int):
    return loop_dir(ledger) / ("turn-%04d.log" % turn)


def pumped_path(ledger: Ledger):
    return loop_dir(ledger) / "pumped.json"


def pumped_at(ledger: Ledger) -> Optional[float]:
    """When `tick` last ran with the loop on, or None (R22: `org doctor` reads this)."""
    raise NotImplementedError


def turns(ledger: Ledger) -> List[dict]:
    """Every line of turns.jsonl, oldest first; a line that is not a JSON object is skipped."""
    raise NotImplementedError


# ---------------------------------------------------------------- the tick


def tick(ledger: Ledger, log=lambda message: None, clock: Optional[Clock] = None) -> Optional[str]:
    """Everything a pump does for the loop. Returns the reason of the turn it started, or None.

    `clock` returns the wall time (default `time.time`); a test or the demo passes its own, and the
    local day of R12 is the local day of that clock. Never raises: a step that fails is logged.

    With `loop.enabled` not true: returns at once, reads nothing else, writes nothing, sends
    nothing. Otherwise, under a lock on `loop/.lock` that does not wait (another process holding
    it is doing this tick's work), in this order:

      1. stamp `pumped.json`.
      2. `reap`: take in every turn line newer than `state.reaped`, and a turn whose process is gone
         without a line (it gets a `failed` line, reason "the turn's process ended without a
         line", written here). Each line taken in, once: an `ok` sets `failures` to 0; a
         `failed` adds one and keeps its reason as `last_failure`; `stopped` changes neither.
         The answer's `tell` is handed to `tell` once. At two failures in a row the loop pauses
         itself (`paused.by` "loop", the reason of the second failure) and sends one message:
         title `PAUSED_TITLE`, body "<reason through notify.clean>. org talk".
      3. `notify.flush`, so what step 2 queued leaves now (graph.tick flushes only with a plan).
      4. Nothing more when paused, when `manager.turn_problems` lists anything, or while a turn
         runs.
      5. `due`: the reason a turn is due, or None. The first ticks of `settled` and `trouble` are
         recorded in `state.pending` here, whether or not a turn can start.
      6. R12, only when a turn is due: at or above the brake (`graph.over_budget`), start none and,
         the first time for this reading's `resets_at`, send one message that says so and when
         the limit resets; when the turns started on the clock's local day have cost
         `loop.day_max_usd` or more, start none (`day_told` is that day).
      7. `start` the turn.
    """
    if not enabled(ledger):
        return None
    raise NotImplementedError


def due(ledger: Ledger, state: dict, now: float) -> Optional[dict]:
    """Whether a turn is due now, as {"why": reason, "runs": [run ids], "detail": text} or None.
    It reads the ledger and may change `state["pending"]` only; it writes no file.

    The gap first: nothing is due until `loop.gap_min` minutes have passed since the last turn
    ended. Then the first that holds, in this order:

      bootstrap  no turn has ever been started (`state.turns` is 0). At bootstrap every run that
                 has already ended is taken as seen: history wakes nothing.
      retry      the last turn failed and `failures` is 1.
      trouble    a run not in `seen` has ended `failed` or `stopped`, or is `dispatch_failed`, or
                 is queued and `held` or `paused`; found on two ticks at least `loop.settle_s`
                 apart (`pending["trouble"].since`); the condition gone on a tick clears it.
      settled    a run not in `seen` has reached a final state, no run is live, and no queued run
                 could start (`graph.blocked` is None and nothing drains); on two ticks as above.
                 A run that ended `done` or `partial` while another is live wakes nothing.
      wake       the last turn's answer named `wake_in_min` and its end plus that many minutes has
                 passed; or it named none, no run is live or queued, and `loop.idle_wake_h` hours
                 have passed since it ended.

    A time that passed while the machine slept counts at the next tick: every test here compares
    `now` with a stored wall time, never counts ticks.
    """
    raise NotImplementedError


def start(ledger: Ledger, state: dict, why: dict, now: float, clock_offset: float = 0.0) -> dict:
    """Start turn `state.turns + 1` for `why` (from `due`): record it in `state.running` and mark
    every run in `why.runs` as seen, write the state, then start `org loop-run <n> --offset
    <clock_offset>` for this ledger through a launcher that exits at once, as `review.start` does,
    so that the turn is init's child and nobody's zombie. Returns `state.running`.

    The state is written before the process starts: a tick that ends between the two leaves a
    turn marked running whose process does not exist, which the next `reap` ends as `failed`, and
    never two turns at once."""
    raise NotImplementedError


def reap(ledger: Ledger, state: dict, now: float) -> None:
    """Step 2 of `tick`; changes `state` and sends what it says, writes nothing else but a line for
    a turn whose process ended without one. A process is gone when `dispatch._gone` says so for
    {"pid", "boot"} of `state.running`."""
    raise NotImplementedError


def tell(ledger: Ledger, turn: int, line: str, now: float) -> bool:
    """Send a turn's `tell` (R10): title "org", the body through `notify.clean`, into notify's outbox
    when `notify.url` is set, and a line of kind `tell` in alerts.jsonl (run "loop:turn-<n>"), at
    most `loop.tells_per_day` sent on the local day of `now`. A line over the limit is recorded in
    alerts.jsonl as kind `tell` with `"sent": false` and not sent. Returns whether it was sent."""
    raise NotImplementedError


def day_cost(ledger: Ledger, now: float) -> float:
    """What the turns that started on the local day of `now` have cost, from turns.jsonl."""
    raise NotImplementedError


# ---------------------------------------------------------------- the turn process


def message(ledger: Ledger, running: dict, now: float) -> str:
    """What a turn is given (R11), built in code, beginning with `OPENING`:

      - the date, the weekday and the time of `now`;
      - why it was woken, and for each run in `running.runs`: its spec, its outcome or what holds
        it, its forecast (`expect_usd`, `expect_hours` of the spec's header), what it took
        (cost, hours), and the two commands `org report <run>` and `org review <run>`;
      - `org status` as text (`status.render(status.collect(ledger, do_sync=False))`);
      - the budget: every weekly window from `budget.windows` with its share used, how old the
        reading is and when it resets; dollars per point (`budget.usd_per_pct`); the points and
        dollars left below the brake; what running and queued runs may still spend (each one's
        cap less what it has spent); and what the manager's sessions cost today and since the
        reset, by kind (`manager.sessions_cost`), beside `loop.day_max_usd`, with the chats'
        estimate (`manager.chat_cost_estimate`) labelled an estimate;
      - the last three turns (when, why, cost, outcome, `did`) and when the last asked to be woken;
      - the paths of `<desk>/roadmap.md`, the briefs (`ledger.briefs`, `<desk>/projects/`) and the journal
        (`<desk>/journal/`);
      - after a failed turn, why it failed (`state.last_failure`).

    Everything an engineer or a model wrote (reports, reviews, `did`, `tell`, a failure's reason
    that quotes a session) stands in a fence (`review._fenced`) and is called data."""
    raise NotImplementedError


def command(ledger: Ledger, message_text: str) -> List[str]:
    """The argument list of a turn's session: `claude -p --output-format stream-json --verbose
    --append-system-prompt <persona with the direct, asks-nothing note, then org/prompts/turn.md>
    --settings <manager.turn_settings> --permission-mode bypassPermissions --json-schema
    <schemas.TURN> [--model manager.turn_model or manager.model] [--effort manager.turn_effort or
    manager.effort] --session-id <new uuid>`. The message goes on stdin."""
    raise NotImplementedError


def check_answer(answer) -> List[str]:
    """What is wrong with a turn's structured answer, or [] (R7): `did` a non-empty list of
    non-empty strings; `wake_in_min` null or a number from 15 to 1440; `tell` null or one line
    (no line break) that is not empty."""
    raise NotImplementedError


def run_turn(ledger: Ledger, turn: int, clock_offset: float = 0.0) -> int:
    """The turn process, `org loop-run <n>`: one session for turn `n`, then its line.

    Reads `state.running` (which must be turn `n`, or it writes a `failed` line and returns 1),
    writes `turn-<n>.md`, starts `command` with the desk as its working directory, the
    environment `manager.turn_env`, in a process group of its own, and reads its stream line by
    line into `turn-<n>.log` and a `stream.Meter`. The first `rate_limit_event` is recorded with
    `budget.record_sample` as an engineer's first reading is (run id "loop:turn-<n>"). It kills
    the group (SIGKILL) when the meter's cost reaches `loop.turn_max_usd` or when
    `loop.turn_timeout_min` have passed on `time.monotonic()`; a SIGTERM to this process kills the
    group too and the outcome is `stopped`.

    Then appends `turn_line` to turns.jsonl, and `manager.record_session(ledger, "turn", ...)`.
    Every wall time it records is `time.time() + clock_offset`, so that a test's clock and the
    turn's agree. Returns 0 when it wrote a line."""
    raise NotImplementedError


def turn_line(turn: int, started_at: float, ended_at: float, why: str, runs: List[str], cost_usd: float,
              outcome: str, reason: Optional[str], answer: Optional[dict], model: Optional[str]) -> dict:
    """One line of turns.jsonl: {"turn", "started_at", "ended_at", "why", "runs", "cost_usd",
    "outcome" (one of OUTCOMES), "reason" (why it failed or stopped, else null), "answer" (the
    valid answer or null), "model", "log": "turn-<n>.log"}."""
    return {"turn": turn, "started_at": started_at, "ended_at": ended_at, "why": why, "runs": list(runs),
            "cost_usd": round(float(cost_usd or 0.0), 6), "outcome": outcome, "reason": reason, "answer": answer,
            "model": model, "log": "turn-%04d.log" % turn}


# ---------------------------------------------------------------- the person's commands and the views


def pause(ledger: Ledger, reason: str = "", by: str = "hand") -> dict:
    """`org loop pause [reason]`: set `state.paused` (a running turn is left to end). Takes the lock
    blocking, up to ten seconds. Returns the state."""
    raise NotImplementedError


def resume(ledger: Ledger) -> dict:
    """`org loop resume`: lift any pause and set `failures` to 0, so the next due tick may start a
    turn. Returns the state."""
    raise NotImplementedError


def status(ledger: Ledger, clock: Optional[Clock] = None) -> dict:
    """What `org loop status` prints and `org status --json` carries as `loop`:
      {"enabled": bool, "problems": [what of R1 is missing], "paused": null | {...},
       "running": null | {"turn", "started_at", "why"}, "last": [the last three turn lines],
       "waits_for": text, "next_wake_at": wall | null, "day_cost_usd": float,
       "day_max_usd": float, "day_reached": bool, "brake": null | reason,
       "line": the one line `org status` shows under the pool's}.
    `line` is "loop: off"; "loop: on, waits for <...>[, next wake HH:MM]"; "loop: a turn running
    since HH:MM (<why>)"; or "loop: paused: <reason>", each followed by "; turns today $x of $y"
    when the loop is on. Reads only; never raises (an unreadable state is shown as such)."""
    raise NotImplementedError


def summary(ledger: Ledger, clock: Optional[Clock] = None) -> dict:
    """`status` for `org status`: the same dict. Kept apart so that status.py has one name to call."""
    return status(ledger, clock)


def render_status(st: dict) -> str:
    """The text of `org loop status`: on or off, paused and why, what of R1 is missing, the last
    three turns (when, why, cost, outcome, the first sentence of `did`), what it waits for now
    with the time of the next wake, the day's cost against `loop.day_max_usd` and the brake."""
    raise NotImplementedError
