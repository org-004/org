#!/bin/bash
# live_memory_incus.sh: the memory limit, proved with real containers in an Incus of one's own.
#
# An engineer cannot use the VM's Incus, but its container has security.nesting, so it can run
# one inside itself. This script needs that Incus and root in the container that holds it; the
# header of tests/live_cache_incus.sh gives the five commands that install it.
#
# What it does, and it exits 0 only if all of it held:
#
#   1. makes a stand-in for the template with the command `org up` uses for its network card;
#   2. does what dispatch does for two runs, with machines.incus.memory set to 128MiB: the
#      backend copies the template (`Incus.create`, the limit on the copy command itself) and
#      `vet` reads Incus's description of the copy and finds the limit in it;
#   3. reads the limit back from inside the first container: its own /sys/fs/cgroup/memory.max;
#   4. has a process in the first container take more than the limit: the kernel kills it, and
#      the container's own memory.events counts the kill;
#   5. shows what was not touched: the second container (its count, and a process started in it
#      before the kill), and this session (every kill the outer container counted is one the
#      first container counted, and the shell running this is still here to say so);
#   6. shows which process the kernel chooses. A stand-in for the engineer's session holds more
#      memory than the command it runs. Without the wrapper the runner gives Claude Code
#      (CLAUDE_CODE_SHELL_PREFIX, org/runner.py) the stand-in is killed; with it the command is
#      killed and the stand-in lives. Skipped, and said so, where the runner has no wrapper;
#   7. makes a user in the `incus` group, to whom Incus gives a restricted project as it gives
#      the `org` user one on the VM, and as that user sets limits.memory in the three ways org
#      does or the owner might (on a copy, on an existing container, on a new one), then asks
#      for a privileged container, which a restricted project must refuse;
#   8. removes the two runs' containers through the backend.
#
# It then deletes the template, the user, its project (containers, image) and its bridge, and
# any image it pulled, however it ends. Takes about a minute; needs the internet (one image).
set -u

root=$(cd "$(dirname "$0")/.." && pwd -P)
tag="orgmem$$"
base="$tag-base"
prefix="$tag-eng-"
user="$tag"
uid=""
limit=128MiB
limit_bytes=$((128 * 1024 * 1024))
tmp=$(mktemp -d)
images_before=""

inc() { incus "$@" </dev/null; }   # incus reads stdin when it is not a terminal; never ours
step() { printf -- '- %s\n' "$*"; }
say() { printf '    %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*"; exit 1; }
kills() { incus exec "$1" -T -- awk '$1 == "oom_kill" { print $2 }' /sys/fs/cgroup/memory.events </dev/null; }
own_kills() { awk '$1 == "oom_kill" { print $2 }' /sys/fs/cgroup/memory.events 2>/dev/null; }

cleanup() {
  local each project
  for each in $(inc list -f csv -c n 2>/dev/null | grep "^$tag-"); do inc delete -f "$each" >/dev/null 2>&1; done
  if [ -n "$uid" ]; then
    project="user-$uid"
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
  fi
  # An image this run pulled is this run's to remove; one that was here before is not.
  for each in $(inc image list -f csv -c f 2>/dev/null); do
    case " $images_before " in *" $each "*) ;; *) inc image delete "$each" >/dev/null 2>&1 ;; esac
  done
  rm -rf "$tmp"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

[ "$(id -u)" = 0 ] || fail "run as root in the container that holds the Incus: it makes and removes a user"
command -v incus >/dev/null || fail "incus is not installed here (the header of tests/live_cache_incus.sh says how)"
inc list -f csv >/dev/null 2>&1 || fail "incus does not answer; has \`incus admin init --auto\` been run?"
getent group incus >/dev/null || fail "there is no \`incus\` group for an unprivileged user to be in"
images_before=$(inc image list -f csv -c f 2>/dev/null | tr '\n' ' ')

step "1. a stand-in for the template, with the card \`org up\` gives the template"
inc init -q images:debian/13 "$base" || fail "incus init $base"
inc config device override "$base" eth0 security.port_isolation=true security.ipv4_filtering=true >/dev/null || fail "the card's settings"
say "$base: Debian 13, stopped, no limits.memory of its own: [$(inc config get "$base" limits.memory)]"

