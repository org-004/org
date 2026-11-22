# org: design

What the pieces are, why they are shaped this way, and what has been checked. The README says
how to use it; this says why it is built the way it is.

## The problem

One person wants several pieces of software work done at once without doing them, and without
losing track of what each costs. That takes two roles with opposite temperaments:

- A **manager** that talks to the person, is skeptical of the request, and turns it into a spec
  exact enough to hand to someone who cannot ask questions. Afterwards it is the person's window
  onto the work.
- **Engineers** that never talk to the person. Each takes one approved spec, plans it, does it on
  a machine of its own, and reports.

Both are Claude Code sessions. Everything else in this repository is the deterministic machinery
between them.

## Principles

These are the decisions the rest follows from.

1. **The manager's side initiates everything.** It pushes the spec out and pulls status back.
   An engineer's machine never needs a route to the manager, so machines can be isolated, and
   reading a run cannot disturb it.
2. **A deterministic runner surrounds a non-deterministic engineer.** The engineer decides how to
   do the work. Whether a phase is over, what it cost, whether a cap was hit and what happens next
   are decided by a program, from structured output, never from prose.
3. **State is files.** One JSON file per fact, written atomically. No database, no daemon that
   must be up. Anything can be inspected with `cat`, and a crashed process loses nothing.
4. **Approval is a human act on an exact text.** The spec's sha256 is in the approval command, and
   the command is one the manager is not allowed to run without the person confirming it.
5. **Numbers are measured, not recalled.** Cost comes from the CLI's own accounting. The engineer
   is handed its measured time and cost before it writes its report, and the next engineer is
   handed how past estimates turned out.
6. **Every run ends with a status and a reason.** If the engineer cannot produce them the runner
   does. `done` is lowered to `partial` unless every acceptance criterion is reported passing.

## Components

```
            the person
                │  talk / approve / watch
   ┌────────────▼─────────────────────────────────────────────┐
   │ manager side                                             │
   │   org talk     Claude Code + persona, confined to a desk │
   │   org dash     status page, chat box, sync loop          │
   │   ledger       drafts · approved specs · runs · budget   │
   │   dispatch     bundle ▸ provision ▸ start                │
   │   sync         one read-only snapshot per run            │
   └────────────┬─────────────────────────────▲───────────────┘
        bundle  │ over a transport            │ snapshot (JSON)
   ┌────────────▼─────────────────────────────┴───────────────┐
   │ an engineer's machine (one per run)                      │
   │   runner       plan ▸ milestone… ▸ report                │
   │     └─ claude -p   one resumed session, bypass mode      │
   │   run dir      spec · job · status · plan · report · log │
   │   workspace    the engineer's clones, caches, worktrees  │
   └──────────────────────────────────────────────────────────┘
```

| Module | Side | Job |
|---|---|---|
| `ledger.py` | manager | paths, ids, config |
| `spec.py` | manager | draft template, checks, approval |
| `dispatch.py` | manager | choose a machine, build the bundle, provision, start, stop, resume |
| `transport.py` | manager | run a script on a machine: OrbStack, ssh, local |
| `sync.py` | manager | pull snapshots into `runs/<run>/mirror/` |
| `status.py` | manager | one document for the terminal, the page and the manager |
| `budget.py` | manager | weekly-limit readings, the learned dollars-per-percent rate, estimate history |
| `manager.py` | manager | launch the manager chat and the read-only question mode |
| `dash.py`, `web/` | manager | the status page and its API |
| `runner.py` | engineer | the three phases, recovery, caps, status |
| `stream.py` | engineer | read the CLI's event stream: spend, activity, limit state |
| `schemas.py`, `prompts/` | engineer | what each turn must return, and the engineer's instructions |

The engineer's side is the same package: dispatch copies it into the run directory, so a run
keeps the code it started with even if the manager's copy is upgraded mid-run.

## The spec

A spec is one Markdown file: a flat `key: value` header for the dispatcher (where to run, caps,
delivery) and sections for the engineer (Intent, Context, Requirements, Out of scope, Acceptance
criteria, Constraints, Alternatives considered, Open questions).

`org spec check` enforces what a program can: ids on requirements and acceptance criteria, at
least one MUST, no open questions, no template text left, no placeholder or unreachable
repositories, well-formed caps. The rest is the manager's job, and its persona is written around
one fact: an engineer cannot ask anyone anything, so every ambiguity left in a spec becomes a
guess.

**Why one file.** The person approves exactly what the engineer reads. A separate metadata file
would be a second thing to review and a place for the two to disagree.

**Why a hash.** The manager edits the draft during the conversation. Without the hash, "approve"
means "approve whatever the file says by the time the command runs". With it, a draft that
changed after the person read it cannot be approved by accident.

**Why approval is a permission prompt.** The manager session runs under a rule that makes
`org approve` always ask. The model can propose the command; only the person can let it run.
Checked against the real harness: with nobody to answer, the command is refused.

