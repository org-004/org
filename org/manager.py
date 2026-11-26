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

from . import secrets, status
from .ledger import Ledger
from .sync import sync_all
from .util import fmt_stamp, now, read_json, read_text, render, write_json

PROMPTS = Path(__file__).parent / "prompts"
BIN = Path(__file__).resolve().parent.parent / "bin"

READ_COMMANDS = ["status", "peek", "logs", "plan", "report", "review", "sync", "spec ls", "spec show", "spec check",
                 "machine ls", "project ls", "ls", "cat", "diff", "pool", "doctor", "config get", "node ls", "repos", "graph"]
# What a conversation may do by itself: drafts, the list of projects, a scratch copy of a repository.
DRAFT_COMMANDS = ["spec new", "spec revise", "project add", "clone", "init"]
# Decisions that are the person's. `exec` is among them: it runs anything on an engineer's machine.
# So are the commands that change where engineers run or how many, and the installation itself.
# `drain`, `undrain` and `upgrade` decide when work runs and which code runs it.
HUMAN_COMMANDS = ["approve", "dispatch", "stop", "resume", "machine rm", "exec", "pump", "login",
                  "config set", "node add", "node rm", "up", "down", "drain", "undrain", "upgrade", "view",
                  # `release` starts a held run of a plan whatever it waited for. `notify` sends a
                  # message out, and `review-run` is the review's own process, which only org starts.
                  "release", "notify", "review-run"]

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
# What stands between the chat and the machine that holds it. The chat has no use for either:
# engineers' containers are reached with `org ls`, `org cat` and `org exec`.
HOST_COMMANDS = ["incus", "sudo", "su", "docker"]
# What is kept of those when the owner has set `manager.direct`: root owns the files that keep
# this machine from the owner's own computer, so root stays out of reach. Incus and Docker
# then divide the machine for the manager; they are not a wall.
ROOT_COMMANDS = ["sudo", "su"]
# How git finds the organization's token in a direct session: a credential helper for
# github.com alone, given through the session's environment and written to no file.
GIT_HELPER = '!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f'


def secret_paths() -> list:
    """Where logins live on this side, as permission-rule path patterns."""
    return secrets.rule_patterns()


def _org(commands) -> list:
    # With arguments and without: `org drain` and `org drain --wait` are the same decision.
    return [rule % c for c in commands for rule in ("Bash(org %s)", "Bash(org %s *)")]


def _read_rules() -> list:
    # No `git -C <dir> <verb>` forms: a wildcard before the verb would also admit options such as
    # `-c`, which can make git run a program. `cd <dir>` and then the plain form does the same job.
    return ["Bash(%s *)" % c for c in READ_SHELL] + ["Bash(git %s *)" % c for c in GIT_READ]


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


def direct(ledger: Ledger) -> bool:
    """Whether the owner has set `manager.direct`: the chat may then change the organization's
    repositories itself. It runs without the shell sandbox, so every shell command that is not
    a read asks the person; pushes and the other outward commands are no longer denied; and
    the session is given the organization's GitHub token. Off unless the owner turns it on."""
    return bool((ledger.config().get("manager") or {}).get("direct"))


def unasked(ledger: Ledger) -> bool:
    """Whether a direct chat runs without questions, as an engineer's session does: the owner
    has set `manager.direct` and `manager.permission_mode` to `bypassPermissions`. The machine
    is then the only wall, which is the owner's decision to make and nobody else's."""
    return direct(ledger) and (ledger.config().get("manager") or {}).get("permission_mode") == "bypassPermissions"


