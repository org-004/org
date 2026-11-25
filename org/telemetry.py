"""What a run's container measured, read from outside it and kept in the ledger.

A container's own counters (the peak of its memory, its CPU time, how many of its processes the
kernel killed for memory) die with it, and the inside of a container is the engineer's to change.
So they are read here, on the machine that holds the containers, from two places only:

  - Incus's description of the container: one `incus list --format json` call (`Incus.info`),
    which says whether it runs, which project it is in, when it was started, the disk its root
    uses where the storage pool reports that, and a `state` with memory, CPU and processes;
  - the kernel's files for it: `<root>/lxc.payload.<name>/` when the container is in Incus's
    default project and `<root>/lxc.payload.<project>_<name>/` in any other, where `<root>` is
    /sys/fs/cgroup, or $ORG_CGROUP_ROOT, which is how a test points this at files of its own.
    `memory.current`, `memory.peak`, `memory.max`, `pids.current`, `memory.events` (the line
    `oom_kill N`) and `cpu.stat` (the line `usage_usec N`).

Nothing a container says chooses a path here: the name has passed the backend's `_own`, and the
project is Incus's word, used only when it is a plain name.

One measurement is one line of `runs/<run>/machine.jsonl`, a JSON object:

    at                  when, in seconds since the epoch, by this machine's clock
    running             whether Incus says the container is running
    memory_bytes        memory in use               (memory.current, else Incus's state.memory.usage)
    memory_peak_bytes   the peak of memory in use   (memory.peak; Incus's usage_peak only when above zero)
    memory_limit_bytes  the limit                   (memory.max when it is a number, else limits.memory in Incus's description)
    cpu_s               cumulative CPU seconds      (cpu.stat usage_usec, else Incus's state.cpu.usage)
    processes           how many processes          (pids.current, else Incus's state.processes)
    oom_kill            cumulative kills for memory (memory.events; nowhere else)
    disk_bytes          what the root disk uses     (Incus's state.disk.root.usage, where the pool reports it)
    started_at          when Incus started it       (Incus's state.started_at, as Incus writes it)
    runner_alive        whether the runner was alive at the last sync (the record's `alive`: the one value
                        that comes from the engineer's machine)
    last                true on the measurement taken just before the machine is deleted
    note                what could not be read, in words, when something could not

A value that could not be read is null. It is never guessed and never zero.

The record's `telemetry` is the summary of that file, folded from the whole file each time, so a
write of the record lost to another writer is made good by the next measurement:

    samples             how many measurements
    first_at, last_at   the first and the last time
    memory_peak_bytes   the largest peak seen
    memory_limit_bytes  the last limit seen
    cpu_s               CPU seconds, added across restarts
    oom_kill            kills for memory, added across restarts
    restarts            how many times the container was seen restarted: a cumulative counter lower
                        than in the measurement before, or another `started_at`
    disk_bytes          the last disk use seen
    running             whether it ran at the last measurement
    note                the last measurement's note, or null

and the record's `oom_kill` is never less than the summary's.
"""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional

from . import backends
from .backends.incus import size_mib
from .ledger import Ledger
from .transport import Incus
from .util import now

MEASURED_KINDS = ("incus",)  # Docker, ssh and local machines are not measured
INTERVAL_S = 60  # at most one measurement a minute for each run
CALL_TIMEOUT_S = 20  # the one `incus` call a measurement makes
MAX_WORKERS = 32  # runs measured side by side in one pump, each waiting on its own call

# A project is put into a path only when it is a plain name, so no word of Incus's can climb out of the root.
PROJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# The states in which a run has no machine yet, or never got one: sync does not ask them either.
NO_MACHINE_STATES = ("queued", "provisioning", "dispatch_failed")
FIELDS = ("memory_bytes", "memory_peak_bytes", "memory_limit_bytes", "cpu_s", "processes", "oom_kill",
          "disk_bytes", "started_at")


