"""The state of everything as one document, and its rendering for a terminal.

`collect()` is the single source for `org status`, the dashboard's API and the manager chatbot:
all three show the same numbers because all three read this.
"""
from __future__ import annotations

import re
import shutil
from typing import List, Optional

from . import budget, dispatch, graph, spec
from .ledger import Ledger
from .runner import TERMINAL_STATES
from .sync import sync_all
from .telemetry import MEASURED_KINDS
from .util import clip, fmt_clock, fmt_dur, fmt_pct, fmt_usd, now, read_json, read_text

STALE_AFTER_S = 180
MARK = {"done": "done ✓", "partial": "partial ◑", "failed": "failed ✗", "stopped": "stopped ■", "waiting": "waiting ⏸"}


def oom_line(v: dict) -> Optional[str]:
    """The sentence about processes killed for memory in a run's container, or None when there were none."""
    n = v.get("oom_kill")
    if not isinstance(n, int) or n <= 0:
        return None
    limit = " (limit %s)" % v["memory_limit"] if v.get("memory_limit") else ""
    return "%d process%s killed for memory in its container%s" % (n, " was" if n == 1 else "es were", limit)


def telemetry_view(rec: dict) -> dict:
    """What a run's view carries as `telemetry`: the record's summary (org/telemetry.py) with `measured` true, or,
    where there is none, {"measured": False, "why": a sentence}: machines of that kind are not
    measured, or the run has no measurements (it ended before org measured containers)."""
    t = rec.get("telemetry")
    if isinstance(t, dict) and isinstance(t.get("samples"), int) and t["samples"] > 0:
        return dict(t, measured=True)
    kind = (rec.get("machine") or {}).get("kind")
    if not kind:
        return {"measured": False, "why": "no machine was started, so nothing was measured"}
    if kind not in MEASURED_KINDS:
        return {"measured": False, "why": "%s machines are not measured" % kind}
    return {"measured": False, "why": "no measurement was taken: the run ended before org measured containers"}


