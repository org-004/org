# org

A product manager and a team of engineers, each in a box of its own.

You talk to the manager. It turns a loose request into an exact spec, pushing back and looking at
the real system on the way, and you approve the text. Approval dispatches an engineer: an
autonomous Claude Code session, alone in its own container, that plans the work, executes it, and
reports. While engineers work, the manager is a status page and a chat box that answers from what
the engineers have written down, without interrupting them.

Standard library Python around the `claude` CLI. No services, no database: the state is files.

```
 your machine: a Docker engine and a 50-line `org` wrapper
 ┌───────────────────────────────────────────────────────────────────────┐
 │ org-manager                                                           │
 │   org talk     refine a spec with you, confined to its desk           │
 │   org dash     status page + chat, on localhost:8787                  │
 │   holds        the ledger, the logins, the Docker socket              │
 │        │ docker run / exec / rm                                       │
 │        ▼                                                              │
 │ org-eng-<run>  one container per run: runner ─▶ claude -p             │
 │ org-eng-<run>  plan ▸ milestones ▸ report · no socket, own network    │
 └───────────────────────────────────────────────────────────────────────┘
   more capacity: another Docker host, added as ssh://user@host
```

The manager always initiates. An engineer never needs a route back, and reading a run's state
never touches the engineer's session.

[docs/design.md](docs/design.md) explains why it is built this way and what has been checked.

## Setup

```sh
gh repo clone <you>/org ~/org && ln -s ~/org/bin/org ~/.local/bin/org
org up         # build the image, start the manager, hand it this machine's logins
org login      # only if `org up` says a step is left
org talk
```

It needs a Docker engine and nothing else: OrbStack, Docker Desktop, Colima, or Docker on Linux.
No Python or Claude Code on the host.

`org up` creates one image (`org:latest`, used for the manager and every engineer), the
`org-manager` container, four volumes (the ledger, Claude Code's state, the GitHub login, ssh
keys) and two networks. Run it again after pulling a new version; the volumes carry everything
over. `org down` removes the manager, `org down --all` the engineers too, and `--purge` the
volumes.

Engineers act as you, so the manager needs three things to hand them, and `org up` copies each
from the host when it is there:

| | From the host | Otherwise |
|---|---|---|
| a long-lived Claude token | `~/.claude/oauth-token` (what `claude setup-token` prints) | `org login` runs `claude setup-token` in the manager |
| a GitHub login | `gh auth token` | `org login` runs `gh auth login` there |
| a git identity | `git config --global user.name` and `user.email` | taken from the GitHub account |

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
   frozen plan against what happened. `org machine rm <run>` deletes the engineer's container
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
| search the web | change anything on an engineer's machine by itself |

What holds that:

- **Claude Code's shell sandbox** confines shell commands to the desk and keeps them from the
  login files, at the operating-system level. `org` itself runs outside it, because it must write
  the ledger and reach the machines, and is governed by rules instead.
- **`blockWritesOutsideWorkingDirectories`** confines the file tools to the desk.
- **Rules.** Read commands are allowed. `org approve`, `dispatch`, `stop` and `exec` always ask
  you, in every permission mode. Pushes and other commands that change GitHub are refused.

Where there is no working sandbox, `org talk` says so and starts the session in the ask-first
permission mode, so that a write to a checkout is always your decision and never a classifier's.

`org talk --dry-run` prints the exact settings. `tests/live_manager_rules.py` attempts a dozen
actions against the real harness and judges by outcome.

`org ask` and the dashboard's chat box are the same persona, headless and read-only: nothing
that would need permission can run. It runs on a cheaper model (`manager.ask_model`).

The manager keeps a brief per project (`desk/projects/<name>.md`): how to build and test, hot
paths, how far estimates run over. Engineers receive it with every spec.

## The engineer

A headless `claude -p` session with all permissions bypassed, because its container is the
sandbox. A deterministic runner drives it through three phases, resuming one session throughout:

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

An engineer cannot ask anyone anything, and is not written to be talked to.

## The pool

Engineers run on Docker hosts, each with a number of slots. One host with four slots is the
default: the daemon the manager is on.

```sh
org pool                                           # nodes, slots in use, what is queued
org node add local --slots 6                       # change the local node
org node add bigbox ssh://me@bigbox --slots 8 --cpus 8 --memory 32g
org node rm bigbox
```

A run goes to the least loaded node with a free slot. When every slot is taken it waits in the
queue, and starts when one frees: anything that looks at the ledger (`org status`, the dashboard)
moves the queue, so there is no scheduler to keep alive. A spec can name a node
(`machine: docker:bigbox`).

`org node add` builds the image on the new host. A remote host needs a Docker daemon and an ssh
login from the manager: `org shell`, `ssh-keygen`, and add the public key on the host. The key
lives in a volume and survives `org up`.

## Looking at an engineer's work

Without touching its session:

```sh
org peek <run>              # state, milestones, spend, recent activity
org diff <run>              # every repository in its workspace: branch, unpushed, uncommitted
org ls <run> [path]         # its files (the workspace by default)
org cat <run> <path>
org exec <run> -- <cmd>     # anything; the manager chat has to ask you first
```

`docker exec -it org-eng-<run> tmux attach -t org-<run>` on the host shows the runner's live
feed, and `claude --resume <session id from status.json>` inside the container reopens the
engineer's own conversation, which is the most direct way to ask why it did something.

## Specs

One Markdown file with a flat header:

```
---
title: CSV export for the table view
project: acme
repos: acme/web, acme/api
delivery: pr            # pr | branch | merge | none
machine: docker         # docker | docker:<node> | orb | ssh:<name> | local
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
can check, including that every repository is one an engineer can reach. Changing an approved
spec means revising the draft and approving again, which records a new version and starts a new
run.

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
org pool                        org node add|rm            org pump
org project add <name> --repo owner/name
org doctor                      org init
```

