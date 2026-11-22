"""The manager: Claude Code with a persona, confined to its desk.

Two ways in, one persona:

  talk   an interactive session for refining specs. It can read everything and run anything, and
         it can write only inside the desk. Three layers hold that line, each for what it can see:
           - the Bash sandbox (Seatbelt on macOS, bubblewrap on Linux) confines shell commands to
             the working directory, which is the desk
           - permissions.blockWritesOutsideWorkingDirectories confines the file tools to it
           - deny rules refuse the commands that change remote state (push, pull requests, ...)
         `org` itself is excluded from the sandbox, because it must write the ledger and reach
         the machines, and goes through permission rules instead: its read commands are allowed,
         and the ones that are a human's decision (approve, dispatch, stop) always ask. That
         prompt is the approval step: the model can propose it, only the person can accept it.

  ask    one headless, read-only answer about the current state, for the dashboard's chat box and
         `org ask`. Nothing that would need permission can run at all.
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
                 "machine ls", "project ls", "ls", "cat", "diff", "pool", "doctor"]
DRAFT_COMMANDS = ["spec new", "spec revise", "project add", "init"]
# Decisions that are the person's. `exec` is among them: it runs anything on an engineer's machine.
HUMAN_COMMANDS = ["approve", "dispatch", "stop", "resume", "machine rm", "exec", "pump", "login"]

# Commands that change state outside this machine. Bash deny rules match the form a model
# normally writes; they are a guard against an overeager manager, not a wall against a hostile
# one. The sandbox and the persona carry the rest.
OUTWARD = [
    "git push", "gh pr create", "gh pr merge", "gh pr close", "gh pr edit", "gh pr comment", "gh pr review", "gh pr reopen",
    "gh issue create", "gh issue close", "gh issue edit", "gh issue comment", "gh issue delete",
    "gh repo create", "gh repo delete", "gh repo edit", "gh repo rename", "gh repo archive", "gh repo fork",
    "gh release", "gh workflow run", "gh run rerun", "gh run cancel", "gh secret", "gh variable", "gh gist create",
]
# The same push, written with options before the verb (git -C <dir> push, git -c k=v push).
OUTWARD_FORMS = ["git * push *", "git * push"]
OUTWARD_API = ["gh api * -X *", "gh api * --method *", "gh api -X *", "gh api --method *", "gh api * -f *", "gh api * -F *",
               "gh api * --field *", "gh api * --raw-field *", "gh api * --input *"]


# Reading is the manager's job, anywhere on the machine. Inside the sandbox every shell command is
# allowed automatically and the operating system stops the writes; where the sandbox is off or not
# available, the harness allows read-only commands only inside the working directory, and a
# manager that has to ask before every `ls` of a checkout is no use. These are the read-only forms.
READ_SHELL = ["cd", "ls", "cat", "head", "tail", "wc", "stat", "file", "du", "tree", "rg", "grep", "find", "jq", "which", "pwd", "date"]
GIT_READ = ["status", "log", "show", "diff", "blame", "ls-files", "ls-remote", "rev-parse", "describe", "shortlog", "grep",
            "branch --list", "branch -a", "branch -r", "branch -vv", "remote -v", "remote show", "remote get-url", "worktree list"]
GH_READ = ["pr view", "pr list", "pr diff", "pr checks", "pr status", "issue view", "issue list", "repo view", "repo list",
           "run view", "run list", "release view", "release list", "search", "api", "auth status"]


def secret_paths() -> list:
    """Where logins live on this side, as permission-rule path patterns."""
    home = Path.home()
    return [str(home / ".claude" / "oauth-token"), str(home / ".claude" / ".credentials.json"),
            str(home / ".config" / "gh") + "/**", str(home / ".ssh") + "/**"]


def _org(commands) -> list:
    return ["Bash(org %s *)" % c for c in commands]


def _read_rules() -> list:
    # No `git -C <dir> <verb>` forms: a wildcard before the verb would also admit options such as
    # `-c`, which can make git run a program. `cd <dir>` and then the plain form does the same job.
    return (["Bash(%s *)" % c for c in READ_SHELL] + ["Bash(git %s *)" % c for c in GIT_READ]
            + ["Bash(gh %s *)" % c for c in GH_READ])


def sandbox_usable() -> bool:
    """Can Claude Code's shell sandbox actually start here?

    macOS has Seatbelt built in. Linux needs bubblewrap and socat, and bubblewrap must be allowed
    to make its namespaces, which a container only permits with the options `org up` gives the
    manager. A probe, because finding out mid-session leaves the shell unusable.
    """
    if sys.platform == "darwin":
        return shutil.which("sandbox-exec") is not None
    if not (shutil.which("bwrap") and shutil.which("socat")):
        return False
    try:
        return subprocess.run(["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--unshare-pid", "--proc", "/proc", "true"],
                              capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def sandboxed(ledger: Ledger) -> bool:
    return bool((ledger.config().get("manager") or {}).get("sandbox", True)) and sandbox_usable()


def session_mode(ledger: Ledger, with_sandbox: Optional[bool] = None) -> Optional[str]:
    """The permission mode a manager session is started in; None leaves Claude Code's default.

    With the sandbox, the operating system stops shell writes outside the desk, so any mode is
    safe, including `auto`, where a classifier answers most prompts. Without it, nothing but a
    prompt stands between the manager and a write to the person's checkouts, and that prompt must
    go to the person, not to a classifier: so the session asks first (`manual`).
    """
    configured = (ledger.config().get("manager") or {}).get("permission_mode")
    if configured:
        return configured
    if with_sandbox is None:
        with_sandbox = sandboxed(ledger)
    return None if with_sandbox else "manual"


def settings(ledger: Ledger, mode: str, with_sandbox: Optional[bool] = None) -> dict:
    """The Claude Code settings layered over the user's own for a manager session."""
    mgr = ledger.config().get("manager") or {}
    desk, root = str(ledger.desk), str(ledger.root)
    if with_sandbox is None:
        with_sandbox = mode == "talk" and sandboxed(ledger)
    perms = {
        # Read(//**) as well as Read: the harness checks the paths given to ls, cat and the like
        # against read rules by path, and a rule without a path does not satisfy that check.
        "allow": ["Read", "Read(//**)", "Grep", "Glob"] + _org(READ_COMMANDS) + _read_rules(),
        "ask": _org(HUMAN_COMMANDS) + ["Bash(orb *)", "Bash(ssh *)"],
        # An absolute path in a rule is written with a second leading slash.
        "deny": ["Bash(%s *)" % c for c in OUTWARD] + ["Bash(%s)" % c for c in OUTWARD_FORMS + OUTWARD_API]
        + ["Edit(/%s/specs/**)" % root, "Edit(/%s/runs/**)" % root, "Edit(/%s/config.json)" % root]
        # The logins this side hands to engineers are not the manager chat's to read or repeat.
        + ["Read(/%s)" % p for p in secret_paths()],
        "blockWritesOutsideWorkingDirectories": True,
    }
    out = {"permissions": perms}
    if mode == "talk":
        perms["allow"] += _org(DRAFT_COMMANDS) + ["Edit(/%s/**)" % desk, "WebSearch", "WebFetch"]
        if with_sandbox:
            out["sandbox"] = {
                "enabled": True,
                # The session was started on the strength of the sandbox: without it, do not run.
                "failIfUnavailable": True,
                "autoAllowBashIfSandboxed": True,
                "allowUnsandboxedCommands": False,
                # Outside the sandbox, and so under the permission rules above instead: org itself
                # (it writes the ledger and reaches the machines) and gh (on macOS its token is in
                # the keychain, which a sandboxed process cannot read). manager.sandbox_exclude
                # adds more, e.g. "git clone *" if private clones fail inside the sandbox.
                "excludedCommands": ["org", "org *", "gh", "gh *"] + list(mgr.get("sandbox_exclude") or []),
                "network": {"allowedDomains": list(mgr.get("network_allow") or [])},
            }
            # The same files, kept from sandboxed shell commands by the operating system.
            secrets = [str(q) for q in (Path.home() / ".claude" / "oauth-token", Path.home() / ".claude" / ".credentials.json",
                                        Path.home() / ".config" / "gh", Path.home() / ".ssh") if q.exists()]
            if secrets:
                out["sandbox"]["filesystem"] = {"denyRead": secrets}
    return out


