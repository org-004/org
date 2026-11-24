"""Sync: pull each run's state from its machine into the ledger's mirror.

One read-only command per run per sync (`org engineer snapshot`, running from the bundle already
on the machine). It returns status, plan, report and the next slice of the event log as one JSON
document. Nothing is written on the engineer's machine and the engineer's session is not touched,
which is the whole of "asking about a run without interrupting it".
"""
from __future__ import annotations

import fcntl
import json
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from . import budget
from .ledger import Ledger
from .runner import RUNNER_STATES, SNAPSHOT_FILES, TERMINAL_STATES
from .backends import BackendError, for_machine
from .transport import TransportError
from .util import now, read_json, write_text


def _count(value, default: int = 0) -> int:
    """A byte count from a snapshot: a whole number that is not negative, or the default."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default


def sync_run(ledger: Ledger, run_id: str, timeout: float = 45, slices: int = 6) -> dict:
    """Pull one run. If another process is already pulling it, return what the ledger has.

    `slices` bounds how much of the event log one call copies; a long log catches up over
    several syncs.
    """
    rec = ledger.run(run_id)
    if not rec or rec.get("state") in ("queued", "provisioning", "dispatch_failed") or rec.get("final") or rec.get("machine_removed_at"):
        return rec
    # The dashboard's sync loop and a `org status` in a terminal may overlap; the event log is
    # appended by offset, so two pulls of the same slice would duplicate it.
    with open(ledger.run_dir(run_id) / ".sync.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return rec
        return _sync_locked(ledger, run_id, ledger.run(run_id), timeout, slices)


def _sync_locked(ledger: Ledger, run_id: str, rec: dict, timeout: float, slices: int) -> dict:
    if rec.get("final"):
        return rec
    cfg = ledger.config()
    mirror = ledger.mirror_dir(run_id)
    snap, events = None, {}
    for _ in range(max(1, slices)):
        script = ('cd "%s" && PYTHONPATH=.org python3 -m org engineer snapshot . --events-from %d'
                  % (rec["remote_dir"], int(rec.get("events_offset") or 0)))
        try:
            out = for_machine(rec["machine"], cfg).run(script, timeout=timeout).stdout
            snap = json.loads(out.decode("utf-8", "replace"))
        except (TransportError, BackendError, ValueError, KeyError) as e:
            rec.update(reachable=False, last_error=str(e)[:400], last_attempt_at=now())
            ledger.save_run(run_id, rec)
            return rec
        if not isinstance(snap, dict):
            rec.update(reachable=False, last_error="the snapshot was not a JSON object", last_attempt_at=now())
            ledger.save_run(run_id, rec)
            return rec
        mirror.mkdir(parents=True, exist_ok=True)
        # The snapshot comes from the engineer's machine, where a session runs with every
        # permission bypassed, so it is input from outside. Only the files a runner writes are
        # taken, by their exact names: a name such as ../../config.json would otherwise be
        # written into the ledger, and config.json names modules that are imported.
        files = snap.get("files")
        for name in SNAPSHOT_FILES:
            text = files.get(name) if isinstance(files, dict) else None
            if isinstance(text, str):
                write_text(mirror / name, text)
        events = snap.get("events")
        events = events if isinstance(events, dict) else {}
        if isinstance(events.get("data"), str) and events["data"]:
            with open(mirror / "events.jsonl", "a", encoding="utf-8") as f:
                f.write(events["data"])
        # Saved after every slice: an interrupted sync must not fetch the same slice twice.
        rec["events_offset"] = _count(events.get("offset"), rec.get("events_offset") or 0)
        ledger.save_run(run_id, rec)
        if not events.get("data") or _count(events.get("offset")) >= _count(events.get("size")):
            break
    if isinstance(snap.get("log_tail"), str) and snap["log_tail"]:
        write_text(mirror / "runner.log.tail", snap["log_tail"])

    status = read_json(mirror / "status.json")
    status = status if isinstance(status, dict) else {}
    # A state the runner does not have is not taken from it: `queued` would hand the run to the
    # dispatcher again, and `provisioning` would hold a slot for ever.
    state = status.get("state") if status.get("state") in RUNNER_STATES else None
    events = {"offset": _count(events.get("offset"), rec.get("events_offset") or 0), "size": _count(events.get("size"))}
    remote_now = snap.get("now") if isinstance(snap.get("now"), (int, float)) else now()
    beat = status.get("heartbeat_at") if isinstance(status.get("heartbeat_at"), (int, float)) else remote_now
    rec.update(
        reachable=True,
        last_error=None,
        last_sync_at=now(),
        alive=bool(snap.get("alive")),
        host=str(snap.get("host") or "")[:200] or None,
        events_offset=events["offset"],
        # Measured against the machine's own clock, so skew between machines does not matter.
        heartbeat_age_s=max(0.0, remote_now - beat),
        clock_skew_s=round(now() - remote_now, 1),
        state=state or rec.get("state"),
        # Kept only as a whole number: an older bundle sends none, and this one is input from outside.
        oom_kill=_count(snap.get("oom_kill"), None),
    )
    if state in TERMINAL_STATES and events["offset"] >= events["size"]:
        rec["final"] = True  # finished and fully copied: no reason to ask again
    ledger.save_run(run_id, rec)
    try:
        budget.record_sample(ledger, status, run_id)
    except (AttributeError, TypeError, ValueError, KeyError):
        pass  # a status.json that is not shaped like one has no reading to keep
    return rec


def sync_all(ledger: Ledger, only: Optional[List[str]] = None, timeout: float = 45, slices: int = 6) -> List[dict]:
    """Sync every run that can still change, several machines at a time."""
    ids = only or [r for r in ledger.run_ids() if not ledger.run(r).get("final")]
    if not ids:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(ids))) as pool:
        return list(pool.map(lambda r: sync_run(ledger, r, timeout, slices), ids))