## The engineer's run

One Claude Code session, resumed across turns, each turn ended by structured output validated
against a schema (`--json-schema`).

| Phase | Turn returns | Runner then |
|---|---|---|
| plan | verdict, milestones, estimate (time, cost, 90th percentile) | checks it against the caps, sends it back once if it does not fit, freezes `plan.md` |
| execute | per milestone: done, partial or blocked; optionally the remaining milestones replanned | records real time and cost, re-forecasts, goes on or stops |
| report | status, reason, a verdict per acceptance criterion, where the work landed | lowers `done` if a criterion is not passing, writes `result.json` and `actuals.json` |

**Why milestones are turns.** A turn boundary is the only moment the CLI reports authoritative
cost, and the only safe place to enforce a cap or change the plan. One turn per milestone gives
the dashboard real progress and gives the review a per-milestone estimate against actual. Width
lives inside a milestone, where the engineer fans out to subagents.

**Why the plan is frozen.** The plan is how the engineer's judgment is assessed later. If it could
be edited after the fact it would always look right. Amendments are allowed and recorded; the
original is kept read-only beside them.

**What the runner handles so the engineer does not have to.**

| Event | Response |
|---|---|
| turn ends without structured output (prose, a question, a crash) | resume the session with a nudge that restates the instruction; three tries |
| the account's usage limit is hit | wait until the reset the CLI reported, then resume; not counted as a retry |
| the session cannot be resumed | start a new one and tell it to read its plan and the repositories first |
| the process is silent for 90 minutes | kill it and resume |
| cost cap reached | stop executing; the report gets a reserved allowance so it can still be written |
| time cap reached | end the turn in flight, then report |
| the runner itself dies | `org resume` restarts it; it continues from the last recorded step, and a lock stops two runners sharing a run |
| the engineer changes `spec.md` | the approved text is restored and the change is recorded as a warning |

**Cost accounting.** The CLI's `total_cost_usd` is cumulative over a session, across resumed
invocations; the per-turn figure is `model_usage_since_last_result`. The meter uses the per-turn
figure and falls back to differencing the cumulative one. Inside a turn, spend is estimated from
streamed token counts with a price table and corrected at the turn's end.

## Status, sync and the page

The runner rewrites `status.json` every few seconds. Sync is one read-only command per run,
`org engineer snapshot`, which returns status, plan, report and the next slice of the event log
as one JSON document. The manager's side writes that into `runs/<run>/mirror/`.

`status.collect()` builds one document from the ledger and the mirrors. The terminal table, the
page and the manager's question mode all render that document, so they cannot disagree.

The page is one self-contained HTML file served on `127.0.0.1`. It can approve a spec and stop a
run, so every API request needs a token (in the URL after `#`, where it is not sent to servers or
logged) and requests whose Host or Origin is not this machine are refused.

**Visual encoding.** Milestone progress is drawn in one blue ramp (not started, running, done)
so it reads without hue; the palette validator caught that green "done" beside red "blocked" is
lost to red-green colour blindness. Status colours always travel with an icon shape and a label.

## The weekly budget

Two exact numbers and one learned one.

- **Utilization** of the account's weekly limit: reported by the CLI at the start of every
  engineer turn. Account-wide, shared with everything else the account does.
- **Dollars** a run cost at list prices: from the CLI.
- **Dollars per percent** is not published. Each finished run saw utilization move by some amount
  while it spent a known number of dollars. Other activity on the account only inflates the
  movement, so each run gives a lower bound and the largest bound seen is the estimate. Until a
  run moves the meter by two points, a run's share is shown as unknown rather than guessed.

## Confining the manager

The manager must be able to read everything and run anything, and change nothing that matters.
Three layers, each for what it can see:

| Layer | Stops |
|---|---|
| Claude Code's Bash sandbox | shell writes outside the desk |
| `permissions.blockWritesOutsideWorkingDirectories` | the file tools writing outside the desk |
| deny and ask rules | commands that change remote state; approve, dispatch and stop without the person |

`org` itself runs outside the sandbox, because it must write the ledger and reach machines, and
is governed by rules instead: read commands allowed, human decisions always asked.

`tests/live_manager_rules.py` attempts ten actions under these settings against the real harness
and judges by outcome. It found two things reading the documentation had not: read-only shell
commands are auto-allowed only inside the working directory, and a bare `Read` rule does not
satisfy the path check on `ls` and `cat`, so the rules now carry `Read(//**)` and an explicit
list of read-only commands.

## Machines

A transport does one thing: run a bash script on a machine with optional bytes on stdin. Files
travel as base64 on stdin, so nothing depends on scp or a binary-clean channel.

| Kind | Provisioned by | Reached by |
|---|---|---|
| `orb` | `orb clone <base> <name>` | `orb -m <name> bash -c …` |
| `ssh:<name>` | exists already | `ssh <host> bash -c …` |
| `local` | nothing; off by default | `bash -c …` |
| `docker` | planned, below | `docker exec` |

