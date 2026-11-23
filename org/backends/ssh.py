"""A host that already exists, reached over ssh: a remote VM, a workstation, a cloud instance.

org does not create or delete these. A host is listed under machines.ssh in config.json:

    "ssh": {"bigbox": {"host": "me@bigbox", "options": ["-p", "2222"], "slots": 2, "push_token": false}}

`slots` is optional; without it any number of runs may share the host.
"""
from __future__ import annotations

from typing import Dict

from ..transport import Ssh, Transport, TransportError
from . import Backend, BackendError


class SshBackend(Backend):
    kind = "ssh"
    blank = False  # its owner set up its logins

    @staticmethod
    def _hosts(cfg: dict) -> dict:
        return (cfg.get("machines") or {}).get("ssh") or {}

    def choose(self, cfg: dict, want: str, run_id: str) -> dict:
        name = want[4:] if want.startswith("ssh:") else ""
        entry = self._hosts(cfg).get(name)
        if not entry or not entry.get("host"):
            raise BackendError("ssh machine %r is not defined in config.json under machines.ssh" % name)
        return {"kind": "ssh", "name": name, "host": entry["host"], "options": entry.get("options") or [],
                "push_token": bool(entry.get("push_token", False)), "wanted_node": name, "node": None}

    def nodes(self, cfg: dict) -> Dict[str, dict]:
        return {name: {"slots": int(entry["slots"]), "host": entry.get("host")}
                for name, entry in self._hosts(cfg).items() if entry.get("slots")}

    def provision(self, machine: dict, run_id: str, cfg: dict) -> Transport:
        return Ssh(machine["name"], machine["host"], machine.get("options"))

    def transport(self, machine: dict, cfg: dict) -> Transport:
        entry = self._hosts(cfg).get(machine["name"]) or {}
        host = entry.get("host") or machine.get("host")
        if not host:
            raise TransportError("ssh machine %r is not in config.json and the run has no host recorded" % machine.get("name"))
        return Ssh(machine["name"], host, entry.get("options") or machine.get("options"))

    def doctor(self, ledger, cfg: dict, add) -> None:
        for name, entry in sorted(self._hosts(cfg).items()):
            try:
                out = Ssh(name, entry.get("host") or "", entry.get("options")).run(
                    'export PATH="$HOME/.local/bin:$PATH"; for c in python3 git claude; do command -v $c >/dev/null || echo "no $c"; done',
                    timeout=20).stdout.decode().strip()
                add(not out, "ssh machine `%s`" % name, entry.get("host", "") + (" — " + out.replace("\n", ", ") if out else " reachable"),
                    "install what is missing on that host")
            except TransportError as e:
                add(False, "ssh machine `%s`" % name, str(e)[:160], "check the host and that key-based login works without a prompt")


BACKEND = SshBackend()
