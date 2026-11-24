# org

A product manager and a team of engineers, each in a box of its own.

You talk to the manager. It turns a loose request into an exact spec, pushing back and looking at
the real system on the way, and you approve the text. Approval dispatches an engineer: an
autonomous Claude Code session, alone in its own container, that plans the work, executes it, and
reports. While engineers work, the manager is a status page and a chat box that answers from what
the engineers have written down, without interrupting them.

Standard library Python around the `claude` CLI. No database: the state is files.

```
 your Mac: Lima and the org-host repository (org-vm, lima.yaml). Nothing of this one.
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │ the VM            Debian 13. No Mac files. One port out: 127.0.0.1:8787     │
 │                                                                             │
 │  the org user     no sudo; holds the ledger (~/.org) and two tokens         │
 │    org talk       refine a spec with you, confined to its desk              │
 │    org dash       status page + chat, a service of this user                │
 │         │ copy the template / run a command in / delete      (incus)        │
 │         ▼                                                                   │
 │  org-eng-<run>    one unprivileged container per run, a copy of org-base    │
 │  org-eng-<run>    runner ─▶ claude -p: plan ▸ milestones ▸ report           │
 │                   the internet, and one port of org-cache                   │
 │  org-cache        the build cache every engineer reads and writes           │
 └─────────────────────────────────────────────────────────────────────────────┘
```

The manager's side always initiates. An engineer never needs a route back, and reading a run's
state never touches the engineer's session.

Nothing in this repository runs on the Mac, and `bin/org` refuses to start there. Engineers run
with every permission bypassed and can change org itself, so the Mac's side is a separate
repository, `org-host`, kept in the owner's personal GitHub account, which the engineers' token
cannot reach.

[docs/design.md](docs/design.md) explains why it is built this way and what has been checked.

## Setup

The Mac's side is `org-host`: a Lima VM definition and the `org-vm` launcher. Its README says what
the VM can and cannot reach and how to make the two tokens. From that repository, on the Mac:

```sh
org-vm login           # asks for the two tokens and keeps them in the Keychain
org-vm up              # makes or starts the VM and hands it the tokens
org-vm install main    # in the VM: fetch org at a commit or branch and run `org up` there
org-vm talk
```

`org-vm <command>` runs `org <command>` in the VM as the `org` user, for every command that
`org-vm` does not keep for itself. It keeps `login`, `up`, `down`, `install`, `push`, `shell`,
`dash`, `info` and `destroy`. The commands in the rest of this file are written as they are
typed in the VM (`org-vm shell` gives a shell there); from the Mac, put `org-vm` where `org` is.

`org up` is what runs in the VM. It needs no root: the VM arrives with Incus installed, the `org`
user in the restricted `incus` group, and Claude Code's managed settings in `/etc`. It makes five
things, keeps any that already exist, and then runs `org doctor`:

