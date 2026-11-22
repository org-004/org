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
7. **Guarantees rest on mechanisms that were made to fail.** Each claim about what the manager
   cannot do is backed by a test that tries to do it against the real harness.

## Components

```
            the person
                │  talk / approve / watch
   ┌────────────▼─────────────────────────────────────────────┐
   │ manager                                                  │
   │   org talk     Claude Code + persona, confined to a desk │
   │   org dash     status page, chat box, sync loop          │
   │   ledger       drafts · approved specs · runs · budget   │
   │   dispatch     place ▸ provision ▸ bundle ▸ start        │
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
| `dispatch.py` | manager | choose and place a machine, logins, bundle, start, stop, resume, the queue |
| `transport.py` | manager | run a script on a machine: Docker, OrbStack, ssh, local |
| `sync.py` | manager | pull snapshots into `runs/<run>/mirror/` |
| `status.py` | manager | one document for the terminal, the page and the manager |
| `budget.py` | manager | weekly-limit readings, the learned dollars-per-percent rate, estimate history |
| `manager.py` | manager | launch the manager chat and the read-only question mode |
| `login.py`, `doctor.py` | manager | the logins engineers are handed; what is missing |
| `dash.py`, `web/` | manager | the status page and its API |
| `runner.py` | engineer | the three phases, recovery, caps, status |
| `stream.py` | engineer | read the CLI's event stream: spend, activity, limit state |
| `schemas.py`, `prompts/` | engineer | what each turn must return, and the engineer's instructions |
| `bin/org`, `docker/` | host | the wrapper, the image, `org up` |

The engineer's side is the same package: dispatch copies it into the run directory, so a run
keeps the code it started with even if the manager's copy is upgraded mid-run.

## Where everything runs

The manager and every engineer are Docker containers from one image. On the host there is a
Docker engine and a shell wrapper.

```
 host: Docker engine + bin/org (forwards every command into the manager)
 ┌──────────────────────────────────────────────────────────────────┐
 │ org-manager     main process: org dash (8787 → host localhost)   │
 │   volumes       ledger · Claude Code state · GitHub login · ssh  │
 │   socket        /var/run/docker.sock                             │
 │        │ docker run / exec / rm                                  │
 │        ▼                                                         │
 │ org-eng-<run>   sleep infinity; the runner is exec'd into it     │
 │   network       org-engineers: no container-to-container traffic │
 │   given         the three logins over stdin; no mounts, no env   │
 └──────────────────────────────────────────────────────────────────┘
