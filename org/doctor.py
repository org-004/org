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

from . import budget
from .dispatch import docker_nodes
from .ledger import Ledger
from .manager import launch_env
from .transport import Ssh, TransportError
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

    default = (machines.get("default") or "orb").strip()
    uses_docker = default.startswith("docker")
    docker = shutil.which("docker")
    if uses_docker:
        add(bool(docker), "docker", docker or "not found", "install a Docker engine (OrbStack, Docker Desktop, Colima, or Docker on Linux)")
    if docker and uses_docker:
        image = (machines.get("docker") or {}).get("image", "org:latest")
        for name, entry in sorted(docker_nodes(cfg).items()):
            base = [docker] + (["-H", entry["host"]] if entry.get("host") else [])
            code, engine = _out(base + ["version", "--format", "{{.Server.Version}}"], timeout=25)
            if code != 0:
                add(False, "docker node `%s`" % name, engine.splitlines()[-1][:150] if engine else "no answer",
                    "check that the daemon is running and reachable%s" % (" as " + entry["host"] if entry.get("host") else ""))
                continue
            have = _out(base + ["image", "inspect", "-f", "{{.Id}}", image])[0] == 0
            add(have, "docker node `%s`" % name, "engine %s, %d slot(s)%s" % (engine, int(entry.get("slots", 1)), "" if have else ", image %s missing" % image),
                "`org up` builds it on this machine; `org node add %s <host>` builds it on a remote one" % name)
        token = Path.home() / ".claude" / "oauth-token"
        add(token.is_file() and token.stat().st_size > 0, "Claude token for engineers", str(token) if token.is_file() else "missing", "org login")
        add(_out(["gh", "auth", "token"])[0] == 0, "GitHub login for engineers", "present" if _out(["gh", "auth", "token"])[0] == 0 else "missing",
            "org login (without it engineers cannot clone private repositories or push)", soft=True)
    uses_orb = default == "orb"
    orb = shutil.which("orb")
    if uses_orb or (orb and not uses_docker):
        add(bool(orb), "orb", orb or "not found", "OrbStack machines are created from the Mac: brew install --cask orbstack", soft=not uses_orb)
        if orb:
            base = (machines.get("orb") or {}).get("base", "base")
            code, listing = _out([orb, "list"])
            names = [line.split()[0] for line in listing.splitlines() if line.split()]
            add(code == 0 and base in names, "template machine `%s`" % base,
                "present" if base in names else ("not in `orb list`" if code == 0 else listing[:120]),
                "create and bootstrap it once (it must have python3, git and claude); engineers run in clones of it", soft=not uses_orb)
        token = Path.home() / ".claude" / "oauth-token"
        add(token.is_file() and token.stat().st_size > 0, "login token for engineers", str(token) if token.is_file() else "missing",
            "run `claude setup-token` and save the token there (`box token` does this); it is copied into each engineer's machine",
            soft=not uses_orb)
    for name, entry in sorted((machines.get("ssh") or {}).items()):
        try:
            out = Ssh(name, entry.get("host") or "", entry.get("options")).run(
                'export PATH="$HOME/.local/bin:$PATH"; for c in python3 git claude; do command -v $c >/dev/null || echo "no $c"; done',
                timeout=20).stdout.decode().strip()
            add(not out, "ssh machine `%s`" % name, entry.get("host", "") + (" — " + out.replace("\n", ", ") if out else " reachable"),
                "install what is missing on that host")
        except TransportError as e:
            add(False, "ssh machine `%s`" % name, str(e)[:160], "check the host and that key-based login works without a prompt")
    if default.startswith("ssh:") and default[4:] not in (machines.get("ssh") or {}):
        add(False, "default machine", default, "define it under machines.ssh in %s" % ledger.config_path)

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
