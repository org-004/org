"""Dispatch: turn an approved spec into an engineer running on its own machine.

Steps, all initiated from the manager's side:

  1. pick the machine (the spec header, else config): a fresh Incus container, an ssh host, or
     whatever another backend provides (org/backends/)
  2. place it: where the backend has a pool, a run needs a node with a free slot, and waits in
     the queue until one frees
  3. provision: the backend creates the machine, if machines of its kind are created
  4. copy the logins in over stdin: the Claude token, and for a machine that starts blank the
     GitHub token and git identity
  5. build the bundle (this package, the approved spec, job.json with caps, budget, prices and how
     past estimates turned out), unpack it, and start the runner detached, in tmux when present

The run record is written before any of that, so a dispatch that waits or fails is visible.
"""
from __future__ import annotations

import base64
import fcntl
import io
import json
import os
import subprocess
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import backends, budget, graph, secrets, telemetry
from .backends import BackendError, for_machine
from .ledger import Ledger
from .runner import TERMINAL_STATES
from .transport import Transport, TransportError
from .util import now, read_json, read_text, write_json

# Resolved, so that a process started from one release keeps bundling that release after the
# `current` link has moved to another.
PACKAGE = Path(__file__).resolve().parent
REMOTE_ROOT = "$HOME/org-runs"


class DispatchError(Exception):
    pass


def remote_dir(run_id: str) -> str:
    return "%s/%s" % (REMOTE_ROOT, run_id)


def workspace_dir(run_id: str) -> str:
    return "$HOME/work/%s" % run_id


def choose_machine(cfg: dict, header: dict, override: Optional[str], run_id: str) -> dict:
    """Resolve what a spec asks for ("incus", "ssh:<name>", "docker:<node>", ...) into a machine record."""
    want = (override or header.get("machine") or (cfg.get("machines") or {}).get("default") or "incus").strip()
    try:
        return backends.resolve(cfg, want).choose(cfg, want, run_id)
    except BackendError as e:
        raise DispatchError(str(e))


# ---------------------------------------------------------------- the pool


def node_load(ledger: Ledger) -> Dict[Tuple[str, str], int]:
    """Engineers currently holding a slot, by (machine kind, node), counted from the ledger."""
    load: Dict[Tuple[str, str], int] = {}
    for run_id in ledger.run_ids():
        rec = ledger.run(run_id)
        machine = rec.get("machine") or {}
        if not machine.get("node"):
            continue
        if rec.get("final") or rec.get("machine_removed_at") or rec.get("state") in ("queued", "dispatch_failed"):
            continue
        if rec.get("state") in TERMINAL_STATES:
            continue
        key = (machine.get("kind"), machine["node"])
        load[key] = load.get(key, 0) + 1
    return load


def place(ledger: Ledger, machine: dict) -> Optional[str]:
    """The least loaded node with a free slot that this run may use.

    "" when machines of this kind have no pool (nothing to place); None when every slot is taken.
    """
    cfg = ledger.config()
    kind = machine["kind"]
    nodes = backends.get(cfg, kind).nodes(cfg)
    wanted = machine.get("wanted_node")
    if not nodes or (wanted and wanted not in nodes):
        return ""
    load = node_load(ledger)
    free = [(load.get((kind, n), 0), n) for n in ([wanted] if wanted else sorted(nodes))
            if load.get((kind, n), 0) < int(nodes[n]["slots"])]
    return min(free)[1] if free else None


def pool(ledger: Ledger) -> List[dict]:
    """Every node that has slots, with what is using them, for status."""
    cfg, load, out = ledger.config(), node_load(ledger), []
    for kind, backend in sorted(backends.load(cfg).items()):
        for node, entry in sorted(backend.nodes(cfg).items()):
            out.append({"kind": kind, "node": node, "host": entry.get("host"), "slots": int(entry["slots"]),
                        "used": load.get((kind, node), 0)})
    return out


