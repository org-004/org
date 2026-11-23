#!/bin/bash
# org up / org down on a Mac with OrbStack: the whole installation, as OrbStack machines.
#
#   org up [--from <machine>] [--distro <distro>] [--slots <n>] [--port <n>]
#   org down [--all] [--purge]
#
# What `up` makes, and keeps if it is already there:
#   org-base        the template: an isolated machine with python3, git, gh, tmux, Docker and
#                   Claude Code, and this Mac's logins. Every engineer is a clone of it.
#                   --from clones a machine you already have instead of creating one.
#                   It is left stopped: it is only ever copied.
#   org-manager     a clone of the template with org installed in it. The ledger is inside.
#   the host agent  one background process on this Mac. It keeps a command open inside the
#                   manager and creates, enters and deletes org-eng-* machines when the manager
#                   asks. Nothing else on the Mac belongs to org. (org/agent.py)
#
# Run it again after pulling a new version, after changing a login on the Mac, and after the Mac
# restarts: it updates the copy of org in the manager and starts the agent. There is no launchd
# unit. `org down` stops the agent and the manager; --all deletes the engineers' machines;
# --purge deletes the manager (and the ledger in it) and the template.
#
# Environment: ORG_MANAGER (org-manager), ORG_TEMPLATE (org-base), ORG_PREFIX (org-eng-),
#              ORG_PORT (8787), ORG_HOST_DIR (~/.config/org), ORG_ORB (orb).
# Written for the bash 3.2 and BSD tools that macOS ships.
set -u

root=$(cd "$(dirname "$0")/.." && pwd)
orb="${ORG_ORB:-orb}"
host_dir="${ORG_HOST_DIR:-$HOME/.config/org}"

say() { printf '%s\n' "$*"; }
die() { printf 'org: %s\n' "$*" >&2; exit 1; }

# What an earlier `org up` decided, so `org down` and a second `org up` act on the same machines.
saved() { [ -f "$host_dir/agent.json" ] && sed -n 's/.*"'"$1"'": *"\{0,1\}\([^",]*\)"\{0,1\},\{0,1\}$/\1/p' "$host_dir/agent.json" | head -1; }
manager="${ORG_MANAGER:-$(saved manager)}";   manager="${manager:-org-manager}"
template="${ORG_TEMPLATE:-$(saved template)}"; template="${template:-org-base}"
prefix="${ORG_PREFIX:-$(saved prefix)}";       prefix="${prefix:-org-eng-}"
port="${ORG_PORT:-$(saved dash_port)}";        port="${port:-8787}"

command -v "$orb" >/dev/null 2>&1 || die "\`$orb\` is not installed. OrbStack provides it: brew install --cask orbstack"

machines() { "$orb" list 2>/dev/null | awk '{print $1}'; }
exists() { machines | grep -qx -- "$1"; }
in_machine() { local machine="$1"; shift; "$orb" -m "$machine" bash -c "$@"; }
ORG='"$HOME/.local/bin/org"'

stop_agent() {
  local pidfile="$host_dir/agent.pid" pid
  [ -f "$pidfile" ] || return 1
  pid=$(cat "$pidfile" 2>/dev/null)
  rm -f "$pidfile"
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  # Only a process that is the agent: a pid is reused after a restart of the Mac.
  ps -p "$pid" -o command= 2>/dev/null | grep -q "org agent run" || return 1
  kill "$pid" 2>/dev/null
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.5
  done
  kill -9 "$pid" 2>/dev/null
  return 0
}

cmd="${1:-up}"
[ $# -gt 0 ] && shift

case "$cmd" in
down)
  all=""; purge=""
  for a in "$@"; do case "$a" in --all) all=1 ;; --purge) purge=1 ;; *) die "usage: org down [--all] [--purge]" ;; esac; done
  if stop_agent; then say "host agent stopped"; else say "no host agent running"; fi
  engineers=$(machines | grep -- "^$prefix" || true)
  if [ -n "$all" ] || [ -n "$purge" ]; then
    for m in $engineers; do "$orb" delete -f "$m" >/dev/null 2>&1 && say "deleted $m"; done
  elif [ -n "$engineers" ]; then
    say "engineers' machines are left as they are: $(printf '%s\n' "$engineers" | wc -l | tr -d ' ') (\`org down --all\` deletes them)"
  fi
  if [ -n "$purge" ]; then
    for m in "$manager" "$template"; do
      if exists "$m"; then "$orb" delete -f "$m" >/dev/null 2>&1 && say "deleted $m"; fi
    done
    rm -f "$host_dir/agent.json"
    say "the ledger was inside $manager and is gone with it"
  elif exists "$manager"; then
    "$orb" stop "$manager" >/dev/null 2>&1 || true
    say "$manager stopped; the ledger stays inside it (\`org down --purge\` deletes it and $template)"
  fi
  exit 0 ;;
