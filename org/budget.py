"""The weekly budget: what the plan's limit says now, and what a dollar of work costs against it.

Two different numbers, kept apart on purpose:

  utilization   The account's weekly limit as the CLI reports it (0..1), from the rate_limit_event
                at the start of every engineer turn. Exact, account-wide, and shared with
                everything else the account does.
  dollars       What a run cost at list prices, from the CLI's own cost figure. Exact per run.

Turning a run's dollars into "percent of the weekly budget" needs the exchange rate between them,
which the plan does not publish. It is learned: a finished run saw the utilization move by some
amount while it spent a known number of dollars. Anything else running on the account during the
run inflates that movement, so each run gives a lower bound on dollars-per-percent and the largest
bound seen is the best estimate. A value in config.json (budget.usd_per_weekly_pct) overrides it.
"""
from __future__ import annotations

import threading
from statistics import median
from typing import List, Optional

from .ledger import Ledger
from .util import now, read_json, write_json

MIN_MOVE = 0.02  # ignore runs that moved the meter by less than two points: rounding dominates


_book_lock = threading.Lock()


def record_sample(ledger: Ledger, status: dict, run_id: str) -> None:
    """Keep the newest reading of every weekly window seen in any synced status."""
    rl = status.get("rate_limit") or {}
    wins = {n: w for n, w in (rl.get("windows") or {}).items() if w and w.get("utilization") is not None}
    if not wins:
        return
    with _book_lock:
        _record(ledger, rl, wins, run_id)


def _sample(rl: dict, w: dict, run_id: str) -> dict:
    return {"at": rl.get("at") or now(), "utilization": w["utilization"], "resets_at": w.get("resets_at"),
            "status": w.get("status"), "run_id": run_id}


def _record(ledger: Ledger, rl: dict, wins: dict, run_id: str) -> None:
    book = read_json(ledger.budget_path, {}) or {}
    held = book.setdefault("windows", {})
    for name, w in wins.items():
        sample = _sample(rl, w, run_id)
        if sample["at"] >= (held.get(name) or {}).get("at", 0):
            held[name] = sample
    week = wins.get("seven_day")
    if week:
        sample = _sample(rl, week, run_id)
        latest = book.get("weekly") or {}
        if sample["at"] >= (latest.get("at") or 0):
            book["weekly"] = sample
        samples = book.setdefault("samples", [])
        if not samples or samples[-1].get("utilization") != sample["utilization"] or samples[-1].get("resets_at") != sample["resets_at"]:
            samples.append(sample)
            del samples[:-500]
    write_json(ledger.budget_path, book)


def weekly(ledger: Ledger) -> Optional[dict]:
    """The newest reading, marked stale once its window has reset."""
    week = (read_json(ledger.budget_path, {}) or {}).get("weekly")
    if not week:
        return None
    week = dict(week)
    week["stale"] = bool(week.get("resets_at") and week["resets_at"] < now())
    return week


def windows(ledger: Ledger) -> dict:
    """Every weekly window the newest `rate_limit_event`s carried, by the name it came with (R25 of
    spec 016): {name: {"at", "utilization", "resets_at", "status", "run_id", "stale"}}. Nothing is
    guessed about which model a window belongs to. `weekly` stays the `seven_day` window."""
    book = read_json(ledger.budget_path, {}) or {}
    out = {}
    for name, w in (book.get("windows") or {}).items():
        w = dict(w)
        w["stale"] = bool(w.get("resets_at") and w["resets_at"] < now())
        out[name] = w
    week = weekly(ledger)
    if week and "seven_day" not in out:
        out["seven_day"] = week
    return out


def finished_actuals(ledger: Ledger) -> List[dict]:
    out = []
    for run_id in ledger.run_ids():
        a = read_json(ledger.mirror_dir(run_id) / "actuals.json")
        r = read_json(ledger.mirror_dir(run_id) / "result.json")
        if a and r and a.get("finished_at"):
            a = dict(a)
            a["status"] = r.get("status")
            out.append(a)
    out.sort(key=lambda a: a.get("finished_at") or 0)
    return out


def usd_per_pct(ledger: Ledger) -> dict:
    """{"value": dollars per 1% of the weekly limit or None, "source": config|calibrated|unknown, "runs": n}"""
    configured = (ledger.config().get("budget") or {}).get("usd_per_weekly_pct")
    if configured:
        return {"value": float(configured), "source": "config", "runs": 0}
    bounds = []
    for a in finished_actuals(ledger):
        w = a.get("weekly") or {}
        start, end = w.get("utilization_start"), w.get("utilization_end")
        cost = (a.get("total") or {}).get("cost_usd") or 0
        if start is None or end is None or cost <= 0:
            continue
        moved = end - start
        if moved >= MIN_MOVE:
            bounds.append(cost / (moved * 100.0))
    if not bounds:
        return {"value": None, "source": "unknown", "runs": 0}
    return {"value": max(bounds), "source": "calibrated", "runs": len(bounds)}


def history(ledger: Ledger) -> dict:
    """How past estimates turned out, for the next engineer's planning prompt."""
    runs, time_ratios, cost_ratios = [], [], []
    for a in finished_actuals(ledger):
        ap = a.get("after_planning") or {}
        row = {
            "run_id": a.get("run_id"),
            "title": a.get("title"),
            "status": a.get("status"),
            "est_minutes": ap.get("est_minutes"),
            "actual_minutes": ap.get("actual_minutes"),
            "est_cost_usd": ap.get("est_cost_usd"),
            "actual_cost_usd": ap.get("actual_cost_usd"),
        }
        runs.append(row)
        if row["est_minutes"] and row["actual_minutes"]:
            time_ratios.append(row["actual_minutes"] / row["est_minutes"])
        if row["est_cost_usd"] and row["actual_cost_usd"]:
            cost_ratios.append(row["actual_cost_usd"] / row["est_cost_usd"])
    return {
        "runs": runs[-12:],
        "time_ratio": round(median(time_ratios), 2) if time_ratios else None,
        "cost_ratio": round(median(cost_ratios), 2) if cost_ratios else None,
    }
