# org: design

What the pieces are, why they are shaped this way, and what has been checked. The README says
how to use it; this says why it is built the way it is.

## The problem

One person wants several pieces of software work done at once without doing them, and without
losing track of what each costs. That takes two roles with opposite temperaments:

- A **manager** that talks to the person, is skeptical of the request, and turns it into a spec
  exact enough to hand to someone who cannot ask questions. Afterwards it is the person's window
  onto the work.
- **Engineers** that never talk to the person. Each takes one approved spec, plans it, does it in
  a container of its own, and reports.

Both are Claude Code sessions. Everything else in this repository is the deterministic machinery
between them.

## Principles

These are the decisions the rest follows from.

1. **The manager's side initiates everything.** It pushes the spec out and pulls status back.
   An engineer's container never needs a route to the manager, so containers can be isolated, and
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
   cannot do is meant to be backed by a test that tries to do it against the real harness. In
   the present layout that test has not been run yet, and this document says so where it matters.
8. **Nothing an engineer can change runs on the Mac.** Engineers can push to this repository. So
   the Mac runs a separate repository that their token cannot reach, and org is fetched and run
   inside a VM that has nothing of the Mac's in it.
9. **A credential is as wide as its job.** Two tokens enter the VM, each scoped to what an
   engineer needs, and they live in memory. The manager chat is given no GitHub token at all.

## Components

```
            the person
                │  talk / approve / watch
   ┌────────────▼─────────────────────────────────────────────┐
   │ manager                                                  │
   │   org talk     Claude Code + persona, confined to a desk │
   │   org dash     status page, chat box, sync loop          │
   │   ledger       drafts · approved specs · runs · budget   │
   │   dispatch     place ▸ provision ▸ vet ▸ bundle ▸ start  │
   │   sync         one read-only snapshot per run            │
   └────────────┬─────────────────────────────▲───────────────┘
        bundle  │ over a transport            │ snapshot (JSON)
   ┌────────────▼─────────────────────────────┴───────────────┐
   │ an engineer's container (one per run)                    │
   │   runner       plan ▸ milestone… ▸ report                │
   │     └─ claude -p   one resumed session, bypass mode      │
   │   run dir      spec · job · status · plan · report · log │
   │   workspace    the engineer's clones, caches, worktrees  │
   └──────────────────────────────────────────────────────────┘
```

| Module | Side | Job |
|---|---|---|
| `ledger.py` | manager | paths, ids, config |
| `spec.py` | manager | draft template, checks (among them the one organization), approval |
| `secrets.py` | both | the two tokens: where they are, what hides them, the environment of a process that acts as the person. The only reader |
| `dispatch.py` | manager | choose and place a machine, tokens, bundle, start, stop, resume, the queue, draining, reaping |
| `backends/` | manager | one kind of machine each: how to pick, create, vet, reach and remove it (`incus`, `ssh`, `local`; `docker` as a plugin) |
| `cache.py` | manager | the build cache as dispatch sees it: look, probe from an engineer's container, write Bazel's settings, the `org doctor` row and the `org pool` line |
| `transport.py` | manager | run a script on a machine: Incus, ssh, local, Docker |
| `admin.py` | manager | a drain that waits, upgrade and rollback, the organization's list of repositories |
| `sync.py` | manager | pull snapshots into `runs/<run>/mirror/` |
| `status.py` | manager | one document for the terminal, the page and the manager |
| `budget.py` | manager | weekly-limit readings, the learned dollars-per-percent rate, estimate history |
| `manager.py` | manager | launch the manager chat and the read-only question mode |
| `login.py`, `doctor.py` | manager | which tokens are in place; what is missing, and what is wider than it should be |
| `dash.py`, `web/` | manager | the status page and its API |
| `runner.py` | engineer | the three phases, recovery, caps, status |
| `stream.py` | engineer | read the CLI's event stream: spend, activity, limit state |
| `schemas.py`, `prompts/` | engineer | what each turn must return, and the engineer's instructions |
| `bin/org`, `incus/` | the VM | the wrapper, `org up` and `org down`, the template's setup and the build cache's (`cache.sh`) |
| `docker/` | | the Docker plugin's image and host script; unmaintained |

The engineer's side is the same package: dispatch copies it into the run directory, so a run
keeps the code it started with even if the manager's copy is upgraded mid-run.

## Where everything runs

One Linux VM on the person's Mac holds everything: the manager, the ledger, and every engineer as
a container inside it. The VM is made by a separate repository, `org-host`, in the person's
personal GitHub account: a Lima VM definition and the `org-vm` launcher. That repository's README
is the account of the Mac's side and of what the VM can reach; this section says what org relies
on and does not repeat it.

```
 the Mac: Lima + org-host (lima.yaml, org-vm). The Keychain holds the two tokens.
        │ commands in, the tokens over stdin                 127.0.0.1:8787 ▲
 ┌──────▼───────────────────────────────────────────────────────────────────┴──┐
 │ the VM (Debian 13)   no Mac files · no route to the Mac's loopback          │
 │                                                                             │
 │  root, admin    the firewall, /etc/claude-code, Incus itself; org-vm only   │
 │                                                                             │
 │  org            no sudo; Incus project user-2000 and its bridge, no other   │
 │    holds        the ledger (~/.org) · /run/org: the two tokens, in memory   │
 │    runs         org · the manager chat (bubblewrap) · org-dash.service      │
 │         │ incus copy · incus exec · incus delete       (names org-eng-…)    │
 │         ▼                                                                   │
 │  org-base       the template, stopped: python3, git, gh, tmux, Docker,      │
 │                 Claude Code, and no login. Only ever copied.                │
 │  org-eng-<run>  a copy of it, one per run, unprivileged                     │
 │    given        both tokens over stdin, into its own /run/org               │
 │    has          root, an init, a Docker daemon of its own, the internet,    │
 │                 and one port of org-cache                                   │
 │  org-cache       the build cache: bazel-remote, one gRPC port, no login     │
 │                  Every engineer reads and writes it; only org up enters it  │
 └─────────────────────────────────────────────────────────────────────────────┘
```

**Why a VM, and why another repository makes it.** Engineers run with every permission bypassed
and can push to this repository. Code from here must therefore never run on the Mac, where the
person's files and logins are. `bin/org` refuses to start on Darwin (unless `ORG_ALLOW_DARWIN`
is set), and the Mac-side host agent, its bridge, the OrbStack backend and `orbstack/` that
earlier versions had are gone. What the Mac runs is `org-host`, which the engineers' token cannot
reach and which is small enough to read. The VM it makes has no Mac files mounted, cannot reach
the Mac's loopback, and forwards one port: the dashboard, to `127.0.0.1:8787`.

**Two users.** `admin` is Lima's, has sudo, and is used only by `org-vm`. `org` runs everything
in this repository and has no sudo. It is in the restricted `incus` group, which confines it to
an Incus project of its own (`user-2000`) and that project's bridge. `org up` needs no root and
refuses to run as root.

**Why the manager is not a machine of its own any more.** In the previous layout the manager was
a separate machine, to keep Python and Claude Code off the Mac and to put one more wall between
the chat and the person's files. The VM now does both. Inside it, org runs directly as the `org`
user, beside the containers it creates, with the ledger in `~/.org` and the dashboard as a systemd
user service (`org-dash.service`). The manager chat is confined by Claude Code's bubblewrap
sandbox and by permission rules, described below.

**Why containers, and why Incus.** An engineer needs a whole system: to install packages, run
services, and build and run containers, because that is what real projects' tests need. A Docker
container gives the first two and not the third without a nested engine. An Incus system
container is a Linux system with its own init, and with `security.nesting` it runs a Docker
daemon of its own. On the VM's btrfs pool a copy of the template is copy-on-write and took about
0.06 seconds when measured.

**The template.** `org-base` holds everything an engineer starts with, so starting a run is a
copy and nothing is installed per run. `org up` builds it: `incus init images:debian/13` with
nesting on, the network card's two security settings, then `incus/bootstrap.sh` run inside it as
root, then stop. The build is refused if a tool is missing or if a login is found in the result.
The template holds no login at all: a token baked into it would be in every engineer's container
and on the VM's disk. It is left stopped, because a copy of a stopped container is a clean copy
and a template that never runs cannot drift; `org up` stops one it finds running.
`org up --rebuild-template` builds a new one under a dated name and points `machines.incus.base`
at it, leaving the old one for the runs that were promised it. The template carries no Bazel and no
build cache settings: see "The build cache" below for why those are written at dispatch.

**The backend acts only on its own containers.** Every action of the Incus backend passes one
check first (`_own()` in `org/backends/incus.py`): the name must start with the configured
prefix (`org-eng-`) and be a plain container name. A template whose name starts with the prefix
is refused, since a run could then be asked to delete it, and a run record cannot point org at
another container. Three other parts of org name containers outside the prefix, on purpose: `org
up` creates the template and the build cache, and `org upgrade` creates and deletes a test
container (`org-test-<commit>`). A cache whose configured name starts with the prefix is
refused, as the template's is. All of it happens inside the `org` user's own Incus project.

**What makes a container a box.** Before an engineer starts, the backend's `vet` reads Incus's
own description of the new container, not anything inside it, and refuses the run if the
container is privileged, has any device beyond its root disk and its network card, shares a
directory from the VM, or has a network card without `security.port_isolation` (it could reach
the other containers) or without `security.ipv4_filtering` (it could take another address).
`machines.incus.allow_open` overrides the refusal. This is the check an engineer's container
passes; the build cache's container is deliberately not held to the isolation part of it (see
"The build cache"). The check runs at dispatch and nothing re-checks a container later. An
engineer cannot change those settings from inside its container; the `org` user could. `vet`
also reads the container's memory limit back, as the next paragraphs describe.

