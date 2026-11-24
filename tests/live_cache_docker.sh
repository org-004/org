#!/bin/sh
# live_cache_docker.sh: the build cache, proved with the real programs in two Docker containers.
#
# A bare debian:13 container is set up by incus/cache.sh and runs bazel-remote with the command
# line and user written in the service that script makes. A second one, the engineer, gets the
# Bazel settings dispatch writes (org.cache.bazelrc) and builds a small C++ workspace: once to
# fill the cache, once from a new output base that must only download, and three times with the
# cache gone (server killed, container stopped, and from nothing), which must still succeed.
#
# Needs Docker, python3 and the internet. Takes about 4 minutes. Exits 0 only if all of it held;
# removes every container, network and file it made, however it ends.
#   sh tests/live_cache_docker.sh
set -u

root=$(cd "$(dirname "$0")/.." && pwd -P)
port=9092
id="org-cache-test-$$"
net="$id-net" cache="$id-cache" eng="$id-eng"
tmp=$(mktemp -d)
bazelisk=1.30.0
case "$(uname -m)" in
  aarch64|arm64) arch=arm64; bsum=b2bcc040c213b44586b121bda6d983c35df121b917c3a7a66fb16f2fa0df1a4f ;;
  x86_64|amd64) arch=amd64; bsum=a248ae2b2b62b5baebb748f6fba7203bff1a27e5d01b55399a8406744cb539e8 ;;
  *) echo "FAIL: no pinned Bazelisk for $(uname -m)"; exit 1 ;;
esac

