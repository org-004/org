"""The `org` command.

    org talk                       talk to the manager: refine a spec, or ask about runs
    org status [--watch]           what every run is doing, time, ETA, spend
    org dash                       the same as a local web page, with the manager chat beside it
    org ask "question"             one answer from the manager about the current state

    org spec new "<title>"         start a draft            org spec check <spec>
    org spec show <spec>           print a draft or spec    org spec ls
    org approve <spec> --sha <8+>  freeze the draft and dispatch an engineer
    org dispatch <spec>            run an approved spec again

    org plan|report|review <run>   the frozen plan, the report, plan against outcome
    org peek <run>                 what the engineer is doing right now
    org logs <run> [-f]            its activity feed
    org stop|resume <run>          stop the engineer, or restart a runner that died
    org ls|cat|diff <run> [path]   look at the files on a run's machine, read-only
    org exec <run> -- <command>    run a command on a run's machine
    org pool                       how many engineers may run at once, and what is queued
    org config get|set <key>       read or change config.json
    org machine rm <run>           delete the container or OrbStack machine a run used
    org login                      the logins engineers are given: Claude, GitHub, git identity
    org doctor                     check that this machine is ready to manage engineers

A <run> may be a run id, a unique prefix, or a spec reference (meaning that spec's latest run).
A <spec> may be its id, its number, or a unique prefix.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__
from .ledger import Ledger
from .util import clip, fmt_clock, fmt_dur, fmt_stamp, fmt_usd, now, read_json, read_text


def die(msg: str, code: int = 1):
    print("org: " + str(msg), file=sys.stderr)
    sys.exit(code)


def need_spec(ledger: Ledger, ref: str) -> str:
    try:
        return ledger.resolve_spec(ref)
    except LookupError as e:
        die(e)


def need_run(ledger: Ledger, ref: str) -> str:
    try:
        return ledger.resolve_run(ref)
    except LookupError as e:
        die(e)


# ---------------------------------------------------------------- engineer side


def cmd_engineer(args) -> int:
    from . import runner

    if args.action == "run":
        return runner.Runner(args.dir).run()
    if args.action == "snapshot":
        json.dump(runner.snapshot(args.dir, events_from=args.events_from), sys.stdout)
        sys.stdout.write("\n")
        return 0
    return 2


# ---------------------------------------------------------------- specs


def cmd_init(args) -> int:
    ledger = Ledger(args.home)
    fresh = ledger.init()
    print("%s ledger at %s" % ("created" if fresh else "found", ledger.root))
    print("config: %s" % ledger.config_path)
    return 0


def cmd_spec(args) -> int:
    from . import spec

    ledger = Ledger(args.home)
    ledger.init()
    if args.action == "new":
        spec_id = spec.new_draft(ledger, args.title, args.project or "")
        print(spec_id)
        print(ledger.draft_path(spec_id))
        return 0
    if args.action == "ls":
        for spec_id in ledger.spec_ids():
            meta = ledger.spec_meta(spec_id) or {}
            state = spec.state_of(ledger, spec_id)
            title = meta.get("title") or spec.parse(read_text(ledger.draft_path(spec_id)) or "")[0].get("title") or ""
            runs = ledger.runs_for(spec_id)
            print("%-34s %-9s %-3s %s" % (spec_id, state, ("v%d" % meta["version"]) if meta else "", clip(title, 60))
                  + (("   runs: " + ", ".join(runs)) if runs else ""))
        return 0
    spec_id = need_spec(ledger, args.spec)
    if args.action == "show":
        path = ledger.spec_dir(spec_id) / "spec.md" if args.approved else ledger.draft_path(spec_id)
        text = read_text(path)
        if text is None:
            die("%s does not exist" % path)
        sys.stdout.write(text)
        return 0
    if args.action == "check":
        text = read_text(ledger.draft_path(spec_id))
        if text is None:
            die("no draft for %s" % spec_id)
        errors, warnings = spec.check_draft(ledger, spec_id)
        for e in errors:
            print("error:   " + e)
        for w in warnings:
            print("warning: " + w)
        sha = spec.draft_sha(ledger, spec_id)
        print("%s  %s  sha256 %s" % (spec_id, "NOT READY" if errors else "ready to approve", sha[:12]))
        print(ledger.draft_path(spec_id))
        return 1 if errors else 0
    if args.action == "revise":
        approved = ledger.spec_dir(spec_id) / "spec.md"
        if not approved.exists():
            die("%s is not approved; edit its draft directly" % spec_id)
        print(ledger.draft_path(spec_id))
        print("Edit the draft; `org approve` will record it as the next version and start a new run.")
        return 0
    return 2


def cmd_approve(args) -> int:
    from . import dispatch, spec

    ledger = Ledger(args.home)
    spec_id = need_spec(ledger, args.spec)
    before = ledger.spec_meta(spec_id) or {}
    try:
        meta = spec.approve(ledger, spec_id, args.sha)
    except spec.ApprovalError as e:
        die(e)
    if before.get("sha256") == meta["sha256"] and ledger.runs_for(spec_id):
        # Approving the same text twice must not quietly start a second engineer on it.
        print("%s v%d is already approved with exactly this text; runs: %s" % (spec_id, meta["version"], ", ".join(ledger.runs_for(spec_id))))
        print("nothing dispatched; `org dispatch %s` starts another engineer on it" % spec_id)
        return 0
    print("approved %s v%d (sha256 %s)" % (spec_id, meta["version"], meta["sha256"][:12]))
    live = [r for r in ledger.runs_for(spec_id) if not ledger.run(r).get("final") and ledger.run(r).get("state") != "dispatch_failed"]
    if live:
        print("note: %s is still running against an earlier approval; `org stop %s` if it should not continue" % (", ".join(live), live[-1]))
    if args.no_dispatch:
        print("not dispatched; `org dispatch %s` starts an engineer" % spec_id)
        return 0
    return _dispatch(ledger, spec_id, args.machine)


def _dispatch(ledger: Ledger, spec_id: str, machine) -> int:
    from . import dispatch

    try:
        run_id, rec = dispatch.dispatch(ledger, spec_id, machine, log=lambda m: print("  " + m))
    except dispatch.DispatchError as e:
        die("dispatch failed: %s" % e)
    print("dispatched %s" % run_id)
    if rec.get("attach"):
        print("watch it:  %s" % rec["attach"])
    print("status:    org status")
    return 0


def cmd_dispatch(args) -> int:
    ledger = Ledger(args.home)
    return _dispatch(ledger, need_spec(ledger, args.spec), args.machine)


# ---------------------------------------------------------------- status


def cmd_status(args) -> int:
    from . import status

    ledger = Ledger(args.home)
    ledger.init()
    if args.json:
        json.dump(status.collect(ledger, do_sync=not args.no_sync), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    if not args.watch:
        print(status.render(status.collect(ledger, do_sync=not args.no_sync)))
        return 0
    try:
        while True:
            text = status.render(status.collect(ledger, do_sync=True))
            sys.stdout.write("\033[2J\033[H" + text + "\n\n" + time.strftime("%H:%M:%S") + "  (ctrl-c to leave)\n")
            sys.stdout.flush()
            time.sleep(max(2.0, args.interval))
    except KeyboardInterrupt:
        return 0


def cmd_sync(args) -> int:
    from . import sync

    ledger = Ledger(args.home)
    only = [need_run(ledger, args.run)] if args.run else None
    if only:
        rec = ledger.run(only[0])
        rec["final"] = False  # an explicit sync always asks the machine
        ledger.save_run(only[0], rec)
    for rec in sync.sync_all(ledger, only):
        ok = "ok" if rec.get("reachable") else ("unreachable: " + str(rec.get("last_error")))
        print("%-34s %-12s %s" % (rec.get("run_id"), rec.get("state"), ok))
    return 0


def _doc(args, name: str, missing: str) -> int:
    from . import sync

    ledger = Ledger(args.home)
    run_id = need_run(ledger, args.run)
    sync.sync_run(ledger, run_id)
    text = read_text(ledger.mirror_dir(run_id) / name)
    if text is None:
        die("%s: %s" % (run_id, missing))
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


def cmd_plan(args) -> int:
    return _doc(args, "plan.md" if args.current else "plan.frozen.md", "no plan yet (the engineer is still planning)")


def cmd_report(args) -> int:
    return _doc(args, "report.md", "no report yet (the run has not finished)")


def cmd_review(args) -> int:
    from . import status, sync

    ledger = Ledger(args.home)
    run_id = need_run(ledger, args.run)
    sync.sync_run(ledger, run_id)
    print(status.scorecard(ledger, run_id))
    return 0


def _feed(path, start: int = 0):
    from .stream import feed

    return feed(path, start)


def cmd_logs(args) -> int:
    from . import sync

    ledger = Ledger(args.home)
    run_id = need_run(ledger, args.run)
    path = ledger.mirror_dir(run_id) / "events.jsonl"
    sync.sync_run(ledger, run_id)
    offset, lines = 0, []
    for offset, line in _feed(path):
        lines.append(line)
    for line in lines[-args.lines:] if not args.all else lines:
        print(line)
    if not args.follow:
        return 0
    try:
        while True:
            time.sleep(4.0)
            rec = sync.sync_run(ledger, run_id)
            for offset, line in _feed(path, offset):
                print(line, flush=True)
            if rec.get("final"):
                return 0
    except KeyboardInterrupt:
        return 0


def cmd_peek(args) -> int:
    """A read-only look at one run, in words: for the human and for the manager chatbot."""
    from . import status, sync

    ledger = Ledger(args.home)
    run_id = need_run(ledger, args.run)
    rec = sync.sync_run(ledger, run_id)
    from . import budget

    v = status.run_view(ledger, run_id, budget.usd_per_pct(ledger)["value"])
    if args.json:
        json.dump(v, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    p = v["progress"]
    print("%s — %s" % (run_id, status.state_label(v)))
    print("spec:      %s v%s  %s" % (v["spec_id"], v["spec_version"], clip(v["title"] or "", 70)))
    print("machine:   %s %s%s" % (v["machine"]["kind"], v["machine"]["name"], ("   " + v["attach"]) if v.get("attach") else ""))
    print("elapsed:   %s   started %s" % (fmt_dur(v["elapsed_s"]), fmt_clock(v["started_at"])))
    est = v.get("estimate") or {}
    if est:
        print("plan:      %s and %s (p90 %s, %s)" % (fmt_dur((est.get("wall_minutes") or 0) * 60), fmt_usd(est.get("cost_usd")),
              fmt_dur((est.get("wall_minutes_p90") or 0) * 60) if est.get("wall_minutes_p90") else "—", fmt_usd(est.get("cost_usd_p90"))))
    if v["eta_at"]:
        print("forecast:  finishes about %s, total about %s (%s)" % (fmt_clock(v["eta_at"]), fmt_usd(v["forecast_cost_usd"]), v.get("forecast_basis")))
    spend = fmt_usd(v["cost_usd"])
    if v["weekly_pct"] is not None:
        spend += " (%.1f%% of the weekly limit)" % v["weekly_pct"]
    if v["cost_cap_usd"]:
        spend += ", cap %s" % fmt_usd(v["cost_cap_usd"])
    print("spend:     %s" % spend)
    if p.get("total"):
        print("milestones: %d of %d done" % (p.get("done", 0), p["total"]))
        for m in p.get("milestones") or []:
            mark = {"done": "✓", "partial": "◑", "blocked": "✗"}.get(m.get("outcome"), "▶" if m.get("started_at") else " ")
            took = fmt_dur(m["ended_at"] - m["started_at"]) if m.get("ended_at") else (fmt_dur(now() - m["started_at"]) + " so far" if m.get("started_at") else "")
            print("  %s %s %s  (est %s, %s)  %s" % (mark, m.get("id"), clip(m.get("title") or "", 50),
                  fmt_dur((m.get("est_minutes") or 0) * 60), fmt_usd(m.get("est_cost_usd")), took))
    if v["terminal"]:
        r = v.get("result") or {}
        print("result:    %s — %s" % (r.get("status"), r.get("msg")))
        for a in r.get("artifacts") or []:
            print("delivered: %s%s%s" % (a.get("repo") or "", ("  branch " + a["branch"]) if a.get("branch") else "",
                                         ("  " + a["pr_url"]) if a.get("pr_url") else ""))
    else:
        print("now:       %s" % (v.get("detail") or ""))
        if v.get("activity"):
            print("           %s  (%s ago)" % (v["activity"], fmt_dur(now() - v["activity_at"]) if v.get("activity_at") else "?"))
        for a in v.get("agents") or []:
            print("           subagent running: %s" % a)
    for w in v["warnings"]:
        print("warning:   %s" % w)
    if v["health"] != "ok":
        print("health:    %s%s" % (v["health"], (" — " + str(rec.get("last_error"))) if rec.get("last_error") else ""))
    if args.lines:
        print("\nrecent activity:")
        for _, line in list(_feed(ledger.mirror_dir(run_id) / "events.jsonl"))[-args.lines:]:
            print(line)
    return 0


def cmd_stop(args) -> int:
    from . import dispatch
    from .transport import TransportError

    ledger = Ledger(args.home)
    run_id = need_run(ledger, args.run)
    try:
        print(dispatch.stop(ledger, run_id))
    except TransportError as e:
        die(e)
    return 0


def cmd_resume(args) -> int:
    from . import dispatch
    from .transport import TransportError

    ledger = Ledger(args.home)
    run_id = need_run(ledger, args.run)
    try:
        print("restarted the runner for %s (%s)" % (run_id, dispatch.resume(ledger, run_id)))
    except TransportError as e:
        die(e)
    return 0


def cmd_machine(args) -> int:
    from . import dispatch, sync
    from .transport import TransportError

    ledger = Ledger(args.home)
    if args.action == "ls":
        for run_id in ledger.run_ids():
            rec = ledger.run(run_id)
            m = rec.get("machine") or {}
            print("%-34s %-6s %-30s %s" % (run_id, m.get("kind"), m.get("name"), "removed" if rec.get("machine_removed_at") else (rec.get("state") or "")))
        return 0
    run_id = need_run(ledger, args.run)
    rec = sync.sync_run(ledger, run_id)
    status = read_json(ledger.mirror_dir(run_id) / "status.json") or {}
    if not rec.get("final") and not args.force:
        die("%s is not finished and fully synced (state %s); `org stop` it first, or pass --force to delete the machine anyway"
            % (run_id, rec.get("state")))
    if status.get("warnings") and not args.force:
        die("the run left warnings (work that may exist only on the machine):\n  " + "\n  ".join(status["warnings"])
            + "\npass --force to delete the machine anyway")
    try:
        print(dispatch.remove_machine(ledger, run_id))
    except TransportError as e:
        die(e)
    return 0


def cmd_project(args) -> int:
    from .util import write_json

    ledger = Ledger(args.home)
    ledger.init()
    projects = ledger.projects()
    if args.action == "ls":
        for name, p in sorted(projects.items()):
            print("%-20s %s" % (name, ", ".join(p.get("repos") or [])))
        return 0
    if args.action == "add":
        entry = projects.setdefault(args.name, {"repos": []})
        for repo in args.repo or []:
            if repo not in entry["repos"]:
                entry["repos"].append(repo)
        if args.path:
            entry["path"] = os.path.abspath(os.path.expanduser(args.path))
        write_json(ledger.projects_path, projects)
        brief = ledger.briefs / (args.name + ".md")
        if not brief.exists():
            brief.parent.mkdir(parents=True, exist_ok=True)
            brief.write_text("# %s\n\nProject brief. The manager keeps this current: how to build and test, conventions,\n"
                             "hot paths, and what past runs learned. Engineers receive it with every spec.\n" % args.name)
        print("%s: %s" % (args.name, ", ".join(entry["repos"]) or "(no repos yet)"))
        print(brief)
        return 0
    return 2


def cmd_talk(args) -> int:
    from . import manager

    return manager.talk(Ledger(args.home), args.prompt, project=args.project, resume=args.resume, dry_run=args.dry_run)


def cmd_ask(args) -> int:
    from . import manager

    return manager.ask_cli(Ledger(args.home), " ".join(args.question))


DIFF_SCRIPT = r"""
cd "%s" 2>/dev/null || { echo "no workspace yet"; exit 0; }
found=0
for g in $(find . -maxdepth 4 -name .git -not -path '*/node_modules/*' 2>/dev/null | sort); do
  r=$(dirname "$g"); found=1
  echo "== $r  [$(git -C "$r" rev-parse --abbrev-ref HEAD 2>/dev/null)]  $(git -C "$r" remote get-url origin 2>/dev/null)"
  git -C "$r" status --short | head -40
  echo "-- last commits"; git -C "$r" log --oneline -8 2>/dev/null
  echo "-- not yet pushed: $(git -C "$r" log --branches --not --remotes --oneline 2>/dev/null | wc -l | tr -d ' ') commit(s)"
  echo "-- uncommitted changes"; git -C "$r" diff --stat | tail -15
  echo
