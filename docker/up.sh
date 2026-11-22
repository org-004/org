#!/bin/bash
# org up / org down: the whole installation, on a machine that has only a Docker engine.
#
#   org up [--no-build]        build the image, start the manager, hand it this machine's logins
#   org down [--all] [--purge] remove the manager (--all: engineers too; --purge: ledger and logins)
#
# What `up` creates:
#   image     org:latest          one image for the manager and every engineer
#   container org-manager         the dashboard as its main process; `org talk` execs into it
#   volumes   org-ledger          specs, runs, budget            (/home/org/.org)
#             org-claude          Claude Code's settings, token and conversations
#             org-gh              the GitHub login
#             org-ssh             ssh keys, for reaching remote Docker hosts in the pool
#   networks  org-manager         the manager's
#             org-engineers       the engineers'; containers on it cannot reach each other
#
# The manager is given the Docker socket, which is how it starts and enters engineer containers.
# That makes it root on the Docker host: it is the control plane, and what confines the manager
# chat is its permission rules, not its container. Engineers get no socket.
#
# Environment: ORG_PORT (8787), ORG_IMAGE (org:latest), ORG_MANAGER (org-manager),
#              ORG_DOCKER_SOCK (/var/run/docker.sock, as the daemon sees it).
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
image="${ORG_IMAGE:-org:latest}"
manager="${ORG_MANAGER:-org-manager}"
port="${ORG_PORT:-8787}"
sock="${ORG_DOCKER_SOCK:-/var/run/docker.sock}"

say() { printf '%s\n' "$*"; }
die() { printf 'org: %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker is not installed. Any engine will do: OrbStack, Docker Desktop, Colima, or Docker on Linux."
docker version >/dev/null 2>&1 || die "the Docker daemon is not answering. Start it and run this again."

cmd="${1:-up}"
[ $# -gt 0 ] && shift

case "$cmd" in
down)
  all=""; purge=""
  for a in "$@"; do case "$a" in --all) all=1 ;; --purge) purge=1 ;; *) die "usage: org down [--all] [--purge]" ;; esac; done
  if docker rm -f "$manager" >/dev/null 2>&1; then say "manager removed"; else say "no manager container"; fi
  engineers=$(docker ps -aq --filter label=org.role=engineer)
  if [ -n "$all" ] && [ -n "$engineers" ]; then
    docker rm -f $engineers >/dev/null
    say "engineer containers removed"
  elif [ -n "$engineers" ]; then
    say "engineer containers are left as they are: $(docker ps -q --filter label=org.role=engineer | wc -l | tr -d ' ') running (\`org down --all\` removes them)"
  fi
  if [ -n "$purge" ]; then
    docker volume rm org-ledger org-claude org-gh org-ssh >/dev/null 2>&1 || true
    say "volumes removed: the ledger and the logins are gone"
  else
    say "the ledger and logins stay in the volumes org-ledger, org-claude, org-gh and org-ssh (\`org down --purge\` deletes them)"
  fi
  exit 0 ;;
up) ;;
*) die "usage: org up [--no-build] | org down [--all] [--purge]" ;;
esac

build=1
for a in "$@"; do case "$a" in --no-build) build="" ;; *) die "usage: org up [--no-build]" ;; esac; done

if [ -n "$build" ]; then
  say "building $image (the first build downloads Claude Code and a compiler toolchain; later ones are quick)"
  docker build -t "$image" -f "$root/docker/Dockerfile" "$root" || die "the image did not build"
elif ! docker image inspect "$image" >/dev/null 2>&1; then
  die "$image does not exist yet; run \`org up\` without --no-build"
fi

for v in org-ledger org-claude org-gh org-ssh; do docker volume create "$v" >/dev/null; done
docker network inspect org-manager >/dev/null 2>&1 || docker network create org-manager >/dev/null
docker network inspect org-engineers >/dev/null 2>&1 \
  || docker network create -o com.docker.network.bridge.enable_icc=false org-engineers >/dev/null

# The socket's group as the daemon's side sees it, so the manager's user can use it without root.
gid=$(docker run --rm -v "$sock":/var/run/docker.sock --entrypoint stat "$image" -c %g /var/run/docker.sock) \
  || die "cannot mount the Docker socket from $sock (set ORG_DOCKER_SOCK to its path on the Docker host)"

tz="${TZ:-}"
if [ -z "$tz" ] && [ -L /etc/localtime ]; then tz=$(readlink /etc/localtime | sed 's|.*/zoneinfo/||'); fi
[ -n "$tz" ] || tz="UTC"

# no-new-privileges makes the image's sudo inert in the manager. seccomp=unconfined and
# systempaths=unconfined are what let Claude Code's shell sandbox (bubblewrap) create its own
# namespaces and process view inside a container. The sandbox is what confines the manager chat;
# the container wall is not a boundary for something that holds the Docker socket anyway.
docker rm -f "$manager" >/dev/null 2>&1 || true
docker run -d --init --name "$manager" --hostname org-manager --restart unless-stopped \
  --label org.role=manager --network org-manager \
  --security-opt no-new-privileges --security-opt seccomp=unconfined --security-opt systempaths=unconfined \
  -p "127.0.0.1:${port}:8787" -e ORG_DASH_PUBLIC_PORT="$port" -e TZ="$tz" \
  --group-add "$gid" \
  -v "$sock":/var/run/docker.sock \
  -v org-ledger:/home/org/.org -v org-claude:/home/org/.claude -v org-gh:/home/org/.config/gh -v org-ssh:/home/org/.ssh \
  "$image" org dash --port 8787 --no-open >/dev/null \
  || die "the manager did not start"

ready=""
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  if docker exec "$manager" org dash --url >/dev/null 2>&1; then ready=1; break; fi
  sleep 0.5
done
[ -n "$ready" ] || { docker logs "$manager" 2>&1 | tail -5; die "the manager started but is not answering"; }
docker exec "$manager" docker version >/dev/null 2>&1 || die "the manager cannot reach the Docker daemon through $sock"
say "manager running: $manager"

# Hand over what this machine already has, so the common case needs no second step.
if [ -s "$HOME/.claude/oauth-token" ]; then
  docker exec -i "$manager" org login --token-stdin < "$HOME/.claude/oauth-token" >/dev/null && say "Claude token: copied from this machine"
fi
if command -v gh >/dev/null 2>&1; then
  token=$(gh auth token 2>/dev/null || true)
  if [ -n "$token" ]; then
    printf '%s\n' "$token" | docker exec -i "$manager" sh -c 'gh auth login --with-token >/dev/null 2>&1 && gh auth setup-git' \
      && say "GitHub login: copied from this machine"
  fi
fi
if command -v git >/dev/null 2>&1; then
  name=$(git config --global user.name 2>/dev/null || true)
  email=$(git config --global user.email 2>/dev/null || true)
  if [ -n "$name" ] && [ -n "$email" ]; then
    docker exec "$manager" git config --global user.name "$name"
    docker exec "$manager" git config --global user.email "$email"
  fi
fi
docker exec "$manager" org init >/dev/null

say ""
if docker exec "$manager" org login --status; then
  say ""
  say "ready.   org talk      talk to the manager"
  say "         org dash      $(docker exec "$manager" org dash --url)"
  say "         org status    what is running"
else
  say ""
  say "one step left:  org login     (the Claude token and GitHub login the engineers are given)"
  say "then:           org talk      org dash      org status"
fi
