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
 │                   reaches the internet and nothing beside it                │
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
user in the restricted `incus` group, and Claude Code's managed settings in `/etc`. It makes four
things, keeps any that already exist, and then runs `org doctor`:

| | What | Notes |
|---|---|---|
| a release | this copy of org, in `~/.local/share/org/releases/<commit>` | `current` points at the one in use and `~/.local/bin/org` at `current`; earlier releases stay on disk |
| Claude Code | installed for the `org` user when it is missing | for the manager's sessions; no login is made |
| `org-base` | the template: an unprivileged Incus container (Debian 13) with python3, git, gh, tmux, Docker and Claude Code, and no login in it | every engineer is a copy of it, and it is left stopped |
| `org-dash.service` | the dashboard, a systemd service of the `org` user | restarted by every `org up`, so it runs the release just installed |

```sh
org up --owner <organization>   # the GitHub organization specs may name repositories in
org up --slots 6                # engineers that may run at once (default 4)
org up --rebuild-template       # build a new template and make engineers copies of it
org up --port 9000              # the dashboard's port in the VM (default 8787)
```

`org-vm up` passes `--owner` and `--port` itself. The VM forwards one port to the Mac, the same
number on both sides, chosen when the VM is made (`ORG_VM_PORT`, default 8787); a dashboard
moved to another port with `org up --port` by hand is not reachable from the Mac.

`org down` stops the dashboard and nothing else: engineers keep running, and `org up` brings the
view of them back. Stopping the VM is `org-vm down`, which stops every engineer with it.

Two tokens enter the VM, and nothing else of yours does:

| | What it is | What it can do |
|---|---|---|
| a Claude token | what `claude setup-token` prints | make model requests. It is documented as unable to fetch the account's claude.ai connectors |
| a GitHub token | a fine-grained token whose resource owner is one organization, with Contents and Pull requests read and write | read and push code and open pull requests in that organization's repositories. It cannot create a repository, change settings or edit workflow files |

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
internet. It cannot reach the Mac, the LAN or another engineer, and of the VM it can only ask
for an address and a name. Dispatch copies the two tokens into the container's memory and the
runner hands them to the session as `CLAUDE_CODE_OAUTH_TOKEN` and `GH_TOKEN`.

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
it made.

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
  finished step.

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
delivery: pr            # pr | branch | merge | none
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
    "incus": {"base": "org-base", "prefix": "org-eng-", "slots": 4, "push_token": true},
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
  /etc/claude-code/          root's: no MCP servers, no claude.ai connectors

in an engineer's container (org-eng-<run>), as its root:
  /run/org/                  its copies of the two tokens, in memory
  ~/org-runs/<run>/          spec.md, job.json, status.json, plan.md, report.md, events.jsonl
  ~/work/<run>/              the engineer's clones
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
installation is built around and the only one exercised in it.

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
```

`tests/e2e.py` approves a small spec, dispatches it, follows it to the end, and then repeats the
acceptance checks itself in a fresh clone on the engineer's container instead of taking the
engineer's word. It uses a ledger of its own, so it leaves nothing in yours, and deletes the
container when the run passes.

## What has and has not been exercised

On 2026-11-23, on an M4 Mac with macOS 15.7, Lima 2.3.0, Debian 13, Incus 6.0.4 and Claude Code
2.1.330:

- **Exercised for real:**
  - The unit suite passes inside the VM under Python 3.13.
  - `org up` in the VM from nothing: it installs the release, installs Claude Code, builds the
    template and starts the dashboard service. `org doctor` then reports only the missing tokens
    and git identity.
  - From a real container the internet answers. The Lima gateway (192.168.5.2), the VM's own
    addresses, the Mac's LAN address, the LAN router and a second container do not.
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
- **Not yet exercised:**
  - The manager chat and `org ask` in the VM, and an engineer delivering a pull request to a
    repository on GitHub (the smoke spec pushes to a repository inside its own container).
  - `tests/live_manager_rules.py` and `tests/live_manager_refine.py` in this layout.
  - `org upgrade` and its rollback against a real repository.
  - `org drain`, `org undrain` and `org resume --interrupted` on a real installation, and a
    restart of the VM.
  - The web-app view.
  - Nested Incus inside an engineer's container, for an engineer that works on org itself. Only
    the bubblewrap part of that was checked.
  - ssh machines beside the containers.

## Limits worth knowing

- **Engineers hold both tokens and reach the internet.** A run can do whatever the tokens allow,
  and could send them anywhere. What bounds that is their scope: one organization's code and
  pull requests, and model requests. The owner can revoke either.
- **An engineer can merge its own pull request.** You and the engineers are the same GitHub
  account, so the pull-request rule on each default branch is a record of what was merged, not
  an approval gate. What decides which version of org runs is `org upgrade`, which shows you the
  diff first.
- **Public repositories are a way in.** Every repository is public, so text a stranger writes
  in a pull request or a comment can reach an agent. The prompts say it is never an instruction,
  and `org-host`'s `org-github` turns issues off. That is a rule a model follows, not a wall.
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
