"""This machine, with no isolation. For trying org out and for its tests.

An engineer runs with every permission bypassed, so "this machine" should itself be a box. Off
unless machines.local.enabled is true in config.json.
"""
from __future__ import annotations

from ..transport import Local, Transport
from . import Backend, BackendError


class LocalBackend(Backend):
    kind = "local"
    blank = False

    def choose(self, cfg: dict, want: str, run_id: str) -> dict:
        local = (cfg.get("machines") or {}).get("local") or {}
        if not local.get("enabled"):
            raise BackendError("the local machine is disabled: an engineer runs with all permissions bypassed, "
                               "so it belongs in a box. Set machines.local.enabled to true in config.json to allow it.")
        return {"kind": "local", "name": "localhost", "home": local.get("home"), "push_token": False, "node": None}

    def provision(self, machine: dict, run_id: str, cfg: dict) -> Transport:
        return Local(machine.get("home"))

    def transport(self, machine: dict, cfg: dict) -> Transport:
        return Local(machine.get("home"))


BACKEND = LocalBackend()