done
[ "$found" = 1 ] || echo "no repositories in the workspace yet"
"""


def _on_machine(args, script: str, timeout: float = 60) -> int:
    from . import dispatch
    from .transport import TransportError

    ledger = Ledger(args.home)
    run_id = need_run(ledger, args.run)
    try:
        p = dispatch.machine_run(ledger, run_id, script, timeout=timeout)
    except (dispatch.DispatchError, TransportError) as e:
        die(e)
    sys.stdout.write(p.stdout.decode("utf-8", "replace"))
    sys.stderr.write(p.stderr.decode("utf-8", "replace"))
    return p.returncode


def cmd_fs(args) -> int:
    """Read-only looks at an engineer's machine. The commands are built here, not passed through."""
    import shlex

    from . import dispatch

    ledger = Ledger(args.home)
    workspace = dispatch.workspace_dir(need_run(ledger, args.run))
    if args.cmd == "diff":
        return _on_machine(args, DIFF_SCRIPT % workspace)
    enter = 'cd "%s" 2>/dev/null || cd "$HOME"; ' % workspace
    if args.cmd == "ls":
        return _on_machine(args, enter + "ls -la -- %s" % shlex.quote(args.path or "."))
    return _on_machine(args, enter + "head -c 400000 -- %s" % shlex.quote(args.path))


