"""Where engineers run.

A backend is one kind of machine: how to pick one for a run, how many may run at once, how to
create it, reach it and remove it. Dispatch, sync and status know only this interface.

Built in:
    incus    a fresh Incus container per run, copied from a template. The default.
    ssh      a host that already exists
    local    this machine, with no isolation; off unless enabled

Plugins are backends that are not loaded unless config names them:

    {"machines": {"plugins": ["org.backends.docker"]}}

`org.backends.docker` runs each engineer as a Docker container. Any importable module that
defines `BACKEND` (a Backend instance) can be listed the same way.
"""
from __future__ import annotations

import importlib
import re
from typing import Dict, List, Optional

from ..transport import Transport

BUILTIN = ["org.backends.incus", "org.backends.ssh", "org.backends.local"]


class BackendError(Exception):
    pass


class Backend:
    kind = ""
    #: True when a new machine has no GitHub login or git identity of its own, so dispatch
    #: copies them in. False when the machine inherits them (a clone of a template) or its owner
    #: set them up (an ssh host).
    blank = False

    def choose(self, cfg: dict, want: str, run_id: str) -> dict:
        """The machine record for a run asking for `want` ("kind" or "kind:detail")."""
        raise NotImplementedError

    def nodes(self, cfg: dict) -> Dict[str, dict]:
        """Capacity: node name -> {"slots": n, ...}. Empty means no limit and nothing to place."""
        return {}

    def assign(self, cfg: dict, machine: dict, node: str) -> None:
        """Record on the machine what it needs to know about the node it was placed on."""
        machine["node"] = node

    def provision(self, machine: dict, run_id: str, cfg: dict) -> Transport:
        """Create the machine if this backend creates machines, and return a way to reach it."""
        raise NotImplementedError

    def vet(self, machine: dict, facts: dict, cfg: dict) -> None:
        """Refuse (raise BackendError) a machine that, by what was found on it, must not run an engineer."""

    def transport(self, machine: dict, cfg: dict) -> Transport:
        """A way to reach a machine that already exists."""
        raise NotImplementedError

    def remove(self, machine: dict, cfg: dict) -> Optional[str]:
        """Delete the machine. None when machines of this kind are not org's to delete."""
        return None

    def before_resume(self, machine: dict, cfg: dict) -> None:
        """Anything a machine needs before its runner is restarted."""

    def resume_facts(self, machine: dict, cfg: dict) -> dict:
        """What `before_resume` may have changed about the machine, as the facts an engineer is told."""
        return {}

    def build_cache(self, tr: Transport, cfg: dict, log, again: bool = False) -> Optional[dict]:
        """Tell a machine that passed `vet` about a build cache beside it, if this kind of machine
        has one. Returns what the run's record keeps, or None. It never fails a run."""
        return None

    def doctor(self, ledger, cfg: dict, add) -> None:
        """Append readiness checks with add(ok, name, found, fix="", soft=False)."""


_loaded: Dict[str, Backend] = {}
_modules: List[str] = []


def machine_name(prefix: str, run_id: str, limit: int = 60) -> str:
    """A hostname for a run's machine. A long name is cut in the middle, so the run number survives."""
    name = re.sub(r"[^a-z0-9-]", "-", (prefix + run_id).lower()).strip("-")
    if len(name) > limit:
        tail = re.search(r"-r\d+$", name)
        end = tail.group(0) if tail else ""
        name = name[:limit - len(end)].rstrip("-") + end
    return name


def load(cfg: dict) -> Dict[str, Backend]:
    """Import the built-in backends and any plugins the config names. Idempotent."""
    wanted = BUILTIN + list(((cfg.get("machines") or {}).get("plugins")) or [])
    for name in wanted:
        if name in _modules:
            continue
        try:
            module = importlib.import_module(name)
        except ImportError as e:
            raise BackendError("backend plugin %s cannot be imported: %s" % (name, e))
        backend = getattr(module, "BACKEND", None)
        if not isinstance(backend, Backend) or not backend.kind:
            raise BackendError("%s does not define BACKEND, a Backend with a kind" % name)
        _loaded[backend.kind] = backend
        _modules.append(name)
    return {k: b for k, b in _loaded.items() if _module_of(b) in wanted}


def _module_of(backend: Backend) -> str:
    return type(backend).__module__


def kinds(cfg: dict) -> List[str]:
    return sorted(load(cfg))


def get(cfg: dict, kind: str) -> Backend:
    backends = load(cfg)
    if kind not in backends:
        hint = ""
        if kind == "docker":
            hint = ' The Docker backend is a plugin: add "org.backends.docker" to machines.plugins in config.json.'
        raise BackendError("no backend for machine kind %r (have: %s).%s" % (kind, ", ".join(sorted(backends)), hint))
    return backends[kind]


def resolve(cfg: dict, want: str) -> Backend:
    """The backend for a request such as "incus", "ssh:bigbox" or "docker:node"."""
    return get(cfg, want.split(":", 1)[0])


def for_machine(machine: dict, cfg: dict) -> Transport:
    """A way to reach a machine recorded in run.json."""
    return get(cfg, machine.get("kind") or "").transport(machine, cfg)
