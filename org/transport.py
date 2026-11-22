"""Reaching an engineer's machine: an OrbStack machine, an SSH host, or this machine.

A transport does one thing: run a bash script on the machine, with optional bytes on stdin, and
return what it printed. Files travel as base64 on stdin, so nothing depends on a transport being
binary-clean or on scp/sftp being available. The manager's side always initiates; an engineer's
machine never needs a route back, which is what lets the machines stay isolated.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from typing import List, Optional


class TransportError(Exception):
    pass


class Transport:
    kind = "?"
    name = "?"

    def argv(self, script: str) -> List[str]:
        raise NotImplementedError

    def env(self) -> Optional[dict]:
        return None

    def run(self, script: str, input: Optional[bytes] = None, timeout: float = 120, check: bool = True):
        argv = self.argv(script)
        try:
            p = subprocess.run(argv, input=input, capture_output=True, timeout=timeout, env=self.env())
        except subprocess.TimeoutExpired:
            raise TransportError("%s: no answer within %ds" % (self.describe(), timeout))
        except FileNotFoundError:
            raise TransportError("%s: `%s` is not installed on this machine" % (self.describe(), argv[0]))
        if check and p.returncode != 0:
            tail = (p.stderr or b"").decode("utf-8", "replace").strip()[-600:]
            raise TransportError("%s: exit %d: %s" % (self.describe(), p.returncode, tail or "(no stderr)"))
        return p

    def describe(self) -> str:
        return "%s:%s" % (self.kind, self.name)

    def attach_hint(self, run_id: str) -> str:
        return ""


class Local(Transport):
    """This machine, with no isolation. For trying org out and for its tests."""

    kind = "local"

    def __init__(self, home: Optional[str] = None):
        self.name = "localhost"
        self.home = home

    def argv(self, script: str) -> List[str]:
        return ["bash", "-c", script]

    def env(self) -> Optional[dict]:
        if not self.home:
            return None
        e = dict(os.environ)
        e["HOME"] = self.home
        return e

    def attach_hint(self, run_id: str) -> str:
        return "tmux attach -t org-%s" % run_id


class Orb(Transport):
    """An OrbStack machine, reached the way `box` reaches one: `orb -m <machine> <argv>`."""

    kind = "orb"

    def __init__(self, machine: str):
        self.name = machine

    def argv(self, script: str) -> List[str]:
        return ["orb", "-m", self.name, "bash", "-c", script]

    def attach_hint(self, run_id: str) -> str:
        return "orb -m %s tmux attach -t org-%s" % (self.name, run_id)

    @staticmethod
    def _orb(args: List[str], timeout: float = 180) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(["orb"] + args, capture_output=True, timeout=timeout)
        except FileNotFoundError:
            raise TransportError("`orb` is not installed on this machine (OrbStack machines are created from the Mac)")
        except subprocess.TimeoutExpired:
            raise TransportError("orb %s: no answer within %ds" % (" ".join(args), timeout))

    @classmethod
    def create(cls, base: str, machine: str) -> "Orb":
        """Clone the template machine and start the clone (copy-on-write, about a second)."""
        p = cls._orb(["clone", base, machine])
        if p.returncode != 0:
            raise TransportError("orb clone %s %s failed: %s" % (base, machine, p.stderr.decode("utf-8", "replace").strip()[-400:]))
        p = cls._orb(["start", machine])
        if p.returncode != 0:
            raise TransportError("orb start %s failed: %s" % (machine, p.stderr.decode("utf-8", "replace").strip()[-400:]))
        return cls(machine)

    @classmethod
    def delete(cls, machine: str) -> None:
        p = cls._orb(["delete", "-f", machine])
        if p.returncode != 0:
            raise TransportError("orb delete %s failed: %s" % (machine, p.stderr.decode("utf-8", "replace").strip()[-400:]))


class Ssh(Transport):
    """A remote VM or any host with sshd, python3 and Claude Code installed."""

    kind = "ssh"

    def __init__(self, name: str, host: str, options: Optional[List[str]] = None):
        self.name = name
        self.host = host
        self.options = list(options or [])

    def argv(self, script: str) -> List[str]:
        return (["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"] + self.options
                + [self.host, "bash -c " + shlex.quote(script)])

    def attach_hint(self, run_id: str) -> str:
        return "ssh -t %s %s tmux attach -t org-%s" % (" ".join(self.options), self.host, run_id)


def for_machine(machine: dict, cfg: dict) -> Transport:
    """Rebuild the transport for a machine recorded in run.json."""
    kind = machine.get("kind")
    if kind == "orb":
        return Orb(machine["name"])
    if kind == "ssh":
        entry = (cfg.get("machines", {}).get("ssh") or {}).get(machine["name"]) or {}
        host = entry.get("host") or machine.get("host")
        if not host:
            raise TransportError("ssh machine %r is not in config.json and the run has no host recorded" % machine.get("name"))
        return Ssh(machine["name"], host, entry.get("options") or machine.get("options"))
    if kind == "local":
        return Local(machine.get("home"))
    raise TransportError("unknown machine kind %r" % kind)
