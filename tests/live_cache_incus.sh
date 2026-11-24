#!/bin/bash
# live_cache_incus.sh: the build cache, proved with real containers in an Incus of one's own.
#
# An engineer cannot use the VM's Incus, but its container has security.nesting, so it can run
# one inside itself. This script needs that Incus and root in the container that holds it:
#
#   apt-get install -y incus
#   printf 'root:1000000:500000000\n' > /etc/subuid      # Debian asks for a billion ids from
#   printf 'root:1000000:500000000\n' > /etc/subgid      # 1000000, and an engineer's container
#   systemctl restart incus                              # has a billion in all: without this no
#   incus admin init --auto                              # nested container starts (newuidmap)
#   tests/live_cache_incus.sh
#
# What it does, and it exits 0 only if all of it held:
#
#   1. makes a user that is not root, in the `incus` group, with a home of its own and a copy
#      of this checkout: `org up` refuses root, and Incus gives such a user a project and a
#      bridge of its own, as on the VM;
#   2. as that user, makes a stand-in for the template with the two commands `org up` uses for
#      it (nesting, a card with port isolation and a pinned address) and puts g++, python3 and
#      Bazelisk in it in place of the real bootstrap;
#   3. as that user, runs `org up` itself, all of it: that makes the cache container with
#      incus/cache.sh under a real service manager (a stand-in `claude` is on its PATH, so that
#      no Claude Code is installed for a test);
#   4. with the code `org up` installed, does what dispatch does for two runs: the backend
#      copies the template, `vet` and `problems()` judge the copy, and the cache is offered:
#      the probe from inside the copy, and Bazel's settings file with the address Incus gave;
#   5. builds a small C++ workspace with Bazel in the first container, which uploads, and in the
#      second, which must get cache hits and run no compile or link;
#   6. shows that the two engineers' containers still cannot open a connection to each other;
#   7. removes the two through the backend and shows the cache untouched, and that the backend
#      refuses to remove the cache.
#
# It then deletes the user, its Incus project (containers, image) and its bridge, however it ends.
# Takes one to five minutes; needs the internet (Debian's packages, Bazelisk, Bazel 9.3.0, bazel-remote).
set -u

root=$(cd "$(dirname "$0")/.." && pwd -P)
user="orgcache$$"
uid=""
bazelisk=1.30.0
case "$(uname -m)" in
  aarch64|arm64) arch=arm64; bsum=b2bcc040c213b44586b121bda6d983c35df121b917c3a7a66fb16f2fa0df1a4f ;;
  x86_64|amd64) arch=amd64; bsum=a248ae2b2b62b5baebb748f6fba7203bff1a27e5d01b55399a8406744cb539e8 ;;
  *) echo "FAIL: no pinned Bazelisk for $(uname -m)"; exit 1 ;;
esac

inc() { incus "$@" </dev/null; }   # incus reads stdin when it is not a terminal; never ours
fail() { printf 'FAIL: %s\n' "$*"; exit 1; }
cleanup() {
  [ -n "$uid" ] || return 0
  local project="user-$uid" each
  # One thing at a time: `incus project delete --force` asks a person to type yes.
  for each in $(inc list --project "$project" -f csv -c n 2>/dev/null); do inc delete -f --project "$project" "$each" >/dev/null 2>&1; done
  for each in $(inc image list --project "$project" -f csv -c f 2>/dev/null); do inc image delete --project "$project" "$each" >/dev/null 2>&1; done
  inc project delete "$project" >/dev/null 2>&1
  inc network delete "incusbr-$uid" >/dev/null 2>&1
  # The daemon that gives unprivileged users their projects remembers the one it made for this
  # uid; restarted, it makes a new one for the next user that is given the same uid.
  systemctl try-restart incus-user.service >/dev/null 2>&1
  pkill -KILL -u "$user" >/dev/null 2>&1
  userdel -r "$user" >/dev/null 2>&1
}
trap cleanup EXIT
trap 'exit 130' INT TERM

[ "$(id -u)" = 0 ] || fail "run as root in the container that holds the Incus: it makes and removes a user"
command -v incus >/dev/null || fail "incus is not installed here (the header of this file says how)"
inc list -f csv >/dev/null 2>&1 || fail "incus does not answer; has \`incus admin init --auto\` been run?"
getent group incus >/dev/null || fail "there is no \`incus\` group for an unprivileged user to be in"

echo "- 1. the user $user: not root, in the incus group, a home and a copy of this checkout of its own"
useradd -m -s /bin/bash -G incus "$user" || fail "useradd"
uid=$(id -u "$user")
home=$(getent passwd "$user" | cut -d: -f6)
mkdir "$home/checkout" && (cd "$root" && tar --exclude __pycache__ -cf - org bin incus tests) | tar -C "$home/checkout" -xf - \
  || fail "could not copy the checkout"
