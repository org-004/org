"""Reaching an engineer's machine: an Incus container, an SSH host, a Docker container, or this machine.

A transport does one thing: run a bash script on the machine, with optional bytes on stdin, and
return what it printed. Files travel as base64 on stdin, so nothing depends on a transport being
binary-clean or on scp/sftp being available. The manager's side always initiates; an engineer's
machine never needs a route back, which is what lets the machines stay isolated.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from typing import Dict, List, Optional


#: The name of an Incus remote, as `incus remote add` is given it here.
REMOTE_RE = re.compile(r"[a-z][a-z0-9-]{0,62}")


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
    a manager beside the daemon, or one that is itself a container holding the daemon's socket.
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


# What Incus.relay runs in a container: its stdin goes to a port on the container's own loopback,
# and what comes back goes to its stdout. It ends when the app closes, or when its input ends:
# the far end closes that to say it is done. An app that stops reading (it answered early) is
# still heard out. The last argument is a backstop, for a relay whose far end vanished without
# closing anything.
RELAY = r"""
import os, socket, sys, threading, time
port, idle = int(sys.argv[1]), float(sys.argv[2])
s = None
for host in ("127.0.0.1", "::1"):
    try:
        s = socket.create_connection((host, port), 10)
        break
    except OSError:
        pass
if s is None:
    sys.exit(1)
s.settimeout(idle)
moved = [time.time()]
def up():
    try:
        while True:
            data = os.read(0, 65536)
            if not data:
                os._exit(0)
            moved[0] = time.time()
            s.sendall(data)
    except OSError:
        pass
    try:
        os.close(0)
    except OSError:
        pass
threading.Thread(target=up, daemon=True).start()
try:
    while True:
        try:
            data = s.recv(65536)
        except socket.timeout:
            if time.time() - moved[0] > idle:
                break
            continue
        if not data:
            break
        moved[0] = time.time()
        while data:
            data = data[os.write(1, data):]
except OSError:
    pass
