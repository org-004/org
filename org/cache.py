"""The build cache: one container beside the engineers' that every engineer reads and writes.

`org up` makes it (incus/up.sh, incus/cache.sh): a container of its own, `org-cache` unless
machines.incus.cache.name says otherwise, running `bazel-remote`, which serves Bazel's remote
cache over gRPC on one port. It holds no login, no token and no key, and asks for none: what
protects it is that only containers on this machine's bridge can reach it.

Nothing here creates, enters or deletes that container. This module only reads Incus's
description of it (`incus list`) and, at dispatch, acts inside the engineer's own container:

  - it tries, from there, to open a connection to the cache's address and port;
  - only when that worked, it writes Bazel's system-wide settings file there, pointing at it.

A build must never fail or wait because of the cache, so every failure here ends in "builds will
not use it" and the run goes on. With machines.incus.cache.enabled false none of this happens,
and nothing is asked of Incus.
"""
from __future__ import annotations

import ipaddress
import re
from typing import List, Optional

from .transport import Incus, Transport, TransportError

DEFAULTS = {"enabled": True, "name": "org-cache", "port": 9092, "max_gb": 20}
#: Where Bazel on Linux reads the settings of the whole system, before the user's and the project's.
BAZELRC = "/etc/bazel.bazelrc"
#: The first line of the file dispatch writes. Only a file that opens with it is ever removed again.
MARK = "# Written by org at dispatch"
#: How long the engineer's container may take to open the connection.
PROBE_SECONDS = 3
#: The two things that can stand between an engineer's container and the cache.
IN_THE_WAY = "the VM's firewall, or port isolation on the cache's network card"

_PROBE = ("python3 -c 'import socket, sys\n"
          "socket.create_connection((sys.argv[1], int(sys.argv[2])), %d).close()' %%s %%d" % PROBE_SECONDS)
# The tests' stand-in for Incus runs a container's commands on the test's own machine, and names
# another file here; in a real container the variable is not set.
# Written beside its place and moved there, so that a write that fails part-way leaves no half of it.
_WRITE = 'f="${ORG_BAZELRC:-%s}"; cat > "$f.org-new" && mv -f "$f.org-new" "$f" || { rm -f "$f.org-new"; exit 1; }' % BAZELRC
_FORGET = 'f="${ORG_BAZELRC:-%s}"; if head -n 1 "$f" 2>/dev/null | grep -q "^%s"; then rm -f "$f"; fi' % (BAZELRC, MARK)


def settings(cfg: dict) -> dict:
    """machines.incus.cache, over the defaults, read as incus/up.sh reads it: only a plain
    `false` switches the cache off, a name that is not given is the default one, and a port
    written as a string of digits is that number."""
    incus = (cfg.get("machines") or {}).get("incus")
    own = incus.get("cache") if isinstance(incus, dict) else None
    s = dict(DEFAULTS, **{k: v for k, v in (own if isinstance(own, dict) else {}).items() if v is not None and v != ""})
    s["enabled"] = s["enabled"] is not False
    if isinstance(s["port"], str) and s["port"].isdigit():
        s["port"] = int(s["port"])
    return s


def _prefix(cfg: dict) -> str:
    from .backends.incus import DEFAULTS as INCUS  # here, so that neither module needs the other to load

    return str(((cfg.get("machines") or {}).get("incus") or {}).get("prefix") or INCUS["prefix"])


def _misnamed(cfg: dict) -> Optional[str]:
    """Why the configured name cannot be the cache's, or None. The engineers' prefix marks the
    containers a run may be asked to delete, and the cache is never one of those."""
    name, prefix = str(settings(cfg)["name"]), _prefix(cfg)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,62}", name):
        return "machines.incus.cache.name %r is not a container's name" % name
    if name.startswith(prefix):
        return "its name `%s` starts with the engineers' prefix `%s`" % (name, prefix)
    return None


def _port(cfg: dict) -> Optional[int]:
    port = settings(cfg).get("port")
    return port if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536 else None


