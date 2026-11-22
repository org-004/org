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
from .runner import TERMINAL_STATES
from .transport import TransportError, for_machine
from .util import now, read_json, write_text


def sync_run(ledger: Ledger, run_id: str, timeout: float = 45, slices: int = 6) -> dict:
    """Pull one run. If another process is already pulling it, return what the ledger has.

    `slices` bounds how much of the event log one call copies; a long log catches up over
    several syncs.
    """
    rec = ledger.run(run_id)
    if not rec or rec.get("state") in ("provisioning", "dispatch_failed") or rec.get("final") or rec.get("machine_removed_at"):
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
        except (TransportError, ValueError, KeyError) as e:
            rec.update(reachable=False, last_error=str(e)[:400], last_attempt_at=now())
            ledger.save_run(run_id, rec)
            return rec
        mirror.mkdir(parents=True, exist_ok=True)
        for name, text in (snap.get("files") or {}).items():
            if text is not None:
                write_text(mirror / name, text)
        events = snap.get("events") or {}
        if events.get("data"):
            with open(mirror / "events.jsonl", "a", encoding="utf-8") as f:
                f.write(events["data"])
        # Saved after every slice: an interrupted sync must not fetch the same slice twice.
        rec["events_offset"] = events.get("offset", rec.get("events_offset", 0))
        ledger.save_run(run_id, rec)
        if not events.get("data") or events.get("offset", 0) >= events.get("size", 0):
            break
    if snap.get("log_tail"):
        write_text(mirror / "runner.log.tail", snap["log_tail"])

    status = read_json(mirror / "status.json") or {}
    remote_now = snap.get("now") or now()
    rec.update(
        reachable=True,
        last_error=None,
        last_sync_at=now(),
        alive=bool(snap.get("alive")),
        host=snap.get("host"),
        events_offset=events.get("offset", rec.get("events_offset", 0)),
        # Measured against the machine's own clock, so skew between machines does not matter.
        heartbeat_age_s=max(0.0, remote_now - (status.get("heartbeat_at") or remote_now)),
        clock_skew_s=round(now() - remote_now, 1),
        state=status.get("state") or rec.get("state"),
    )
    if status.get("state") in TERMINAL_STATES and events.get("offset", 0) >= events.get("size", 0):
        rec["final"] = True  # finished and fully copied: no reason to ask again
    ledger.save_run(run_id, rec)
    budget.record_sample(ledger, status, run_id)
    return rec


def sync_all(ledger: Ledger, only: Optional[List[str]] = None, timeout: float = 45, slices: int = 6) -> List[dict]:
    """Sync every run that can still change, several machines at a time."""
    ids = only or [r for r in ledger.run_ids() if not ledger.run(r).get("final")]
    if not ids:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(ids))) as pool:
        return list(pool.map(lambda r: sync_run(ledger, r, timeout, slices), ids))
