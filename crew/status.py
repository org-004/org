"""The state of everything as one document, and its rendering for a terminal.

`collect()` is the single source for `crew status`, the dashboard's API and the manager chatbot:
all three show the same numbers because all three read this.
"""
from __future__ import annotations

import shutil
from typing import List, Optional

from . import budget, spec
from .ledger import Ledger
from .runner import TERMINAL_STATES
from .sync import sync_all
from .util import clip, fmt_clock, fmt_dur, fmt_pct, fmt_usd, now, read_json, read_text

STALE_AFTER_S = 180
MARK = {"done": "done ✓", "partial": "partial ◑", "failed": "failed ✗", "stopped": "stopped ■", "waiting": "waiting ⏸"}


def run_view(ledger: Ledger, run_id: str, rate: Optional[float]) -> dict:
    rec = ledger.run(run_id)
    st = read_json(ledger.mirror_dir(run_id) / "status.json") or {}
    state = st.get("state") or rec.get("state") or "dispatched"
    terminal = state in TERMINAL_STATES
    health = "ok"
    if rec.get("state") == "dispatch_failed":
        state, health, terminal = "dispatch_failed", "error", True
    elif not terminal:
        if rec.get("reachable") is False:
            health = "unreachable"
        elif st and rec.get("alive") is False:
            health = "dead"  # the runner process is gone and the run never finished
        elif (rec.get("heartbeat_age_s") or 0) > STALE_AFTER_S:
            health = "stalled"

    t = now()
    elapsed = st.get("elapsed_s")
    if elapsed is None:
        elapsed = (t - rec["dispatched_at"]) if rec.get("dispatched_at") and not terminal else None
    elif not terminal and health == "ok" and rec.get("last_sync_at"):
        elapsed += t - rec["last_sync_at"]  # the status file is as old as the last pull
    cost = st.get("cost") or {}
    forecast = st.get("forecast") or {}
    skew = rec.get("clock_skew_s") or 0.0
    usd = cost.get("usd")
    machine = rec.get("machine") or {}
    activity = st.get("activity") or {}
    return {
        "run_id": run_id,
        "spec_id": rec.get("spec_id"),
        "spec_version": rec.get("spec_version"),
        "title": st.get("title") or rec.get("title"),
        "state": state,
        "health": health,
        "terminal": terminal,
        "phase": st.get("phase"),
        "detail": st.get("detail") or rec.get("error") or rec.get("last_error"),
        "machine": {"kind": machine.get("kind"), "name": machine.get("name"), "removed": bool(rec.get("machine_removed_at"))},
        "attach": rec.get("attach"),
        "model": st.get("model"),
        "started_at": (st.get("started_at") + skew) if st.get("started_at") else rec.get("dispatched_at"),
        "finished_at": (st.get("finished_at") + skew) if st.get("finished_at") else None,
        "elapsed_s": elapsed,
        "eta_at": (forecast.get("eta_at") + skew) if forecast.get("eta_at") and not terminal else None,
        "deadline_at": (st.get("deadline_at") + skew) if st.get("deadline_at") else None,
        "waiting_until": (st.get("waiting_until") + skew) if st.get("waiting_until") else None,
        "cost_usd": usd,
        "cost_cap_usd": cost.get("cap_usd"),
        "cost_by_model": cost.get("by_model") or {},
        "weekly_pct": (usd / rate) if (usd is not None and rate) else None,
        "estimate": st.get("estimate"),
        # The plan's cost as a share of the weekly limit: what the human budgets against.
        "estimate_weekly_pct": ((st.get("estimate") or {}).get("cost_usd") / rate)
        if (rate and (st.get("estimate") or {}).get("cost_usd") is not None) else None,
        "forecast_cost_usd": forecast.get("cost_usd") if not terminal else None,
        "forecast_basis": forecast.get("basis"),
        "progress": st.get("progress") or {"done": 0, "total": 0, "milestones": []},
        "activity": activity.get("text"),
        "activity_at": (activity.get("at") + skew) if activity.get("at") else None,
        "agents_running": st.get("agents_running") or 0,
        "agents": st.get("agents") or [],
        "result": st.get("result"),
        "end_reason": st.get("end_reason"),
        "warnings": st.get("warnings") or [],
        "retries": st.get("retries") or 0,
        "turns": st.get("turns") or 0,
        "caps": st.get("caps") or {},
        "last_sync_at": rec.get("last_sync_at"),
        "reachable": rec.get("reachable"),
        "has_plan": (ledger.mirror_dir(run_id) / "plan.md").exists(),
        "has_report": (ledger.mirror_dir(run_id) / "report.md").exists(),
    }