def launch_env(ledger: Ledger) -> dict:
    env = dict(os.environ)
    env["PATH"] = str(BIN) + os.pathsep + env.get("PATH", "")
    env["ORG_HOME"] = str(ledger.root)
    # On the Mac the manager uses the Mac's own login. Inside an OrbStack machine the only login
    # that works is the long-lived token `box` copies in, and only shells started by zsh export
    # it; a process started any other way falls back to an expired session. Same rule as zshrc.
    token = Path.home() / ".claude" / "oauth-token"
    in_box = os.path.isdir("/opt/orbstack-guest") or env.get("ORG_USE_TOKEN_FILE") == "1"
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


def prepare_container_session(ledger: Ledger, claude: str) -> None:
    """In the manager's own container, skip Claude Code's first-run screens.

    The install inside the image is org's, used for nothing else, so org answers the two questions
    a new install asks: the theme screen, and whether the desk is trusted. On a Mac this does
    nothing: that install and its answers are the person's.
    """
    if not os.environ.get("ORG_IN_CONTAINER"):
        return
    path = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")) / ".claude.json"
    data = read_json(path, {}) or {}
    project = data.setdefault("projects", {}).setdefault(str(ledger.desk), {})
    changed = False
    if not project.get("hasTrustDialogAccepted"):
        project["hasTrustDialogAccepted"], changed = True, True
    if not data.get("hasCompletedOnboarding"):
        try:
            version = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=20).stdout.split()[0]
        except (OSError, IndexError, subprocess.TimeoutExpired):
            version = "0"
        data.update(hasCompletedOnboarding=True, lastOnboardingVersion=version)
        changed = True
    if changed:
        write_json(path, data)
        os.chmod(path, 0o600)