def sandboxed(ledger: Ledger) -> bool:
    if direct(ledger):
        return False  # a sandboxed shell has no login and cannot write a clone outside the desk
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
    # `manager.direct` changes the chat only: a question from the dashboard or a review stays
    # read-only whatever it says.
    direct_mode = mode == "talk" and direct(ledger)
    if direct_mode:
        with_sandbox = False
    if with_sandbox is None:
        with_sandbox = mode == "talk" and sandboxed(ledger)
    free = direct_mode and unasked(ledger)
    outward = [] if direct_mode else ["Bash(%s *)" % c for c in OUTWARD] + ["Bash(%s)" % c for c in OUTWARD_FORMS + OUTWARD_API]
    host = ROOT_COMMANDS if direct_mode else HOST_COMMANDS
    perms = {
        # Read(//**) as well as Read: the harness checks the paths given to ls, cat and the like
        # against read rules by path, and a rule without a path does not satisfy that check.
        "allow": ["Read", "Read(//**)", "Grep", "Glob"] + _org(READ_COMMANDS) + _read_rules(),
        # An ask rule asks in every permission mode, so a chat that is to run without
        # questions has none.
        "ask": [] if free else _org(HUMAN_COMMANDS) + ["Bash(ssh *)"],
        # An absolute path in a rule is written with a second leading slash.
        "deny": outward
        + ["Edit(/%s/specs/**)" % root, "Edit(/%s/runs/**)" % root, "Edit(/%s/config.json)" % root]
        # The logins this side hands to engineers are not a confined chat's to read or repeat.
        # A direct chat already holds both tokens in its environment, and the owner gave it
        # the files too: "The logins on this machine are yours to read".
        + ([] if direct_mode else ["Read(/%s)" % p for p in secret_paths()])
        # No MCP server is configured here and none may be: a tool from one would be a way out
        # of this machine that no rule above describes.
        + ["mcp__*"]
        + ["Bash(%s *)" % c for c in host] + ["Bash(%s)" % c for c in host],
        # In a direct session a file written outside the desk asks the person instead.
        "blockWritesOutsideWorkingDirectories": not direct_mode,
    }
    out = {"permissions": perms, "disableClaudeAiConnectors": True}
    if mode == "talk":
        perms["allow"] += _org(DRAFT_COMMANDS) + ["Edit(/%s/**)" % desk, "WebSearch", "WebFetch"]
        if with_sandbox:
            out["sandbox"] = {
                "enabled": True,
                # The session was started on the strength of the sandbox: without it, do not run.
                "failIfUnavailable": True,
                "autoAllowBashIfSandboxed": True,
                "allowUnsandboxedCommands": False,
                # Outside the sandbox, and so under the permission rules above instead: org itself,
                # which writes the ledger and reaches the engineers' containers. Nothing else:
                # the chat has no GitHub login to keep out of the sandbox, because every
                # repository it works with is public. manager.sandbox_exclude adds more.
                "excludedCommands": ["org", "org *"] + list(mgr.get("sandbox_exclude") or []),
                "network": {"allowedDomains": list(mgr.get("network_allow") or [])},
            }
            # The same files, kept from sandboxed shell commands by the operating system.
            # So is Incus's directory, which holds its sockets: whoever can write to one can enter
            # every engineer's container. The directory and not the sockets: this user cannot
            # list it, and the sandbox can cover a directory it can reach but not a file inside
            # one it cannot read, so naming the sockets stops the sandbox from starting at all.
            hidden = [q for q in secrets.all_paths() + ["/var/lib/incus"] if os.path.exists(q)]
            if hidden:
                out["sandbox"]["filesystem"] = {"denyRead": hidden}
    return out


def launch_env(ledger: Ledger, github: bool = False) -> dict:
    env = dict(os.environ)
    # ~/.local/bin is where Claude Code installs itself, and a service or a command passed in
    # from the host has only the PATH it was given.
    env["PATH"] = os.pathsep.join([str(BIN), str(Path.home() / ".local" / "bin"), env.get("PATH", "")])
    env["ORG_HOME"] = str(ledger.root)
    # The manager's session makes model requests with the token this installation was handed.
    # It is never given the GitHub token: the chat reads public repositories and pushes nothing.
    token = secrets.claude_token()
    if token and not env.get("CLAUDE_CODE_OAUTH_TOKEN") and not env.get("ANTHROPIC_API_KEY"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"):
        env.pop(name, None)
    # A session started from inside a direct chat inherits that chat's environment: take the
    # credential helper out again, so that only a session meant to have it has it, once.
    kept = [(env.get("GIT_CONFIG_KEY_%d" % i), env.get("GIT_CONFIG_VALUE_%d" % i)) for i in range(int(env.get("GIT_CONFIG_COUNT") or 0))]
    for name in [k for k in env if k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))] + ["GIT_CONFIG_COUNT"]:
        env.pop(name, None)
    kept = [(k, v) for k, v in kept if k is not None and v is not None and v != GIT_HELPER]
    for i, (k, v) in enumerate(kept):
        env["GIT_CONFIG_KEY_%d" % i], env["GIT_CONFIG_VALUE_%d" % i] = k, v
    if kept:
        env["GIT_CONFIG_COUNT"] = str(len(kept))
    if github:
        # Only a direct chat (`manager.direct`) is started this way: it is given the token that
        # engineers are given, which reaches the repositories of one organization, and git is
        # told to use it for github.com.
        github_token = secrets.github_token()
        if github_token:
            env["GH_TOKEN"] = github_token
            n = int(env.get("GIT_CONFIG_COUNT") or 0)
            env["GIT_CONFIG_KEY_%d" % n] = "credential.https://github.com.helper"
            env["GIT_CONFIG_VALUE_%d" % n] = GIT_HELPER
            env["GIT_CONFIG_COUNT"] = str(n + 1)
    # No claude.ai connectors, whatever the account has attached. A token from `claude
    # setup-token` cannot fetch them anyway; this holds if a wider login ever lands here.
    env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
    return env