cleanup() {
  docker rm -f "$cache" "$eng" >/dev/null 2>&1
  docker network rm "$net" >/dev/null 2>&1
  rm -rf "$tmp"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

step() { printf -- '- %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*"; exit 1; }
in_cache() { docker exec "$cache" sh -c "$1"; }
in_eng() { docker exec "$eng" sh -c "$1"; }
probe() {  # the probe dispatch runs in the engineer's container; prints why it failed
  out=$(in_eng "python3 -c 'import socket, sys
socket.create_connection((sys.argv[1], int(sys.argv[2])), 3).close()' $1 $2" 2>&1)
  rc=$?
  printf '%s\n' "$out" | tail -n 1
  return "$rc"
}
server_pid() { in_cache 'for p in /proc/[0-9]*; do [ "$(cat $p/comm 2>/dev/null)" = bazel-remote ] && echo ${p#/proc/}; done'; }
hello() {  # $1: the workspace, $2: what the binary prints
  printf '#include <iostream>\n#include "greet.h"\nint main() { std::cout << greet("%s") << std::endl; }\n' "$2" \
    | docker exec -i "$eng" sh -c "cat > /work/$1/hello.cc"
}
build() {  # $1: the workspace, $2: the output base; leaves Bazel's output in $tmp/build.log
  docker exec -w "/work/$1" "$eng" bazel --output_base="/ob/$2" build //:hello >"$tmp/build.log" 2>&1
  ok=$?
  # rules_cc 0.2.14 resolves to a later version, and Bazel says so on each fresh analysis; not ours.
  grep -E '^(WARNING|ERROR):' "$tmp/build.log" | grep -v 'requires module version' >"$tmp/warn"
  sed 's/^/    /' "$tmp/warn" | head -n 8
  [ -s "$tmp/warn" ] || echo "    (Bazel printed no WARNING, apart from the rules_cc version one)"
  grep -E 'INFO: [0-9]+ process|INFO: Elapsed|Build completed successfully' "$tmp/build.log" | sed 's/^/    /'
  [ "$ok" = 0 ] && grep -q 'Build completed successfully' "$tmp/build.log" || { tail -n 20 "$tmp/build.log"; return 1; }
}
runs() {  # $1: the workspace, $2: what bazel-bin/hello must print
  out=$(docker exec -w "/work/$1" "$eng" ./bazel-bin/hello 2>&1) && [ "$out" = "hello, $2" ] \
    || fail "bazel-bin/hello in $1 printed '$out', not 'hello, $2'"
  echo "    bazel-bin/hello: $out"
}

step "a. network $net, and the cache container from bare debian:13, set up by incus/cache.sh"
docker network create "$net" >/dev/null || fail "docker network create"
docker run -d --init --name "$cache" --network "$net" debian:13 sleep infinity >/dev/null || fail "docker run (cache)"
docker exec -i -e ORG_CACHE_PORT=$port -e ORG_CACHE_MAX_GB=2 "$cache" sh -s <"$root/incus/cache.sh" >"$tmp/facts" 2>"$tmp/progress" \
  || { cat "$tmp/progress"; fail "incus/cache.sh exited non-zero"; }
echo "    facts: $(tr '\n' ' ' <"$tmp/facts")"
grep -qx sha256=ok "$tmp/facts" || fail "cache.sh did not report sha256=ok"
grep -qx service=none "$tmp/facts" || fail "cache.sh did not report service=none"

step "b. the server, started with the service's own ExecStart= and User="
unit=/etc/systemd/system/bazel-remote.service
cmd=$(in_cache "sed -n 's/^ExecStart=//p' $unit")
user=$(in_cache "sed -n 's/^User=//p' $unit")
[ -n "$cmd" ] && [ -n "$user" ] || fail "no ExecStart= or User= in $unit"
echo "    as $user: $cmd"
docker exec -d -u "$user" "$cache" sh -c "exec $cmd >/tmp/bazel-remote.log 2>&1" || fail "could not start the server"
addr=$(docker inspect -f "{{(index .NetworkSettings.Networks \"$net\").IPAddress}}" "$cache")
i=0
until docker run --rm --network "$net" debian:13 timeout 3 bash -c "exec 3<>/dev/tcp/$addr/$port" 2>/dev/null; do
  i=$((i + 1)); [ "$i" -lt 15 ] || fail "nothing answers at $addr:$port from another container"; sleep 1
done
echo "    $addr:$port answers from another container"
pid=$(server_pid)
owner=$(in_cache "stat -c %U /proc/$pid")
[ -n "$pid" ] && [ "$owner" = "$user" ] && [ "$owner" != root ] || fail "the server (pid '$pid') runs as '$owner'"
echo "    the server runs as $owner"

step "c. the engineer container: g++, python3, curl, Bazelisk $bazelisk (sha256 checked)"
docker run -d --init --name "$eng" --network "$net" debian:13 sleep infinity >/dev/null || fail "docker run (engineer)"
in_eng "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends g++ python3 ca-certificates curl" \
  >/dev/null 2>&1 || fail "apt-get install in the engineer container"
in_eng "curl -fsSL -o /tmp/bazel https://github.com/bazelbuild/bazelisk/releases/download/v$bazelisk/bazelisk-linux-$arch \
  && echo '$bsum  /tmp/bazel' | sha256sum -c --quiet && install -m 755 /tmp/bazel /usr/local/bin/bazel" \
  || fail "Bazelisk did not download, or its SHA-256 is not the pinned one"
why=$(probe "$addr" "$port") && [ -z "$why" ] || fail "dispatch's probe to $addr:$port failed: $why"
echo "    dispatch's probe reaches $addr:$port"
why=$(probe "$addr" 8080) && fail "the HTTP port $addr:8080 is reachable from the engineer"
echo "    $addr:8080 is not reachable: $why"

step "d. /etc/bazel.bazelrc, from org.cache.bazelrc"
python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from org.cache import bazelrc; sys.stdout.write(bazelrc(sys.argv[2], int(sys.argv[3])))' \
  "$root" "$addr" "$port" | docker exec -i "$eng" sh -c 'cat > /etc/bazel.bazelrc' || fail "could not write /etc/bazel.bazelrc"
in_eng 'cat /etc/bazel.bazelrc' | sed 's/^/    | /'

step "e. a workspace: Bazel 9.3.0, rules_cc 0.2.14, a library and a binary"
w="$tmp/ws"
mkdir -p "$w"
echo 9.3.0 >"$w/.bazelversion"
echo 'bazel_dep(name = "rules_cc", version = "0.2.14")' >"$w/MODULE.bazel"
cat >"$w/BUILD.bazel" <<'EOF'
load("@rules_cc//cc:cc_binary.bzl", "cc_binary")
load("@rules_cc//cc:cc_library.bzl", "cc_library")

cc_library(name = "greet", srcs = ["greet.cc"], hdrs = ["greet.h"])

cc_binary(name = "hello", srcs = ["hello.cc"], deps = [":greet"])
EOF
printf '#include <string>\nstd::string greet(const std::string& who);\n' >"$w/greet.h"
printf '#include "greet.h"\nstd::string greet(const std::string& who) { return "hello, " + who; }\n' >"$w/greet.cc"
in_eng 'mkdir -p /work /ob' && docker cp "$w" "$eng:/work/src" >/dev/null || fail "could not copy the workspace"
in_eng 'for d in d1 d2 d3; do cp -r /work/src /work/$d; done'
hello d1 world && hello d2 world && hello d3 world

step "f. build in d1: fills the cache"
build d1 1 || fail "the first build failed"
runs d1 world
n=$(in_cache 'find /var/lib/bazel-remote/ac.v2 -type f | wc -l')
[ "$n" -gt 0 ] || fail "the cache holds no action results after the first build"
echo "    the server holds $n action results (ac.v2), $(in_cache 'find /var/lib/bazel-remote/cas.v2 -type f | wc -l') blobs (cas.v2)"

step "g. build a copy in d2 with a new output base: everything from the cache"
build d2 2 || fail "the second build failed"
line=$(grep -E 'INFO: [0-9]+ processes?:' "$tmp/build.log")
case "$line" in *"remote cache hit"*) ;; *) fail "no remote cache hits: $line" ;; esac
case "$line" in *sandbox*|*local*|*worker*) fail "an action was run, not downloaded: $line" ;; esac
runs d2 world
echo "    no compile or link action was run"

step "h1. server killed, container up: d2 changed and built"
in_cache "kill $(server_pid)"
sleep 1
why=$(probe "$addr" "$port") && fail "the server still answers after it was killed"
echo "    the probe now says: $why"
hello d2 killed
build d2 2 || fail "the build with the server killed failed"
runs d2 killed

step "h2. cache container stopped: d2 changed and built"
docker stop "$cache" >/dev/null || fail "docker stop"
why=$(probe "$addr" "$port") && fail "the address still answers after the container stopped"
echo "    the probe now says: $why"
hello d2 stopped
build d2 2 || fail "the build with the cache container stopped failed"
runs d2 stopped

step "h3. cache still stopped: d3 from nothing, with a new output base"
build d3 3 || fail "the build from nothing with the cache stopped failed"
runs d3 world

echo "PASS: cache.sh installed bazel-remote 2.6.4 (sha256 ok) running as $owner; dispatch's probe reached $addr:$port and not :8080;"
echo "PASS: a second output base built only from the cache; with the server killed, the container stopped, and from nothing, builds succeeded locally."