def path(ledger: Ledger, run_id: str) -> Path:
    """Where a run's measurements are kept: beside run.json, not in the mirror."""
    return ledger.run_dir(run_id) / "machine.jsonl"


# ---------------------------------------------------------------- values


def _count(value) -> Optional[int]:
    """A whole number that is not negative, or None: anything else is not a reading."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _seconds(value) -> Optional[float]:
    """A number of seconds that is not negative, or None, so a line written by hand cannot pass for a reading."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value < 0:
        return None
    return value


def _is_at(value) -> bool:
    """Whether a line's `at` is a time: a number, and not a bool or NaN."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value


# ---------------------------------------------------------------- the kernel's files


def _cgroup_root() -> Path:
    """The root of the kernel's files, read at each call and not at import, so a test that points it elsewhere is obeyed."""
    return Path(os.environ.get("ORG_CGROUP_ROOT") or "/sys/fs/cgroup")


def _read(directory: Path, filename: str) -> Optional[str]:
    try:
        with open(directory / filename, "r", encoding="utf-8", errors="replace") as f:
            return f.read(65536)
    except (OSError, ValueError):
        return None


def _number(text: Optional[str]) -> Optional[int]:
    """The file's one number, or None when it holds anything else."""
    text = (text or "").strip()
    return int(text) if re.fullmatch(r"[0-9]{1,20}", text) else None


def _keyed(text: Optional[str], key: str) -> Optional[int]:
    """The number on the line `key N` of a file such as memory.events or cpu.stat, or None."""
    for row in (text or "").splitlines():
        words = row.split()
        if len(words) == 2 and words[0] == key:
            return _number(words[1])
    return None


KERNEL_FILES = (("memory.current", "memory_bytes"), ("memory.peak", "memory_peak_bytes"), ("memory.max", "memory_limit_bytes"),
                ("pids.current", "processes"), ("memory.events", "oom_kill"), ("cpu.stat", "cpu_s"))


def _kernel(directory: Path) -> dict:
    """What the kernel's files say, each file on its own; `unread` names those that said nothing usable."""
    texts = {name: _read(directory, name) for name, _ in KERNEL_FILES}
    usec = _keyed(texts["cpu.stat"], "usage_usec")
    found = {
        "memory_bytes": _number(texts["memory.current"]),
        "memory_peak_bytes": _number(texts["memory.peak"]),
        "memory_limit_bytes": _number(texts["memory.max"]),
        "processes": _number(texts["pids.current"]),
        "oom_kill": _keyed(texts["memory.events"], "oom_kill"),
        "cpu_s": None if usec is None else round(usec / 1e6, 3),
    }
    # The word `max` is the kernel saying there is no limit, which is a reading and not a failure.
    no_limit = (texts["memory.max"] or "").strip() == "max"
    found["unread"] = [name for name, key in KERNEL_FILES
                       if found[key] is None and not (name == "memory.max" and no_limit)]
    return found


# ---------------------------------------------------------------- one measurement


def _sentence(parts: List[str]) -> Optional[str]:
    if not parts:
        return None
    text = "; ".join(parts)
    return text[0].upper() + text[1:] + "."


def _blank(rec: dict) -> dict:
    """A line in which nothing was read yet: every value null until it is."""
    alive = rec.get("alive") if isinstance(rec, dict) else None
    return dict({key: None for key in FIELDS}, at=round(now(), 3), running=None,
                runner_alive=alive if isinstance(alive, bool) else None, note=None)


