#!/bin/bash
# up.sh: `org up` and `org down`, on the Linux machine that holds everything.
#
#   org up                      install this copy of org, make what is missing, start the dashboard
#   org up --rebuild-template   also build a new template for engineers and switch to it
#   org up --slots 6            engineers that may run at once (default 4)
#   org up --port 9000          the dashboard's port (default 8787)
#   org up --owner org-004      the GitHub organization specs may name repositories in
#   org down                    stop the dashboard. Engineers keep running.
#
# Run as the user org runs as. It needs no root: the machine was prepared by whoever made it
# (Incus installed, this user in the `incus` group, Claude Code's managed settings in /etc).
#
# What it makes, keeping whatever already exists:
#
#   ~/.local/share/org/releases/<id>   this copy; `current` points at the one in use, so going
#                                      back is moving the link
#   org-base                           the template container: Debian with python3, git, gh, tmux,
#                                      Docker and Claude Code, and no login. Left stopped.
#   org-cache                          the build cache: a container of its own that every engineer
#                                      reads and writes, with no login. Left running.
#                                      (machines.incus.cache.enabled false: not made, not used)
#   org-dash.service                   the dashboard, a service of this user
#
# Run it again after fetching a new version; it takes a few seconds when everything exists.
set -eu

here=$(cd "$(dirname "$0")/.." && pwd -P)
share="$HOME/.local/share/org"
export PATH="$HOME/.local/bin:$PATH"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

say() { printf 'org: %s\n' "$*"; }
die() { printf 'org: %s\n' "$*" >&2; exit 1; }
inc() { incus "$@" </dev/null; }   # incus reads stdin when it is not a terminal; never ours

