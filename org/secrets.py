"""The two tokens: a Claude token for model requests and a GitHub token for one organization.

They are handed to this machine from outside (the host's `org-vm` writes them at every start)
and live in memory: /run/org, a tmpfs only this user can read. Nothing here writes a token to a
disk, and nothing here asks `gh` or Claude Code for a login of its own: a token found by that
route would be a wider one than the pair this installation was given.

    $ORG_SECRETS_DIR, else /run/org
        claude-token     from `claude setup-token`: model requests only, no claude.ai connectors
        github-token     a fine-grained token whose resource owner is the organization

~/.claude/oauth-token is still read for the Claude token, for an installation set up by hand.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

CLAUDE = "claude-token"
GITHUB = "github-token"


def directory() -> Path:
    return Path(os.environ.get("ORG_SECRETS_DIR") or "/run/org")


def _candidates(name: str) -> List[Path]:
    # ~/.org-secrets is where dispatch puts a token on a machine that has no /run/org to give.
    paths = [directory() / name, Path.home() / ".org-secrets" / name]
    if name == CLAUDE:
        paths.append(Path.home() / ".claude" / "oauth-token")
    return paths


def path(name: str) -> Optional[Path]:
    """Where a token is, or None when this machine has not been given it."""
    for p in _candidates(name):
        try:
            if p.is_file() and p.stat().st_size > 0:
                return p
        except OSError:
            continue
    return None


def read(name: str) -> str:
    p = path(name)
    try:
        return p.read_text().strip() if p else ""
    except OSError:
        return ""


def claude_token() -> str:
    return read(CLAUDE)


def github_token() -> str:
    return read(GITHUB)


def _places():
    """(path, is a directory) for every place a token or a login may be on this machine."""
    home = Path.home()
    return [(directory(), True), (home / ".org-secrets", True), (home / ".claude" / "oauth-token", False),
            (home / ".claude" / ".credentials.json", False), (home / ".config" / "gh", True), (home / ".ssh", True)]


def all_paths() -> List[str]:
    """Those places, for the sandbox, which hides whole paths."""
    return [str(p) for p, _ in _places()]


def rule_patterns() -> List[str]:
    """The same, as permission-rule patterns: a directory covers everything under it."""
    return [str(p) + ("/**" if is_dir else "") for p, is_dir in _places()]


def is_fine_grained(token: str) -> bool:
    """A fine-grained GitHub token names the one owner it can reach. A classic one reaches all of them."""
    return token.startswith("github_pat_")


def process_env(env: dict) -> dict:
    """Add the tokens to an environment for a process that acts as the person: an engineer's
    session, or a command of org's own that pushes. Never the manager chat's environment."""
    if not env.get("CLAUDE_CODE_OAUTH_TOKEN") and not env.get("ANTHROPIC_API_KEY"):
        token = claude_token()
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    github = github_token()
    if github:
        env["GH_TOKEN"] = github
    return env