def _measure(ledger: Ledger, rec: dict) -> dict:
    """One line, read from Incus's description of the container and from the kernel's files for it."""
    line = _blank(rec)
    notes: List[str] = []
    try:
        machine = rec["machine"]
        cfg = ledger.config()
        # Through the backend, so the name has passed `_own` before it is given to `incus`.
        name = backends.get(cfg, machine["kind"]).transport(machine, cfg).name
    except Exception as e:  # a BackendError above all, but a record of any shape gets a line, not an exception
        line["note"] = _sentence(["the container was not asked about: %s" % (str(e)[:200] or type(e).__name__)])
        return line
    try:
        entry = Incus.info(name, CALL_TIMEOUT_S)
    except Exception as e:  # TransportError for a failure or a timeout; anything else costs the line no more
        line["note"] = _sentence(["Incus did not answer: %s" % (str(e)[:200] or type(e).__name__)])
        return line
    if not isinstance(entry, dict):
        line["running"] = False  # Incus answered, and it has no such container to run
        line["note"] = _sentence(["Incus does not know the container %s" % name])
        return line

    status = entry.get("status")
    running = status == "Running" if isinstance(status, str) else None
    line["running"] = running
    state = entry.get("state") if isinstance(entry.get("state"), dict) else {}
    memory = state.get("memory") if isinstance(state.get("memory"), dict) else {}
    cpu = state.get("cpu") if isinstance(state.get("cpu"), dict) else {}
    disk = state.get("disk") if isinstance(state.get("disk"), dict) else {}
    root = disk.get("root") if isinstance(disk.get("root"), dict) else {}
    config = entry.get("expanded_config") if isinstance(entry.get("expanded_config"), dict) else {}

    # A stopped container's disk is still on the pool, so it is taken whatever the status.
    line["disk_bytes"] = _count(root.get("usage"))

    kernel: dict = {}
    project = entry.get("project")
    if running is False:
        notes.append("the container is not running")
    if project in (None, "", "default"):
        directory = _cgroup_root() / ("lxc.payload.%s" % name)
    elif isinstance(project, str) and PROJECT_RE.fullmatch(project):
        directory = _cgroup_root() / ("lxc.payload.%s_%s" % (project, name))
    else:
        directory = None
        notes.append("Incus named the project %r, which is not a plain name, so no file of the kernel's was read"
                     % (str(project)[:60],))
    if directory is not None:
        if directory.is_dir():
            kernel = _kernel(directory)
            if kernel["unread"]:
                notes.append("the kernel's %s could not be read" % ", ".join(kernel["unread"]))
        elif running is not False:
            # A stopped container has no directory, which the note above already says.
            notes.append("the kernel's directory for it, %s, is missing" % directory.name)

    line["memory_bytes"] = kernel.get("memory_bytes")
    line["memory_peak_bytes"] = kernel.get("memory_peak_bytes")
    line["cpu_s"] = kernel.get("cpu_s")
    line["processes"] = kernel.get("processes")
    line["oom_kill"] = kernel.get("oom_kill")  # nothing else says it, so there is no fallback
    line["memory_limit_bytes"] = kernel.get("memory_limit_bytes")
    if line["memory_limit_bytes"] is None:
        mib = size_mib(config.get("limits.memory"))
        line["memory_limit_bytes"] = mib * 1024 * 1024 if mib is not None else None
    if running:
        # Incus's state says something only of a container that runs: a stopped one's zeros are not readings.
        if line["memory_bytes"] is None:
            line["memory_bytes"] = _count(memory.get("usage"))
        if line["memory_peak_bytes"] is None:
            peak = _count(memory.get("usage_peak"))
            line["memory_peak_bytes"] = peak if peak else None  # Incus 6.0 leaves it 0: not read, not a peak of zero
        if line["cpu_s"] is None:
            usage = _count(cpu.get("usage"))
            line["cpu_s"] = None if usage is None else round(usage / 1e9, 3)
        if line["processes"] is None:
            line["processes"] = _count(state.get("processes"))
        started = state.get("started_at")
        if isinstance(started, str) and started and not started.startswith("0001-"):
            line["started_at"] = started[:64]
    line["note"] = _sentence(notes)
    return line


# ---------------------------------------------------------------- the file and its summary


