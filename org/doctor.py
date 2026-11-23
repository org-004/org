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

from . import backends, budget
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
    default = (machines.get("default") or "orb").strip()
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
