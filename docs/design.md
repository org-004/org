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
| `backends/` | manager | one kind of machine each: how to pick, create, reach and remove it (`orb`, `ssh`, `local`; `docker` as a plugin) |
| `transport.py` | manager | run a script on a machine: OrbStack, ssh, local, Docker |
| `agent.py` | both ends | the host agent on the Mac, and the bridge and client in the manager's machine |
| `sync.py` | manager | pull snapshots into `runs/<run>/mirror/` |
| `status.py` | manager | one document for the terminal, the page and the manager |
| `budget.py` | manager | weekly-limit readings, the learned dollars-per-percent rate, estimate history |
| `manager.py` | manager | launch the manager chat and the read-only question mode |
| `login.py`, `doctor.py` | manager | the logins engineers are handed; what is missing |
| `dash.py`, `web/` | manager | the status page and its API |
| `runner.py` | engineer | the three phases, recovery, caps, status |
| `stream.py` | engineer | read the CLI's event stream: spend, activity, limit state |
| `schemas.py`, `prompts/` | engineer | what each turn must return, and the engineer's instructions |
| `bin/org`, `orbstack/` | host | the wrapper, `org up` and `org down`, the template's setup |
| `docker/` | host | the Docker plugin's image and its `org up` |

The engineer's side is the same package: dispatch copies it into the run directory, so a run
keeps the code it started with even if the manager's copy is upgraded mid-run.

## Where everything runs

The manager and every engineer are OrbStack machines on the person's Mac. On the Mac itself
there is OrbStack, a shell wrapper, and one background process.

```
 the Mac: OrbStack + bin/org (forwards every command into the manager) + the host agent
 ┌────────────────────────────────────────────────────────────────────────┐
 │ org-base        the template, stopped: python3, git, gh, tmux, Docker, │
 │                 Claude Code, the logins. Only ever cloned.             │
 │                                                                        │
 │ org-manager     a clone of it, with org installed                      │
 │   holds         the ledger · the logins · agent.sock                   │
 │   serves        the dashboard, on its loopback (→ the Mac's localhost) │
 │        │ create · run a script in · delete      (through the agent)    │
 │        ▼                                                               │
 │ org-eng-<run>   a clone of the template, one per run                   │
 │   given         the Claude token over stdin; the rest it inherited     │
 │   has           sudo, systemd, a Docker daemon of its own              │
 └────────────────────────────────────────────────────────────────────────┘
```

**Why machines.** An engineer needs a whole system: to install packages, run services, and build
and run containers, because that is what real projects' tests need. A container gives the first
two and not the third without a nested engine. An OrbStack machine is a Linux system with
systemd and its own Docker daemon, and an isolated one has none of the Mac's files, cannot run
commands on the Mac, and cannot reach it over the network. Cloning one is copy-on-write and takes
a few hundredths of a second.

**The template.** `org-base` holds everything an engineer starts with, so starting a run is a
clone and nothing is installed per run. `org up` creates it (`orb create --isolated`) or clones a
machine the person already has (`--from`), then runs `orbstack/bootstrap.sh` inside it, which
installs only what is missing. It is left stopped: a clone of a stopped machine is a clean copy,
and a template that never runs cannot drift.

**Why the manager is a machine too.** It keeps Python and Claude Code off the Mac, and it puts
the manager chat one more wall away from the person's files: the chat is confined by its sandbox
and rules, and behind those is a machine that has nothing of the Mac's in it.

**The host agent, and why it is shaped this way.** An isolated machine cannot run `orb`, and
cannot reach the Mac at all, so nothing can listen on the Mac for the manager. The Mac can always
reach a machine. So the agent runs on the Mac and dials in: it keeps one command open inside the
manager's machine, the bridge, and requests travel up that command's stdout and answers down its
stdin, a JSON line each.