printf 'arch=%s\nbsum=%s\nbazelisk=%s\n' "$arch" "$bsum" "$bazelisk" > "$home/vars"

cat > "$home/live.sh" <<'LIVE'
set -u
. "$HOME/vars"
inc() { incus "$@" </dev/null; }
step() { printf -- '- %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*"; exit 1; }

step "2. a stand-in for the template, made as \`org up\` makes the template"
inc init -q images:debian/13 org-base -c security.nesting=true || fail "incus init org-base"
inc config device override org-base eth0 security.port_isolation=true security.ipv4_filtering=true >/dev/null || fail "the card's settings"
inc start -q org-base || fail "org-base did not start: $(inc info --show-log org-base 2>&1 | tail -n 5)"
incus exec org-base -T -- sh -c 'for i in $(seq 1 60); do ip -4 -o addr show scope global 2>/dev/null | grep -q inet && exit 0; sleep 0.5; done; exit 1' </dev/null \
  || fail "org-base got no network address"
# In place of incus/bootstrap.sh: only what the Bazel build needs. The workspace is written here
# and built nowhere, so every result the second container gets can only come from the cache.
incus exec org-base -T --env "ARCH=$arch" --env "BSUM=$bsum" --env "BAZELISK=$bazelisk" -- sh -s <<'TEMPLATE' || fail "the stand-in template could not be set up"
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends g++ python3 ca-certificates curl >/dev/null 2>&1
curl -fsSL -o /tmp/bazel "https://github.com/bazelbuild/bazelisk/releases/download/v$BAZELISK/bazelisk-linux-$ARCH"
echo "$BSUM  /tmp/bazel" | sha256sum -c --quiet
install -m 755 /tmp/bazel /usr/local/bin/bazel
mkdir -p /work/ws && cd /work/ws
echo 9.3.0 > .bazelversion
echo 'bazel_dep(name = "rules_cc", version = "0.2.14")' > MODULE.bazel
cat > BUILD.bazel <<'EOF'
load("@rules_cc//cc:cc_binary.bzl", "cc_binary")
load("@rules_cc//cc:cc_library.bzl", "cc_library")

cc_library(name = "greet", srcs = ["greet.cc"], hdrs = ["greet.h"])

cc_binary(name = "hello", srcs = ["hello.cc"], deps = [":greet"])
EOF
printf '#include <string>\nstd::string greet(const std::string& who);\n' > greet.h
printf '#include "greet.h"\nstd::string greet(const std::string& who) { return "hello, " + who; }\n' > greet.cc
printf '#include <iostream>\n#include "greet.h"\nint main() { std::cout << greet("engineers") << std::endl; }\n' > hello.cc
bazel --version >/dev/null 2>&1   # Bazelisk fetches Bazel 9.3.0 once, for both copies
: > /etc/machine-id
TEMPLATE
inc stop -q org-base || fail "incus stop org-base"
echo "    org-base: nesting, a card with port isolation and a pinned address; g++, python3, Bazelisk $bazelisk; stopped"

step "3. \`org up\`, as $(id -un) (uid $(id -u)), from the copy of the checkout"
mkdir -p "$HOME/.local/bin"
printf '#!/bin/sh\necho "0.0.0 (a stand-in: this test installs no Claude Code)"\n' > "$HOME/.local/bin/claude"
chmod 755 "$HOME/.local/bin/claude"
"$HOME/checkout/bin/org" up > "$HOME/up.out" 2>&1
rc=$?
grep -E '^org: (release|template|build cache|warning: no user service)' "$HOME/up.out" | sed 's/^/    /'
grep -E 'build cache' "$HOME/up.out" | grep -v '^org:' | sed 's/^/    doctor: /'
[ "$rc" = 0 ] || { cat "$HOME/up.out"; fail "\`org up\` exited $rc"; }
grep -q '^org: build cache: org-cache ready' "$HOME/up.out" || { cat "$HOME/up.out"; fail "\`org up\` did not say the build cache is ready"; }
"$HOME/checkout/bin/org" up > "$HOME/up2.out" 2>&1 || { cat "$HOME/up2.out"; fail "a second \`org up\` failed"; }
grep -q '^org: build cache: org-cache is here' "$HOME/up2.out" || { cat "$HOME/up2.out"; fail "a second \`org up\` did not leave the cache as it is"; }
echo "    a second \`org up\`: $(grep '^org: build cache' "$HOME/up2.out" | sed 's/^org: //')"

exec python3 "$HOME/live.py"
LIVE

cat > "$home/live.py" <<'PY'
"""Steps 4 to 7, with the code `org up` installed and the ledger it made, as its user."""
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".local/share/org/current"))
from org import cache  # noqa: E402
from org.backends import BackendError  # noqa: E402
from org.backends.incus import BACKEND, problems  # noqa: E402
from org.ledger import Ledger  # noqa: E402
from org.transport import Incus  # noqa: E402