down="" rebuild="" slots="" port="" owner=""
while [ $# -gt 0 ]; do
  case "$1" in
    --down) down=1; shift ;;
    --rebuild-template) rebuild=1; shift ;;
    --slots) [ $# -ge 2 ] || die "--slots takes a number"; slots="$2"; shift 2 ;;
    --port) [ $# -ge 2 ] || die "--port takes a number"; port="$2"; shift 2 ;;
    --owner) [ $# -ge 2 ] || die "--owner takes a GitHub organization or user"; owner="$2"; shift 2 ;;
    -h|--help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option $1 (org up --help)" ;;
  esac
done

if [ -n "$down" ]; then
  systemctl --user disable --now org-dash.service >/dev/null 2>&1 || true
  say "the dashboard is stopped. Engineers that were running keep running; \`org up\` brings the view of them back."
  exit 0
fi

[ "$(uname -s)" = Linux ] || die "this runs on the Linux machine that holds the engineers, not on $(uname -s)"
[ "$(id -u)" != 0 ] || die "run as the org user, not as root"
command -v incus >/dev/null || die "incus is not installed here; this machine is made by org-vm on the host"
inc list -f csv >/dev/null 2>&1 || die "incus does not answer for $(id -un). Is this user in the \`incus\` group?"
command -v python3 >/dev/null || die "python3 is not installed"

# ---- 1. this copy of org, as a release of its own
if [ -d "$here/.git" ] && rev=$(git -C "$here" rev-parse --short=12 HEAD 2>/dev/null); then
  # Work that is not committed is not what the commit's name says it is.
  [ -z "$(git -C "$here" status --porcelain 2>/dev/null)" ] || rev="$rev-dirty-$(date +%Y%m%d%H%M%S)"
else
  rev="copy-$(date +%Y%m%d%H%M%S)"
fi
case "$here" in
  "$share"/releases/*) rel="$here" ;;   # `org up` from the installed copy: nothing to install
  *)
    rel="$share/releases/$rev"
    if [ ! -d "$rel" ]; then
      mkdir -p "$rel.tmp"
      tar -C "$here" --exclude __pycache__ --exclude '*.pyc' -cf - org bin incus tests | tar -C "$rel.tmp" -xf -
      printf '%s\n' "$rev" > "$rel.tmp/RELEASE"
      mv "$rel.tmp" "$rel"
    fi
    ;;
esac
mkdir -p "$HOME/.local/bin"
before=$(readlink "$share/current" 2>/dev/null || true)
# The release in use until now stays on disk and is remembered: `org upgrade --rollback` returns to it.
if [ -n "$before" ] && [ "$before" != "releases/$(basename "$rel")" ]; then ln -sfn "$before" "$share/previous"; fi
ln -sfn "releases/$(basename "$rel")" "$share/current"
ln -sf "$share/current/bin/org" "$HOME/.local/bin/org"
org="$HOME/.local/bin/org"
say "release $(basename "$rel")$([ -n "$before" ] && [ "$before" != "releases/$(basename "$rel")" ] && echo " (was ${before#releases/})")"

# ---- 2. Claude Code for this user (the manager's sessions)
if ! command -v claude >/dev/null 2>&1; then
  say "installing Claude Code"
  curl -fsSL https://claude.ai/install.sh | bash >/dev/null 2>&1 || die "Claude Code did not install (https://claude.ai/install.sh)"
fi
[ -e /etc/claude-code/managed-mcp.json ] || say "warning: /etc/claude-code/managed-mcp.json is missing, so nothing here forbids MCP servers"
if [ -e "$HOME/.claude/.credentials.json" ]; then
  say "warning: ~/.claude/.credentials.json exists: someone ran \`claude /login\` here. That login can reach the account's connectors; remove the file."
fi

# ---- 3. the ledger and its settings
"$org" init >/dev/null
cfg() { "$org" config set "$1" "$2" >/dev/null; }
get() { "$org" config get "$1" 2>/dev/null || true; }
cfg machines.default '"incus"'
cfg manager.own_claude true
[ -z "$slots" ] || cfg machines.incus.slots "$slots"
[ -z "$port" ] || cfg dash.port "$port"
[ -z "$owner" ] || cfg github.owner "\"$owner\""
case "$(get manager.model)" in ""|null) cfg manager.model '"fable"'; cfg manager.effort '"high"' ;; esac
base=$(get machines.incus.base | tr -d '"'); base=${base:-org-base}
prefix=$(get machines.incus.prefix | tr -d '"'); prefix=${prefix:-org-eng-}
case "$base" in "$prefix"*) die "the template \`$base\` starts with the engineers' prefix \`$prefix\`" ;; esac
# The build cache (step 5). The prefix marks what a run may be asked to delete, and the cache is never that.
cache_on=$(get machines.incus.cache.enabled)
if [ "$cache_on" != false ]; then
  cache=$(get machines.incus.cache.name | tr -d '"'); cache=${cache:-org-cache}
  cache_port=$(get machines.incus.cache.port | tr -d '"'); cache_port=${cache_port:-9092}
  cache_gb=$(get machines.incus.cache.max_gb | tr -d '"'); cache_gb=${cache_gb:-20}
  case "$cache" in "$prefix"*) die "the build cache \`$cache\` starts with the engineers' prefix \`$prefix\`" ;; esac
  [ "$cache" != "$base" ] || die "the build cache and the template are both named \`$cache\`"
  case "$cache_port" in ""|*[!0-9]*) die "machines.incus.cache.port must be a number, not $cache_port" ;; esac
  case "$cache_gb" in ""|*[!0-9]*) die "machines.incus.cache.max_gb must be a whole number of gigabytes, not $cache_gb" ;; esac
fi

# ---- 4. the template every engineer is a copy of
# The status of a container by exact name, or nothing when there is none.
status_of() { inc list --format json "^$1\$" | python3 -c 'import json,sys
for c in json.load(sys.stdin):
    if c.get("name") == sys.argv[1]: print(c.get("status") or "?")' "$1"; }
exists() { [ -n "$(status_of "$1")" ]; }
build_template() {
  local name="$1" facts
  say "template: building $name (a few minutes)"
  inc init -q images:debian/13 "$name" -c security.nesting=true
  # Its network card: a port the other engineers' containers cannot reach, and an address it cannot change.
  inc config device override "$name" eth0 security.port_isolation=true security.ipv4_filtering=true >/dev/null
  inc start -q "$name"
  incus exec "$name" -T -- sh -c 'for i in $(seq 1 60); do ip -4 -o addr show scope global 2>/dev/null | grep -q inet && exit 0; sleep 0.5; done; exit 1' </dev/null \
    || die "template: $name got no network address"
  facts=$(incus exec "$name" -T -- sh -s < "$rel/incus/bootstrap.sh") || die "template: the bootstrap did not finish in $name"
  inc stop -q "$name"
  missing=$(printf '%s\n' "$facts" | sed -n 's/^missing=//p')
  [ -z "$missing" ] || die "template: $name is missing $missing"
  [ "$(printf '%s\n' "$facts" | sed -n 's/^login=//p')" = no ] || die "template: $name has a login in it; a template must be blank"
  say "template: $name ready ($(printf '%s\n' "$facts" | sed -n 's/^claude=//p'); docker $(printf '%s\n' "$facts" | sed -n 's/^docker=//p'))"
}
if [ -n "$rebuild" ]; then
  new="org-base-$(date +%Y%m%d%H%M%S)"
  build_template "$new"
  cfg machines.incus.base "\"$new\""
  say "template: engineers are now copies of $new. $base is left as it was; delete it when no run needs it: incus delete $base"
elif exists "$base"; then
  # A template that is running can change under the copies made of it.
  [ "$(status_of "$base")" != Running ] || inc stop -q "$base"
  say "template: $base is here"
else
  build_template "$base"
fi

# ---- 5. the build cache every engineer reads and writes
# A container of its own beside the engineers', running one program that serves Bazel's remote
# cache on one port. It holds no login and asks for none. Engineers are told of it at dispatch,
# and only when they can reach it. `org down` leaves it running, with what it holds.
# A cache that could not be made is refused at the end, after the rest is in place: nothing else
# here depends on it, and engineers build without it.
cache_failed=""
if [ "$cache_on" != false ]; then
  build_cache() {
    local name="$1" facts
    say "build cache: making $name"
    # Unprivileged and without nesting: it runs one server and no containers of its own. It comes
    # back by itself when this machine restarts.
    inc init -q images:debian/13 "$name" -c boot.autostart=true
    # Its network card: an address it cannot change, as an engineer's card has. It is not given
    # the port isolation an engineer's card has, and that is the point: an isolated card cannot
    # reach another isolated card, so engineers could never reach a cache that had it.
    inc config device override "$name" eth0 security.ipv4_filtering=true >/dev/null
    inc start -q "$name"
    incus exec "$name" -T -- sh -c 'for i in $(seq 1 60); do ip -4 -o addr show scope global 2>/dev/null | grep -q inet && exit 0; sleep 0.5; done; exit 1' </dev/null \
      || { inc delete -q -f "$name"; cache_failed="build cache: $name got no network address, so it was removed"; return 0; }
    # The script is its stdin. Nothing else goes in: no login, no key, nothing of this user's.
    facts=$(incus exec "$name" -T --env "ORG_CACHE_PORT=$cache_port" --env "ORG_CACHE_MAX_GB=$cache_gb" -- sh -s < "$rel/incus/cache.sh") || true
    if [ "$(printf '%s\n' "$facts" | sed -n 's/^server=//p')" != up ]; then
      # Removed, so that the next `org up` makes it again and does not take a broken one for the cache.
      inc delete -q -f "$name"
      cache_failed="build cache: the server did not come up in $name (sha256: $(printf '%s\n' "$facts" | sed -n 's/^sha256=//p')), so it was removed. Engineers build without it; \`org up\` tries again."
      return 0
    fi
    say "build cache: $name ready (bazel-remote $(printf '%s\n' "$facts" | sed -n 's/^version=//p'), port $cache_port, at most $cache_gb GB)"
  }
  if exists "$cache"; then
    # An existing one is left as it is, with what it holds. To empty it: incus delete -f, then `org up`.
    [ "$(status_of "$cache")" = Running ] || inc start -q "$cache"
    say "build cache: $cache is here"
  else
    build_cache "$cache"
  fi
fi

# ---- 6. the dashboard, as a service of this user
unit="$HOME/.config/systemd/user/org-dash.service"
mkdir -p "$(dirname "$unit")"
cat > "$unit" <<UNIT
[Unit]
Description=org dashboard

[Service]
ExecStart=$HOME/.local/bin/org dash --no-open
Restart=on-failure
RestartSec=3
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
UNIT
if systemctl --user daemon-reload 2>/dev/null; then
  # Restarted, so that it runs the release just installed and not the one it started from.
  systemctl --user enable -q org-dash.service
  systemctl --user restart org-dash.service
  say "dashboard: $("$org" dash --url)"
else
  say "warning: no user service manager here (is lingering enabled for $(id -un)?). Start the dashboard by hand: org dash --no-open"
fi

# ---- 7. what is still missing
echo
"$org" doctor || true
[ -z "$cache_failed" ] || die "$cache_failed"