def machines_help(cfg: dict) -> str:
    """The sentence of the persona that says what a spec's `machine` may be, from what is configured here."""
    from . import backends

    machines = cfg.get("machines") or {}
    default = (machines.get("default") or "incus").split(":")[0]
    try:
        kinds = backends.kinds(cfg)
    except backends.BackendError:
        kinds = [default]
    known = {
        "incus": "`incus` (a fresh container for the run, copied from the template)",
        "docker": "`docker` (a fresh container; `docker:<node>` names a Docker host from `org pool`)",
    }
    parts = [known.get(default, "`%s`" % default) + ", the default"]
    parts += [known[k] for k in ("incus", "docker") if k in kinds and k != default]
    ssh = sorted(machines.get("ssh") or {})
    if ssh:
        parts.append("`ssh:<name>` for a remote machine that is configured (%s); choose one only when the work needs what "
                     "the default cannot give" % ", ".join("`ssh:%s`" % n for n in ssh))
    if (machines.get("local") or {}).get("enabled"):
        parts.append("`local` (this machine, with no isolation)")
    text = "`machine` is " + "; or ".join(parts) + "."
    return text + " Leave it out unless there is a reason: the default is what this installation is set up for."


# Said only to a session whose shell is sandboxed.
SANDBOX_NOTE = """
- Your shell commands run in a sandbox with no GitHub login, and there is none to find: every repository you work with is public, so `git clone`, `git fetch` and `curl -s https://api.github.com/repos/<owner>/<name>/pulls` read all you need. `org repos` lists the organization's repositories and `org clone <owner/name>` puts a copy in your scratch space. You cannot push, comment or open anything on GitHub, and `gh` will only tell you nobody is logged in. An `org` command must be the whole command: no pipe, no redirect, no `;` or `&&`, no `cd` in front, or it runs inside the sandbox and one that writes fails."""


DIRECT_NOTE = """

## What is yours to do (the owner has set `manager.direct`)

- You may change a repository of the organization yourself when that is the quickest way to a verified result: a small fix, a document, a configuration value, a failed check with a plain cause. Dispatch an engineer when the work needs a long build or test, or more than about half an hour.
- Work in a clone of your own under `%(desk)s/work/<name>`, made with `git clone` (a scratch clone from `org clone` is given a push address that refuses). Follow the repository's own rules (`AGENTS.md`), run its checks, commit as its log does, and push. Your shell has the organization's token as `GH_TOKEN`, and git uses it for github.com.
%(asks)s
- The logins kept on this machine (the Claude token, the organization's GitHub token) are yours to read and use. The repositories are public: a token is never written into a commit, a spec, a brief, a report or a log, and never sent anywhere but the service it belongs to.
- After you push, read the checks on the commit and say what they show."""


DIRECT_ASKS = ("- There is no shell sandbox in this mode: every shell command that is not a read asks the owner, and so does a file written outside the desk. "
               "Say in a few words what a command is for before you run it. `org approve`, `org release`, `org stop`, `org exec` and `org upgrade` ask too, "
               "so a spec is dispatched when the owner confirms the command: tell them what it builds, its caps and the assumption you consider riskiest.")
DIRECT_FREE = ("- Nothing asks the owner in this mode: there is no shell sandbox and no question before a command, an approval, a dispatch or an upgrade, as in an engineer's session. "
               "The machine is yours and it is the only wall. So you are the check: read a command before you run it, look at what you are about to delete or overwrite, "
               "verify before you push, and say afterwards what you did. `org approve`, `org release`, `org dispatch`, `org stop`, `org exec` and `org upgrade` are yours to run when the work calls for them: "
               "a spec of yours that `org spec check` reports ready is approved by you, and the journal says why it was worth its caps. "
               "Never pass `--skip-tests` to an upgrade.")