ORG = str(Path.home() / ".local/bin/org")


def step(text):
    print("- " + text, flush=True)


def say(text):
    print("    " + text, flush=True)


def check(ok, what):
    if not ok:
        print("FAIL: " + what, flush=True)
        sys.exit(1)


def inside(name, script):
    """Run a command in a container, as this test and not as org: org never enters the cache."""
    p = Incus.incus(["exec", name, "-T", "--", "sh", "-c", script], 120)
    return p.stdout.decode("utf-8", "replace").strip()


def results(name):
    return int(inside(name, "find /var/lib/bazel-remote/ac.v2 -type f 2>/dev/null | wc -l") or 0)


def build(tr, who):
    p = tr.run("cd /work/ws && bazel build //:hello 2>&1 && ./bazel-bin/hello", timeout=1500, check=False)
    out = p.stdout.decode("utf-8", "replace")
    for line in out.splitlines():
        if re.search(r"INFO: \d+ process|INFO: Elapsed|Build completed successfully|^hello, ", line):
            say("%s: %s" % (who, line.strip()))
    check(p.returncode == 0 and "Build completed successfully" in out and "hello, engineers" in out,
          "the build in %s failed:\n%s" % (who, "\n".join(out.splitlines()[-25:])))
    return next(line for line in out.splitlines() if re.search(r"INFO: \d+ process", line))


cfg = Ledger().config()
name, port = cache.settings(cfg)["name"], cache.settings(cfg)["port"]

step("4. the cache container, as Incus describes it")
info = Incus.info(name)
check(info is not None and info.get("status") == "Running", "%s is not running" % name)
conf, devices = info.get("expanded_config") or {}, info.get("expanded_devices") or {}
card = devices.get("eth0") or {}
addr = cache.address(info)
say("config: %s" % ", ".join("%s=%s" % (k, v) for k, v in sorted(conf.items()) if k.startswith(("security.", "boot."))))
say("devices: %s" % "; ".join("%s (%s)" % (k, ", ".join("%s=%s" % kv for kv in sorted(v.items()))) for k, v in sorted(devices.items())))
check(str(conf.get("security.privileged", "false")).lower() != "true", "the cache is privileged")
check(str(conf.get("security.nesting", "false")).lower() != "true", "the cache has nesting")
check(conf.get("boot.autostart") == "true", "the cache does not start with the machine")
check(sorted(devices) == ["eth0", "root"] and devices["root"].get("path") == "/" and not devices["root"].get("source"),
      "the cache has a device beyond its disk and its card")
check(card.get("security.ipv4_filtering") == "true", "the cache's card can take another address")
check("security.port_isolation" not in card, "the cache's card is isolated, so no engineer could reach it")
check(cache.faults(info) == [], "org's own check of the cache: %s" % cache.faults(info))
check(problems(info) != [], "problems() would take the cache for an engineer's box")
check(addr, "Incus gave the cache no IPv4 address")
say("address, from `incus list`: %s; as an engineer's box it would be refused: %s" % (addr, "; ".join(problems(info))))
server = inside(name, "systemctl is-active bazel-remote; systemctl is-enabled bazel-remote; ps -o user:20=,args= -C bazel-remote")
say("inside it (the test looks; org never does): %s" % " | ".join(server.splitlines()))
check(server.splitlines()[:2] == ["active", "enabled"] and server.splitlines()[2].startswith("bazel-remote "),
      "the server is not an enabled, running service of the user bazel-remote")
check(str(port) in server and ("--grpc_address 0.0.0.0:%d" % port) in server, "the server does not listen on %d" % port)
held = inside(name, "ls -d /run/org /root/.claude /root/.config/gh /root/.ssh/id_* /root/.org-secrets 2>/dev/null")
check(held == "", "the cache container holds a login: %s" % held)
say("no login in it: no /run/org, ~/.claude, ~/.config/gh, ssh key")
for command in ("doctor", "pool"):
    out = subprocess.run([ORG, command], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120).stdout
    rows = [line.strip() for line in out.splitlines() if "build cache" in line]
    check(len(rows) == 1 and ("running at %s:%d" % (addr, port)) in rows[0] and not rows[0].startswith("FAIL"),
          "`org %s` does not show the cache running at its address:\n%s" % (command, out))
    say("org %s: %s" % (command, rows[0]))
check(results(name) == 0, "the cache already holds results before anything was built")