**The memory limit.** On 2026-11-24 the VM (11951 MiB, no swap) ran out of memory while three
engineers were building. The kernel killed a test run in one container and five processes
outside the containers, among them things the VM needs. Nothing bounded what a container could
take, so the walls above kept engineers from each other's files and addresses and not from each
other's memory. A hard limit per container moves a build that is too large to fail where it was
started, with the kernel killing a process in that container, and makes the sum of the
containers' memory something that can be arranged to fit.

The limit is `machines.incus.memory`. Absent or `"auto"`, each container gets
`floor((MemTotal - reserve) / slots)` in whole MiB. The division by slots is what makes the
containers fit together: the pool already says how many run at once, so the share is what is
left of the machine for each. `MemTotal` is the machine's, from `/proc/meminfo`, not a figure
written down, so a bigger VM gives bigger shares. The reserve, `machines.incus.memory_reserve`
(default `3GiB`), is the part kept for what the limit does not cover: the machine's services,
Incus's daemon, the manager chat and the dashboard, which run beside the containers and which
nothing limits. On the VM, 3072 MiB reserved: 4 slots give `2219MiB` each, 2 slots `4439MiB`.
`none` is no limit. A size (a positive whole number followed by `MiB` or `GiB`) is taken as
given, for an owner who accepts overcommitting; `org doctor` still warns when the limit times
the slots exceeds `MemTotal` minus the reserve. Anything else is refused with a sentence naming
the setting, because a typo that silently meant "no limit" would restore the failure it is there
to prevent. With `auto`, and no slot count or no readable `MemTotal`, there is nothing to divide
and so no limit, and `org doctor` says so. `org up --memory` sets the first setting as `--slots`
sets the slots.

A ledger with no `machines.incus.memory` gets the automatic limit after `org upgrade`; that was
chosen over leaving old installations unlimited, because they are the ones that failed. On a
small machine it is noticeable: 4 GiB and 4 slots give (4096 - 3072) / 4 = 256 MiB each, and
`org doctor` warns that is too little (it warns under 2 GiB). The owner lowers the slots or the
reserve, or sets a size. It applies to containers made after the upgrade; one already running
is left alone, since a limit that arrived under a build in progress could kill it, and a stopped
one gets the limit when `org resume` starts it: set, read back from Incus as `vet` reads a new
container's, and written into the run's facts so that the runner about to start tells the
engineer. A run dispatched before the upgrade carries the old release's runner, which does not
know the fact, so its container is limited and its engineer is not told.

The limit rides on the copy command itself: `incus copy` is given `-c limits.memory=<n>MiB`, so
the copy never exists, and never starts, without it. Setting it afterwards would leave a window
in which a container is unbounded, and a failure to set it would leave a container that looks
like any other. Incus's own default is a hard limit. Then, in the order "Before an engineer
starts" gives, `vet` reads Incus's description of the container, and refuses the run if
`expanded_config` (the container's settings with its profiles') does not carry exactly the limit
asked for, or carries `limits.memory.enforce=soft`, before any token is copied in. That reading
is of Incus's description, as the rest of `vet` is, and not of anything inside the container.
The refusal, and a refused copy, name `org config set machines.incus.memory none` as the way to
dispatch without a limit, so a project or profile that will not take the key does not leave the
owner stuck. The limit is stored in the run's machine record. `org upgrade`'s throwaway test
container gets the same limit, since a suite that needs more should fail there and not in an
engineer's container; the template and the build cache's container get none, because the
template runs only while it is built and the cache's memory is the reserve's to cover.

**What is killed first.** A hard limit alone chooses its victim by the kernel's score, and the
session (Claude Code) and the runner hold memory for the whole run, so a large build could cost
the run its session instead of its own compiler. With a limit in force the runner gives Claude
Code a wrapper through `CLAUDE_CODE_SHELL_PREFIX`, which Claude Code documents as running every
shell command as `<wrapper> '<command line>'`. The wrapper raises its own `oom_score_adj` to 500
and then runs the command, so the commands the session runs, and what they start, score higher
than the session and the runner, and the kernel kills a build step before the session. Raising
one's own adjustment needs no privilege; nothing is given to anybody, and the wrapper is not a
way to lower anyone's score. What it does not reach: containers the engineer starts through a
Docker daemon are that daemon's children, not the shell's. The engineer's prompt says how much
memory it has, that the limit is hard, that a compiler or test ending with `Killed` or
`terminated` is a reason to lower parallelism and not to run it again unchanged, and that
`/sys/fs/cgroup/memory.events` counts the kills.

**What the owner sees.** The kernel's count of kills for memory is read from outside the container
(see "A finished run's machine"), and every sync's snapshot also carries the container's own
`oom_kill` count, read from `/sys/fs/cgroup/memory.events`; the record's `oom_kill` is the larger of
the two, so a container with an older bundle shows it too. `org status` prints a `!` line under the table
(`! <run>: 2 processes were killed for memory in its container (limit 2219MiB)`), `org peek`
shows the same and `org status --json` carries `oom_kill`. The count includes kills in
containers the engineer runs inside its own, as the kernel counts a cgroup with everything under
it, so it can say more than the engineer's own shell did. A restarted container counts from
zero, so sync keeps the highest count it has seen and never lowers it: that kills happened in
a run stays visible, at the price of undercounting after a restart. `org doctor` has a row `memory per
engineer` (limit, slots, reserve, machine memory) and warns when there is no limit, when the
limit times the slots exceeds `MemTotal` minus the reserve, and when the limit is under 2 GiB.
`org pool` shows the limit on the Incus line.

**What the limit does not cover.** Everything outside the engineers' containers: the machine's
services, Incus's daemon, the manager chat, the dashboard and the build cache's container. The
reserve is the margin for them and nothing limits them. There is no swap, CPU, disk or
process-count limit.

**The network.** An engineer reaches the internet, to clone, install and call the model. It
cannot reach the Mac, the LAN, the VM itself or another engineer's container, and of the VM it
can only ask for an address and a name. The one exception is one port of the build cache's
container, described below. Two things hold the rest. The per-card settings above are Incus's,
set on the template and inherited by every copy; port isolation is what keeps one engineer's
card from another's. A firewall in the VM, installed by `org-host` and owned by root there, drops
what containers send to private, link-local and other non-public ranges and all of their IPv6.
An engineer is root in its container and can change neither. The firewall's rules name the
containers' bridges: they do not filter what the `org` user's own processes send.

**Credentials.** Exactly two tokens enter the VM. `org-vm` keeps them in the Mac's Keychain and
pipes them in at every start, into `/run/org`, a memory-only directory that the `org` user alone
can read.

| Token | What it is | What it can do |
|---|---|---|
| Claude | what `claude setup-token` prints | model requests only; documented as unable to fetch the account's claude.ai connectors |
| GitHub | a fine-grained token whose resource owner is one organization; Contents, read and write | read and push code there; it cannot create repositories, change settings or edit workflow files |

`org/secrets.py` is the only reader. Dispatch copies both into each engineer's container over
stdin, into that container's own `/run/org`, and the runner exports them to the session as
`CLAUDE_CODE_OAUTH_TOKEN` and `GH_TOKEN`; git asks `gh` for its password and `gh` reads
`GH_TOKEN`. Nothing reads `gh auth token` or performs a browser login any more, and `org login`
only reports what is in place: a login made in the VM would be the whole account, not the narrow
pair. `org doctor` fails on a GitHub token that is not fine-grained and on a
`~/.claude/.credentials.json`, which is what `claude /login` leaves. The git identity (a name and
an address) is copied from the VM's git config, where `org-vm` put it.

**Claude Code is locked down in both places, to different strength.** In the VM,
`/etc/claude-code/managed-mcp.json` holds an empty server map and the managed settings set
`disableClaudeAiConnectors`; root owns both, so neither the `org` user nor the chat can change
them. Every session org starts, manager and engineer alike, is given
`ENABLE_CLAUDEAI_MCP_SERVERS=false`, and the manager's settings deny every `mcp__*` tool. The
template carries the same two files. There the honesty is different: an engineer is root in its
own container and could undo them. What protects the account from an engineer is not those
settings but the token's scope, which is model requests.

**Before an engineer starts**, in this order: the container is copied and started; dispatch asks
it what it has (python3, git and Claude Code, or the run is refused; whether a Docker daemon
answers and how many cores, which the engineer is told); `vet` reads Incus's description of it;
the build cache is offered to it, if there is one; only then are the tokens and the git identity
copied in, the bundle unpacked and the runner
started. A dispatch that fails at any step removes the container it made, so a refused container
is never left running and never receives a token.

