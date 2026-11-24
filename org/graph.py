"""Plans: specs that come after other specs, and what org does between them without the person.

A spec's header may say `after: 008, 9`. Its run is created at approval as any run is, and waits
in the queue until each named spec is *satisfied*: the latest run of its latest approved version
reported `done` or `partial`, and the review of that run (org/review.py) said `continue`. A set of
approved specs joined by `after` is a plan.

Everything here hangs off `dispatch.pump`, which already runs every few seconds: there is no
process of its own. `tick` is the one place that acts, under a lock of its own, so that whatever
number of processes pump at the same moment, each thing happens once.

What may happen without the person, and the bound on each, is decided here in code:

  - a waiting run starts when what it waits for is satisfied (never before, unless released);
  - a finished run that others wait on is reviewed once, and the review's verdict is honoured
    only within the bounds of `apply_verdict`;
  - a run of a plan whose runner died or whose machine stopped is resumed, and one that never
    got a machine is dispatched again, each a bounded number of times;
  - nothing of the above happens while the weekly limit is nearly spent.

Anything else is a pause: the run in question is marked `paused`, every queued run that waits on
it, directly or through other specs, is marked `held`, and the person is told (org/notify.py).
Only `org release`, which the person runs, starts a held run. Nothing here writes, changes or
approves a spec.

What this module keeps on a run's record (run.json):

  after          [spec id, ...]  on a run whose spec has `after`, resolved when it was dispatched
  retry_of       run id          on a run dispatched again because a review said `retry`
  redispatch_of  run id          on a run dispatched again because the first never got a machine
  released_at    time            `org release` lifted its hold: it starts when a slot is free
  held           {"cause": run id, "reason": str, "at": time}  on a queued run that a pause reaches
  paused         {"kind": str, "reason": str, "at": time, "alerted_at": time or absent}
                                 on the run that caused a pause; `kind` is one of PAUSE_KINDS
  review         {"state": "running" or "done", "attempts": n, "proc": {"pid", "boot", "at"},
                  "started_at": time, "finished_at": time, "cost_usd": dollars over every attempt,
                  "answer": the session's answer as validated, or absent,
                  "verdict": "continue", "retry" or "pause" as honoured, "reason": str,
                  "notes": {waiting spec id: note},
                  "reviewed": {waiting spec id: sha256 of the approved text the review was given}}
  resumes        n               times `tick` resumed it
  dead_since     time            when `tick` first saw its runner gone; cleared when it is alive

A record is written only under the lock that its other writers use: the placement lock
(`dispatch._placing`) for a queued run, the run's `.sync.lock` for one that sync still pulls, and
`tick`'s own lock for a run that is final, which nothing else writes.
"""
from __future__ import annotations

import fcntl
import os
import signal
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple

from . import budget, notify
from .ledger import SPEC_ID_RE, Ledger
from .util import clip, fmt_usd, now, read_json, read_text, write_json

# Why a plan paused. Each is a `kind` in a run's `paused` and in alerts.jsonl.
PAUSE_KINDS = (
    "review",      # the review said `pause`, or said something code does not honour
    "review_failed",  # no valid answer: invalid twice, a session error, a timeout, a process that died twice
    "retries",     # the review said `retry` and graph.max_retries is used up
    "stopped",     # the person stopped a run that others wait on
    "resumes",     # the runner died or the machine stopped more than graph.max_resumes times
    "dispatch",    # the run never got a machine, twice
    "budget",      # the weekly limit is at or above graph.pause_above_weekly_pct
    "unreviewed",  # a spec it waits on was reviewed before this text was approved, so no review read it
)
REPORT_OK = ("done", "partial")
UNREVIEWED = "reviewed before this text was approved"
# Where the reports of earlier runs travel in a run's bundle, relative to its run directory.
EARLIER = "earlier"


class GraphError(Exception):
    pass


# How many attempts a finished run's review gets before the plan pauses: a second chance covers a
# session that answered badly once, and a third would be the model deciding how often it may try.
REVIEW_ATTEMPTS = 2
# The fields this module keeps on a record; a record with none of them is no run of a plan.
PLAN_FIELDS = ("after", "held", "paused", "retry_of", "redispatch_of")


def _setting(ledger: Ledger, key: str, cfg: Optional[dict] = None) -> float:
    """A number under `graph.` in config.json, or its default when the ledger's is not a number."""
    from .ledger import DEFAULT_CONFIG

    value = ((cfg or ledger.config()).get("graph") or {}).get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        value = DEFAULT_CONFIG["graph"][key]
    return value


def _save(ledger: Ledger, run_id: str, change) -> Optional[dict]:
    """Apply `change(record)` to a run's record under the lock its other writers use, and return
    the record as saved; None when that lock is busy, so that the caller tries again on a later
    tick rather than waiting inside a pump.

    A queued run is written under the placement lock, which `start` holds while it decides; a run
    that sync still pulls under its `.sync.lock`, which sync takes without waiting and skips when
    taken; a final run, which nothing else writes, under `tick`'s own lock, which the caller holds.
    `change` returns False to leave the record as it was."""
    from . import dispatch

    rec = ledger.run(run_id)
    if not rec:
        return None
    if rec.get("state") == "queued":
        with dispatch._placing(ledger):
            rec = ledger.run(run_id)
            if rec.get("state") == "queued":
                if change(rec) is not False:
                    ledger.save_run(run_id, rec)
                return rec
    if rec.get("final"):
        if change(rec) is not False:
            ledger.save_run(run_id, rec)
        return rec
    with _sync_lock(ledger, run_id) as got:
        if not got:
            return None
        rec = ledger.run(run_id)
        if change(rec) is not False:
            ledger.save_run(run_id, rec)
        return rec