os._exit(0)
"""


class Incus(Transport):
    """An Incus system container on this machine: a whole Linux system with its own init, network
    and Docker daemon, sharing this machine's kernel. Reached with `incus exec`, as its root.

    Every `incus` command here is given a stdin of its own. Several of them read it when it is
    not a terminal (`incus init` takes a configuration from it), and one that inherited the
    caller's would eat whatever the caller was being fed.
    """

    kind = "incus"

    def __init__(self, container: str, remote: Optional[str] = None):
        self.name = container
        self.remote = remote
        self.target = self.at(remote, container)

    @staticmethod
    def at(remote: Optional[str], name: str) -> str:
        """The name Incus is given: `remote:name` for a container on another machine's Incus (a
        remote of this user's client, `incus remote add`), the plain name for one on this machine."""
        if not remote:
            return name
        if not REMOTE_RE.fullmatch(str(remote)):
            raise TransportError("%r is not the name of an Incus remote: lowercase letters, digits and dashes" % (remote,))
        return "%s:%s" % (remote, name)

    def argv(self, script: str) -> List[str]:
        return ["incus", "exec", self.target, "-T", "--", "bash", "-c", script]

    def describe(self) -> str:
        return "incus:%s" % self.target

    def run(self, script: str, input: Optional[bytes] = None, timeout: float = 120, check: bool = True):
        return super().run(script, input=b"" if input is None else input, timeout=timeout, check=check)

    def relay(self, port: int, idle: float = 360) -> subprocess.Popen:
        """A pipe to a port on the container's own loopback: write to its stdin, read its stdout.

        The address is fixed here and the port is a number, so nothing the container says decides
        where this connects. python3 is in every engineer's container; nothing else is needed.
        """
        try:
            return subprocess.Popen(["incus", "exec", self.target, "-T", "--", "python3", "-c", RELAY, str(int(port)), str(int(idle))],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        except FileNotFoundError:
            raise TransportError("`incus` is not installed on this machine")

    def attach_hint(self, run_id: str) -> str:
        return "incus exec %s -t -- tmux attach -t org-%s" % (self.target, run_id)

    @staticmethod
    def incus(args: List[str], timeout: float = 180, input: bytes = b"") -> subprocess.CompletedProcess:
        try:
            return subprocess.run(["incus"] + args, capture_output=True, timeout=timeout, input=input)
        except FileNotFoundError:
            raise TransportError("`incus` is not installed on this machine")
        except subprocess.TimeoutExpired:
            raise TransportError("incus %s: no answer within %ds" % (" ".join(args[:2]), timeout))

    @classmethod
    def ok(cls, args: List[str], what: str, timeout: float = 180) -> str:
        p = cls.incus(args, timeout)
        if p.returncode != 0:
            raise TransportError("%s failed: %s" % (what, p.stderr.decode("utf-8", "replace").strip()[-400:]))
        return p.stdout.decode("utf-8", "replace").strip()

    @classmethod
    def info(cls, container: str, timeout: float = 60, remote: Optional[str] = None) -> Optional[dict]:
        """The container as Incus describes it (status, expanded_config, expanded_devices), or None."""
        # `incus list`, not `incus query`: a user confined to a project of its own is shown that
        # project by the first and the default one, where it has nothing, by the second. The
        # filter is a pattern, so it is anchored and the name is compared again.
        where = [cls.at(remote, "")] if remote else []
        p = cls.incus(["list", "--format", "json"] + where + ["^%s$" % container], timeout)
        if p.returncode != 0:
            raise TransportError("incus list failed: %s" % p.stderr.decode("utf-8", "replace").strip()[-300:])
        try:
            data = json.loads(p.stdout.decode("utf-8", "replace"))
        except ValueError:
            # Not None: None says Incus does not know the container, and on that word a finished
            # run's machine is recorded as gone.
            raise TransportError("incus list did not answer in JSON")
        for entry in data if isinstance(data, list) else []:
            if isinstance(entry, dict) and entry.get("name") == container:
                return entry
        return None

    @classmethod
    def create(cls, base: str, container: str, config: Optional[Dict[str, str]] = None,
               remote: Optional[str] = None) -> "Incus":
        """Copy the template and start the copy. On a copy-on-write pool the copy is instant.

        `config` is given to the command that makes the copy, so the copy never exists without
        it and is never started without it; a key Incus refuses leaves no copy behind. On a
        remote, the template is that machine's own, so the copy never crosses the network.
        """
        args = ["copy", "-q", cls.at(remote, base), cls.at(remote, container)]
        for key in sorted(config or {}):
            args += ["-c", "%s=%s" % (key, config[key])]
        cls.ok(args, "incus copy %s %s" % (cls.at(remote, base), cls.at(remote, container)))
        try:
            return cls.start(container, remote)
        except TransportError:
            cls.incus(["delete", "-q", "-f", cls.at(remote, container)], 120)  # the copy is ours and is of no use
            raise

    @classmethod
    def start(cls, container: str, remote: Optional[str] = None) -> "Incus":
        info = cls.info(container, remote=remote)
        if info is None:
            raise TransportError("incus: no container named %s" % cls.at(remote, container))
        if info.get("status") != "Running":
            cls.ok(["start", "-q", cls.at(remote, container)], "incus start %s" % cls.at(remote, container))
        tr = cls(container, remote)
        # Started is not ready: wait for its init and for an address, or the first clone fails.
        ready = ('for i in $(seq 1 60); do ip -4 -o addr show scope global 2>/dev/null | grep -q inet && exit 0; sleep 0.5; done; exit 1')
        if tr.run(ready, timeout=90, check=False).returncode != 0:
            raise TransportError("%s started but got no network address within 30s" % cls.at(remote, container))
        return tr

    @classmethod
    def set_config(cls, container: str, key: str, value: str, remote: Optional[str] = None) -> None:
        """Set one key on a container that exists, as `incus config set` does."""
        target = cls.at(remote, container)
        cls.ok(["config", "set", target, key, value], "incus config set %s %s %s" % (target, key, value), 60)

    @classmethod
    def delete(cls, container: str, remote: Optional[str] = None) -> None:
        target = cls.at(remote, container)
        cls.ok(["delete", "-q", "-f", target], "incus delete %s" % target, 120)

    @classmethod
    def memory_mib(cls, remote: str, timeout: float = 60) -> Optional[int]:
        """The memory of the machine behind a remote, in whole MiB, as its Incus reports it, or None."""
        p = cls.incus(["query", cls.at(remote, "/1.0/resources")], timeout)
        try:
            total = json.loads(p.stdout.decode("utf-8", "replace"))["memory"]["total"] if p.returncode == 0 else None
            return int(total) // (1024 * 1024) if total else None
        except (ValueError, KeyError, TypeError):
            return None