def cmd_exec(args) -> int:
    """Run anything on a run's machine, from its workspace. The engineer's session is untouched,
    but the command can change what the engineer is working on, so the manager chat must ask."""
    import shlex

    from . import dispatch

    ledger = Ledger(args.home)
    command = args.command[0] if len(args.command) == 1 else " ".join(shlex.quote(a) for a in args.command)
    enter = 'cd "%s" 2>/dev/null || cd "$HOME"; ' % dispatch.workspace_dir(need_run(ledger, args.run))
    return _on_machine(args, 'export PATH="$HOME/.local/bin:$PATH"; ' + enter + command, timeout=args.timeout)


def cmd_pool(args) -> int:
    from . import dispatch

    ledger = Ledger(args.home)
    nodes = dispatch.pool(ledger)
    for p in nodes:
        where = p["host"] or {"orb": "OrbStack machines on the host", "docker": "this Docker daemon"}.get(p["kind"], "")
        print("%-20s %d of %d slot(s) in use   %s" % (p["kind"] + ":" + p["node"], p["used"], p["slots"], where))
    if not nodes:
        print("no limit is set on how many engineers run at once (machines.orb.slots in config.json sets one)")
    queued = [r for r in ledger.run_ids() if ledger.run(r).get("state") == "queued"]
    for run_id in queued:
        print("queued: %s  (%s)" % (run_id, ledger.run(run_id).get("queue_reason") or "waiting"))
    return 0