# Said to a chat that is not direct: the owner approves, and the session changes nothing outside its desk.
CONFINED_NOTE = """

## What is yours to do (`manager.direct` is not set)

- The logins kept on this machine are not yours to read.
- You write only inside `%(desk)s`. Reading is allowed everywhere: to read a checkout elsewhere on the machine, `cd` into it and use plain commands.
- You never change project state: no commits, pushes, issues, merges or releases, and nothing on an engineer's machine. Something you are about to fix is a requirement for a spec. If testing a hypothesis needs a change to exist, make it in a scratch clone and discard it.
- Approval is the owner's act, bound to the exact text they read. Present the draft: its path, what will be built and what will not, the caps, the assumption you consider riskiest, and the sha256 prefix that `org spec check` printed. Only when they clearly approve that text, run `org approve <spec> --sha <prefix>`; Claude Code asks them to confirm the command, and that confirmation is the approval. Each spec of a plan is approved on its own text. Enthusiasm about an idea is not approval of a text.
- `org release`, `org stop` and `org exec` ask the owner in the same way. A paused plan is not got moving by a release you suggest: say what the reason was and what you would change."""


def direct_note(ledger: Ledger) -> str:
    """What the persona gains in a direct chat; the empty string otherwise."""
    if not direct(ledger):
        return ""
    return DIRECT_NOTE % {"desk": ledger.desk, "asks": DIRECT_FREE if unasked(ledger) else DIRECT_ASKS}


def mode_note(ledger: Ledger) -> str:
    """The last section of a chat's persona: what the session may do itself, by `manager.direct`."""
    return direct_note(ledger) or CONFINED_NOTE % {"desk": ledger.desk}


def persona(ledger: Ledger, live_state: bool = True, extra: str = "", with_sandbox: bool = False) -> str:
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
    return render(read_text(PROMPTS / "manager.md"), desk=ledger.desk, root=ledger.root, state=state, guide=PROMPTS / "manager",
                  machines=machines_help(ledger.config()), sandbox_note=SANDBOX_NOTE if with_sandbox else "") + extra


def _claude(ledger: Ledger) -> Optional[str]:
    return shutil.which(ledger.config().get("claude_bin") or "claude", path=launch_env(ledger)["PATH"])


def prepare_own_session(ledger: Ledger, claude: str) -> None:
    """In a manager that `org up` made, skip Claude Code's first-run screens.

    The install inside the manager's container or machine is org's, used for nothing else, so org
    answers the two questions a new install asks: the theme screen, and whether the desk is
    trusted. `org up` marks such a manager (ORG_IN_CONTAINER in the image, manager.own_claude in a
    machine's config). On a Mac this does nothing: that install and its answers are the person's.
    """
    if not (os.environ.get("ORG_IN_CONTAINER") or (ledger.config().get("manager") or {}).get("own_claude")):
        return
    # Claude Code keeps this file in its config directory when one is set, and in the home otherwise.
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    path = (Path(config_dir) if config_dir else Path.home()) / ".claude.json"
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
    direct_mode = direct(ledger)
    note = mode_note(ledger)
    mode = session_mode(ledger, with_sandbox)
    if dry_run:
        print("would run, in %s:\n  %s --append-system-prompt <persona, %d characters> --settings <below> --name 'org manager'%s\n"
              % (ledger.desk, claude, len(persona(ledger, with_sandbox=with_sandbox, extra=note)), (" --permission-mode " + mode) if mode else ""))
        print("shell sandbox: %s\n" % ("on" if with_sandbox else "off by manager.direct: the session asks before shell commands that are not reads, and may push"
                                       if direct_mode else "not available here, so the session asks before shell commands that are not reads"))
        print(json.dumps(settings(ledger, "talk", with_sandbox), indent=2))
        return 0
    try:
        sync_all(ledger)  # so the persona opens with what the machines say now
    except Exception:
        pass
    prepare_own_session(ledger, claude)
    mgr = ledger.config().get("manager") or {}
    if direct_mode:
        print("org: manager.direct is set: no shell sandbox, and the manager has the organization's GitHub token.\n     "
              + ("manager.permission_mode is bypassPermissions: it asks nothing." if unasked(ledger)
                 else "It asks before any shell command that is not a read."), file=sys.stderr)
    elif not with_sandbox and mgr.get("sandbox", True):
        print("org: no shell sandbox on this machine; the manager will ask before any shell command that is not a read.\n"
              "     `org doctor` says what is missing.", file=sys.stderr)
    cmd = [claude, "--append-system-prompt", persona(ledger, with_sandbox=with_sandbox, extra=note),
           "--settings", json.dumps(settings(ledger, "talk", with_sandbox)),
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
    os.execve(claude, cmd, launch_env(ledger, github=direct_mode))
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
