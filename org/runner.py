"""The engineer's runner: one run, start to finish, on the engineer's machine.

The engineer is a headless Claude Code session. The runner is the deterministic part around it:

    plan        one turn; ends with a validated plan and plan.md, which are then frozen
    execute     one turn per milestone, in order; each ends with a validated outcome
    report      one turn; ends with report.md and the final <status, msg>

Every turn resumes the same session, so the engineer keeps its context, and every turn ends with
structured output, so each transition is decided from data. Between turns the runner records the
CLI's own cost figure, re-forecasts the finish time and cost, and enforces the spec's caps. While
a turn runs it keeps status.json fresh: that file is all the manager's side ever reads, so the
human can watch without touching the engineer.

Nothing here talks to the manager's machine. The manager pulls.
"""
from __future__ import annotations

import fcntl
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional

from . import pricing, schemas
from .stream import Meter, weekly_window
from .util import clip, fmt_dur, fmt_usd, now, read_json, read_text, render, sha256_file, write_json, write_text

PROMPTS = Path(__file__).parent / "prompts"
TERMINAL_STATES = ("done", "partial", "failed", "stopped")
STATE_FOR_STATUS = {"done": "done", "partial": "partial", "fail": "failed"}
SNAPSHOT_FILES = (
    "status.json",
    "plan.md",
    "plan.json",
    "plan.frozen.md",
    "plan.frozen.json",
    "report.md",
    "result.json",
    "actuals.json",
)
LIMIT_TEXT_RE = re.compile(r"hit your .{0,40}limit|usage limit|limit reached|rate.?limit", re.I)

NUDGE = """The previous turn ended without the structured output this turn requires ({{why}}).

Continue the same task from where it stands; do not start it over. The instruction was:

{{original}}"""

RECOVER = """Your earlier session could not be resumed, so this is a fresh one. Nothing on disk was lost.
Before doing anything else, read `{{run_dir}}/plan.md` and `{{run_dir}}/status.json` and look at the
state of the repositories under `{{workspace}}` to see what is already done. Then:

{{original}}"""

REPAIR_PLAN = """The plan cannot be accepted as returned:

{{problems}}

Fix `{{run_dir}}/plan.md` and return the corrected structured output. If the work cannot fit the
caps, either cut SHOULD requirements and list them in `dropped_requirements`, or return the verdict
`infeasible` with the reason."""


class Stop(Exception):
    """The human asked for the run to stop."""


class TurnFailed(Exception):
    """A turn could not be completed after its retries."""


