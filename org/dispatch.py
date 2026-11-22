"""Dispatch: turn an approved spec into an engineer running on its own machine.

Steps, all initiated from the manager's side:

  1. pick the machine (the spec header, else config): a container from the Docker pool, a fresh
     OrbStack clone, an SSH host, or local
  2. place it: a Docker run needs a node with a free slot, and waits in the queue until one frees
  3. provision: `docker run`, or `orb clone` and `orb start` as `box new` does
  4. copy the logins in over stdin: the Claude token, and for a blank container the GitHub token
     and git identity
  5. build the bundle (this package, the approved spec, job.json with caps, budget, prices and how
     past estimates turned out), unpack it, and start the runner detached, in tmux when present

The run record is written before any of that, so a dispatch that waits or fails is visible.
"""
from __future__ import annotations

import base64
import fcntl
import io
import json
import re
import subprocess
import tarfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import budget
from .ledger import Ledger
from .runner import TERMINAL_STATES
from .transport import Docker, Local, Orb, Ssh, Transport, TransportError, for_machine
from .util import now, read_text

PACKAGE = Path(__file__).parent
REMOTE_ROOT = "$HOME/org-runs"


class DispatchError(Exception):
    pass


def remote_dir(run_id: str) -> str:
    return "%s/%s" % (REMOTE_ROOT, run_id)


def workspace_dir(run_id: str) -> str:
    return "$HOME/work/%s" % run_id


