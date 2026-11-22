# crew

A product manager on your machine, engineers in theirs.

You talk to the manager. It turns a loose request into an exact spec, pushing back and looking at
the real system on the way, and you approve the text. Approval dispatches an engineer: an
autonomous Claude Code session, alone on its own OrbStack machine or remote VM, that plans the
work, executes it, and reports. While engineers work, the manager is a status page and a chat box
that answers from what the engineers have written down, without interrupting them.

Standard library Python (3.9 or later) around the `claude` CLI. No services, no database: the
state is files.

```
 your Mac                                   engineer machines (several at once)
 ┌───────────────────────────────┐          ┌──────────────────────────────────┐
 │ crew talk    refine a spec    │ approve  │ OrbStack clone of `base`         │
 │ crew dash    status + chat    │ ───────▶ │   runner ─▶ claude -p (bypass)   │
 │ crew status  the same, in a   │          │   plan ▸ milestones ▸ report     │
 │              terminal         │ ◀─────── │   status.json  plan.md  report.md│
 │ ledger: ~/.crew               │   pull   ├──────────────────────────────────┤
 └───────────────────────────────┘          │ or a remote VM over ssh          │
                                            └──────────────────────────────────┘
```

The Mac always initiates. An engineer's machine never needs a route back, so the machines can
stay isolated, and reading a run's state never touches the engineer's session.

## Setup

On the Mac:

```sh
git clone <this repo> ~/crew && ln -s ~/crew/bin/crew ~/.local/bin/crew
crew init          # creates the ledger at ~/.crew ($CREW_HOME to move it)
crew doctor        # says what is missing
```

What it needs:

- Claude Code, logged in (the manager uses the Mac's own login).
- OrbStack, with a template machine that has `python3`, `git`, `gh` (logged in) and `claude`.
  The default name is `base`; each engineer runs in a fresh `orb clone` of it.
- `~/.claude/oauth-token`: a long-lived token from `claude setup-token`. It is copied into each
  engineer's machine over stdin, mode 600, the same way `box <name>` does it.
- For remote VMs, an entry per host in `~/.crew/config.json` (below). The host needs `python3`,
  `git`, `claude` with a working login, and key-based ssh.

## The flow

```sh
crew talk "add CSV export to the table view"
```

1. **Refine.** The manager restates what you are after, reads the repositories (cloning into its
   scratch space to build and test), checks what is already running, challenges the request and
   offers alternatives. It asks only what is yours to decide.
2. **Review.** It writes `~/.crew/desk/drafts/<spec>.md` and runs `crew spec check`, which prints
   the draft's sha256. Read the draft there, or in `crew dash` under "Drafts awaiting approval".
3. **Approve.** Say so. The manager runs `crew approve <spec> --sha <prefix>` and Claude Code asks
   you to confirm that command. That confirmation is the approval: the rules the manager runs
   under make the command always ask, and the hash ties it to the text you read. If the draft
   changed after you looked, the hash no longer matches and nothing happens. You can also approve
   from a terminal or from the dashboard.
4. **Watch.** `crew status`, `crew dash`, or `crew ask "why is the export run late?"`.
5. **Afterwards.** `crew report <run>` is the engineer's report. `crew review <run>` sets the
   frozen plan against what happened. `crew machine rm <run>` deletes the OrbStack machine once
   the work is pushed.

## The manager

One persona, two ways in.

`crew talk` is an interactive Claude Code session. It can read everything and run anything, and it
can change nothing that matters:

| It can | It cannot |
|---|---|
| read any file, any repository, any run | edit your checkouts, approved specs, or run records |
| clone, build and test in `desk/scratch` | commit, push, open or merge pull requests, file issues |
| write drafts and project briefs in the desk | approve, dispatch or stop without your confirmation |
| search the web | touch an engineer's machine |

Three layers hold that, each for what it can see: Claude Code's Bash sandbox confines shell
commands to the desk; `blockWritesOutsideWorkingDirectories` confines the file tools to it; deny
rules refuse the commands that change remote state. `crew talk --dry-run` prints the exact
settings. `python3 tests/live_manager_rules.py` checks them against the real harness.

`crew ask` and the dashboard's chat box are the same persona, headless and read-only: nothing
that would need permission can run. It answers from the ledger and from `crew peek`, and runs on
a cheaper model (`manager.ask_model`, default `sonnet`).

The manager keeps a brief per project (`desk/projects/<name>.md`): how to build and test, hot
paths, how far estimates run over. Engineers receive it with every spec.

## The engineer

A headless `claude -p` session with all permissions bypassed, because the machine is the sandbox.
A deterministic runner drives it through three phases, resuming one session throughout:

1. **Plan.** Research, then `plan.md` and a structured plan: verdict, milestones, and an estimate
   of time and cost with a 90th percentile. The runner checks the plan against the spec's caps,
   sends it back once if it does not fit, and freezes it (`plan.frozen.md`) for later review.
   A plan may say `infeasible`; the run then ends with `fail` and the reason.
2. **Execute.** One turn per milestone. Parallelism lives inside a milestone: the engineer fans
   out to subagents on cheaper models, in separate worktrees, against interfaces it settled
   first. After each milestone the runner records the real cost and re-forecasts the finish. The
   engineer may replan the remaining milestones; the original stays frozen.
3. **Report.** The runner hands over its measurements (estimate against actual per milestone,
   retries, waits, repositories with unpushed work) and the engineer verifies each acceptance
   criterion, writes `report.md`, and returns `done`, `partial` or `fail` with a reason.

The runner, not the engineer, owns the things that must not depend on a model remembering:

- **Status.** `status.json` is rewritten every few seconds: phase, milestones, spend, forecast,
  current tool call, running subagents, the weekly limit as the CLI reports it.
- **Caps.** `max_cost_usd` is passed to the CLI as a hard budget, less a reserve so a run that
  hits the cap can still write its report. `max_hours` ends the turn in flight.
- **Recovery.** A turn that crashes or ends in prose is resumed with a nudge. A usage limit is
  waited out until the reset. A session that cannot be resumed is replaced, and the engineer is
  told to read its own plan and the repositories. A runner that dies is restarted with
  `crew resume` and continues from the last finished step.
- **Honesty.** `done` is lowered to `partial` unless every acceptance criterion in the spec is
  reported as passing. The spec in the run directory is read-only and is restored if it changes.

An engineer cannot ask anyone anything, and is not written to be talked to.

## Specs

One Markdown file with a flat header:

```
---
title: CSV export for the table view
project: acme
repos: gasblaster/acme-web, gasblaster/acme-api
delivery: pr            # pr | branch | merge | none
machine: orb            # orb | ssh:<name> | local
max_cost_usd: 40        # hard caps; set at least one
max_hours: 6
max_weekly_pct: 10      # enforced once the dollars-per-percent rate is known
model: fable            # optional overrides for the engineer
effort: high
---
```

and these sections: Intent, Context, Requirements (`- R1 (MUST): ...`, `- R2 (SHOULD): ...`), Out of
scope, Acceptance criteria (`- AC1: <command> <expected observation>`), Constraints, Alternatives
considered, Open questions (which must read `None`). `crew spec check` enforces the parts a
program can check. Changing an approved spec means revising the draft and approving again, which
records a new version and starts a new run.

## Time, money and the weekly limit

- **Spend** is the CLI's own cost figure at list prices, taken at the end of every turn. Inside a
  turn it is estimated from streamed token counts and corrected when the turn ends.
- **The weekly limit** is the account-wide figure the CLI reports at the start of every engineer
  turn. It is exact and shared with everything else you do with Claude.
- **A run's share of the weekly limit** needs the exchange rate between the two, which is not
  published. crew learns it from finished runs (the largest dollars-per-point any run observed,
  since other activity only inflates the movement), or you set `budget.usd_per_weekly_pct`.
  Until one of those exists the share shows as unknown instead of as a guess.
- **ETA** starts as the plan's estimate and is re-derived from the pace of finished milestones.
- **History.** Each new engineer is told how past estimates turned out on this ledger and is
  asked to correct for it.

## Commands

```
crew talk [message]             talk to the manager        crew ask "question"
crew status [--watch|--json]    every run                  crew dash
crew spec new|check|show|ls|revise
crew approve <spec> --sha <8+>  freeze and dispatch        crew dispatch <spec> [--machine M]
crew peek <run>                 one run, in words          crew logs <run> [-f]
crew plan|report|review <run>
crew stop <run>                 crew resume <run>          crew machine ls | rm <run>
crew project add <name> --repo owner/name [--path ~/checkout]
crew doctor                     crew init
```

A `<run>` may be a run id, a unique prefix, or a spec reference (its latest run). A `<spec>` may
be its id, its number, or a unique prefix.

## Configuration

`~/.crew/config.json`. Everything is optional; these are the knobs most likely to matter.

```json
{
  "machines": {
    "default": "orb",
    "orb": {"base": "base", "prefix": "eng-", "push_token": true},
    "ssh": {"bigbox": {"host": "me@bigbox", "options": ["-p", "2222"], "push_token": false}}
  },
  "engineer": {"model": "fable", "effort": "high", "max_turn_retries": 3, "report_reserve_usd": 3.0},
  "manager": {"ask_model": "sonnet", "sandbox": true, "network_allow": ["github.com", "*.github.com"]},
  "budget": {"usd_per_weekly_pct": null},
  "dash": {"port": 8787, "sync_interval_s": 10}
}
```

## Where things are

```
~/.crew/
  desk/drafts/<spec>.md      specs under refinement      (the manager may write here)
  desk/projects/<name>.md    project briefs
  desk/scratch/              the manager's throwaway clones
  specs/<spec>/spec.md       approved, read-only; meta.json holds the sha256 and version
  runs/<run>/run.json        where it runs
  runs/<run>/mirror/         status.json, plan.frozen.md, report.md, actuals.json, events.jsonl
  budget.json                weekly-limit readings

on an engineer's machine:
  ~/crew-runs/<run>/         spec.md, job.json, status.json, plan.md, report.md, events.jsonl
  ~/work/<run>/              the engineer's clones
  tmux session crew-<run>    the runner's live feed
```

`claude --resume <session id from status.json>` on the engineer's machine reopens the engineer's
own conversation, which is the most direct way to ask why it did something after the fact.

## The dashboard

`crew dash` serves one self-contained page on `127.0.0.1` and prints its URL. The part after `#`
is an access token: the page can approve specs and stop runs, so every request must carry it, and
requests not addressed to this machine are refused. Stopping the dashboard does not affect
engineers.

## Tests

```sh
python3 -m unittest discover -s tests       # no tokens spent: a scripted stand-in for
                                            # the claude CLI and a simulated `orb`
python3 tests/demo_ledger.py /tmp/demo && crew --home /tmp/demo dash   # the page, with made-up runs
python3 tests/live_manager_rules.py         # the manager's confinement, real harness (a few cents)
python3 tests/e2e.py --machine orb          # a real engineer on a real machine (well under $1)
```

`tests/e2e.py` approves a small spec, dispatches it, follows it to the end, and then repeats the
acceptance checks itself in a fresh clone on the engineer's machine instead of taking the
engineer's word.

## What has and has not been exercised

Built and tested inside one Linux OrbStack machine, which cannot run `orb` or macOS. So:

- **Exercised for real:** the engineer runner with a real Claude Code engineer, locally and over
  ssh; the manager's question mode; the manager's permission rules against the real harness; the
  dashboard in a real browser; the whole suite under Python 3.9 and 3.14.
- **Simulated only:** OrbStack. The `orb` commands are the ones `box` already uses (`orb clone`,
  `orb start`, `orb -m <name> <argv>`, `orb delete -f`), tested against a stand-in.
- **Not exercised:** the macOS Bash sandbox, and `crew talk` as an interactive session.

First things to run on the Mac: `crew doctor`, `python3 tests/live_manager_rules.py`, then
`python3 tests/e2e.py --machine orb`.

## Limits worth knowing

- One engineer per spec, one machine per engineer. Width comes from the engineer's subagents and
  from running several specs at once.
- Engineers carry the template machine's `gh` login and the Claude token. The isolation is
  OrbStack's; a run can do whatever those credentials can do. A spec's `delivery` says how work
  lands, and the default is a pull request, not a merge.
- All engineers and you share one weekly limit. A run that hits it waits for the reset and
  resumes on its own.
- The manager's shell deny rules stop an overeager model, not a hostile one. The sandbox is the
  boundary; keep `manager.sandbox` on.
- `gh` runs outside the manager's sandbox, because on macOS its token lives in the keychain, and
  is governed by the permission rules instead: reads are allowed, anything that changes GitHub is
  refused. If cloning a private repository fails inside the sandbox for the same reason, add
  `"git clone *"` to `manager.sandbox_exclude`; the clone will then ask before it runs.
- `machine: local` runs an engineer on the manager's machine with permissions bypassed. It is
  off unless enabled in config, and belongs inside a box.