@contextmanager
def _sync_lock(ledger: Ledger, run_id: str):
    """The run's `.sync.lock`, taken without waiting: yields whether it was got."""
    ledger.run_dir(run_id).mkdir(parents=True, exist_ok=True)
    with open(ledger.run_dir(run_id) / ".sync.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True


def _of_plan(rec: dict) -> bool:
    return any(rec.get(k) for k in PLAN_FIELDS)


def _named_specs(ledger: Ledger) -> List[str]:
    """Every spec that an approved spec names in `after`."""
    out: List[str] = []
    for spec_id in ledger.approved_ids():
        for other in after_of(ledger, spec_id):
            if other not in out:
                out.append(other)
    return out


# ---------------------------------------------------------------- is there a plan at all


def _marker(ledger: Ledger):
    return ledger.root / "graph.json"


def has_plan(ledger: Ledger) -> bool:
    """Has a spec with `after` ever been dispatched here? One `stat`; everything below that looks
    at more than one run asks this first, so a ledger without a plan pays nothing for the feature."""
    return _marker(ledger).exists()


# ---------------------------------------------------------------- `after`: reading and checking


def short(spec_id: str) -> str:
    """How a spec is named in a sentence: its number, as in `009`."""
    m = SPEC_ID_RE.match(spec_id or "")
    return m.group(1) if m else str(spec_id)


def resolve(ledger: Ledger, ref: str) -> str:
    """The id of the spec that `ref` (an id or a number) names. LookupError when none or several do."""
    return ledger.resolve_spec(str(ref).strip())


def _named(ledger: Ledger, refs) -> List[str]:
    """The ids `refs` name, in order and once each; a reference that names nothing is kept as written."""
    out: List[str] = []
    for ref in refs or []:
        try:
            spec_id = resolve(ledger, ref)
        except LookupError:
            spec_id = str(ref).strip()
        if spec_id and spec_id not in out:
            out.append(spec_id)
    return out


def after_of(ledger: Ledger, spec_id: str) -> List[str]:
    """The specs that the approved text of `spec_id` comes after, as ids. [] when it names none."""
    meta = ledger.spec_meta(spec_id) or {}
    return _named(ledger, (meta.get("header") or {}).get("after"))


def _edges(ledger: Ledger, spec_id: str) -> List[str]:
    """What `spec_id` comes after by its approved text or by its draft: both count for a cycle,
    because the draft is what would be approved next."""
    from . import spec

    refs = list(((ledger.spec_meta(spec_id) or {}).get("header") or {}).get("after") or [])
    draft = read_text(ledger.draft_path(spec_id))
    if draft:
        refs += list(spec.parse(draft)[0].get("after") or [])
    return _named(ledger, refs)


def check_after(ledger: Ledger, spec_id: Optional[str], after: List[str]) -> List[str]:
    """Why a spec's `after` line cannot be approved: one sentence per fault, [] when it can.

    Refused: a reference that names no spec of this ledger (a draft counts: approvals may come in
    any order), one that several specs match, the spec itself, and a line that closes a cycle with
    the `after` lines of other specs. `spec_id` is None for a text that is no spec of this ledger
    yet; then only the references themselves are checked.
    """
    errors: List[str] = []
    named: List[str] = []
    for ref in after or []:
        try:
            other = resolve(ledger, ref)
        except LookupError as e:
            errors.append("header: after: %s" % e)
            continue
        if spec_id and other == spec_id:
            errors.append("header: after: %s is this spec itself" % ref)
        elif other not in named:
            named.append(other)
    if not spec_id:
        return errors
    # A walk from each named spec along the others' `after` lines: reaching this spec is a cycle.
    for first in named:
        path, seen, found = [first], {first}, None
        stack = [(first, iter(_edges(ledger, first)))]
        while stack and found is None:
            node, rest = stack[-1]
            nxt = next(rest, None)
            if nxt is None:
                stack.pop()
                path.pop()
            elif nxt == spec_id:
                found = [spec_id] + path + [spec_id]
            elif nxt not in seen:
                seen.add(nxt)
                path.append(nxt)
                stack.append((nxt, iter(_edges(ledger, nxt))))
        if found:
            errors.append("header: after: the specs form a cycle: %s" % " comes after ".join(short(s) for s in found))
    return errors


def record_fields(ledger: Ledger, meta: dict, plan: Optional[dict] = None) -> dict:
    """What a new run's record gains because of a plan: {} for a spec that names no other and is
    not dispatched again by `tick`.

    `after` is the approved header's list as ids. `plan` comes from `tick` alone and holds
    `retry_of` or `redispatch_of`. Returning anything marks the ledger as having a plan.
    """
    out: dict = {}
    after = _named(ledger, (meta.get("header") or {}).get("after"))
    if after:
        out["after"] = after
    for key in ("retry_of", "redispatch_of"):
        if (plan or {}).get(key):
            out[key] = plan[key]
    if out and not has_plan(ledger):
        write_json(_marker(ledger), {"since": now()})
    return out


def waiters(ledger: Ledger, spec_id: str) -> List[str]:
    """The approved specs whose `after` names `spec_id`, as ids, in order."""
    return [s for s in ledger.approved_ids() if s != spec_id and spec_id in after_of(ledger, s)]


# ---------------------------------------------------------------- satisfaction


def current_run(ledger: Ledger, spec_id: str) -> Optional[str]:
    """The latest run of the latest approved version of a spec, or None when it has none.

    This is the only run that can satisfy the spec, the only one that is reviewed, and the only
    one whose pause holds anything: a newer run or a newer version takes all three over."""
    meta = ledger.spec_meta(spec_id)
    if not meta:
        return None
    runs = [r for r in ledger.runs_for(spec_id) if ledger.run(r).get("spec_version", 1) == meta.get("version", 1)]
    return runs[-1] if runs else None


def report_status(ledger: Ledger, run_id: str) -> Optional[str]:
    """`done`, `partial` or `fail` as the run's report said (mirror/result.json), or None without one."""
    result = read_json(ledger.mirror_dir(run_id) / "result.json")
    status = result.get("status") if isinstance(result, dict) else None
    return status if status in ("done", "partial", "fail") else None


def satisfied(ledger: Ledger, spec_id: str, waiter: Optional[dict] = None) -> Tuple[bool, str]:
    """Is the spec satisfied, and the words for it in a `waiting for` clause.

    `waiter` is the record of the run that asks. A `continue` is a statement about the waiting
    specs the review was given, so for a waiter whose approved text was not among them (it was
    approved, or approved again, after the review) the spec is not satisfied: the words are
    `<state>, reviewed before this text was approved`, and `hold` makes that a pause.

    True only when `current_run` is final, its report's status is `done` or `partial`, and its
    record's `review.verdict` is `continue`. Otherwise the words say where it stands, such as
    `not approved yet`, `no run yet`, `queued`, `held`, `executing`, `dispatch failed`, `stopped`,
    `done, review pending`, `failed, being run again` or `paused`. A run of an earlier version and
    a run that was stopped never satisfy it.
    """
    from .runner import TERMINAL_STATES

    if not ledger.spec_meta(spec_id):
        return False, "not approved yet"
    run_id = current_run(ledger, spec_id)
    if not run_id:
        return False, "no run yet"
    rec = ledger.run(run_id)
    state = rec.get("state") or "queued"
    if rec.get("paused"):
        return False, "paused"
    if state == "queued":
        return False, "held" if rec.get("held") else "queued"
    if state == "dispatch_failed":
        return False, "dispatch failed"
    if state == "stopped":
        return False, "stopped"
    if state not in TERMINAL_STATES:
        return False, state
    if not rec.get("final"):
        return False, "%s, being copied" % state  # sync has not yet pulled all of it
    review = rec.get("review") or {}
    if review.get("verdict") == "continue" and report_status(ledger, run_id) in REPORT_OK:
        if waiter and (review.get("reviewed") or {}).get(waiter.get("spec_id")) != waiter.get("spec_sha"):
            return False, "%s, %s" % (state, UNREVIEWED)
        return True, state
    if review.get("verdict") == "retry":
        return False, "%s, being run again" % state
    return False, "%s, review pending" % state


def _and(parts: List[str]) -> str:
    return parts[0] if len(parts) == 1 else "%s and %s" % (", ".join(parts[:-1]), parts[-1])


def waiting_for(ledger: Ledger, record: dict) -> Optional[str]:
    """The sentence for `queue_reason` while a run's `after` is not all satisfied, one clause per
    unsatisfied spec, as in `waiting for 009 (executing) and 011 (not approved yet)`; three or
    more are joined with commas and a final `and`. None when every named spec is satisfied."""
    clauses = []
    for spec_id in record.get("after") or []:
        ok, words = satisfied(ledger, spec_id, record)
        if not ok:
            clauses.append("%s (%s)" % (short(spec_id), words))
    return ("waiting for " + _and(clauses)) if clauses else None


def over_budget(ledger: Ledger) -> Optional[str]:
    """The reason nothing may start by itself, or None: the newest weekly reading is not stale and
    is at or above `graph.pause_above_weekly_pct`. With no reading the limit is not taken to be near."""
    week = budget.weekly(ledger)
    if not week or week.get("stale") or not isinstance(week.get("utilization"), (int, float)):
        return None
    used, limit = week["utilization"] * 100.0, _setting(ledger, "pause_above_weekly_pct")
    if used < limit:
        return None
    return "the weekly limit is %.0f%% used, at or above graph.pause_above_weekly_pct (%g%%)" % (used, limit)


def blocked(ledger: Ledger, record: dict) -> Optional[str]:
    """Why a queued run must not start now, as its `queue_reason`; None when it may.

    Called by `dispatch.start` under the placement lock; it reads and never writes. In order:
    a record with none of `after`, `held`, `paused`, `retry_of`, `redispatch_of` is no run of a
    plan and gets None at once; a held or paused run gets `held: <reason>`; a released run gets
    None whatever it waited for; a run that waits gets `waiting_for`; and a run that could start
    while `over_budget` gets that reason (the next `tick` makes it a pause).
    """
    if not any(record.get(k) for k in ("after", "held", "paused", "retry_of", "redispatch_of")):
        return None
    if record.get("held") or record.get("paused"):
        return "held: %s" % (record.get("held") or record.get("paused") or {}).get("reason", "")
    if record.get("released_at"):
        return None
    return waiting_for(ledger, record) or over_budget(ledger)


# ---------------------------------------------------------------- what pump does for plans


def tick(ledger: Ledger, log=lambda message: None) -> None:
    """Everything a pump does for plans. Returns at once without a plan, and when another process
    is already inside (a non-blocking lock on `runs/.graph.lock`): each step happens once.

    The steps, in order; each is a function below so that it can be tested alone:
      1. `recover`: resume or dispatch again what died, within its bounds;
      2. `reviews`: take in a review that answered, notice one whose process ended, start one
         for a finished run that a queued run waits on;
      3. `hold`: lay holds from every paused run over what waits on it, and lift those whose
         cause is no longer current or is reached only through a released run;
      4. `tell`: write each new pause to alerts.jsonl and hand it to org/notify.py, and when a
         plan's last run finished with nothing held, say so once;
      5. `notify.flush`: send what is waiting to be sent.
    It never raises: a step that fails is logged and the rest still run.
    """
    if not has_plan(ledger):
        return
    try:
        ledger.runs.mkdir(parents=True, exist_ok=True)
        lock = open(ledger.runs / ".graph.lock", "w")
    except OSError as e:
        log("plans: cannot open the lock: %s" % e)
        return
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return  # another process is doing this tick's work
        for name, step in (("recover", lambda: recover(ledger, log)), ("reviews", lambda: reviews(ledger, log)),
                           ("hold", lambda: hold(ledger)), ("tell", lambda: tell(ledger)), ("notify", lambda: notify.flush(ledger))):
            try:
                step()
            except Exception as e:  # noqa: BLE001  (a plan's trouble must never stop the queue)
                log("plans: %s failed: %s: %s" % (name, type(e).__name__, e))


def _health(ledger: Ledger, rec: dict) -> Optional[str]:
    """The health `status.run_view` gives a run that has a machine and has not ended, derived from
    the record and mirror the same way; None for a run that has no such health."""
    from .runner import TERMINAL_STATES

    if rec.get("state") in ("queued", "provisioning", "dispatch_failed") or rec.get("final"):
        return None
    st = read_json(ledger.mirror_dir(rec.get("run_id") or "") / "status.json") or {}
    if (st.get("state") or rec.get("state")) in TERMINAL_STATES:
        return None
    if rec.get("reachable") is False:
        return "unreachable"
    if st and rec.get("alive") is False:
        return "dead"
    return "ok"


def recover(ledger: Ledger, log=lambda message: None) -> None:
    """Recovery that needs no review, for runs of a plan only (a record with `after`, or the
    current run of a spec that an approved spec names).

    A run whose health is `dead` has `dead_since` set; once that is `graph.dead_after_min` old it
    is resumed with `dispatch.resume`. A run that is unreachable and whose machine the backend
    says is stopped (`machine_stopped`) is resumed at once. Both count in `resumes`, at most
    `graph.max_resumes` per run; past that the run is paused (`resumes`). A run in
    `dispatch_failed` that is its spec's current run is dispatched again with `redispatch_of`,
    unless it has `redispatch_of` itself, which is a pause (`dispatch`). While `over_budget`,
    none of these is done and the run is paused (`budget`) instead. Each resume and dispatch is
    written to alerts.jsonl (`notify.alert`, kinds `resume` and `redispatch`).
    """
    named = _named_specs(ledger)
    cfg = ledger.config()
    for run_id in ledger.run_ids():
        rec = ledger.run(run_id)
        if rec.get("final") or rec.get("paused") or rec.get("state") == "queued":
            continue
        spec_id = rec.get("spec_id") or ""
        current = current_run(ledger, spec_id) == run_id
        if not (rec.get("after") or (spec_id in named and current)):
            continue
        try:
            if rec.get("state") == "dispatch_failed":
                if current:
                    _redispatch(ledger, run_id, rec, log)
                continue
            health = _health(ledger, rec)
            # A run resumed since sync last saw it still looks as it did before: wait for sync.
            if rec.get("resumed_at") and (rec.get("last_sync_at") or 0) < rec["resumed_at"]:
                continue
            if health != "dead" and rec.get("dead_since"):
                _save(ledger, run_id, lambda r: _set(r, dead_since=None))
            if health == "dead":
                since = rec.get("dead_since")
                if not since:
                    _save(ledger, run_id, lambda r: _set(r, dead_since=r.get("dead_since") or now()))
                    continue
                if now() - since < _setting(ledger, "dead_after_min", cfg) * 60:
                    continue
                why = "its runner has been gone since %s" % _clock(since)
            elif health == "unreachable" and machine_stopped(ledger, rec):
                why = "its machine %s is stopped" % (rec.get("machine") or {}).get("name")
            else:
                continue
            _resume(ledger, run_id, rec, why, cfg, log)
        except Exception as e:  # noqa: BLE001  (one run's trouble must not stop the others' recovery)
            log("plans: recovering %s failed: %s: %s" % (run_id, type(e).__name__, e))


def _set(rec: dict, **fields) -> None:
    """Set fields on a record; None removes one."""
    for key, value in fields.items():
        if value is None:
            rec.pop(key, None)
        else:
            rec[key] = value


def _clock(t: float) -> str:
    from .util import fmt_clock

    return fmt_clock(t)


def _resume(ledger: Ledger, run_id: str, rec: dict, why: str, cfg: dict, log) -> None:
    """Resume one run within `graph.max_resumes`, under its `.sync.lock` so that no pull of the
    same run writes its record meanwhile. The count goes up before the attempt, so that an attempt
    that fails counts too and the bound holds."""
    from . import dispatch

    limit = int(_setting(ledger, "max_resumes", cfg))
    if int(rec.get("resumes") or 0) >= limit:
        pause(ledger, run_id, "resumes", "%s, and it was resumed %d time(s) already (graph.max_resumes is %d)"
              % (why, int(rec.get("resumes") or 0), limit))
        return
    reason = over_budget(ledger)
    if reason:
        pause(ledger, run_id, "budget", "%s; not resumed because %s" % (why, reason))
        return
    with _sync_lock(ledger, run_id) as got:
        if not got:
            return  # sync is pulling it: the next tick looks again
        rec = ledger.run(run_id)
        n = int(rec.get("resumes") or 0) + 1
        rec.update(resumes=n)
        rec.pop("dead_since", None)
        ledger.save_run(run_id, rec)
        log("plans: resuming %s: %s" % (run_id, why))
        dispatch.resume(ledger, run_id, log)
        notify.alert(ledger, run_id, "resume", "%s; resumed (%d of %d)" % (why, n, limit))


def _redispatch(ledger: Ledger, run_id: str, rec: dict, log) -> None:
    """Dispatch again, once, the spec of a current run that never got a machine."""
    from . import dispatch

    error = clip(rec.get("error") or "no machine", 200)
    if rec.get("redispatch_of"):
        pause(ledger, run_id, "dispatch", "it never got a machine, twice: %s" % error)
        return
    reason = over_budget(ledger)
    if reason:
        pause(ledger, run_id, "budget", "it never got a machine (%s), and it was not dispatched again because %s" % (error, reason))
        return
    spec_id = rec["spec_id"]
    try:
        # What it retried travels on: the failed run's report is in the new run's bundle too.
        new_id, _ = dispatch.dispatch(ledger, spec_id, log=log, plan={"redispatch_of": run_id, "retry_of": rec.get("retry_of")})
    except dispatch.DispatchError as e:
        if current_run(ledger, spec_id) == run_id:
            # No new run was even recorded, so nothing would stop this from being tried at every pump.
            pause(ledger, run_id, "dispatch", "it never got a machine (%s), and dispatching it again failed: %s" % (error, clip(e, 200)))
            return
        new_id = current_run(ledger, spec_id)
        log("plans: %s dispatched again as %s, which failed too: %s" % (run_id, new_id, e))
    notify.alert(ledger, run_id, "redispatch", "it never got a machine (%s); dispatched again as %s" % (error, new_id))


def machine_stopped(ledger: Ledger, record: dict) -> bool:
    """Does the backend say this run's machine exists and is not running? Asked only for an Incus
    or Docker machine (`Incus.info`, `Docker.state`); for any other kind, and when the question
    itself fails, the answer is no: unreachable is not the same as stopped."""
    from .transport import Docker, Incus

    machine = record.get("machine") or {}
    name = machine.get("name")
    if not name:
        return False
    try:
        if machine.get("kind") == "incus":
            info = Incus.info(name)
            return info is not None and info.get("status") != "Running"
        if machine.get("kind") == "docker":
            state = Docker.state(name, machine.get("host"))
            return state is not None and state != "running"
    except Exception:  # noqa: BLE001  (a question that fails is not an answer)
        return False
    return False


def reviews(ledger: Ledger, log=lambda message: None) -> None:
    """The review of each finished run that something waits on.

    A run is due when it is its spec's `current_run`, final, in `done`, `partial` or `failed`,
    has no `review`, and a queued run that is neither held nor released has its spec in `after`.
    A `stopped` run is never reviewed: it is paused (`stopped`) with no session.

    Due: unless `over_budget` (a pause, `budget`), set `review` to state `running`, attempts 1,
    and the process `review.start` returns. Running: if `review.outcome` has an answer for this
    attempt, add its cost and either `apply_verdict` (kind `answer`), count a failed attempt
    (kind `invalid`), or pause with `review_failed` (kinds `error` and `timeout`); if there is
    none and `dispatch._gone(proc)`, count a failed attempt; if there is none and the review's
    current attempt began more than `graph.review_timeout_min` plus one minute ago, end its
    process group and pause. A failed attempt starts the second attempt; a second failed attempt is a pause.
    """
    waited = _waited_on(ledger)
    for spec_id in _named_specs(ledger):
        run_id = current_run(ledger, spec_id)
        if not run_id:
            continue
        try:
            rec = ledger.run(run_id)
            review = rec.get("review") or {}
            if review.get("state") == "running":
                _review_running(ledger, run_id, rec, log)
            elif not review and spec_id in waited and _finished(rec) and not rec.get("paused"):
                _review_due(ledger, run_id, rec, log)
            elif review.get("state") == "done" and review.get("verdict") in ("retry", "pause") and not rec.get("paused"):
                # The verdict is on record and what follows it is not: the tick that recorded it
                # ended before it dispatched the run again or marked the pause. A retry would by
                # now have put a newer run in this one's place. Left alone, what waits would wait
                # for ever with nobody told; so it is a pause, and the person decides.
                said = "the review said %s: %s" % (review["verdict"], review.get("reason") or "no reason given")
                if review["verdict"] == "retry":
                    pause(ledger, run_id, "dispatch", "%s; it was not dispatched again, because the process doing that ended first" % said)
                else:
                    pause(ledger, run_id, "review", said)
        except Exception as e:  # noqa: BLE001  (one review's trouble must not stop the others)
            log("plans: the review of %s failed: %s: %s" % (run_id, type(e).__name__, e))


def _finished(rec: dict) -> bool:
    return bool(rec.get("final")) and rec.get("state") in ("done", "partial", "failed", "stopped")


def _waited_on(ledger: Ledger) -> List[str]:
    """The specs that a queued run, neither held nor released, names in `after`: those whose
    finished run is worth a review now."""
    out: List[str] = []
    for run_id in ledger.run_ids():
        rec = ledger.run(run_id)
        if rec.get("state") == "queued" and not rec.get("held") and not rec.get("paused") and not rec.get("released_at"):
            out += [s for s in rec.get("after") or [] if s not in out]
    return out


def _review_due(ledger: Ledger, run_id: str, rec: dict, log) -> None:
    if rec.get("state") == "stopped":
        pause(ledger, run_id, "stopped", "it was stopped before it finished, and other specs wait on it")
        return
    reason = over_budget(ledger)
    if reason:
        pause(ledger, run_id, "budget", "it finished and was not reviewed because %s" % reason)
        return
    _start_attempt(ledger, run_id, 1, log)


def _start_attempt(ledger: Ledger, run_id: str, attempt: int, log) -> None:
    """Record the attempt before its process starts: a start that fails then leaves a review whose
    process is gone, which counts as a failed attempt, so even that is bounded."""
    from . import review

    def begin(r):
        rv = r.get("review") or {"state": "running", "cost_usd": 0.0, "started_at": now()}
        rv.update(state="running", attempts=attempt, proc={})
        r["review"] = rv

    _save(ledger, run_id, begin)
    log("plans: reviewing %s (attempt %d)" % (run_id, attempt))
    proc = review.start(ledger, run_id, attempt)
    _save(ledger, run_id, lambda r: r["review"].update(proc=dict(proc or {})))


def _review_running(ledger: Ledger, run_id: str, rec: dict, log) -> None:
    from . import dispatch, review

    rv = rec["review"]
    attempt = int(rv.get("attempts") or 1)
    out = review.outcome(ledger, run_id, attempt)
    if out:
        cost = out.get("cost_usd") if isinstance(out.get("cost_usd"), (int, float)) else 0.0
        kind = out.get("kind")
        _save(ledger, run_id, lambda r: r["review"].update(cost_usd=round(float(r["review"].get("cost_usd") or 0) + cost, 6)))
        if kind == "answer" and isinstance(out.get("answer"), dict):
            _save(ledger, run_id, lambda r: r["review"].update(answer=out["answer"]))
            apply_verdict(ledger, run_id, out["answer"], log)
        elif kind == "invalid":
            problems = "; ".join(str(p) for p in out.get("problems") or []) or "no reason given"
            _failed_attempt(ledger, run_id, attempt, "its answer was not valid: %s" % clip(problems, 200), log)
        elif kind == "timeout":
            limit = _setting(ledger, "review_timeout_min")
            _review_failed(ledger, run_id, "the review session ran longer than graph.review_timeout_min (%g min)" % limit)
        else:
            _review_failed(ledger, run_id, "the review session failed: %s" % clip(out.get("error") or kind or "no reason given", 200))
        return
    proc = rv.get("proc") or {}
    if dispatch._gone(proc):
        _failed_attempt(ledger, run_id, attempt, "its process ended without an answer", log)
        return
    limit = _setting(ledger, "review_timeout_min")
    began = proc.get("at") or rv.get("started_at") or now()
    if now() - began > (limit + 1) * 60:
        _end_group(proc)
        _review_failed(ledger, run_id, "the review did not answer within graph.review_timeout_min (%g min)" % limit)


def _end_group(proc: dict) -> None:
    """End a review's process group. Never this process's own group: a record that names it is
    a mistake, and ending it would end whatever is pumping."""
    try:
        pid = int(proc.get("pid") or 0)
        if pid <= 1 or pid == os.getpid() or os.getpgid(pid) == os.getpgrp():
            return
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (OSError, ValueError):
        pass


def _failed_attempt(ledger: Ledger, run_id: str, attempt: int, why: str, log) -> None:
    if attempt < REVIEW_ATTEMPTS:
        log("plans: the review of %s failed (%s); trying once more" % (run_id, why))
        _save(ledger, run_id, lambda r: r["review"].update(last_failure=why))
        _start_attempt(ledger, run_id, attempt + 1, log)
        return
    _review_failed(ledger, run_id, "the review gave no valid answer in %d attempts; the last: %s" % (attempt, why))


def _review_failed(ledger: Ledger, run_id: str, why: str) -> None:
    why = notify.plain(why)
    _save(ledger, run_id, lambda r: r["review"].update(state="done", verdict="pause", reason=why, finished_at=now()))
    pause(ledger, run_id, "review_failed", why)


def apply_verdict(ledger: Ledger, run_id: str, answer: dict, log=lambda message: None) -> str:
    """Honour a validated answer within the bounds, record it, and return the verdict as honoured.

    `continue` is honoured only when `report_status` is `done` or `partial`; then
    `review.notes` keeps the note for each spec in `waiters`. `retry` is honoured only when the
    report's status is not `done`, the runs of this spec version that have `retry_of` number
    fewer than `graph.max_retries`, and not `over_budget`; then the spec is dispatched again with
    `plan={"retry_of": run_id}` and alerts.jsonl gets a `retry` line. Everything else is `pause`:
    the run is marked `paused` with the kind and a reason that says what the review answered and
    why it was not followed. The reason the session gave is kept as text and never acted on.
    """
    from . import dispatch

    rec = ledger.run(run_id)
    spec_id = rec.get("spec_id") or ""
    status = report_status(ledger, run_id)
    asked = answer.get("verdict") if isinstance(answer, dict) else None
    # The reason and the notes were written by a model that read text from public sources, and
    # they are printed in a terminal: kept as one plain line each, with no control character.
    said = notify.plain(clip((answer or {}).get("reason") or "", 300)) if isinstance(answer, dict) else ""
    told = "the review said %s: %s" % (asked or "nothing usable", said or "no reason given")
    kind, why, notes, reviewed = "review", None, {}, {}
    if asked == "continue":
        if status in REPORT_OK:
            waiting = waiters(ledger, spec_id)
            for n in (answer.get("notes") or []):
                if isinstance(n, dict) and n.get("spec") in waiting and isinstance(n.get("note"), str):
                    notes[n["spec"]] = notify.plain(n["note"])
            # Which text of each waiting spec this `continue` is about: the one approved now.
            reviewed = {w: (ledger.spec_meta(w) or {}).get("sha256") for w in notes}
        else:
            why = "a run whose report says %s does not satisfy what waits on it" % (status or "nothing (there is no report)")
    elif asked == "retry":
        limit = int(_setting(ledger, "max_retries"))
        version = rec.get("spec_version", 1)
        tried = [r for r in ledger.runs_for(spec_id) if ledger.run(r).get("retry_of") and ledger.run(r).get("spec_version", 1) == version]
        if status == "done":
            why = "the report says done, and a run that did its work is not run again"
        elif len(tried) >= limit:
            kind, why = "retries", "this version was already run again %d time(s) (graph.max_retries is %d)" % (len(tried), limit)
        else:
            why = over_budget(ledger)
            kind = "budget" if why else kind
    elif asked != "pause":
        why = "that is not a verdict code follows"
    honoured = asked if (why is None and asked in ("continue", "retry")) else "pause"

    def record(r):
        rv = r.get("review") or {}
        rv.update(state="done", verdict=honoured, reason=said, notes=notes, reviewed=reviewed, finished_at=now())
        if isinstance(answer, dict):
            rv.setdefault("answer", answer)
        r["review"] = rv

    _save(ledger, run_id, record)
    if honoured == "pause":
        pause(ledger, run_id, kind, told if why is None else "%s; not followed: %s" % (told, why))
    elif honoured == "retry":
        try:
            new_id, _ = dispatch.dispatch(ledger, spec_id, log=log, plan={"retry_of": run_id})
        except dispatch.DispatchError as e:
            if current_run(ledger, spec_id) == run_id:
                _save(ledger, run_id, lambda r: r["review"].update(verdict="pause"))
                pause(ledger, run_id, "dispatch", "%s; dispatching it again failed: %s" % (told, clip(e, 200)))
                return "pause"
            new_id = current_run(ledger, spec_id)
        notify.alert(ledger, run_id, "retry", "%s; dispatched again as %s" % (told, new_id))
        log("plans: %s is run again as %s" % (run_id, new_id))
    return honoured


def pause(ledger: Ledger, run_id: str, kind: str, reason: str) -> None:
    """Mark a run as the cause of a pause, once: a run that is already paused keeps its first
    reason. `hold` and `tell` do the rest on this or the next tick."""
    def mark(r):
        if r.get("paused"):
            return False
        r["paused"] = {"kind": kind, "reason": notify.plain(clip(reason, 600)), "at": now()}

    _save(ledger, run_id, mark)


def hold(ledger: Ledger) -> None:
    """Make `held` on every queued run agree with the pauses in force.

    A pause is in force while its run has `paused` and is either queued itself or its spec's
    `current_run`. It reaches every queued run that is not released and whose `after` leads to
    the paused run's spec, directly or through other specs, by a path that does not pass through
    a spec whose current run is released. Such a run gets `held` (cause, the pause's reason,
    time) unless it has one; a run whose `held` names a cause that no longer reaches it loses it
    and waits as usual. A paused queued run holds itself.
    """
    from . import dispatch

    # A run that could start now but for the weekly limit would otherwise start the moment the
    # reading drops or goes stale, with nobody told: it is a pause like any other.
    reason = over_budget(ledger)
    if reason:
        for run_id in ledger.run_ids():
            rec = ledger.run(run_id)
            if (rec.get("state") == "queued" and _of_plan(rec) and not rec.get("held") and not rec.get("paused")
                    and not rec.get("released_at") and not waiting_for(ledger, rec)):
                pause(ledger, run_id, "budget", "it could start, and was not started because %s" % reason)

    # A run that waits on a spec whose review was not given this run's text would start on a
    # `continue` that says nothing about it. Nothing reads the report for it a second time (a
    # finished run is reviewed once), so it is a pause, and `org release` is the person's answer.
    for run_id in ledger.run_ids():
        rec = ledger.run(run_id)
        if rec.get("state") == "queued" and rec.get("after") and not rec.get("held") and not rec.get("paused") and not rec.get("released_at"):
            unread = [s for s in rec["after"] if satisfied(ledger, s, rec)[1].endswith(UNREVIEWED)]
            if unread:
                pause(ledger, run_id, "unreviewed", "%s %s reviewed before this text of %s was approved, so no review has read it against "
                      "what was built" % (_and([short(s) for s in unread]), "was" if len(unread) == 1 else "were", short(rec.get("spec_id") or "")))

    records = {r: ledger.run(r) for r in ledger.run_ids()}
    current = {}
    for spec_id in set(rec.get("spec_id") for rec in records.values() if rec.get("spec_id")):
        current[spec_id] = current_run(ledger, spec_id)
    # The pauses in force, by the spec whose run caused each.
    causes: Dict[str, List[str]] = {}
    for run_id, rec in records.items():
        if rec.get("paused") and (rec.get("state") == "queued" or current.get(rec.get("spec_id")) == run_id):
            causes.setdefault(rec.get("spec_id"), []).append(run_id)
    released = {s for s, r in current.items() if r and records.get(r, {}).get("released_at")}

    def reaching(run_id: str, rec: dict) -> List[str]:
        found = [run_id] if run_id in causes.get(rec.get("spec_id"), []) and rec.get("paused") else []
        seen, todo = set(), list(rec.get("after") or [])
        while todo:
            spec_id = todo.pop(0)
            if spec_id in seen:
                continue
            seen.add(spec_id)
            found += [c for c in causes.get(spec_id, []) if c not in found]
            if spec_id not in released:
                todo += after_of(ledger, spec_id)
        return found

    wanted: Dict[str, Optional[str]] = {}
    for run_id, rec in records.items():
        if rec.get("state") != "queued" or rec.get("released_at"):
            continue
        found = reaching(run_id, rec)
        held = rec.get("held") or {}
        if held and held.get("cause") in found:
            continue
        if found or held:
            wanted[run_id] = found[0] if found else None
    if not wanted:
        return
    with dispatch._placing(ledger):
        for run_id, cause in wanted.items():
            rec = ledger.run(run_id)
            if rec.get("state") != "queued" or rec.get("released_at"):
                continue
            if cause is None:
                rec.pop("held", None)
            else:
                rec["held"] = {"cause": cause, "reason": (ledger.run(cause).get("paused") or {}).get("reason") or "", "at": now()}
            ledger.save_run(run_id, rec)


def tell(ledger: Ledger) -> None:
    """Tell the person about each pause once: for every run with `paused` and no `alerted_at`,
    count the runs it holds, call `notify.pause`, and set `alerted_at`. Then, for each plan
    (`plans`) whose every spec's current run is final, with none held or paused, that has not
    been reported before, call `notify.finished` once with each run's outcome and the total spent."""
    for run_id in ledger.run_ids():
        rec = ledger.run(run_id)
        paused = rec.get("paused") or {}
        if not paused or paused.get("alerted_at"):
            continue
        reason, since = paused.get("reason") or "", paused.get("at") or 0

        def mark(r):
            if not r.get("paused") or r["paused"].get("alerted_at"):
                return False
            r["paused"]["alerted_at"] = now()

        # Told first and marked after, so that a tick that ends in between leaves a pause that
        # is looked at again and never one that nobody was told of. The line in alerts.jsonl is
        # the sign that it was told: a record whose lock is busy now is marked on a later tick
        # without a second message.
        if not any(a.get("run") == run_id and a.get("kind") == "pause" and a.get("reason") == reason and (a.get("at") or 0) >= since
                   for a in notify.alerts(ledger)):
            notify.pause(ledger, run_id, reason, _held_by(ledger, run_id))
        _save(ledger, run_id, mark)

    marker = read_json(_marker(ledger), {}) or {}
    told = marker.get("finished") or {}
    for plan in plans(ledger):
        runs = [current_run(ledger, s) for s in plan]
        if not all(runs):
            continue
        recs = [ledger.run(r) for r in runs]
        if not all(r.get("final") for r in recs) or any(r.get("paused") or r.get("held") for r in recs):
            continue
        key = ",".join(runs)
        if key in told:
            continue
        told[key] = now()
        marker["finished"] = told
        write_json(_marker(ledger), marker)
        notify.finished(ledger, runs[-1], [(r, rec.get("state") or "?") for r, rec in zip(runs, recs)],
                        round(sum(_spent(ledger, r) for r in runs), 2))


def _held_by(ledger: Ledger, cause: str) -> int:
    """How many queued runs a pause holds, the paused run itself among them when it is queued."""
    return sum(1 for r in ledger.run_ids() if (ledger.run(r).get("held") or {}).get("cause") == cause
               and ledger.run(r).get("state") == "queued")


def _spent(ledger: Ledger, run_id: str) -> float:
    """What a run and the runs it replaced cost, their reviews included, in dollars."""
    total, seen = 0.0, set()
    while run_id and run_id not in seen:
        seen.add(run_id)
        rec = ledger.run(run_id)
        st = read_json(ledger.mirror_dir(run_id) / "status.json") or {}
        usd = (st.get("cost") or {}).get("usd") if isinstance(st, dict) else None
        total += float(usd) if isinstance(usd, (int, float)) else 0.0
        rv = (rec.get("review") or {}).get("cost_usd")
        total += float(rv) if isinstance(rv, (int, float)) else 0.0
        run_id = rec.get("retry_of") or rec.get("redispatch_of")
    return total


def release(ledger: Ledger, run_id: str) -> str:
    """Lift the hold on a queued run and return a sentence saying what will happen.

    Under the placement lock: `held` and `paused` are removed and `released_at` is set, so the
    run starts at the next pump with a free slot, whether or not what it waited for is satisfied
    and whatever the weekly limit; what depends on it then waits for it as usual. GraphError,
    with a sentence that names what to do instead, for a run that is not queued or not held."""
    from . import dispatch

    with dispatch._placing(ledger):
        rec = ledger.run(run_id)
        if not rec:
            raise GraphError("there is no run %s" % run_id)
        if rec.get("state") != "queued":
            raise GraphError("%s is not queued (it is %s), so there is nothing to release: `org release` lifts the hold on a "
                             "queued run of a plan. `org graph` shows what is held." % (run_id, rec.get("state")))
        if not (rec.get("held") or rec.get("paused")):
            why = blocked(ledger, rec) or rec.get("queue_reason") or "it waits for a free slot"
            raise GraphError("%s is not held, so there is nothing to release: it is queued (%s) and starts by itself. "
                             "`org stop %s` removes it from the queue." % (run_id, why, run_id))
        rec.pop("held", None)
        rec.pop("paused", None)
        rec["released_at"] = now()
        ledger.save_run(run_id, rec)
    return ("released %s: it starts when a slot is free, whether or not what it waited for is satisfied; "
            "what comes after it waits for it as usual" % run_id)


# ---------------------------------------------------------------- what travels, and what is shown


def bundle_files(ledger: Ledger, run_id: str) -> List[Tuple[str, bytes]]:
    """Files added to a run's bundle because of a plan, as (name in the run directory, bytes).
    [] for a run that has neither `after` nor `retry_of`.

    For each spec in `after`, from its `current_run` (the satisfying run, or for a released run
    whatever run there is): `earlier/<run id>/report.md`, the mirror's report, and
    `earlier/<run id>/review.md`, which says in plain sentences which spec and run this is, how
    it ended, the review's verdict and reason, and the review's note for this run's spec; for a
    run that did not satisfy its spec it says so. For `retry_of`, the same two files for the
    failed run, with a first sentence saying that this run is another attempt at the same spec.
    """
    record = ledger.run(run_id) if run_id else {}
    if not (record.get("after") or record.get("retry_of")):
        return []
    mine = record.get("spec_id") or ""
    out: List[Tuple[str, bytes]] = []
    for spec_id in record.get("after") or []:
        earlier = current_run(ledger, spec_id)
        if not earlier:
            continue
        ok, words = satisfied(ledger, spec_id)
        lines = ["# %s: the run of %s that this run comes after" % (earlier, spec_id), "",
                 "Your spec, %s, names %s in `after`. This is what its latest run, %s, left." % (mine, spec_id, earlier)]
        if not ok:
            lines.append("That run did not satisfy %s (%s)%s." % (
                spec_id, words, ": the person released this run, and it started anyway" if record.get("released_at") else ""))
        lines += _ending(ledger, earlier)
        note = ((ledger.run(earlier).get("review") or {}).get("notes") or {}).get(mine)
        if note:
            lines += ["", "The review's note for %s:" % mine, "", note]
        out += _earlier(ledger, earlier, lines)
    failed = record.get("retry_of")
    if failed:
        lines = ["# %s: the run this one is another attempt after" % failed, "",
                 "This run is another attempt at the same spec, %s, at the same approved text: run %s did not finish well, "
                 "and the review said to try again." % (mine, failed)]
        lines += _ending(ledger, failed)
        out += _earlier(ledger, failed, lines)
    return out


def _ending(ledger: Ledger, run_id: str) -> List[str]:
    """How a run ended and what its review said, as sentences."""
    rec = ledger.run(run_id)
    status = report_status(ledger, run_id)
    lines = ["", "It ended %s; its report's status is %s." % (rec.get("state") or "?", status or "unknown (it wrote no report)")]
    rv = rec.get("review") or {}
    if rv.get("verdict"):
        lines.append("The review said %s: %s" % (rv["verdict"], rv.get("reason") or "no reason given"))
    else:
        lines.append("It has no review.")
    return lines


def _earlier(ledger: Ledger, run_id: str, lines: List[str]) -> List[Tuple[str, bytes]]:
    out = []
    report = ledger.mirror_dir(run_id) / "report.md"
    if report.exists():
        out.append(("%s/%s/report.md" % (EARLIER, run_id), report.read_bytes()))
    else:
        lines += ["", "It left no report."]
    out.append(("%s/%s/review.md" % (EARLIER, run_id), ("\n".join(lines) + "\n").encode("utf-8")))
    return out


def annotate(ledger: Ledger, record: dict, view: dict) -> dict:
    """Lay a plan's facts over a run's view (`status.run_view`) and return it. A view of a run
    with none of the fields this module keeps is returned as it came.

    Adds `after`, `held`, `paused` and `review` (without `proc`) as on the record, and appends to
    `warnings`: for a held run `held: <reason>. \\`org release <run>\\` starts it anyway`; for a
    paused run that holds something or has not ended `paused: <reason>. <n> run(s) held`; and
    `notify.failed`'s sentence when a message about this run could not be sent.
    """
    if not any(record.get(k) for k in ("after", "held", "paused", "review", "retry_of", "redispatch_of", "released_at")):
        return view
    run_id = record.get("run_id") or view.get("run_id") or ""
    for key in ("after", "held", "paused"):
        if record.get(key):
            view[key] = record[key]
    if record.get("review"):
        view["review"] = {k: v for k, v in record["review"].items() if k != "proc"}
    warnings = list(view.get("warnings") or [])
    held, paused = record.get("held"), record.get("paused")
    if held and record.get("state") == "queued":
        warnings.append("held: %s. `org release %s` starts it anyway" % (held.get("reason") or "", run_id))
    if paused:
        n = _held_by(ledger, run_id)
        itself = bool(held and held.get("cause") == run_id and record.get("state") == "queued")
        # A paused queued run that holds only itself is told by its `held` line already.
        if n - itself > 0 or not (record.get("final") or itself):
            warnings.append("paused: %s. %d run(s) held" % (paused.get("reason") or "", n))
    failed = notify.failed(ledger, run_id)
    if failed:
        warnings.append(failed)
    view["warnings"] = warnings
    return view


def review_text(ledger: Ledger, run_id: str) -> str:
    """What `org review` adds for a run that was reviewed: the verdict as honoured, what the
    session answered when that differs, the reason, the notes, the attempts and the cost in
    dollars. "" for a run without a review."""
    if not ledger.run(run_id).get("review"):
        return ""
    rec = ledger.run(run_id)
    rv = rec["review"]
    if rv.get("state") == "running":
        lines = ["review: running (attempt %s)" % rv.get("attempts", 1)]
    else:
        lines = ["review: %s" % (rv.get("verdict") or "?")]
        asked = (rv.get("answer") or {}).get("verdict")
        if asked and asked != rv.get("verdict"):
            lines.append("  the session answered %s; code did not follow it" % asked)
        if rv.get("reason"):
            lines.append("  reason: %s" % rv["reason"])
        for spec_id, note in sorted((rv.get("notes") or {}).items()):
            lines.append("  note for %s: %s" % (spec_id, note))
    if (rec.get("paused") or {}).get("reason"):
        lines.append("  paused: %s" % rec["paused"]["reason"])
    lines.append("  attempts %s · cost %s" % (rv.get("attempts", 1), fmt_usd(rv.get("cost_usd") or 0.0)))
    return "\n".join(lines)


def plans(ledger: Ledger) -> List[List[str]]:
    """Every plan: the approved specs joined by `after`, each plan in an order in which every
    spec comes after those it names, plans ordered by their first spec. [] without a plan."""
    if not has_plan(ledger):
        return []
    approved = ledger.approved_ids()
    edges = {s: [a for a in after_of(ledger, s) if a in approved] for s in approved}
    joined = {s for s, after in edges.items() if after} | {a for after in edges.values() for a in after}
    # Each plan is a connected part of the specs joined by `after`, found by a walk both ways.
    neighbours: Dict[str, set] = {s: set() for s in joined}
    for s, after in edges.items():
        for a in after:
            neighbours[s].add(a)
            neighbours[a].add(s)
    out, seen = [], set()
    for first in sorted(joined):
        if first in seen:
            continue
        part, todo = set(), [first]
        while todo:
            s = todo.pop()
            if s not in part:
                part.add(s)
                todo += sorted(neighbours[s] - part)
        seen |= part
        # In order: each spec once all it names are placed, the lowest id first among those ready.
        order, left = [], set(part)
        while left:
            ready = sorted(s for s in left if all(a in order for a in edges[s] if a in part))
            if not ready:  # a cycle, which approval refuses; shown in id order rather than not at all
                ready = sorted(left)
            order.append(ready[0])
            left.discard(ready[0])
        out.append(order)
    return out


def render(ledger: Ledger) -> str:
    """`org graph`: for each plan, its specs in order, each with its current run, that run's
    state, what it waits for or why it is held, and its review's verdict and reason. The runs
    that the current one replaced are listed under it with theirs: a `retry` is the verdict on
    the run that failed, and would otherwise be nowhere on the page."""
    if not has_plan(ledger):
        return "No plans: no spec that names another in `after` has been dispatched."
    found = plans(ledger)
    if not found:
        return "No plans: no approved spec names another in `after`."
    lines: List[str] = []
    for i, plan in enumerate(found):
        if i:
            lines.append("")
        lines.append("Plan %s" % " → ".join(short(s) for s in plan))
        for spec_id in plan:
            run_id = current_run(ledger, spec_id)
            rec = ledger.run(run_id) if run_id else {}
            after = after_of(ledger, spec_id)
            head = "  %s%s" % (spec_id, (" (after %s)" % ", ".join(short(a) for a in after)) if after else "")
            if not run_id:
                lines.append("%s: no run yet" % head)
                continue
            what = rec.get("state") or "?"
            if rec.get("held"):
                what += ", held: %s" % rec["held"].get("reason")
            elif rec.get("paused"):
                what += ", paused: %s" % rec["paused"].get("reason")
            elif rec.get("state") == "queued":
                what += ", %s" % (blocked(ledger, rec) or rec.get("queue_reason") or "waiting for a free slot")
            if rec.get("released_at"):
                what += " (released)"
            lines.append("%s: %s %s" % (head, run_id, what))
            replaced, old = [], rec.get("retry_of") or rec.get("redispatch_of")
            while old and old not in replaced:
                replaced.append(old)
                old = ledger.run(old).get("retry_of") or ledger.run(old).get("redispatch_of")
            for old in reversed(replaced):
                was = ledger.run(old)
                said = was.get("review") or {}
                lines.append("    before it: %s %s%s" % (old, was.get("state") or "?",
                                                         ("; review: %s: %s" % (said["verdict"], said.get("reason") or "")) if said.get("verdict") else ""))
            rv = rec.get("review") or {}
            if rv.get("state") == "running":
                lines.append("    review: running (attempt %s)" % rv.get("attempts", 1))
            elif rv:
                lines.append("    review: %s: %s" % (rv.get("verdict") or "?", rv.get("reason") or ""))
    return "\n".join(lines)