```

**Why Docker, and why the manager is a container too.** The first version put the manager on
the Mac and engineers in OrbStack machines. That needs Python, Claude Code and the `orb` command
on the Mac. Moving the manager into an OrbStack machine does not work cleanly: an isolated
machine can reach other machines over the network, but it cannot create or delete them, because
`orb` does not work from inside one. Something on the Mac would have to do that on its behalf, a
kubelet of our own, reachable from every box.

Docker already has that agent and an access model for it. The daemon creates and removes
containers, and whoever holds its socket may ask. So the manager is a container holding the
socket, engineers are containers that do not, and the host needs nothing but the engine.
OrbStack ships one, as do Docker Desktop and Colima.

**One image, two roles.** The difference is what a container is given. The manager gets the
socket, the volumes and the logins. An engineer gets its own network and sudo.

**The engineer's container idles and the runner is exec'd into it**, so a container is driven
exactly like any other machine (`docker exec` is the transport), stays inspectable after the run,
and can be re-entered by `org resume`.

**The pool.** A node is a Docker host with a number of slots: the local daemon, or another one
addressed as `ssh://user@host`. A run takes the least loaded node with a free slot. Otherwise it
is recorded as queued, and `pump()` starts it when a slot frees. `pump()` runs whenever something
reads the ledger (status, the dashboard's loop), so the queue moves without a scheduler process
of its own. Placement is decided under a lock, so two readers cannot hand out one slot twice.
Slots are counted from the ledger, not from `docker ps`, so a finished run that still has a
container does not hold a slot.

**Logins.** An engineer acts as the person: it needs the long-lived Claude token, a GitHub login
and a git identity. All three are written into the container over stdin after it starts. Nothing
is passed as an environment variable or on a command line, where `docker inspect` or a process
list would show it.

**The other machines** still work when the package is run directly on a machine instead of
through the manager container:

| Kind | Provisioned by | Reached by |
|---|---|---|
| `docker[:node]` | `docker run` on a pool node | `docker exec` |
| `orb` | `orb clone <base> <name>` | `orb -m <name> bash -c …` |
| `ssh:<name>` | exists already | `ssh <host> bash -c …` |
| `local` | nothing; off by default | `bash -c …` |

A transport does one thing: run a bash script on a machine with optional bytes on stdin. Files
travel as base64 on stdin, so nothing depends on scp or a binary-clean channel.

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
Checked against the real harness, in the default and auto permission modes: with nobody to
answer, the command is refused.

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

The page is one self-contained HTML file. It can approve a spec and stop a run, so every API
request needs a token (in the URL after `#`, where it is not sent to servers or logged) and
requests whose Host or Origin is not this machine are refused. In the manager container it
listens on all interfaces and is published on the host's `127.0.0.1` only.

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

| Layer | Stops | Enforced by |
|---|---|---|
| shell sandbox | shell writes outside the desk; shell reads of the login files | the operating system (bubblewrap, or Seatbelt on macOS) |
| `blockWritesOutsideWorkingDirectories` | the file tools writing outside the desk | the harness |
| deny rules | pushes and other commands that change GitHub; reading the login files | the harness |
| ask rules | approve, dispatch, stop, exec | the harness, by asking the person, in every permission mode |

`org` and `gh` run outside the sandbox (`org` must write the ledger and reach machines; `gh`
needs its token) and are governed by rules instead.

**The sandbox is the mechanism, so it is required when it exists.** `org talk` probes whether
the sandbox can start. If it can, the session is told to refuse to run without it. If it cannot,
the session is started in the ask-first permission mode and says so. The reason is a live
finding: with no sandbox and Claude Code in its auto mode, where a classifier answers most
permission prompts, the classifier allowed a `git branch` in a repository outside the desk. A
guarantee that depends on a classifier's judgment is not a guarantee, so without a sandbox the
prompt goes to the person.

**Inside a container the sandbox needs two Docker options** on the manager:
`seccomp=unconfined`, or bubblewrap cannot create its namespaces, and `systempaths=unconfined`,
or it cannot mount its own `/proc`. Claude Code offers a weaker nested mode that avoids the
second, at the price of showing sandboxed commands the container's other processes; the full
sandbox was chosen. Both options weaken the manager container's own wall, which is not a
boundary anyway for something that holds the Docker socket.

**What the live check taught**, beyond the above (`tests/live_manager_rules.py`, eleven outcomes):

- Read-only shell commands are auto-allowed only inside the working directory, and a bare `Read`
  rule does not satisfy the path check on `ls` and `cat`: the rules carry `Read(//**)` and an
  explicit list of read-only commands.
- An allow rule with a wildcard before the git verb (`git -C * log *`) also admits `-c` options,
  which can run programs. Those rules were removed; `cd` and the plain form replace them.
- A push written as `git -C <dir> push` does not match a deny rule for `git push`: both forms
  are denied.

## Trust

| Who | Holds | Can |
|---|---|---|
| the person | everything | everything |
| manager chat | read access to its container; the desk | draft; propose approve, dispatch, stop, exec |
| `org` CLI in the manager | the ledger; the Docker socket; the logins | whatever the person or the page asks of it |
| engineer | its container; the logins placed there | anything those logins allow, from its own container |
| dashboard page | the access token | approve, stop, ask |

- An engineer runs with permissions bypassed, so its container is the boundary. Outside it, an
  engineer can do exactly what its GitHub login and Claude token allow: a token scoped to the
  repositories in play narrows that. The default delivery is a pull request, not a merge.
- The manager container holds the Docker socket, which is root on the Docker host, and on a Mac
  the Docker host can mount the Mac's files. The manager chat cannot reach the socket (sandboxed
  shell commands get no Unix sockets, and `docker` is not an allowed command), but the `org`
  program can, and it is the trusted part.
- Engineers share a bridge network with inter-container traffic off, and none with the manager.

## What has been checked

| Claim | How |
|---|---|
| runner: phases, recovery, caps, stop, resume, replanning, the run lock | unit tests against a scripted stand-in for the CLI |
| a real engineer plans, builds, pushes and reports | `tests/e2e.py` in a real container, locally, and over ssh; the script repeats the acceptance checks itself |
| `org up`; the manager in its container starts engineers in sibling containers | run against a real Docker daemon, then `e2e.py` from inside the manager |
| pool placement, the queue, a second node, limits per node | unit tests against a simulated client; a second node with a real engineer on it |
| logins reach an engineer over stdin and never on a command line | unit test over every recorded client call |
| the manager's confinement | `tests/live_manager_rules.py`: rules alone on a machine without the sandbox, and with the sandbox live inside the manager container, in the default and auto modes |
| the manager's refinement | `tests/live_manager_refine.py`: a request with a false premise; it read the code and said so |
| the page | a real browser, light, dark and phone width; no console errors |
| Python 3.9 and 3.14 | the whole suite under both |

Not checked: anything on macOS itself (there the manager is the same Linux container, so what
differs is the engine's socket path and port publishing); a remote Docker host over `ssh://`
(the same client flag was exercised with a local address); a real OrbStack clone.

## Alternatives considered

- **A host agent for OrbStack machines.** A small daemon on the Mac that creates machines for a
  boxed manager. It would be reachable from every box, and it would be ours to secure. Docker's
  daemon is that agent, already built.
- **A shared volume instead of `docker exec`.** Mounting every run directory into the manager
  would make sync a file read and let the manager browse engineers' files natively. It works only
  for the local node, and `docker exec` works for all of them, so there is one mechanism.
- **A socket proxy** that lets the manager call only some of the Docker API. It narrows what a
  compromised `org` could do, at the cost of another moving part. Not built.
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
- The sandbox keeps the manager's shell off the filesystem, but changes made over the network
  are stopped only by deny rules for the commands that make them. A read-only GitHub token for
  the manager, separate from the engineers', would close that; today both use one login.
- An engineer cannot run Docker inside its container.
- One engineer per spec. A spec too large for one engineer is the manager's to split.
- No notification when a run finishes; the page and `org status` have to be looked at.
- The image is built on each pool node from this checkout; there is no registry.
