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
| `bin/org`, `incus/` | the VM | the wrapper, `org up` and `org down`, the template's setup |
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
 │    has          root, an init, a Docker daemon of its own, the internet     │
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
at it, leaving the old one for the runs that were promised it.

**The backend acts only on its own containers.** Every action of the Incus backend passes one
check first (`_own()` in `org/backends/incus.py`): the name must start with the configured prefix
(`org-eng-`) and be a plain container name. A template whose name starts with the prefix is
refused, since a run could then be asked to delete it, and a run record cannot point org at
another container. Two other parts of org name containers outside the prefix, on purpose:
`org up` creates the template, and `org upgrade` creates and deletes a test container
(`org-test-<commit>`). All of it happens inside the `org` user's own Incus project.

**What makes a container a box.** Before an engineer starts, the backend's `vet` reads Incus's
own description of the new container, not anything inside it, and refuses the run if the
container is privileged, has any device beyond its root disk and its network card, shares a
directory from the VM, or has a network card without `security.port_isolation` (it could reach
the other containers) or without `security.ipv4_filtering` (it could take another address).
`machines.incus.allow_open` overrides the refusal. The check runs at dispatch and nothing
re-checks a container later. An engineer cannot change those settings from inside its container;
the `org` user could.

**The network.** An engineer reaches the internet, to clone, install and call the model. It
cannot reach the Mac, the LAN or other engineers, and of the VM it can only ask for an address
and a name. Two things hold that. The per-card settings above are Incus's, set on the template
and inherited by every copy. A firewall in the VM, installed by `org-host` and owned by root
there, drops what containers send to private, link-local and other non-public ranges and all of
their IPv6. An engineer is root in its container and can change neither. The firewall's rules
name the containers' bridges: they do not filter what the `org` user's own processes send.

**Credentials.** Exactly two tokens enter the VM. `org-vm` keeps them in the Mac's Keychain and
pipes them in at every start, into `/run/org`, a memory-only directory that the `org` user alone
can read.

| Token | What it is | What it can do |
|---|---|---|
| Claude | what `claude setup-token` prints | model requests only; documented as unable to fetch the account's claude.ai connectors |
| GitHub | a fine-grained token whose resource owner is one organization; Contents and Pull requests, read and write | read and push code and open pull requests there; it cannot create repositories, change settings or edit workflow files |

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
only then are the tokens and the git identity copied in, the bundle unpacked and the runner
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
that hand out addresses and names.

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
figure and falls back to differencing the cumulative one. Inside a turn, spend is estimated from
streamed token counts with a price table and corrected at the turn's end.

## Status, sync and the page

The runner rewrites `status.json` every few seconds. Sync is one read-only command per run,
`org engineer snapshot`, which returns status, plan, report and the next slice of the event log
as one JSON document. The manager's side writes that into `runs/<run>/mirror/`.

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
| ask rules | approve, dispatch, stop, resume, exec, `machine rm`, drain, undrain, upgrade, up, down, `config set`; ssh | the harness, by asking the person, in every permission mode |
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

**The pull-request rule is an audit trail, not an approval gate.** The person and the engineers
are the same GitHub account. `org-host`'s `org-github` makes each default branch
pull-request-only with nobody exempt, which records what was merged; it does not stop an
engineer merging its own pull request. For this repository the gate is `org upgrade`, where the
person reads the diff before the code runs beside the tokens.

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
| engineer | root in its container; copies of both tokens | anything in its container; on GitHub and the model, what the tokens allow |
| dashboard page | the access token | approve, stop, ask |
| a stranger on GitHub | a public repository to read | write text that an agent may read |

- An engineer runs with permissions bypassed, so its container is the boundary: unprivileged,
  no device but its disk and an isolated network card, and behind it a VM with nothing of the
  Mac's in it. Outside the container an engineer can do exactly what the two tokens allow, and it
  can reach the internet, so it could also send the tokens anywhere. Their scope is the limit:
  one organization's code and pull requests, and model requests. The default delivery is a pull
  request, and an engineer could merge it.
