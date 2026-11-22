"""The manager: Claude Code with a persona, confined to its desk.

Two ways in, one persona:

  talk   an interactive session for refining specs. It can read everything and run anything, and
         it can write only inside the desk. Three layers hold that line, each for what it can see:
           - the Bash sandbox (Seatbelt on macOS, bubblewrap on Linux) confines shell commands to
             the working directory, which is the desk
           - permissions.blockWritesOutsideWorkingDirectories confines the file tools to it
           - deny rules refuse the commands that change remote state (push, pull requests, ...)
         `crew` itself is excluded from the sandbox, because it must write the ledger and reach
         the machines, and goes through permission rules instead: its read commands are allowed,
         and the ones that are a human's decision (approve, dispatch, stop) always ask. That
         prompt is the approval step: the model can propose it, only the person can accept it.

  ask    one headless, read-only answer about the current state, for the dashboard's chat box and
         `crew ask`. Nothing that would need permission can run at all.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Callable, Optional

from . import status
from .ledger import Ledger
from .sync import sync_all
from .util import fmt_stamp, now, read_json, read_text, render, write_json

PROMPTS = Path(__file__).parent / "prompts"
BIN = Path(__file__).resolve().parent.parent / "bin"

READ_COMMANDS = ["status", "peek", "logs", "plan", "report", "review", "sync", "spec ls", "spec show", "spec check",
                 "machine ls", "project ls"]
DRAFT_COMMANDS = ["spec new", "spec revise", "project add", "init"]
HUMAN_COMMANDS = ["approve", "dispatch", "stop", "resume", "machine rm"]

# Commands that change state outside this machine. Bash deny rules match the form a model
# normally writes; they are a guard against an overeager manager, not a wall against a hostile
# one. The sandbox and the persona carry the rest.
OUTWARD = [
    "git push", "gh pr create", "gh pr merge", "gh pr close", "gh pr edit", "gh pr comment", "gh pr review", "gh pr reopen",
    "gh issue create", "gh issue close", "gh issue edit", "gh issue comment", "gh issue delete",
    "gh repo create", "gh repo delete", "gh repo edit", "gh repo rename", "gh repo archive", "gh repo fork",
    "gh release", "gh workflow run", "gh run rerun", "gh run cancel", "gh secret", "gh variable", "gh gist create",
]
OUTWARD_API = ["gh api * -X *", "gh api * --method *", "gh api -X *", "gh api --method *", "gh api * -f *", "gh api * -F *",
               "gh api * --field *", "gh api * --raw-field *", "gh api * --input *"]


def _crew(commands) -> list:
    return ["Bash(crew %s *)" % c for c in commands]


def settings(ledger: Ledger, mode: str) -> dict:
    """The Claude Code settings layered over the user's own for a manager session."""
    mgr = ledger.config().get("manager") or {}
    desk, root = str(ledger.desk), str(ledger.root)
    perms = {
        "allow": ["Read", "Grep", "Glob"] + _crew(READ_COMMANDS),
        "ask": _crew(HUMAN_COMMANDS) + ["Bash(orb *)", "Bash(ssh *)"],
        # An absolute path in a rule is written with a second leading slash.
        "deny": ["Bash(%s *)" % c for c in OUTWARD] + ["Bash(%s)" % c for c in OUTWARD_API]
        + ["Edit(/%s/specs/**)" % root, "Edit(/%s/runs/**)" % root, "Edit(/%s/config.json)" % root],
        "blockWritesOutsideWorkingDirectories": True,
    }
    out = {"permissions": perms}
    if mode == "talk":
        perms["allow"] += _crew(DRAFT_COMMANDS) + ["Edit(/%s/**)" % desk, "WebSearch", "WebFetch"]
        if mgr.get("sandbox", True):
            out["sandbox"] = {
                "enabled": True,
                "autoAllowBashIfSandboxed": True,
                "allowUnsandboxedCommands": False,
                "excludedCommands": ["crew", "crew *"],
                "network": {"allowedDomains": list(mgr.get("network_allow") or [])},
            }
    return out


def launch_env(ledger: Ledger) -> dict:
    env = dict(os.environ)
    env["PATH"] = str(BIN) + os.pathsep + env.get("PATH", "")
    env["CREW_HOME"] = str(ledger.root)
    # On the Mac the manager uses the Mac's own login. Inside an OrbStack machine the only login
    # that works is the long-lived token `box` copies in, and only shells started by zsh export
    # it; a process started any other way falls back to an expired session. Same rule as zshrc.
    token = Path.home() / ".claude" / "oauth-token"
    in_box = os.path.isdir("/opt/orbstack-guest") or env.get("CREW_USE_TOKEN_FILE") == "1"
    if in_box and not env.get("CLAUDE_CODE_OAUTH_TOKEN") and not env.get("ANTHROPIC_API_KEY") and token.is_file():
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token.read_text().strip()
    return env


def persona(ledger: Ledger, live_state: bool = True, extra: str = "") -> str:
    if live_state:
        state = status.render(status.collect(ledger, do_sync=False), width=160)
        projects = ledger.projects()
        if projects:
            state += "\n\nPROJECTS\n" + "\n".join(
                "%s: %s%s" % (name, ", ".join(p.get("repos") or []) or "(no repos yet)",
                              ("   local checkout: " + p["path"]) if p.get("path") else "")
                for name, p in sorted(projects.items()))
    else:
        state = "(sent with each question)"
    return render(read_text(PROMPTS / "manager.md"), desk=ledger.desk, root=ledger.root, state=state) + extra