up) ;;
*) die "usage: org up [--from <machine>] [--distro <distro>] [--slots <n>] [--port <n>] | org down [--all] [--purge]" ;;
esac

from=""; distro="debian:trixie"; slots=""
while [ $# -gt 0 ]; do
  case "$1" in
    --from)   [ $# -ge 2 ] || die "--from takes the name of a machine"; from="$2"; shift 2 ;;
    --distro) [ $# -ge 2 ] || die "--distro takes a distribution, as \`orb create\` does"; distro="$2"; shift 2 ;;
    --slots)  [ $# -ge 2 ] || die "--slots takes a number"; slots="$2"; shift 2 ;;
    --port)   [ $# -ge 2 ] || die "--port takes a number"; port="$2"; shift 2 ;;
    *) die "usage: org up [--from <machine>] [--distro <distro>] [--slots <n>] [--port <n>]" ;;
  esac
done
case "$slots" in *[!0-9]*) die "--slots takes a number, not '$slots'" ;; esac
case "$port" in ''|*[!0-9]*) die "the port must be a number, not '$port'" ;; esac
for name in "$manager" "$template"; do
  case "$name" in "$prefix"*) die "the machine name $name starts with the engineers' prefix $prefix, so it could be deleted as one" ;; esac
done

command -v python3 >/dev/null 2>&1 || die "python3 is not on this Mac, and the host agent needs it: xcode-select --install"
if ! "$orb" status >/dev/null 2>&1; then
  say "starting OrbStack"
  "$orb" start >/dev/null 2>&1 || die "OrbStack did not start; open the app and run this again"
fi

# ---- the template

bootstrap() {  # prints the machine's facts; what it installs is reported on stderr as it goes
  "$orb" -m "$1" bash -s -- ${ORG_UP_NO_INSTALL:+--no-install} < "$root/orbstack/bootstrap.sh"
}
fact() { printf '%s\n' "$facts" | sed -n "s/^$1=//p" | head -1; }

if exists "$template"; then
  say "template: $template"
elif [ -n "$from" ]; then
  exists "$from" || die "there is no machine named $from to clone (\`orb list\`)"
  "$orb" clone "$from" "$template" >/dev/null || die "could not clone $from"
  say "template: $template, cloned from $from"
else
  say "template: creating $template ($distro, isolated)"
  "$orb" create --isolated "$distro" "$template" >/dev/null
  exists "$template" \
    || die "could not create $template from $distro. Another distribution may work (--distro ubuntu), or clone a machine you have (--from <machine>)."
fi
"$orb" start "$template" >/dev/null 2>&1 || die "$template did not start"
facts=$(bootstrap "$template") || die "the setup inside $template did not finish; \`orb -m $template\` gives a shell there"

if [ "$(fact mac_files)" = yes ]; then
  # An engineer runs with every permission bypassed. A machine that shares the Mac's files and
  # can run commands on the Mac is no box for one.
  say "template: $template can see this Mac's files; making it an isolated machine"
  "$orb" config set "machine.$template.isolated" true >/dev/null 2>&1
  "$orb" stop "$template" >/dev/null 2>&1; "$orb" start "$template" >/dev/null 2>&1
  facts=$(bootstrap "$template") || die "$template did not come back after isolating it"
  [ "$(fact mac_files)" = yes ] && die "$template still sees this Mac's files. Create an isolated one: orb delete $template; org up"
fi
[ -z "$(fact missing)" ] || die "$template lacks: $(fact missing). Install them there (\`orb -m $template\`) and run this again."
say "template: $(fact claude), Docker $([ "$(fact docker)" = yes ] && echo "running" || echo "not available (engineers will not be able to run containers)")"

# ---- this Mac's logins, for the template and so for every clone of it

logins() {  # machine; uses $facts for what it already has
  local m="$1" token name email
  if [ -s "$HOME/.claude/oauth-token" ]; then
    in_machine "$m" 'umask 077; mkdir -p "$HOME/.claude"; cat > "$HOME/.claude/oauth-token"' < "$HOME/.claude/oauth-token" \
      || die "could not copy the Claude token into $m"
  fi
  if [ "$(fact github)" != yes ] && command -v gh >/dev/null 2>&1; then
    token=$(gh auth token 2>/dev/null || true)
    if [ -n "$token" ]; then
      printf '%s\n' "$token" | in_machine "$m" 'gh auth login --with-token >/dev/null 2>&1 && gh auth setup-git' || say "warning: the GitHub login was not accepted in $m"
    fi
  fi
  if [ -z "$(fact git_name)" ] || [ -z "$(fact git_email)" ]; then
    name=$(git config --global user.name 2>/dev/null || true)
    email=$(git config --global user.email 2>/dev/null || true)
    if [ -n "$name" ] && [ -n "$email" ]; then
      printf '%s\n%s\n' "$name" "$email" \
        | in_machine "$m" 'IFS= read -r n; IFS= read -r e; git config --global user.name "$n" && git config --global user.email "$e"'
    fi
  fi
}
logins "$template"
# The template is a disk to copy, not a machine to run: stopped, a clone of it is a clean copy,
# and it costs nothing while engineers work. `orb -m org-base` starts it again to add a tool.
"$orb" stop "$template" >/dev/null 2>&1 || true

