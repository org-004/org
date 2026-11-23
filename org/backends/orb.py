"""OrbStack machines: a fresh one per run, cloned from a template. The default backend.

An OrbStack machine is a whole Linux system: an engineer in one has systemd and sudo, and can run
a Docker daemon of its own, which a plain container cannot. Cloning is copy-on-write and takes
about a second.

Two ways to reach OrbStack, set by machines.orb.via:

  cli     the manager is on the Mac, where the `orb` command works
  agent   the manager is itself an isolated machine, where it does not: creating, entering and
          deleting machines goes through the host agent (org/agent.py), which holds those three
          powers and nothing else. `org up` sets this.

An engineer runs with every permission bypassed, so its machine must be an isolated one: a normal
OrbStack machine has the Mac's files mounted and can run commands on the Mac. A clone of an
isolated template is isolated, and dispatch checks it before anything is started.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Dict, Optional

from .. import agent
from ..transport import Orb, Transport, TransportError
from . import Backend, BackendError, machine_name


class OrbBackend(Backend):
    kind = "orb"
    blank = False  # a clone inherits its template's GitHub login and git identity

    @staticmethod
    def _settings(cfg: dict) -> dict:
        return (cfg.get("machines") or {}).get("orb") or {}

    @staticmethod
    def _client(cfg: dict) -> agent.Client:
        return agent.Client(agent.socket_path(cfg["_home"]))

    def choose(self, cfg: dict, want: str, run_id: str) -> dict:
        if ":" in want:
            raise BackendError("an OrbStack machine is made fresh for each run and takes no name: write `orb`, not %r" % want)
        orb = self._settings(cfg)
        via = orb.get("via") or "cli"
        if via not in ("cli", "agent"):
            raise BackendError("machines.orb.via must be cli or agent, not %r" % via)
        return {"kind": "orb", "via": via, "name": machine_name(orb.get("prefix", "eng-"), run_id),
                "base": orb.get("base", "base"), "push_token": orb.get("push_token", True), "node": None}

    def nodes(self, cfg: dict) -> Dict[str, dict]:
        slots = self._settings(cfg).get("slots")
        return {"mac": {"slots": int(slots), "host": None}} if slots else {}

    def provision(self, machine: dict, run_id: str, cfg: dict) -> Transport:
        if machine.get("via") == "agent":
            client = self._client(cfg)
            try:
                client.create(machine["name"])
            except agent.AgentError as e:
                raise TransportError(str(e))
            return agent.AgentTransport(client, machine["name"])
        return Orb.create(machine.get("base", "base"), machine["name"])

    def vet(self, machine: dict, facts: dict, cfg: dict) -> None:
        if facts.get("mac_files") == "yes" and not self._settings(cfg).get("allow_shared"):
            raise BackendError(
                "%s can see the Mac's files, so it is not an isolated machine, and an engineer runs with every permission "
                "bypassed. Make the template isolated (`orb create --isolated …`, or `orb config set machine.%s.isolated true` "
                "and restart it); machines.orb.allow_shared overrides this." % (machine["name"], machine.get("base", "base")))

    def transport(self, machine: dict, cfg: dict) -> Transport:
        if machine.get("via") == "agent":
            return agent.AgentTransport(self._client(cfg), machine["name"])
        return Orb(machine["name"])

    def remove(self, machine: dict, cfg: dict) -> Optional[str]:
        if machine.get("via") == "agent":
            try:
                self._client(cfg).delete(machine["name"])
            except agent.AgentError as e:
                raise TransportError(str(e))
        else:
            Orb.delete(machine["name"])
        return "deleted %s" % machine["name"]

    def doctor(self, ledger, cfg: dict, add) -> None:
        orb = self._settings(cfg)
        base = orb.get("base", "base")
        if (orb.get("via") or "cli") == "agent":
            try:
                info = self._client(cfg).info()
                found = "connected: engineers are %s<run>, cloned from %s" % (info.get("prefix"), info.get("template"))
                same = info.get("prefix") == orb.get("prefix") and info.get("template") == base
                add(same, "host agent", found if same else found + "; this ledger expects %s<run> from %s" % (orb.get("prefix"), base),
                    "run `org up` on the Mac again so both sides agree")
            except agent.AgentError as e:
                add(False, "host agent", str(e)[:200], "run `org up` on the Mac")
        else:
            path = shutil.which("orb")
            add(bool(path), "orb", path or "not found", "OrbStack machines are created from the Mac: brew install --cask orbstack")
            if path:
                try:
                    p = subprocess.run([path, "list"], capture_output=True, text=True, timeout=20)
                    names = [line.split()[0] for line in p.stdout.splitlines() if line.split()] if p.returncode == 0 else None
                except (OSError, subprocess.TimeoutExpired):
                    names = None
                if names is None:
                    add(False, "template machine `%s`" % base, "`orb list` does not answer here (inside an isolated machine it cannot)",
                        "run org on the Mac, or run `org up` there, which gives a manager in a machine a host agent")
                else:
                    add(base in names, "template machine `%s`" % base, "present" if base in names else "not in `orb list`",
                        "`org up` makes one; or `orb create --isolated <distro> %s` and install python3, git and claude in it" % base)
        slots = orb.get("slots")
        add(True, "engineers at once", ("%d" % int(slots)) if slots else "no limit set (machines.orb.slots)")
        token = Path.home() / ".claude" / "oauth-token"
        add(token.is_file() and token.stat().st_size > 0, "Claude token for engineers", str(token) if token.is_file() else "missing",
            "org login (it is copied into each engineer's machine)")


BACKEND = OrbBackend()