```
 the Mac                                         the manager's machine
 ┌──────────────────────────┐  orb -m org-manager … org agent bridge
 │ org agent run            │─────────────────────────▶┌───────────────────────────────┐
 │                          │◀── requests, a line each │ bridge                        │
 │   orb clone org-base …   │─── answers ─────────────▶│   ~/.org/agent.sock (0600)    │
 │   orb -m org-eng-… …     │                          │   the dashboard, on 127.0.0.1 │
 │   orb delete org-eng-…   │                          └───────────────────────────────┘
 └──────────────────────────┘                             ▲ org dispatch, sync, status
```

- It does five things: `info`, `list`, `create` (clone the template, start the clone), `exec`
  (run a bash script in a machine, with stdin) and `delete`.
- It acts only on machines whose names start with its prefix (`org-eng-`), and refuses a
  configuration in which the manager or the template would match that prefix. It cannot touch
  the template, the manager, or any other machine the person has, and no request can name a
  command to run on the Mac. That one check is the whole of its safety, so it is the first thing
  `handle()` does and it has a test that throws a dozen bad names at it.
- In the manager's machine the socket is the user's alone (mode 0600), and the bridge refuses a
  caller that is another user or has no process in this machine.
- The agent pings every 15 seconds and the bridge exits after 75 seconds of silence. This is
  needed: when the `orb` client on the Mac is killed outright, nothing reaches the command inside
  the machine, neither a signal nor end of file. (Terminated politely, the command gets SIGTERM;
  when its stdin is closed, it gets end of file.) When the link ends for any reason the agent
  opens it again, backing off to 30 seconds.
- The orb backend uses the agent when `machines.orb.via` is `agent`, which `org up` sets, and the
  `orb` command itself when the manager runs on the Mac (`cli`, the default).

**Nothing on the Mac starts by itself.** `org up` starts the agent with `nohup` and records its
pid. There is no launchd unit: after a restart `org up` brings it back, and takes two seconds
when the machines exist. Engineers do not depend on the agent while they work, only the manager's
view of them does.

**The dashboard's port.** The bridge serves the page on the manager machine's loopback. OrbStack
forwards a machine's listening ports to the Mac's localhost, for isolated machines too, so the
page is at `127.0.0.1:8787` on the Mac. Other machines cannot reach a loopback port. On
connecting, the agent checks that the page answers on the Mac; where it does not, the agent
listens on the port itself and carries each connection through another `orb -m` command, as
base64 lines.

