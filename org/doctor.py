"""`org doctor`: is this machine ready to manage engineers?

Checks what dispatch and the manager need, and says what to do about each thing that is missing.
Nothing here changes anything.
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

from . import backends, budget, login, notify, secrets
from .ledger import Ledger
from .manager import launch_env
from .util import fmt_usd

BIN = Path(__file__).resolve().parent.parent / "bin" / "org"


def _out(argv: List[str], timeout: float = 15) -> Tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def checks(ledger: Ledger) -> List[Tuple[str, str, str, str]]:
    """(level, name, what was found, what to do) where level is ok, warn or fail."""
    cfg = ledger.config()
    machines = cfg.get("machines") or {}
    rows: List[Tuple[str, str, str, str]] = []

    def add(ok, name, found, fix="", soft=False):
        rows.append(("ok" if ok else ("warn" if soft else "fail"), name, found, "" if ok else fix))

    add(sys.version_info >= (3, 9), "python", platform.python_version(), "org needs Python 3.9 or later")
    claude = shutil.which(cfg.get("claude_bin") or "claude", path=launch_env(ledger)["PATH"])
    version = _out([claude, "--version"])[1].splitlines()[0] if claude else "not found"
    add(bool(claude), "claude CLI", version, "install Claude Code: https://claude.ai/install.sh")
    on_path = shutil.which("org")
    add(bool(on_path), "org on PATH", on_path or "not found", "ln -s %s ~/.local/bin/org" % BIN, soft=True)
    add(ledger.config_path.exists(), "ledger", str(ledger.root), "org init")

    # Where engineers run. Each backend says what it needs: the one specs go to by default, the
    # ssh hosts that are configured, and any plugin that has been switched on.
    default = (machines.get("default") or "incus").strip()
    try:
        loaded = backends.load(cfg)
        backends.resolve(cfg, default).choose(cfg, default, "check")
        add(True, "default machine", default)
    except backends.BackendError as e:
        loaded = {}
        add(False, "default machine", "%s: %s" % (default, e), "fix machines in %s" % ledger.config_path)
    for kind, backend in sorted(loaded.items()):
        plugin = type(backend).__module__ not in backends.BUILTIN
        if kind == default.split(":")[0] or plugin or (kind == "ssh" and machines.get("ssh")):
            backend.doctor(ledger, cfg, add)

    # What engineers are handed, and that it is the narrow pair and nothing wider.
    have = login.status()
    add(have["claude_token"], "Claude token", "present" if have["claude_token"] else "missing",
        "on the host: org-vm login, then org-vm up")
    add(have["github"] and have["github_fine_grained"], "GitHub token",
        "missing" if not have["github"] else ("fine-grained" if have["github_fine_grained"] else "a classic token: it reaches every repository its owner has"),
        "on the host: org-vm login with a fine-grained token whose resource owner is the organization, then org-vm up")
    add(bool(have["git_name"] and have["git_email"]), "git identity",
        ("%s <%s>" % (have["git_name"], have["git_email"])) if have["git_name"] and have["git_email"] else "not set",
        'git config --global user.name "..." and user.email "..."')
    owner = (cfg.get("github") or {}).get("owner")
    add(bool(owner), "GitHub owner", owner or "not set: a spec may name a repository anywhere",
        "org up --owner <organization>", soft=True)
    # Claude Code here must not be able to reach the account's connectors or any MCP server.
    full_login = Path.home() / ".claude" / ".credentials.json"
    add(not full_login.exists(), "no account login", "none" if not full_login.exists() else "%s exists" % full_login,
        "someone ran `claude /login` here; that login reaches the account's connectors. Remove the file.")
    managed = Path("/etc/claude-code/managed-mcp.json")
    add(managed.is_file(), "MCP servers", "forbidden by %s" % managed if managed.is_file() else "nothing forbids them",
        "the machine's setup writes %s; make the machine with org-vm" % managed, soft=True)

    if platform.system() == "Darwin":
        add(bool(shutil.which("sandbox-exec")), "manager sandbox", "Seatbelt (built in)", "", soft=True)
    else:
        missing = [c for c in ("bwrap", "socat") if not shutil.which(c)]
        add(not missing, "manager sandbox", "bubblewrap and socat" if not missing else "missing " + ", ".join(missing),
            "install them, or accept that `org talk` relies on permission rules alone here (it asks before shell writes)", soft=True)

    rate = budget.usd_per_pct(ledger)
    week = budget.weekly(ledger)
    found = "not seen yet" if not week else "%d%% used" % round((week.get("utilization") or 0) * 100)
    found += "; 1%% ≈ %s (%s)" % (fmt_usd(rate["value"]), rate["source"]) if rate["value"] else "; dollars per 1% not known yet"
    rows.append(("ok", "weekly limit", found, ""))
    add(*notify.doctor_row(cfg), soft=True)
    return rows


def run(ledger: Ledger) -> int:
    rows = checks(ledger)
    mark = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}
    width = max(len(r[1]) for r in rows)
    for level, name, found, fix in rows:
        print("%s  %s  %s" % (mark[level], name.ljust(width), found))
        if fix:
            print("      %s  → %s" % (" " * width, fix))
    failed = [r for r in rows if r[0] == "fail"]
    print()
    print("ready" if not failed else "%d thing(s) to fix before engineers can be dispatched" % len(failed))
    return 1 if failed else 0
