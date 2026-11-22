"""Dispatch: turn an approved spec into an engineer running on its own machine.

Steps, all initiated from the manager's side:

  1. pick the machine (the spec header, else config): a fresh OrbStack clone, an SSH host, or local
  2. build the bundle: this package, the approved spec, job.json (caps, budget, price table, how
     past estimates turned out)
  3. provision: for OrbStack, `orb clone <base> <name>` and `orb start`, as `box new` does
  4. copy the Claude login token in over stdin, as `box <name>` does (per machine, opt-in for SSH)
  5. unpack the bundle and start the runner detached, in tmux when the machine has it

The run record is written before step 3, so a dispatch that fails halfway is visible in status.
"""
from __future__ import annotations

import base64
import io
import json
import re
import tarfile
import time
from pathlib import Path
from typing import Optional, Tuple

from . import budget, pricing
from .ledger import Ledger
from .transport import Local, Orb, Ssh, Transport, TransportError, for_machine
from .util import now, read_json, read_text

PACKAGE = Path(__file__).parent
REMOTE_ROOT = "$HOME/crew-runs"


class DispatchError(Exception):
    pass


def remote_dir(run_id: str) -> str:
    return "%s/%s" % (REMOTE_ROOT, run_id)


def choose_machine(cfg: dict, header: dict, override: Optional[str], run_id: str) -> dict:
    """Resolve "orb" | "ssh:<name>" | "local" into a machine record for run.json."""
    machines = cfg.get("machines") or {}
    want = (override or header.get("machine") or machines.get("default") or "orb").strip()
    if want == "orb":
        orb = machines.get("orb") or {}
        name = re.sub(r"[^a-z0-9-]", "-", (orb.get("prefix", "eng-") + run_id).lower())[:60].strip("-")
        return {"kind": "orb", "name": name, "base": orb.get("base", "base"), "push_token": orb.get("push_token", True)}
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
    raise DispatchError("unknown machine %r: use orb, ssh:<name> or local" % want)


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
                add(tar, ".crew/crew/" + rel.as_posix(), p.read_bytes())
        add(tar, "spec.md", spec_text, 0o444)
        add(tar, ".crew/spec.md", spec_text, 0o444)  # the runner restores spec.md from this copy
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
    tmux = 'command -v tmux >/dev/null 2>&1 && tmux new-session -d -s "crew-%s" "$START" 2>/dev/null' % run_id
    return "\n".join([
        'RUN="%s"' % remote_dir(run_id),
        'export PATH="$HOME/.local/bin:$PATH"',
        # $RUN expands here, on the machine; the single quotes then travel inside START.
        "START=\"cd '$RUN' && PYTHONPATH='$RUN/.crew' exec python3 -m crew engineer run '$RUN'\"",
        plain if detach == "nohup" else "if %s; then echo tmux; else %s; fi" % (tmux, plain),
    ]) + "\n"


def provision(machine: dict) -> Transport:
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


def dispatch(ledger: Ledger, spec_id: str, machine_override: Optional[str] = None, log=print) -> Tuple[str, dict]:
    """Start a new run of an approved spec. Returns (run_id, run record)."""
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
        "state": "provisioning",
        "events_offset": 0,
    }
    ledger.run_dir(run_id).mkdir(parents=True, exist_ok=True)
    ledger.save_run(run_id, record)
    try:
        log("machine: %s %s" % (machine["kind"], machine["name"]))
        tr = provision(machine)
        facts = preflight(tr)
        record["facts"] = facts
        token = Path.home() / ".claude" / "oauth-token"
        if machine.get("push_token"):
            if token.is_file() and token.stat().st_size:
                tr.run('umask 077; mkdir -p "$HOME/.claude"; cat > "$HOME/.claude/oauth-token"', input=token.read_bytes(), timeout=60)
                log("login token copied in")
            else:
                log("warning: no ~/.claude/oauth-token here; the engineer will use whatever login the machine has")
        job = build_job(ledger, meta, run_id, machine)
        data = build_bundle(ledger, meta, job)
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
    return run_id, record


def stop(ledger: Ledger, run_id: str) -> str:
    """Ask the runner to stop. It ends the engineer's turn and records the run as stopped."""
    rec = ledger.run(run_id)
    tr = for_machine(rec["machine"], ledger.config())
    script = ('RUN="%s"; touch "$RUN/STOP"; '
              'PID=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get(\'pid\',\'\'))" "$RUN/status.json" 2>/dev/null); '
              '[ -n "$PID" ] && kill -TERM "$PID" 2>/dev/null; echo "signalled ${PID:-nothing}"' % rec["remote_dir"])
    return tr.run(script, timeout=60).stdout.decode().strip()


def resume(ledger: Ledger, run_id: str) -> str:
    """Restart the runner for a run whose process died. It continues from its saved state."""
    rec = ledger.run(run_id)
    tr = for_machine(rec["machine"], ledger.config())
    tr.run('rm -f "%s/STOP"' % rec["remote_dir"], timeout=30)
    how = tr.run(start_script(run_id, (ledger.config().get("machines") or {}).get("detach", "auto")), timeout=60).stdout.decode().strip()
    rec.update(final=False, resumed_at=now())
    ledger.save_run(run_id, rec)
    return how


def remove_machine(ledger: Ledger, run_id: str) -> str:
    """Delete the OrbStack machine a run used. Other kinds of machine are not crew's to delete."""
    rec = ledger.run(run_id)
    machine = rec.get("machine") or {}
    if machine.get("kind") != "orb":
        return "nothing to delete: %s machines are not created by crew" % machine.get("kind")
    Orb.delete(machine["name"])
    rec["machine_removed_at"] = now()
    ledger.save_run(run_id, rec)
    return "deleted %s" % machine["name"]
