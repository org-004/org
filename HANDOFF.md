# Handoff: OrbStack as the backing infrastructure (in progress)

Branch `worktree-build`. Written at the end of a session that ran inside an isolated OrbStack
machine, where `orb` cannot be run. The rest needs a Mac. Delete this file when the work is done.

## What was asked

- "If engineers can't use docker, that seems like a good reason to use Orbstack (if that allows
  for docker builds). Use orbstack as the backing infra for the VMs. You can include the docker
  stuff as a different plugin if we decide to switch back to your original implementation."
- Still wanted from before: the manager in a box too, able to read the engineers' boxes; "a simple
  setup script I run on Mac that sets up the manager/ engineer boxes and compute pool… Sort of
  like kubelet on my computer"; "less dependencies are better".
- The session on the Mac does the full end-to-end test on real OrbStack.

Does an OrbStack machine allow Docker builds: yes. In the earlier session a Docker daemon ran
inside an isolated OrbStack machine, built a 1.4 GB image and ran containers with networking.
OrbStack's release notes also list fixes for "Docker not working in isolated machines" (2.2.0).
The template machine has to have Docker installed for engineers to use it.

## Done in this commit

All of it is unit-tested against `tests/fake_orb.py`. None of it has touched real OrbStack.

- `org/backends/`: one interface for where engineers run. Built in: `orb` (default), `ssh`,
  `local`. `org/backends/docker.py` is a plugin, loaded only when config names it:
  `{"machines": {"plugins": ["org.backends.docker"], "default": "docker"}}`. Inside the Docker
  image (`ORG_IN_CONTAINER`) that is the default, so `docker/up.sh` still works as before.
- `org/agent.py`: the host agent, for a manager that is itself an isolated machine. See below.
- `dispatch`, `sync`, `doctor`, `spec`, `status` and `cli` go through the backends.
  `transport.for_machine` moved to `backends.for_machine`.
- Pool for OrbStack: `machines.orb.slots` (unset means no limit). A run with no free slot queues.
- Before an engineer starts, dispatch records `facts.docker` (does a Docker daemon answer) and
  `facts.mac_files` (is `/mnt/mac` populated), and the orb backend refuses a machine that can see
  the Mac's files unless `machines.orb.allow_shared` is set.
- New commands: `org config get|set <dotted.key> [json]`, `org agent run|status|bridge|pipe`.
- Spec check: a `machine:` the ledger cannot provide is an error at check time, not at dispatch.
- `tests/test_agent.py`: 14 tests. `tests/fake_orb.py` now has create, clone (copies the
  source's home), start, stop, list (with a header row), status, `-m`, delete.

Run the tests with `python3 -m unittest discover -s tests`. Keep the code working on Python 3.9:
that is what macOS ships.

## The host agent, and why it is shaped this way

An isolated machine cannot run `orb`, and cannot reach the Mac over the network at all. OrbStack's
docs (docs.orbstack.dev/machines/isolated) say an isolated machine "Can't reach the macOS host
over the network or run `mac` commands", and that it keeps "internet access, `.orb.local`
domains, and SSH and `orb` access from your Mac". So nothing can listen on the Mac for the
manager. The agent dials in instead:

- On the Mac, `org agent run` (config `~/.config/org/agent.json`: orb, manager, template, prefix,
  dash_port) keeps `orb -m <manager> bash -c 'exec "$HOME/.local/bin/org" agent bridge --dash-port N'`
  open, and reconnects when it ends.
- In the manager's machine, the bridge listens on `<ledger>/agent.sock` (mode 0600, and it refuses
  a caller that is another user or has no pid in this machine). `agent.Client` sends one JSON line
  and reads one back. The bridge passes requests up its stdout and answers down its stdin.
- The agent does four things: `info`, `list`, `create` (`orb clone <template> <name>` then
  `orb start`), `delete` (`orb delete -f`), `exec` (`orb -m <name> bash -c <script>` with stdin).
  It refuses any name that does not start with its prefix (`org-eng-`), and refuses a config in
  which the manager or the template would match that prefix.
- The bridge also serves the dashboard on the manager machine's 127.0.0.1. On connecting, the
  agent checks whether `http://127.0.0.1:<port>/` on the Mac already answers (OrbStack forwards a
  machine's ports to the Mac's localhost). If not, it listens on that port itself and carries each
  connection through `orb -m <manager> … org agent pipe <port>`, as base64 lines.
- The orb backend uses the agent when `machines.orb.via` is `agent`, and the `orb` command when
  it is `cli` (the default: a manager running on the Mac itself).

## Not done