def load(ledger: Ledger, run_id: str) -> List[dict]:
    """A run's measurements, oldest first. Lines that are not JSON objects are passed over."""
    try:
        with open(path(ledger, run_id), "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except (OSError, ValueError):
        return []
    lines = []
    for row in text.splitlines():
        try:
            item = json.loads(row)
        except ValueError:
            continue
        if isinstance(item, dict):
            lines.append(item)
    return lines


def summarize(lines: List[dict]) -> Optional[dict]:
    """The summary of measurements, as the module's docstring describes it; None when there are none."""
    lines = [line for line in lines or [] if isinstance(line, dict)]
    if not lines:
        return None
    counters = ("cpu_s", "oom_kill")
    # For each counter: the sum of its last values in the lives before this one, and its last value in this life.
    before = {key: None for key in counters}
    current = {key: None for key in counters}
    started = None
    restarts = 0
    peak = limit = disk = None
    ats = []
    for line in lines:
        values = {"cpu_s": _seconds(line.get("cpu_s")), "oom_kill": _count(line.get("oom_kill"))}
        start = line.get("started_at") if isinstance(line.get("started_at"), str) and line.get("started_at") else None
        restarted = any(values[key] is not None and current[key] is not None and values[key] < current[key] for key in counters)
        restarted = restarted or (start is not None and started is not None and start != started)
        if restarted:
            restarts += 1
            # What the life before counted happened all the same: it is kept, and the new life counts from its own zero.
            for key in counters:
                if current[key] is not None:
                    before[key] = (before[key] or 0) + current[key]
                current[key] = None
        for key in counters:
            if values[key] is not None:
                current[key] = values[key]
        if start is not None:
            started = start
        p = _count(line.get("memory_peak_bytes"))
        if p is not None and (peak is None or p > peak):
            peak = p
        if _count(line.get("memory_limit_bytes")) is not None:
            limit = line["memory_limit_bytes"]
        if _count(line.get("disk_bytes")) is not None:
            disk = line["disk_bytes"]
        if _is_at(line.get("at")):
            ats.append(line["at"])

    def total(key):
        if before[key] is None and current[key] is None:
            return None  # never read, which is not zero
        value = (before[key] or 0) + (current[key] or 0)
        return round(value, 3) if key == "cpu_s" else value

    last = lines[-1]
    return {
        "samples": len(lines),
        "first_at": ats[0] if ats else None,
        "last_at": ats[-1] if ats else None,
        "memory_peak_bytes": peak,
        "memory_limit_bytes": limit,
        "cpu_s": total("cpu_s"),
        "oom_kill": total("oom_kill"),
        "restarts": restarts,
        "disk_bytes": disk,
        "running": last.get("running") if isinstance(last.get("running"), bool) else None,
        "note": last.get("note") if isinstance(last.get("note"), str) else None,
    }


def _append(target: Path, line: dict) -> None:
    """Append the line in one write. A line before it that was cut short is ended first, so this one stays whole."""
    data = (json.dumps(line, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    with open(target, "a+b") as f:
        f.seek(0, os.SEEK_END)
        if f.tell() > 0:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                data = b"\n" + data
        f.write(data)


def _measured(rec) -> bool:
    """Whether the record names a machine of a kind that is measured."""
    machine = rec.get("machine") if isinstance(rec, dict) else None
    return isinstance(machine, dict) and machine.get("kind") in MEASURED_KINDS


def take(ledger: Ledger, run_id: str, rec: dict, last: bool = False) -> Optional[dict]:
    """Take one measurement of the run's container, now, whatever the time since the one before.

    The caller holds the run's lock (`Ledger.run_lock`) and saves `rec` afterwards. This appends
    the line to machine.jsonl, sets `rec["telemetry"]` from the whole file and raises
    `rec["oom_kill"]` to the summary's when that is larger, and returns the line. It makes one
    `incus` call, with CALL_TIMEOUT_S. A machine of a kind that is not measured, or a record with
    no machine, gets no line and None is returned. It never raises: what could not be read is
    null in the line and said in its `note`.
    """
    try:
        if not _measured(rec):
            return None
        try:
            line = _measure(ledger, rec)
        except Exception as e:  # a mistake here must cost a reading, not the pump
            line = _blank(rec)
            line["note"] = _sentence(["the measurement failed: %s: %s" % (type(e).__name__, str(e)[:200])])
        if last:
            line["last"] = True
        try:
            _append(path(ledger, run_id), line)
        except Exception:
            return None  # a line that is not in the file is not a measurement
        summary = summarize(load(ledger, run_id))
        rec["telemetry"] = summary
        kills = summary.get("oom_kill") if summary else None
        held = rec.get("oom_kill")
        if _count(kills) is not None and (_count(held) is None or kills > held):
            rec["oom_kill"] = kills  # never lowered: a restarted container counts from zero, and the kills still happened
        return line
    except Exception:
        return None


# ---------------------------------------------------------------- every run that is due


def _to_measure(rec) -> bool:
    """A run sync would ask, holding a machine of a kind that is measured."""
    return (isinstance(rec, dict) and rec.get("state") not in NO_MACHINE_STATES and not rec.get("final")
            and not rec.get("machine_removed_at") and _measured(rec))


TAIL_BYTES = 16384  # far more than one line, so the last line is found without reading a long file whole


def _last_at(ledger: Ledger, run_id: str) -> Optional[float]:
    """The time of the last measurement in the file, read from its end."""
    try:
        with open(path(ledger, run_id), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            rows = f.read().decode("utf-8", "replace").splitlines()
    except (OSError, ValueError):
        return None
    if size > TAIL_BYTES:
        rows = rows[1:]  # the first may be cut in two
    for row in reversed(rows):
        try:
            item = json.loads(row)
        except ValueError:
            continue
        if isinstance(item, dict) and _is_at(item.get("at")):
            return item["at"]
    if size > TAIL_BYTES:
        for item in reversed(load(ledger, run_id)):  # an end of garbage: the whole file then
            if _is_at(item.get("at")):
                return item["at"]
    return None


def _due(ledger: Ledger, run_id: str) -> bool:
    """Decided from the file and not the record, whose last write may have been lost to another writer."""
    at = _last_at(ledger, run_id)
    if at is None:
        return True
    age = now() - at
    return age >= INTERVAL_S or age < -INTERVAL_S  # a line far in the future is a clock that was set back


def _measure_one(ledger: Ledger, run_id: str, log) -> bool:
    try:
        with ledger.run_lock(run_id) as held:
            if not held:
                return False  # being synced or torn down: the next pump measures it
            rec = ledger.run(run_id)
            # Asked again under the lock: another process may have measured it, finished it or removed its machine.
            if not _to_measure(rec) or not _due(ledger, run_id):
                return False
            if take(ledger, run_id, rec) is None:
                return False
            ledger.save_run(run_id, rec)
            return True
    except Exception as e:  # a ledger that cannot be written costs this run's measurement, nothing more
        log("measuring %s: %s: %s" % (run_id, type(e).__name__, e))
        return False


def measure_due(ledger: Ledger, log=lambda message: None) -> List[str]:
    """Measure every run that holds an Incus container, is not final, and was last measured a minute ago or more.

    The same runs sync asks: not queued, provisioning or dispatch_failed, not final, machine not
    removed. Each under its run's lock, taken without waiting (a run being synced or torn down is
    left for the next pump), with the record read again under it and saved after. Runs that are
    due are measured side by side, so the caller waits for one call's timeout and not for one per
    run. Returns the runs it measured. It never raises.
    """
    try:
        due = []
        for run_id in ledger.run_ids():
            try:
                if _to_measure(ledger.run(run_id)) and _due(ledger, run_id):
                    due.append(run_id)
            except Exception as e:  # one odd run must not keep the others from being measured
                log("measuring %s: %s: %s" % (run_id, type(e).__name__, e))
        if not due:
            return []
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(due))) as pool:
            done = list(pool.map(lambda r: _measure_one(ledger, r, log), due))
        return [run_id for run_id, ok in zip(due, done) if ok]
    except Exception as e:
        try:
            log("measure_due: %s: %s" % (type(e).__name__, e))
        except Exception:
            pass
        return []


