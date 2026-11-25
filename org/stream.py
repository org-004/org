"""Reading the CLI's stream-json output: spend, activity, subagents and the usage-limit state.

Event shapes were taken from Claude Code 2.1.322 (`claude -p --output-format stream-json
--verbose --forward-subagent-text`):

  system/init            session_id, model
  assistant              message.id, message.model, message.usage, message.content[],
                         parent_tool_use_id (set for a subagent's messages)
  system/task_started    task_id, description, subagent_type      (a subagent began)
  system/task_notification  task_id, status, usage                (a subagent ended)
  rate_limit_event       rate_limit_info.status, .resetsAt, .rateLimitType,
                         .unifiedWindows.<window>.utilization     (0..1 of the plan's limit)
  result                 subtype, is_error, total_cost_usd, modelUsage,
                         model_usage_since_last_result, structured_output, result

Unknown event types and missing fields are ignored: the stream grows new ones between versions.

One process may print several `result` events: an API error after the turn's own result, and
a result with `num_turns: 0` after each wake-up for background work. Each is closed with its own
cost. A result whose `model_usage_since_last_result` is present and empty (`{}`) used no tokens
and costs nothing; `total_cost_usd` is cumulative over the session and is never charged whole
when the per-turn field is there.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from . import pricing
from .util import clip, now

USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
CUMULATIVE_KEYS = ("costUSD", "inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")


def describe_tool(name: str, inp: Optional[dict]) -> str:
    inp = inp or {}
    if name == "Bash":
        return "Bash: " + clip(inp.get("command", ""), 110)
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        return "%s: %s" % (name, inp.get("file_path") or inp.get("notebook_path") or "")
    if name in ("Grep", "Glob"):
        return "%s: %s" % (name, clip(inp.get("pattern", ""), 80))
    if name == "Agent":
        who = inp.get("model") or inp.get("subagent_type") or "default"
        return "Agent[%s]: %s" % (who, clip(inp.get("description") or inp.get("prompt", ""), 80))
    if name == "Workflow":
        return "Workflow: " + clip(inp.get("name") or inp.get("description") or "script", 80)
    if name in ("WebSearch", "WebFetch"):
        return "%s: %s" % (name, clip(inp.get("query") or inp.get("url") or "", 80))
    if name == "StructuredOutput":
        return "writing the turn's structured output"
    return name


def normalize_rate_limit(info: dict) -> dict:
    """One shape for the limit state, whichever field spelling the CLI used."""
    info = info or {}
    kind = info.get("rateLimitType") or info.get("rate_limit_type")
    out = {
        "status": info.get("status"),
        "type": kind,
        "resets_at": info.get("resetsAt") or info.get("resets_at"),
        "at": now(),
        "windows": {},
    }
    for name, w in (info.get("unifiedWindows") or {}).items():
        out["windows"][name] = {
            "utilization": w.get("utilization"),
            "resets_at": w.get("resetsAt") or w.get("resets_at"),
            "status": w.get("status"),
        }
    if not out["windows"] and info.get("utilization") is not None:
        out["windows"][kind or "unknown"] = {
            "utilization": info.get("utilization"),
            "resets_at": out["resets_at"],
            "status": info.get("status"),
        }
    return out


def weekly_window(rate_limit: Optional[dict]) -> Optional[dict]:
    if not rate_limit:
        return None
    return (rate_limit.get("windows") or {}).get("seven_day")


def feed(path, start: int = 0):
    """Yield (next offset, line) for each human-readable line in an event log, from a byte offset.

    This is the activity feed behind `org logs`, `org peek` and the dashboard's activity view.
    """
    try:
        f = open(path, "rb")
    except FileNotFoundError:
        return
    with f:
        f.seek(start)
        while True:
            raw = f.readline()
            if not raw or not raw.endswith(b"\n"):
                return
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            kind = ev.get("type")
            lead = "  ↳ " if ev.get("parent_tool_use_id") else "  "
            if kind == "assistant":
                for block in (ev.get("message") or {}).get("content") or []:
                    text = None
                    if block.get("type") == "tool_use":
                        text = describe_tool(block.get("name") or "tool", block.get("input"))
                    elif block.get("type") == "text" and (block.get("text") or "").strip():
                        text = clip(block["text"], 240)
                    if text:
                        yield f.tell(), lead + text
            elif kind == "system" and ev.get("subtype") == "task_started":
                yield f.tell(), "  + subagent: %s" % clip(ev.get("description") or "", 120)
            elif kind == "result":
                cost = sum((m.get("costUSD") or 0) for m in (ev.get("model_usage_since_last_result") or {}).values())
                yield f.tell(), "— turn ended: %s, $%.2f" % (ev.get("subtype"), cost)


class Meter:
    """Accumulates one run's spend and activity across turns."""

    def __init__(self, prices: Dict[str, Dict[str, float]], confirmed_usd: float = 0.0, by_model: Optional[dict] = None,
                 cumulative: Optional[dict] = None, totals: Optional[dict] = None):
        self.prices = prices
        self.confirmed_usd = confirmed_usd  # sum of the CLI's own per-turn costs
        self.by_model: Dict[str, Dict[str, float]] = by_model or {}
        self.cumulative: Dict[str, dict] = cumulative or {}  # session id -> last cumulative modelUsage
        self.totals: Dict[str, float] = totals or {}  # session id -> last cumulative total_cost_usd
        self.correction: Dict[str, float] = {}  # model -> CLI cost / table cost, last turn
        self._turn: Dict[str, Any] = {}  # message id -> (model, usage) for the turn in flight
        self.session_id: Optional[str] = None
        self.model: Optional[str] = None
        self.rate_limit: Optional[dict] = None
        self.activity: Optional[dict] = None
        self.agents: Dict[str, str] = {}  # running subagents: task id -> description
        self.agents_spawned = 0
        self.last_event_at: Optional[float] = None
        self.unpriced: set = set()

    # ------------------------------------------------------------ spend
    def live_usd(self) -> float:
        """Table-priced estimate for the turn in flight."""
        total = 0.0
        for model, usage in self._turn.values():
            c = pricing.usage_cost(model, usage, self.prices)
            if c is None:
                self.unpriced.add(model)
                continue
            total += c * self.correction.get(model, 1.0)
        return total

    def total_usd(self) -> float:
        return self.confirmed_usd + self.live_usd()

    def _turn_usage(self, ev: dict) -> dict:
        """Per-model usage of this turn alone.

        `total_cost_usd` and `modelUsage` are cumulative over a session, across resumed
        invocations (seen in 2.1.322: 0.106, 0.232, 0.362 for three turns). The per-turn figure is
        `model_usage_since_last_result`; `{}` there means the turn used no tokens (an API error,
        a wake-up with nothing to do). If a CLI version omits it, difference the cumulative one.
        """
        since = ev.get("model_usage_since_last_result")
        cumulative = ev.get("modelUsage") or {}
        sid = self._sid(ev)
        if not isinstance(since, dict):
            before, since = self.cumulative.get(sid) or {}, {}
            for model, m in cumulative.items():
                p = before.get(model) or {}
                delta = {k: (m.get(k) or 0) - (p.get(k) or 0) for k in CUMULATIVE_KEYS}
                # Counters that went backwards mean this process started from zero.
                since[model] = delta if delta["costUSD"] >= 0 else {k: m.get(k) or 0 for k in CUMULATIVE_KEYS}
        if cumulative:
            self.cumulative[sid] = {model: {k: m.get(k) or 0 for k in CUMULATIVE_KEYS} for model, m in cumulative.items()}
        return since

    def _sid(self, ev: dict) -> str:
        return ev.get("session_id") or self.session_id or "?"

    def _turn_cost(self, ev: dict, per_model: dict) -> float:
        """What this result cost, from the most specific figure the CLI gave.

        The per-turn field, when present, decides, even when empty. Without it, the differenced
        `modelUsage`. Without either, `total_cost_usd` less the last one seen for the session: the
        whole figure for a session not seen before, or when it went backwards (a new process).
        """
        sid = self._sid(ev)
        total = ev.get("total_cost_usd")
        last = self.totals.get(sid)
        if total is not None:
            self.totals[sid] = float(total)
        if isinstance(ev.get("model_usage_since_last_result"), dict) or ev.get("modelUsage"):
            return sum((m.get("costUSD") or 0.0) for m in per_model.values())
        if total is None:
            return 0.0
        if last is None or total < last:
            return float(total)
        return float(total) - last

    def _close_turn(self, ev: dict) -> dict:
        per_model = self._turn_usage(ev)
        cost = self._turn_cost(ev, per_model)
        # How far off was the table for each model this turn?
        est: Dict[str, float] = {}
        for model, usage in self._turn.values():
            c = pricing.usage_cost(model, usage, self.prices)
            if c:
                est[model] = est.get(model, 0.0) + c
        for model, m in per_model.items():
            actual = m.get("costUSD") or 0.0
            if est.get(model) and actual > 0:
                self.correction[model] = min(5.0, max(0.2, actual / est[model]))
            row = self.by_model.setdefault(
                model, {"usd": 0.0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
            )
            row["usd"] += actual
            row["input"] += m.get("inputTokens") or 0
            row["output"] += m.get("outputTokens") or 0
            row["cache_read"] += m.get("cacheReadInputTokens") or 0
            row["cache_write"] += m.get("cacheCreationInputTokens") or 0
        self.confirmed_usd += cost
        self._turn = {}
        self.agents = {}
        return {
            "is_error": bool(ev.get("is_error")),
            "subtype": ev.get("subtype"),
            "terminal_reason": ev.get("terminal_reason"),
            "api_error_status": ev.get("api_error_status"),
            "cost_usd": cost,
            "duration_ms": ev.get("duration_ms"),
            "num_turns": ev.get("num_turns"),
            "structured": ev.get("structured_output"),
            "text": ev.get("result"),
            "denials": len(ev.get("permission_denials") or []),
        }

    # ------------------------------------------------------------ events
    def feed(self, ev: dict) -> Optional[dict]:
        """Take one event. Returns the turn summary when the event is the turn's `result`."""
        self.last_event_at = now()
        kind = ev.get("type")
        if kind == "system":
            sub = ev.get("subtype")
            if sub == "init":
                self.session_id = ev.get("session_id") or self.session_id
                self.model = ev.get("model") or self.model
            elif sub == "task_started":
                self.agents[ev.get("task_id") or "?"] = ev.get("description") or ev.get("subagent_type") or "subagent"
                self.agents_spawned += 1
            elif sub == "task_notification":
                self.agents.pop(ev.get("task_id") or "?", None)
            elif sub == "task_updated":
                if (ev.get("patch") or {}).get("status") in ("completed", "failed", "killed", "stopped"):
                    self.agents.pop(ev.get("task_id") or "?", None)
            elif sub == "compact_boundary":
                self.activity = {"at": now(), "text": "context compacted", "sub": False}
        elif kind == "assistant":
            msg = ev.get("message") or {}
            mid, model, usage = msg.get("id"), msg.get("model"), msg.get("usage")
            if mid and usage:
                # One API message arrives as several events with the usage repeated or growing.
                _, seen = self._turn.get(mid, (model, {}))
                merged = dict(seen)
                for k in USAGE_KEYS:
                    merged[k] = max(seen.get(k) or 0, usage.get(k) or 0)
                if usage.get("cache_creation"):
                    merged["cache_creation"] = usage["cache_creation"]
                self._turn[mid] = (model, merged)
            is_sub = ev.get("parent_tool_use_id") is not None
            for block in msg.get("content") or []:
                text = None
                if block.get("type") == "tool_use":
                    text = describe_tool(block.get("name") or "tool", block.get("input"))
                elif block.get("type") == "text" and (block.get("text") or "").strip():
                    text = clip(block["text"], 160)
                if text:
                    self.activity = {"at": now(), "text": text, "sub": is_sub}
        elif kind == "rate_limit_event":
            self.rate_limit = normalize_rate_limit(ev.get("rate_limit_info") or {})
        elif kind == "result":
            return self._close_turn(ev)
        return None