1. **`org up` and `org down` for OrbStack.** Nothing exists yet. `bin/org` still sends `up` and
   `down` to `docker/up.sh`. Intended shape, as a script run on the Mac (bash 3.2, BSD tools):
   - Template `org-base`: keep it if it exists; else clone the machine named by `--from` (the
     user's own template is called `base`, made with `orb create --isolated arch base`); else
     `orb create --isolated <distro> org-base`. Then run a bootstrap inside it that installs
     what is missing: python3, git, gh, tmux, curl, bubblewrap, socat, Docker (daemon enabled,
     user in the `docker` group) and Claude Code (`curl -fsSL https://claude.ai/install.sh | bash`).
     Copy in what the Mac has and the template lacks: `~/.claude/oauth-token`, `gh auth token`
     (`gh auth login --with-token`, `gh auth setup-git`), git user.name and user.email.
     Engineers are clones of the template, so they inherit all of it.
   - Manager `org-manager`: clone of the template. Copy `org/` and `bin/` into
     `~/.local/share/org` (tar over stdin, base64, `COPYFILE_DISABLE=1` on the Mac), link
     `~/.local/bin/org`. Then inside it: `org init`, and `org config set` for
     `machines.default orb`, `machines.orb.via agent`, `machines.orb.base org-base`,
     `machines.orb.prefix org-eng-`, `machines.orb.slots <n>`.
   - Write `~/.config/org/agent.json`, stop any old agent (pid file), start
     `python3 -m org agent run` with nohup and a log, wait until
     `orb -m org-manager bash -c '"$HOME/.local/bin/org" agent status'` succeeds.
   - `org down`: stop the agent and the manager machine. `--all`: delete engineer machines.
     `--purge`: delete the manager (the ledger is inside it) and the template.
   - After a restart of the Mac, `org up` again brings it back. No launchd or systemd units.
   - Keep `docker/up.sh` reachable as `org up --backend docker`.
2. **`bin/org` on the Mac.** With an OrbStack manager, forward every command except `up`, `down`
   and `agent run`: `exec orb -m "$manager" bash -c 'exec "$HOME/.local/bin/org" "$@"' org "$@"`.
   `org dash` should print and open the URL from `org dash --url` run in the manager.
3. **Tell the engineer about Docker.** `job.json` now carries `facts` (`docker`, `cores`). The
   engineer's system prompt (`org/prompts/engineer.md`, rendered in `org/runner.py`) should say
   whether Docker is available on its machine.
4. **`org/prompts/manager.md`** names `orb` and `ssh:<name>` as the `machine` values. Fine for
   OrbStack; wrong when the Docker plugin is the default.
5. **README.md and docs/design.md** still describe Docker containers as the primary setup. They
   need rewriting: OrbStack machines as the default, the host agent, Docker as a plugin, the trust
   table, and what has and has not been checked.
6. **`tests/e2e.py`** builds a temporary ledger. For the agent path it must use the manager's own
   ledger (the bridge serves only that one): run it inside the manager machine with
   `--home ~/.org --machine orb`.
7. **A test of the setup script** against `tests/fake_orb.py` (skip the package installs there).

## To verify on real OrbStack

None of these could be checked from inside a machine. The docs do not settle them.

- Does a long-lived `orb -m <machine> bash -c …` pass stdin and stdout as clean pipes, line by
  line, for hours? The bridge depends on it. The agent pings every 15 s; the bridge exits after
  75 s of silence.
- When the `orb` client on the Mac is killed, does the command inside the machine get end of file
  on stdin? The bridge relies on that to exit and remove its socket.
- `orb clone`: must the source be stopped? Is the clone left stopped (the code runs `orb start`
  after it, as the user's `box` helper does)? Does a clone of an isolated machine stay isolated?
  `thread2`, a clone of the isolated `base`, is isolated, which suggests yes.
- `orb list`: its columns and whether it has a header. The code takes the first word of each
  line, so either works.
- Does OrbStack forward a port that a machine listens on at 127.0.0.1 to the Mac's localhost, and
  does it for an isolated machine? If not, the agent's own forward takes over; check its log line
  `dashboard: http://127.0.0.1:8787 (…)` in `~/.config/org/agent.log`.
- Is `/mnt/mac` empty in an isolated machine and populated in a normal one? It is empty in
  `thread2` (isolated). The isolation check depends on the second half.
- `/mnt/machines` is empty in an isolated machine, so engineers cannot read each other's files or
  the manager's. Confirm on a fresh clone.
- Then the real thing: `org up`, `org doctor`, `org talk`, approve a small spec, watch an engineer
  run in `org-eng-<run>`, `org status`, `org dash`, `org machine rm`. And once with the manager on
  the Mac itself (`machines.orb.via` unset), which needs no agent.

## Constraints from the user

- The GitHub repository stays private. Never push to master or main, never force-push, never merge.
- Commits: present tense, `Area: what changed`, no Claude attribution in commits or pull requests.
- The manager must not permanently modify project state. The engineer may write state, never the spec.
- Engineers run with permissions bypassed, so they belong in isolated machines only.
- Writing for this user: plain words, no greetings, praise, filler or offers; lead with the error
  or the disagreement.