A `<run>` may be a run id, a unique prefix, or a spec reference (its latest run). A `<spec>` may
be its id, its number, a unique prefix, or the path of its draft.

## Configuration

`config.json` in the ledger (`org shell`, then `~/.org/config.json`). Everything is optional.

```json
{
  "machines": {
    "default": "docker",
    "docker": {"image": "org:latest", "nodes": {"local": {"host": null, "slots": 4}}}
  },
  "engineer": {"model": "fable", "effort": "high", "max_turn_retries": 3, "report_reserve_usd": 3.0},
  "manager": {"model": null, "ask_model": "sonnet", "sandbox": true, "network_allow": ["github.com", "*.github.com"]},
  "budget": {"usd_per_weekly_pct": null},
  "status": {"sync_timeout_s": 8},
  "dash": {"port": 8787, "sync_interval_s": 10}
}
```

`docker/claude-settings.json` is the Claude Code settings file baked into the image: no
attribution lines in commits or pull requests, no auto memory.

## Where things are

```
in the manager (volume org-ledger, at ~/.org):
  desk/drafts/<spec>.md      specs under refinement      (the manager may write here)
  desk/projects/<name>.md    project briefs
  desk/scratch/              the manager's throwaway clones
  specs/<spec>/spec.md       approved, read-only; meta.json holds the sha256 and version
  runs/<run>/run.json        where it runs
  runs/<run>/mirror/         status.json, plan.frozen.md, report.md, actuals.json, events.jsonl
  budget.json                weekly-limit readings

in an engineer's container:
  ~/org-runs/<run>/          spec.md, job.json, status.json, plan.md, report.md, events.jsonl
  ~/work/<run>/              the engineer's clones
  tmux session org-<run>     the runner's live feed
```

## The dashboard

`org dash` prints the URL and opens it. The page is served by the manager on the host's
`127.0.0.1` only. The part of the URL after `#` is an access token: the page can approve specs
and stop runs, so every request must carry it, and requests not addressed to this machine are
refused. `ORG_PORT=9000 org up` moves it.

## Without containers

The same package runs straight on a machine that has Python 3.9 or later and Claude Code:
`ORG_LOCAL=1 org ...`, or simply `org ...` when no manager container exists. The ledger is then
`~/.org` on that machine, and engineers can run on other kinds of machine:

| `machine:` | Provisioned by | Reached by |
|---|---|---|
| `orb` | `orb clone base <name>`: a fresh OrbStack machine per run | `orb -m <name>` |
| `ssh:<name>` | exists already; listed under `machines.ssh` in config | `ssh` |
| `local` | nothing; off unless enabled, and belongs inside a box | a subprocess |

This is how it was first built, with the manager on the Mac. It needs more on the host and the
`orb` path has only been run against a simulation, so the containers are the recommended way.

## Tests

```sh
python3 -m unittest discover -s tests       # no tokens spent: a scripted stand-in for the claude
                                            # CLI and simulated docker, orb and gh
python3 tests/demo_ledger.py /tmp/demo && ORG_LOCAL=1 org --home /tmp/demo dash   # the page, with made-up runs

# with real Claude Code; inside the manager these are /opt/org/tests/...
python3 tests/live_manager_rules.py         # the manager's confinement (a few cents)
python3 tests/live_manager_refine.py        # a two-turn refinement of a request with a false premise
python3 tests/e2e.py --machine docker       # a real engineer in a real container (well under $1)
```

`tests/e2e.py` approves a small spec, dispatches it, follows it to the end, and then repeats the
acceptance checks itself in a fresh clone on the engineer's machine instead of taking the
engineer's word. After `org up`, `docker exec org-manager python3 /opt/org/tests/e2e.py --machine docker`
is the whole system in one command.

## What has and has not been exercised

Built and tested inside one Linux OrbStack machine, with a Docker daemon running in it.

- **Exercised for real:** `org up` against that daemon; the manager in its container starting
  engineers in sibling containers through the socket; real engineers finishing the smoke spec in
  containers, locally and over ssh; the manager's confinement inside its container with the shell
  sandbox live, in the default and auto permission modes; the manager's refinement and question
  mode; the dashboard in a real browser; the suite under Python 3.9 and 3.14.
- **Simulated only:** OrbStack machines (`orb clone`), and a remote Docker host over `ssh://`
  (the same client flag was exercised with a local address).
- **Not exercised:** anything on macOS itself. There the manager is the same Linux container, so
  what differs is Docker's socket and port publishing, which `org up` and `org doctor` report on.

First thing to run on the Mac: `org up`, then the one-command check above.

## Limits worth knowing

- **The manager holds the Docker socket**, which is root on the Docker host. Its container is
  not what confines it; the sandbox and rules are. It is started without Docker's syscall filter
  and path masking, because the sandbox needs both to start.
- **Engineers carry your GitHub login and Claude token.** A run can do whatever those allow. The
  default delivery is a pull request, not a merge; a GitHub token scoped to the repositories in
  play narrows the rest.
- **Rules cannot see GitHub.** The manager's shell is kept off the filesystem by the sandbox, but
  changes over the network are stopped by deny rules for the commands that make them. Those stop
  an overeager manager, not a determined one.
- **An engineer cannot run Docker** in its container. A project whose tests need containers needs
  a nested engine, or an OrbStack machine (`machine: orb`, without containers).
- **One engineer per spec.** Width comes from the engineer's subagents and from running several
  specs at once.
- **All engineers and you share one weekly limit.** A run that hits it waits for the reset and
  resumes on its own.
