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
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import backends, budget, secrets
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
        "delivery": header.get("delivery") or "pr",
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


def dispatch(ledger: Ledger, spec_id: str, machine_override: Optional[str] = None, log=print) -> Tuple[str, dict]:
    """Create a run of an approved spec and start it if a machine is free. Returns (run_id, record)."""
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
    queued = [r for r in ledger.run_ids() if ledger.run(r).get("state") == "queued"]
    queued.sort(key=lambda r: ledger.run(r).get("created_at") or 0)
    for run_id in queued:
        try:
            if start(ledger, run_id, log).get("state") != "queued":
                started.append(run_id)
        except DispatchError as e:
            log("could not start %s: %s" % (run_id, e))
    return started


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
    if rec.get("state") == "queued":
        rec.update(state="stopped", final=True, error="stopped while queued")
        ledger.save_run(run_id, rec)
        return "removed from the queue"
    tr = for_machine(rec["machine"], ledger.config())
    script = ('RUN="%s"; touch "$RUN/STOP"; '
              'PID=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get(\'pid\',\'\'))" "$RUN/status.json" 2>/dev/null); '
              '[ -n "$PID" ] && kill -TERM "$PID" 2>/dev/null; echo "signalled ${PID:-nothing}"' % rec["remote_dir"])
    return tr.run(script, timeout=60).stdout.decode().strip()


def resume(ledger: Ledger, run_id: str, log=lambda message: None) -> str:
    """Restart the runner for a run whose process died. It continues from its saved state."""
    rec = ledger.run(run_id)
    machine, cfg = rec["machine"], ledger.config()
    backend = backends.get(cfg, machine["kind"])
    backend.before_resume(machine, cfg)
    tr = for_machine(machine, cfg)
    # The tokens were put in the machine's memory, and a machine that has been stopped since
    # has none: without them the runner would start an engineer that cannot make a request.
    push_logins(tr, machine, backend.blank, log)
    tr.run('rm -f "%s/STOP"' % rec["remote_dir"], timeout=30)
    how = tr.run(start_script(run_id, (cfg.get("machines") or {}).get("detach", "auto")), timeout=60).stdout.decode().strip()
    rec.update(final=False, resumed_at=now())
    ledger.save_run(run_id, rec)
    return how


def remove_machine(ledger: Ledger, run_id: str) -> str:
    """Delete the machine a run used, where machines of its kind are org's to delete."""
    rec = ledger.run(run_id)
    machine, cfg = rec.get("machine") or {}, ledger.config()
    said = backends.get(cfg, machine.get("kind") or "").remove(machine, cfg)
    if said is None:
        return "nothing to delete: %s machines are not created by org" % machine.get("kind")
    rec["machine_removed_at"] = now()
    ledger.save_run(run_id, rec)
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