def talk(ledger: Ledger, prompt: Optional[str] = None, project: Optional[str] = None, resume: bool = False,
         dry_run: bool = False) -> int:
    """Replace this process with the manager's interactive session."""
    ledger.init()
    claude = _claude(ledger)
    if not claude:
        print("org: the claude CLI is not on PATH", file=sys.stderr)
        return 127
    with_sandbox = sandboxed(ledger)
    mode = session_mode(ledger, with_sandbox)
    if dry_run:
        print("would run, in %s:\n  %s --append-system-prompt <persona, %d characters> --settings <below> --name 'org manager'%s\n"
              % (ledger.desk, claude, len(persona(ledger)), (" --permission-mode " + mode) if mode else ""))
        print("shell sandbox: %s\n" % ("on" if with_sandbox else "not available here, so the session asks before shell commands that are not reads"))
        print(json.dumps(settings(ledger, "talk", with_sandbox), indent=2))
        return 0
    try:
        sync_all(ledger)  # so the persona opens with what the machines say now
    except Exception:
        pass
    prepare_container_session(ledger, claude)
    mgr = ledger.config().get("manager") or {}
    if not with_sandbox and mgr.get("sandbox", True):
        print("org: no shell sandbox on this machine; the manager will ask before any shell command that is not a read.\n"
              "     `org doctor` says what is missing.", file=sys.stderr)
    cmd = [claude, "--append-system-prompt", persona(ledger), "--settings", json.dumps(settings(ledger, "talk", with_sandbox)),
           "--name", "org manager"]
    for flag, key in (("--model", "model"), ("--effort", "effort")):
        if mgr.get(key):
            cmd += [flag, str(mgr[key])]
    if mode:
        cmd += ["--permission-mode", mode]
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
            print("org: %s" % out["error"], file=sys.stderr)
        return 1
    return 0
