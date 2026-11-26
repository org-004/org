"""Machines that start when work is queued for them and stop when they have been idle.

A node of machines.incus.nodes may say how it is started and stopped:

    "w1": {"remote": "w1", "slots": 1,
           "power": {"start": ["/path/to/node.sh", "start", "w1"], "stop": ["/path/to/node.sh", "stop", "w1"],
                     "idle_stop_min": 20}}

`start` and `stop` are commands this user runs, detached, with no shell. They are given nothing
on stdin and their output goes to <ledger>/logs/power-<node>.log. org does not know what a node
is (a cloud VM, a machine on a power switch): the commands do. `start` must leave the node
answering as the remote it is, wherever its address now is.

- A run placed on a node that does not answer is queued, and the node is started, at most once
  every WAKE_EVERY_S. The queue moves when the node answers, as it moves when a slot frees.
- A node with power that has held no run for idle_stop_min (default 20) is stopped, once.
  A node without power is never started or stopped by org.

What was done is kept in <ledger>/power.json: per node, when it was last woken, last seen busy,
last stopped. Nothing here raises: a command that fails is a line in its log, and the next
pump tries again.
"""
from __future__ import annotations

import os
import subprocess
import time
from typing import Dict, List, Optional

from .util import read_json, write_json

WAKE_EVERY_S = 300  # a node is asked to start no more often than this
READY_CACHE_S = 30  # how long an answer about a node is believed
IDLE_STOP_MIN = 20

_ready: Dict[str, tuple] = {}  # node -> (answered at, whether it answered)


def settings(entry: Optional[dict]) -> Optional[dict]:
    """A node's power settings, or None when org does not start and stop it."""
    power = (entry or {}).get("power")
    if not isinstance(power, dict):
        return None
    start, stop = power.get("start"), power.get("stop")
    good = lambda argv: isinstance(argv, list) and argv and all(isinstance(a, str) and a for a in argv)  # noqa: E731
    if not good(start) or not good(stop):
        return None
    idle = power.get("idle_stop_min", IDLE_STOP_MIN)
    return {"start": start, "stop": stop,
            "idle_stop_min": float(idle) if isinstance(idle, (int, float)) and not isinstance(idle, bool) and idle > 0 else IDLE_STOP_MIN}


def _path(ledger):
    return ledger.root / "power.json"


def _state(ledger) -> dict:
    return read_json(_path(ledger)) or {}


def _save(ledger, state: dict) -> None:
    write_json(_path(ledger), state)


def _run(ledger, node: str, argv: List[str]) -> Optional[str]:
    """Start a command detached; None when it started, else why not."""
    logs = ledger.root / "logs"
    try:
        logs.mkdir(parents=True, exist_ok=True)
        out = open(logs / ("power-%s.log" % node), "a")
        out.write("%s $ %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(argv)))
        out.flush()
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        out.close()
        return None
    except OSError as e:
        return str(e)


def ready(node: str, ask) -> bool:
    """Whether the node answers, asked through `ask()` (True or False, never raising) and believed
    for READY_CACHE_S, so that a pump that runs every few seconds does not wait on a node that
    is off each time."""
    seen = _ready.get(node)
    if seen and time.monotonic() - seen[0] < READY_CACHE_S:
        return seen[1]
    try:
        answer = bool(ask())
    except Exception:  # noqa: BLE001
        answer = False
    _ready[node] = (time.monotonic(), answer)
    return answer


def forget(node: str) -> None:
    _ready.pop(node, None)


def wake(ledger, node: str, entry: dict, log=lambda message: None) -> bool:
    """Ask a node with power to start, unless it was asked lately. True when it was asked now."""
    power = settings(entry)
    if not power:
        return False
    state = _state(ledger)
    mine = state.setdefault(node, {})
    if time.time() - float(mine.get("woken_at") or 0) < WAKE_EVERY_S:
        return False
    why = _run(ledger, node, power["start"])
    mine["woken_at"] = time.time()
    _save(ledger, state)
    forget(node)
    log("power: starting %s%s" % (node, (" failed: " + why) if why else ""))
    return why is None


def waking(ledger, nodes: Dict[str, dict]) -> List[str]:
    """Nodes asked to start in the last ten minutes: what a queued run is waiting for."""
    state = _state(ledger)
    return sorted(n for n in nodes if settings(nodes[n]) and time.time() - float((state.get(n) or {}).get("woken_at") or 0) < 600)


def idle(ledger, nodes: Dict[str, dict], load: Dict[str, int], is_up, log=lambda message: None) -> List[str]:
    """Stop each node with power that has held no run for its idle_stop_min. Returns those stopped."""
    state, changed, stopped = _state(ledger), False, []
    t = time.time()
    for node, entry in sorted(nodes.items()):
        power = settings(entry)
        if not power:
            continue
        mine = state.setdefault(node, {})
        if load.get(node):
            mine["busy_at"] = t
            changed = True
            continue
        since = max(float(mine.get("busy_at") or 0), float(mine.get("woken_at") or 0))
        if not since:
            mine["busy_at"] = since = t  # first seen: idle from now
            changed = True
        if t - since < power["idle_stop_min"] * 60:
            continue
        if float(mine.get("stopped_at") or 0) > since:
            continue  # stopped already since it was last busy
        if not is_up(node):
            continue
        why = _run(ledger, node, power["stop"])
        mine["stopped_at"] = t
        changed = True
        forget(node)
        stopped.append(node)
        log("power: stopping %s, idle for %d min%s" % (node, (t - since) // 60, (" failed: " + why) if why else ""))
    if changed:
        _save(ledger, state)
    return stopped