step "2. what dispatch does for two runs, with machines.incus.memory $limit"
cat > "$tmp/runs.py" <<'PY'
"""Provision and vet two runs' containers as dispatch does, printing each `incus` call that changes something."""
import json
import sys

from org.backends.incus import BACKEND
from org.transport import Incus

base, prefix, limit, out = sys.argv[1:5]
cfg = {"machines": {"incus": {"base": base, "prefix": prefix, "memory": limit, "slots": 2}}}
real = Incus.incus


def shown(args, *more, **kw):
    if args[0] in ("copy", "start", "config", "delete"):
        print("    $ incus " + " ".join(args), flush=True)
    return real(args, *more, **kw)


Incus.incus = staticmethod(shown)
machines = []
for run_id in ("001-a-large-build-r1", "002-a-small-one-r1"):
    machine, facts = BACKEND.choose(cfg, "incus", run_id), {}
    BACKEND.provision(machine, run_id, cfg)
    BACKEND.vet(machine, facts, cfg)
    found = (Incus.info(machine["name"]).get("expanded_config") or {}).get("limits.memory")
    print("    %s: the run's record says %s, Incus's description says limits.memory=%s, the engineer is told %s"
          % (machine["name"], json.dumps(machine.get("limits")), found, facts.get("memory_limit")), flush=True)
    if (machine.get("limits") or {}).get("memory") != limit or found != limit or facts.get("memory_limit") != limit:
        sys.exit("FAIL: %s does not carry the limit %s everywhere it should" % (machine["name"], limit))
    machines.append(machine)
with open(out, "w") as f:
    json.dump({"cfg": cfg, "machines": machines}, f)
PY
(cd "$tmp" && PYTHONPATH="$root" python3 "$tmp/runs.py" "$base" "$prefix" "$limit" "$tmp/runs.json") || fail "the backend did not make the two containers"
a=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["machines"][0]["name"])' "$tmp/runs.json")
b=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["machines"][1]["name"])' "$tmp/runs.json")

step "3. the limit, read inside $a"
max=$(incus exec "$a" -T -- cat /sys/fs/cgroup/memory.max </dev/null)
say "/sys/fs/cgroup/memory.max: $max"
[ "$max" = "$limit_bytes" ] || fail "memory.max in $a is $max, not $limit_bytes ($limit)"

step "4. a process in $a takes more than $limit"
incus exec "$b" -T -- sh -c 'nohup sleep 600 >/dev/null 2>&1 & echo $! > /run/bystander' </dev/null || fail "could not start a process in $b"
a_before=$(kills "$a"); b_before=$(kills "$b"); own_before=$(own_kills)
say "kills counted before: $a $a_before, $b $b_before, the container this runs in ${own_before:-not counted}"
hog='head -c 400M /dev/zero | tail > /dev/null'   # tail keeps a line it never sees the end of: 400 MB of it
incus exec "$a" -T -- bash -c "$hog" </dev/null > "$tmp/hog.out" 2>&1
rc=$?
sed 's/^/    | /' "$tmp/hog.out"
say "\`$hog\` ended with status $rc"
[ "$rc" = 137 ] || fail "the process that took more than the limit was not killed (status $rc)"
a_after=$(kills "$a")
say "/sys/fs/cgroup/memory.events in $a: oom_kill $a_after"
[ "$a_after" -gt "$a_before" ] || fail "the kill was not counted in $a"

step "5. what was not touched"
b_after=$(kills "$b"); own_after=$(own_kills)
incus exec "$b" -T -- sh -c 'kill -0 "$(cat /run/bystander)"' </dev/null || fail "the process started in $b before the kill is gone"
[ "$b_after" = "$b_before" ] || fail "$b counted a kill ($b_before to $b_after)"
say "$b: oom_kill still $b_after, and the process started in it before the kill is still running"
if [ -n "$own_before" ]; then
  # The outer container's count includes its children's, so it rises with the first container's
  # and by exactly as much when nothing outside that container was killed.
  [ $((own_after - own_before)) = $((a_after - a_before)) ] \
    || fail "the container this runs in counted $((own_after - own_before)) kill(s) and $a only $((a_after - a_before))"
  say "the container this runs in: oom_kill $own_before to $own_after, which is $a's $((a_after - a_before)) and no other"
