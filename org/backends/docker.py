"""Plugin: each engineer as a Docker container. Not loaded unless config names it:

    {"machines": {"plugins": ["org.backends.docker"], "default": "docker"}}

`org up --backend docker` sets that up, with the manager as a container too (docker/up.sh).

What it gives: the host needs only a Docker engine, and remote capacity is another Docker host
(`ssh://user@host`). What it costs: an engineer in a plain container cannot run Docker itself,
and a manager that starts containers must hold the Docker socket, which is root on the Docker
host. The OrbStack backend has neither cost, which is why it is the default.

The pool: machines.docker.nodes maps a node name to {"host": <DOCKER_HOST or null>, "slots": n,
"cpus": ..., "memory": ...}.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Dict, Optional

from ..transport import Docker, Transport
from . import Backend, BackendError, machine_name


def nodes_of(cfg: dict) -> Dict[str, dict]:
    nodes = ((cfg.get("machines") or {}).get("docker") or {}).get("nodes") or {}
    return nodes or {"local": {"host": None, "slots": 4}}


class DockerBackend(Backend):
    kind = "docker"
    blank = True  # a new container has no GitHub login and no git identity

    @staticmethod
    def _settings(cfg: dict) -> dict:
        return (cfg.get("machines") or {}).get("docker") or {}

    def choose(self, cfg: dict, want: str, run_id: str) -> dict:
        d = self._settings(cfg)
        node = (want.split(":", 1)[1] or None) if ":" in want else None
        if node and node not in nodes_of(cfg):
            raise BackendError("docker node %r is not defined in config.json under machines.docker.nodes" % node)
        return {"kind": "docker", "name": machine_name(d.get("prefix", "org-eng-"), run_id), "wanted_node": node, "node": None,
                "image": d.get("image", "org:latest"), "network": d.get("network", "org-engineers"),
                "push_token": d.get("push_token", True)}

    def nodes(self, cfg: dict) -> Dict[str, dict]:
        return {name: {"slots": int(entry.get("slots", 1)), "host": entry.get("host")} for name, entry in nodes_of(cfg).items()}

    def assign(self, cfg: dict, machine: dict, node: str) -> None:
        entry = nodes_of(cfg)[node]
        machine.update(node=node, host=entry.get("host"), limits={k: entry[k] for k in ("cpus", "memory") if entry.get(k)})

    def provision(self, machine: dict, run_id: str, cfg: dict) -> Transport:
        return Docker.create(machine["name"], machine["image"], host=machine.get("host"), node=machine.get("node") or "local",
                             network=machine.get("network"), labels={"org.role": "engineer", "org.run": run_id},
                             limits=machine.get("limits"))

    def transport(self, machine: dict, cfg: dict) -> Transport:
        return Docker(machine["name"], machine.get("host"), machine.get("node") or "local")

    def remove(self, machine: dict, cfg: dict) -> Optional[str]:
        Docker.delete(machine["name"], machine.get("host"))
        return "deleted %s" % machine["name"]

    def before_resume(self, machine: dict, cfg: dict) -> None:
        if Docker.state(machine["name"], machine.get("host")) == "exited":  # the Docker host restarted
            Docker._ok(machine.get("host"), ["start", machine["name"]], "docker start %s" % machine["name"])

    def doctor(self, ledger, cfg: dict, add) -> None:
        def out(argv, timeout=25):
            try:
                p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
                return p.returncode, (p.stdout + p.stderr).strip()
            except (OSError, subprocess.TimeoutExpired) as e:
                return 127, str(e)

        docker = shutil.which("docker")
        add(bool(docker), "docker", docker or "not found", "install a Docker engine (OrbStack, Docker Desktop, Colima, or Docker on Linux)")
        if docker:
            image = self._settings(cfg).get("image", "org:latest")
            for name, entry in sorted(nodes_of(cfg).items()):
                base = [docker] + (["-H", entry["host"]] if entry.get("host") else [])
                code, engine = out(base + ["version", "--format", "{{.Server.Version}}"])
                if code != 0:
                    add(False, "docker node `%s`" % name, engine.splitlines()[-1][:150] if engine else "no answer",
                        "check that the daemon is running and reachable%s" % (" as " + entry["host"] if entry.get("host") else ""))
                    continue
                have = out(base + ["image", "inspect", "-f", "{{.Id}}", image])[0] == 0
                add(have, "docker node `%s`" % name,
                    "engine %s, %d slot(s)%s" % (engine, int(entry.get("slots", 1)), "" if have else ", image %s missing" % image),
                    "`org up --backend docker` builds it on this machine; `org node add %s <host>` builds it on a remote one" % name)
        token = Path.home() / ".claude" / "oauth-token"
        add(token.is_file() and token.stat().st_size > 0, "Claude token for engineers", str(token) if token.is_file() else "missing", "org login")
        logged_in = out(["gh", "auth", "token"])[0] == 0
        add(logged_in, "GitHub login for engineers", "present" if logged_in else "missing",
            "org login (without it engineers cannot clone private repositories or push)", soft=True)


BACKEND = DockerBackend()