def collect(ledger: Ledger, do_sync: bool = False) -> dict:
    if do_sync:
        # Someone is waiting on this. A machine that does not answer within a few seconds is
        # shown as it was last seen, marked unreachable; the long timeout and the bulk of the
        # event log are left to the dashboard's loop and to `crew sync`.
        quick = float((ledger.config().get("status") or {}).get("sync_timeout_s") or 8)
        sync_all(ledger, timeout=quick, slices=1)
    rate = budget.usd_per_pct(ledger)
    week = budget.weekly(ledger)
    runs = [run_view(ledger, r, rate["value"]) for r in ledger.run_ids()]
    runs.sort(key=lambda v: (v["terminal"], -(v["finished_at"] or v["started_at"] or 0)))

    window_start = (week["resets_at"] - 7 * 86400) if week and week.get("resets_at") and not week.get("stale") else None
    spent = sum(v["cost_usd"] or 0 for v in runs if window_start and (v["started_at"] or 0) >= window_start)

    drafts = []
    for spec_id in ledger.draft_ids():
        state = spec.state_of(ledger, spec_id)
        if state == "approved":
            continue  # the draft is identical to the approved text: nothing pending
        text = read_text(ledger.draft_path(spec_id)) or ""
        errors, warnings = spec.check(text, spec.default_machine(ledger))
        meta, _ = spec.parse(text)
        drafts.append({
            "id": spec_id,
            "title": meta.get("title"),
            "state": state,
            "sha": spec.draft_sha(ledger, spec_id),
            "errors": errors,
            "warnings": warnings,
            "modified_at": ledger.draft_path(spec_id).stat().st_mtime,
        })
    specs = []
    for spec_id in ledger.approved_ids():
        meta = ledger.spec_meta(spec_id) or {}
        specs.append({"id": spec_id, "title": meta.get("title"), "version": meta.get("version"), "sha": meta.get("sha256"),
                      "approved_at": meta.get("approved_at"), "runs": ledger.runs_for(spec_id)})
    return {
        "now": now(),
        "home": str(ledger.root),
        "budget": {
            "weekly": week,
            "usd_per_pct": rate,
            "crew_spend_this_window_usd": spent if window_start else None,
            "crew_pct_this_window": (spent / rate["value"]) if (window_start and rate["value"]) else None,
        },
        "runs": runs,
        "drafts": drafts,
        "specs": specs,
    }


# ---------------------------------------------------------------- terminal rendering


def state_label(v: dict) -> str:
    label = MARK.get(v["state"], v["state"])
    if v["health"] not in ("ok", "error"):
        label += " (%s)" % v["health"]
    return label


def budget_line(b: dict) -> str:
    week, rate = b.get("weekly"), b.get("usd_per_pct") or {}
    if not week:
        head = "Weekly limit: not seen yet (the first engineer turn reports it)"
    elif week.get("stale"):
        head = "Weekly limit: reset %s; no reading since" % fmt_clock(week.get("resets_at"))
    else:
        head = "Weekly limit: %s used, resets %s (in %s)" % (
            fmt_pct(week.get("utilization")), fmt_clock(week.get("resets_at")), fmt_dur((week.get("resets_at") or now()) - now()))
    if rate.get("value"):
        src = "config" if rate["source"] == "config" else "learned from %d run(s)" % rate["runs"]
        head += " · 1%% ≈ %s (%s)" % (fmt_usd(rate["value"]), src)
    else:
        head += " · dollars per 1% not known yet"
    if b.get("crew_spend_this_window_usd") is not None:
        head += " · crew spent %s this window" % fmt_usd(b["crew_spend_this_window_usd"])
        if b.get("crew_pct_this_window") is not None:
            head += " (%.1f%%)" % b["crew_pct_this_window"]
    return head


def _table(rows: List[List[str]], width: int) -> List[str]:
    if not rows:
        return []
    cols = len(rows[0])
    widths = [max(len(r[i]) for r in rows) for i in range(cols)]
    fixed = sum(widths[:-1]) + 2 * (cols - 1)
    widths[-1] = max(12, min(widths[-1], width - fixed))
    out = []
    for r in rows:
        cells = [r[i].ljust(widths[i]) for i in range(cols - 1)] + [clip(r[-1], widths[-1])]
        out.append("  ".join(cells).rstrip())
    return out


