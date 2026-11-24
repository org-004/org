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
                  "notes": {waiting spec id: note}}
  resumes        n               times `tick` resumed it
  dead_since     time            when `tick` first saw its runner gone; cleared when it is alive

A record is written only under the lock that its other writers use: the placement lock
(`dispatch._placing`) for a queued run, the run's `.sync.lock` for one that sync still pulls, and
`tick`'s own lock for a run that is final, which nothing else writes.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from . import budget, notify
from .ledger import SPEC_ID_RE, Ledger
from .util import now, read_json, read_text, write_json

# Why a plan paused. Each is a `kind` in a run's `paused` and in alerts.jsonl.
PAUSE_KINDS = (
    "review",      # the review said `pause`, or said something code does not honour
    "review_failed",  # no valid answer: invalid twice, a session error, a timeout, a process that died twice
    "retries",     # the review said `retry` and graph.max_retries is used up
    "stopped",     # the person stopped a run that others wait on
    "resumes",     # the runner died or the machine stopped more than graph.max_resumes times
    "dispatch",    # the run never got a machine, twice
    "budget",      # the weekly limit is at or above graph.pause_above_weekly_pct
)
REPORT_OK = ("done", "partial")
# Where the reports of earlier runs travel in a run's bundle, relative to its run directory.
EARLIER = "earlier"


class GraphError(Exception):
    pass


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


def satisfied(ledger: Ledger, spec_id: str) -> Tuple[bool, str]:
    """Is the spec satisfied, and the words for it in a `waiting for` clause.

    True only when `current_run` is final, its report's status is `done` or `partial`, and its
    record's `review.verdict` is `continue`. Otherwise the words say where it stands, such as
    `not approved yet`, `no run yet`, `queued`, `held`, `executing`, `dispatch failed`, `stopped`,
    `done, review pending`, `failed, being run again` or `paused`. A run of an earlier version and
    a run that was stopped never satisfy it.
    """
    raise NotImplementedError


def waiting_for(ledger: Ledger, record: dict) -> Optional[str]:
    """The sentence for `queue_reason` while a run's `after` is not all satisfied, one clause per
    unsatisfied spec, as in `waiting for 009 (executing) and 011 (not approved yet)`; three or
    more are joined with commas and a final `and`. None when every named spec is satisfied."""
    raise NotImplementedError


def over_budget(ledger: Ledger) -> Optional[str]:
    """The reason nothing may start by itself, or None: the newest weekly reading is not stale and
    is at or above `graph.pause_above_weekly_pct`. With no reading the limit is not taken to be near."""
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


def machine_stopped(ledger: Ledger, record: dict) -> bool:
    """Does the backend say this run's machine exists and is not running? Asked only for an Incus
    or Docker machine (`Incus.info`, `Docker.state`); for any other kind, and when the question
    itself fails, the answer is no: unreachable is not the same as stopped."""
    raise NotImplementedError


def reviews(ledger: Ledger, log=lambda message: None) -> None:
    """The review of each finished run that something waits on.

    A run is due when it is its spec's `current_run`, final, in `done`, `partial` or `failed`,
    has no `review`, and a queued run that is neither held nor released has its spec in `after`.
    A `stopped` run is never reviewed: it is paused (`stopped`) with no session.

    Due: unless `over_budget` (a pause, `budget`), set `review` to state `running`, attempts 1,
    and the process `review.start` returns. Running: if `review.outcome` has an answer for this
    attempt, add its cost and either `apply_verdict` (kind `answer`), count a failed attempt
    (kind `invalid`), or pause with `review_failed` (kinds `error` and `timeout`); if there is
    none and `dispatch._gone(proc)`, count a failed attempt; if there is none and the review
    began more than `graph.review_timeout_min` plus one minute ago, end its process group and
    pause. A failed attempt starts the second attempt; a second failed attempt is a pause.
    """
    raise NotImplementedError


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
    raise NotImplementedError


def pause(ledger: Ledger, run_id: str, kind: str, reason: str) -> None:
    """Mark a run as the cause of a pause, once: a run that is already paused keeps its first
    reason. `hold` and `tell` do the rest on this or the next tick."""
    raise NotImplementedError


def hold(ledger: Ledger) -> None:
    """Make `held` on every queued run agree with the pauses in force.

    A pause is in force while its run has `paused` and is either queued itself or its spec's
    `current_run`. It reaches every queued run that is not released and whose `after` leads to
    the paused run's spec, directly or through other specs, by a path that does not pass through
    a spec whose current run is released. Such a run gets `held` (cause, the pause's reason,
    time) unless it has one; a run whose `held` names a cause that no longer reaches it loses it
    and waits as usual. A paused queued run holds itself.
    """
    raise NotImplementedError


def tell(ledger: Ledger) -> None:
    """Tell the person about each pause once: for every run with `paused` and no `alerted_at`,
    count the runs it holds, call `notify.pause`, and set `alerted_at`. Then, for each plan
    (`plans`) whose every spec's current run is final, with none held or paused, that has not
    been reported before, call `notify.finished` once with each run's outcome and the total spent."""
    raise NotImplementedError


def release(ledger: Ledger, run_id: str) -> str:
    """Lift the hold on a queued run and return a sentence saying what will happen.

    Under the placement lock: `held` and `paused` are removed and `released_at` is set, so the
    run starts at the next pump with a free slot, whether or not what it waited for is satisfied
    and whatever the weekly limit; what depends on it then waits for it as usual. GraphError,
    with a sentence that names what to do instead, for a run that is not queued or not held."""
    raise NotImplementedError


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
    raise NotImplementedError


def annotate(ledger: Ledger, record: dict, view: dict) -> dict:
    """Lay a plan's facts over a run's view (`status.run_view`) and return it. A view of a run
    with none of the fields this module keeps is returned as it came.

    Adds `after`, `held`, `paused` and `review` (without `proc`) as on the record, and appends to
    `warnings`: for a held run `held: <reason>. \\`org release <run>\\` starts it anyway`; for a
    paused run that holds something or has not ended `paused: <reason>. <n> run(s) held`; and
    `notify.failed`'s sentence when the alert for its pause could not be sent.
    """
    if not any(record.get(k) for k in ("after", "held", "paused", "review", "retry_of", "redispatch_of", "released_at")):
        return view
    raise NotImplementedError


def review_text(ledger: Ledger, run_id: str) -> str:
    """What `org review` adds for a run that was reviewed: the verdict as honoured, what the
    session answered when that differs, the reason, the notes, the attempts and the cost in
    dollars. "" for a run without a review."""
    if not ledger.run(run_id).get("review"):
        return ""
    raise NotImplementedError


def plans(ledger: Ledger) -> List[List[str]]:
    """Every plan: the approved specs joined by `after`, each plan in an order in which every
    spec comes after those it names, plans ordered by their first spec. [] without a plan."""
    raise NotImplementedError


def render(ledger: Ledger) -> str:
    """`org graph`: for each plan, its specs in order, each with its current run, that run's
    state, what it waits for or why it is held, and its review's verdict and reason."""
    if not has_plan(ledger):
        return "No plans: no spec that names another in `after` has been dispatched."
    raise NotImplementedError