**The pool.** `machines.orb.slots` is how many engineers' machines may run at once. A run that
finds no free slot is recorded as queued, and `pump()` starts it when a slot frees. `pump()` runs
whenever something reads the ledger (status, the dashboard's loop), so the queue moves without a
scheduler process of its own. Placement is decided under a lock, so two readers cannot hand out
one slot twice. Slots are counted from the ledger, not from `orb list`, so a finished run whose
machine still exists does not hold a slot.

**Logins.** An engineer acts as the person: it needs the long-lived Claude token, a GitHub login
and a git identity. `org up` writes all three into the template over stdin, so every clone has
them, and dispatch writes the manager's current Claude token into each new machine, so a renewed
token does not need a new template. Nothing is passed as an environment variable or on a command
line.

**Before an engineer starts**, dispatch asks the new machine what it has: python3, git and
Claude Code, or the run is refused; whether a Docker daemon answers, which the engineer is told;
and whether it can see the Mac's files, in which case the run is refused, because a machine that
is not isolated is no box for a session with every permission bypassed.

**Backends.** Where engineers run is one interface (`org/backends/`): choose a machine for a run,
say how many may run at once, create it, reach it, remove it.

| Kind | Provisioned by | Reached by |
|---|---|---|
| `orb` | a clone of the template, through the agent or the `orb` command | `orb -m <name> bash -c …` |
| `ssh:<name>` | exists already | `ssh <host> bash -c …` |
| `local` | nothing; off by default | `bash -c …` |
| `docker[:node]` (plugin) | `docker run` on a pool node | `docker exec` |

A transport does one thing: run a bash script on a machine with optional bytes on stdin. Files
travel as base64 on stdin, so nothing depends on scp or a binary-clean channel.

**The Docker plugin.** The first container version made the manager and every engineer Docker
containers from one image, with the manager holding the Docker socket. It is still here
(`org up --backend docker`, `org/backends/docker.py`, `docker/`), loaded only when config names
it, for hosts without OrbStack. It was set aside as the default for one reason: an engineer in a
container cannot run Docker. It also makes the manager root on the Docker host, where the host
agent gives it three operations on machines with one prefix.

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
requests whose Host or Origin is not this machine are refused. In the manager's machine it
listens on the loopback only, which is what reaches the Mac's localhost and nothing else.

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

**What the chat must not reach inside the manager's machine** is the host agent's socket: whoever
can write to it can run anything on any engineer's machine. Sandboxed shell commands get no Unix
sockets, so the chat cannot open it, and `org exec`, which can, always asks the person. The same
holds for the machine's own Docker daemon. Both are in the live check.

**The sandbox is the mechanism, so it is required when it exists.** `org talk` probes whether
the sandbox can start. If it can, the session is told to refuse to run without it. If it cannot,
the session is started in the ask-first permission mode and says so. The reason is a live
finding: with no sandbox and Claude Code in its auto mode, where a classifier answers most
permission prompts, the classifier allowed a `git branch` in a repository outside the desk. A
guarantee that depends on a classifier's judgment is not a guarantee, so without a sandbox the
prompt goes to the person.

**Inside an OrbStack machine the sandbox needs nothing special**: bubblewrap can make its
namespaces there. **Inside a container it needs two Docker options** on the manager:
`seccomp=unconfined`, or bubblewrap cannot create its namespaces, and `systempaths=unconfined`,
or it cannot mount its own `/proc`. Claude Code offers a weaker nested mode that avoids the
second, at the price of showing sandboxed commands the container's other processes; the full
sandbox was chosen. Both options weaken the manager container's own wall, which is not a
boundary anyway for something that holds the Docker socket.

**What the live check taught**, beyond the above (`tests/live_manager_rules.py`, thirteen outcomes):

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
| manager chat | read access to the manager's machine; the desk | draft; propose approve, dispatch, stop, exec |
| `org` program in the manager | the ledger; the agent's socket; the logins | whatever the person or the page asks of it |
| host agent, on the Mac | the `orb` command | clone the template into `org-eng-…`; run a script in, and delete, machines so named |
| engineer | its machine; the logins placed there | anything those logins allow, from its own machine |
| dashboard page | the access token | approve, stop, ask |

- An engineer runs with permissions bypassed, so its machine is the boundary. An isolated machine
  has none of the Mac's files and cannot run commands on the Mac or reach it over the network.
  Outside the machine, an engineer can do exactly what its GitHub login and Claude token allow: a
  token scoped to the repositories in play narrows that. The default delivery is a pull request,
  not a merge.
- The template is the root of what engineers hold. Everything in `org-base` is in every
  engineer's machine, so a template cloned from a machine the person works in (`--from`) carries
  that machine's files and logins to every engineer.
- The manager's machine holds no power over the Mac. What it can ask of the agent is bounded by
  the prefix; the worst a compromised `org` program can do there is create, enter and delete
  engineers' machines, which is its job.
- Machines share a network. An engineer can reach the internet and any port another machine opens
  on its network address. The manager opens none: the dashboard is on its loopback and the agent's
  link is a Unix socket. OrbStack's `--isolate-network` option did not stop one machine reaching
  another when tried (OrbStack 2.2.5), so it is not relied on.
- OrbStack forwards every machine's listening ports to the Mac's localhost. A server an engineer
  starts is reachable there. If one listened on the dashboard's port while the manager was
  stopped, a bookmark of the dashboard would open the engineer's page, and that page could read
  the token from the URL. With both listening, the manager's page was the one shown, in either
  order of starting (two trials; OrbStack does not document a rule). Nothing in org checks whose
  page it is opening.

## What has been checked

| Claim | How |
|---|---|
| runner: phases, recovery, caps, stop, resume, replanning, the run lock | unit tests against a scripted stand-in for the CLI |
| the agent: the prefix check, exec with input and exit status, concurrent requests, reconnecting, a second bridge, slots and the queue, refusing a machine that sees the Mac | unit tests with the real agent and bridge against a stand-in `orb` |
| `org up` and `org down`: template, manager, logins, the agent's lifecycle, a second `up`, `--from`, and a spec from draft to finished run through the wrapper | `tests/test_up.py`, the real scripts against the stand-in `orb` |
| `org up` on a Mac | real OrbStack 2.2.5: from nothing in under a minute, again in two seconds, after `org down`, and a second installation beside the first with `--from` and other names |
| a real engineer plans, builds, pushes and reports in a machine created through the agent | `tests/e2e.py` inside the manager, on a ledger of its own and on the manager's; the script repeats the acceptance checks itself; the machine is deleted through the agent afterwards |
| the same with the manager on the Mac and no agent | `ORG_LOCAL=1 python3 tests/e2e.py --machine orb --base org-base` |
| the manager's confinement | `tests/live_manager_rules.py` inside the manager's machine with the sandbox live, in the default and auto modes: thirteen outcomes, among them that the agent's socket and the Docker daemon are out of the chat's reach |
| the manager chat starts in its machine with no first-run screens, and answers from `org status` | by hand, in a terminal |
| Docker in an engineer's machine | a pull and a run inside an isolated machine and inside a clone of it; the daemon answers on the first try after the clone starts |
| what an isolated machine cannot do | no `/mnt/mac`, no `/mnt/machines`, `mac` refused, no connection to a listener on the Mac |
| the pipe the agent depends on | line by line through `orb -m` with a 20 second silence and a 200 kB line; what the far end sees when the client is terminated, killed, or closes its stdin |
| the dashboard's port | answers on the Mac's localhost through OrbStack's forwarding; the agent's own relay returns the identical page; another machine cannot reach it |
| logins reach an engineer over stdin and never on a command line | unit test over every recorded client call |
| the manager's refinement | `tests/live_manager_refine.py`: a request with a false premise; it read the code and said so (run in the container version) |
| the page | a real browser, light, dark and phone width; no console errors (in the container version) |
| Python 3.9 and 3.13 | the whole suite under 3.9 on macOS and 3.13 on Linux |
| the Docker plugin | as the container version was: `org up` against a real daemon on Linux, real engineers in containers locally and over ssh. Not run again since it became a plugin, beyond its unit tests |

Not checked: the agent's link held open for hours (it has run for minutes); a restart of the Mac;
`org up --from` a machine that is not isolated, where the script isolates the clone; a template on
a distribution other than Debian (creating an isolated Arch machine failed in OrbStack on the day
this was tested, on an Arch package conflict, which is why Debian is the default); ssh machines
alongside OrbStack ones.

## Alternatives considered

- **Docker containers for everything.** Built first and kept as a plugin. Docker's daemon is a
  ready-made agent with an access model, and the host needs only an engine. It lost as the
  default because an engineer in a container cannot run Docker, and because holding the socket
  makes the manager root on the Docker host.
- **The agent listening on the Mac.** Simpler than dialling in, but an isolated machine cannot
  reach the Mac, and a machine that could would no longer be isolated. Something listening on the
  Mac would also be reachable by every other machine.
- **A launchd unit for the agent.** It would survive a restart by itself. It is also a thing
  installed on the Mac that outlives `org down` unless removed with care, and `org up` already
  takes two seconds.
- **Installing per run instead of cloning a template.** No template to keep, but minutes per run
  and a dependency on package mirrors at dispatch time.
- **A shared mount instead of running a command per sync.** Mounting every run directory into the
  manager would make sync a file read. Isolated machines share no files, which is the point of
  them, and a command works for every kind of machine, so there is one mechanism.
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
- Nothing restarts the host agent after the Mac restarts; `org up` does.
- Machines can reach each other over the network, and their ports reach the Mac's localhost.
- A finished run's machine stays until `org machine rm`; nothing reclaims the disk by itself.
- One engineer per spec. A spec too large for one engineer is the manager's to split.
- No notification when a run finishes; the page and `org status` have to be looked at.
- The template is built once and is not rebuilt when `orbstack/bootstrap.sh` changes; a new tool
  reaches existing installations by `orb -m org-base`, or by `org down --purge` and `org up`.
