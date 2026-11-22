"""`org login`: give this side the three things an engineer is handed at dispatch.

  1. a long-lived Claude token in ~/.claude/oauth-token   (from `claude setup-token`)
  2. a GitHub login                                        (`gh auth login`)
  3. a git identity                                        (taken from the GitHub account if unset)

On a Mac that already has these, nothing happens. In a manager container that started blank this
is the one interactive step after `org up`: both logins are browser flows that print a URL, so
they work from inside a container with no browser of its own.
"""
from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

TOKEN = Path.home() / ".claude" / "oauth-token"


def _out(argv, timeout: float = 30, env: Optional[dict] = None) -> str:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        return p.stdout.strip() if p.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def token_works(token: str, claude: str) -> bool:
    """One tiny request on the cheapest model: the only way to know a token is good."""
    env = dict(os.environ, CLAUDE_CODE_OAUTH_TOKEN=token)
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        p = subprocess.run([claude, "-p", "reply with the single word ok", "--model", "haiku"], capture_output=True, text=True,
                           timeout=120, env=env, cwd="/tmp", stdin=subprocess.DEVNULL)
        return p.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def store_token(token: str) -> None:
    TOKEN.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(TOKEN), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token.strip() + "\n")


def status() -> dict:
    """What is in place, without changing or testing anything."""
    return {
        "claude_token": TOKEN.is_file() and TOKEN.stat().st_size > 0,
        "github": bool(_out(["gh", "auth", "token"])),
        "git_name": _out(["git", "config", "--global", "user.name"]),
        "git_email": _out(["git", "config", "--global", "user.email"]),
    }


def run(force: bool = False, token_stdin: bool = False) -> int:
    claude = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
    # ---- 1. the Claude token
    if token_stdin:  # `org up` pipes in a token the host already has
        token = sys.stdin.readline().strip()
        if not token:
            print("no token on stdin")
            return 1
        store_token(token)
        print("Claude token stored")
        return 0
    have = TOKEN.read_text().strip() if TOKEN.is_file() else ""
    if have and not force and token_works(have, claude):
        print("Claude: the token in %s works" % TOKEN)
    else:
        print("Claude: a long-lived token is needed. `claude setup-token` will print a URL to open in your\n"
              "browser, then print the token (it starts with sk-ant-oat01-).\n")
        subprocess.run([claude, "setup-token"])
        token = getpass.getpass("\npaste the token it printed (input is hidden): ").strip().replace("\n", "").replace(" ", "")
        if not token:
            print("nothing stored")
            return 1
        if not token_works(token, claude):
            print("the API rejected that token (%d characters); nothing stored" % len(token))
            return 1
        store_token(token)
        print("Claude: token accepted and stored in %s" % TOKEN)
    # ---- 2. GitHub
    if _out(["gh", "auth", "token"]) and not force:
        print("GitHub: logged in as %s" % (_out(["gh", "api", "user", "--jq", ".login"]) or "?"))
    else:
        print("\nGitHub: logging in (a one-time code and a URL to open in your browser)\n")
        if subprocess.run(["gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web"]).returncode != 0:
            print("GitHub login did not complete")
            return 1
    subprocess.run(["gh", "auth", "setup-git"], capture_output=True)
    # ---- 3. who commits
    name, email = _out(["git", "config", "--global", "user.name"]), _out(["git", "config", "--global", "user.email"])
    if not (name and email):
        user = json.loads(_out(["gh", "api", "user"]) or "{}")
        name = name or user.get("name") or user.get("login") or ""
        email = email or user.get("email") or (("%s+%s@users.noreply.github.com" % (user["id"], user["login"])) if user.get("id") else "")
        if name and email:
            subprocess.run(["git", "config", "--global", "user.name", name])
            subprocess.run(["git", "config", "--global", "user.email", email])
    print("git: commits will be made as %s <%s>" % (name or "?", email or "?"))
    return 0