## Container mode (next)

The first version puts the manager on the Mac, which needs Python, Claude Code and the `orb`
command there. The question that followed: can the manager live in a box as well, with one setup
script on the Mac?

**With OrbStack machines, not cleanly.** An isolated machine can reach other machines over the
network, so a boxed manager could use the ssh transport. It cannot create or delete machines:
`orb` does not work from inside one. That would take an agent on the Mac that boxes can call,
and every box can reach the Mac's loopback, so that agent would be a new hole in the isolation
that has to be defended.

**With Docker, yes.** The Docker daemon already is that agent, with an access model that exists:
whoever holds the socket controls the containers.

```
 Mac: Docker engine (OrbStack provides one) + a 30-line `org` wrapper
 ┌──────────────────────────────────────────────────────────────────┐
 │ org-manager   org dash (port 8787 → Mac localhost) · org talk    │
 │   holds: docker.sock, the ledger volume, the login tokens        │
 │        │ docker run / exec / rm                                  │
 │        ▼                                                         │
 │ org-eng-<run> …  one container per run, no socket, own network   │
 └──────────────────────────────────────────────────────────────────┘
   remote capacity: another Docker host, reached as ssh://user@host
```

- **One image** for both roles: Python, git, gh, tmux, Claude Code, this package.
- **`org up`** on the Mac builds the image, creates the volumes and networks, starts the manager
  and hands it the login tokens. After that `org talk` on the Mac is `docker exec` into the
  manager. The Mac needs only a Docker engine.
- **The pool** is a list of Docker hosts with a number of slots each. A run that finds no free
  slot is queued and started when one frees.
- **The manager and engineers' files.** The manager reaches any engineer's filesystem through
  `docker exec`: read-only `org ls`, `org cat` and `org diff` for the manager chat, and
  `org exec`, which asks the person first.

**What it costs.** The socket makes the manager container root on the Docker VM, so the
container is not what confines the manager; its permission rules and sandbox are, exactly as on
the Mac. An engineer in a plain container cannot run Docker itself, so a project whose tests
need containers needs a nested engine or an OrbStack machine.

## Trust

| Who | Holds | Can |
|---|---|---|
| the person | everything | everything |
| manager chat | read access to the machine; the desk | draft; propose approve, dispatch, stop |
| `org` CLI | the ledger; machine control; the tokens | whatever the person or the page asks of it |
| engineer | its machine; the `gh` login and Claude token placed there | anything those credentials allow, on its own machine |
| dashboard page | the access token | approve, stop, ask |

An engineer runs with permissions bypassed, so its machine is the boundary. What it can do
outside that machine is exactly what its credentials allow: a narrower `gh` token narrows it.
The default delivery is a pull request, not a merge.

## What has been checked

| Claim | How |
|---|---|
| runner: phases, recovery, caps, stop, resume, replanning, the run lock | unit tests against a scripted stand-in for the CLI |
| a real engineer plans, builds, pushes and reports | `tests/e2e.py`, locally and over ssh; the script repeats the acceptance checks itself |
| the manager's confinement | `tests/live_manager_rules.py`, real harness, ten outcomes |
| the manager's refinement | `tests/live_manager_refine.py`: a request with a false premise; it read the code and said so |
| dispatch, sync, status, two engineers at once | unit tests against a simulated `orb` |
| the page | a real browser, light, dark and phone width; no console errors |
| Python 3.9 and 3.14 | the whole suite under both |

Not checked: a real OrbStack clone (the commands are the ones the `box` helper uses), the macOS
Bash sandbox, and an interactive `org talk` session beyond launching it.

## Alternatives considered

- **Claude Code's own background sessions** (`claude --bg`, `claude agents --json`). They run on
  the local machine only and report state but not cost. The headless stream is the documented
  interface and carries cost, limits and structured output.
- **The Agent SDK.** It needs a package manager on every machine. The CLI is already there.
- **Git as the bus between manager and engineers.** Durable, but a heartbeat every few seconds
  makes a poor commit history, and it needs a repository before there is a project.
- **Engineers pushing status to the manager.** Simpler to write, but it needs a route from every
  machine back to the manager, which is the isolation being given up.
- **One long engineer turn.** No cost checkpoints, no enforceable caps, no progress to show.
- **Letting the manager approve when the person says "approve" in chat.** The model would be
  judging whether it had permission. A harness prompt does not depend on that judgment.

## Known gaps

- A run's share of the weekly limit is unknown until the rate has been learned or configured.
- The manager's shell deny rules match the form a model normally writes. They stop an overeager
  manager, not a hostile one; the sandbox is the boundary.
- One engineer per spec. A spec too large for one engineer is the manager's to split.
- No notification when a run finishes; the page and `org status` have to be looked at.