def address(info: dict) -> Optional[str]:
    """The container's IPv4 address as Incus reports it, or None when it has none yet."""
    state = info.get("state")
    network = state.get("network") if isinstance(state, dict) else None
    for card in sorted(network if isinstance(network, dict) else {}, key=lambda name: (name != "eth0", name)):
        if card == "lo" or not isinstance(network[card], dict):
            continue
        found = network[card].get("addresses")
        for one in found if isinstance(found, list) else []:
            if isinstance(one, dict) and one.get("family") == "inet" and one.get("scope") == "global":
                try:
                    return str(ipaddress.IPv4Address(one.get("address")))
                except ValueError:
                    continue
    return None


def faults(info: dict) -> List[str]:
    """Why the cache container, as Incus describes it, is more than a place to keep build results.

    This is not the check an engineer's container passes (backends/incus.py, `problems`): the
    cache's network card is deliberately not isolated from the other containers, because an
    isolated card cannot reach another isolated card and the engineers' cards are isolated."""
    out = []
    config, devices = info.get("expanded_config"), info.get("expanded_devices")
    if not isinstance(config, dict) or not isinstance(devices, dict):
        return ["Incus's description of it has no configuration or no devices to read"]
    if str(config.get("security.privileged", "")).lower() == "true":
        out.append("it is privileged: its root is this machine's root")
    for name, dev in sorted(devices.items()):
        kind = dev.get("type") if isinstance(dev, dict) else None
        if kind == "disk" and dev.get("path") == "/" and not dev.get("source"):
            continue
        if kind == "nic":
            continue
        out.append("device %s (%s) is more than its own disk and its network card" % (name, kind))
    return out


def bazelrc(address: str, port: int) -> str:
    """Bazel's system-wide settings for an engineer's container that reached the cache.

    Only what the cache needs: where it is, that what is built here is uploaded, and the two
    options that together make Bazel 9 build locally when the cache stops answering part-way
    (without the second, the first applies to remote execution only and the build fails).
    Bazel 9.3.0 was seen to do that without an error and without a warning either: the only
    sign is a wait of some seconds. No disk cache, no limit on cores, nothing about any project.

    The second of them exists from Bazel 8.6.0 on (8.5.0 and 7.7.3 were seen not to know it),
    and Bazel has no way to write an option that an older version may skip: a project pinned
    to an older Bazel fails every build at that line until the file is removed. The file says
    so itself, because the engineer that meets the error is the one who can remove it."""
    return "\n".join([
        MARK + ", in this engineer's container only, after it reached the build cache.",
        "# The cache is a container on the same machine that every engineer reads and writes, so a",
        "# first build downloads what an earlier engineer built. If it stops answering, Bazel builds",
        "# locally instead and the build still succeeds. A project without Bazel never reads this file.",
        "# The last line needs Bazel 8.6 or later (9.3.0 is what was tried). An older Bazel stops at it",
        "# with \"Unrecognized option\": remove this file there, and builds go on without the cache.",
        "build --remote_cache=grpc://%s:%d" % (address, port),
        "build --remote_upload_local_results=true",
        "build --remote_local_fallback",
        "build --incompatible_remote_local_fallback_for_remote_cache",
    ]) + "\n"


def look(cfg: dict) -> dict:
    """The cache as `incus list` shows it: {"state": off, misnamed, missing, stopped or running, ...}.
    Raises TransportError when Incus does not answer."""
    s = settings(cfg)
    if not s["enabled"]:
        return {"state": "off"}
    name = str(s["name"])
    why = _misnamed(cfg)
    if why:
        return {"state": "misnamed", "name": name, "why": why}
    info = Incus.info(name)
    if not isinstance(info, dict):
        return {"state": "missing", "name": name}
    seen = {"name": name, "faults": faults(info), "port": _port(cfg)}
    if info.get("status") != "Running":
        return dict(seen, state="stopped")
    return dict(seen, state="running", address=address(info))


def reaches(tr: Transport, address: str, port: int) -> bool:
    """Whether the machine behind `tr` can open a TCP connection to address:port within three seconds."""
    return tr.run(_PROBE % (address, port), timeout=PROBE_SECONDS + 20, check=False).returncode == 0