def _mib(n) -> str:
    return "%d MiB" % (n // 1048576) if isinstance(n, (int, float)) and not isinstance(n, bool) else "not read"


def _num(n) -> bool:
    return isinstance(n, (int, float)) and not isinstance(n, bool)


def _gone(m: dict) -> str:
    """When and why the machine was deleted, as the end of a phrase, or '' while it is still there."""
    if not m.get("removed_at"):
        return ""
    return "deleted %s" % fmt_clock(m["removed_at"])


def _kills(n) -> str:
    return "not read" if not _num(n) else "%d process%s killed for memory" % (n, "" if n == 1 else "es")


def machine_block(v: dict) -> List[str]:
    """The lines of `org review` about what a run's machine measured, from the run's view: peak memory beside the
    limit, CPU seconds and the average number of cores they come to over the run's elapsed time,
    kills for memory, restarts, disk, and when the container was deleted. One line when there is
    nothing measured, saying why."""
    m, t = v.get("machine") or {}, v.get("telemetry") or {}
    if not m.get("removed_at"):
        end = "still there"
    elif m.get("removed_by") == "teardown":
        end = "deleted %s when the run ended" % fmt_clock(m["removed_at"])
    else:
        end = "deleted %s with `org machine rm`" % fmt_clock(m["removed_at"])
    lines = ["machine: %s %s · %s" % (m.get("kind") or "none", clip(m.get("name") or "", 40), end)]
    if not t.get("measured"):
        return lines + ["  measured  " + (t.get("why") or "no measurement was taken")]
    cpu, elapsed = t.get("cpu_s"), v.get("elapsed_s")
    cpu_text = "not read" if not _num(cpu) else "%d s" % round(cpu)
    # Not over less than a minute: the container's CPU time also counts its start, and over a
    # few seconds the quotient says more cores than the machine has.
    if _num(cpu) and _num(elapsed) and elapsed >= 60:
        cpu_text += ", %.2f cores on average over %s" % (cpu / elapsed, fmt_dur(elapsed))
    disk = t.get("disk_bytes")
    if not _num(disk):
        disk_text = "not reported by the storage pool"
    elif disk >= 1 << 30:
        disk_text = "%.1f GiB at the last measurement" % (disk / (1 << 30))
    else:
        disk_text = "%s at the last measurement" % _mib(disk)
    restarts = t.get("restarts")
    lines += [
        "  memory    peak %s of a %s limit" % (_mib(t.get("memory_peak_bytes")), _mib(t.get("memory_limit_bytes"))),
        "  cpu       " + cpu_text,
        "  kills     " + _kills(t.get("oom_kill")),
        "  restarts  " + (str(restarts) if _num(restarts) else "not read"),
        "  disk      " + disk_text,
        "  measured  %d times, %s to %s" % (t["samples"], fmt_clock(t.get("first_at")), fmt_clock(t.get("last_at"))),
    ]
    if t.get("note"):
        lines.append("  note      " + clip(t["note"], 200))
    return lines


def machine_line(v: dict) -> str:
    """The same in one line, for `org peek`."""
    m, t = v.get("machine") or {}, v.get("telemetry") or {}
    if not t.get("measured"):
        return t.get("why") or "no measurement was taken"
    limit = t.get("memory_limit_bytes")
    parts = ["peak %s of %s" % (_mib(t.get("memory_peak_bytes")), _mib(limit))]
    cpu = t.get("cpu_s")
    parts.append("cpu %s" % ("not read" if not _num(cpu) else "%d s" % round(cpu)))
    kills, restarts = t.get("oom_kill"), t.get("restarts")
    parts.append("%s killed for memory" % ("not read" if not _num(kills) else kills))
    parts.append("%s restart%s" % ("not read" if not _num(restarts) else restarts, "" if restarts == 1 else "s"))
    if m.get("removed_at"):
        parts.append(_gone(m))
    return " · ".join(parts)


def _text(x, n=200) -> Optional[str]:
    return x[:n] if isinstance(x, str) else None


def _strings(x, cap: int) -> List[str]:
    return [i[:200] for i in x if isinstance(i, str)][:cap] if isinstance(x, list) else []


def clean_leftover(raw) -> List[dict]:
    """The `leftover` list of a mirrored status.json, as far as it can be trusted: that file comes from
    the engineer's machine, so only dicts, only the known keys, only the known types and bounded sizes pass."""
    out = []
    for e in [e for e in (raw if isinstance(raw, list) else []) if isinstance(e, dict)][:20]:
        item = {"path": _text(e.get("path")) or "", "branch": _text(e.get("branch")) or "",
                "branches": _strings(e.get("branches"), 20), "commits": _strings(e.get("commits"), 20),
                "files": _strings(e.get("files"), 50)}
        for key in ("commits_total", "files_total"):
            n = e.get(key)
            item[key] = n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else len(item[key[:-6]])
        out.append(item)
    return out


_STILL_HAS = re.compile(r"(\S.*?) still has (\d+) uncommitted file\(s\) and (\d+) unpushed commit\(s\)")


def run_view(ledger: Ledger, run_id: str, rate: Optional[float]) -> dict:
    rec = ledger.run(run_id)
    st = read_json(ledger.mirror_dir(run_id) / "status.json") or {}
    state = st.get("state") or rec.get("state") or "dispatched"
    terminal = state in TERMINAL_STATES
    health = "ok"
    if rec.get("state") == "dispatch_failed":
        state, health, terminal = "dispatch_failed", "error", True
    elif rec.get("state") == "queued":
        state, terminal = "queued", False  # no machine yet, so nothing to be unhealthy
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
    removed = bool(rec.get("machine_removed_at"))
    warnings = list(st.get("warnings") or [])
    if removed:
        # The work the runner said was still there went with the machine, so the sentence must not offer it.
        warnings = [_STILL_HAS.sub(r"\1: \2 uncommitted file(s) and \3 unpushed commit(s) were deleted with the machine", w)
                    if isinstance(w, str) else w for w in warnings]
    failing = rec.get("machine_remove_error")
    if failing and not removed and isinstance(failing, dict):
        tries = failing.get("tries") or 0
        warnings.append("its machine could not be deleted (%d tr%s, last %s): %s" % (
            tries, "y" if tries == 1 else "ies", fmt_clock(failing.get("at")), clip(failing.get("error") or "", 300)))
    view = {
        "run_id": run_id,
        "spec_id": rec.get("spec_id"),
        "spec_version": rec.get("spec_version"),
        "title": st.get("title") or rec.get("title"),
        "state": state,
        "health": health,
        "terminal": terminal,
        "phase": st.get("phase"),
        "detail": st.get("detail") or rec.get("queue_reason") or rec.get("error") or rec.get("last_error"),
        "machine": {"kind": machine.get("kind"), "name": machine.get("name"), "node": machine.get("node"),
                    "removed": removed, "removed_at": rec.get("machine_removed_at"),
                    "removed_by": rec.get("machine_removed_by")},
        "attach": None if removed else rec.get("attach"),
        "telemetry": telemetry_view(rec),
        "leftover": clean_leftover(st.get("leftover")),
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
        "warnings": warnings,
        "oom_kill": rec.get("oom_kill"),
        "memory_limit": ((machine.get("limits") or {}).get("memory")),
        "retries": st.get("retries") or 0,
        "turns": st.get("turns") or 0,
        "caps": st.get("caps") or {},
        "last_sync_at": rec.get("last_sync_at"),
        "reachable": rec.get("reachable"),
        "has_plan": (ledger.mirror_dir(run_id) / "plan.md").exists(),
        "has_report": (ledger.mirror_dir(run_id) / "report.md").exists(),
    }
    # For a run of a plan: why it is held or what it paused, as sentences in `warnings` too, so
    # that `org status` and the dashboard draw them as they draw any other warning.
    return graph.annotate(ledger, rec, view)


def collect(ledger: Ledger, do_sync: bool = False) -> dict:
    if do_sync:
        # Someone is waiting on this. A machine that does not answer within a few seconds is
        # shown as it was last seen, marked unreachable; the long timeout and the bulk of the
        # event log are left to the dashboard's loop and to `org sync`.
        quick = float((ledger.config().get("status") or {}).get("sync_timeout_s") or 8)
        sync_all(ledger, timeout=quick, slices=1)
        dispatch.pump(ledger)  # a run that just finished may have freed a slot for a queued one
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
        errors, warnings = spec.check_draft(ledger, spec_id)
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
        "pool": dispatch.pool(ledger),
        "budget": {
            "weekly": week,
            "usd_per_pct": rate,
            "org_spend_this_window_usd": spent if window_start else None,
            "org_pct_this_window": (spent / rate["value"]) if (window_start and rate["value"]) else None,
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
    if b.get("org_spend_this_window_usd") is not None:
        head += " · engineers spent %s this window" % fmt_usd(b["org_spend_this_window_usd"])
        if b.get("org_pct_this_window") is not None:
            head += " (%.1f%%)" % b["org_pct_this_window"]
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
    lines = [budget_line(state["budget"])]
    if state.get("pool"):
        queued = len([v for v in state["runs"] if v["state"] == "queued"])
        several = len({p["kind"] for p in state["pool"]}) > 1
        lines.append("Pool: " + " · ".join("%s %d/%d" % ((p["kind"] + ":" if several else "") + p["node"], p["used"], p["slots"])
                                           for p in state["pool"])
                     + ((" · %d queued" % queued) if queued else ""))
    lines.append("")
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
            if oom_line(v):
                lines.append("  ! %s: %s" % (v["run_id"], oom_line(v)))
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


def _leftover_lines(v: dict) -> List[str]:
    """What the runner found on the machine at the end that was neither committed nor pushed, per clone."""
    left = v.get("leftover") or []
    if not left:
        return []
    gone = v["machine"].get("removed")
    lines = ["", "left on the machine when the run ended" + (" (deleted with the machine)" if gone else "") + ":"]
    for e in left:
        names = e["branches"] or ([e["branch"]] if e["branch"] else [])
        lines.append("  %s  branch %s" % (e["path"] or "?", ", ".join(names) or "?"))
        if e["commits"]:
            more = e["commits_total"] - len(e["commits"])
            lines += ["    commit  " + c for c in e["commits"]] + (["    … and %d more commit(s)" % more] if more > 0 else [])
        if e["files"]:
            more = e["files_total"] - len(e["files"])
            lines += ["    file    " + f for f in e["files"]] + (["    … and %d more file(s)" % more] if more > 0 else [])
    return lines


def scorecard(ledger: Ledger, run_id: str) -> str:
    """Plan against outcome for one run: the page a human reads to judge the engineer's planning."""
    mirror = ledger.mirror_dir(run_id)
    a = read_json(mirror / "actuals.json")
    result = read_json(mirror / "result.json") or {}
    view = run_view(ledger, run_id, None)
    block = ["", *machine_block(view), *_leftover_lines(view)]
    if not a:
        return "%s has not finished (no actuals.json in %s). Run `org sync` if it should have." % (run_id, mirror) + "\n" + "\n".join(block)

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
    return "\n".join(lines + block)
