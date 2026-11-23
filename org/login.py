"""`org login`: say which of the things an engineer is handed are in place here.

  1. a Claude token      from `claude setup-token`: model requests only
  2. a GitHub token      a fine-grained one whose resource owner is the organization
  3. a git identity      who commits are made as

The two tokens are not typed in here and nothing here opens a browser login: a login made on
this machine would be the whole account, not the narrow pair. They are handed in from the host
(`org-vm login` stores them in the host's keychain, `org-vm up` copies them into this machine's
memory) and read from there by org/secrets.py.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import secrets

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
    github = secrets.github_token()
    return {
        "claude_token": bool(secrets.claude_token()),
        "github": bool(github),
        "github_fine_grained": secrets.is_fine_grained(github),
        "git_name": _out(["git", "config", "--global", "user.name"]),
        "git_email": _out(["git", "config", "--global", "user.email"]),
    }


def run(force: bool = False, token_stdin: bool = False) -> int:
    if token_stdin:  # the Docker plugin's `org up` pipes in a token the host already has
        token = sys.stdin.readline().strip()
        if not token:
            print("no token on stdin")
            return 1
        store_token(token)
        print("Claude token stored")
        return 0
    s = status()
    print("Claude token: %s" % ("present" if s["claude_token"] else "missing"))
    if s["github"] and not s["github_fine_grained"]:
        print("GitHub token: present, but it is not a fine-grained one (github_pat_...). A classic token reaches every "
              "repository its owner has; replace it.")
    else:
        print("GitHub token: %s" % ("present, fine-grained" if s["github"] else "missing"))
    print("git identity: %s" % (("%s <%s>" % (s["git_name"], s["git_email"])) if s["git_name"] and s["git_email"] else "not set"))
    ok = s["claude_token"] and s["github"] and s["github_fine_grained"]
    if not ok:
        print("\nThe tokens are handed in from the host, not typed here:\n"
              "  org-vm login    stores them in the host's keychain\n"
              "  org-vm up       copies them into this machine's memory (%s)" % secrets.directory())
    if not (s["git_name"] and s["git_email"]):
        print("\nSet who commits are made as:\n  git config --global user.name \"...\"\n  git config --global user.email \"...\"")
    return 0 if ok and s["git_name"] and s["git_email"] else 1