def offer(tr: Transport, cfg: dict, log, again: bool = False) -> Optional[dict]:
    """Tell the engineer's container behind `tr` about the build cache, if it can reach it.

    Returns what the run's record keeps, or None when the cache is switched off. `again` is for a
    container that was offered the cache before (a resumed run): the address may have changed,
    and settings written then are taken away when the cache is no longer reached, so that no
    build waits on an address nothing answers at."""
    try:
        seen = look(cfg)
        state = seen["state"]
        if state == "off":
            return None
        kept = {"state": state}
        if state == "misnamed":
            log("build cache not used: %s" % seen["why"])
        elif state == "stopped":
            log("build cache not used: %s is stopped (`org up` starts it)" % seen["name"])
        elif state == "running":
            port = seen["port"]
            if seen["faults"]:
                kept["state"] = "refused"
                log("build cache not used: %s is not what `org up` makes: %s" % (seen["name"], "; ".join(seen["faults"])))
            elif not seen["address"] or not port:
                kept["state"] = "unreachable"
                log("build cache not reachable, so builds will not use it: %s"
                    % ("%s has no IPv4 address" % seen["name"] if port else "machines.incus.cache.port is not a port"))
            else:
                kept.update(address=seen["address"], port=port)
                if reaches(tr, seen["address"], port):
                    tr.run(_WRITE, input=bazelrc(seen["address"], port).encode(), timeout=60)
                    kept["state"] = "reachable"
                    log("build cache reachable at %s:%d" % (seen["address"], port))
                else:
                    kept["state"] = "unreachable"
                    log("build cache not reachable at %s:%d, so builds will not use it (in the way: %s)"
                        % (seen["address"], port, IN_THE_WAY))
        if again and kept["state"] != "reachable":
            tr.run(_FORGET, timeout=60, check=False)
        return kept
    except Exception as e:  # whatever it is, it is the cache's trouble and not the run's
        log("build cache not used: %s" % (str(e) or type(e).__name__)[:200])
        if again:
            try:
                tr.run(_FORGET, timeout=60, check=False)
            except Exception:
                pass
        return {"state": "error"}


def doctor(cfg: dict, add) -> None:
    """One row for `org doctor`, from `incus list` alone."""
    s = settings(cfg)
    row = "build cache `%s`" % s.get("name")
    try:
        seen = look(cfg)
    except TransportError as e:
        add(False, row, str(e)[:200], "is this user in the `incus` group?")
        return
    except Exception as e:
        add(False, row, "could not be read: %s" % (str(e) or type(e).__name__)[:200], "incus list %s" % s.get("name"))
        return
    state = seen["state"]
    if state == "off":
        add(True, "build cache", "switched off (machines.incus.cache.enabled)")
    elif state == "misnamed":
        add(False, row, seen["why"], "choose another machines.incus.cache.name")
    elif state == "missing":
        add(False, row, "missing", "org up")
    elif seen["faults"]:
        add(False, row, "; ".join(seen["faults"]), "incus delete -f %s, then org up" % seen["name"])
    elif state == "stopped":
        add(False, row, "stopped: builds do not use it", "org up", soft=True)
    elif not seen["address"] or not seen["port"]:
        add(False, row, "running, but %s" % ("it has no IPv4 address" if seen["port"] else "machines.incus.cache.port is not a port"),
            "incus restart %s" % seen["name"] if seen["port"] else "set machines.incus.cache.port", soft=True)
    else:
        add(True, row, "running at %s:%d" % (seen["address"], seen["port"]))


def pool_line(cfg: dict) -> Optional[str]:
    """One line for `org pool`: the cache's name, whether it runs, and its address. None when switched off."""
    try:
        seen = look(cfg)
    except Exception:  # `org pool` is about the engineers; the cache's trouble is `org doctor`'s to tell
        return None
    state = seen["state"]
    if state == "off":
        return None
    if state == "running":
        where = ("at %s:%s" % (seen["address"], seen["port"] or "?")) if seen["address"] else "with no address yet"
        return "build cache: %s running %s" % (seen["name"], where)
    if state == "misnamed":
        return "build cache: not used, %s" % seen["why"]
    return "build cache: %s %s%s" % (seen["name"], state, " (`org up` makes it)" if state == "missing" else " (`org up` starts it)")