def cmd_node(args) -> int:
    """Docker backend: which Docker hosts engineers may run on, and how many at once on each."""
    import subprocess
    from pathlib import Path

    from . import backends, dispatch
    from .transport import Docker, TransportError
    from .util import write_json

    ledger = Ledger(args.home)
    ledger.init()
    if args.action == "ls":
        return cmd_pool(args)
    if "docker" not in backends.kinds(ledger.config()):
        die('nodes belong to the Docker backend, which is a plugin and is not enabled here. For OrbStack machines the '
            'pool is one number: `org config set machines.orb.slots 6`.')
    from .backends.docker import nodes_of

    saved = read_json(ledger.config_path, {}) or {}
    docker_cfg = saved.setdefault("machines", {}).setdefault("docker", {})
    nodes = docker_cfg.setdefault("nodes", dict(nodes_of(ledger.config())))
    if args.action == "rm":
        if args.name not in nodes:
            die("no node named %s (have: %s)" % (args.name, ", ".join(sorted(nodes))))
        if dispatch.node_load(ledger).get(("docker", args.name)):
            die("%s still has engineers running; stop them or let them finish first" % args.name)
        del nodes[args.name]
        write_json(ledger.config_path, saved)
        print("removed %s; containers already on it are untouched" % args.name)
        return 0
    host = None if args.host in (None, "", "local") else args.host
    cfg = ledger.config()["machines"]["docker"]
    if not args.no_build:
        root = Path(__file__).resolve().parent.parent
        if not (root / "docker" / "Dockerfile").exists():
            die("this install has no docker/Dockerfile to build from; build the image on that host yourself and pass --no-build")
        print("building %s on %s (the first build on a host takes a few minutes)" % (cfg["image"], host or "this Docker daemon"))
        env = dict(os.environ)
        if subprocess.run(Docker.cli(host) + ["buildx", "version"], capture_output=True).returncode != 0:
            env["DOCKER_BUILDKIT"] = "0"  # a client without the buildx plugin can only use the classic builder
        built = subprocess.run(Docker.cli(host) + ["build", "-q", "-t", cfg["image"], "-f", str(root / "docker" / "Dockerfile"), str(root)], env=env)
        if built.returncode != 0:
            die("the image did not build on %s; nothing was changed" % (host or "this daemon"))
    try:
        Docker.ensure_network(host, cfg.get("network") or "org-engineers")
    except TransportError as e:
        die(e)
    entry = {"host": host, "slots": args.slots}
    for key in ("cpus", "memory"):
        if getattr(args, key):
            entry[key] = getattr(args, key)
    nodes[args.name] = entry
    write_json(ledger.config_path, saved)
    print("node %s: %s, %d slot(s)" % (args.name, host or "this Docker daemon", args.slots))
    return 0


