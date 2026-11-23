# org

A product manager and a team of engineers, each in a box of its own.

You talk to the manager. It turns a loose request into an exact spec, pushing back and looking at
the real system on the way, and you approve the text. Approval dispatches an engineer: an
autonomous Claude Code session, alone in its own machine, that plans the work, executes it, and
reports. While engineers work, the manager is a status page and a chat box that answers from what
the engineers have written down, without interrupting them.

Standard library Python around the `claude` CLI. No services, no database: the state is files.

```
 your Mac: OrbStack, the `org` wrapper, and one background process (the host agent)
 ┌───────────────────────────────────────────────────────────────────────┐
 │ org-manager    an isolated OrbStack machine                           │
 │   org talk     refine a spec with you, confined to its desk           │
 │   org dash     status page + chat, on localhost:8787                  │
 │   holds        the ledger, the logins, a socket to the host agent     │
 │        │ create / run a command in / delete, through the host agent   │
 │        ▼                                                              │
 │ org-eng-<run>  one isolated machine per run, cloned from org-base     │
 │ org-eng-<run>  runner ─▶ claude -p: plan ▸ milestones ▸ report        │
 └───────────────────────────────────────────────────────────────────────┘
   more capacity: any host with sshd, listed as ssh:<name>
```

The manager always initiates. An engineer never needs a route back, and reading a run's state
never touches the engineer's session.

[docs/design.md](docs/design.md) explains why it is built this way and what has been checked.

## Setup

```sh
gh repo clone <you>/org ~/org && mkdir -p ~/.local/bin && ln -s ~/org/bin/org ~/.local/bin/org
org up         # make the template and the manager, hand them this Mac's logins, start the agent
org login      # only if `org up` says a step is left
org talk
```