@contextmanager
def _placing(ledger: Ledger):
    """Placement is decided by one process at a time, or two could hand out the same slot."""
    ledger.runs.mkdir(parents=True, exist_ok=True)
    with open(ledger.runs / ".dispatch.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


# ---------------------------------------------------------------- what travels to the machine


def build_job(ledger: Ledger, meta: dict, run_id: str, machine: dict, facts: Optional[dict] = None) -> dict:
    cfg = ledger.config()
    header = meta.get("header") or {}
    eng = cfg.get("engineer") or {}
    rate = budget.usd_per_pct(ledger)
    return {
        "run_id": run_id,
        "spec_id": meta["id"],
        "spec_sha": meta["sha256"],
        "spec_version": meta.get("version", 1),
        "title": meta.get("title"),
        "project": header.get("project"),
        "repos": header.get("repos") or [],
        "delivery": header.get("delivery") or "merge",
        "model": header.get("model") or eng.get("model") or "fable",
        "effort": header.get("effort") or eng.get("effort") or "high",
        "caps": {k: header[k] for k in ("max_cost_usd", "max_weekly_pct", "max_hours") if header.get(k)},
        "acceptance_ids": meta.get("acceptance_ids") or [],
        "budget": {"usd_per_weekly_pct": rate["value"], "rate_source": rate["source"], "weekly": budget.weekly(ledger)},
        "pricing": cfg.get("pricing") or {},
        "history": budget.history(ledger),
        "engineer": {k: eng[k] for k in ("max_turn_retries", "silence_timeout_min", "report_reserve_usd", "retry_backoff_s") if k in eng},
        "claude_bin": machine.get("claude_bin") or "claude",
        "workspace": machine.get("workspace"),
        "machine": {"kind": machine["kind"], "name": machine["name"]},
        # What the machine has, as found just before the run: cores, and whether Docker answers.
        "facts": facts or {},
        "dispatched_at": now(),
    }


def build_bundle(ledger: Ledger, meta: dict, job: dict) -> bytes:
    """A tar.gz holding the runner's code and its inputs, laid out as the run directory."""
    buf = io.BytesIO()
    stamp = int(time.time())

    def add(tar, name: str, data: bytes, mode: int = 0o644):
        info = tarfile.TarInfo(name)
        info.size, info.mtime, info.mode = len(data), stamp, mode
        tar.addfile(info, io.BytesIO(data))

    spec_text = (ledger.spec_dir(meta["id"]) / "spec.md").read_bytes()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(PACKAGE.rglob("*")):
            rel = p.relative_to(PACKAGE)
            if not p.is_file() or "__pycache__" in rel.parts or rel.parts[0] == "web":
                continue
            if p.suffix in (".py", ".md"):
                add(tar, ".org/org/" + rel.as_posix(), p.read_bytes())
        add(tar, "spec.md", spec_text, 0o444)
        add(tar, ".org/spec.md", spec_text, 0o444)  # the runner restores spec.md from this copy
        add(tar, "meta.json", json.dumps(meta, indent=2, sort_keys=True).encode())
        add(tar, "job.json", json.dumps(job, indent=2, sort_keys=True).encode())
        brief = read_text(ledger.briefs / ((job.get("project") or "") + ".md")) if job.get("project") else None
        if brief:
            add(tar, "project.md", brief.encode())
        # For a run of a plan: what the specs it comes after really left (org/graph.py).
        for name, data in graph.bundle_files(ledger, job.get("run_id") or ""):
            add(tar, name, data)
    return buf.getvalue()


def start_script(run_id: str, detach: str = "auto") -> str:
    """Start the runner so that it outlives this connection. Prints how it was started."""
    plain = (
        'if command -v setsid >/dev/null 2>&1; then nohup setsid bash -c "$START" >>"$RUN/runner.out" 2>&1 </dev/null & '
        'else nohup bash -c "$START" >>"$RUN/runner.out" 2>&1 </dev/null & fi; echo nohup'
    )
    tmux = 'command -v tmux >/dev/null 2>&1 && tmux new-session -d -s "org-%s" "$START" 2>/dev/null' % run_id
    return "\n".join([
        'RUN="%s"' % remote_dir(run_id),
        'export PATH="$HOME/.local/bin:$PATH"',
        # $RUN expands here, on the machine; the single quotes then travel inside START.
        "START=\"cd '$RUN' && PYTHONPATH='$RUN/.org' exec python3 -m org engineer run '$RUN'\"",
        plain if detach == "nohup" else "if %s; then echo tmux; else %s; fi" % (tmux, plain),
    ]) + "\n"


def preflight(tr: Transport) -> dict:
    """What the machine has. A missing python3 or claude is reported before anything is started."""
    script = ('export PATH="$HOME/.local/bin:$PATH"; '
              'printf "python3=%s\\n" "$(command -v python3 || true)"; '
              'printf "claude=%s\\n" "$(command -v claude || true)"; '
              'printf "tmux=%s\\n" "$(command -v tmux || true)"; '
              'printf "git=%s\\n" "$(command -v git || true)"; '
              # Can an engineer build and run containers here? Only an answer from a daemon counts.
              # A machine started a second ago may still be starting one, so where a daemon is
              # set to start with the machine it is given a few seconds.
              'D=no; if command -v docker >/dev/null 2>&1; then for i in 1 2 3 4 5 6; do '
              'if docker info >/dev/null 2>&1; then D=yes; break; fi; '
              'systemctl is-enabled docker >/dev/null 2>&1 || break; sleep 1; done; fi; printf "docker=%s\\n" "$D"; '
              'printf "cores=%s\\n" "$(getconf _NPROCESSORS_ONLN 2>/dev/null || true)"; '
              'printf "host=%s\\n" "$(uname -n)"')
    out = tr.run(script, timeout=60).stdout.decode("utf-8", "replace")
    facts = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    missing = [k for k in ("python3", "claude", "git") if not facts.get(k)]
    if missing:
        raise DispatchError("%s is missing %s" % (tr.describe(), ", ".join(missing)))
    return facts


def _local(argv: List[str]) -> str:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        return p.stdout.strip() if p.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


# Where a token goes on an engineer's machine: memory when the machine lets us (/run is a tmpfs,
# and an engineer in a container is root there), else a directory only its user can read.
_PUT = ('umask 077; d="${ORG_SECRETS_DIR:-/run/org}"; if ! { mkdir -p "$d" 2>/dev/null && [ -w "$d" ]; }; then d="$HOME/.org-secrets"; mkdir -p "$d"; fi; '
        'chmod 700 "$d"; cat > "$d/%s"')


def push_logins(tr: Transport, machine: dict, blank: bool, log) -> None:
    """Give the machine what it needs to act as the person: the Claude token, and for a machine
    that starts blank the GitHub token and git identity. Everything goes in over stdin, and the
    tokens are the two this installation was handed (org/secrets.py), never a login found here."""
    if machine.get("push_token"):
        token = secrets.claude_token()
        if token:
            tr.run(_PUT % secrets.CLAUDE, input=(token + "\n").encode(), timeout=60)
            log("Claude token copied in")
        else:
            log("warning: this side has no Claude token; the engineer will use whatever login the machine has")
    if not blank:
        return  # an ssh host is set up by its owner
    github = secrets.github_token()
    if github:
        tr.run(_PUT % secrets.GITHUB, input=(github + "\n").encode(), timeout=60)
        # git asks gh, and gh reads GH_TOKEN, which the runner sets from the file above.
        tr.run("git config --global credential.https://github.com.helper '' && "
               "git config --global --add credential.https://github.com.helper '!gh auth git-credential'", timeout=30, check=False)
        log("GitHub token copied in")
    else:
        log("warning: this side has no GitHub token; the engineer can read public repositories and cannot push")
    name, email = _local(["git", "config", "--global", "user.name"]), _local(["git", "config", "--global", "user.email"])
    if name and email:
        tr.run('IFS= read -r n; IFS= read -r e; git config --global user.name "$n" && git config --global user.email "$e"',
               input=("%s\n%s\n" % (name, email)).encode(), timeout=30, check=False)
    else:
        log("warning: no git identity on this side (`git config --global user.name` and user.email); commits will fail until one is set")


# ---------------------------------------------------------------- dispatch, the queue


def dispatch(ledger: Ledger, spec_id: str, machine_override: Optional[str] = None, log=print,
             plan: Optional[dict] = None) -> Tuple[str, dict]:
    """Create a run of an approved spec and start it if a machine is free. Returns (run_id, record).

    `plan` is given only by org/graph.py, for a run it dispatches again: which run it replaces."""
    meta = ledger.spec_meta(spec_id)
    if not meta:
        raise DispatchError("%s is not approved" % spec_id)
    cfg = ledger.config()
    run_id = ledger.next_run_id(spec_id)
    machine = choose_machine(cfg, meta.get("header") or {}, machine_override, run_id)
    machine["claude_bin"] = cfg.get("claude_bin") or "claude"
    record = {
        "run_id": run_id,
        "spec_id": spec_id,
        "spec_sha": meta["sha256"],
        "spec_version": meta.get("version", 1),
        "title": meta.get("title"),
        "machine": machine,
        "remote_dir": remote_dir(run_id),
        "created_at": now(),
        "state": "queued",
        "events_offset": 0,
    }
    # Nothing for a spec that names no other: `after`, and what a run dispatched again replaces.
    record.update(graph.record_fields(ledger, meta, plan))
    ledger.run_dir(run_id).mkdir(parents=True, exist_ok=True)
    ledger.save_run(run_id, record)
    return run_id, start(ledger, run_id, log)


def start(ledger: Ledger, run_id: str, log=print) -> dict:
    """Start a queued run if its machine can take it now; otherwise leave it queued."""
    cfg = ledger.config()
    with _placing(ledger):
        record = ledger.run(run_id)
        if record.get("state") != "queued":
            return record
        # Read under the lock that `org drain` sets it under: once drain returns, nothing starts.
        held = draining(ledger)
        if held:
            record["queue_reason"] = "draining since %s%s" % (
                time.strftime("%a %H:%M", time.localtime(held.get("since") or now())), (": " + held["reason"]) if held.get("reason") else "")
            ledger.save_run(run_id, record)
            log("queued: %s" % record["queue_reason"])
            return record
        # A run of a plan waits for the specs it comes after, and a held one for the person.
        waits = graph.blocked(ledger, record)
        if waits:
            record["queue_reason"] = waits
            ledger.save_run(run_id, record)
            log("queued: %s" % waits)
            return record
        machine = record["machine"]
        try:
            backend = backends.get(cfg, machine["kind"])
        except BackendError as e:
            record.update(state="dispatch_failed", error=str(e))
            ledger.save_run(run_id, record)
            raise DispatchError("%s: %s" % (run_id, e))
        node = place(ledger, machine)
        if node is None:
            where = machine.get("wanted_node") or "any node"
            record["queue_reason"] = "no free slot on %s (%s)" % (
                where, ", ".join("%s %d/%d" % (p["node"], p["used"], p["slots"]) for p in pool(ledger) if p["kind"] == machine["kind"]))
            ledger.save_run(run_id, record)
            log("queued: %s" % record["queue_reason"])
            return record
        if node:
            backend.assign(cfg, machine, node)
        record.update(state="provisioning", queue_reason=None, provisioner=_me())
        ledger.save_run(run_id, record)  # the slot is taken from here on
    meta = ledger.spec_meta(record["spec_id"])
    made = False
    try:
        log("machine: %s %s%s" % (machine["kind"], machine["name"], (" on " + machine["node"]) if machine.get("node") else ""))
        tr = backend.provision(machine, run_id, cfg)
        made = True
        record["facts"] = preflight(tr)
        backend.vet(machine, record["facts"], cfg)
        # Only a container that passed: one that was refused is told nothing and gets no file.
        offered = backend.build_cache(tr, cfg, log)
        if offered:
            record["build_cache"] = offered
        push_logins(tr, machine, backend.blank, log)
        job = build_job(ledger, meta, run_id, machine, record["facts"])
        data = build_bundle(ledger, meta, job)
        # A shared host may already hold a run with this id, from another ledger or an earlier
        # life of this one. Its work is not ours to overwrite.
        taken = tr.run('test -e "%s/job.json" && echo taken || echo free' % record["remote_dir"], timeout=60).stdout.decode().strip()
        if taken == "taken":
            raise DispatchError("%s already has a run directory %s (from another ledger?). Remove it there, or dispatch elsewhere."
                                % (tr.describe(), record["remote_dir"]))
        tr.run('mkdir -p "%s" && base64 -d | tar xzf - -C "%s"' % (record["remote_dir"], record["remote_dir"]),
               input=base64.b64encode(data), timeout=120)
        log("bundle unpacked (%d kB)" % (len(data) // 1024))
        how = tr.run(start_script(run_id, (cfg.get("machines") or {}).get("detach", "auto")), timeout=60).stdout.decode().strip()
        record.update(state="dispatched", dispatched_at=now(), started_with=how.splitlines()[-1] if how else "?",
                      attach=tr.attach_hint(run_id) if "tmux" in how else "")
        ledger.save_run(run_id, record)
        log("runner started (%s)" % record["started_with"])
    except (TransportError, DispatchError, BackendError, OSError) as e:
        record.update(state="dispatch_failed", error=str(e))
        ledger.save_run(run_id, record)
        # A machine made for a run that never started holds nothing worth keeping, and one that
        # was refused must not be left running. Only one this attempt made: a machine that was
        # already there under this name is someone's work.
        if made and backend.blank:
            try:
                backend.remove(machine, cfg)
            except (TransportError, BackendError, OSError):
                pass
        raise DispatchError("%s: %s" % (run_id, e))
    return record


def _boot_id() -> str:
    return read_text("/proc/sys/kernel/random/boot_id", "").strip() if os.path.exists("/proc/sys/kernel/random/boot_id") else ""


def _me() -> dict:
    return {"pid": os.getpid(), "boot": _boot_id(), "at": now()}


def _gone(who: dict) -> bool:
    """Has the process that recorded itself as `who` ended? A restart of the machine ends them all."""
    if not who or not who.get("pid"):
        return True
    if (who.get("boot") or "") != _boot_id():
        return True
    try:
        os.kill(int(who["pid"]), 0)
    except ProcessLookupError:
        return True
    except (PermissionError, ValueError, OSError):
        return False
    return False


def reap(ledger: Ledger, log=lambda message: None) -> List[str]:
    """A run left in `provisioning` by a process that died would hold its slot for ever, and a
    drain would wait for it for ever. Record it as failed, and remove what was made of its machine."""
    reaped = []
    for run_id in ledger.run_ids():
        rec = ledger.run(run_id)
        if rec.get("state") != "provisioning" or not _gone(rec.get("provisioner") or {}):
            continue
        with _placing(ledger):
            rec = ledger.run(run_id)
            if rec.get("state") != "provisioning" or not _gone(rec.get("provisioner") or {}):
                continue
            rec.update(state="dispatch_failed", error="the process that was setting up this run's machine ended before it "
                                                      "finished; dispatch the spec again")
            ledger.save_run(run_id, rec)
        try:
            cfg = ledger.config()
            backends.get(cfg, rec["machine"]["kind"]).remove(rec["machine"], cfg)
        except (BackendError, TransportError, KeyError):
            pass
        log("%s: its dispatch was interrupted; recorded as failed" % run_id)
        reaped.append(run_id)
    return reaped


# ---------------------------------------------------------------- draining


def _drain_path(ledger: Ledger) -> Path:
    return ledger.root / "drain.json"


def draining(ledger: Ledger) -> Optional[dict]:
    """The drain in force, or None. While one is, approved work queues and nothing new starts."""
    data = read_json(_drain_path(ledger))
    return data if isinstance(data, dict) else None


def drain(ledger: Ledger, reason: str = "") -> dict:
    with _placing(ledger):
        held = draining(ledger) or {"since": now(), "reason": reason}
        write_json(_drain_path(ledger), held)
    return held


def undrain(ledger: Ledger) -> bool:
    with _placing(ledger):
        try:
            _drain_path(ledger).unlink()
        except FileNotFoundError:
            return False
    return True


def active(ledger: Ledger) -> List[str]:
    """Runs that hold a machine and have not finished: what a drain waits for."""
    out = []
    for run_id in ledger.run_ids():
        rec = ledger.run(run_id)
        if rec.get("final") or rec.get("machine_removed_at"):
            continue
        if rec.get("state") in ("queued", "dispatch_failed") or rec.get("state") in TERMINAL_STATES:
            continue
        out.append(run_id)
    return out


def pump(ledger: Ledger, log=lambda message: None) -> List[str]:
    """Start whatever is queued and can now be placed. Returns the runs it started.

    Called whenever something looks at the ledger (status, sync, the dashboard's loop), so a freed
    slot is taken up without a scheduler process of its own.
    """
    started = []
    reap(ledger, log)
    graph.tick(ledger, log)  # returns at once in a ledger that has no plan
    queued = [r for r in ledger.run_ids() if ledger.run(r).get("state") == "queued"]
    queued.sort(key=lambda r: ledger.run(r).get("created_at") or 0)
    for run_id in queued:
        try:
            if start(ledger, run_id, log).get("state") != "queued":
                started.append(run_id)
        except DispatchError as e:
            log("could not start %s: %s" % (run_id, e))
    # Last, and each guarded: neither what the containers measured nor the deleting of finished
    # runs' machines may keep a queued run from starting, or raise out of here.
    for step in (telemetry.measure_due, teardown):
        try:
            step(ledger, log)
        except Exception as e:  # whatever it is, the next pump tries again
            log("%s: %s: %s" % (step.__name__, type(e).__name__, e))
    return started


# ---------------------------------------------------------------- a finished run leaves no machine

# The two values of machines.after_finish. Written in single quotes on purpose: the check that
# no new place calls `incus delete` counts added lines here that carry that word in double quotes.
AFTER_FINISH_DELETE, AFTER_FINISH_KEEP = 'delete', 'keep'
REMOVE_RETRY_S = 60  # a deletion that failed is tried again no more often than this
TEARDOWN_WORKERS = 8  # how many finished runs' machines one pump measures and deletes side by side
STOPPED_WHILE_QUEUED = "stopped while queued"  # the error `stop` writes on a run that never had a machine


def after_finish(cfg: dict) -> str:
    """What becomes of a finished run's machine: AFTER_FINISH_DELETE when machines.after_finish
    is absent or says so, AFTER_FINISH_KEEP when it says keep or anything else. A value that is
    not understood must never delete; `after_finish_problem` is what `org doctor` says of it."""
    value = _after_finish_value(cfg)
    return AFTER_FINISH_DELETE if value is _ABSENT or (isinstance(value, str) and value == AFTER_FINISH_DELETE) else AFTER_FINISH_KEEP


def after_finish_problem(cfg: dict) -> Optional[str]:
    """Why machines.after_finish is not understood, as a sentence, or None when it is absent or one of the two values."""
    value = _after_finish_value(cfg)
    if value is _ABSENT or (isinstance(value, str) and value in (AFTER_FINISH_DELETE, AFTER_FINISH_KEEP)):
        return None
    return ("machines.after_finish is %s, which is neither %s nor %s, so it is treated as %s and finished runs' "
            "machines are kept" % (json.dumps(value), AFTER_FINISH_DELETE, AFTER_FINISH_KEEP, AFTER_FINISH_KEEP))


_ABSENT = object()  # machines.after_finish is not in the config at all, which is not the same as null


def _after_finish_value(cfg: dict):
    # A `machines` that is not a table says nothing about this, as if it were absent. A key that
    # is there with null is a value that is not understood, and so keeps.
    machines = (cfg or {}).get("machines") if isinstance(cfg, dict) else None
    return machines.get("after_finish", _ABSENT) if isinstance(machines, dict) else _ABSENT


def _deletes(backend: backends.Backend) -> bool:
    """Whether machines of this backend's kind are org's to delete. Backends whose machines org did
    not make (ssh, local) keep the base class's `remove`, which deletes nothing; asking that way
    costs no call to the machine on every pump."""
    return type(backend).remove is not backends.Backend.remove


def _teardown_due(rec: dict) -> bool:
    """Whether a run's record says its machine is to be deleted now: finished, fully copied, not
    already removed, and not refused within the last REMOVE_RETRY_S."""
    if not rec or not rec.get("final") or rec.get("state") not in TERMINAL_STATES or rec.get("machine_removed_at"):
        return False
    failed = rec.get("machine_remove_error")
    if isinstance(failed, dict):
        try:
            # A failure dated in the future (the clock was set back) does not hold the retry back.
            if 0 <= now() - float(failed.get("at") or 0) < REMOVE_RETRY_S:
                return False
        except (TypeError, ValueError):
            pass  # a time that cannot be read does not hold the deletion back for ever
    return True


def teardown(ledger: Ledger, log=lambda message: None) -> List[str]:
    """Delete the machine of every finished run, unless machines.after_finish keeps them. Returns the runs whose
    machine was deleted or found gone.

    A run is taken when its record is `final`, its state is terminal, `machine_removed_at` is not
    set, and no deletion of it failed within REMOVE_RETRY_S. For each, under the run's lock
    (`Ledger.run_lock`, not waited for) and with the record read again under it: the last
    measurement (`telemetry.take(..., last=True)`), the tail of the runner's log into the mirror,
    then `Backend.remove`, then the record: `machine_removed_at`, `machine_removed_by`
    ("teardown"; `org machine rm` writes "machine rm"), and `machine_remove_error` cleared.
    `remove` answering None (a machine org did not make) leaves the record as it was. A run with
    no machine on record is marked removed. A deletion that fails is written on the record as
    `machine_remove_error` = {"error": text, "at": when, "tries": how many} and the next run is
    taken. It never raises.
    """
    try:
        cfg = ledger.config()
        if after_finish(cfg) != AFTER_FINISH_DELETE:
            return []
        run_ids = ledger.run_ids()
    except Exception as e:  # a ledger that cannot be read now is read again by the next pump
        log("teardown: %s: %s" % (type(e).__name__, e))
        return []
    due = []
    for run_id in run_ids:
        try:
            if _teardown_due(ledger.run(run_id)):
                due.append(run_id)
        except Exception as e:
            log("%s: its record could not be read: %s: %s" % (run_id, type(e).__name__, e))

    def one(run_id: str) -> bool:
        try:
            return teardown_run(ledger, run_id, cfg, log)
        except Exception as e:  # one run's trouble must not keep the others' machines standing
            log("%s: its machine was not deleted: %s: %s" % (run_id, type(e).__name__, e))
            return False

    if not due:
        return []
    # Side by side: the first pump after an upgrade finds every finished run's machine at once,
    # and one after another a pump would wait for the sum of their measurements and deletions.
    with ThreadPoolExecutor(max_workers=min(TEARDOWN_WORKERS, len(due))) as pool:
        done = list(pool.map(one, due))
    return [run_id for run_id, ok in zip(due, done) if ok]


def teardown_run(ledger: Ledger, run_id: str, cfg: dict, log=lambda message: None) -> bool:
    """Delete one finished run's machine, as `teardown` describes. True when it was deleted or found gone.

    Everything is decided again under the run's lock, from the record read under it: between the
    look in `teardown` and here, `org resume` may have made the run not final, or another pump
    may have deleted the machine already.
    """
    with ledger.run_lock(run_id) as held:
        if not held:
            return False  # synced, measured, resumed or torn down elsewhere: the next pump takes it
        rec = ledger.run(run_id)
        if not _teardown_due(rec):
            return False
        machine = rec.get("machine")
        if not isinstance(machine, dict) or not machine.get("name"):
            # Nothing was ever made for it, so there is nothing to delete and nothing to warn of.
            _mark_removed(ledger, run_id, rec, "teardown")
            return True
        if rec.get("error") == STOPPED_WHILE_QUEUED and not rec.get("dispatched_at"):
            # It was stopped in the queue: its machine was chosen and never made. Nothing is
            # measured and no backend is asked, since a name that was never made may be another's.
            _mark_removed(ledger, run_id, rec, "teardown")
            return True
        try:
            backend = backends.get(cfg, machine.get("kind") or "")
        except Exception as e:  # a kind config no longer knows is a deletion that cannot be done
            _remove_failed(ledger, run_id, rec, e, log)
            return False
        if not _deletes(backend):
            return False  # a machine org did not make is not touched, and not marked
        before = json.dumps(rec, sort_keys=True, default=str)
        # The container's own counters die with it, so they are read first. A measurement that
        # cannot be taken says so in its line; it never holds the deletion back.
        try:
            telemetry.take(ledger, run_id, rec, last=True)
        except Exception as e:
            log("%s: its last measurement was not taken: %s: %s" % (run_id, type(e).__name__, e))
        _keep_runner_log(ledger, run_id, rec, cfg)
        try:
            said = backend.remove(machine, cfg)
        except Exception as e:  # TransportError, BackendError, OSError or anything else: recorded, not raised
            _remove_failed(ledger, run_id, rec, e, log)
            return False
        if said is None:
            if json.dumps(rec, sort_keys=True, default=str) != before:
                _save_machine(ledger, run_id, rec)
            return False
        _mark_removed(ledger, run_id, rec, "teardown")
        log("%s: %s" % (run_id, said))
        return True


RUNNER_LOG_TAIL = 5_000_000  # what is kept of a run's runner.log: its last 5 MB, cut as bytes and kept as text


def _keep_runner_log(ledger: Ledger, run_id: str, rec: dict, cfg: dict) -> None:
    """Copy the tail of the runner's log into the mirror before the machine goes. It comes from
    the engineer's machine, so it is written under one fixed name, as text, and bounded; anything
    that goes wrong here is passed over, since the deletion does not wait for it."""
    if not rec.get("remote_dir"):
        return
    try:
        p = for_machine(rec["machine"], cfg).run('tail -c %d "%s/runner.log"' % (RUNNER_LOG_TAIL, rec["remote_dir"]),
                                                 timeout=30, check=False)
        if p.returncode != 0 or not p.stdout:
            return
        text = p.stdout[-RUNNER_LOG_TAIL:].decode("utf-8", "replace")
        mirror = ledger.mirror_dir(run_id)
        mirror.mkdir(parents=True, exist_ok=True)
        (mirror / "runner.log").write_text(text, encoding="utf-8")
    except Exception:
        pass


# What the deleting of a machine writes on a run's record, and nothing else there is its to write.
_MACHINE_FIELDS = ("telemetry", "oom_kill", "machine_removed_at", "machine_removed_by", "machine_remove_error")


def _save_machine(ledger: Ledger, run_id: str, rec: dict) -> None:
    """Write what was learned about a run's machine onto the record as it is now, not as it was read.

    The record was read before a measurement, a copy of a log and a deletion, each a call that
    may take many seconds, and a final run's record is also written by the plan's tick, under
    its own lock (org/graph.py, `_save`). Saved as it was read, it would undo what a tick wrote
    in between: a review marked started would be started again. So the record is read again
    under the tick's lock and only the fields of `_MACHINE_FIELDS` are laid over it. When that
    lock cannot be had within a few seconds (a tick is setting up a machine), the same is done
    without it: the reading and the writing are then a moment apart instead of none.
    """
    with graph.writing(ledger):
        fresh = ledger.run(run_id) or dict(rec)
        for key in _MACHINE_FIELDS:
            if key in rec:
                fresh[key] = rec[key]
            else:
                fresh.pop(key, None)
        ledger.save_run(run_id, fresh)
    rec.clear()
    rec.update(fresh)


def _mark_removed(ledger: Ledger, run_id: str, rec: dict, by: str) -> None:
    rec.update(machine_removed_at=now(), machine_removed_by=by)
    rec.pop("machine_remove_error", None)
    _save_machine(ledger, run_id, rec)


def _remove_failed(ledger: Ledger, run_id: str, rec: dict, e: BaseException, log) -> None:
    """Write a deletion that failed on the record, where `org status` warns of it and from which
    `_teardown_due` knows when to try again."""
    previous = rec.get("machine_remove_error")
    try:
        tries = int((previous or {}).get("tries") or 0) if isinstance(previous, dict) else 0
    except (TypeError, ValueError):
        tries = 0
    rec["machine_remove_error"] = {"error": str(e)[:400], "at": now(), "tries": tries + 1}
    _save_machine(ledger, run_id, rec)
    log("%s: its machine could not be deleted (try %d): %s" % (run_id, tries + 1, str(e)[:400]))


# ---------------------------------------------------------------- acting on a run's machine


def machine_run(ledger: Ledger, run_id: str, script: str, timeout: float = 60, input: Optional[bytes] = None):
    """Run a script on a run's machine. The engineer's session is not involved."""
    rec = ledger.run(run_id)
    if rec.get("state") in ("queued", "provisioning", "dispatch_failed") or rec.get("machine_removed_at"):
        raise DispatchError("%s has no machine to reach (%s)" % (run_id, "removed" if rec.get("machine_removed_at") else rec.get("state")))
    return for_machine(rec["machine"], ledger.config()).run(script, timeout=timeout, input=input, check=False)


def stop(ledger: Ledger, run_id: str) -> str:
    """Ask the runner to stop. It ends the engineer's turn and records the run as stopped."""
    rec = ledger.run(run_id)
    if rec.get("machine_removed_at"):
        raise DispatchError("%s has no machine to reach (removed), so there is no runner to stop" % run_id)
    if rec.get("state") == "queued":
        # Under the placement lock, as every other writer of a queued run's record is (`start`,
        # and the holds of org/graph.py): written beside one of them, the run could come back queued.
        with _placing(ledger):
            rec = ledger.run(run_id)
            if rec.get("state") == "queued":
                rec.update(state="stopped", final=True, error=STOPPED_WHILE_QUEUED)
                ledger.save_run(run_id, rec)
                return "removed from the queue"
    tr = for_machine(rec["machine"], ledger.config())
    script = ('RUN="%s"; touch "$RUN/STOP"; '
              'PID=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get(\'pid\',\'\'))" "$RUN/status.json" 2>/dev/null); '
              '[ -n "$PID" ] && kill -TERM "$PID" 2>/dev/null; echo "signalled ${PID:-nothing}"' % rec["remote_dir"])
    return tr.run(script, timeout=60).stdout.decode().strip()


# Lays facts, read as JSON from stdin, over those in a run's job.json. Written whole or not at all.
_NEW_FACTS = ('python3 -c \'import json, os, sys\np = sys.argv[1]\njob = json.load(open(p))\n'
              'job["facts"] = dict(job.get("facts") or {}, **json.load(sys.stdin))\n'
              'open(p + ".new", "w").write(json.dumps(job, indent=2))\nos.replace(p + ".new", p)\' "%s/job.json"')


def resume(ledger: Ledger, run_id: str, log=lambda message: None, locked: bool = False) -> str:
    """Restart the runner for a run whose process died. It continues from its saved state.

    Under the run's lock for the whole of it: a pump that would delete the machine of a run that
    was final reads the record again under the same lock, and finds it made not final here.
    `locked` says the caller holds that lock already (the plan's tick does, org/graph.py): asking
    for it a second time from the same process would wait for itself and then refuse.
    """
    with (nullcontext(True) if locked else ledger.run_lock(run_id, wait=10)) as held:
        if not held:
            raise DispatchError("%s is being synced or its machine removed by another process; try again in a moment" % run_id)
        rec = ledger.run(run_id)
        if rec.get("machine_removed_at"):
            if rec.get("machine_removed_by") == "teardown":
                raise DispatchError("%s's container was deleted when the run ended, and a finished run cannot be continued" % run_id)
            raise DispatchError("%s's machine was deleted with `org machine rm`, so there is no runner to restart" % run_id)
        return _resume_locked(ledger, run_id, rec, log)


def _resume_locked(ledger: Ledger, run_id: str, rec: dict, log) -> str:
    machine, cfg = rec["machine"], ledger.config()
    backend = backends.get(cfg, machine["kind"])
    backend.before_resume(machine, cfg)
    tr = for_machine(machine, cfg)
    # What `before_resume` changed about the machine is told to the engineer as dispatch would
    # have told it: the runner that is about to start reads its facts from job.json.
    news = {k: v for k, v in backend.resume_facts(machine, cfg).items() if (rec.get("facts") or {}).get(k) != v}
    if news:
        rec["facts"] = dict(rec.get("facts") or {}, **news)
        tr.run(_NEW_FACTS % rec["remote_dir"], input=json.dumps(news).encode(), timeout=30)
    # The build cache may have another address by now, or be gone: ask again.
    offered = backend.build_cache(tr, cfg, log, again=True)
    if offered:
        rec["build_cache"] = offered
    # The tokens were put in the machine's memory, and a machine that has been stopped since
    # has none: without them the runner would start an engineer that cannot make a request.
    push_logins(tr, machine, backend.blank, log)
    tr.run('rm -f "%s/STOP"' % rec["remote_dir"], timeout=30)
    how = tr.run(start_script(run_id, (cfg.get("machines") or {}).get("detach", "auto")), timeout=60).stdout.decode().strip()
    rec.update(final=False, resumed_at=now())
    ledger.save_run(run_id, rec)
    return how


def remove_machine(ledger: Ledger, run_id: str) -> str:
    """Delete the machine a run used, where machines of its kind are org's to delete (`org machine rm`).

    Under the run's lock, as `teardown` is, so that the two never both write the record. The
    backend's error is raised to the caller, which says it to the person.
    """
    with ledger.run_lock(run_id, wait=10) as held:
        if not held:
            raise DispatchError("%s is being synced or its machine removed by another process; try again in a moment" % run_id)
        rec = ledger.run(run_id)
        machine, cfg = rec.get("machine") or {}, ledger.config()
        backend = backends.get(cfg, machine.get("kind") or "")
        if not rec.get("machine_removed_at") and _deletes(backend):
            # What the container measured dies with it: read it first, whatever comes of that.
            try:
                telemetry.take(ledger, run_id, rec, last=True)
            except Exception:
                pass
            # Saved now, so that what was measured is kept even when the deletion below fails.
            _save_machine(ledger, run_id, rec)
        said = backend.remove(machine, cfg)
        if said is None:
            return "nothing to delete: %s machines are not created by org" % machine.get("kind")
        if not rec.get("machine_removed_at"):
            # When and by what it was first deleted stays as it was written; asking again changes nothing.
            _mark_removed(ledger, run_id, rec, "machine rm")
        return said


def interrupted(ledger: Ledger) -> List[str]:
    """Runs whose machine is there and whose runner is not: what a restart of this machine leaves."""
    out = []
    for run_id in active(ledger):
        rec = ledger.run(run_id)
        if rec.get("state") == "provisioning":
            continue
        if rec.get("reachable") is False or rec.get("alive") is False:
            out.append(run_id)
    return out
