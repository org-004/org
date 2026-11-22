"""The `crew` command.

    crew talk                       talk to the manager: refine a spec, or ask about runs
    crew status [--watch]           what every run is doing, time, ETA, spend
    crew dash                       the same as a local web page, with the manager chat beside it
    crew ask "question"             one answer from the manager about the current state

    crew spec new "<title>"         start a draft            crew spec check <spec>
    crew spec show <spec>           print a draft or spec    crew spec ls
    crew approve <spec> --sha <8+>  freeze the draft and dispatch an engineer
    crew dispatch <spec>            run an approved spec again

    crew plan|report|review <run>   the frozen plan, the report, plan against outcome
    crew peek <run>                 what the engineer is doing right now
    crew logs <run> [-f]            its activity feed
    crew stop|resume <run>          stop the engineer, or restart a runner that died
    crew machine rm <run>           delete the OrbStack machine a run used
    crew doctor                     check that this machine is ready to manage engineers

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
    print("crew: " + str(msg), file=sys.stderr)
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
        errors, warnings = spec.check(text, spec.default_machine(ledger))
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
        print("Edit the draft; `crew approve` will record it as the next version and start a new run.")
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
        print("nothing dispatched; `crew dispatch %s` starts another engineer on it" % spec_id)
        return 0
    print("approved %s v%d (sha256 %s)" % (spec_id, meta["version"], meta["sha256"][:12]))
    live = [r for r in ledger.runs_for(spec_id) if not ledger.run(r).get("final") and ledger.run(r).get("state") != "dispatch_failed"]
    if live:
        print("note: %s is still running against an earlier approval; `crew stop %s` if it should not continue" % (", ".join(live), live[-1]))
    if args.no_dispatch:
        print("not dispatched; `crew dispatch %s` starts an engineer" % spec_id)
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
    print("status:    crew status")
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
        die("%s is not finished and fully synced (state %s); `crew stop` it first, or pass --force to delete the machine anyway"
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


def cmd_dash(args) -> int:
    from . import dash

    return dash.serve(Ledger(args.home), port=args.port, open_browser=not args.no_open)


def cmd_doctor(args) -> int:
    from . import doctor

    return doctor.run(Ledger(args.home))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="crew", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--home", help="ledger directory (default $CREW_HOME or ~/.crew)")
    p.add_argument("--version", action="version", version="crew " + __version__)
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
    s.add_argument("--sha", required=True, help="first 8+ characters of the draft's sha256, from `crew spec check`")
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
    s.add_argument("--no-open", action="store_true")
    s.set_defaults(fn=cmd_dash)

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