It needs [OrbStack](https://orbstack.dev) and the `python3` that macOS's command line tools
provide (the host agent runs on it). Nothing else is installed on the Mac.

`org up` makes three things and keeps any that already exist:

| | What | Notes |
|---|---|---|
| `org-base` | the template: an isolated machine (Debian) with python3, git, gh, tmux, Docker and Claude Code | every engineer is a clone of it, and it is left stopped; add what your projects need with `orb -m org-base` |
| `org-manager` | a clone of the template with org installed | the ledger is inside it, at `~/.org` |
| the host agent | one background process on the Mac | config, pid and log in `~/.config/org/` |

```sh
org up --from mybox        # the template is a clone of a machine you already have, not a new one
org up --distro ubuntu     # another distribution for a new template (default debian:trixie)
org up --slots 6           # engineers that may run at once (default 4)
org up --port 9000         # the dashboard's port (default 8787)
```

Run `org up` again after pulling a new version, after a login on the Mac changes, and after the
Mac restarts: it updates the copy of org in the manager and starts the agent. It takes a couple
of seconds when the machines exist. `org down` stops the agent and the manager, `org down --all`
also deletes the engineers' machines, and `--purge` deletes the manager, the ledger in it, and
the template.

Engineers act as you, so the template and the manager are handed three things from the Mac:

| | From the Mac | Otherwise |
|---|---|---|
| a long-lived Claude token | `~/.claude/oauth-token` (what `claude setup-token` prints), copied on every `org up` | `org login` runs `claude setup-token` in the manager |
| a GitHub login | `gh auth token`, if the machine has none | `org login` runs `gh auth login` there |
| a git identity | `git config --global user.name` and `user.email`, if the machine has none | taken from the GitHub account |

## The flow

```sh
org talk "add CSV export to the table view"
```

1. **Refine.** The manager restates what you are after, reads the repositories (cloning into its
   scratch space to build and test), checks what is already running, challenges the request and
   offers alternatives. It asks only what is yours to decide.
2. **Review.** It writes a draft and runs `org spec check`, which prints the draft's sha256. Read
   it with `org spec show <spec>`, or in `org dash` under "Drafts awaiting approval".
3. **Approve.** Say so. The manager runs `org approve <spec> --sha <prefix>` and Claude Code asks
   you to confirm that command. That confirmation is the approval: the rules the manager runs
   under make the command always ask, and the hash ties it to the text you read. If the draft
   changed after you looked, the hash no longer matches and nothing happens. You can also approve
   from a terminal or from the dashboard.
4. **Watch.** `org status`, `org dash`, or `org ask "why is the export run late?"`.
5. **Afterwards.** `org report <run>` is the engineer's report. `org review <run>` sets the
   frozen plan against what happened. `org machine rm <run>` deletes the engineer's machine
   once the work is pushed; the report and the record stay.

## The manager

One persona, two ways in.

`org talk` is an interactive Claude Code session. It can read everything and run anything, and it
can change nothing that matters:

| It can | It cannot |
|---|---|
| read any file, any repository, any run | edit checkouts, approved specs, or run records |
| clone, build and test in its scratch space | commit, push, open or merge pull requests, file issues |
| write drafts and project briefs in its desk | approve, dispatch, stop or `org exec` without your confirmation |
| look at an engineer's files: `org ls`, `org cat`, `org diff` | read the login files it hands to engineers |
| search the web | reach the host agent, or change anything on an engineer's machine, by itself |

What holds that:

- **Claude Code's shell sandbox** confines shell commands to the desk and keeps them from the
  login files and from every Unix socket, at the operating-system level. `org` itself runs
  outside it, because it must write the ledger and reach the machines, and is governed by rules
  instead.
- **`blockWritesOutsideWorkingDirectories`** confines the file tools to the desk.
- **Rules.** Read commands are allowed. `org approve`, `dispatch`, `stop` and `exec` always ask
  you, in every permission mode. Pushes and other commands that change GitHub are refused.

Where there is no working sandbox, `org talk` says so and starts the session in the ask-first
permission mode, so that a write to a checkout is always your decision and never a classifier's.

`org talk --dry-run` prints the exact settings. `tests/live_manager_rules.py` attempts fourteen
actions against the real harness and judges by outcome.

`org ask` and the dashboard's chat box are the same persona, headless and read-only: nothing
that would need permission can run. It runs on a cheaper model (`manager.ask_model`).

The manager's Claude Code is a fresh install, so `org talk` runs on that install's default model
until you choose one: `org config set manager.model opus`.

The manager keeps a brief per project (`desk/projects/<name>.md`): how to build and test, hot
paths, how far estimates run over. Engineers receive it with every spec.

## The engineer

A headless `claude -p` session with all permissions bypassed, because its machine is the
sandbox: an isolated OrbStack machine has none of the Mac's files, cannot run commands on the
Mac and cannot reach it over the network. It is a whole Linux system with sudo and a Docker
daemon of its own, so a project whose tests need containers can run them. A deterministic runner
drives the session through three phases, resuming one session throughout:

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
  `org resume` and continues from the last finished step.
- **Honesty.** `done` is lowered to `partial` unless every acceptance criterion in the spec is
  reported as passing. The spec in the run directory is read-only and is restored if it changes.

An engineer cannot ask anyone anything, and is not written to be talked to. It is told what its
machine has: its cores, and whether a Docker daemon answered just before the run.

## The pool

One number: how many engineers' machines may run at once.

```sh
org pool                                  # slots in use, what is queued
org config set machines.orb.slots 6       # change it
```

A run takes a slot when it is dispatched. When every slot is taken it waits in the queue, and
starts when one frees: anything that looks at the ledger (`org status`, the dashboard) moves the
queue, so there is no scheduler to keep alive.

A host that already exists can take runs too. List it in the manager's `config.json` and name it
in a spec (`machine: ssh:bigbox`); the manager needs a key that logs in there without a prompt.

```json
{"machines": {"ssh": {"bigbox": {"host": "me@bigbox", "slots": 2, "push_token": true}}}}
```

## Looking at an engineer's work

Without touching its session:

```sh
org peek <run>              # state, milestones, spend, recent activity
org diff <run>              # every repository in its workspace: branch, unpushed, uncommitted
org ls <run> [path]         # its files (the workspace by default)
org cat <run> <path>
org exec <run> -- <cmd>     # anything; the manager chat has to ask you first
```

`orb -m org-eng-<run> tmux attach -t org-<run>` on the Mac shows the runner's live feed, and
`claude --resume <session id from status.json>` inside that machine reopens the engineer's own
conversation, which is the most direct way to ask why it did something.

## Specs

One Markdown file with a flat header:

```
---
title: CSV export for the table view
project: acme
repos: acme/web, acme/api
delivery: pr            # pr | branch | merge | none
machine: orb            # orb | ssh:<name>; leave it out for the default
max_cost_usd: 40        # hard caps; set at least one
max_hours: 6
max_weekly_pct: 10      # enforced once the dollars-per-percent rate is known
model: fable            # optional overrides for the engineer
effort: high
---
```

and these sections: Intent, Context, Requirements (`- R1 (MUST): ...`, `- R2 (SHOULD): ...`), Out of
scope, Acceptance criteria (`- AC1: <command> <expected observation>`), Constraints, Alternatives
considered, Open questions (which must read `None`). `org spec check` enforces the parts a program
can check, including that every repository is one an engineer can reach and that the machine it
names is one this installation has. Changing an approved spec means revising the draft and
approving again, which records a new version and starts a new run.

## Time, money and the weekly limit

- **Spend** is the CLI's own cost figure at list prices, taken at the end of every turn. Inside a
  turn it is estimated from streamed token counts and corrected when the turn ends.
- **The weekly limit** is the account-wide figure the CLI reports at the start of every engineer
  turn. It is exact and shared with everything else you do with Claude.
- **A run's share of the weekly limit** needs the exchange rate between the two, which is not
  published. org learns it from finished runs (the largest dollars-per-point any run observed,
  since other activity only inflates the movement), or you set `budget.usd_per_weekly_pct`.
  Until one of those exists the share shows as unknown instead of as a guess.
- **ETA** starts as the plan's estimate and is re-derived from the pace of finished milestones.
- **History.** Each new engineer is told how past estimates turned out on this ledger and is
  asked to correct for it.

## Commands

```
org up | down | login | shell   set up, tear down, log in, a shell in the manager
org talk [message]              talk to the manager        org ask "question"
org status [--watch|--json]     every run                  org dash
org spec new|check|show|ls|revise
org approve <spec> --sha <8+>   freeze and dispatch        org dispatch <spec> [--machine M]
org peek <run>                  one run, in words          org logs <run> [-f]
org plan|report|review <run>
org ls|cat|diff <run>           an engineer's files        org exec <run> -- <cmd>
org stop <run>                  org resume <run>           org machine ls | rm <run>
org pool                        org pump                   org config get|set <key> [value]
org project add <name> --repo owner/name
org doctor                      org agent status           org init
```

A `<run>` may be a run id, a unique prefix, or a spec reference (its latest run). A `<spec>` may
be its id, its number, a unique prefix, or the path of its draft.

On the Mac, `org` runs `up`, `down` and the agent itself and passes every other command into the
manager's machine.

## Configuration

`config.json` in the ledger, read and changed with `org config get|set <dotted.key> [json]`.
Everything is optional; these are the values `org up` sets and the defaults around them.

```json
{
  "machines": {
    "default": "orb",
    "orb": {"via": "agent", "base": "org-base", "prefix": "org-eng-", "slots": 4, "push_token": true},
    "ssh": {}
  },
  "engineer": {"model": "fable", "effort": "high", "max_turn_retries": 3, "report_reserve_usd": 3.0},
  "manager": {"model": null, "ask_model": "sonnet", "sandbox": true, "network_allow": ["github.com", "*.github.com"]},
  "budget": {"usd_per_weekly_pct": null},
  "status": {"sync_timeout_s": 8},
  "dash": {"port": 8787, "sync_interval_s": 10}
}
```

The template's `~/.claude/settings.json` is written by `org up` when it has none: no attribution
lines in commits or pull requests, no auto memory.

## Where things are

```
on the Mac:
  ~/.config/org/agent.json   which machines the host agent serves; its presence is what makes
                             `org` forward into the manager
  ~/.config/org/agent.log    what the agent created, deleted and refused
  ~/.config/org/agent.pid

in the manager's machine (~/.org):
  desk/drafts/<spec>.md      specs under refinement      (the manager may write here)
  desk/projects/<name>.md    project briefs
  desk/scratch/              the manager's throwaway clones
  specs/<spec>/spec.md       approved, read-only; meta.json holds the sha256 and version
  runs/<run>/run.json        where it runs
  runs/<run>/mirror/         status.json, plan.frozen.md, report.md, actuals.json, events.jsonl
  budget.json                weekly-limit readings
  agent.sock                 the manager's end of the link to the host agent
  ~/.local/share/org/        the copy of this checkout that `org up` installed

in an engineer's machine:
  ~/org-runs/<run>/          spec.md, job.json, status.json, plan.md, report.md, events.jsonl
  ~/work/<run>/              the engineer's clones
  tmux session org-<run>     the runner's live feed
```

## The host agent

An isolated machine cannot create, enter or delete machines: `orb` does not work inside one, and
it cannot reach the Mac at all. So one process on the Mac does those three things for the
manager. It dials in, keeping a command open inside the manager's machine, and requests travel
up that pipe; nothing listens on the Mac. It acts only on machines named `org-eng-…`, clones only
the template, and a request has no way to name a command to run on the Mac. `org agent status`
says whether it is connected, and `~/.config/org/agent.log` records what it did.

## The dashboard

`org dash` prints the URL and opens it. The page is served inside the manager's machine on its
loopback, which OrbStack forwards to the Mac's `127.0.0.1`; other machines cannot reach it. The
part of the URL after `#` is an access token: the page can approve specs and stop runs, so every
request must carry it, and requests not addressed to this machine are refused.

## Docker instead of OrbStack machines

The same system runs with the manager and every engineer as Docker containers, on any Docker
engine. It is a plugin, kept for hosts without OrbStack.

```sh
org up --backend docker      # build the image, start the manager container, hand it the logins
org down --backend docker
```

There the manager holds the Docker socket instead of a link to the host agent, the pool is a set
of Docker hosts (`org node add bigbox ssh://me@bigbox --slots 8`), and an engineer cannot run
Docker inside its container. `docker/up.sh` and `org/backends/docker.py` describe it. Any module
that defines `BACKEND` can be loaded the same way: `{"machines": {"plugins": ["your.module"]}}`.

## Without a manager machine

The same package runs straight on a machine that has Python 3.9 or later and Claude Code:
`ORG_LOCAL=1 org ...`, or simply `org ...` where `org up` has never run. The ledger is then
`~/.org` on that machine. On the Mac that means the manager chat runs on the Mac itself, under
Seatbelt, and engineers' machines are made with the `orb` command directly, with no agent:

| `machine:` | Provisioned by | Reached by |
|---|---|---|
| `orb` | `orb clone <machines.orb.base> <name>`: a fresh OrbStack machine per run | `orb -m <name>` |
| `ssh:<name>` | exists already; listed under `machines.ssh` in config | `ssh` |
| `local` | nothing; off unless enabled, and belongs inside a box | a subprocess |

## Tests

```sh
python3 -m unittest discover -s tests       # no tokens spent: a scripted stand-in for the claude
                                            # CLI and simulated orb, docker and gh
python3 tests/demo_ledger.py /tmp/demo && ORG_LOCAL=1 org --home /tmp/demo dash   # the page, with made-up runs

# with real Claude Code, after `org up`
org shell -c 'python3 ~/.local/share/org/tests/e2e.py --machine orb'        # a real engineer in a real machine (well under $1)
org shell -c 'python3 ~/.local/share/org/tests/live_manager_rules.py'       # the manager's confinement (a few cents)
org shell -c 'python3 ~/.local/share/org/tests/live_manager_refine.py'      # a two-turn refinement of a request with a false premise
```

`tests/e2e.py` approves a small spec, dispatches it, follows it to the end, and then repeats the
acceptance checks itself in a fresh clone on the engineer's machine instead of taking the
engineer's word. It uses a ledger of its own, so it leaves nothing in yours, and deletes the
machine when the run passes.

## What has and has not been exercised

On a Mac with OrbStack 2.2.5:

- **Exercised for real:** `org up` from nothing (under a minute) and again (two seconds);
  `org down` and `org up` after it; a second installation beside the first, made with `--from`
  and other names; a real engineer finishing the smoke spec in a machine created
  through the host agent, on a temporary ledger and on the manager's own, and with the manager on
  the Mac and no agent; the manager chat starting in its machine with no first-run screens and
  answering from `org status`; the manager's confinement inside its machine with the shell
  sandbox live, in the default and auto permission modes; Docker building and running inside an
  isolated machine; the dashboard reaching the Mac's localhost, and the agent's own relay for
  where it does not; the unit suite under Python 3.9 on macOS and 3.13 on Linux.
- **Simulated only:** ssh machines beside OrbStack ones; `org up --from` a machine that is not
  isolated, which the script isolates before using.
- **Not exercised:** the link between the agent and the manager over hours (it ran for minutes;
  it pings every 15 seconds and reconnects when the link ends); a restart of the Mac.
- **The Docker plugin** was exercised on Linux before OrbStack became the default (see
  docs/design.md) and has not been run since on a Mac.

## Limits worth knowing

- **Engineers carry your GitHub login and Claude token.** A run can do whatever those allow. The
  default delivery is a pull request, not a merge; a GitHub token scoped to the repositories in
  play narrows the rest.
- **The template is the trust root.** Every engineer is a copy of `org-base`, so whatever is in
  it, including logins, is in every engineer's machine. With `--from`, that is everything in the
  machine you cloned.
- **Rules cannot see GitHub.** The manager's shell is kept off the filesystem by the sandbox, but
  changes over the network are stopped by deny rules for the commands that make them. Those stop
  an overeager manager, not a determined one.
- **Machines share a network.** An isolated machine cannot reach the Mac, but it can reach the
  internet and any port another machine opens on its network address. The dashboard and the
  agent's socket are not such ports. OrbStack also forwards every machine's listening ports to
  the Mac's localhost, so a server an engineer starts shows up there, and on the dashboard's
  port while the manager is stopped it is what a bookmark of the dashboard would open.
- **Nothing starts by itself.** After the Mac restarts, `org up` brings the agent back. Until
  then engineers that were running keep running, but nothing reads them.
- **One engineer per spec.** Width comes from the engineer's subagents and from running several
  specs at once.
- **All engineers and you share one weekly limit.** A run that hits it waits for the reset and
  resumes on its own.