- All engineers and the manager share the VM's kernel. A kernel exploit from a container reaches
  the VM, which holds both tokens, the ledger and every engineer, and still nothing of the Mac's.
- The `org` user is the control plane. It holds both tokens and can enter every engineer's
  container. As `org-host` sets the VM up, it cannot leave its Incus project, become root, or
  change the firewall or Claude Code's managed settings.
- The template is the root of what engineers start with. It holds tools and settings and no
  login, and it is only ever copied.
- An engineer cannot reach the manager: not the dashboard, not the ledger, not another
  engineer. What travels back is the snapshot, and sync takes only named files and known states
  from it.
- The Mac is reached by one port, and what answers on it is the dashboard. When the web-app view
  is on, that same port also serves pages an engineer wrote, at their own origins.

## What has been checked

On 2026-11-23, on an M4 Mac with macOS 15.7, Lima 2.3.0, Debian 13, Incus 6.0.4 and Claude Code
2.1.330.

| Claim | How |
|---|---|
| the runner, the manager's side, the Incus backend (the prefix check, `vet`, slots, draining, reaping, resuming a stopped container, tokens over stdin and never on a command line), the dashboard's API, the Docker backend | the unit suite, inside the VM under Python 3.13: a scripted stand-in for the claude CLI, and simulated incus and docker |
| `org up` in the VM from nothing | it installed the release, installed Claude Code, built the template and started the dashboard service; `org doctor` then reported only the missing tokens and git identity |
| what a container can reach | from a real container the internet answered; the Lima gateway (192.168.5.2), the VM's own addresses, the Mac's LAN address, the LAN router and a second container did not |
| Docker in an engineer's container | it installed, pulled, built and ran inside a container |
| the sandbox's footing | bubblewrap worked for the `org` user in the VM and inside a container |
| the cost of a copy | a copy of a container took about 0.06 s |
| a real engineer plans, builds, pushes and reports in a container | `tests/e2e.py --machine incus`, twice: on the VM as first built, and again after the VM was deleted, recreated from `org-host` and installed from this repository with `org-vm install main`. Each time: `done` in about thirty seconds for about $0.33 on sonnet, the script's own check in a fresh clone agreed with all five acceptance criteria, and the container was deleted afterwards |
| a run without a working login fails with a reason | the same test with no tokens, and with a Claude token cut short when it was copied: everything up to the model call worked, and the run ended `failed` with the API's own message |
| the GitHub token reaches one organization | from the VM, both of the organization's repositories answered and two private repositories in the owner's personal account answered 404 |
| no MCP servers in the VM | `claude mcp list` there, with the Claude token: none |

Not yet exercised:

- The manager chat and `org ask` in the VM, and an engineer delivering a pull request to a
  repository on GitHub (the smoke spec pushes to a repository inside its own container).
- `tests/live_manager_rules.py` and `tests/live_manager_refine.py` in this layout.
- `org upgrade` and its rollback against a real repository.
- `org drain`, `org undrain` and `org resume --interrupted` on a real installation.
- A restart of the VM.
- The web-app view.
- Nested Incus inside an engineer's container, for an engineer that works on org itself. Only
  the bubblewrap part of that was checked.
- ssh machines beside the containers.

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
- **A machine of its own for the manager.** Kept through the OrbStack layout. Inside a VM that
  already has nothing of the Mac's it is a second copy of the same wall, and the manager has to
  sit where it can drive Incus anyway.
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
- An engineer can merge its own pull request. The ruleset records; it does not gate.
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
- Nothing restarts anything after the Mac restarts. `org-vm up` and then
  `org resume --interrupted` do.
- A finished run's container stays until `org machine rm`, and old releases and old templates
  stay until someone deletes them; nothing reclaims the disk by itself.
- A rollback moves links. It does not undo what the newer release wrote.
- A run's share of the weekly limit is unknown until the rate has been learned or configured.
- One engineer per spec. A spec too large for one engineer is the manager's to split.
- No notification when a run finishes; the page and `org status` have to be looked at.
- The Docker plugin's host script still asks for a whole GitHub login and should not be used.