# ---- the manager

if exists "$manager"; then
  say "manager:  $manager"
else
  "$orb" clone "$template" "$manager" >/dev/null || die "could not clone $template into $manager"
  say "manager:  $manager, cloned from $template"
fi
"$orb" start "$manager" >/dev/null 2>&1 || die "$manager did not start"
facts=$(ORG_UP_NO_INSTALL=1 bootstrap "$manager") || die "$manager is not answering"
[ -z "$(fact missing)" ] || die "$manager lacks: $(fact missing). It predates this template: \`org down --purge\` and \`org up\` rebuild it (the ledger goes with it), or install them there."
logins "$manager"

# org itself: this checkout, copied into the manager. The LOCAL file tells the copy's `org`
# wrapper that it is the installation, not a way in to one.
COPYFILE_DISABLE=1 tar --no-xattrs -czf - -C "$root" --exclude '__pycache__' --exclude '.DS_Store' org bin tests | base64 \
  | in_machine "$manager" '
      set -e
      d="$HOME/.local/share/org"
      rm -rf "$d.new" "$d.old"
      mkdir -p "$d.new" "$HOME/.local/bin"
      base64 -d | tar -xzf - -C "$d.new"
      touch "$d.new/LOCAL"
      if [ -d "$d" ]; then mv "$d" "$d.old"; fi
      mv "$d.new" "$d"
      rm -rf "$d.old"
      ln -sfn "$d/bin/org" "$HOME/.local/bin/org"' \
  || die "could not install org in $manager"

in_machine "$manager" '
  set -e
  org="$HOME/.local/bin/org"
  "$org" init >/dev/null
  "$org" config set machines.default orb >/dev/null
  "$org" config set machines.orb.via agent >/dev/null
  "$org" config set machines.orb.base "$1" >/dev/null
  "$org" config set machines.orb.prefix "$2" >/dev/null
  "$org" config set dash.port "$4" >/dev/null
  "$org" config set manager.own_claude true >/dev/null
  if [ -n "$3" ]; then "$org" config set machines.orb.slots "$3" >/dev/null
  elif [ "$("$org" config get machines.orb.slots)" = null ]; then "$org" config set machines.orb.slots 4 >/dev/null; fi
  # The Claude Code in this machine is a fresh install, and its default model is not the one to
  # write specs with. A choice already made is kept.
  if [ "$("$org" config get manager.model)" = null ]; then "$org" config set manager.model fable >/dev/null; fi
  if [ "$("$org" config get manager.effort)" = null ]; then "$org" config set manager.effort high >/dev/null; fi
' org "$template" "$prefix" "$slots" "$port" || die "could not set up the ledger in $manager"

# ---- the host agent

mkdir -p "$host_dir" && chmod 700 "$host_dir"
python3 - "$host_dir/agent.json" "$orb" "$manager" "$template" "$prefix" "$port" <<'PY' || die "could not write $host_dir/agent.json"
import json, sys
path, orb, manager, template, prefix, port = sys.argv[1:7]
with open(path, "w") as f:
    json.dump({"orb": orb, "manager": manager, "template": template, "prefix": prefix, "dash_port": int(port)}, f, indent=2, sort_keys=True)
    f.write("\n")
PY
stop_agent || true
PYTHONPATH="$root" nohup python3 -m org agent run --config "$host_dir/agent.json" >>"$host_dir/agent.log" 2>&1 </dev/null &
echo $! > "$host_dir/agent.pid"

connected=""
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40; do
  if in_machine "$manager" "$ORG agent status" >/dev/null 2>&1; then connected=1; break; fi
  kill -0 "$(cat "$host_dir/agent.pid")" 2>/dev/null || break
  sleep 0.5
done
if [ -z "$connected" ]; then
  tail -5 "$host_dir/agent.log" >&2
  die "the host agent did not connect to $manager; its log is $host_dir/agent.log"
fi
say "agent:    connected; up to $(in_machine "$manager" "$ORG config get machines.orb.slots") engineers at once, as $prefix<run>"

say ""
if in_machine "$manager" "$ORG login --status"; then
  say ""
  say "ready.   org talk      talk to the manager"
  say "         org dash      $(in_machine "$manager" "$ORG dash --url")"
  say "         org status    what is running"
else
  say ""
  say "one step left:  org login     (the Claude token and GitHub login the engineers are given)"
  say "then:           org talk      org dash      org status"
fi
