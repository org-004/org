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

More machines than this one: machines.incus.nodes names each machine whose Incus holds engineers.
A node without `remote` is this machine; one with `remote` is another machine's Incus, reached as
a remote of this user's client (`incus remote add <remote> https://<address>:8443`), with a
template and a build cache of its own that its own `org up` made:

    "nodes": {"vm": {"slots": 1},
              "w1": {"remote": "w1", "slots": 1, "memory": "auto", "memory_total": "16GiB"}}

`slots`, `memory` and `memory_reserve` are per node and fall back to machines.incus's own;
`memory_total` is what `auto` divides on a remote when its Incus does not say. Without `nodes`,
the pool is this machine alone, with machines.incus.slots, as before. A spec may ask for one node
with `machine: incus:<node>`.
"""
from __future__ import annotations

import os
import re
import shutil
from typing import Dict, List, Optional

from .. import cache, power
from ..transport import REMOTE_RE, Incus, Transport, TransportError
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


def node_entries(cfg: dict) -> Dict[str, dict]:
    """machines.incus.nodes, checked: node name -> {"remote": name or None, "slots": n, ...}.

    Without nodes, this machine alone as the node `vm`, with machines.incus.slots, or no pool
    at all when that sets no number (as before nodes existed). A node that is written wrong is
    refused here, before anything is placed on it."""
    raw = ((cfg.get("machines") or {}).get("incus") or {}).get("nodes")
    if not raw:
        slots = slot_count(cfg)
        return {"vm": {"remote": None, "slots": slots}} if slots else {}
    if not isinstance(raw, dict):
        raise BackendError("machines.incus.nodes must map a node's name to its settings, not %r" % (raw,))
    out, remotes = {}, set()
    for name, entry in sorted(raw.items()):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", str(name)) or not isinstance(entry, dict):
            raise BackendError("machines.incus.nodes.%s: a node is a lowercase name with settings {remote, slots, memory}" % name)
        remote = entry.get("remote") or None
        if remote is not None and not REMOTE_RE.fullmatch(str(remote)):
            raise BackendError("machines.incus.nodes.%s.remote %r is not the name of an Incus remote" % (name, remote))
        if remote in remotes:
            raise BackendError("machines.incus.nodes: two nodes are %s" % (("the remote %s" % remote) if remote else "this machine"))
        remotes.add(remote)
        slots = entry.get("slots", 1)
        if isinstance(slots, bool) or not isinstance(slots, int) or slots < 1:
            raise BackendError("machines.incus.nodes.%s.slots must be a whole number of 1 or more, not %r" % (name, slots))
        out[name] = dict(entry, remote=remote, slots=slots)
    return out


def node_entry(cfg: dict, node: Optional[str]) -> Optional[dict]:
    """The settings of one node, or None for a run placed before nodes existed (this machine)."""
    if not node:
        return None
    nodes = node_entries(cfg)
    if node not in nodes:
        raise BackendError("the node %s is not in machines.incus.nodes" % node)
    return nodes[node]


def memory_reserve_mib(cfg: dict, entry: Optional[dict] = None) -> int:
    """machines.incus.memory_reserve in MiB: what the automatic limit leaves to the machine itself."""
    raw = (entry or {}).get("memory_reserve")
    if raw is None:
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


def memory_limit(cfg: dict, mem_total_mib: Optional[int] = None, node: Optional[str] = None) -> Optional[str]:
    """The hard memory limit an engineer's container gets, as Incus writes one, or None for no limit.

    machines.incus.memory decides. Absent or "auto" is the arithmetic that makes the containers
    fit in the machine together: (the machine's memory - the reserve) / slots, in whole MiB. A
    size is taken as given, for an owner who accepts overcommitting, and "none" is no limit.
    Anything else is refused here, before a container is made: a limit that was mistyped must
    not quietly become no limit.

    With `node`, that node's settings come first, and its slots and its machine's memory are
    what is divided: a remote's memory is what its Incus says, else its `memory_total`.
    """
    entry = node_entry(cfg, node)
    want = (entry or {}).get("memory")
    if want is None:
        want = ((cfg.get("machines") or {}).get("incus") or {}).get("memory")
    if want is None or want == "auto":
        reserve = memory_reserve_mib(cfg, entry)
        slots = entry["slots"] if entry else slot_count(cfg)
        remote = (entry or {}).get("remote")
        if mem_total_mib is not None:
            total = mem_total_mib
        elif remote:
            total = size_mib((entry or {}).get("memory_total")) or Incus.memory_mib(remote)
            if total is None:
                raise BackendError("machines.incus.nodes.%s: memory is automatic and the remote %s does not say how much memory "
                                   "its machine has. Set machines.incus.nodes.%s.memory_total (such as 16GiB) or .memory."
                                   % (node, remote, node))
        else:
            total = machine_memory_mib()
        if not slots or total is None:
            return None  # nothing to divide, or nothing to divide by: `org doctor` says so
        each = (int(total) - reserve) // slots
        if each < 1:
            raise BackendError("machines.incus.memory is automatic and comes to nothing: %s has %d MiB and "
                               "machines.incus.memory_reserve keeps %d MiB of it. Lower the reserve, or set "
                               "machines.incus.memory to a size or to none."
                               % ("the node %s" % node if remote else "this machine", int(total), reserve))
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
        wanted = want.split(":", 1)[1] if ":" in want else None
        if wanted is not None and wanted not in node_entries(cfg):
            raise BackendError("a container is made fresh for each run and takes no name; `incus:<node>` names a node of "
                               "machines.incus.nodes (%s), and %r is none of them"
                               % (", ".join(sorted(node_entries(cfg))) or "there are none", wanted))
        s = self._settings(cfg)
        name = self._own(cfg, machine_name(s["prefix"], run_id))
        if str(s["base"]).startswith(str(s["prefix"])):
            raise BackendError("the template %r starts with the engineers' prefix %r, so a run could be asked to delete it"
                               % (s["base"], s["prefix"]))
        return {"kind": "incus", "name": name, "base": s["base"],
                "push_token": s.get("push_token", True), "node": None, "wanted_node": wanted}

    def nodes(self, cfg: dict) -> Dict[str, dict]:
        return {name: {"slots": e["slots"], "host": ("remote %s" % e["remote"]) if e["remote"] else None}
                for name, e in node_entries(cfg).items()}

    def ready(self, cfg: dict, node: str) -> bool:
        entry = node_entries(cfg).get(node) or {}
        remote = entry.get("remote")
        if not remote:
            return True  # this machine
        base = self._settings(cfg)["base"]

        def ask():
            Incus.info(base, 8, remote=remote)  # raises when the remote does not answer
            return True
        return power.ready(node, ask)

    def wake(self, ledger, cfg: dict, node: str, log=lambda message: None) -> bool:
        return power.wake(ledger, node, node_entries(cfg).get(node) or {}, log)

    def waking(self, ledger, cfg: dict) -> List[str]:
        return power.waking(ledger, node_entries(cfg))

    def power_tick(self, ledger, cfg: dict, load: Dict[str, int], log=lambda message: None) -> None:
        power.idle(ledger, node_entries(cfg), load, lambda n: self.ready(cfg, n), log)

    def assign(self, cfg: dict, machine: dict, node: str) -> None:
        machine["node"] = node
        remote = node_entries(cfg)[node]["remote"]
        if remote:
            machine["remote"] = remote
        else:
            machine.pop("remote", None)

    def provision(self, machine: dict, run_id: str, cfg: dict) -> Transport:
        name = self._own(cfg, machine["name"])
        base = machine.get("base") or self._settings(cfg)["base"]
        remote = machine.get("remote")
        # Asked before anything is made: a setting that is wrong fails the dispatch, not the copy.
        limit = memory_limit(cfg, node=self._limit_node(cfg, machine))
        if Incus.info(base, remote=remote) is None:
            raise TransportError("the template container %s does not exist%s: `org up` makes it"
                                 % (base, (" on the remote %s" % remote) if remote else ""))
        if limit is None:
            # The record says what the container was given, and it was given no limit.
            limits = machine.pop("limits", None) or {}
            limits.pop("memory", None)
            if limits:
                machine["limits"] = limits
            return Incus.create(base, name, remote=remote)
        machine["limits"] = dict(machine.get("limits") or {}, memory=limit)
        try:
            return Incus.create(base, name, {"limits.memory": limit}, remote=remote)
        except TransportError as e:
            # Only the copy is given the key. A container that started and got no address failed
            # for another reason, and the pointer would send the owner the wrong way.
            if not str(e).startswith("incus copy "):
                raise
            raise TransportError("%s. The copy was asked for limits.memory=%s; if Incus will not take that key here, %s."
                                 % (str(e).rstrip("."), limit, WITHOUT_A_LIMIT))

    def _limit_node(self, cfg: dict, machine: dict) -> Optional[str]:
        """The node whose settings size this machine's memory: its own when nodes are configured.
        The implicit `vm` of a ledger without nodes is sized as before, from machines.incus."""
        node = machine.get("node")
        raw = ((cfg.get("machines") or {}).get("incus") or {}).get("nodes")
        return node if node and raw else None

    def vet(self, machine: dict, facts: dict, cfg: dict) -> None:
        info = Incus.info(self._own(cfg, machine["name"]), remote=machine.get("remote"))
        if info is None:
            raise BackendError("%s is gone" % machine["name"])
        found = problems(info)
        if found and not self._settings(cfg).get("allow_open"):
            raise BackendError("%s is not a box for an engineer, which runs with every permission bypassed: %s. "
                               "`org up` makes a template that is; machines.incus.allow_open overrides this."
                               % (machine["name"], "; ".join(found)))
        # What was asked of the copy is read back from Incus, as the rest is, and allow_open does
        # not excuse it: a limit the owner set is not one to lose quietly.
        asked = (machine.get("limits") or {}).get("memory")
        if asked:
            config = info.get("expanded_config") or {}
            got = config.get("limits.memory")
            if got != asked:
                raise BackendError("%s was asked for limits.memory=%s and Incus describes it with %s, so its memory is not "
                                   "limited as asked. If Incus will not keep that key here, %s."
                                   % (machine["name"], asked, "no limits.memory" if got is None else "limits.memory=%s" % got,
                                      WITHOUT_A_LIMIT))
            if str(config.get("limits.memory.enforce", "")).lower() == "soft":
                raise BackendError("%s was asked for a hard limits.memory=%s and Incus describes it with limits.memory.enforce=soft, "
                                   "so the limit is not hard. Remove limits.memory.enforce where it is set (a profile, most "
                                   "likely), or %s." % (machine["name"], asked, WITHOUT_A_LIMIT))
            # This is what the engineer is told, and only a limit Incus confirmed is told.
            facts["memory_limit"] = asked

    def transport(self, machine: dict, cfg: dict) -> Transport:
        return Incus(self._own(cfg, machine["name"]), machine.get("remote"))

    def remove(self, machine: dict, cfg: dict) -> Optional[str]:
        name, remote = self._own(cfg, machine["name"]), machine.get("remote")
        if Incus.info(name, remote=remote) is None:
            return "%s was already gone" % Incus.at(remote, name)
        Incus.delete(name, remote)
        return "deleted %s" % Incus.at(remote, name)

    def before_resume(self, machine: dict, cfg: dict) -> None:
        # After this machine restarts, a run's container is stopped and its memory is empty.
        name, remote = self._own(cfg, machine["name"]), machine.get("remote")
        info = Incus.info(name, remote=remote)
        limit = (memory_limit(cfg, node=self._limit_node(cfg, machine))
                 if info is not None and info.get("status") != "Running" else None)
        if limit is not None:
            # A stopped container takes a new limit when it starts; a running one is left as it is.
            try:
                Incus.set_config(name, "limits.memory", limit, remote)
            except TransportError as e:
                raise TransportError("%s. If Incus will not take limits.memory here, %s." % (str(e).rstrip("."), WITHOUT_A_LIMIT))
            # Read back before the start, as `vet` reads a new container's: what the record and the
            # engineer are told is what Incus says, not what it was asked.
            got = ((Incus.info(name, remote=remote) or {}).get("expanded_config") or {}).get("limits.memory")
            if got != limit:
                raise BackendError("%s was asked for limits.memory=%s and Incus describes it with %s, so its memory is not "
                                   "limited as asked. If Incus will not keep that key here, %s."
                                   % (name, limit, "no limits.memory" if got is None else "limits.memory=%s" % got, WITHOUT_A_LIMIT))
            machine["limits"] = dict(machine.get("limits") or {}, memory=limit)
        Incus.start(name, remote)

    def resume_facts(self, machine: dict, cfg: dict) -> dict:
        # The limit on record is one Incus confirmed, at dispatch or just now in `before_resume`.
        limit = (machine.get("limits") or {}).get("memory")
        return {"memory_limit": limit} if limit else {}

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
        try:
            nodes = node_entries(cfg)
        except BackendError as e:
            add(False, "machines.incus.nodes", str(e), "correct it with `org config set`, as it says")
            return
        for node, entry in sorted(nodes.items()):
            remote = entry["remote"]
            if not remote:
                continue
            try:
                info = Incus.info(s["base"], 30, remote=remote)
            except TransportError as e:
                if power.settings(entry):
                    add(True, "node %s (remote %s)" % (node, remote), "not answering: started when a run is queued for it")
                    continue
                add(False, "node %s (remote %s)" % (node, remote), str(e)[:200],
                    "incus remote add %s https://<address>:8443, with this user's certificate trusted there" % remote)
                continue
            found = problems(info) if info else ["missing"]
            add(not found, "node %s: template `%s`" % (node, s["base"]),
                "; ".join(found) if found else "%s, isolated" % (info.get("status") or "?").lower(),
                "`org up` on that machine")
            cache.doctor(cfg, add, remote=remote)
        if ((cfg.get("machines") or {}).get("incus") or {}).get("nodes"):
            for node, entry in sorted(nodes.items()):
                try:
                    limit = memory_limit(cfg, node=node)
                except (BackendError, TransportError) as e:
                    add(False, "node %s memory per engineer" % node, str(e)[:300], "set it as it says", soft=True)
                    continue
                add(True, "node %s" % node, "%s, %d at once, %s each" % (
                    ("remote %s" % entry["remote"]) if entry["remote"] else "this machine", entry["slots"], limit or "no limit"))
            return
        add(True, "engineers at once", ("%d" % int(s["slots"])) if s.get("slots") else "no limit set (machines.incus.slots)")
        try:
            limit, reserve = memory_limit(cfg), memory_reserve_mib(cfg)
        except BackendError as e:
            add(False, "memory per engineer", str(e), "correct it with `org config set`, as it says", soft=True)
            return
        total, slots, want = machine_memory_mib(), slot_count(cfg), s.get("memory")
        found = "%s for %s at once; %d MiB kept for the machine, which has %s (machines.incus.memory %s)" % (
            "%s each" % limit if limit else "no limit", slots or "any number", reserve,
            "an amount nothing says" if total is None else "%d MiB" % total, "auto" if want is None else want)
        each = size_mib(limit) if limit else None
        if each is None:
            fix = ("nothing stops one engineer's build from taking the memory the others and the machine need: "
                   "`org config set machines.incus.memory auto`, or a size such as 4GiB" if want == "none" else
                   "the automatic limit has nothing to divide: set machines.incus.slots, or machines.incus.memory to a size such as 4GiB")
        elif slots and total is not None and each * slots > total - reserve:
            fix = ("%d engineers at %s each can take more than the %d MiB left after the reserve: lower machines.incus.memory "
                   "or machines.incus.slots, or set machines.incus.memory to auto" % (slots, limit, total - reserve))
        elif each < 2048:
            fix = "under 2GiB is too little for Claude Code and a build: " + (
                "lower machines.incus.slots or machines.incus.memory_reserve, or set machines.incus.memory to a size such as 4GiB"
                if want in (None, "auto") else "set machines.incus.memory to 2GiB or more")
        else:
            fix = ""
        add(not fix, "memory per engineer", found, fix, soft=True)


BACKEND = IncusBackend()