def cmd_config(args) -> int:
    """Read or change config.json by dotted key. Values are JSON; a bare word is taken as a string."""
    from .util import write_json

    ledger = Ledger(args.home)
    ledger.init()
    if args.action == "get":
        value = ledger.config()
        for part in (args.key.split(".") if args.key else []):
            if not isinstance(value, dict) or part not in value:
                die("no such key: %s" % args.key)
            value = value[part]
        if isinstance(value, dict):
            value = {k: v for k, v in value.items() if not k.startswith("_")}
        print(json.dumps(value, indent=2, sort_keys=True))
        return 0
    try:
        value = json.loads(args.value)
    except ValueError:
        value = args.value
    saved = read_json(ledger.config_path, {}) or {}
    node = saved
    parts = args.key.split(".")
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value
    write_json(ledger.config_path, saved)
    print("%s = %s" % (args.key, json.dumps(value)))
    return 0


def cmd_agent(args) -> int:
    """The host agent: what creates, enters and deletes OrbStack machines for a manager in a machine."""
    from . import agent

    if args.action == "run":      # on the Mac
        return agent.serve(args.config)
    if args.action == "pipe":     # in the manager's machine, started by the agent
        return agent.pipe(args.port)
    ledger = Ledger(args.home)
    if args.action == "bridge":   # in the manager's machine, started by the agent
        return agent.bridge(ledger, args.dash_port)
    try:
        client = agent.Client(agent.socket_path(ledger.root))
        info = client.info()
        print("host agent connected: manager %s; engineers %s<run>, cloned from %s" % (info["manager"], info["prefix"], info["template"]))
        for name in client.machines():
            print("  " + name)
        return 0
    except agent.AgentError as e:
        die(e)


