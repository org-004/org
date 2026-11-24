"""Incus containers: a fresh one per run, copied from a template. The default backend.

An Incus system container is a whole Linux system: an engineer in one is root there, has an init
and a network of its own, and can run a Docker daemon, which a plain Docker container cannot.
On a copy-on-write pool a copy of the template takes a few hundredths of a second.

The manager runs beside the containers, on the machine that holds them, and creates them itself
with the `incus` command. That machine is the outer wall (`org-vm` makes it a VM with nothing of
the host's in it); the container is the inner one, and this module is what checks it:

  - only containers whose names start with the prefix are ever created, entered or deleted
  - a container must be unprivileged, with no device but its own disk and a network card whose
    port is isolated and whose address is pinned, so it reaches the internet and, beside it,
    only the one port of the build cache's container (org/cache.py), whose card is not isolated.
    That is read from Incus's description of the container, not from anything inside it.
  - it starts blank: no login is baked into the template. Each run's tokens are copied into the
    container's memory at dispatch.
"""
from __future__ import annotations

import os
import re
import shutil
from typing import Dict, List, Optional

from .. import cache
from ..transport import Incus, Transport, TransportError
from . import Backend, BackendError, machine_name

DEFAULTS = {"base": "org-base", "prefix": "org-eng-", "slots": 4, "push_token": True}

# What the machine keeps for everything outside the engineers' containers (its own services,
# Incus's daemon, the manager chat) when machines.incus.memory_reserve does not say.
RESERVE = "3GiB"
SIZE = re.compile(r"([1-9][0-9]*)(MiB|GiB)")
WITHOUT_A_LIMIT = "`org config set machines.incus.memory none` dispatches without a limit"


def size_mib(text) -> Optional[int]:
    """A size as the settings write one, in MiB: a positive whole number followed by MiB or GiB."""
    found = SIZE.fullmatch(text) if isinstance(text, str) else None
    return int(found.group(1)) * (1024 if found.group(2) == "GiB" else 1) if found else None