class CapReached(Exception):
    """A cost or time cap from the spec was hit."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


class Runner:
    def __init__(self, run_dir):
        self.dir = Path(run_dir).resolve()
        self.job = read_json(self.dir / "job.json")
        if not self.job:
            raise SystemExit("no job.json in %s" % self.dir)
        self.cfg = self.job.get("engineer") or {}
        self.caps = self.job.get("caps") or {}
        self.prices = pricing.table(self.job.get("pricing"))
        self.workspace = Path(self.job.get("workspace") or (Path.home() / "work" / self.job["run_id"])).expanduser()
        self.state = read_json(self.dir / "state.json") or self._new_state()
        self.meter = Meter(self.prices, self.state.get("confirmed_usd", 0.0), self.state.get("by_model") or {},
                           self.state.get("cumulative") or {})
        self.stop_requested = False
        self.proc: Optional[subprocess.Popen] = None
        self._killed = False
        self._last_said = None
        self._events = None
        self._log = None
        self._lock = None

    # ------------------------------------------------------------ state and status
    def _new_state(self) -> dict:
        return {
            "phase": "planning",  # planning | executing | reporting | finished
            "state": "planning",  # what the dashboard shows
            "detail": "starting",
            "started_at": now(),
            "plan_done_at": None,
            "exec_started_at": None,
            "exec_ended_at": None,
            "finished_at": None,
            "session_id": str(uuid.uuid4()),
            "session_started": False,
            "turns": [],
            "milestones": [],
            "amendments": [],
            "plan": None,
            "estimate": None,
            "verdict": None,
            "end_reason": None,
            "confirmed_usd": 0.0,
            "estimated_usd": 0.0,
            "by_model": {},
            "plan_cost_usd": None,
            "retries": 0,
            "waits": [],
            "warnings": [],
            "util_start": None,
            "waiting_until": None,
            "result": None,
        }

    def save_state(self) -> None:
        self.state["confirmed_usd"] = self.meter.confirmed_usd
        self.state["by_model"] = self.meter.by_model
        self.state["cumulative"] = self.meter.cumulative
        write_json(self.dir / "state.json", self.state)

    def cost_cap(self) -> Optional[float]:
        """The run's dollar cap: the spec's, and its weekly-percent cap when that can be priced."""
        limits = []
        if self.caps.get("max_cost_usd"):
            limits.append(float(self.caps["max_cost_usd"]))
        per_pct = (self.job.get("budget") or {}).get("usd_per_weekly_pct")
        if self.caps.get("max_weekly_pct") and per_pct:
            limits.append(float(self.caps["max_weekly_pct"]) * float(per_pct))
        return min(limits) if limits else None

    def deadline(self) -> Optional[float]:
        if self.caps.get("max_hours"):
            return self.state["started_at"] + float(self.caps["max_hours"]) * 3600.0
        return None

    def forecast(self) -> Optional[dict]:
        """Projected finish time and total cost, re-derived from the pace so far."""
        st, est = self.state, self.state.get("estimate")
        ms = st.get("milestones") or []
        if not est or st["phase"] in ("planning", "finished") or not ms:
            return None
        t = now()
        done = [m for m in ms if m.get("ended_at")]
        todo = [m for m in ms if not m.get("ended_at")]
        est_min = sum(m.get("est_minutes") or 0 for m in done)
        act_min = sum((m["ended_at"] - m["started_at"]) / 60.0 for m in done)
        est_usd = sum(m.get("est_cost_usd") or 0 for m in done)
        act_usd = sum(m.get("cost_usd") or 0 for m in done)
        pace = min(5.0, max(0.4, act_min / est_min)) if est_min > 0 else 1.0
        cost_pace = min(5.0, max(0.4, act_usd / est_usd)) if est_usd > 0 else 1.0
        spent = self.meter.total_usd()
        left_min = left_usd = 0.0
        for m in todo:
            m_min = (m.get("est_minutes") or 0) * pace
            m_usd = (m.get("est_cost_usd") or 0) * cost_pace
            if m.get("started_at"):
                m_min = max(m_min - (t - m["started_at"]) / 60.0, 0.1 * (m.get("est_minutes") or 0))
                m_usd = max(m_usd - (spent - (m.get("cost_start") or 0)), 0.0)
            left_min += m_min
            left_usd += m_usd
        # Whatever the plan's totals hold beyond its milestones is the report and other overhead.
        planned_min = sum(m.get("est_minutes") or 0 for m in ms)
        planned_usd = sum(m.get("est_cost_usd") or 0 for m in ms)
        if st["phase"] != "reporting":
            left_min += max((est.get("wall_minutes") or 0) - planned_min, 2.0)
            left_usd += max((est.get("cost_usd") or 0) - planned_usd, 0.0)
        else:
            left_min += 1.0  # the report is being written: "in about a minute", not a time already past
        return {
            "eta_at": t + left_min * 60.0,
            "cost_usd": spent + left_usd,
            "pace": round(pace, 2),
            "cost_pace": round(cost_pace, 2),
            "basis": "pace of %d finished milestone(s)" % len(done) if done else "plan",
        }

    def status(self) -> dict:
        st, m, t = self.state, self.meter, now()
        ms = st.get("milestones") or []
        current = next((x for x in ms if x.get("started_at") and not x.get("ended_at")), None)
        end = st.get("finished_at") or t
        return {
            "run_id": self.job["run_id"],
            "spec_id": self.job.get("spec_id"),
            "spec_sha": self.job.get("spec_sha"),
            "title": self.job.get("title"),
            "state": st["state"],
            "phase": st["phase"],
            "detail": st.get("detail"),
            "started_at": st["started_at"],
            "plan_done_at": st.get("plan_done_at"),
            "finished_at": st.get("finished_at"),
            "elapsed_s": end - st["started_at"],
            "heartbeat_at": t,
            "pid": os.getpid(),
            "session_id": st.get("session_id"),
            "model": self.job.get("model"),
            "effort": self.job.get("effort"),
            "cost": {
                "usd": m.total_usd(),
                "confirmed_usd": m.confirmed_usd,
                "live_usd": m.live_usd(),
                "estimated_usd": st.get("estimated_usd", 0.0),
                "cap_usd": self.cost_cap(),
                "by_model": m.by_model,
            },
            "estimate": st.get("estimate"),
            "forecast": self.forecast(),
            "progress": {
                "done": len([x for x in ms if x.get("ended_at")]),
                "total": len(ms),
                "current": (current or {}).get("title"),
                "milestones": [
                    {
                        "id": x.get("id"),
                        "title": x.get("title"),
                        "est_minutes": x.get("est_minutes"),
                        "est_cost_usd": x.get("est_cost_usd"),
                        "started_at": x.get("started_at"),
                        "ended_at": x.get("ended_at"),
                        "outcome": x.get("outcome"),
                        "cost_usd": x.get("cost_usd"),
                    }
                    for x in ms
                ],
            },
            "activity": m.activity,
            "agents_running": len(m.agents),
            "agents": list(m.agents.values())[:8],
            "agents_spawned": m.agents_spawned,
            "rate_limit": m.rate_limit,
            "waiting_until": st.get("waiting_until"),
            "caps": self.caps,
            "deadline_at": self.deadline(),
            "result": st.get("result"),
            "end_reason": st.get("end_reason"),
            "warnings": st.get("warnings") or [],
            "turns": len(st.get("turns") or []),
            "retries": st.get("retries", 0),
        }

    def write_status(self) -> None:
        write_json(self.dir / "status.json", self.status())

    def set(self, state: Optional[str] = None, detail: Optional[str] = None, phase: Optional[str] = None) -> None:
        if phase:
            self.state["phase"] = phase
        if state:
            self.state["state"] = state
        if detail is not None:
            self.state["detail"] = detail
        self.save_state()
        self.write_status()

    def say(self, text: str) -> None:
        line = "%s  %s" % (time.strftime("%H:%M:%S"), text)
        print(line, flush=True)
        if self._log:
            self._log.write(line + "\n")
            self._log.flush()

    def warn(self, text: str) -> None:
        if text not in self.state["warnings"]:
            self.state["warnings"].append(text)
        self.say("warning: " + text)

    # ------------------------------------------------------------ process control
    def _on_signal(self, signum, frame) -> None:
        self.stop_requested = True

    def _stop_now(self) -> bool:
        if not self.stop_requested and (self.dir / "STOP").exists():
            self.stop_requested = True
        return self.stop_requested

    def _kill(self, proc: subprocess.Popen) -> None:
        if self._killed:
            return
        self._killed = True
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

        def hard():
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

        timer = threading.Timer(20.0, hard)
        timer.daemon = True
        timer.start()

    def _sleep(self, seconds: float) -> None:
        """Sleep in short steps, keeping the heartbeat and honouring a stop request."""
        end = now() + seconds
        while now() < end:
            if self._stop_now():
                raise Stop()
            self.write_status()
            time.sleep(min(5.0, max(0.05, end - now())))

    def _claude(self) -> str:
        name = self.job.get("claude_bin") or "claude"
        found = shutil.which(name, path=self._env()["PATH"])
        return found or name

    def _env(self) -> dict:
        env = dict(os.environ)
        env["PATH"] = str(Path.home() / ".local" / "bin") + os.pathsep + env.get("PATH", "")
        env.setdefault("DISABLE_AUTOUPDATER", "1")
        # The machine is the sandbox; this also lets bypass mode run where the user is root.
        env.setdefault("IS_SANDBOX", "1")
        # Headless runs drop background subagents after ten idle minutes unless told not to.
        env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = "0"
        token = Path.home() / ".claude" / "oauth-token"
        if not env.get("CLAUDE_CODE_OAUTH_TOKEN") and not env.get("ANTHROPIC_API_KEY") and token.is_file():
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token.read_text().strip()
        env["ORG_RUN_DIR"] = str(self.dir)
        return env

    # ------------------------------------------------------------ one CLI invocation
    def invoke(self, label: str, prompt: str, schema: dict, budget_usd: Optional[float]) -> dict:
        st = self.state
        cmd = [
            self._claude(),
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--forward-subagent-text",
            "--dangerously-skip-permissions",
            "--model", str(self.job.get("model") or "fable"),
            "--effort", str(self.job.get("effort") or "high"),
            "--append-system-prompt", self.system_prompt,
            "--json-schema", json.dumps(schema, separators=(",", ":")),
        ]
        cmd += ["--resume", st["session_id"]] if st["session_started"] else ["--session-id", st["session_id"]]
        if budget_usd is not None:
            cmd += ["--max-budget-usd", "%.2f" % max(budget_usd, 0.01)]
        started = now()
        self._killed = False
        self.meter.last_event_at = started
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._log,
                cwd=str(self.workspace),
                env=self._env(),
                start_new_session=True,
            )
        except OSError as e:
            return {"is_error": True, "subtype": "spawn_failed", "structured": None, "text": str(e),
                    "cost_usd": 0.0, "label": label, "started_at": started, "ended_at": now(), "rc": None}
        self.proc = proc
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

        lines: "queue.Queue" = queue.Queue()

        def pump():
            for raw in proc.stdout:
                lines.put(raw)
            lines.put(None)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()

        result, flags, saw_init, last_status = None, {}, False, 0.0
        silence = float(self.cfg.get("silence_timeout_min") or 90) * 60.0
        deadline = self.deadline()
        while True:
            try:
                raw = lines.get(timeout=2.0)
            except queue.Empty:
                raw = b""
            if raw is None:
                break
            if raw:
                self._events.write(raw if raw.endswith(b"\n") else raw + b"\n")
                try:
                    ev = json.loads(raw)
                except ValueError:
                    ev = None
                if isinstance(ev, dict):
                    summary = self.meter.feed(ev)
                    if ev.get("type") == "system" and ev.get("subtype") == "init":
                        saw_init = True
                        st["session_id"] = ev.get("session_id") or st["session_id"]
                        st["session_started"] = True
                        self.save_state()
                    if summary is not None:
                        result = summary
                    self._echo_activity()
                    self._note_utilization()
            t = now()
            if t - last_status >= 5.0:
                self._events.flush()
                self.write_status()
                last_status = t
            if self._killed:
                continue
            if self._stop_now():
                flags["stopped"] = True
                self._kill(proc)
            elif deadline and t > deadline and st["phase"] != "reporting":
                flags["time_cap"] = True
                self._kill(proc)
            elif t - (self.meter.last_event_at or started) > silence:
                flags["silent"] = True
                self._kill(proc)
        rc = proc.wait()
        reader.join(timeout=5)
        proc.stdout.close()
        self.proc = None
        self._events.flush()
        if result is None:
            # The turn died without the CLI's cost figure. Keep the table-priced estimate.
            lost = self.meter.live_usd()
            self.meter.confirmed_usd += lost
            self.meter._turn = {}
            st["estimated_usd"] = st.get("estimated_usd", 0.0) + lost
            result = {"is_error": True, "subtype": "no_result", "structured": None, "text": None, "cost_usd": lost}
        result.update(flags)
        result.update({"rc": rc, "label": label, "started_at": started, "ended_at": now(), "saw_init": saw_init})
        st["turns"].append(
            {
                "label": label,
                "started_at": started,
                "ended_at": result["ended_at"],
                "cost_usd": result.get("cost_usd"),
                "subtype": result.get("subtype"),
                "is_error": result.get("is_error"),
                "num_turns": result.get("num_turns"),
                "rc": rc,
            }
        )
        self.save_state()
        self.write_status()
        return result

    def _echo_activity(self) -> None:
        act = self.meter.activity
        if act and act["text"] != self._last_said:
            self._last_said = act["text"]
            self.say(("  ↳ " if act.get("sub") else "  ") + act["text"])

    def _note_utilization(self) -> None:
        week = weekly_window(self.meter.rate_limit)
        if week and week.get("utilization") is not None and self.state.get("util_start") is None:
            self.state["util_start"] = {"utilization": week["utilization"], "resets_at": week.get("resets_at"), "at": now()}

    # ------------------------------------------------------------ one logical turn
    def _limit_wait(self, res: dict) -> Optional[float]:
        """Seconds to wait if the turn died on the plan's usage limit, else None."""
        rl = self.meter.rate_limit or {}
        hit = rl.get("status") == "rejected" or bool(LIMIT_TEXT_RE.search(res.get("text") or ""))
        if not hit:
            return None
        resets = [rl.get("resets_at")] + [w.get("resets_at") for w in (rl.get("windows") or {}).values()
                                          if w.get("status") == "rejected"]
        resets = [r for r in resets if r and r > now()]
        if resets:  # a little past the reset, so the first request does not race it
            return min(resets) - now() + float(self.cfg.get("limit_pad_s", 60))
        return float(self.cfg.get("limit_default_wait_s", 900))

    def _why(self, res: dict) -> str:
        if res.get("silent"):
            return "the process produced no output for too long and was restarted"
        if res.get("subtype") == "no_result":
            return "the process exited with code %s before finishing" % res.get("rc")
        if res.get("is_error"):
            return "the turn ended in an error: %s %s" % (res.get("subtype"), clip(res.get("text") or "", 200))
        return "the turn ended with prose instead of the structured output"

    def turn(self, label: str, prompt: str, schema: dict, reserve: bool = True) -> dict:
        st = self.state
        max_retries = int(self.cfg.get("max_turn_retries", 3))
        backoff = float(self.cfg.get("retry_backoff_s", 30))
        retries, text, lost_sessions = 0, prompt, 0
        while True:
            if self._stop_now():
                raise Stop()
            deadline = self.deadline()
            if deadline and now() > deadline and st["phase"] != "reporting":
                raise CapReached("time")
            budget, cap = None, self.cost_cap()
            if cap is not None:
                hold = float(self.cfg.get("report_reserve_usd", 3.0))
                hold = min(hold, cap * 0.25)
                if reserve:
                    budget = cap - hold - self.meter.confirmed_usd
                    if budget <= 0:
                        raise CapReached("cost")
                else:
                    budget = max(cap - self.meter.confirmed_usd, hold)
            self.say("turn %s%s" % (label, " (retry %d)" % retries if retries else ""))
            res = self.invoke(label, text, schema, budget)
            if res.get("stopped"):
                raise Stop()
            if not res.get("is_error") and res.get("structured") is not None:
                return res
            if res.get("time_cap"):
                raise CapReached("time")
            if res.get("subtype") == "error_max_budget_usd":
                raise CapReached("cost")
            wait = self._limit_wait(res)
            if wait is not None:
                until = now() + wait
                st["waits"].append({"from": now(), "until": until, "reason": "usage limit"})
                st["waiting_until"] = until
                previous = st["state"]
                self.set(state="waiting", detail="usage limit reached; resuming at %s" % time.strftime("%a %H:%M", time.localtime(until)))
                self.say("usage limit reached; waiting %s" % fmt_dur(wait))
                self._sleep(wait)
                st["waiting_until"] = None
                if self.meter.rate_limit:
                    self.meter.rate_limit["status"] = None
                self.set(state=previous, detail="resuming after the usage limit reset")
                text = render(NUDGE, why="the usage limit was reached and has now reset", original=prompt)
                continue
            retries += 1
            st["retries"] = st.get("retries", 0) + 1
            if retries > max_retries:
                raise TurnFailed("%s: %s" % (label, self._why(res)))
            if st["session_started"] and not res.get("saw_init"):
                # --resume itself failed. Once is a hiccup; twice means the session is gone.
                lost_sessions += 1
                if lost_sessions >= 2:
                    st["session_id"], st["session_started"] = str(uuid.uuid4()), False
                    self.warn("the session could not be resumed; continued in a new one")
                    text = render(RECOVER, run_dir=self.dir, workspace=self.workspace, original=prompt)
                    self._sleep(backoff)
                    continue
            if not st["session_started"]:
                st["session_id"] = str(uuid.uuid4())
                text = prompt
            else:
                text = render(NUDGE, why=self._why(res), original=prompt)
            self.say("turn %s did not finish (%s); retrying" % (label, self._why(res)))
            self._sleep(min(backoff * (2 ** (retries - 1)), 300.0))

    # ------------------------------------------------------------ phases
    def prepare(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._events = open(self.dir / "events.jsonl", "ab")
        self._log = open(self.dir / "runner.log", "a", encoding="utf-8")
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        self.check_spec()
        self.system_prompt = render(
            read_text(PROMPTS / "engineer.md"),
            run_id=self.job["run_id"],
            spec_id=self.job.get("spec_id") or self.job["run_id"],
            run_dir=self.dir,
            workspace=self.workspace,
            model=self.job.get("model") or "fable",
            delivery=self.job.get("delivery") or "pr",
            price_table=pricing.prompt_table(self.prices),
            machine_facts=machine_facts(self.job.get("facts") or {}),
        )

    def check_spec(self) -> None:
        """The spec is read-only. If the copy in the run directory changed, put it back."""
        spec, pristine = self.dir / "spec.md", self.dir / ".org" / "spec.md"
        want = self.job.get("spec_sha")
        if want and sha256_file(spec) != want and pristine.exists() and sha256_file(pristine) == want:
            try:
                if spec.exists():
                    os.chmod(spec, 0o644)
                shutil.copyfile(pristine, spec)
                self.warn("spec.md in the run directory had been changed; the approved text was restored")
            except OSError as e:
                self.warn("spec.md differs from the approved text and could not be restored: %s" % e)
        try:
            os.chmod(spec, 0o444)
        except OSError:
            pass

    def _caps_text(self) -> str:
        lines = []
        cap = self.cost_cap()
        if self.caps.get("max_cost_usd"):
            lines.append("- Cost: at most %s for the whole run." % fmt_usd(self.caps["max_cost_usd"]))
        if self.caps.get("max_weekly_pct"):
            per = (self.job.get("budget") or {}).get("usd_per_weekly_pct")
            lines.append(
                "- Weekly budget: at most %s%% of the weekly limit%s."
                % (("%g" % self.caps["max_weekly_pct"]), (", about %s" % fmt_usd(self.caps["max_weekly_pct"] * per)) if per
                   else " (not yet convertible to dollars on this account; treat it as a strong preference)")
            )
        if self.caps.get("max_hours"):
            lines.append("- Time: at most %g hours of wall-clock time for the whole run, planning included." % self.caps["max_hours"])
        if cap is not None:
            lines.append("- The runner enforces the cost cap of %s and ends the run when it is reached." % fmt_usd(cap))
        return "\n".join(lines) or "None. Be economical anyway: the budget is shared."

    def _budget_text(self) -> str:
        b = self.job.get("budget") or {}
        week = b.get("weekly") or {}
        lines = []
        if week.get("utilization") is not None:
            left = ""
            if week.get("resets_at"):
                left = ", resetting %s (in %s)" % (
                    time.strftime("%a %H:%M", time.localtime(week["resets_at"])), fmt_dur(week["resets_at"] - now()))
            lines.append("- The weekly limit was %.0f%% used when this run was dispatched%s." % (week["utilization"] * 100, left))
        if b.get("usd_per_weekly_pct"):
            lines.append("- On this account 1%% of the weekly limit is about %s of API-equivalent spend." % fmt_usd(b["usd_per_weekly_pct"]))
        lines.append("- Live figures are in `%s/status.json` under `rate_limit` and `cost`." % self.dir)
        return "\n".join(lines)

    def _history_text(self) -> str:
        h = self.job.get("history") or {}
        if not h.get("runs"):
            return "No finished runs yet. Estimates of agentic work usually run low; say what yours rests on."
        lines = []
        if h.get("time_ratio"):
            lines.append("- Across %d finished run(s), actual time was %.1fx the estimate at the median." % (len(h["runs"]), h["time_ratio"]))
        if h.get("cost_ratio"):
            lines.append("- Actual cost was %.1fx the estimate at the median." % h["cost_ratio"])
        lines.append("Correct for that bias instead of repeating it. The most recent runs:")
        for r in h["runs"][-6:]:
            lines.append(
                "- %s: %s; estimated %s and %s, took %s and %s."
                % (clip(r.get("title") or r.get("run_id"), 50), r.get("status"),
                   fmt_dur((r.get("est_minutes") or 0) * 60), fmt_usd(r.get("est_cost_usd")),
                   fmt_dur((r.get("actual_minutes") or 0) * 60), fmt_usd(r.get("actual_cost_usd")))
            )
        return "\n".join(lines)

    def _plan_problems(self, plan: dict) -> List[str]:
        out = schemas.problems(plan, schemas.PLAN)
        if out:
            return out
        if plan["verdict"] == "infeasible":
            if not (plan.get("infeasible_reason") or "").strip():
                out.append("verdict is infeasible but infeasible_reason is empty")
            return out
        ms = plan.get("milestones") or []
        if not ms:
            out.append("verdict is proceed but there are no milestones")
        ids = [m.get("id") for m in ms]
        if len(set(ids)) != len(ids):
            out.append("milestone ids must be unique")
        if any((m.get("est_minutes") or 0) <= 0 for m in ms):
            out.append("every milestone needs a positive est_minutes")
        est = plan.get("estimate") or {}
        cap = self.cost_cap()
        spent = self.meter.confirmed_usd
        if cap is not None and (est.get("cost_usd") or 0) + spent > cap:
            out.append("estimated cost %s plus the %s already spent exceeds the cap of %s"
                       % (fmt_usd(est.get("cost_usd")), fmt_usd(spent), fmt_usd(cap)))
        deadline = self.deadline()
        if deadline and now() + (est.get("wall_minutes") or 0) * 60.0 > deadline:
            out.append("estimated %s of work does not fit in the %s left before the time cap"
                       % (fmt_dur((est.get("wall_minutes") or 0) * 60), fmt_dur(deadline - now())))
        if len((read_text(self.dir / "plan.md") or "").strip()) < 200:
            out.append("plan.md is missing or too short to review")
        return out

    def phase_plan(self) -> None:
        st = self.state
        self.set(state="planning", phase="planning", detail="researching and planning")
        prompt = render(
            read_text(PROMPTS / "plan.md"),
            run_dir=self.dir,
            spec_sha=self.job.get("spec_sha") or "",
            repos=", ".join(self.job.get("repos") or []) or "none named; see the spec",
            caps=self._caps_text(),
            budget=self._budget_text(),
            history=self._history_text(),
        )
        res = self.turn("plan", prompt, schemas.PLAN)
        plan = res["structured"]
        problems = self._plan_problems(plan)
        if problems:
            self.say("plan rejected: " + "; ".join(problems))
            res = self.turn("plan-repair", render(REPAIR_PLAN, problems="\n".join("- " + p for p in problems), run_dir=self.dir), schemas.PLAN)
            plan = res["structured"]
            problems = self._plan_problems(plan)
            if problems:
                # Still not acceptable: the honest reading is that it does not fit.
                plan = dict(plan)
                plan["verdict"] = "infeasible"
                plan["infeasible_reason"] = "no acceptable plan was produced: " + "; ".join(problems)
        # Freeze. The engineer may amend plan.md later; these copies are what a reviewer judges.
        write_json(self.dir / "plan.json", plan)
        write_json(self.dir / "plan.frozen.json", plan)
        frozen = self.dir / "plan.frozen.md"
        if (self.dir / "plan.md").exists():
            if frozen.exists():  # left by a runner that died before recording that planning was done
                os.chmod(frozen, 0o644)
            shutil.copyfile(self.dir / "plan.md", frozen)
            os.chmod(frozen, 0o444)
        st["plan"] = plan
        st["verdict"] = plan["verdict"]
        st["estimate"] = plan.get("estimate")
        st["milestones"] = [dict(m) for m in plan.get("milestones") or []] if plan["verdict"] == "proceed" else []
        st["plan_done_at"] = now()
        st["plan_cost_usd"] = self.meter.confirmed_usd
        if plan["verdict"] == "infeasible":
            st["end_reason"] = "the plan's verdict was infeasible: " + (plan.get("infeasible_reason") or "")
        est = plan.get("estimate") or {}
        self.say("plan: %s, %d milestone(s), estimated %s and %s"
                 % (plan["verdict"], len(st["milestones"]), fmt_dur((est.get("wall_minutes") or 0) * 60), fmt_usd(est.get("cost_usd"))))
        self.save_state()

    def _standing(self) -> str:
        st = self.state
        lines = []
        for m in st["milestones"]:
            if m.get("ended_at"):
                lines.append("- %s %s: %s in %s for %s (estimated %s, %s)" % (
                    m["id"], m["title"], m.get("outcome"), fmt_dur(m["ended_at"] - m["started_at"]), fmt_usd(m.get("cost_usd")),
                    fmt_dur((m.get("est_minutes") or 0) * 60), fmt_usd(m.get("est_cost_usd"))))
        if not lines:
            lines.append("- This is the first milestone.")
        cap, deadline = self.cost_cap(), self.deadline()
        lines.append("- Spent so far: %s%s." % (fmt_usd(self.meter.confirmed_usd), (" of a %s cap" % fmt_usd(cap)) if cap else ""))
        if deadline:
            lines.append("- Time left before the cap: %s." % fmt_dur(deadline - now()))
        return "\n".join(lines)

    def phase_execute(self) -> None:
        st = self.state
        if not st.get("exec_started_at"):
            st["exec_started_at"] = now()
        self.set(state="executing", phase="executing")
        while True:
            ms = st["milestones"]
            idx = next((i for i, m in enumerate(ms) if not m.get("ended_at")), None)
            if idx is None:
                st["end_reason"] = st.get("end_reason") or "all milestones were executed"
                break
            m = ms[idx]
            if not m.get("started_at"):
                m["started_at"] = now()
                m["cost_start"] = self.meter.confirmed_usd
            self.set(detail="milestone %d/%d: %s" % (idx + 1, len(ms), m["title"]))
            self.check_spec()
            prompt = render(
                read_text(PROMPTS / "milestone.md"),
                milestone_id=m["id"],
                position="%d of %d" % (idx + 1, len(ms)),
                title=m["title"],
                goal=m.get("goal") or m["title"],
                verification=m.get("verification") or "as described in plan.md",
                est_minutes="%g" % (m.get("est_minutes") or 0),
                est_cost_usd="%.2f" % (m.get("est_cost_usd") or 0),
                standing=self._standing(),
                run_dir=self.dir,
            )
            res = self.turn("milestone:" + m["id"], prompt, schemas.MILESTONE_RESULT)
            out = res["structured"]
            m["ended_at"] = now()
            m["outcome"] = out.get("outcome")
            m["summary"] = out.get("summary")
            m["cost_usd"] = self.meter.confirmed_usd - (m.get("cost_start") or 0)
            self.say("milestone %s: %s in %s for %s" % (m["id"], m["outcome"], fmt_dur(m["ended_at"] - m["started_at"]), fmt_usd(m["cost_usd"])))
            revised = out.get("revised_remaining")
            if revised is not None and not any(schemas.problems(x, schemas.MILESTONE) for x in revised):
                st["amendments"].append({"at": now(), "after": m["id"], "replaced": [x.get("id") for x in ms[idx + 1:]],
                                         "with": [x.get("id") for x in revised]})
                st["milestones"] = ms[: idx + 1] + [dict(x) for x in revised]
                write_json(self.dir / "plan.json", dict(st["plan"], milestones=st["milestones"]))
                self.say("plan amended: %d milestone(s) now remain" % len(revised))
            self.save_state()
            if out.get("outcome") == "blocked":
                st["end_reason"] = "milestone %s was blocked: %s" % (m["id"], clip(out.get("summary") or "", 300))
                break
            if out.get("stop"):
                st["end_reason"] = "the engineer stopped after %s: %s" % (m["id"], clip(out.get("stop_reason") or out.get("summary") or "", 300))
                break
        st["exec_ended_at"] = now()
        self.save_state()

    def repo_checks(self) -> List[dict]:
        """Every git repository in the workspace, with what would be lost if the machine vanished."""
        found, skip = [], {"node_modules", ".venv", "venv", "target", "build", "dist", "__pycache__"}
        base_depth = len(self.workspace.parts)
        for root, dirs, files in os.walk(self.workspace):
            depth = len(Path(root).parts) - base_depth
            if ".git" in dirs:
                found.append(Path(root))
            dirs[:] = [d for d in dirs if d not in skip and not d.startswith(("bazel-", ".")) and depth < 3]
        out = []
        for repo in found[:40]:
            def git(*args):
                try:
                    r = subprocess.run(["git", "-C", str(repo)] + list(args), capture_output=True, text=True, timeout=30)
                    return r.stdout.strip() if r.returncode == 0 else ""
                except (OSError, subprocess.TimeoutExpired):
                    return ""
            out.append({
                "path": str(repo),
                "origin": git("remote", "get-url", "origin"),
                "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
                "head": git("rev-parse", "--short", "HEAD"),
                "uncommitted_files": len([x for x in git("status", "--porcelain").splitlines() if x.strip()]),
                "unpushed_commits": len([x for x in git("log", "--branches", "--not", "--remotes", "--oneline").splitlines() if x.strip()]),
            })
        return out

    def actuals(self) -> dict:
        st, t = self.state, now()
        end = st.get("finished_at") or t
        est = st.get("estimate") or {}
        plan_done = st.get("plan_done_at")
        plan_cost = st.get("plan_cost_usd")
        total = self.meter.confirmed_usd
        week = weekly_window(self.meter.rate_limit) or {}
        reporting = [x for x in st.get("turns") or [] if x.get("label") == "report"]
        return {
            "run_id": self.job["run_id"],
            "spec_id": self.job.get("spec_id"),
            "title": self.job.get("title"),
            "model": self.job.get("model"),
            "planning": {
                "minutes": round(((plan_done or end) - st["started_at"]) / 60.0, 1),
                "cost_usd": round(plan_cost if plan_cost is not None else total, 4),
            },
            "after_planning": {
                "est_minutes": est.get("wall_minutes"),
                "est_minutes_p90": est.get("wall_minutes_p90"),
                "actual_minutes": round((end - plan_done) / 60.0, 1) if plan_done else None,
                "est_cost_usd": est.get("cost_usd"),
                "est_cost_usd_p90": est.get("cost_usd_p90"),
                "actual_cost_usd": round(total - plan_cost, 4) if plan_cost is not None else None,
            },
            "milestones": [
                {
                    "id": m.get("id"),
                    "title": m.get("title"),
                    "outcome": m.get("outcome"),
                    "est_minutes": m.get("est_minutes"),
                    "actual_minutes": round((m["ended_at"] - m["started_at"]) / 60.0, 1) if m.get("ended_at") else None,
                    "est_cost_usd": m.get("est_cost_usd"),
                    "actual_cost_usd": round(m["cost_usd"], 4) if m.get("cost_usd") is not None else None,
                }
                for m in st.get("milestones") or []
            ],
            # Verifying and writing the report is real time and money; the plan's totals include it.
            "report": {
                "minutes": round(sum(x["ended_at"] - x["started_at"] for x in reporting) / 60.0, 1),
                "cost_usd": round(sum(x.get("cost_usd") or 0 for x in reporting), 4),
            } if reporting else None,
            "total": {
                "elapsed_minutes": round((end - st["started_at"]) / 60.0, 1),
                "cost_usd": round(total, 4),
                "of_which_estimated_usd": round(st.get("estimated_usd", 0.0), 4),
                "by_model": self.meter.by_model,
            },
            "turns": len(st.get("turns") or []),
            "retries": st.get("retries", 0),
            "waits": [{"minutes": round((w["until"] - w["from"]) / 60.0, 1), "reason": w["reason"]} for w in st.get("waits") or []],
            "amendments": len(st.get("amendments") or []),
            "subagents_spawned": self.meter.agents_spawned,
            "weekly": {
                "utilization_start": (st.get("util_start") or {}).get("utilization"),
                "utilization_end": week.get("utilization"),
                "resets_at": week.get("resets_at"),
            },
            "caps": self.caps,
            "end_reason": st.get("end_reason"),
            "started_at": st["started_at"],
            "finished_at": st.get("finished_at"),
        }

    def phase_report(self) -> dict:
        st = self.state
        reason = st.get("end_reason") or "the run ended"
        self.set(state="reporting", phase="reporting", detail="verifying and writing the report")
        checks = self.repo_checks()
        if checks:
            lines = ["- `%s` on `%s` at %s: %d uncommitted file(s), %d unpushed commit(s)%s" % (
                c["path"], c["branch"], c["head"] or "no commits", c["uncommitted_files"], c["unpushed_commits"],
                "" if c["origin"] else ", no remote") for c in checks]
        else:
            lines = ["None."]
        prompt = render(
            read_text(PROMPTS / "report.md"),
            reason=reason,
            actuals=json.dumps(self.actuals(), indent=2, sort_keys=True),
            repo_checks="\n".join(lines),
            acceptance_ids=", ".join(self.job.get("acceptance_ids") or []) or "see the spec",
            run_dir=self.dir,
        )
        res = self.turn("report", prompt, schemas.RESULT, reserve=False)
        result = dict(res["structured"])
        # A run is done only if every criterion in the spec was reported as passing.
        verdicts = {a.get("id"): a.get("verdict") for a in result.get("acceptance") or []}
        failing = [i for i in (self.job.get("acceptance_ids") or []) if verdicts.get(i) != "pass"]
        failing += [i for i, v in verdicts.items() if v != "pass" and i not in failing]
        if result.get("status") == "done" and failing:
            result["status"] = "partial"
            result["lowered_by_runner"] = True
            result["msg"] = "%s [Runner: lowered from done; not shown passing: %s.]" % (result.get("msg", ""), ", ".join(failing))
        if self.job.get("delivery") != "none":
            for c in self.repo_checks():
                if c["unpushed_commits"] or c["uncommitted_files"]:
                    self.warn("%s still has %d uncommitted file(s) and %d unpushed commit(s)"
                              % (c["path"], c["uncommitted_files"], c["unpushed_commits"]))
        return result

    def finish(self, state: str, result: dict) -> None:
        st = self.state
        st["finished_at"] = now()
        st["phase"] = "finished"
        st["state"] = state
        st["detail"] = clip(result.get("msg") or "", 200)
        st["result"] = result
        write_json(self.dir / "result.json", result)
        write_json(self.dir / "actuals.json", self.actuals())
        if not (self.dir / "report.md").exists():
            write_text(self.dir / "report.md", "# Report\n\nNo report was written by the engineer.\n\n"
                       "Status: %s\n\n%s\n\nMeasured figures are in actuals.json.\n" % (result.get("status"), result.get("msg")))
        self.save_state()
        self.write_status()
        self.say("finished: %s — %s (%s, %s)" % (state, clip(result.get("msg") or "", 160),
                 fmt_dur(st["finished_at"] - st["started_at"]), fmt_usd(self.meter.confirmed_usd)))

    # ------------------------------------------------------------ the run
    def run(self) -> int:
        st = self.state
        if st.get("phase") == "finished":
            print("run %s already finished: %s" % (self.job["run_id"], st.get("state")))
            return 0
        # One runner per run. `org resume` on a run whose runner is in fact alive must not start
        # a second engineer in the same session and workspace.
        self._lock = open(self.dir / ".runner.lock", "w")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("run %s already has a live runner; not starting another" % self.job["run_id"])
            self._lock.close()
            return 3
        self.prepare()
        self.say("run %s on %s, model %s" % (self.job["run_id"], os.uname().nodename, self.job.get("model")))
        try:
            try:
                if not st.get("plan_done_at"):
                    self.phase_plan()
                if st.get("verdict") == "proceed" and not st.get("exec_ended_at"):
                    self.phase_execute()
            except CapReached as e:
                cap = self.cost_cap()
                st["end_reason"] = ("the cost cap of %s was reached" % fmt_usd(cap)) if e.kind == "cost" else (
                    "the time cap of %g hours was reached" % self.caps.get("max_hours", 0))
                self.say(st["end_reason"])
            except TurnFailed as e:
                st["end_reason"] = "a turn failed repeatedly (%s)" % e
                self.say(st["end_reason"])
            self.save_state()
            try:
                result = self.phase_report()
            except (TurnFailed, CapReached) as e:
                progressed = any(m.get("outcome") in ("done", "partial") for m in st.get("milestones") or [])
                result = {
                    "status": "partial" if progressed else "fail",
                    "msg": "%s; the engineer did not produce a report (%s)." % (st.get("end_reason") or "the run ended", e),
                    "acceptance": [{"id": i, "verdict": "unverified", "evidence": "no report"} for i in self.job.get("acceptance_ids") or []],
                    "synthesized_by_runner": True,
                }
            self.finish(STATE_FOR_STATUS.get(result.get("status"), "failed"), result)
            return 0
        except Stop:
            self.finish("stopped", {"status": "fail", "msg": "Stopped by request before completion.", "acceptance": [],
                                    "synthesized_by_runner": True})
            return 0
        except Exception as e:  # the runner itself broke: leave a readable record, never a silent death
            import traceback

            traceback.print_exc(file=self._log or sys.stderr)
            try:
                self.finish("failed", {"status": "fail", "msg": "The runner failed: %s: %s" % (type(e).__name__, e),
                                       "acceptance": [], "synthesized_by_runner": True})
            except Exception:
                pass
            return 1
        finally:
            for f in (self._events, self._log, self._lock):
                try:
                    if f:
                        f.close()
                except OSError:
                    pass


# ---------------------------------------------------------------- what the manager's side pulls


def machine_facts(facts: dict) -> str:
    """What dispatch found on the machine just before the run, as the engineer is told it."""
    cores = str(facts.get("cores") or "").strip()
    out = ["It has %s CPU core%s." % (cores, "" if cores == "1" else "s")] if cores.isdigit() else []
    docker = facts.get("docker")
    if docker == "yes":
        out.append("Docker works here: a daemon of this machine's own answered `docker info` just before the run, so you can "
                   "build images and run containers, and nothing you start there outlives the machine.")
    elif docker == "no":
        out.append("Docker does not work here: no daemon answered `docker info` just before the run. If the work needs "
                   "containers, try to install or start one, and if that fails say so in the plan or the report instead "
                   "of working around it silently; a machine with Docker is something the human can grant next time.")
    else:
        out.append("Whether Docker works here was not checked; `docker info` says.")
    return " ".join(out)


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def snapshot(run_dir, events_from: int = 0, max_bytes: int = 2_000_000) -> dict:
    """Everything the manager's side needs about a run, as one JSON document.

    Reads only. Events are returned incrementally from a byte offset and cut at a line boundary.
    """
    d = Path(run_dir).resolve()
    files = {}
    for name in SNAPSHOT_FILES:
        p = d / name
        if p.is_file() and p.stat().st_size <= 2_000_000:
            files[name] = read_text(p)
    status = read_json(d / "status.json") or {}
    events = {"offset": events_from, "size": 0, "data": ""}
    ev = d / "events.jsonl"
    if ev.is_file():
        size = ev.stat().st_size
        events["size"] = size
        start = events_from if 0 <= events_from <= size else max(0, size - max_bytes)
        with open(ev, "rb") as f:
            f.seek(start)
            chunk = f.read(max_bytes)
        if start + len(chunk) < size or not chunk.endswith(b"\n"):
            cut = chunk.rfind(b"\n")
            chunk = chunk[: cut + 1] if cut >= 0 else b""
        events["data"] = chunk.decode("utf-8", errors="replace")
        events["offset"] = start + len(chunk)
    log = read_text(d / "runner.log") or ""
    return {
        "ok": True,
        "now": now(),
        "host": os.uname().nodename,
        "alive": pid_alive(status.get("pid")) if status.get("state") not in TERMINAL_STATES else False,
        "files": files,
        "events": events,
        "log_tail": log[-4000:],
    }