def cmd_pump(args) -> int:
    from . import dispatch

    started = dispatch.pump(Ledger(args.home), log=lambda m: print("  " + m))
    print("started: %s" % (", ".join(started) or "nothing"))
    return 0


def cmd_login(args) -> int:
    from . import login

    if args.status:
        s = login.status()
        print("Claude token: %s" % ("present" if s["claude_token"] else "missing"))
        print("GitHub:       %s" % ("logged in" if s["github"] else "not logged in"))
        print("git identity: %s" % (("%s <%s>" % (s["git_name"], s["git_email"])) if s["git_name"] and s["git_email"] else "not set"))
        return 0 if (s["claude_token"] and s["github"]) else 1
    return login.run(force=args.force, token_stdin=args.token_stdin)


def cmd_dash(args) -> int:
    from . import dash

    ledger = Ledger(args.home)
    if args.url:
        ledger.init()
        print(dash.url(ledger, args.port))
        return 0
    return dash.serve(ledger, port=args.port, open_browser=not args.no_open, host=args.host)


def cmd_doctor(args) -> int:
    from . import doctor

    return doctor.run(Ledger(args.home))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="org", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--home", help="ledger directory (default $ORG_HOME or ~/.org)")
    p.add_argument("--version", action="version", version="org " + __version__)
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("init", help="create the ledger").set_defaults(fn=cmd_init)
    sub.add_parser("doctor", help="check that this machine is ready to manage engineers").set_defaults(fn=cmd_doctor)

    s = sub.add_parser("talk", help="talk to the manager")
    s.add_argument("prompt", nargs="?", help="an opening message")
    s.add_argument("--project")
    s.add_argument("--resume", action="store_true", help="continue the previous conversation")
    s.add_argument("--dry-run", action="store_true", help="print the session's settings instead of starting it")
    s.set_defaults(fn=cmd_talk)

    s = sub.add_parser("ask", help="one answer from the manager about the current state")
    s.add_argument("question", nargs="+")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("spec", help="drafts and approved specs")
    ss = s.add_subparsers(dest="action", required=True)
    a = ss.add_parser("new")
    a.add_argument("title")
    a.add_argument("--project")
    ss.add_parser("ls")
    for name in ("check", "revise"):
        ss.add_parser(name).add_argument("spec")
    a = ss.add_parser("show")
    a.add_argument("spec")
    a.add_argument("--approved", action="store_true", help="the approved text instead of the draft")
    s.set_defaults(fn=cmd_spec)

    s = sub.add_parser("approve", help="freeze a draft and dispatch an engineer")
    s.add_argument("spec")
    s.add_argument("--sha", required=True, help="first 8+ characters of the draft's sha256, from `org spec check`")
    s.add_argument("--machine", help="orb, ssh:<name> or local (overrides the spec header)")
    s.add_argument("--no-dispatch", action="store_true")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("dispatch", help="start a new run of an approved spec")
    s.add_argument("spec")
    s.add_argument("--machine")
    s.set_defaults(fn=cmd_dispatch)

    s = sub.add_parser("status", help="every run: state, time, ETA, spend")
    s.add_argument("--json", action="store_true")
    s.add_argument("--watch", "-w", action="store_true")
    s.add_argument("--interval", type=float, default=10.0)
    s.add_argument("--no-sync", action="store_true", help="show the ledger as it is, without asking the machines")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("sync", help="pull state from the engineers' machines")
    s.add_argument("run", nargs="?")
    s.set_defaults(fn=cmd_sync)

    for name, fn, text in (("plan", cmd_plan, "the plan as frozen before execution"), ("report", cmd_report, "the engineer's report"),
                           ("review", cmd_review, "plan against outcome")):
        s = sub.add_parser(name, help=text)
        s.add_argument("run")
        if name == "plan":
            s.add_argument("--current", action="store_true", help="the plan with the engineer's later amendments")
        s.set_defaults(fn=fn)

    s = sub.add_parser("peek", help="what one engineer is doing right now")
    s.add_argument("run")
    s.add_argument("-n", "--lines", type=int, default=12)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_peek)

    s = sub.add_parser("logs", help="a run's activity feed")
    s.add_argument("run")
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("-n", "--lines", type=int, default=40)
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_logs)

    s = sub.add_parser("stop", help="stop a run's engineer")
    s.add_argument("run")
    s.set_defaults(fn=cmd_stop)
    s = sub.add_parser("resume", help="restart the runner of a run whose process died")
    s.add_argument("run")
    s.set_defaults(fn=cmd_resume)

    s = sub.add_parser("machine", help="the machines runs used")
    ss = s.add_subparsers(dest="action", required=True)
    ss.add_parser("ls")
    a = ss.add_parser("rm")
    a.add_argument("run")
    a.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_machine)

    s = sub.add_parser("project", help="projects and their repositories")
    ss = s.add_subparsers(dest="action", required=True)
    ss.add_parser("ls")
    a = ss.add_parser("add")
    a.add_argument("name")
    a.add_argument("--repo", action="append", help="owner/name or a clone URL; repeatable")
    a.add_argument("--path", help="a local checkout the manager may read")
    s.set_defaults(fn=cmd_project)

    s = sub.add_parser("dash", help="the dashboard as a local web page")
    s.add_argument("--port", type=int)
    s.add_argument("--host", default=None, help="address to listen on (default 127.0.0.1; 0.0.0.0 inside a container)")
    s.add_argument("--no-open", action="store_true")
    s.add_argument("--url", action="store_true", help="print the dashboard's URL and exit")
    s.set_defaults(fn=cmd_dash)

    for name, text in (("ls", "list files on a run's machine (its workspace by default)"),
                       ("cat", "print a file from a run's machine"),
                       ("diff", "the state of every repository in a run's workspace")):
        s = sub.add_parser(name, help=text)
        s.add_argument("run")
        if name == "ls":
            s.add_argument("path", nargs="?")
        elif name == "cat":
            s.add_argument("path")
        s.set_defaults(fn=cmd_fs)

    s = sub.add_parser("exec", help="run a command on a run's machine")
    s.add_argument("run")
    s.add_argument("--timeout", type=float, default=600)
    s.add_argument("command", nargs=argparse.REMAINDER, help="after --, the command; one argument is taken as a shell line")
    s.set_defaults(fn=cmd_exec)

    sub.add_parser("pool", help="how many engineers may run at once, where, and what is queued").set_defaults(fn=cmd_pool)

    s = sub.add_parser("config", help="read or change config.json")
    ss = s.add_subparsers(dest="action", required=True)
    a = ss.add_parser("get")
    a.add_argument("key", nargs="?", help="a dotted key such as machines.orb; omit for everything")
    a = ss.add_parser("set")
    a.add_argument("key")
    a.add_argument("value", help="JSON, or a bare word for a string")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("agent", help="the host agent that manages OrbStack machines for a manager in a machine")
    ss = s.add_subparsers(dest="action", required=True)
    a = ss.add_parser("run", help="run it, on the Mac (`org up` does this)")
    a.add_argument("--config", help="its config file (default ~/.config/org/agent.json)")
    ss.add_parser("status", help="is it connected, and which machines does it hold")
    a = ss.add_parser("bridge", help="the manager's end of the link; the agent starts it")
    a.add_argument("--dash-port", type=int, help="also serve the dashboard on this port of the loopback")
    a = ss.add_parser("pipe", help="carry one connection to a local port; the agent starts it")
    a.add_argument("port", type=int)
    s.set_defaults(fn=cmd_agent)

    s = sub.add_parser("node", help="Docker backend: add or remove Docker hosts in the pool")
    ss = s.add_subparsers(dest="action", required=True)
    ss.add_parser("ls")
    a = ss.add_parser("add", help="add a host, or change one: org node add bigbox ssh://me@bigbox --slots 8")
    a.add_argument("name")
    a.add_argument("host", nargs="?", help="a DOCKER_HOST value such as ssh://user@host; omit for the daemon this side reaches directly")
    a.add_argument("--slots", type=int, default=4, help="engineers that may run there at once")
    a.add_argument("--cpus", help="CPU limit per engineer container, e.g. 4")
    a.add_argument("--memory", help="memory limit per engineer container, e.g. 16g")
    a.add_argument("--no-build", action="store_true", help="the image is already on that host")
    ss.add_parser("rm").add_argument("name")
    s.set_defaults(fn=cmd_node)
    sub.add_parser("pump", help="start queued runs that can now be placed").set_defaults(fn=cmd_pump)

    s = sub.add_parser("login", help="the Claude token, GitHub login and git identity engineers are given")
    s.add_argument("--status", action="store_true", help="say what is in place and exit")
    s.add_argument("--force", action="store_true", help="log in again even if a login is present")
    s.add_argument("--token-stdin", action="store_true", help="store a Claude token read from stdin")
    s.set_defaults(fn=cmd_login)

    s = sub.add_parser("engineer", help=argparse.SUPPRESS)
    ss = s.add_subparsers(dest="action", required=True)
    a = ss.add_parser("run")
    a.add_argument("dir")
    a = ss.add_parser("snapshot")
    a.add_argument("dir")
    a.add_argument("--events-from", type=int, default=0)
    s.set_defaults(fn=cmd_engineer)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return 0
    try:
        return int(args.fn(args) or 0)
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130