def _claude(ledger: Ledger) -> Optional[str]:
    return shutil.which(ledger.config().get("claude_bin") or "claude", path=launch_env(ledger)["PATH"])


def talk(ledger: Ledger, prompt: Optional[str] = None, project: Optional[str] = None, resume: bool = False) -> int:
    """Replace this process with the manager's interactive session."""
    ledger.init()
    claude = _claude(ledger)
    if not claude:
        print("crew: the claude CLI is not on PATH", file=sys.stderr)
        return 127
    try:
        sync_all(ledger)  # so the persona opens with what the machines say now
    except Exception:
        pass
    mgr = ledger.config().get("manager") or {}
    cmd = [claude, "--append-system-prompt", persona(ledger), "--settings", json.dumps(settings(ledger, "talk")),
           "--name", "crew manager"]
    for flag, key in (("--model", "model"), ("--effort", "effort"), ("--permission-mode", "permission_mode")):
        if mgr.get(key):
            cmd += [flag, str(mgr[key])]
    if resume:
        cmd.append("--continue")
    if project and prompt:
        prompt = "Project: %s. %s" % (project, prompt)
    elif project:
        prompt = "This conversation is about the project %s." % project
    if prompt:
        cmd.append(prompt)
    os.chdir(ledger.desk)
    os.execve(claude, cmd, launch_env(ledger))
    return 0  # not reached


def ask(ledger: Ledger, question: str, on_text: Optional[Callable[[str], None]] = None, timeout: float = 600) -> dict:
    """One read-only answer about the current state. Streams text to on_text as it arrives."""
    ledger.init()
    claude = _claude(ledger)
    if not claude:
        return {"text": "", "error": "the claude CLI is not on PATH"}
    mgr = ledger.config().get("manager") or {}
    state = status.collect(ledger, do_sync=True)
    message = "State as of %s:\n\n```\n%s\n```\n\nQuestion: %s" % (fmt_stamp(now()), status.render(state, width=200), question.strip())
    session_path = ledger.chat / "session.json"
    session = read_json(session_path, {}) or {}

    def attempt(session_id: Optional[str]) -> dict:
        sid = session_id or str(uuid.uuid4())
        cmd = [claude, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
               "--append-system-prompt", persona(ledger, live_state=False, extra=read_text(PROMPTS / "ask.md")),
               "--settings", json.dumps(settings(ledger, "ask")), "--permission-mode", "dontAsk",
               "--model", str(mgr.get("ask_model") or "sonnet"), "--effort", str(mgr.get("ask_effort") or "low")]
        cmd += ["--resume", sid] if session_id else ["--session-id", sid]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=str(ledger.root), env=launch_env(ledger))
        try:
            proc.stdin.write(message.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        text, streamed, final, started = [], False, None, False
        for raw in proc.stdout:
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            kind = ev.get("type")
            if kind == "system" and ev.get("subtype") == "init":
                started = True
                sid = ev.get("session_id") or sid
            elif kind == "stream_event" and not ev.get("parent_tool_use_id"):
                inner = ev.get("event") or {}
                delta = inner.get("delta") or {}
                if inner.get("type") == "content_block_delta" and delta.get("type") == "text_delta" and delta.get("text"):
                    streamed = True
                    text.append(delta["text"])
                    if on_text:
                        on_text(delta["text"])
                elif inner.get("type") == "content_block_stop" and streamed and text and not text[-1].endswith("\n"):
                    text.append("\n")  # a paragraph break between text blocks that tool calls separate
                    if on_text:
                        on_text("\n")
            elif kind == "assistant" and not streamed and not ev.get("parent_tool_use_id"):
                for block in (ev.get("message") or {}).get("content") or []:
                    if block.get("type") == "text" and block.get("text"):
                        text.append(block["text"])
                        if on_text:
                            on_text(block["text"])
            elif kind == "result":
                final = ev
        err = proc.stderr.read().decode("utf-8", "replace")
        proc.stdout.close()
        proc.stderr.close()
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = -9
        return {"sid": sid, "started": started, "final": final, "text": "".join(text).strip(), "rc": rc, "stderr": err.strip()[-400:]}

    out = attempt(session.get("id"))
    if not out["started"] and session.get("id"):
        out = attempt(None)  # the earlier conversation could not be resumed: start a new one
    final = out["final"] or {}
    text = out["text"] or (final.get("result") or "")
    cost, error = 0.0, None
    if final.get("is_error") or not out["started"] or not final or (not text and out["rc"] != 0):
        error = (final.get("result") if final.get("is_error") else None) or out["stderr"] or "the manager did not answer"
    else:
        # Only a conversation that answered is worth resuming next time.
        per_turn = final.get("model_usage_since_last_result") or {}
        cost = sum((m.get("costUSD") or 0) for m in per_turn.values())
        same = out["sid"] == session.get("id")
        write_json(session_path, {"id": out["sid"], "questions": (session.get("questions", 0) if same else 0) + 1,
                                  "cost_usd": (session.get("cost_usd", 0.0) if same else 0.0) + cost, "last_at": now()})
    return {"text": text, "error": error, "cost_usd": cost, "session_id": out["sid"]}


def ask_cli(ledger: Ledger, question: str) -> int:
    def show(chunk: str) -> None:
        sys.stdout.write(chunk)
        sys.stdout.flush()

    out = ask(ledger, question, on_text=show)
    sys.stdout.write("\n")
    if out.get("error"):
        if out["error"].strip() != (out.get("text") or "").strip():
            print("crew: %s" % out["error"], file=sys.stderr)
        return 1
    return 0