**The pool.** `machines.incus.slots` is how many engineers' containers may run at once. A run
that finds no free slot is recorded as queued, and `pump()` starts it when a slot frees. `pump()`
runs whenever something reads the ledger (status, the dashboard's loop), so the queue moves
without a scheduler process of its own. Placement is decided under a lock, so two readers cannot
hand out one slot twice. Slots are counted from the ledger, not from `incus list`, so a finished
run whose container still exists does not hold a slot. A run left in `provisioning` by a process
that died would hold its slot for ever, so `pump()` first reaps such runs: each records the
process that was setting it up (pid and boot id), and when that process is gone the run is
recorded as failed and what was made of its container is removed.

**Draining.** `org drain` writes a flag in the ledger (`drain.json`), under the same lock that
placement is decided under, and `dispatch.start()` reads it under that lock: once `org drain`
returns, nothing starts. Approved work is still recorded and stays queued, with the reason beside
it. Running engineers are not touched. `--wait` returns when no run holds a container, and
`org undrain` removes the flag and starts what queued.

**Restarts.** The tokens are in memory, so stopping the VM loses them in the VM and in every
container. Nothing on the Mac starts the VM by itself: `org-vm up` does, and hands the tokens in
again. `org resume --interrupted` then finds every run whose container is there and whose runner
is not, starts the container if it is stopped, copies the tokens in again, and restarts the
runner, which continues from its last recorded step. A resume of a single run does the same.

**Releases and upgrades.** `org up` installs the copy it is run from as a release,
`~/.local/share/org/releases/<commit>`, and moves the `current` link to it, remembering the one
before as `previous`. An upgrade is the moment code written by engineers starts running beside
the tokens and the ledger, so `org upgrade <ref>` makes it the person's decision on an exact
commit:

- The commit must be an ancestor of the origin's default branch. GitHub serves a commit pushed
  to any fork through the parent repository's address, so having a sha proves nothing; being
  merged does.
- The person is shown the commits and the files that changed since the release in use, with the
  files that hold the walls and the tokens listed apart, and types yes. Without a terminal the
  command refuses unless `--yes` is passed.
- The commit's own tests run in a throwaway container copied from the template. It holds no
  token and is not the `org` user, so code nobody has run yet does not run beside the ledger.
- Dispatch is drained, the commit is installed (which switches `current`), the dashboard is
  restarted so it runs the new release, and the drain is lifted. Engineers already at work are
  not waited for: they keep the code they were bundled with.

`org upgrade --rollback` swaps `current` and `previous` and restarts the dashboard. It moves
links only: it does not drain, and it does not undo anything the newer release wrote to the
ledger. The first install, `org-vm install <ref>`, is `org-host`'s and takes the ref it is given.

**The dashboard's port.** `org-dash.service` serves the page on the VM's loopback, port 8787,
and Lima forwards that one port to the Mac's `127.0.0.1`. Nothing else a process in the VM
listens on is forwarded, so a server an engineer starts does not appear on the Mac. Containers
cannot reach the VM's loopback, and the firewall leaves them no port of the VM but the ones
that hand out addresses and names. The cache's port is not a port of the VM: it is the port of
a container on the bridge.

**Backends.** Where engineers run is one interface (`org/backends/`): choose a machine for a run,
say how many may run at once, create it, vet it, reach it, remove it.

| Kind | Provisioned by | Reached by |
|---|---|---|
| `incus` | a copy of the template, one per run | `incus exec <name> -T -- bash -c …` |
| `ssh:<name>` | exists already | `ssh <host> bash -c …` |
| `local` | nothing; off by default | `bash -c …` |
| `docker[:node]` (plugin) | `docker run` on a pool node | `docker exec` |

A transport does one thing: run a bash script on a machine with optional bytes on stdin. Files
and tokens travel on stdin, files as base64, so nothing depends on scp or a binary-clean channel
and no token is ever on a command line.

The ssh and local backends are still present and secondary. An ssh host is set up by its owner:
of the two tokens dispatch gives it the Claude one, if asked, and not the other. The VM holds no
key to reach such a host unless the person puts one there, and none of the walls above apply to
it.

**The Docker plugin is unmaintained in this layout.** The first container version made the
manager and every engineer Docker containers from one image, with the manager holding the Docker
socket. The backend and its unit tests are still here (`org/backends/docker.py`), but the
host-side script `docker/up.sh` has not been updated for the token changes: it still calls
`gh auth token` and gives the manager a whole GitHub login, and `org up` no longer leads to it.
It is recorded here as history, not as something that works.

## A finished run's machine

**The rule.** The owner decided on 2026-11-24, seeing ten finished engineers still running in their
containers, that a finished engineer leaves no machine behind. The record of a run is what the
engineer made explicit (its report, plan and result, which the mirror already holds) and what it
pushed to the remote. When a run's record is `final` and its state is `done`, `partial`, `failed` or
`stopped`, the next `pump` deletes its machine through the backend's `remove`. `pump` runs whenever
something looks: `org status`, `org pump`, and the dashboard's loop every ten seconds. It does so
whether or not the run left warnings, belongs to a plan, is being reviewed, is paused or is held. A
run that is not `final` is never deleted this way, and the record is read again under the lock before
the deletion, so a run that `org resume` made not final in between is left alone.

**Why no grace period and no confirmation.** A terminal run cannot be continued: the runner started
again on one says it has finished, and a retry is a new run in a new container. The engineer is told
that only its report and what it pushed survive. A grace period would keep exactly the state the
owner decided against, and a confirmation would leave containers standing with nobody asked. The
count behind a warning about unpushed work is wrong in the direction of keeping things (it counts
worker branches that were rebased into the pushed one), so a warning does not stop the deletion; it
is shown afterwards as what was deleted with the machine, and the runner also writes into
`status.json` what it was (branch names, up to 20 commit subjects, up to 50 paths per clone), which
`org review` prints. The deletion is not undoable.

**The setting.** `machines.after_finish`: absent (the key is not there) or `delete` is the rule above; `keep` is the old
behaviour, where nothing is deleted until `org machine rm <run>`. Any other value is treated as
`keep`, because a mistyped setting must never delete, and `org doctor` has a row that says the value
is not understood. Changing `keep` back to `delete` deletes everything finished at the next pump.
There is no cut-off date: the first pump after `org upgrade` deletes the container of every run that
was already finished, after one last measurement of each. An owner who wants those containers sets
`keep` before upgrading.

**The order.** For one run: a last measurement; the last 5 MB of the runner's `runner.log`, copied
into the mirror as `runner.log` (text, under that one fixed name, bounded, because it comes from the
engineer's machine); the deletion; then `machine_removed_at` on the record. A measurement that
cannot be taken (the container is stopped, Incus does not answer, a file is missing) is noted and
the deletion goes ahead: where the two pull apart, the measurements lose. A container Incus no
longer knows, or one that never existed (a run stopped while queued), is marked removed without an
error and without a warning. A machine org did not make (`remove` returns `None`: ssh, local) is
not touched and not marked.

A run stopped while it was queued had a machine chosen and never made: it is marked removed with
no measurement and no call to a backend, since a name that was never made may be another's. The
machines of several finished runs are measured and deleted side by side, eight at a time, so that
the first pump after an upgrade waits for the slowest of them and not for their sum. An answer from
Incus that is not JSON is an error, not "no such container": on that word a run is recorded as
having no machine.

**The failure path.** A deletion that fails is recorded on the run as `machine_remove_error`: the
error's text, when, and how many tries. It shows as a warning in `org status` and in the run view,
is tried again on later pumps no more than once a minute, and never raises out of `pump`, never
keeps a queued run from starting and never keeps another run's machine from being deleted in the same
pump.

**The lock.** The measuring and the deletion hold the run's own lock, the one sync holds, so a
dashboard loop and an `org status` that pump at the same moment end with one record: one of them
calls Incus, or the second finds the container gone, and neither loses the other's write. `org
resume` and `org machine rm` take the same lock now. A final run's record has one more writer, the
plan's tick, which writes it under its own lock (`runs/.graph.lock`) and not the run's. A deletion
takes seconds, so what it learned is not saved onto the record as it was read before: under the
tick's lock the record is read again and only the machine's fields are laid over it (`telemetry`,
`oom_kill`, `machine_removed_at`, `machine_removed_by`, `machine_remove_error`). Without that, a
review a tick had just marked started was started a second time. The tick's lock is waited for
five seconds at most and held for that one reading and writing; when a tick holds it for longer
(it is setting up a machine), the same is done without it. A ledger that never had a plan has no
tick and gets no such lock file.

**The walls that did not move.** Only `Backend.remove` deletes. It passes the name through `_own`,
the check of the engineers' prefix, and answers that the machine was already gone when Incus does
not know the name. The template and the build cache's container are outside the prefix and out of
reach of this code. No other place calls `incus delete`.

**What is measured, and from where.** While a run holds an Incus container and is not final, org
reads from outside the container at most once a minute: Incus's description of it (`incus list
--format json`, which gives the state and the disk the pool reports) and the kernel's files for it,
`/sys/fs/cgroup/lxc.payload.<name>/` or, where the container is in a project of its own,
`lxc.payload.<project>_<name>/`: `memory.current`, `memory.peak`, `memory.max`, `pids.current`,
`memory.events` and `cpu.stat`. This is one `incus` call per run per minute, and a failure of it is
noted and never raised. It is read from outside because the inside is the engineer's to change and
dies with it; nothing from inside the container, and nothing an engineer's machine sends, is trusted
for these numbers or chooses a path that is read or written. Only "the runner was alive" comes from
the last sync. This needs no bundle of any age in the container, so it works for runs dispatched
before it existed. Docker, ssh and local machines are not measured, and where the summary is shown
for one the text says so.

**The file and the summary.** Each measurement is one JSON line appended to
`runs/<run>/machine.jsonl`, beside `run.json` and not in `mirror/`. A value that could not be read
is null or absent, never guessed and never zero. The run's record carries `telemetry`, kept up with
each measurement and still there after the deletion: the largest peak of memory, the limit, CPU
seconds, kills for memory, restarts seen, the disk used where the pool reports it, how many
measurements, and the first and last time. A cumulative counter (CPU seconds, kills for memory) that
is lower than in the measurement before means the container restarted, as a stopped and started
container counts from zero: the totals add the parts and the restart is counted. Another start
time in Incus's description counts as a restart too. The summary is worked out from the whole file
each time, so a write of the record lost to another writer is made good by the next measurement.
`org review <run>`
prints the summary as a block, with CPU as seconds and the average number of cores they come to over
the run's elapsed time; `org peek` as a line; `org status --json` as `telemetry` on the run; the
dashboard's page for the run shows it. A run from before this has no measurements and says so in one
line.

**After the deletion.** `org diff`, `org ls`, `org cat` and `org exec` say the run has no machine to
reach (removed); `attach` is null; `org resume <run>` refuses in one sentence, saying the container
was deleted when the run ended and a finished run cannot be continued. The review between two specs
of a plan works from the report, `org review`'s output and the remote. What stays: the build cache's
container, the template, old releases and old templates. `org machine rm` remains for what is left
(a run under `keep`, and a run that is not final, with `--force`).

## The build cache

**Why it exists.** An engineer's first Bazel build in a fresh container compiled everything from
nothing. For `org-004/solar-bay-assembly` that was 611.7 s with an empty cache against 148.8 s
from a filled one, measured in that project's run 004: about ten minutes of every run of a
project that needs it. Each container is a copy of a template that cannot hold a project's build
results, so the results have to live somewhere that outlasts a container and that every
engineer can reach.

**What it is.** A container of its own beside the engineers', `org-cache`
(`machines.incus.cache.name`), running `bazel-remote` 2.6.4
(https://github.com/buchgr/bazel-remote): one program that serves Bazel's remote cache over gRPC
from one directory, `/var/lib/bazel-remote`, and keeps it under `machines.incus.cache.max_gb`
gigabytes (default 20) by dropping what was used longest ago. It listens for gRPC on
`machines.incus.cache.port` (default 9092, the server's own default) and binds its HTTP port to
the container's own loopback, where no other container can reach it. It runs as a service of a
user `bazel-remote` that is not root and starts with the container; the container has
`boot.autostart`, so it returns when the VM restarts. It runs no container of its own, so it is
made without nesting.

**Who makes it.** `org up`, as the `org` user, when it is missing: from `images:debian/13`,
unprivileged, with no device but its root disk and its network card. `incus/cache.sh` is run
once inside it, as `incus/bootstrap.sh` is run inside the template. It downloads the pinned
release from GitHub for arm64 or x86-64 and installs it only if its SHA-256 equals the one
written in the script, so a changed download installs nothing. `org up` leaves an existing cache
as it is and starts it if it is stopped. If the server does not come up, `org up` removes the
container again, so that the next `org up` makes it afresh and does not take a broken one for the
cache, and ends with an error after the rest of its work: nothing else depends on the cache, and
engineers build without it in the meantime. `org down` leaves it running with what it holds.
Only Incus machines are offered it.

**Its network card, and why.** The card has `security.ipv4_filtering`, as an engineer's has, so
the cache cannot take another address, and its IPv6 is treated as the template's is. It does not
have `security.port_isolation`, on purpose. Incus's port isolation keeps a card from other cards
that also have it set: an isolated card cannot reach another isolated card, and the engineers'
cards are isolated. A card without it can be reached by an isolated one. That was confirmed with
real containers in a nested Incus 6.0.4: an engineer's container connects to the cache, and does
not connect to another engineer's. It follows, and was also observed, that the cache container
can open connections to engineers' containers. That is why it holds no login, token or key,
why none is ever copied into it, and why nothing but `org up` ever enters it. The server asks
for no credential either.

**What an engineer can now reach that it could not.** One port (gRPC, 9092 unless changed) of
one container, `org-cache`, on the VM's bridge, if the VM's firewall lets it through. Nothing
else is newly reachable: not the VM, not another engineer, not the cache's HTTP port. The server
listens on that port over IPv6 as well as IPv4; the settings engineers are given use its IPv4
address.

**Engineers use each other's build and test results.** Every engineer writes to the cache and
every engineer reads from it, so a result one engineer's build uploaded is used by another
engineer's build and by its tests. The server checks a stored file against its hash (the
content-addressed store is keyed by SHA-256) and checks that a stored action result is a
well-formed record. It cannot check that the stored result of an action is the true one. An
engineer that uploads a wrong output for an action poisons that action for every later build
that has the same inputs, until the cache is emptied. There is no credential, no TLS and no rule
about who may write: what protects the server is only that nothing but containers on the VM's
bridge can reach it. The owner accepted this on 2026-11-24 for the sake of build time. CI does
not use this cache, so what CI builds is not affected by it.

**How an engineer's container is told.** The template cannot be told, because the cache's
address is not known when the template is built, so dispatch does it, after `vet` has passed and
before the tokens are copied in (a container that fails `vet` is refused first and gets no file).
If the cache container exists and is running, dispatch reads its IPv4 address from Incus's
description of it, and from inside the engineer's container tries to open a TCP connection to
that address and port, giving up after 3 seconds. It prints one line either way: `build cache
reachable at <address>:<port>`, or `build cache not reachable at <address>:<port>, so builds will
not use it` with what may be in the way, which is the VM's firewall or port isolation on the
cache's network card. The run's record keeps which, as `build_cache` (`state`, `address`,
`port`). Only when the connection was made does dispatch write Bazel's system-wide settings
file, `/etc/bazel.bazelrc`, in the engineer's container. It carries a comment saying who wrote
it and why, and exactly four settings: `build --remote_cache=grpc://<address>:<port>`,
`build --remote_upload_local_results=true`, `build --remote_local_fallback` and
`build --incompatible_remote_local_fallback_for_remote_cache`. Nothing else: no disk cache, no
limit on cores, nothing about any project, and a project without Bazel never reads the file.
Writing only after the connection was tried is what keeps a build from waiting on a cache it
cannot reach.

The last two settings together make Bazel 9 build locally when the cache stops answering
part-way. With the first of them alone the build fails ("Failed to query remote execution
capabilities"), which was tried with Bazel 9.3.0. Bazel 9.3.0 then succeeds without printing a
warning: the only sign is a wait, about 3 s when the connection is refused and about 20 s when
the address does not answer.

That last setting is also the file's weak point. It exists from Bazel 8.6.0 on: 8.5.0 and 7.7.3
do not know it, and a Bazel that meets an option it does not know in an rc file stops
("Unrecognized option"), whatever the command. An rc file cannot say "if this version knows
it", and dispatch cannot know which Bazel a project will fetch. So in a container that reached
the cache, a project pinned to a Bazel older than 8.6 fails every build until
`/etc/bazel.bazelrc` is removed there, which the file's own comment tells the engineer to do.
Leaving the setting out would be worse for the project this was built for: Bazel 9.3.0 without
it fails the build whenever the cache stops answering.

A stopped cache gets a line saying so; a cache container that is not what `org up` makes (it is
privileged, or has a device beyond its disk and its card) is not used, and the line says why; a
missing cache prints nothing and the run goes on as before. The checks on an engineer's container
(`problems()`) are unchanged. When a stopped engineer's container is resumed the same is done
again, so a cache whose address changed is found, and a settings file org wrote earlier is
removed if the cache is there and no longer reachable. A failure anywhere in this never fails a
dispatch: whatever goes wrong while the cache is looked at or offered is caught there, said in
one line, and recorded as `error`.

**The backend does not manage it.** It acts only on names with the engineers' prefix, exactly as
before, and only reads Incus's description of the cache (`incus list`). It never creates, enters
or deletes the cache on behalf of a run.

**What is not known until the owner tries it on the VM.** The VM's firewall is made by
`org-host`, the owner's separate repository; root owns it and nothing here can change it. It
"drops what containers send to private, link-local and other non-public ranges". Whether it also
drops traffic from one container to another on the same bridge, which is switched and not
routed, could not be tried anywhere but on the VM. If it does, dispatch says "not reachable" and
builds go on without the cache until `org-host` allows exactly this: source the engineers'
bridge, destination the cache container's address and gRPC port, TCP, nothing wider. The first
dispatch after `org upgrade` and `org up` shows which it is.

**Seeing it.** `org doctor` has one row for the cache, from `incus list` alone: missing (fix:
`org up`), stopped, or `running at <address>:<port>`. It fails the row if the container is
privileged or has a device beyond its disk and its card. When the cache is switched off the row
says `switched off (machines.incus.cache.enabled)` and is not a failure. `org pool` prints one
line, `build cache: org-cache running at <address>:<port>` (or stopped, or missing).

**Switching it off, and emptying it.** `org config set machines.incus.cache.enabled false`: `org
up` then makes no cache, dispatch looks at none, asks Incus nothing about it and writes no
settings, and the installation behaves as before the cache existed, but for the row in `org
doctor` that says it is switched off. A cache container that already exists is left where it
is, and so are the settings in engineers' containers dispatched before, which nothing reaches
into: `incus delete -f org-cache` removes the container, and their builds then go on without
it. To empty it,
delete it and run `org up`, which makes a new, empty one. That is also how a changed port or size
takes effect, since an existing container is not reconfigured. Engineers dispatched in between
build without the cache.

## The spec

A spec is one Markdown file: a flat `key: value` header for the dispatcher (where to run, caps,
delivery) and sections for the engineer (Intent, Context, Requirements, Out of scope, Acceptance
criteria, Constraints, Alternatives considered, Open questions).

`org spec check` enforces what a program can: ids on requirements and acceptance criteria, at
least one MUST, no open questions, no template text left, no placeholder or unreachable
repositories, no repository outside the organization named by `github.owner`, well-formed caps.
The rest is the manager's job, and its persona is written around one fact: an engineer cannot ask
anyone anything, so every ambiguity left in a spec becomes a guess.

**Why one file.** The person approves exactly what the engineer reads. A separate metadata file
would be a second thing to review and a place for the two to disagree.

**Why a hash.** The manager edits the draft during the conversation. Without the hash, "approve"
means "approve whatever the file says by the time the command runs". With it, a draft that
changed after the person read it cannot be approved by accident.

**Why approval is a permission prompt.** The manager session runs under a rule that makes
`org approve` always ask. The model can propose the command; only the person can let it run.
In the layouts before this one that was checked against the real harness, in the default and
auto permission modes: with nobody to answer, the command is refused.

**Why the organization is checked here.** The engineers' token pushes to one organization and
nowhere else. A spec that names a repository elsewhere would send an engineer to work it cannot
deliver, so it is refused before approval, with the two ways out: move the repository in, or
name it under Context as something to read.

**Why `after` is a line in the header.** Work that builds on other work was sequenced by the
owner coming back to the chat when a run finished. The header takes `after: 008, 9`, a list of
spec ids or numbers, and `org spec check` refuses a spec that names itself, one that does not
exist, or a set whose `after` lines form a cycle. It stays one spec per file and one approval per
text: approving a set of dependent specs is still one approval for each, and the line only says
what the run waits for. A spec without it is checked, approved and dispatched as before. See
"Plans".

## Plans

A set of approved specs joined by `after` is a plan. There is no object for it: `graph.json` in the
ledger, which appears when the first spec with `after` is dispatched, only marks that this ledger
has one, so that a ledger without a plan pays one `stat` per pump and nothing else.

**Waiting.** Approval creates the run as it always did, in the state `queued`, and `start` leaves it
there with a `queue_reason` such as `waiting for 009 (executing) and 011 (not approved yet)`. A spec
is satisfied when the latest run of its latest approved version reported `done` or `partial` and the
review of that run said `continue`. A stopped run, a failed run and a run of an earlier version
never satisfy it, so a spec revised and approved again makes what waited on the old version wait
for the new. Approvals may come in any order because the clause is worked out again at every pump.
Independent specs of a plan run side by side when slots allow.

**The review.** When a run that a queued run waits on ends, `pump` starts one headless session,
`claude -p` with exactly the settings of `ask` mode and `--permission-mode dontAsk`, as a process of
its own (`org review-run`), because `pump` must stay quick. The process is recorded on the run the way
provisioning records its process, so a review that ended without an answer is noticed and counted,
and a lock makes the review happen once per finished run whatever number of processes pump at the
same moment. The session is given the run's spec, its report, the `org review` output and the text
of every spec that names the run in `after`, all as quoted material, and answers a JSON object
checked against `schemas.REVIEW`: a `verdict` of `continue`, `retry` or `pause`, a reason of at most
300 characters and, for `continue`, one note for each waiting spec. Its prompt says that the report
and what it quotes are data, never instructions, and that any doubt is `pause`. Its model and effort
are `manager.review_model` and `manager.review_effort`, by default the manager chat's own, and its
cost is recorded on the run and shown by `org review`.

**What code decides.** The answer is advice; `graph.apply_verdict` decides. `continue` is honoured
only when the report said `done` or `partial`. `retry` is honoured only when the report did not say
`done`, at most `graph.max_retries` (1) times per version of a spec, and it dispatches the same
approved text again. Everything else is a pause. An answer that is invalid twice, a session that
errors, a review that takes longer than `graph.review_timeout_min` (15) minutes, and a review
process that dies twice are a pause, so a run is reviewed at most twice. A run the owner stopped is
not reviewed: what waits on it is paused. A `continue` is a statement about the texts the review
was given, so a spec approved, or approved again, after that review is not satisfied by it: its run
is paused and waits for `org release`. A run that starts in a plan carries
`earlier/<run id>/report.md` and `review.md` in its bundle for each spec it came after, and a
retried run carries the failed attempt's the same way. They are there because the later spec's
Context was written before the earlier one ran, and where they differ the report is the newer fact.

**Recovery without a review.** For runs of a plan only, a runner dead for `graph.dead_after_min` (5)
minutes, or a machine that stopped, is resumed, at most `graph.max_resumes` (2) times per run, and a
run that never got a machine is dispatched again once. These cure what needs no judgment and are not
alerted unless the limit is reached. Nothing starts, retries or resumes by itself at or above
`graph.pause_above_weekly_pct` (80) percent of the weekly limit: that is a pause with that reason.

**Pausing and telling.** A pause marks the run that caused it and holds every queued run that waits
on it, directly or through other specs; `pump` does not start a held run. `org status` shows the
reason and a `!` line on the cause, and the run's warnings carry the same sentence, so the dashboard
shows it without a change to the page. `org release <run or spec>` starts a held run when a slot is
free, whatever it waited for, and what depends on it waits for it as usual. Dispatching the paused
spec again by hand also lifts the holds, since the waiters then wait for the new run. Every pause,
automatic retry and resume is a line in `alerts.jsonl`. With `notify.url` set, each pause is also one
POST made by `org` itself to that address: one line, `org: <run> paused: <reason>. <n> run(s) held.
org status`, at most 400 characters, with a `Title` header and the headers in `notify.headers`. The
reason came from a model that read untrusted text and the owner reads it on a phone, so link-like
text and control characters are removed in code. A send that fails is tried on later pumps, three
times in all, then recorded as failed and shown as a `!` line, and it never stands in the way of
anything else. `outbox.json` holds what is on its way. When the last run of a plan ends with nothing
held, one message says so. `org notify test` sends a test message and `org doctor` has an `alerts`
row.

**Why `pump` and no daemon.** `pump` already runs every ten seconds under the dashboard and at every
`org status`, and it already starts queued runs. A scheduler would be a second thing to keep alive
and the first thing to die quietly. The cost is that an alert is written at the next pump after the
pause, not at the instant of it, and that with the dashboard stopped and nobody running `org status`
a plan waits, as every queued run does.

**Why a POST and no mail.** The VM has no mail transfer agent, and sending mail directly would put a
mail account's password on the machine that holds the two tokens. A POST to an address the owner
configures needs no secret of org's: a push service that takes one can forward it to a phone or to
mail, and which one is the owner's choice, kept in `config.json`.

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
| the VM restarts | `org resume --interrupted` starts the container, copies the tokens in again and restarts the runner |
| the engineer changes `spec.md` | the approved text is restored and the change is recorded as a warning |

**Cost accounting.** The CLI's `total_cost_usd` is cumulative over a session, across resumed
invocations; the per-turn figure is `model_usage_since_last_result`. The meter uses the per-turn
figure whenever the field is present, and a present but empty field (`{}`) means the turn used no
tokens and costs nothing: Claude Code prints that for an API error and for a wake-up with nothing to
do. Without the field the meter differences the cumulative per-model `modelUsage`, and without that
too it differences `total_cost_usd` against the last one seen for the session (the whole figure for
a new session, or when it went backwards). The last of each is kept in `state.json` (`cumulative`,
`cumulative_total`), so a restarted runner differences from where it stopped; a `state.json` without
`cumulative_total` is read as having none. One `claude` process may print several results (run 013's
milestone turn printed six: its own, two API errors, three wake-ups for background work); each is
closed with its own cost, and `Runner.invoke` keeps the last as the turn's result. Inside a turn,
spend is estimated from streamed token counts with a price table and corrected at the turn's end.

Until 2026-11-25 the meter took an empty per-turn field for a missing one and charged the session's
whole `total_cost_usd` again, which stopped run 013 at its $150 cap with $33.06 spent. As an alarm
for a fault of that kind, the runner warns once per run when `confirmed_usd` less `estimated_usd`
differs from the sum of `by_model` by more than 1% and more than $0.50: both are summed from the same
results, so they agree unless something was charged that no model spent.

## Status, sync and the page

The runner rewrites `status.json` every few seconds. Sync is one read-only command per run,
`org engineer snapshot`, which returns status, plan, report and the next slice of the event log
as one JSON document. The manager's side writes that into `runs/<run>/mirror/`.
When a run's machine is deleted, `mirror/runner.log` is added: the last 5 MB of the runner's log. What the machine measured is not in the mirror but in `runs/<run>/machine.jsonl`.

**A snapshot is input from outside.** It comes from a container where a session runs with every
permission bypassed, so sync takes from it only what a runner writes: the files named in
`runner.SNAPSHOT_FILES`, by their exact names, and a state only if it is one a runner has. A run
that is queued, being provisioned or failed at dispatch is not synced at all.

**What a review of sync found.** Sync used to write any file name a snapshot returned into the
mirror. A name such as `../../config.json` escaped the mirror and landed in the ledger, and
`config.json` names modules that are imported. An engineer could so have put text of its
choosing into a file the `org` program acts on, beside the tokens. Sync also copied the run's `state` unchecked, so a snapshot
that said `queued` would have handed the run to the dispatcher again, and one that said
`provisioning` would have held a slot for ever. Both are fixed as described above, and a unit
test sends such a snapshot. The text an engineer writes in its status and report is still shown
on the page and read by the manager chat; it is data there, not instruction.

`status.collect()` builds one document from the ledger and the mirrors. The terminal table, the
page and the manager's question mode all render that document, so they cannot disagree.

The page is one self-contained HTML file. It can approve a spec and stop a run, so every API
request needs a token (in the URL after `#`, where it is not sent to servers or logged) and
requests whose Host or Origin is not this machine are refused. It listens on the VM's loopback
only, which is what Lima forwards to the Mac's localhost and nothing else reaches.

**A web app in an engineer's container** can be viewed through the dashboard; this is being
added now, behind `dash.view`, which is off by default, and no person has exercised it yet.
`org view <run> <port>` and a control on the page open the server at an origin of its own,
`http://v-<id>.localhost:8787/`, relayed through `incus exec` into the container's loopback,
with a single-use ticket exchanged for a host-only cookie and a restrictive
Content-Security-Policy added. A page an engineer serves is an untrusted web page running in the
person's browser, and on macOS 15 it opens in Chrome and Firefox only, because Safari does not
resolve `*.localhost` there.

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
It runs as the `org` user, the same user that holds the tokens and drives Incus, so what stands
between the chat and that power is all here.

| Layer | Stops | Enforced by |
|---|---|---|
| shell sandbox | shell writes outside the desk; shell reads of the token directory, the other places a login could be, and Incus's sockets; network beyond an allow list | the operating system (bubblewrap) |
| `blockWritesOutsideWorkingDirectories` | the file tools writing outside the desk | the harness |
| deny rules | edits to approved specs, run records and `config.json`; reading the token and login paths; `incus`, `sudo`, `su`, `docker`; every `mcp__*` tool; pushes and other commands that change GitHub | the harness |
| ask rules | approve, dispatch, stop, resume, exec, `machine rm`, drain, undrain, upgrade, up, down, `config set`, `release`, `notify`; ssh | the harness, by asking the person, in every permission mode |
| no GitHub token | anything on GitHub that needs a login | the session's environment is stripped of one, and the file is hidden by the two layers above |
| managed settings | MCP servers and claude.ai connectors | root-owned files in `/etc/claude-code`, and `ENABLE_CLAUDEAI_MCP_SERVERS=false` at launch |

`org` runs outside the sandbox, because it must write the ledger and reach the containers, and is
governed by rules instead. Nothing else is excluded.

**The exemption holds only for a command that is `org` and nothing else.** In a pipe, or after
`cd … &&`, the harness sandboxes the whole line, and an `org` command that writes then fails. A
sandboxed session is told this in its prompt.

**The manager has no GitHub login, by decision.** In the previous layout the chat's `gh` ran
outside the sandbox with the person's login, and git inside the sandbox had none, so private
repositories arrived through `org clone`. The first real conversation showed the cost: asked for
the person's repositories, the manager ran `gh repo list | grep`, which the harness sandboxed as
a whole line, read "not logged in", and reported that it had no GitHub access. A read-only token
for the manager was the fix on the list. The fix taken is wider: every repository is public, so
the manager reads them with no login, `gh` is no longer excluded from the sandbox, and the
session's environment is stripped of any GitHub token. `org repos` lists the organization's
repositories through GitHub's public API. `org clone <owner/name>` takes a repository name or
URL and nothing else, so it cannot be turned into another git invocation, runs git with no
credential helper, clones into `desk/scratch/<name>`, fetches when the copy exists, and points
the copy's push URL nowhere. A change on GitHub made by the chat is therefore not something a
deny rule has to catch: there is nothing to make it with.

**The review has no mode of its own: it runs with the settings of `ask`.** The headless session between two specs of
a plan (see "Plans") gets what the dashboard's question box gets: reading, the read-only `org`
commands (`org graph` among them), everything outward denied, `--permission-mode dontAsk`, so a
command that would ask is refused. `release`, `notify`, `review-run`, `approve`, `dispatch` and `stop`
are not among what it can run. The chat may look at plans with `org graph` and may not release one.

**What the chat can still reach over the network** is the sandbox's allow list
(`manager.network_allow`: `github.com`, `*.github.com`, `*.githubusercontent.com`) from its
shell, and the web through its search and fetch tools.

**The Claude token is in the chat's own session.** The session makes its model requests with
it, so it is in that process's environment. Hiding `/run/org` keeps the chat from the GitHub
token and from the Claude token as a file. Whether a shell command the chat runs can see the
environment variable depends on Claude Code and was not checked; the token it would find makes
model requests and nothing else.

**What the chat must not reach inside the VM** is Incus: whoever can write to its socket can
enter every engineer's container, where both tokens are. The sandbox hides the sockets, the deny
rules refuse `incus` (and `sudo`, `su` and `docker`), and `org exec`, which can enter a
container, always asks the person. The deny rules match the form a model normally writes and are
a guard against an overeager manager; the sandbox is the wall. The live check knocks on a
stand-in Unix socket to prove the chat cannot speak to one, and that check has not been run in
this layout yet.

**The sandbox is the mechanism, so it is required when it exists.** `org talk` probes whether
the sandbox can start. If it can, the session is told to refuse to run without it. If it cannot,
the session is started in the ask-first permission mode and says so. The reason is a live
finding: with no sandbox and Claude Code in its auto mode, where a classifier answers most
permission prompts, the classifier allowed a `git branch` in a repository outside the desk. A
guarantee that depends on a classifier's judgment is not a guarantee, so without a sandbox the
prompt goes to the person.

**In the VM the sandbox needs nothing special**: bubblewrap can make its namespaces for the
`org` user there. It can inside an engineer's container too, which matters for an engineer that
works on org itself.

**`manager.direct` takes most of this down, by the owner's decision.** The confinement above
was built for an owner who approved every spec and wanted a chat that could change nothing. On
2026-11-25 the owner of the first installation decided otherwise: "I just care that you don't
run arbitrary code on my host machine. Everything else is game", "incus containers are helpful
for you to divide up your own environment (the VM, which you essentially own) as you see fit.
Not a security safeguard", and, for the work itself, "you should just try to edit these things
yourself and i'll approve every command". The first try showed what stood in the way: a CI
file was changed in a scratch clone within minutes, and the push was refused by a deny rule
that no prompt can lift, from a shell that had no login. So there is a switch, off by default.
With `manager.direct` true, the chat (`talk` mode only; `ask` and the review are unchanged):

| What changes | Why |
|---|---|
| no shell sandbox, and the ask-first permission mode | a sandboxed shell has no login and cannot write outside the desk; without it every shell command that is not a read asks the person, which is the control they asked for |
| `OUTWARD`, `incus` and `docker` are no longer denied | pushing is the point, and containers divide the machine for the manager |
| `blockWritesOutsideWorkingDirectories` is off | a file outside the desk asks instead of being refused |
| the session gets the organization's GitHub token as `GH_TOKEN`, and a credential helper for github.com through `GIT_CONFIG_*` | the token engineers already hold; it reaches one organization; nothing is written to the user's git configuration |

What stays: `sudo` and `su` are denied, because root owns what keeps the VM from the owner's
own computer, and that wall is not this repository's (`org-host`); the logins are unreadable to
the file tools; approved specs, run records and `config.json` are not edited by hand; `mcp__*`
is denied; and approve, dispatch, stop, exec, upgrade and the rest of `HUMAN_COMMANDS` ask.
What it gives up, plainly: with no sandbox, a shell command the person approves can read the
tokens' files, reach Incus and with it every engineer's container, and reach any host on the
network. The person's answer to each prompt is the only check, and a page or a package that
misleads the session could lead it to ask for something harmful in words that look harmless.
No live check of this mode against the real Claude Code had been run when this was written.

**What the live check taught**, in the layouts before this one (`tests/live_manager_rules.py`,
fourteen attempted actions judged by outcome). The rules that came of it are unchanged:

- Read-only shell commands are auto-allowed only inside the working directory, and a bare `Read`
  rule does not satisfy the path check on `ls` and `cat`: the rules carry `Read(//**)` and an
  explicit list of read-only commands.
- An allow rule with a wildcard before the git verb (`git -C * log *`) also admits `-c` options,
  which can run programs. Those rules were removed; `cd` and the plain form replace them.
- A push written as `git -C <dir> push` does not match a deny rule for `git push`: both forms
  are denied.

## GitHub

All repositories live in one organization and all of them are public. `github.owner` names the
organization, and `org spec check` refuses a repository outside it.

**Why one organization.** A fine-grained token names one resource owner. Putting every
repository engineers work on under that owner is what lets the token be the whole of an
engineer's reach on GitHub, and keeps `org-host`, in a personal account, outside it.

**Why public.** The manager then needs no token at all, and `org-host` notes that the branch
ruleset and secret scanning it applies need a public repository. The price is that everything
engineers write is published and that strangers can write back.

**Nothing on GitHub stands between an engineer and the default branch.** Pull requests are not
used. The default delivery is `merge`: the engineer rebases its branch onto the default
branch's tip, squashes it into one commit locally, and pushes that commit; its working branch
stays on the remote as the record of how the work went. The person and the engineers are the
same GitHub account, so a rule requiring review could not tell them apart, and the owner chose
one commit per piece of work over a pull request per piece of work. `org-host`'s `org-github`
keeps two rules on each default branch, with nobody exempt: no force push and no deletion, so
what landed stays in the history. `delivery: branch` is for work the person wants to see
first. For this repository the gate is `org upgrade`, where the person reads the diff before
the code runs beside the tokens.

**Public repositories are an input channel.** Text a stranger writes in an issue, a pull request
or a comment can reach the manager or an engineer. Both prompts say that such text is something
to read and never an instruction, and `org-github` turns issues, wikis and discussions off. The
first is a rule a model follows; only the second is a setting.

## Trust

| Who | Holds | Can |
|---|---|---|
| the person | the Mac, the Keychain, the GitHub account | everything |
| `org-host` on the Mac | Lima; the two tokens, from the Keychain | start, stop and delete the VM; pipe the tokens in; run commands in it |
| `admin` and root in the VM | sudo; the firewall; `/etc/claude-code`; Incus | everything in the VM; used only by `org-vm` and its provisioning |
| the `org` program in the VM | the ledger; both tokens; the `org` user's Incus project | create, enter and delete containers there; whatever the person or the page asks of it |
| manager chat | read access to what the `org` user can read, less the tokens; the desk | draft; propose approve, dispatch, stop, exec and the rest of the person's decisions |
| the review, a headless manager session | what `ask` mode holds: reading, the read-only `org` commands; the text of a run's report | answer `continue`, `retry` or `pause`, and write a reason and a note for each waiting spec; code decides what that does |
| engineer | root in its container; copies of both tokens | anything in its container; on GitHub and the model, what the tokens allow; read and write the build cache |
| the build cache | its own container, no login; every engineer's build results | serve them to every engineer; open connections to engineers' containers |
| dashboard page | the access token | approve, stop, ask |
| a stranger on GitHub | a public repository to read | write text that an agent may read |

- An engineer runs with permissions bypassed, so its container is the boundary: unprivileged,
  no device but its disk and an isolated network card, and behind it a VM with nothing of the
  Mac's in it. Through that card it reaches the internet and one port of the build cache's
  container. Outside the container an engineer can do exactly what the two tokens allow, and it
  can reach the internet, so it could also send the tokens anywhere. Their scope is the limit:
  one organization's code, and model requests. The default delivery lands on the default
  branch directly.
- All engineers and the manager share the VM's kernel. A kernel exploit from a container reaches
  the VM, which holds both tokens, the ledger and every engineer, and still nothing of the Mac's.
- The `org` user is the control plane. It holds both tokens and can enter every engineer's
  container. As `org-host` sets the VM up, it cannot leave its Incus project, become root, or
  change the firewall or Claude Code's managed settings.
- The template is the root of what engineers start with. It holds tools and settings and no
  login, and it is only ever copied.
- An engineer cannot reach the manager: not the dashboard, not the ledger, not the VM, not
  another engineer's container. What travels back is the snapshot, and sync takes only named
  files and known states from it.
- The build cache is the one thing engineers share. Its contents are written by every engineer
  and used by every engineer's builds and tests, and nothing can check that a stored result is
  the true one. Its network card is not isolated, so it can open connections to engineers'
  containers; it holds nothing and only `org up` enters it. The owner accepted this on
  2026-11-24 for the sake of build time.
- The review reads text an engineer wrote from public sources, and a stranger could have shaped
  it. The prompt says that text is data, and that is a rule a model follows, not a wall. The wall is
  what code does with the answer. The worst a wrong or manipulated verdict can do is start a spec the
  owner already approved, or spend one more run's cap on a retry of the same text. It cannot change
  or approve text, stop a run, raise a cap or act on a spec outside the plan, and a session with
  `ask`'s settings has no command that could. Its reason and notes are kept as one plain line each,
  with control characters removed, because they are printed in the owner's terminal; they are
  handed to the next engineer as text to read; the engineer's prompt says they are never
  instructions. The POST goes only to the one configured address, made by `org` and not by a model,
  and its body is made safe in code: one line, at most 400 characters, no link-like text, no control
  character.
- The Mac is reached by one port, and what answers on it is the dashboard. When the web-app view
  is on, that same port also serves pages an engineer wrote, at their own origins.

## What has been checked

On 2026-11-23, on an M4 Mac with macOS 15.7, Lima 2.3.0, Debian 13, Incus 6.0.4 and Claude Code
2.1.330.

| Claim | How |
|---|---|
| the runner, the manager's side, the Incus backend (the prefix check, `vet`, slots, draining, reaping, resuming a stopped container, tokens over stdin and never on a command line), the dashboard's API, the Docker backend | the unit suite, inside the VM under Python 3.13: a scripted stand-in for the claude CLI, and simulated incus and docker |
| `org up` in the VM from nothing | it installed the release, installed Claude Code, built the template and started the dashboard service; `org doctor` then reported only the missing tokens and git identity |
| what a container can reach | from a real container the internet answered; the Lima gateway (192.168.5.2), the VM's own addresses, the Mac's LAN address, the LAN router and a second container did not. This was before the build cache, whose container an engineer is meant to reach |
| Docker in an engineer's container | it installed, pulled, built and ran inside a container |
| the sandbox's footing | bubblewrap worked for the `org` user in the VM and inside a container |
| the cost of a copy | a copy of a container took about 0.06 s |
| a real engineer plans, builds, pushes and reports in a container | `tests/e2e.py --machine incus`, twice: on the VM as first built, and again after the VM was deleted, recreated from `org-host` and installed from this repository with `org-vm install main`. Each time: `done` in about thirty seconds for about $0.33 on sonnet, the script's own check in a fresh clone agreed with all five acceptance criteria, and the container was deleted afterwards |
| a run without a working login fails with a reason | the same test with no tokens, and with a Claude token cut short when it was copied: everything up to the model call worked, and the run ended `failed` with the API's own message |
| the GitHub token reaches one organization | from the VM, both of the organization's repositories answered and two private repositories in the owner's personal account answered 404 |
| no MCP servers in the VM | `claude mcp list` there, with the Claude token: none |
| the build cache's code: the container's description, the probe, the settings file, the `org doctor` row and the `org pool` line, switched on and off | `tests/test_cache.py`, against the simulated Incus |
| the real server and the real build | `tests/live_cache_docker.sh` (needs Docker): `bazel-remote` 2.6.4 installed by `incus/cache.sh` in a bare `debian:13`, and Bazel 9.3.0 through Bazelisk 1.30.0. A build uploads; a second build from a new output base gets cache hits and runs no compile or link; builds still succeed with the server killed and with its container stopped |
| the memory limit's code: the arithmetic and its refusals, the limit on the copy, the read-back in `vet`, the wrapper, the `oom_kill` count, the `org status` line and the `org doctor` row | the unit suite, against the simulated Incus |
| the memory limit itself | `tests/live_memory_incus.sh`, in an Incus of one's own (2026-11-24): the limit set on the copy is found by `vet` and read back as the container's own `memory.max`; a process taking more is killed and counted in that container's `memory.events`, and a second container and the session outside are untouched; with the wrapper the command is killed and the stand-in for a session lives, and without it the session is killed; a restricted project like the `org` user's accepts `limits.memory` on a copy, on an existing container and on a new one |
| plans: the header and its refusals, waiting and what satisfies a spec, the bundle, every verdict under every report status and each bound, one review for two pumps at once and for a process that died, recovery and its limits, the weekly pause, holds across a chain of three specs, release, the POST's body and a server that fails twice | `tests/test_after.py`, `tests/test_graph.py`, `tests/test_review.py` and `tests/test_notify.py`, against the scripted stand-in for the claude CLI, the simulated Incus and a local HTTP server |
| three plans from approval to their end | `tests/demo_graph.py`: B after A with `done`; with a failure, a `retry` and a second run that reports `done`; with two failures, a hold, one message at the local server and `org release` |
| port isolation, and so who can reach whom | real containers in a nested Incus 6.0.4: an engineer's container connects to the cache and not to another engineer's, and the cache container can open connections to engineers' containers |
| `org up` making the cache container, and two engineers' containers using it | `tests/live_cache_incus.sh`, on arm64 in an Incus 6.0.4 installed inside an engineer's container (2026-11-24): `org up`, run by an unprivileged user, made the cache container with `incus/cache.sh` under systemd; two copies of a stand-in template (the template's settings, g++ and Bazelisk in place of the bootstrap) passed `vet` and `problems()`, reached the cache and were given `/etc/bazel.bazelrc` with the address Incus gave; a Bazel 9.3.0 build in the first uploaded and the same build in the second was all cache hits; neither could open a connection to the other; the backend removed both and refused to remove the cache |

Not yet exercised:

- Plans with a real model and a real notification service. No real review has run, so what one
  costs and how well it reads a report are not known, and nothing has been posted to a real push
  service. The owner's first checks after upgrading are `org notify test`, and then a plan of two
  specs of trivial work to see one real review in `org graph`.
- The manager chat and `org ask` in the VM, and an engineer landing a commit on the default
  branch of a repository on GitHub (the smoke spec pushes to a repository inside its own
  container).
- `tests/live_manager_rules.py` and `tests/live_manager_refine.py` in this layout.
- `org upgrade` and its rollback against a real repository.
- `org drain`, `org undrain` and `org resume --interrupted` on a real installation.
- A restart of the VM.
- The web-app view.
- Nested Incus inside an engineer's container, for an engineer that works on org itself. Only
  the bubblewrap part of that was checked.
- ssh machines beside the containers.
- The VM's real firewall with the build cache: whether it lets one container reach another on
  the bridge. If it does not, dispatch says "not reachable" and builds go on without the cache.
- Anything of the build cache on x86-64: the pinned x86-64 hashes were checked, and nothing was
  run.
- The build cache under the real template, or under a real project's build.
- That the VM's own restricted Incus project (`user-2000`) accepts `limits.memory`. The engineer
  who wrote the limit proved it only in a restricted project of an Incus of its own. It is the
  owner's first check after upgrading: if the project refuses the key, dispatch refuses the run
  with Incus's own words and names `machines.incus.memory none`.

`org-host`'s README lists what was checked of the VM's own walls. The layouts before this one
(Docker containers, then OrbStack machines) had their own live checks, among them a real
engineer finishing the smoke spec, the manager's confinement with the sandbox live, the
two-turn refinement, and the page in a real browser. None of those has been repeated here, and
none is claimed for this layout.

## Alternatives considered

- **Docker containers for everything.** Built first. Docker's daemon is a ready-made agent with
  an access model, and the host needs only an engine. It lost because an engineer in a container
  cannot run Docker, and because holding the socket makes the manager root on the Docker host.
  The plugin that remains is unmaintained.
- **OrbStack machines.** The default before this one: the manager and every engineer as isolated
  OrbStack machines, created for the manager by a host agent on the Mac. It was left for three
  reasons. The Mac had to run code from this repository, the host agent and `org up`, which
  engineers can change. The machines shared a kernel and a network with the person's other
  OrbStack machines. And OrbStack forwards every machine's listening ports to the Mac's
  localhost, so a server an engineer started appeared there.
- **A Lima VM with Incus inside.** Chosen. Both are open source. One file, `lima.yaml`, lists
  every door between the VM and the Mac, and the Mac runs nothing else. Engineers are system
  containers that nest, so an engineer can in principle stand up a copy of org, containers and
  all, to test a change to it; of that, only bubblewrap inside a container has been checked.
- **Rules only, with no review: start the next spec when the one before reports `done` with every
  criterion passing.** About half the work and no model in the loop. Two of the first six runs
  reported `partial`, so it would pause too often, and a `done` report can describe a result the
  next spec did not expect, so it would also continue when it should not.
- **Let the review revise the next spec when its assumptions broke.** It would recover more without
  the owner and end approval as the owner's act. A review that sees how a spec should change says so
  in its reason, and that is all.
- **Have the next spec's engineer check its assumptions first and stop if they fail.** It costs a
  container and a planning turn to learn what the review reads from the report, and the engineer
  gets the earlier reports anyway and says so if its spec no longer fits.
- **Mail from the VM.** It needs a mail account's password on the machine that holds the two tokens.
- **A scheduler daemon for plans.** `pump` is already called every ten seconds; a second mechanism
  would be a second thing to keep alive.
- **A machine of its own for the manager.** Kept through the OrbStack layout. Inside a VM that
  already has nothing of the Mac's it is a second copy of the same wall, and the manager has to
  sit where it can drive Incus anyway.
- **A hosted cache that only CI writes.** No opening on the VM, and no engineer's results in
  another's build. It needs an account, a third credential, and CI rebuilt to match the
  engineers' machines. Set aside by the owner.
- **The cache server on the VM itself.** The firewall leaves containers no port of the VM, and it
  would run as the `org` user, beside the ledger and the tokens.
- **A directory shared into every engineer's container.** `vet` refuses a container that shares a
  directory, and should go on refusing.
- **A cache baked into the template.** It ages with every landing, and engineers could not add
  to it.
- **Bazel's settings baked into the template.** The cache's address is not known when the
  template is built, and writing them only after the connection was tried keeps a build from
  waiting on a cache it cannot reach.
- **A name instead of an address in those settings.** One more thing that can fail to resolve.
- **Logins baked into the template.** Simpler at dispatch, since every copy would have them.
  It puts the tokens on disk and in every container whether or not a run needs them, and a
  renewed token needs a new template. The template is blank and each run is handed its tokens.
- **Logging in inside the VM** (`claude /login`, `gh auth login`). Those logins are the whole
  account: connectors, every repository. Two narrow tokens handed in from outside are not.
- **A read-only GitHub token for the manager.** It would have let the manager read private
  repositories. Public repositories and no token leave nothing to steal from the chat.
- **Installing per run instead of copying a template.** No template to keep, but minutes per run
  and a dependency on package mirrors at dispatch time.
- **A shared mount instead of running a command per sync.** Mounting every run directory into the
  manager's side would make sync a file read. `vet` refuses a container that shares a directory
  with the VM, which is the point of it, and a command works for every kind of machine.
- **Claude Code's own background sessions** (`claude --bg`, `claude agents --json`). They run on
  the local machine only and report state but not cost. The headless stream is the documented
  interface and carries cost, limits and structured output.
- **The Agent SDK.** It needs a package manager on every machine. The CLI is already there.
- **Git as the bus between manager and engineers.** Durable, but a heartbeat every few seconds
  makes a poor commit history, and it needs a repository before there is a project.
- **Engineers pushing status to the manager.** Simpler to write, but it needs a route from every
  container back to the manager, which is the isolation being given up.
- **One long engineer turn.** No cost checkpoints, no enforceable caps, no progress to show.
- **Letting the manager approve when the person says "approve" in chat.** The model would be
  judging whether it had permission. A harness prompt does not depend on that judgment.

## Known gaps

- Nothing in this layout has made a real model call yet; see what has been checked.
- An engineer lands its own work on the default branch; nothing on GitHub reviews it first.
- The build cache's settings stop a Bazel older than 8.6 with "Unrecognized option" (see "The
  build cache"); the file has to be removed in such a project's container.
- An `org up` that loses power between making the cache container and seeing its server up
  leaves a container the next `org up` takes for the cache; `org doctor` then shows it running
  and dispatch says "not reachable". `incus delete -f org-cache`, then `org up`, makes it again.
- The build cache cannot tell a true stored result from a false one, has no credential and no
  TLS, and is reached by every engineer; one engineer's wrong output is used by the others until
  the cache is emptied. Its network card is not isolated, so it can open connections to
  engineers' containers. Whether the VM's firewall lets engineers reach it is not known until it
  is tried on the VM.
- An engineer holds both tokens and reaches the internet. Their scope bounds what a leaked one
  can do; nothing prevents the leak.
- All engineers and the manager share one kernel, and the `org` user holds both tokens and can
  enter every container.
- An engineer is root in its container and can undo the Claude Code settings there.
- Public repositories are an input channel. Issues are off; a stranger's text in a pull request
  is kept from being an instruction only by the prompts.
- The firewall fences the containers, not the `org` user's own processes. The manager's shell is
  held by the sandbox's allow list instead, and its search and fetch tools reach the web.
- `vet` runs at dispatch and is not repeated while a run is going.
- The memory limit bounds the engineers' containers and nothing else; the reserve is a guess at
  what the rest needs, and a machine too small for its slots gets limits `org doctor` calls too
  small until the owner lowers the slots or the reserve. A container started before the upgrade
  has no limit until it is stopped and resumed.
- The wrapper that has a build step killed before the session was tried with a stand-in for the
  session, called as Claude Code's documentation and the text of version 2.1.330 say a shell
  command is called. No real session has run under it yet. It reaches what the session's shell
  tool starts; a container started through a Docker daemon is the daemon's child and scores as
  before.
- Nothing restarts anything after the Mac restarts. `org-vm up` and then
  `org resume --interrupted` do.
- A finished run's container is deleted by the next pump, and that is not undoable (see "A finished
  run's machine"). The build cache's container, the template, old releases and old templates stay
  until someone deletes them; nothing reclaims their disk by itself.
- A rollback moves links. It does not undo what the newer release wrote.
- A run's share of the weekly limit is unknown until the rate has been learned or configured.
- One engineer per spec. A spec too large for one engineer is the manager's to split.
- No notification when a run finishes, unless it is part of a plan and stops it or ends it; the
  page and `org status` have to be looked at.
- Plans have run only against stand-ins. No real review has run and no real notification service has
  been posted to; see what has been checked.
- A stopped machine is recognised as such only for Incus and Docker. For any other kind, a machine
  that cannot be reached is not taken to be stopped, and its run is resumed only once its runner has
  been dead for `graph.dead_after_min` minutes.
- An alert is written on the next pump after a pause, not at the instant of it, and with nothing
  pumping (the dashboard stopped, nobody running `org status`) a plan waits and says nothing. With
  no `notify.url`, a pause is visible only in `org status` and the dashboard.
- The review is a model reading text an engineer wrote. Its verdict is bounded in code, but a
  manipulated `continue` on a `done` report starts a spec the owner approved, on a result that may
  not match what that spec assumed; the engineer of that spec is told to say so in its report.
- A finished run is reviewed once, for the specs that waited on it then. A spec approved later, or
  approved again in a new version, was not read by that review, so its run is paused (`unreviewed`)
  and waits for `org release`: nothing reads the report for it a second time.
- A pause stays on the run that caused it. If the owner resumes a paused run by hand and it then
  finishes well, what waited on it is still held and needs `org release`; dispatching the spec
  again is the way to have the plan go on by itself, with a review.
- The review session can read `config.json`, as `ask` mode can, and so the address in `notify.url`
  and the headers in `notify.headers`. An address that is a secret topic could be copied into a
  note, which travels to the next engineer. `org doctor` shows only its host; treat the address
  as something an engineer may learn.
- The limits hold against two pumps at once and against a process that ends between two steps: a
  verdict that was recorded and not acted on becomes a pause, and a pause is told again rather
  than never. One thing the owner does at the same instant as a pump is not guarded: `org sync
  <run>` writes a final run's record under the run's lock and not the tick's, and could undo a count
  or a review that was just written. It leaves a run whose machine is deleted alone: sync would never
  ask a machine that is gone, and the run would stay not final for good. A review still running when its
  spec is approved again or dispatched again by hand is left to end by itself, and its cost is
  not recorded.
- The Docker plugin's host script still asks for a whole GitHub login and should not be used.
