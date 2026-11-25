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

from pathlib import Path
from typing import List, Optional

from .ledger import Ledger

MEASURED_KINDS = ("incus",)  # Docker, ssh and local machines are not measured
INTERVAL_S = 60  # at most one measurement a minute for each run
CALL_TIMEOUT_S = 20  # the one `incus` call a measurement makes


def path(ledger: Ledger, run_id: str) -> Path:
    """Where a run's measurements are kept: beside run.json, not in the mirror."""
    return ledger.run_dir(run_id) / "machine.jsonl"


def load(ledger: Ledger, run_id: str) -> List[dict]:
    """A run's measurements, oldest first. Lines that are not JSON objects are passed over."""
    raise NotImplementedError


def summarize(lines: List[dict]) -> Optional[dict]:
    """The summary of measurements, as the module's docstring describes it; None when there are none."""
    raise NotImplementedError


def take(ledger: Ledger, run_id: str, rec: dict, last: bool = False) -> Optional[dict]:
    """Take one measurement of the run's container, now, whatever the time since the one before.

    The caller holds the run's lock (`Ledger.run_lock`) and saves `rec` afterwards. This appends
    the line to machine.jsonl, sets `rec["telemetry"]` from the whole file and raises
    `rec["oom_kill"]` to the summary's when that is larger, and returns the line. It makes one
    `incus` call, with CALL_TIMEOUT_S. A machine of a kind that is not measured, or a record with
    no machine, gets no line and None is returned. It never raises: what could not be read is
    null in the line and said in its `note`.
    """
    return None


def measure_due(ledger: Ledger, log=lambda message: None) -> List[str]:
    """Measure every run that holds an Incus container, is not final, and was last measured a minute ago or more.

    The same runs sync asks: not queued, provisioning or dispatch_failed, not final, machine not
    removed. Each under its run's lock, taken without waiting (a run being synced or torn down is
    left for the next pump), with the record read again under it and saved after. Runs that are
    due are measured side by side, so the caller waits for one call's timeout and not for one per
    run. Returns the runs it measured. It never raises.
    """
    return []