def machine_memory_mib() -> Optional[int]:
    """This machine's memory in whole MiB, or None where nothing says: only Linux has the file."""
    try:
        # $ORG_MEMINFO is for the tests, whose arithmetic must not depend on where they run.
        with open(os.environ.get("ORG_MEMINFO") or "/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def memory_reserve_mib(cfg: dict) -> int:
    """machines.incus.memory_reserve in MiB: what the automatic limit leaves to the machine itself."""
    raw = ((cfg.get("machines") or {}).get("incus") or {}).get("memory_reserve")
    reserve = size_mib(RESERVE if raw is None else raw)
    if reserve is None:
        raise BackendError("machines.incus.memory_reserve %r is not a size: write a whole number followed by MiB or GiB, "
                           "such as 3GiB" % (raw,))
    return reserve


def slot_count(cfg: dict) -> Optional[int]:
    """How many engineers may run at once, or None when machines.incus.slots sets no usable number."""
    slots = dict(DEFAULTS, **((cfg.get("machines") or {}).get("incus") or {})).get("slots")
    try:
        return int(slots) if slots and not isinstance(slots, bool) and int(slots) > 0 else None
    except (TypeError, ValueError):
        return None


def memory_limit(cfg: dict, mem_total_mib: Optional[int] = None) -> Optional[str]:
    """The hard memory limit an engineer's container gets, as Incus writes one, or None for no limit.

    machines.incus.memory decides. Absent or "auto" is the arithmetic that makes the containers
    fit in the machine together: (the machine's memory - the reserve) / slots, in whole MiB. A
    size is taken as given, for an owner who accepts overcommitting, and "none" is no limit.
    Anything else is refused here, before a container is made: a limit that was mistyped must
    not quietly become no limit.
    """
    want = ((cfg.get("machines") or {}).get("incus") or {}).get("memory")
    if want is None or want == "auto":
        reserve, slots = memory_reserve_mib(cfg), slot_count(cfg)
        total = machine_memory_mib() if mem_total_mib is None else mem_total_mib
        if not slots or total is None:
            return None  # nothing to divide, or nothing to divide by: `org doctor` says so
        each = (int(total) - reserve) // slots
        if each < 1:
            raise BackendError("machines.incus.memory is automatic and comes to nothing: this machine has %d MiB and "
                               "machines.incus.memory_reserve keeps %d MiB of it. Lower the reserve, or set "
                               "machines.incus.memory to a size or to none." % (int(total), reserve))
        return "%dMiB" % each
    if want == "none":
        return None
    if size_mib(want) is None:
        raise BackendError("machines.incus.memory %r is not auto, none or a size: write a whole number followed by MiB "
                           "or GiB, such as 4GiB" % (want,))
    return want


def problems(info: dict) -> List[str]:
    """Why a container, as Incus describes it, must not hold a session with every permission bypassed."""
    out = []
    config = info.get("expanded_config") or {}
    if str(config.get("security.privileged", "")).lower() == "true":
        out.append("it is privileged: its root is this machine's root")
    for name, dev in sorted((info.get("expanded_devices") or {}).items()):
        kind = dev.get("type")
        if kind == "disk":
            if dev.get("path") != "/" or dev.get("source"):
                out.append("device %s shares %s from this machine" % (name, dev.get("source") or dev.get("path")))
        elif kind == "nic":
            for key, why in (("security.port_isolation", "it can reach the other containers"),
                             ("security.ipv4_filtering", "it can take another address")):
                if str(dev.get(key, "")).lower() != "true":
                    out.append("network card %s has no %s: %s" % (name, key, why))
        else:
            out.append("device %s (%s) gives it part of this machine" % (name, kind))
    return out


class IncusBackend(Backend):
    kind = "incus"
    blank = True  # nothing is inherited from the template: the tokens arrive with each run

    @staticmethod
    def _settings(cfg: dict) -> dict:
        return dict(DEFAULTS, **((cfg.get("machines") or {}).get("incus") or {}))

    def _own(self, cfg: dict, name: str) -> str:
        """The one check every action passes: this backend touches only its own containers."""
        prefix = str(self._settings(cfg).get("prefix") or "")
        if not re.fullmatch(r"[a-z][a-z0-9-]*-", prefix):
            raise BackendError("machines.incus.prefix %r must be lowercase letters, digits and dashes, and end in a dash" % prefix)
        if not (isinstance(name, str) and name.startswith(prefix) and len(name) <= 63
                and re.fullmatch(r"[a-z0-9][a-z0-9-]*", name[len(prefix):])):
            raise BackendError("org only acts on containers named %s<run>, not %r" % (prefix, name))
        return name

    def choose(self, cfg: dict, want: str, run_id: str) -> dict:
        if ":" in want:
            raise BackendError("a container is made fresh for each run and takes no name: write `incus`, not %r" % want)
        s = self._settings(cfg)
        name = self._own(cfg, machine_name(s["prefix"], run_id))
        if str(s["base"]).startswith(str(s["prefix"])):
            raise BackendError("the template %r starts with the engineers' prefix %r, so a run could be asked to delete it"
                               % (s["base"], s["prefix"]))
        return {"kind": "incus", "name": name, "base": s["base"],
                "push_token": s.get("push_token", True), "node": None}

    def nodes(self, cfg: dict) -> Dict[str, dict]:
        slots = self._settings(cfg).get("slots")
        return {"vm": {"slots": int(slots), "host": None}} if slots else {}

    def provision(self, machine: dict, run_id: str, cfg: dict) -> Transport:
        name = self._own(cfg, machine["name"])
        base = machine.get("base") or self._settings(cfg)["base"]
        if Incus.info(base) is None:
            raise TransportError("the template container %s does not exist: `org up` makes it" % base)
        return Incus.create(base, name)

    def vet(self, machine: dict, facts: dict, cfg: dict) -> None:
        info = Incus.info(self._own(cfg, machine["name"]))
        if info is None:
            raise BackendError("%s is gone" % machine["name"])
        found = problems(info)
        if found and not self._settings(cfg).get("allow_open"):
            raise BackendError("%s is not a box for an engineer, which runs with every permission bypassed: %s. "
                               "`org up` makes a template that is; machines.incus.allow_open overrides this."
                               % (machine["name"], "; ".join(found)))

    def transport(self, machine: dict, cfg: dict) -> Transport:
        return Incus(self._own(cfg, machine["name"]))

    def remove(self, machine: dict, cfg: dict) -> Optional[str]:
        name = self._own(cfg, machine["name"])
        if Incus.info(name) is None:
            return "%s was already gone" % name
        Incus.delete(name)
        return "deleted %s" % name

    def before_resume(self, machine: dict, cfg: dict) -> None:
        # After this machine restarts, a run's container is stopped and its memory is empty.
        Incus.start(self._own(cfg, machine["name"]))

    def build_cache(self, tr: Transport, cfg: dict, log, again: bool = False) -> Optional[dict]:
        # The cache is a container `org up` made, outside the prefix: this backend only reads
        # Incus's description of it, and what is done is done in the engineer's container.
        return cache.offer(tr, cfg, log, again)

    def doctor(self, ledger, cfg: dict, add) -> None:
        s = self._settings(cfg)
        path = shutil.which("incus")
        add(bool(path), "incus", path or "not found", "this machine holds the engineers' containers: `org-vm up` on the host makes one that has it")
        if path:
            try:
                info = Incus.info(s["base"])
            except TransportError as e:
                info = None
                add(False, "incus answers", str(e)[:200], "is this user in the `incus` group?")
            if info is None:
                add(False, "template container `%s`" % s["base"], "missing", "org up")
            else:
                found = problems(info)
                add(not found, "template container `%s`" % s["base"],
                    "; ".join(found) if found else "%s, isolated" % (info.get("status") or "?").lower(), "org up --rebuild-template")
            cache.doctor(cfg, add)
        add(True, "engineers at once", ("%d" % int(s["slots"])) if s.get("slots") else "no limit set (machines.incus.slots)")


BACKEND = IncusBackend()