def _machine_name(prefix: str, run_id: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", (prefix + run_id).lower())[:60].strip("-")


def docker_nodes(cfg: dict) -> Dict[str, dict]:
    nodes = ((cfg.get("machines") or {}).get("docker") or {}).get("nodes") or {}
    return nodes or {"local": {"host": None, "slots": 4}}


def choose_machine(cfg: dict, header: dict, override: Optional[str], run_id: str) -> dict:
    """Resolve "docker[:<node>]" | "orb" | "ssh:<name>" | "local" into a machine record."""
    machines = cfg.get("machines") or {}
    want = (override or header.get("machine") or machines.get("default") or "orb").strip()
    if want == "docker" or want.startswith("docker:"):
        d = machines.get("docker") or {}
        node = want[7:] or None if ":" in want else None
        if node and node not in docker_nodes(cfg):
            raise DispatchError("docker node %r is not defined in config.json under machines.docker.nodes" % node)
        return {"kind": "docker", "name": _machine_name(d.get("prefix", "org-eng-"), run_id), "wanted_node": node, "node": None,
                "image": d.get("image", "org:latest"), "network": d.get("network", "org-engineers"),
                "push_token": d.get("push_token", True)}
    if want == "orb":
        orb = machines.get("orb") or {}
        return {"kind": "orb", "name": _machine_name(orb.get("prefix", "eng-"), run_id), "base": orb.get("base", "base"),
                "push_token": orb.get("push_token", True)}
    if want.startswith("ssh:"):
        name = want[4:]
        entry = (machines.get("ssh") or {}).get(name)
        if not entry or not entry.get("host"):
            raise DispatchError("ssh machine %r is not defined in config.json under machines.ssh" % name)
        return {"kind": "ssh", "name": name, "host": entry["host"], "options": entry.get("options") or [],
                "push_token": bool(entry.get("push_token", False))}
    if want == "local":
        local = machines.get("local") or {}
        if not local.get("enabled"):
            raise DispatchError("the local machine is disabled: an engineer runs with all permissions bypassed, "
                                "so it belongs in a container. Set machines.local.enabled to true in config.json to allow it.")
        return {"kind": "local", "name": "localhost", "home": local.get("home"), "push_token": False}
    raise DispatchError("unknown machine %r: use docker, docker:<node>, orb, ssh:<name> or local" % want)


# ---------------------------------------------------------------- the pool


def node_load(ledger: Ledger) -> Dict[str, int]:
    """Engineers currently holding a slot on each Docker node, counted from the ledger."""
    load: Dict[str, int] = {}
    for run_id in ledger.run_ids():
        rec = ledger.run(run_id)
        machine = rec.get("machine") or {}
        if machine.get("kind") != "docker" or not machine.get("node"):
            continue
        if rec.get("final") or rec.get("machine_removed_at") or rec.get("state") in ("queued", "dispatch_failed"):
            continue
        if rec.get("state") in TERMINAL_STATES:
            continue
        load[machine["node"]] = load.get(machine["node"], 0) + 1
    return load


def place(ledger: Ledger, machine: dict) -> Optional[str]:
    """The least loaded node with a free slot that this run may use, or None."""
    nodes, load = docker_nodes(ledger.config()), node_load(ledger)
    wanted = machine.get("wanted_node")
    free = [(load.get(n, 0), n) for n in ([wanted] if wanted else sorted(nodes))
            if n in nodes and load.get(n, 0) < int(nodes[n].get("slots", 1))]
    return min(free)[1] if free else None


def pool(ledger: Ledger) -> List[dict]:
    """Each node with its slots and what is using them, for status."""
    nodes, load = docker_nodes(ledger.config()), node_load(ledger)
    return [{"node": n, "host": nodes[n].get("host"), "slots": int(nodes[n].get("slots", 1)), "used": load.get(n, 0)}
            for n in sorted(nodes)]


@contextmanager
def _placing(ledger: Ledger):
    """Placement is decided by one process at a time, or two could hand out the same slot."""
    ledger.runs.mkdir(parents=True, exist_ok=True)
    with open(ledger.runs / ".dispatch.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


# ---------------------------------------------------------------- what travels to the machine


def build_job(ledger: Ledger, meta: dict, run_id: str, machine: dict) -> dict:
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


def provision(machine: dict, run_id: str = "") -> Transport:
    if machine["kind"] == "docker":
        return Docker.create(machine["name"], machine["image"], host=machine.get("host"), node=machine.get("node") or "local",
                             network=machine.get("network"), labels={"org.role": "engineer", "org.run": run_id},
                             limits=machine.get("limits"))
    if machine["kind"] == "orb":
        return Orb.create(machine.get("base", "base"), machine["name"])
    if machine["kind"] == "ssh":
        return Ssh(machine["name"], machine["host"], machine.get("options"))
    return Local(machine.get("home"))


def preflight(tr: Transport) -> dict:
    """What the machine has. A missing python3 or claude is reported before anything is started."""
    script = ('export PATH="$HOME/.local/bin:$PATH"; '
              'printf "python3=%s\\n" "$(command -v python3 || true)"; '
              'printf "claude=%s\\n" "$(command -v claude || true)"; '
              'printf "tmux=%s\\n" "$(command -v tmux || true)"; '
              'printf "git=%s\\n" "$(command -v git || true)"; '
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


def push_logins(tr: Transport, machine: dict, log) -> None:
    """Give the machine what it needs to act as the person: the Claude token, and for a blank
    container the GitHub token and git identity. Everything goes in over stdin."""
    token = Path.home() / ".claude" / "oauth-token"
    if machine.get("push_token"):
        if token.is_file() and token.stat().st_size:
            tr.run('umask 077; mkdir -p "$HOME/.claude"; cat > "$HOME/.claude/oauth-token"', input=token.read_bytes(), timeout=60)
            log("login token copied in")
        else:
            log("warning: no ~/.claude/oauth-token here; the engineer will use whatever login the machine has")
    if machine["kind"] != "docker":
        return  # an OrbStack clone inherits its template's GitHub login; an ssh host is set up by its owner
    gh = _local(["gh", "auth", "token"])
    if gh:
        tr.run("gh auth login --with-token >/dev/null 2>&1 && gh auth setup-git", input=(gh + "\n").encode(), timeout=60)
        log("GitHub login copied in")
    else:
        log("warning: no GitHub login on this side (`gh auth login`); the engineer cannot clone private repositories or push")
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
        machine = record["machine"]
        if machine["kind"] == "docker":
            node = place(ledger, machine)
            if node is None:
                where = machine.get("wanted_node") or "any node"
                record["queue_reason"] = "no free slot on %s (%s)" % (
                    where, ", ".join("%s %d/%d" % (p["node"], p["used"], p["slots"]) for p in pool(ledger)))
                ledger.save_run(run_id, record)
                log("queued: %s" % record["queue_reason"])
                return record
            entry = docker_nodes(cfg)[node]
            machine.update(node=node, host=entry.get("host"),
                           limits={k: entry[k] for k in ("cpus", "memory") if entry.get(k)})
        record.update(state="provisioning", queue_reason=None)
        ledger.save_run(run_id, record)  # the slot is taken from here on
    meta = ledger.spec_meta(record["spec_id"])
    try:
        log("machine: %s %s%s" % (machine["kind"], machine["name"], (" on " + machine["node"]) if machine.get("node") else ""))
        tr = provision(machine, run_id)
        record["facts"] = preflight(tr)
        push_logins(tr, machine, log)
        job = build_job(ledger, meta, run_id, machine)
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
    except (TransportError, DispatchError, OSError) as e:
        record.update(state="dispatch_failed", error=str(e))
        ledger.save_run(run_id, record)
        raise DispatchError("%s: %s" % (run_id, e))
    return record


def pump(ledger: Ledger, log=lambda message: None) -> List[str]:
    """Start whatever is queued and can now be placed. Returns the runs it started.

    Called whenever something looks at the ledger (status, sync, the dashboard's loop), so a freed
    slot is taken up without a scheduler process of its own.
    """
    started = []
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


def resume(ledger: Ledger, run_id: str) -> str:
    """Restart the runner for a run whose process died. It continues from its saved state."""
    rec = ledger.run(run_id)
    machine = rec["machine"]
    if machine.get("kind") == "docker" and Docker.state(machine["name"], machine.get("host")) == "exited":
        Docker._ok(machine.get("host"), ["start", machine["name"]], "docker start %s" % machine["name"])  # the host restarted
    tr = for_machine(machine, ledger.config())
    tr.run('rm -f "%s/STOP"' % rec["remote_dir"], timeout=30)
    how = tr.run(start_script(run_id, (ledger.config().get("machines") or {}).get("detach", "auto")), timeout=60).stdout.decode().strip()
    rec.update(final=False, resumed_at=now())
    ledger.save_run(run_id, rec)
    return how


def remove_machine(ledger: Ledger, run_id: str) -> str:
    """Delete the container or OrbStack machine a run used. Other machines are not org's to delete."""
    rec = ledger.run(run_id)
    machine = rec.get("machine") or {}
    if machine.get("kind") == "docker":
        Docker.delete(machine["name"], machine.get("host"))
    elif machine.get("kind") == "orb":
        Orb.delete(machine["name"])
    else:
        return "nothing to delete: %s machines are not created by org" % machine.get("kind")
    rec["machine_removed_at"] = now()
    ledger.save_run(run_id, rec)
    return "deleted %s" % machine["name"]