def render(state: dict, width: Optional[int] = None) -> str:
    width = width or shutil.get_terminal_size((140, 40)).columns
    lines = [budget_line(state["budget"]), ""]
    runs = state["runs"]
    if runs:
        rows = [["RUN", "STATE", "DONE", "ELAPSED", "ETA", "SPEND", "PLAN", "CAP", "MACHINE", "NOW"]]
        for v in runs:
            p = v["progress"]
            spend = fmt_usd(v["cost_usd"])
            if v["weekly_pct"] is not None:
                spend += " · %.1f%%" % v["weekly_pct"]
            est = v.get("estimate") or {}
            plan = "%s %s" % (fmt_dur(est["wall_minutes"] * 60), fmt_usd(est.get("cost_usd"))) if est.get("wall_minutes") else "—"
            if v.get("estimate_weekly_pct") is not None and plan != "—":
                plan += " · %.1f%%" % v["estimate_weekly_pct"]
            if v["terminal"]:
                what = clip((v.get("result") or {}).get("msg") or v.get("detail") or "", 200)
                eta = "—"
            else:
                what = v.get("activity") or v.get("detail") or ""
                if v["agents_running"]:
                    what = "[%d agents] %s" % (v["agents_running"], what)
                if v["state"] == "waiting" and v.get("waiting_until"):
                    eta = "resumes " + fmt_clock(v["waiting_until"])
                else:
                    eta = fmt_clock(v["eta_at"]) if v["eta_at"] else "—"
            rows.append([v["run_id"], state_label(v), "%d/%d" % (p.get("done", 0), p.get("total", 0)) if p.get("total") else "—",
                         fmt_dur(v["elapsed_s"]), eta, spend, plan, fmt_usd(v["cost_cap_usd"]) if v["cost_cap_usd"] else "—",
                         clip(v["machine"]["name"] or "", 22), what])
        lines += _table(rows, width)
        for v in runs:
            for w in v["warnings"]:
                lines.append("  ! %s: %s" % (v["run_id"], clip(w, width - 8)))
    else:
        lines.append("No runs yet.")
    if state["drafts"]:
        lines += ["", "DRAFTS"]
        rows = []
        for d in state["drafts"]:
            ok = "ready to approve" if not d["errors"] else "%d check(s) failing" % len(d["errors"])
            rows.append([d["id"], d["state"], "sha " + (d["sha"] or "")[:12], ok, clip(d.get("title") or "", 60)])
        lines += _table(rows, width)
    return "\n".join(lines)


def scorecard(ledger: Ledger, run_id: str) -> str:
    """Plan against outcome for one run: the page a human reads to judge the engineer's planning."""
    mirror = ledger.mirror_dir(run_id)
    a = read_json(mirror / "actuals.json")
    result = read_json(mirror / "result.json") or {}
    if not a:
        return "%s has not finished (no actuals.json in %s). Run `crew sync` if it should have." % (run_id, mirror)

    def ratio(actual, est):
        return "%.2fx" % (actual / est) if actual and est else "—"

    ap, pl, tot = a.get("after_planning") or {}, a.get("planning") or {}, a.get("total") or {}
    lines = [
        "%s — %s" % (run_id, result.get("status", "?")),
        clip(result.get("msg") or "", 300),
        "",
    ]
    rows = [["", "ESTIMATE", "ACTUAL", "RATIO"]]
    rows.append(["time after planning", fmt_dur((ap.get("est_minutes") or 0) * 60) if ap.get("est_minutes") else "—",
                 fmt_dur((ap.get("actual_minutes") or 0) * 60), ratio(ap.get("actual_minutes"), ap.get("est_minutes"))])
    rows.append(["cost after planning", fmt_usd(ap.get("est_cost_usd")), fmt_usd(ap.get("actual_cost_usd")),
                 ratio(ap.get("actual_cost_usd"), ap.get("est_cost_usd"))])
    rows.append(["planning", "—", "%s, %s" % (fmt_dur((pl.get("minutes") or 0) * 60), fmt_usd(pl.get("cost_usd"))), "—"])
    for m in a.get("milestones") or []:
        rows.append([clip("%s %s" % (m.get("id"), m.get("title")), 44),
                     "%s %s" % (fmt_dur((m.get("est_minutes") or 0) * 60), fmt_usd(m.get("est_cost_usd"))),
                     "%s %s" % (fmt_dur((m.get("actual_minutes") or 0) * 60), fmt_usd(m.get("actual_cost_usd"))) if m.get("actual_minutes") is not None else "not run",
                     "%s / %s  %s" % (ratio(m.get("actual_minutes"), m.get("est_minutes")),
                                      ratio(m.get("actual_cost_usd"), m.get("est_cost_usd")), m.get("outcome") or "")])
    rep = a.get("report")
    if rep:
        rows.append(["verify and report", "in the totals", "%s %s" % (fmt_dur((rep.get("minutes") or 0) * 60), fmt_usd(rep.get("cost_usd"))), "—"])
    rows.append(["total", "", "%s, %s" % (fmt_dur((tot.get("elapsed_minutes") or 0) * 60), fmt_usd(tot.get("cost_usd"))), ""])
    lines += _table(rows, 200)
    lines += ["", "turns %s · retries %s · usage-limit waits %s · plan amendments %s · subagents %s" % (
        a.get("turns"), a.get("retries"), len(a.get("waits") or []), a.get("amendments"), a.get("subagents_spawned"))]
    by_model = tot.get("by_model") or {}
    if by_model:
        lines.append("by model: " + ", ".join("%s %s" % (k.replace("claude-", ""), fmt_usd(v.get("usd"))) for k, v in sorted(by_model.items(), key=lambda kv: -kv[1].get("usd", 0))))
    acc = result.get("acceptance") or []
    if acc:
        lines.append("acceptance: " + ", ".join("%s %s" % (x.get("id"), x.get("verdict")) for x in acc))
    if a.get("end_reason"):
        lines.append("ended because: " + a["end_reason"])
    lines += ["", "plan as frozen: %s" % (mirror / "plan.frozen.md"), "report:         %s" % (mirror / "report.md")]
    return "\n".join(lines)