fi
say "this shell (pid $$) and its parent (pid $PPID, $(ps -o comm= -p $PPID)) are still running"

step "6. which process the kernel chooses: the session, or the command it ran"
if (cd "$tmp" && PYTHONPATH="$root" python3 -c 'import sys; from org import runner; sys.stdout.write(runner.SHELL_PREFIX)' > "$tmp/wrapper" 2>/dev/null) && [ -s "$tmp/wrapper" ]; then
  cat > "$tmp/session.pl" <<'PERL'
# A stand-in for a Claude Code session. It holds memory of its own, more than its command will
# have taken when the container reaches its limit, runs one command as Claude Code 2.1 runs a
# shell tool's (the prefix, then the whole command line as one quoted argument), and says what
# became of the command and of itself.
$| = 1;
my $held = "";
$held .= "x" x (1 << 20) for 1 .. 70;
my $command = 'head -c 400M /dev/zero | tail > /dev/null';
my $prefix = $ENV{CLAUDE_CODE_SHELL_PREFIX} // "";
(my $quoted = $command) =~ s/'/'\\''/g;
my $rc = system("/bin/bash", "-c", length $prefix ? "$prefix '$quoted'" : $command);
printf "stand-in: its command ended with %s\n", ($rc & 127) ? "signal " . ($rc & 127) : "status " . ($rc >> 8);
open(my $f, "<", "/proc/$$/oom_score_adj");
my $adj = <$f>;
chomp $adj;
printf "stand-in: alive after it, holding %d MiB, its own oom_score_adj %s\n", length($held) >> 20, $adj;
PERL
  inc file push -q "$tmp/session.pl" "$a/root/session.pl" || fail "could not copy the stand-in into $a"
  inc file push -q --mode 0755 "$tmp/wrapper" "$a/root/wrapper" || fail "could not copy the wrapper into $a"
  say "the wrapper, as org/runner.py writes it:"
  sed 's/^/    | /' "$tmp/wrapper"
  adj=$(incus exec "$a" -T -- /root/wrapper 'cat /proc/self/oom_score_adj' </dev/null)
  say "a command run through it has oom_score_adj $adj; one run without it has $(incus exec "$a" -T -- cat /proc/self/oom_score_adj </dev/null)"
  [ "${adj:-0}" -gt 0 ] || fail "the wrapper did not raise the adjustment of what it runs"
  [ "$(incus exec "$a" -T -- /root/wrapper 'echo "$0 $# $1"; exit 7' </dev/null; echo " $?")" = "bash 0  7" ] \
    || fail "the wrapper does not hand the command line to bash as it was given, or loses its exit status"

  before=$(kills "$a")
  incus exec "$a" -T -- perl /root/session.pl </dev/null > "$tmp/control.out" 2>&1
  rc=$?
  say "without the wrapper (the stand-in holds 70 MiB of $limit; its command is the one from step 4):"
  sed 's/^/    | /' "$tmp/control.out"
  say "the stand-in ended with status $rc; kills counted: $before to $(kills "$a")"
  { [ "$rc" = 137 ] && ! grep -q "alive after it" "$tmp/control.out"; } \
    || fail "without the wrapper the stand-in was not the one killed, so this run cannot show that the wrapper matters"

  before=$(kills "$a")
  incus exec "$a" -T --env CLAUDE_CODE_SHELL_PREFIX=/root/wrapper -- perl /root/session.pl </dev/null > "$tmp/wrapped.out" 2>&1
  rc=$?
  say "with CLAUDE_CODE_SHELL_PREFIX=/root/wrapper:"
  sed 's/^/    | /' "$tmp/wrapped.out"
  say "the stand-in ended with status $rc; kills counted: $before to $(kills "$a")"
  [ "$rc" = 0 ] || fail "with the wrapper the stand-in did not survive (status $rc)"
  grep -q "its command ended with status 137" "$tmp/wrapped.out" || fail "with the wrapper the command was not the one killed"
  grep -q "alive after it, holding 70 MiB, its own oom_score_adj 0" "$tmp/wrapped.out" || fail "the stand-in's own adjustment changed"
  r12="the session's stand-in outlived a command that the kernel killed for memory"
else
  say "skipped: org/runner.py in this checkout has no SHELL_PREFIX, so there is no wrapper to try"
  r12="the wrapper was not tried (this checkout has none)"
fi

step "7. a restricted project, as Incus gives a user in the \`incus\` group: is limits.memory allowed there?"
useradd -m -s /bin/bash -G incus "$user" || fail "useradd"
uid=$(id -u "$user")
home=$(getent passwd "$user" | cut -d: -f6)
cat > "$home/restricted.sh" <<'RESTRICTED'
set -u
inc() { incus "$@" </dev/null; }
say() { printf '    %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*"; exit 1; }
try() { printf '    $ incus %s\n' "$*"; inc "$@" > "$HOME/out" 2>&1; rc=$?; sed 's/^/    | /' "$HOME/out"; say "exit status $rc"; return $rc; }
inc init -q images:debian/13 base > "$HOME/out" 2>&1 || { cat "$HOME/out"; fail "the user could not make a container at all"; }
say "as $(id -un) (uid $(id -u)), in the project Incus made for it:"
try copy -q base copied -c limits.memory=128MiB || fail "limits.memory was refused on a copy: the words above are Incus's"
try config set base limits.memory 96MiB || fail "limits.memory was refused on an existing container"
try init -q images:debian/13 fresh -c limits.memory=64MiB || fail "limits.memory was refused on a new container"
for each in copied base fresh; do
  say "$each: limits.memory=$(inc list --format json "^$each\$" | python3 -c 'import json,sys; print((json.load(sys.stdin)[0].get("expanded_config") or {}).get("limits.memory"))')"
done
inc start -q copied > "$HOME/out" 2>&1 || { cat "$HOME/out"; fail "the copy with a limit did not start"; }
max=$(incus exec copied -T -- cat /sys/fs/cgroup/memory.max </dev/null)
say "copied, started: its own /sys/fs/cgroup/memory.max is $max"
[ "$max" = $((128 * 1024 * 1024)) ] || fail "the limit is not in force inside the restricted project's container"
# The same project must refuse what a restricted project is for refusing, or it proves nothing.
if try config set fresh security.privileged true; then fail "the project took security.privileged: it is not restricted"; fi
RESTRICTED
chown "$user:$user" "$home/restricted.sh"
su - "$user" -c 'bash "$HOME/restricted.sh"' </dev/null || exit 1
say "the project, as root sees it: user-$uid, $(inc project show "user-$uid" | grep -E '^ +restricted' | tr -s ' ' | tr '\n' ';' | sed 's/^ //; s/;$//; s/; /, /g')"
inc project show "user-$uid" | grep -q '^ *restricted: "true"' || fail "the user's project is not restricted"

step "8. removing the runs' containers through the backend"
cat > "$tmp/remove.py" <<'PY'
import json
import sys

from org.backends.incus import BACKEND
from org.transport import Incus

runs = json.load(open(sys.argv[1]))
for machine in runs["machines"]:
    print("    " + BACKEND.remove(machine, runs["cfg"]))
    if Incus.info(machine["name"]) is not None:
        sys.exit("FAIL: %s is still there" % machine["name"])
PY
(cd "$tmp" && PYTHONPATH="$root" python3 "$tmp/remove.py" "$tmp/runs.json") || fail "the backend did not remove the containers"

step "9. removing the rest: the template, the user, its project and bridge, the image"
cleanup
left=$(inc list -f csv -c n | grep -c "^$tag-"; inc project list -f csv | cut -d, -f1 | grep -c "^user-$uid\b"; inc network list -f csv | cut -d, -f1 | grep -c "^incusbr-$uid\$"; getent passwd "$user" | grep -c .)
[ "$(echo $left)" = "0 0 0 0" ] || fail "something of this test is left behind (containers, project, bridge, user: $(echo $left))"
uid=""
say "nothing is left"
echo "PASS: a container made by the backend had its limit in memory.max; a process past the limit was killed there and counted there;"
echo "PASS: the container beside it and this session were not touched; $r12;"
echo "PASS: a restricted project took limits.memory on a copy, on an existing container and on a new one."