| | What | Notes |
|---|---|---|
| a release | this copy of org, in `~/.local/share/org/releases/<commit>` | `current` points at the one in use and `~/.local/bin/org` at `current`; earlier releases stay on disk |
| Claude Code | installed for the `org` user when it is missing | for the manager's sessions; no login is made |
| `org-base` | the template: an unprivileged Incus container (Debian 13) with python3, git, gh, tmux, Docker and Claude Code, and no login in it | every engineer is a copy of it, and it is left stopped |
| `org-cache` | the build cache: an unprivileged Incus container (Debian 13) running `bazel-remote` 2.6.4 | left running; see [The build cache](#the-build-cache) |
| `org-dash.service` | the dashboard, a systemd service of the `org` user | restarted by every `org up`, so it runs the release just installed |

```sh
org up --owner <organization>   # the GitHub organization specs may name repositories in
org up --slots 6                # engineers that may run at once (default 4)
org up --memory auto            # each engineer's memory: auto, none or a size such as 4GiB
org up --rebuild-template       # build a new template and make engineers copies of it
org up --port 9000              # the dashboard's port in the VM (default 8787)
```

`org-vm up` passes `--owner` and `--port` itself. The VM forwards one port to the Mac, the same
number on both sides, chosen when the VM is made (`ORG_VM_PORT`, default 8787); a dashboard
moved to another port with `org up --port` by hand is not reachable from the Mac.

`org down` stops the dashboard and nothing else: engineers keep running, the build cache keeps
running with what it holds, and `org up` brings the view of them back. Stopping the VM is
`org-vm down`, which stops every engineer with it.

Two tokens enter the VM, and nothing else of yours does:

| | What it is | What it can do |
|---|---|---|
| a Claude token | what `claude setup-token` prints | make model requests. It is documented as unable to fetch the account's claude.ai connectors |
| a GitHub token | a fine-grained token whose resource owner is one organization, with Contents read and write | read and push code in that organization's repositories. It cannot create a repository, change settings or edit workflow files |

`org-vm` pipes them in at every start, into `/run/org`, which is memory. They are never on the
VM's disk, so they are gone when the VM stops and `org-vm up` hands them in again. `org-vm` also
copies the Mac's git identity (a name and an address, not a login) into the VM.

`org login` only reports which of the three are in place. Nothing in the VM performs a browser
login or asks `gh` for one: a login made there would be the whole account, not the narrow pair.
`org doctor` fails if `~/.claude/.credentials.json` exists, because that file is such a login.

## The flow

```sh
org talk "add CSV export to the table view"
```

1. **Refine.** The manager restates what you are after, reads the repositories (cloning into its
   scratch space to build and test), checks what is already running, challenges the request and
   offers alternatives. It asks only what is yours to decide.
2. **Review.** It writes a draft and runs `org spec check`, which prints the draft's sha256. Read
   it with `org spec show <spec>`, or on the dashboard under "Drafts awaiting approval".
3. **Approve.** Say so. The manager runs `org approve <spec> --sha <prefix>` and Claude Code asks
   you to confirm that command. That confirmation is the approval: the rules the manager runs
   under make the command always ask, and the hash ties it to the text you read. If the draft
   changed after you looked, the hash no longer matches and nothing happens. You can also approve
   from a terminal or from the dashboard.
4. **Watch.** `org status`, the dashboard, or `org ask "why is the export run late?"`.
5. **Afterwards.** `org report <run>` is the engineer's report. `org review <run>` sets the
   frozen plan against what happened. `org machine rm <run>` deletes the engineer's container
   once the work is pushed; the report and the record stay.

## The manager

One persona, two ways in. It is not a machine of its own: it runs in the VM as the `org` user,
beside the ledger.

`org talk` is an interactive Claude Code session. It can read everything and run anything, and it
can change nothing that matters:

| It can | It cannot |
|---|---|
| read any file the `org` user can, any repository, any run | edit approved specs, run records or `config.json` |
| clone, build and test in its scratch space | commit, push, open or merge pull requests, file issues: it has no GitHub login at all |
| write drafts and project briefs in its desk | approve, dispatch, stop, resume, drain, upgrade or `org exec` without your confirmation |
| look at an engineer's files: `org ls`, `org cat`, `org diff` | read the two tokens |
| search the web | run `incus`, `sudo`, `su` or `docker`, use an MCP server or a claude.ai connector, or reach Incus's socket |

What holds that:

- **Claude Code's shell sandbox** (bubblewrap) confines shell commands to the desk, hides the
  token directory and Incus's sockets from them, and limits their network to an allow list
  (`manager.network_allow`: GitHub's hosts by default), at the operating-system level. `org`
  itself runs outside it, because it must write the ledger and reach the containers, and is
  governed by rules instead.
- **`blockWritesOutsideWorkingDirectories`** confines the file tools to the desk.
- **Rules.** Read commands are allowed. `org approve`, `dispatch`, `stop`, `resume`, `exec`,
  `drain`, `undrain`, `upgrade`, `up`, `down`, `machine rm` and `config set` always ask you, in
  every permission mode. `incus`, `sudo`, `su`, `docker`, every MCP tool, and pushes and other
  commands that change GitHub are refused.
- **No GitHub token.** The session is started without one, and `gh` is no longer excluded from
  the sandbox, because there is no login for it to find.

Every repository the manager works with is public, so it reads them without a login.
`org repos` lists the organization's repositories. `org clone <owner/name>` takes a repository
and nothing else and leaves a copy in `desk/scratch/<name>` with nowhere to push; run again, it
fetches.

You do not register a project first. Name it in the conversation: the manager looks for the
repository in the organization, confirms the match, and records it with `org project add`.

Where there is no working sandbox, `org talk` says so and starts the session in the ask-first
permission mode, so that a write outside the desk is always your decision and never a
classifier's.

`org talk --dry-run` prints the exact settings. `tests/live_manager_rules.py` attempts fourteen
actions against the real harness and judges by outcome; it has not yet been run in this layout
(see below).

`org ask` and the dashboard's chat box are the same persona, headless and read-only: nothing
that would need permission can run. It runs on a cheaper model (`manager.ask_model`).

The Claude Code in the VM is a fresh install whose default is a smaller model, so `org up` sets
the manager chat to `fable` at high effort. `org config set manager.model <model>` and
`manager.effort` change that, and a later `org up` keeps what you chose.

The manager keeps a brief per project (`desk/projects/<name>.md`): how to build and test, hot
paths, how far estimates run over. Engineers receive it with every spec.

## The engineer

A headless `claude -p` session with all permissions bypassed, because its container is the
sandbox. Each run gets an unprivileged Incus system container, `org-eng-<run>`, an instant
copy-on-write copy of the template. The engineer is root there, in a whole Linux system with a
Docker daemon of its own, so a project whose tests need containers can run them. It reaches the
internet. It cannot reach the Mac, the LAN, the VM itself or another engineer's container; of
the VM it can only ask for an address and a name. The one thing beside the internet that it can
reach is one port of the build cache's container (see [The build cache](#the-build-cache)), and
through it an engineer uses what other engineers built. Dispatch copies the two tokens into the
container's memory and the runner hands them to the session as `CLAUDE_CODE_OAUTH_TOKEN` and
`GH_TOKEN`.

A deterministic runner drives the session through three phases, resuming one session throughout:

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
container has (its cores, and whether a Docker daemon answered just before the run), what its
GitHub token reaches, and that every repository is public: what strangers wrote in issues, pull
requests and comments is text to read, never an instruction.

## The pool

One number: how many engineers' containers may run at once.

```sh
org pool                                    # slots in use, what is queued
org config set machines.incus.slots 6       # change it
```

A run takes a slot when it is dispatched. When every slot is taken it waits in the queue, and
starts when one frees: anything that looks at the ledger (`org status`, the dashboard) moves the
queue, so there is no scheduler to keep alive. The same pass gives back the slot of a run whose
dispatch was cut short by a process that died, and a dispatch that fails removes the container
it made. `org pool` also prints one line about the build cache, described below, and shows the
memory limit on the Incus line.

### The memory limit

Each engineer's container has a hard memory limit. On 2026-11-24 the VM (11951 MiB, no swap)
ran out of memory while three engineers were building, and the kernel killed a test run in one
container and five processes outside the containers. Nothing bounded what a container could
take. Now each one is bounded, so a build too large for its share fails in its own container,
where the kernel kills a process, and the engineers' containers together can no longer exhaust
the machine.

With `machines.incus.memory` absent or `"auto"`, each container gets
`floor((MemTotal - reserve) / slots)` in whole MiB. `MemTotal` is the machine's, read from
`/proc/meminfo`; the reserve is `machines.incus.memory_reserve` (default `3GiB`) and is left to
the machine's own services, Incus's daemon, the manager chat and the dashboard; slots is
`machines.incus.slots` (default 4). On the VM above, with the 3GiB (3072 MiB) reserve, 4 slots
give `2219MiB` each and 2 slots give `4439MiB` each.

```sh
org config set machines.incus.memory auto        # the default: the arithmetic above
org config set machines.incus.memory 6GiB        # a size, taken as given
org config set machines.incus.memory none        # no limit
org config set machines.incus.memory_reserve 2GiB
org up --memory auto                             # sets machines.incus.memory, as --slots sets the slots
```

A size is a positive whole number followed by `MiB` or `GiB`. A size you give is taken as given:
it is for an owner who accepts that the containers together may promise more than the machine
has. Anything else is refused with a sentence that names the setting. With `auto` and no slot
count, or no readable `MemTotal`, there is no limit, and `org doctor` says so.

**What changes for an installation that already exists.** A ledger with no
`machines.incus.memory` gets the automatic limit after `org upgrade`. On a small machine this
is the first thing you will notice: on a 4 GiB machine with 4 slots the limit is
(4096 - 3072) / 4 = 256 MiB each, which `org doctor` warns is too little. Lower the slots or the
reserve, or set a size. The limit applies to containers made after the upgrade. A container
already running is left alone, and a stopped one gets the limit when `org resume` starts it.
A run that was dispatched before the upgrade carries the old release's runner, which knows
nothing of the limit: resumed, its container is limited and its engineer is not told. A run
dispatched after it is told at resume what its container was given then.

Dispatch gives the limit to Incus on the `incus copy` command itself (`-c limits.memory=<n>MiB`),
so the copy never exists or starts without it, and Incus enforces it hard by default. Before any
token is copied in, dispatch reads Incus's description of the container and refuses the run if
it does not carry exactly the limit asked for, or says `limits.memory.enforce=soft`. That
refusal, and a refused copy, point to `org config set machines.incus.memory none` as the way to
dispatch without a limit. The limit is kept in the run's machine record. The throwaway
container `org upgrade` tests with gets the same limit; the template and the build cache's
container get none.

The engineer is told how much memory it has, that the limit is hard, that a compiler or test
ending with `Killed` or `terminated` is a reason to lower parallelism and not to run it again
unchanged, and that `/sys/fs/cgroup/memory.events` counts the kills. When the kernel must
choose, it kills a build step before the session: with a limit in force the runner gives Claude
Code a wrapper (`CLAUDE_CODE_SHELL_PREFIX`, which Claude Code runs every shell command through)
that raises its own `oom_score_adj` to 500 and then runs the command, so what the session runs
scores higher than the session and the runner. Raising one's own adjustment needs no privilege,
and nothing is given to anybody. Containers an engineer starts through a Docker daemon are that
daemon's children, not the shell's, and are not covered.

**What you see.** Each sync's snapshot carries the container's `oom_kill` count from
`/sys/fs/cgroup/memory.events`. `org status` prints a line under the table when it is not zero,
for example `! <run>: 2 processes were killed for memory in its container (limit 2219MiB)`;
`org peek` shows the same, and `org status --json` carries `oom_kill`. The count includes kills
in containers the engineer runs inside its own, because the kernel counts a cgroup together with
everything under it. A container that is stopped and started counts from zero again, so the
ledger keeps the highest count it has seen: the line does not go away, and may then say fewer
than there were. `org doctor` has a row `memory per engineer` (the limit, the slots, the
reserve and the machine's memory), and warns when there is no limit, when the limit times the
slots exceeds `MemTotal` minus the reserve, or when the limit is under 2 GiB.

**What it does not cover.** Everything outside the engineers' containers: the machine's
services, Incus's daemon, the manager chat, the dashboard and the build cache's container.
That is what the reserve is for, and nothing limits them. There is no swap, CPU, disk or
process-count limit.

## The build cache

An engineer's first Bazel build in a fresh container compiled everything from nothing, which was
about ten minutes of every run of a project that needs it. For `org-004/solar-bay-assembly` it
took 611.7 s with an empty cache and 148.8 s from a filled one, measured in that project's run
004. So there is one more container on the VM, `org-cache` (`machines.incus.cache.name`), that
every engineer reads and writes. It runs `bazel-remote` 2.6.4
(https://github.com/buchgr/bazel-remote), one program that serves Bazel's remote cache over gRPC
from one directory, `/var/lib/bazel-remote`, and keeps that directory under
`machines.incus.cache.max_gb` gigabytes (default 20) by dropping what was used longest ago. It
listens for gRPC on `machines.incus.cache.port` (default 9092, the server's own default). Its
HTTP port is bound to the container's own loopback, so other containers cannot reach it. It runs
as a service of a user `bazel-remote` that is not root and starts with the container, and the
container has `boot.autostart`, so it comes back when the VM restarts.

**Who makes it.** `org up`, as the `org` user, when it is missing: from `images:debian/13`,
unprivileged, without nesting, with no device but its root disk and its network card.
`incus/cache.sh` is run once inside it, as `incus/bootstrap.sh` is run inside the template. It
downloads the pinned `bazel-remote` release from GitHub for arm64 or x86-64 and installs it only
if its SHA-256 equals the one written in the script. `org up` leaves an existing cache as it is
and starts it if it is stopped. If the server does not come up, `org up` removes the container
again and ends with an error after the rest of its work; engineers then build without a cache and
the next `org up` tries again. A cache name that starts with the engineers' prefix is refused, as
the template's is. Only Incus machines are offered the cache: ssh, local and Docker machines are
not.

**What an engineer can now reach.** One port (gRPC, 9092 unless changed) of one container, on the
VM's bridge, if the VM's firewall lets it through. Nothing else is newly reachable: not the VM,
not another engineer, not the cache's HTTP port. The server listens on that port over IPv6 as
well as IPv4; the settings engineers are given use its IPv4 address.

**Engineers use each other's results, and nothing can check that they are true.** Every engineer
writes to the cache and every engineer reads from it, so a result one engineer's build uploaded
is used by another engineer's build and by its tests. The server checks a stored file against
its hash and checks that a stored action result is a well-formed record, but it cannot check that
the stored result of an action is the true one. An engineer that uploads a wrong output for an
action poisons that action for every later build with the same inputs, until the cache is
emptied. There is no credential, no TLS and no rule about who may write: what protects the server
is only that nothing but containers on the VM's bridge can reach it. The owner accepted this on
2026-11-24 for the sake of build time. CI does not use this cache.

**The cache can reach the engineers.** Its network card has `security.ipv4_filtering`, as an
engineer's has, but not `security.port_isolation`, on purpose: an isolated card cannot reach
another isolated card, and the engineers' cards are isolated. So the cache container can open
connections to engineers' containers. That is why it holds no login, token or key, why none is
ever copied into it, and why nothing but `org up` ever enters it. The backend never creates,
enters or deletes it on behalf of a run, and only reads Incus's description of it.

**How an engineer is told.** At dispatch, after `vet` has passed and before the tokens are
copied in, dispatch looks for the cache. If it is running, dispatch tries from inside the
engineer's container to open a connection to its address and port, and prints `build cache
reachable at <address>:<port>` or `build cache not reachable at <address>:<port>, so builds will
not use it`, with what may be in the way. The run's record keeps which, as `build_cache`. Only
when the connection was made does dispatch write Bazel's system-wide settings file,
`/etc/bazel.bazelrc`, in the engineer's container, with exactly four settings: `build
--remote_cache=grpc://<address>:<port>`, `build --remote_upload_local_results=true`, `build
--remote_local_fallback` and `build --incompatible_remote_local_fallback_for_remote_cache`.
Nothing else goes in it, and a project without Bazel never reads it. The last two together make
Bazel 9 build locally when the cache stops answering part-way; with the first alone the build
fails, which was tried with Bazel 9.3.0. That Bazel then succeeds without printing a warning:
the only sign is a wait, about 3 s when the connection is refused and about 20 s when the
address does not answer. The last setting exists from Bazel 8.6.0 on. A project pinned to an
older Bazel (7.7.3 and 8.5.0 were tried) stops at it with "Unrecognized option" in every build,
and Bazel has no way to write an option an older version may skip; the file says so in its
comment, and an engineer there removes the file and builds without the cache. A stopped cache gets a line saying so, and a missing one prints nothing
and the run goes on as before. When a stopped engineer's container is resumed the same is done
again, so that a cache whose address changed is found, and a settings file org wrote earlier is
removed if the cache is there and no longer reachable. A failure anywhere in this never fails a
dispatch.

**What is not known yet.** The VM's firewall is made by `org-host`, and nothing here can change
it. It drops what containers send to private, link-local and other non-public ranges, and whether
it also drops traffic from one container to another on the same bridge, which is switched and
not routed, could not be tried anywhere but on the VM. If it does, dispatch says "not reachable"
and builds go on without the cache until `org-host` allows exactly this: source the engineers'
bridge, destination the cache container's address and gRPC port, TCP, nothing wider. The first
dispatch after `org upgrade` and `org up` shows which it is.

**Seeing it.** `org doctor` has one row for it, taken from `incus list` alone: missing (fix:
`org up`), stopped, or `running at <address>:<port>`. It fails the row if the container is
privileged or has a device beyond its disk and its card. `org pool` prints one line,
`build cache: org-cache running at <address>:<port>` (or stopped, or missing).

**Switching it off, emptying it.** `org config set machines.incus.cache.enabled false`: `org up`
then makes no cache, dispatch looks at none, asks Incus nothing about it and writes no settings,
and the installation behaves as it did before the cache existed, but for a row in `org doctor`
that says it is switched off. A cache container that already exists is left where it is, and so
are the settings in engineers' containers that were dispatched before: `incus delete -f
org-cache` removes the container, and their builds then go on without it. The same command, followed
by `org up`, which makes a new and empty one, is how to empty the cache. It is also how a changed
port or size takes effect, because an existing container is not reconfigured. Engineers
dispatched in between build without the cache.

## Running the installation

```sh
org drain [--wait] [--reason "why"]    # start nothing new; with --wait, return when no run holds a container
org drain --status
org undrain                            # lift it and start what queued

org upgrade <commit or branch>         # install a commit of org that is on the default branch
org upgrade --rollback                 # go back to the release in use before

org up --rebuild-template              # a new template; engineers from now on are copies of it
org resume --interrupted               # after the VM restarts: every run whose runner is gone
```

- **Draining.** While a drain is in force, approved work queues and nothing new starts.
  Engineers already running are left alone.
- **Upgrading.** The commit must be on the default branch of the repository this copy was
  fetched from; one that exists only on another branch or in a fork is refused. You are shown
  the commits and files that changed, with the files that hold the walls and the tokens named
  apart, and asked to type yes. The commit's own tests then run in a throwaway container that
  holds no token. Dispatch is drained, the `current` link is switched, the dashboard restarts,
  and the drain is lifted. Engineers already at work keep the code they started with.
  Rolling back moves the link to the release before and restarts the dashboard.
- **The template.** `org up --rebuild-template` builds `org-base-<timestamp>` from
  `incus/bootstrap.sh`, points `machines.incus.base` at it, and leaves the old template where
  it was.
- **After a restart.** The tokens lived in memory, in the VM and in every container. `org-vm up`
  hands them to the VM again. `org resume --interrupted` then starts each stopped container,
  copies the tokens into it again, and restarts its runner, which continues from its last
  finished step. The build cache comes back by itself with the VM.

## Looking at an engineer's work

Without touching its session:

```sh
org peek <run>              # state, milestones, spend, recent activity
org diff <run>              # every repository in its workspace: branch, unpushed, uncommitted
org ls <run> [path]         # its files (the workspace by default)
org cat <run> <path>
org exec <run> -- <cmd>     # anything; the manager chat has to ask you first
```

In the VM, `incus exec org-eng-<run> -t -- tmux attach -t org-<run>` shows the runner's live
feed; `org peek` prints that line.

**A web app an engineer is serving** can be opened in your browser; this is new, and no person
has exercised it yet. With `dash.view` set (it is off by default), `org view <run> <port>` and a
control on the dashboard open the server at an origin of its own,
`http://v-<id>.localhost:8787/`: the dashboard relays requests through `incus exec` into the
container's loopback, exchanges a single-use ticket for a host-only cookie, and adds a
restrictive Content-Security-Policy. A page an engineer serves is an untrusted web page running
in your browser, and on macOS 15 it opens in Chrome and Firefox only, because Safari does not
resolve `*.localhost` there.

## Specs

One Markdown file with a flat header:

```
---
title: CSV export for the table view
project: acme
repos: acme/web, acme/api
delivery: merge         # merge | branch | none
machine: incus          # incus | ssh:<name>; leave it out for the default
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
can check, including that every repository is one an engineer can reach, that it is in the
organization named by `github.owner` (the only place an engineer can push), and that the machine
the spec names is one this installation has. Changing an approved spec means revising the draft
and approving again, which records a new version and starts a new run.

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
org up | down | login | doctor | init                      set up, stop the dashboard, what is in place
org talk [message]              talk to the manager        org ask "question"
org status [--watch|--json]     every run                  org dash [--url]
org spec new|check|show|ls|revise
org approve <spec> --sha <8+>   freeze and dispatch        org dispatch <spec> [--machine M]
org peek <run>                  one run, in words          org logs <run> [-f]
org plan|report|review <run>
org ls|cat|diff <run>           an engineer's files        org exec <run> -- <cmd>
org view <run> <port>           a web app in an engineer's container (new; needs dash.view)
org stop <run>                  org resume <run> | --interrupted
org machine ls | rm <run>       org pool                   org pump
org drain [--wait] [--reason R] [--status]                 org undrain
org upgrade <ref> | --rollback  install a commit from the default branch, or go back one
org config get|set <key> [value]                           org sync [run]
org project add <name> --repo owner/name                   org project ls
org repos                       the organization's public repositories
org clone <owner/name>          a public repository, into the manager's scratch space
```

A `<run>` may be a run id, a unique prefix, or a spec reference (its latest run). A `<spec>` may
be its id, its number, a unique prefix, or the path of its draft.

## Configuration

`config.json` in the ledger, read and changed with `org config get|set <dotted.key> [json]`.
Everything is optional; these are the values `org up` sets and the defaults around them.

```json
{
  "machines": {
    "default": "incus",
    "incus": {"base": "org-base", "prefix": "org-eng-", "slots": 4, "push_token": true,
              "cache": {"enabled": true, "name": "org-cache", "port": 9092, "max_gb": 20}},
    "ssh": {}
  },
  "github": {"owner": "<organization>"},
  "engineer": {"model": "fable", "effort": "high", "max_turn_retries": 3, "report_reserve_usd": 3.0},
  "manager": {"model": "fable", "effort": "high", "ask_model": "sonnet", "sandbox": true, "own_claude": true,
              "network_allow": ["github.com", "*.github.com", "*.githubusercontent.com"]},
  "budget": {"usd_per_weekly_pct": null},
  "status": {"sync_timeout_s": 8},
  "dash": {"port": 8787, "sync_interval_s": 10}
}
```

`machines.incus.cache` is the build cache: `enabled` (default `true`), `name` (`org-cache`), `port`
(`9092`) and `max_gb` (`20`). The port and size are read by `org up` when it makes the container
and do not change an existing one. The memory limit's settings, `machines.incus.memory` and
`machines.incus.memory_reserve`, are described under [The memory limit](#the-memory-limit); both
are absent from the file above because they have defaults.

Claude Code's settings in the template are written when it is built: no attribution lines in
commits or pull requests, no auto memory, no claude.ai connectors, and a managed file that
allows no MCP server.

## Where things are

```
on the Mac (the org-host repository; nothing of this one):
  org-vm, lima.yaml          the launcher and the VM's definition
  the Keychain               the two tokens

in the VM, as the org user:
  /run/org/                  claude-token and github-token, in memory, readable by org alone
  ~/.org/                    the ledger
    desk/drafts/<spec>.md    specs under refinement      (the manager may write here)
    desk/projects/<name>.md  project briefs
    desk/scratch/            the manager's throwaway clones
    specs/<spec>/spec.md     approved, read-only; meta.json holds the sha256 and version
    runs/<run>/run.json      where it runs
    runs/<run>/mirror/       status.json, plan.frozen.md, report.md, actuals.json, events.jsonl
    budget.json              weekly-limit readings
    drain.json               there while a drain is in force
  ~/src/org/                 the checkout `org-vm install` made and `org upgrade` fetches into
  ~/.local/share/org/        releases/<commit>/, and the links `current` and `previous`
  the container org-cache    bazel-remote and /var/lib/bazel-remote (its results); holds no login
  /etc/claude-code/          root's: no MCP servers, no claude.ai connectors

in an engineer's container (org-eng-<run>), as its root:
  /run/org/                  its copies of the two tokens, in memory
  ~/org-runs/<run>/          spec.md, job.json, status.json, plan.md, report.md, events.jsonl
  ~/work/<run>/              the engineer's clones
  /etc/bazel.bazelrc         only when the build cache was reachable: four settings, written at dispatch
  tmux session org-<run>     the runner's live feed
```

## The dashboard

On the Mac, `org-vm dash` opens it. The page is served by `org-dash.service` on the VM's
loopback, port 8787, which is the one port the VM forwards to the Mac's `127.0.0.1`; engineers'
containers cannot reach it. The part of the URL after `#` is an access token: the page can
approve specs and stop runs, so every request must carry it, and requests not addressed to this
machine are refused. `org dash --url` prints the address.

## Other kinds of machine

Where engineers run is one interface (`org/backends/`). The Incus backend is the one this
installation is built around and the only one exercised in it. It is also the only one offered
the build cache.

| `machine:` | Provisioned by | Reached by | State |
|---|---|---|---|
| `incus` | a copy of the template, one per run | `incus exec` | the default |
| `ssh:<name>` | exists already; listed under `machines.ssh` in config | `ssh` | present; not exercised in this layout |
| `local` | nothing; off unless `machines.local.enabled` | a subprocess | for trying org out and for its tests; no isolation |
| `docker[:node]` | `docker run`, as a plugin | `docker exec` | unmaintained, see below |

An ssh host is listed in `config.json` and named in a spec (`machine: ssh:bigbox`):

```json
{"machines": {"ssh": {"bigbox": {"host": "me@bigbox", "slots": 2, "push_token": true}}}}
```

The VM holds no ssh key, so one has to be put there on purpose, and the firewall that fences the
containers does not apply to such a host: it is as isolated as its owner made it. With
`push_token` it is given the Claude token; its GitHub login is whatever its owner set up there.

**The Docker plugin is unmaintained in this layout.** The backend (`org/backends/docker.py`) and
its unit tests are still here, but its host-side script, `docker/up.sh`, has not been updated for
the token changes: it still calls `gh auth token` and hands the manager a full GitHub login,
and `org up` no longer reaches it. Do not use it as it stands.

Any module that defines `BACKEND` can be loaded as a plugin:
`{"machines": {"plugins": ["your.module"]}}`. The modules `config.json` names are imported, which
is one reason the manager chat may not edit that file.

## Tests

In the VM (`org-vm shell`):

```sh
cd ~/.local/share/org/current
python3 -m unittest discover -s tests       # no tokens spent: a scripted stand-in for the claude
                                            # CLI, and simulated incus and docker
python3 tests/demo_ledger.py /tmp/demo && org --home /tmp/demo status      # made-up runs in every state

# with real Claude Code and the tokens handed in
python3 tests/e2e.py --machine incus        # a real engineer in a real container (about a dollar)
python3 tests/live_manager_rules.py         # the manager's confinement (a few cents)
python3 tests/live_manager_refine.py        # a two-turn refinement of a request with a false premise

# the build cache
sh tests/live_cache_docker.sh               # needs Docker; real bazel-remote and Bazel, a few minutes
tests/live_cache_incus.sh                   # as root, where there is an Incus of one's own (see below)

# the memory limit
tests/live_memory_incus.sh                  # the same kind of Incus; about a minute
```

`tests/e2e.py` approves a small spec, dispatches it, follows it to the end, and then repeats the
acceptance checks itself in a fresh clone on the engineer's container instead of taking the
engineer's word. It uses a ledger of its own, so it leaves nothing in yours, and deletes the
container when the run passes.

The build cache has unit tests in `tests/test_cache.py`, against the simulated Incus, which run
with the rest. `tests/live_cache_docker.sh` installs the real `bazel-remote` 2.6.4 with
`incus/cache.sh` in a bare `debian:13` container and builds a small workspace with real Bazel
9.3.0, fetched through Bazelisk 1.30.0 (the template itself carries no Bazel): a build uploads, a
second build from a new output base gets cache hits and runs no compile or link, and builds still
succeed with the server killed and with its container stopped. `tests/live_cache_incus.sh` needs
an Incus of one's own, for example one installed inside an engineer's container, and root there;
its header says how to install one. It makes a user that is not root and, as that user, a small
stand-in for the template (the template's settings, with g++ and Bazelisk in place of the real
bootstrap), then runs the whole of `org up`, which makes the cache container. With the code
`org up` installed it copies the template twice as dispatch does, has `vet` and `problems()` judge
the copies, offers them the cache, builds in the first and requires cache hits in the second,
requires that neither engineer's container can open a connection to the other, and removes what
it made.

`tests/live_memory_incus.sh` needs the same kind of Incus and root. It copies a stand-in for the
template with `machines.incus.memory` set to 128MiB, as dispatch does, and shows that `vet` finds
the limit in Incus's description; that the container reads it back from its own `memory.max`;
that a process taking more is killed and counted in the container's `memory.events` while a
second container and the session running the script are untouched; that with the runner's
wrapper the command is killed and the stand-in for the session lives, and without it the
stand-in is killed; and that a restricted Incus project, like the one the `org` user is given,
accepts `limits.memory` on a copy, on an existing container and on a new one.

## What has and has not been exercised

On 2026-11-23, on an M4 Mac with macOS 15.7, Lima 2.3.0, Debian 13, Incus 6.0.4 and Claude Code
2.1.330:

- **Exercised for real:**
  - The unit suite passes inside the VM under Python 3.13.
  - `org up` in the VM from nothing: it installs the release, installs Claude Code, builds the
    template and starts the dashboard service. `org doctor` then reports only the missing tokens
    and git identity.
  - From a real container the internet answers. The Lima gateway (192.168.5.2), the VM's own
    addresses, the Mac's LAN address, the LAN router and a second container do not (before the build cache, which an engineer is meant to reach).
  - Docker installs, pulls, builds and runs inside a container. bubblewrap works for the `org`
    user in the VM and inside a container. A copy of a container takes about 0.06 s.
  - `tests/e2e.py --machine incus` with a real engineer, twice: once on the VM as first built,
    once after the VM was deleted, recreated from `org-host` and installed from this repository
    with `org-vm install main`. Each time the engineer planned, built, pushed and reported
    `done` in about thirty seconds for about $0.33 on sonnet, the script's own check in a fresh
    clone agreed with all five acceptance criteria, and the container was deleted afterwards.
  - The same test first with no tokens, and then with a Claude token that had been cut short
    when it was copied: everything up to the model call worked, and the run ended `failed` with
    the API's own message, "Not logged in" and "OAuth access token is invalid".
  - With the GitHub token in the VM: both of the organization's repositories answered, and two
    private repositories in the owner's personal account answered 404.
  - `claude mcp list` in the VM, with the Claude token: no MCP servers.
  - The build cache, apart from the VM, on arm64 (2026-11-24): the unit tests;
    `tests/live_cache_docker.sh` with the real server and Bazel 9.3.0 as described under Tests;
    and `tests/live_cache_incus.sh` in an Incus 6.0.4 installed inside an engineer's container,
    where `org up`, run by an unprivileged user, made the cache container, two copies of a
    stand-in template passed `problems()`, reached the cache and were given its address, the
    second built from the first's results, and neither could open a connection to the other. In
    a separate trial there, the cache container could open connections to engineers' containers.
  - The memory limit, apart from the VM: the unit tests, and `tests/live_memory_incus.sh` in an
    Incus of one's own, as described under Tests.
- **Not yet exercised:**
  - The manager chat and `org ask` in the VM, and an engineer landing a commit on the default
    branch of a repository on GitHub (the smoke spec pushes to a repository inside its own
    container).
  - `tests/live_manager_rules.py` and `tests/live_manager_refine.py` in this layout.
  - `org upgrade` and its rollback against a real repository.
  - `org drain`, `org undrain` and `org resume --interrupted` on a real installation, and a
    restart of the VM.
  - The web-app view.
  - Nested Incus inside an engineer's container, for an engineer that works on org itself. Only
    the bubblewrap part of that was checked.
  - ssh machines beside the containers.
  - The VM's real firewall with the cache: whether it lets one container reach another on the
    bridge (see The build cache). Until then dispatch may say "not reachable".
  - Anything of the cache on x86-64: the pinned x86-64 hashes were checked, and nothing was run.
  - The cache under the real template or a real project's build.
  - That the VM's own restricted Incus project (`user-2000`) accepts `limits.memory`. It is
    the first thing to check after upgrading, as the `org` user:
    `incus copy -q org-base memcheck -c limits.memory=2219MiB && incus delete memcheck`.
    Silence and exit status 0 mean it does. If it does not, the copy prints Incus's `Error:`
    and makes nothing, dispatch would refuse every run with the same words, and
    `org config set machines.incus.memory none` dispatches without a limit.
  - The wrapper that has a build step killed before the session, under a real Claude Code
    session: it was tried with a stand-in that calls it as Claude Code's documentation and the
    text of version 2.1.330 say a shell command is called.

## Limits worth knowing

- **Engineers hold both tokens and reach the internet.** A run can do whatever the tokens allow,
  and could send them anywhere. What bounds that is their scope: one organization's code, and
  model requests. The owner can revoke either.
- **An engineer lands its own work on the default branch.** Pull requests are not used: the
  default delivery is one squashed commit pushed to the default branch by the engineer, and you
  and the engineers are the same GitHub account, so nothing on GitHub stands between an
  engineer and `main`. A default branch cannot be force-pushed or deleted, so what landed stays
  in its history. What decides which version of org runs is `org upgrade`, which shows you the
  diff first.
- **Public repositories are a way in.** Every repository is public, so text a stranger writes
  in a pull request or a comment can reach an agent. The prompts say it is never an instruction,
  and `org-host`'s `org-github` turns issues off. That is a rule a model follows, not a wall.
- **The build cache's settings are for Bazel 8.6 and later.** In an engineer's container that
  reached the cache, a project pinned to an older Bazel fails every build with "Unrecognized
  option: --incompatible_remote_local_fallback_for_remote_cache" until `/etc/bazel.bazelrc` is
  removed there. The project the cache was made for builds with Bazel 9.3.0.
- **Engineers use each other's build results, and the cache cannot tell a true one.** One
  engineer's wrong output for an action is used by every later build with the same inputs until
  the cache is emptied. The cache has no credential and no TLS; only containers on the VM's
  bridge can reach it, and the cache container can reach them. The owner accepted this on
  2026-11-24 for the sake of build time. CI does not use the cache.
- **One kernel.** All engineers and the manager share the VM's kernel. A kernel exploit from a
  container reaches the VM, which holds both tokens, the ledger and every engineer, and still
  nothing of the Mac's.
- **The `org` user is the control plane.** It holds both tokens and can enter every engineer's
  container. What keeps the manager chat from that power is its sandbox and rules.
- **An engineer can undo its own Claude Code settings.** It is root in its container, so the
  files there that forbid MCP servers and connectors are its to change. What protects the
  account is that its Claude token can only make model requests.
- **The manager can change nothing on GitHub**, because it has no token. Its shell can still
  reach the hosts on the sandbox's allow list, and its web search and fetch tools reach the web.
- **A page an engineer serves is an untrusted web page** running in your browser on the Mac,
  when you open it with the web-app view.
- **Nothing starts by itself.** After the Mac restarts, `org-vm up` brings the VM and the tokens
  back and `org resume --interrupted` restarts the runners. Until then nothing runs.
- **Containers stay.** A finished run's container is kept until `org machine rm`; nothing
  reclaims the disk by itself.
- **One engineer per spec.** Width comes from the engineer's subagents and from running several
  specs at once.
- **All engineers and you share one weekly limit.** A run that hits it waits for the reset and
  resumes on its own.
