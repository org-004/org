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


class Docker(Transport):
    """A container on a Docker host: the local daemon, or a remote one (`ssh://user@host`).

    The daemon is what creates and removes machines, so whoever can reach it can run engineers:
    the Mac directly, or a manager that is itself a container holding the daemon's socket.
    """

    kind = "docker"

    def __init__(self, container: str, host: Optional[str] = None, node: str = "local"):
        self.name = container
        self.host = host or None
        self.node = node

    @staticmethod
    def cli(host: Optional[str]) -> List[str]:
        return ["docker"] + (["-H", host] if host else [])

    def argv(self, script: str) -> List[str]:
        return self.cli(self.host) + ["exec", "-i", self.name, "bash", "-c", script]

    def describe(self) -> str:
        return "docker:%s/%s" % (self.node, self.name)

    def attach_hint(self, run_id: str) -> str:
        return "%s exec -it %s tmux attach -t org-%s" % (" ".join(self.cli(self.host)), self.name, run_id)

    @classmethod
    def _docker(cls, host: Optional[str], args: List[str], timeout: float = 180, input: Optional[bytes] = None):
        try:
            return subprocess.run(cls.cli(host) + args, capture_output=True, timeout=timeout, input=input)
        except FileNotFoundError:
            raise TransportError("`docker` is not installed on this machine")
        except subprocess.TimeoutExpired:
            raise TransportError("docker %s: no answer within %ds" % (" ".join(args[:2]), timeout))

    @classmethod
    def _ok(cls, host: Optional[str], args: List[str], what: str, timeout: float = 180) -> str:
        p = cls._docker(host, args, timeout)
        if p.returncode != 0:
            raise TransportError("%s failed: %s" % (what, p.stderr.decode("utf-8", "replace").strip()[-400:]))
        return p.stdout.decode("utf-8", "replace").strip()

    @classmethod
    def ensure_network(cls, host: Optional[str], network: str) -> None:
        """Engineers get a bridge of their own, on which containers cannot talk to each other."""
        if cls._docker(host, ["network", "inspect", network], 60).returncode == 0:
            return
        p = cls._docker(host, ["network", "create", "--driver", "bridge",
                               "-o", "com.docker.network.bridge.enable_icc=false", network], 60)
        if p.returncode != 0 and b"already exists" not in p.stderr:
            raise TransportError("docker network create %s failed: %s" % (network, p.stderr.decode("utf-8", "replace").strip()[-300:]))

    @classmethod
    def create(cls, container: str, image: str, host: Optional[str] = None, node: str = "local",
               network: Optional[str] = None, labels: Optional[dict] = None, limits: Optional[dict] = None) -> "Docker":
        """Start a container that idles; the runner is started in it afterwards, like any machine."""
        if network:
            cls.ensure_network(host, network)
        args = ["run", "-d", "--init", "--name", container, "--hostname", container[:60]]
        for key, value in sorted((labels or {}).items()):
            args += ["--label", "%s=%s" % (key, value)]
        if network:
            args += ["--network", network]
        for flag in ("cpus", "memory"):
            if (limits or {}).get(flag):
                args += ["--" + flag, str(limits[flag])]
        cls._ok(host, args + [image, "sleep", "infinity"], "docker run %s" % container)
        return cls(container, host, node)

    @classmethod
    def delete(cls, container: str, host: Optional[str] = None) -> None:
        cls._ok(host, ["rm", "-f", container], "docker rm %s" % container, 120)

    @classmethod
    def state(cls, container: str, host: Optional[str] = None) -> Optional[str]:
        """running, exited, ... or None when the container does not exist."""
        p = cls._docker(host, ["inspect", "-f", "{{.State.Status}}", container], 60)
        return p.stdout.decode().strip() if p.returncode == 0 else None


def for_machine(machine: dict, cfg: dict) -> Transport:
    """Rebuild the transport for a machine recorded in run.json."""
    kind = machine.get("kind")
    if kind == "docker":
        return Docker(machine["name"], machine.get("host"), machine.get("node") or "local")
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
