#!/usr/bin/env python3
"""A stand-in for the `claude` CLI, for testing the runner without spending tokens.

It accepts the flags the runner passes, reads the prompt on stdin, and writes stream-json events
in the shapes Claude Code 2.1.322 emits. What it "does" each turn comes from a scenario file named
by $FAKE_CLAUDE_SCENARIO:

    {
      "utilization": 0.2,
      "turns": {
        "plan":         {"structured": {...}, "files": {"$RUN/plan.md": "..."}, "cost": 0.5},
        "milestone:M1": [{"die": true}, {"structured": {...}}],     <- one entry per attempt
        "report":       {"structured": {...}}
      }
    }

A turn entry may hold: structured, files, cost, sleep, die, prose, limit (seconds until reset),
budget, subagents, no_session. Every invocation is appended to invocations.jsonl beside the
scenario, so tests can assert on the flags and prompts the runner used.
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path

MODELS = {"fable": "claude-fable-5-2", "opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}
VALUE_FLAGS = {
    "--output-format", "--model", "--effort", "--append-system-prompt", "--json-schema", "--session-id",
    "--resume", "--max-budget-usd", "--settings", "--permission-mode", "--allowedTools", "--add-dir",
    "--append-system-prompt-file", "--input-format", "--setting-sources",
}


def parse(argv):
    opts, flags, rest, i = {}, set(), [], 0
    while i < len(argv):
        a = argv[i]
        if a in VALUE_FLAGS and i + 1 < len(argv):
            opts[a] = argv[i + 1]
            i += 2
        elif a.startswith("-"):
            flags.add(a)
            i += 1
        else:
            rest.append(a)
            i += 1
    return opts, flags, rest


def label_of(prompt):
    """Which turn is this? Nudges and recoveries quote the original instruction, so search, don't match the start."""
    import re

    m = re.search(r"Execute milestone `([^`]+)`", prompt)
    if m:
        return "milestone:" + m.group(1)
    if "The plan cannot be accepted" in prompt:
        return "plan-repair"
    if "The run is ending." in prompt:
        return "report"
    if "Plan the implementation of the spec" in prompt:
        return "plan"
    if "Question:" in prompt:
        return "ask"
    return "other"


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    opts, flags, rest = parse(sys.argv[1:])
    prompt = sys.stdin.read() if not rest else " ".join(rest)
    scen_path = Path(os.environ["FAKE_CLAUDE_SCENARIO"])
    scenario = json.loads(scen_path.read_text())
    home = scen_path.parent
    sessions = home / "sessions"
    sessions.mkdir(exist_ok=True)
    label = label_of(prompt)

    counts_path = home / "counts.json"
    counts = json.loads(counts_path.read_text()) if counts_path.exists() else {}
    attempt = counts.get(label, 0)
    counts[label] = attempt + 1
    counts_path.write_text(json.dumps(counts))

    entry = (scenario.get("turns") or {}).get(label, {})
    if isinstance(entry, list):
        entry = entry[min(attempt, len(entry) - 1)]

    with open(home / "invocations.jsonl", "a") as f:
        f.write(json.dumps({"label": label, "attempt": attempt, "opts": {k: v for k, v in opts.items() if k != "--append-system-prompt"},
                            "flags": sorted(flags), "prompt": prompt, "cwd": os.getcwd(),
                            "system_prompt": opts.get("--append-system-prompt", "")[:200],
                            "env": {k: os.environ.get(k) for k in ("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", "IS_SANDBOX", "ORG_RUN_DIR")}}) + "\n")

    resume = opts.get("--resume")
    sid = resume or opts.get("--session-id") or str(uuid.uuid4())
    if resume and (not (sessions / resume).exists() or entry.get("no_session")):
        sys.stderr.write("No conversation found with session ID: %s\n" % resume)
        return 1
    if opts.get("--session-id") and (sessions / sid).exists():
        sys.stderr.write("Error: Session ID %s is already in use.\n" % sid)
        return 1
    # As in the real CLI, a session's cost and usage are cumulative across resumed invocations.
    prior = json.loads((sessions / sid).read_text()) if (sessions / sid).exists() else {"cost": 0.0, "turns": 0}
    (sessions / sid).write_text(json.dumps(prior))

    model = MODELS.get(opts.get("--model", "fable"), opts.get("--model", "claude-fable-5-2"))
    run_dir = os.environ.get("ORG_RUN_DIR", "")
    emit({"type": "system", "subtype": "init", "session_id": sid, "model": model, "cwd": os.getcwd(), "tools": []})

    util = scenario.get("utilization", 0.2) + 0.01 * sum(counts.values())
    resets = int(time.time()) + 36 * 3600
    if entry.get("limit") is not None:
        resets = int(time.time()) + int(entry["limit"])
        emit({"type": "rate_limit_event", "session_id": sid, "rate_limit_info": {
            "status": "rejected", "resetsAt": resets, "rateLimitType": "seven_day",
            "unifiedWindows": {"seven_day": {"utilization": 1.0, "resetsAt": resets, "status": "rejected"}}}})
        emit({"type": "result", "subtype": "success", "is_error": True, "session_id": sid, "num_turns": 1, "duration_ms": 5,
              "result": "You've hit your weekly limit · resets soon", "total_cost_usd": 0.0, "modelUsage": {},
              "model_usage_since_last_result": {}})
        return 1
    emit({"type": "rate_limit_event", "session_id": sid, "rate_limit_info": {
        "status": "allowed", "resetsAt": resets, "rateLimitType": "seven_day",
        "unifiedWindows": {"seven_day": {"utilization": round(util, 4), "resetsAt": resets, "status": "allowed"}}}})

    usage = {"input_tokens": 10, "output_tokens": 500, "cache_read_input_tokens": 20000, "cache_creation_input_tokens": 3000,
             "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 3000}}
    emit({"type": "assistant", "session_id": sid, "parent_tool_use_id": None, "message": {
        "id": "msg_" + uuid.uuid4().hex[:12], "model": model, "usage": usage,
        "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "echo working on " + label}}]}})

    for n in range(int(entry.get("subagents", 0))):
        tid = "task%d" % n
        emit({"type": "system", "subtype": "task_started", "task_id": tid, "description": "worker %d" % n, "subagent_type": "general-purpose", "session_id": sid})
        emit({"type": "assistant", "session_id": sid, "parent_tool_use_id": "toolu_agent%d" % n, "message": {
            "id": "msg_" + uuid.uuid4().hex[:12], "model": MODELS["haiku"],
            "usage": {"input_tokens": 5, "output_tokens": 100, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 1000},
            "content": [{"type": "text", "text": "subagent %d finished its part" % n}]}})
        emit({"type": "system", "subtype": "task_notification", "task_id": tid, "status": "completed", "session_id": sid,
              "usage": {"total_tokens": 1105, "tool_uses": 1, "duration_ms": 10}})

    if entry.get("die"):
        sys.stderr.write("fake claude: simulated crash\n")
        return 1

    for path, content in (entry.get("files") or {}).items():
        p = Path(path.replace("$RUN", run_dir).replace("$CWD", os.getcwd()))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    slept = 0.0
    while slept < float(entry.get("sleep", 0)):
        time.sleep(0.1)
        slept += 0.1

    cost = float(entry.get("cost", 0.25))
    total, turns = prior["cost"] + cost, prior["turns"] + 1
    (sessions / sid).write_text(json.dumps({"cost": total, "turns": turns}))
    this_turn = {model: {"inputTokens": 10, "outputTokens": 500, "cacheReadInputTokens": 20000,
                         "cacheCreationInputTokens": 3000, "costUSD": cost}}
    so_far = {model: {"inputTokens": 10 * turns, "outputTokens": 500 * turns, "cacheReadInputTokens": 20000 * turns,
                      "cacheCreationInputTokens": 3000 * turns, "costUSD": total}}
    base = {"type": "result", "session_id": sid, "num_turns": 3, "duration_ms": 1200, "total_cost_usd": total,
            "modelUsage": so_far, "permission_denials": []}
    if not scenario.get("no_since"):
        base["model_usage_since_last_result"] = this_turn
    if entry.get("budget"):
        emit(dict(base, subtype="error_max_budget_usd", is_error=True, result="Reached maximum budget"))
        return 1
    if entry.get("say"):
        # The manager's question mode: text streamed as partial-message deltas, then the result.
        words = entry["say"].split(" ")
        if "--include-partial-messages" in flags:
            for i, w in enumerate(words):
                emit({"type": "stream_event", "session_id": sid, "parent_tool_use_id": None, "event": {
                    "type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": w + (" " if i < len(words) - 1 else "")}}})
        emit(dict(base, subtype="success", is_error=False, result=entry["say"]))
        return 0
    if entry.get("prose") or "structured" not in entry:
        emit(dict(base, subtype="success", is_error=False, result=entry.get("prose") or "Done. Want me to continue?"))
        return 0
    emit({"type": "assistant", "session_id": sid, "parent_tool_use_id": None, "message": {
        "id": "msg_" + uuid.uuid4().hex[:12], "model": model, "usage": usage,
        "content": [{"type": "tool_use", "id": "toolu_2", "name": "StructuredOutput", "input": entry["structured"]}]}})
    emit(dict(base, subtype="success", is_error=False, structured_output=entry["structured"], result=json.dumps(entry["structured"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