step("5. two runs' containers, made and judged as dispatch does, then offered the cache")
engineers = []
for who in ("a", "b"):
    machine = BACKEND.choose(cfg, "incus", "live-cache-" + who)
    tr = BACKEND.provision(machine, "live-cache-" + who, cfg)   # a copy of the template, started
    BACKEND.vet(machine, {}, cfg)                               # raises if it is not a box
    described = Incus.info(machine["name"])
    check(problems(described) == [], "problems() refuses %s: %s" % (machine["name"], problems(described)))
    theirs = described["expanded_devices"]["eth0"]
    check(theirs.get("security.port_isolation") == "true" and theirs.get("security.ipv4_filtering") == "true",
          "%s does not have the engineers' card settings" % machine["name"])
    log = []
    kept = BACKEND.build_cache(tr, cfg, log.append)
    say("%s (problems(): none; %s): %s" % (machine["name"], cache.address(described), " / ".join(log)))
    check(log == ["build cache reachable at %s:%d" % (addr, port)], "dispatch's line was %r" % log)
    check(kept == {"state": "reachable", "address": addr, "port": port}, "the run's record would keep %r" % kept)
    wrote = tr.run("cat %s" % cache.BAZELRC).stdout.decode()
    check(wrote == cache.bazelrc(addr, port), "%s in %s is not what dispatch writes:\n%s" % (cache.BAZELRC, machine["name"], wrote))
    engineers.append((machine, tr, cache.address(described)))
for line in wrote.splitlines():
    say("| " + line)

step("6. a Bazel build in the first uploads; the same build in the second is all cache hits")
(first, tr_a, addr_a), (second, tr_b, addr_b) = engineers
line = build(tr_a, first["name"])
check("remote cache hit" not in line, "the first build already found results in the cache: %s" % line)
uploaded = results(name)
check(uploaded > 0, "the cache holds no action results after the first build")
say("the cache now holds %d action results" % uploaded)
line = build(tr_b, second["name"])
check("remote cache hit" in line, "no remote cache hits in the second container: %s" % line)
check(not re.search(r"sandbox|local|worker", line), "the second container ran an action instead of downloading it: %s" % line)
say("%s ran no compile or link action" % second["name"])

step("7. the two engineers' containers still cannot open a connection to each other")
for machine, tr, own in engineers:
    tr.run("setsid python3 -m http.server %d >/dev/null 2>&1 </dev/null & sleep 2" % port, timeout=60)
    check(cache.reaches(tr, own, port), "the listener in %s does not answer even there" % machine["name"])
    say("%s listens on %s:%d (it answers from inside itself)" % (machine["name"], own, port))
for (machine, tr, _), (other, _, there) in ((engineers[0], engineers[1]), (engineers[1], engineers[0])):
    p = tr.run(cache._PROBE % (there, port), timeout=30, check=False)
    why = (p.stderr.decode("utf-8", "replace").strip().splitlines() or ["connected"])[-1]
    check(p.returncode != 0, "%s opened a connection to %s at %s:%d" % (machine["name"], other["name"], there, port))
    say("%s -> %s (%s:%d): refused, %s" % (machine["name"], other["name"], there, port, why))
    check(cache.reaches(tr, addr, port), "%s no longer reaches the cache" % machine["name"])
    say("%s -> %s (%s:%d): reachable" % (machine["name"], name, addr, port))

step("8. removing the runs' containers leaves the cache, and the backend will not remove the cache")
for machine, _, _ in engineers:
    say(BACKEND.remove(machine, cfg))
    check(Incus.info(machine["name"]) is None, "%s is still there" % machine["name"])
try:
    BACKEND.remove(dict(first, name=name), cfg)
    check(False, "the backend removed the cache")
except BackendError as e:
    say("asked to remove %s, the backend says: %s" % (name, e))
after = Incus.info(name)
check(after is not None and after.get("status") == "Running" and results(name) == uploaded, "the cache did not survive the removals untouched")
say("%s is still running, with its %d action results" % (name, uploaded))
PY
chown -R "$user:$user" "$home"

su - "$user" -c 'bash "$HOME/live.sh"' </dev/null || exit 1

echo "- 9. removing what this made: the user, its Incus project (containers and image) and its bridge"
cleanup
left=$(inc project list -f csv | cut -d, -f1 | grep -c "^user-$uid\b"; inc network list -f csv | cut -d, -f1 | grep -c "^incusbr-$uid\$"; getent passwd "$user" | grep -c .)
[ "$(echo $left)" = "0 0 0" ] || fail "something of this test is left behind (project, bridge, user: $(echo $left))"
uid=""
echo "    nothing is left"
echo "PASS: \`org up\`, run by an unprivileged user, made the cache container; two engineers' containers passed problems(),"
echo "PASS: reached the cache and were given its address; the second built from the first's results; neither reaches the other."
