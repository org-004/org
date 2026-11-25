#!/bin/bash
# live_teardown_incus.sh: a finished run's container, measured from outside and deleted by a pump,
# proved with real containers in an Incus of one's own.
#
# An engineer cannot use the VM's Incus, but its container has security.nesting, so it can run
# one inside itself. This script needs that Incus and root in the container that holds it; the
# header of tests/live_cache_incus.sh gives the five commands that install it. Never run it on the
# VM: it makes and deletes containers, a user and that user's project, and the VM's are the owner's.
#
# What it does, and it exits 0 only if all of it held:
#
#   1. makes a stand-in for the template with the command `org up` uses for its network card;
#   2. in Incus's default project, as root, does what dispatch does for two runs with
#      machines.incus.memory set to 128MiB (the backend's `provision` and `vet`), and writes their
#      records by hand in a ledger of its own, as executing;
#   3. has a process in the first container hold 64 MiB, then reads the container's own
#      memory.peak and memory.events from inside it, and right after has org measure it from
#      outside (`telemetry.measure_due`): the two must agree, and the limit, CPU time, processes
#      and status must be there;
#   4. has a process in it take more than the limit, which the kernel kills, and measures again:
#      the peak and the count of kills must agree with the container's own files;
#   5. makes both runs final, with machines.after_finish set to keep: a pump deletes nothing;
#   6. sets machines.after_finish to delete: a pump takes a last measurement and deletes both
#      containers. Incus no longer lists them and the kernel's directories are gone, while the
#      record keeps machine_removed_at and the summary, machine.jsonl ends with the last line, and
#      `org review` still prints the block. A second pump deletes nothing more, and the template
#      is still there;
#   7. makes a user in the `incus` group, to whom Incus gives a restricted project as it gives the
#      `org` user one on the VM, copies the `org` package to its home, and as that user does steps
#      2, 3, 4 and 6 for one run in that project (its kernel's directory has the project's name in it).
#
# It then deletes the containers, the template, the user, its project (containers, image) and its
# bridge, and any image it pulled, however it ends. Takes about twenty seconds when Incus already
# holds Debian 13, and needs the internet to pull it when it does not.
set -u

# Never on the VM. It is refused where the installation shows: an `org` user on this machine, or
# engineers' containers (the default prefix) in any project of this Incus.
if getent passwd org >/dev/null 2>&1 || incus list --all-projects -f csv -c n </dev/null 2>/dev/null | grep -q '^org-eng-'; then
  printf 'FAIL: this machine has an `org` user or containers named org-eng-*: it looks like the installation, where this must never run\n'
  exit 1
fi

root=$(cd "$(dirname "$0")/.." && pwd -P)
tag="orgtd$$"
base="$tag-base"
prefix="$tag-eng-"
user="$tag"
uid=""
limit=128MiB
tmp=$(mktemp -d)
chmod 755 "$tmp"
images_before=""

inc() { incus "$@" </dev/null; }   # incus reads stdin when it is not a terminal; never ours
step() { printf -- '- %s\n' "$*"; }
say() { printf '    %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*"; exit 1; }

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

# What dispatch, the pump and `org review` do, with the code of this checkout, in one project. Run
# by root for the default project and by the user for its restricted one; it prints the steps and
# writes one line to its last argument for each thing it proved.
cat > "$tmp/live.py" <<'PY'
"""Provision, measure, finish and tear down runs as org does, and compare with the containers' own files."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

checkout, home, base, prefix, limit, where, keep, proved = sys.argv[1:9]
sys.path.insert(0, checkout)
from org import dispatch, telemetry  # noqa: E402
from org.backends.incus import BACKEND  # noqa: E402
from org.ledger import Ledger  # noqa: E402
from org.transport import Incus  # noqa: E402
from org.util import read_json, write_json  # noqa: E402

MIB = 1024 * 1024
HELD = 64 * MIB  # what dd holds: one buffer of 64 MiB
MARGIN = 8 * MIB  # what a peak may grow between the read inside and the one outside right after
LIMIT = int(limit.replace("MiB", "")) * MIB
deletes = []
real = Incus.incus


def shown(args, *more, **kw):
    if args[0] in ("copy", "delete"):
        print("    $ incus " + " ".join(args), flush=True)
        if args[0] == "delete":
            deletes.append(args[-1])
    return real(args, *more, **kw)


Incus.incus = staticmethod(shown)


def step(text):
    print("- %s: %s" % (where, text), flush=True)


def say(text):
    print("    " + text, flush=True)


def check(ok, what):
    if not ok:
        print("FAIL: %s: %s" % (where, what), flush=True)
        sys.exit(1)


def passed(text):
    with open(proved, "a") as f:
        f.write(text + "\n")


def inside(name, script):
    """Run a command in a container, as this test and not as org, and return what it printed and its status."""
    p = subprocess.run(["incus", "exec", name, "-T", "--", "sh", "-c", script], stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    return p.stdout.decode("utf-8", "replace").strip(), p.returncode


def own_files(name):
    """The container's own memory.peak, oom_kill, memory.max and CPU time, read inside it."""
    out, rc = inside(name, "cat /sys/fs/cgroup/memory.peak; awk '$1 == \"oom_kill\" { print $2 }' /sys/fs/cgroup/memory.events;"
                           " cat /sys/fs/cgroup/memory.max; awk '$1 == \"usage_usec\" { print $2 }' /sys/fs/cgroup/cpu.stat")
    words = out.split()
    check(rc == 0 and len(words) == 4 and all(w.isdigit() for w in words), "could not read %s's own files: %r" % (name, out))
    return {"memory_peak_bytes": int(words[0]), "oom_kill": int(words[1]), "memory_limit_bytes": int(words[2]),
            "cpu_s": int(words[3]) / 1e6}


def agree(name, own, line, at_least):
    """Every value of the line beside where it came from, and the line held to the container's own files."""
    say("inside %s:  memory.peak %d (%d MiB), memory.events oom_kill %d, memory.max %d, cpu.stat usage_usec %d"
        % (name, own["memory_peak_bytes"], own["memory_peak_bytes"] // MIB, own["oom_kill"], own["memory_limit_bytes"],
           own["cpu_s"] * 1e6))
    say("org, outside: memory_peak_bytes %s, oom_kill %s, memory_limit_bytes %s, cpu_s %s, processes %s, running %s, "
        "memory_bytes %s, disk_bytes %s, started_at %s, note %s"
        % tuple(json.dumps(line.get(k)) for k in ("memory_peak_bytes", "oom_kill", "memory_limit_bytes", "cpu_s", "processes",
                                                 "running", "memory_bytes", "disk_bytes", "started_at", "note")))
    peak = line.get("memory_peak_bytes")
    check(isinstance(peak, int) and own["memory_peak_bytes"] <= peak <= own["memory_peak_bytes"] + MARGIN,
          "org's peak %r is not the container's own %d (or at most %d MiB above it, read right after)"
          % (peak, own["memory_peak_bytes"], MARGIN // MIB))
    check(peak >= at_least, "the peak %d is below the %d bytes a process was made to hold" % (peak, at_least))
    check(line.get("oom_kill") == own["oom_kill"], "org counted %r kills, the container's own memory.events %d"
          % (line.get("oom_kill"), own["oom_kill"]))
    check(line.get("memory_limit_bytes") == LIMIT == own["memory_limit_bytes"],
          "the limit: org %r, inside %d, set %d" % (line.get("memory_limit_bytes"), own["memory_limit_bytes"], LIMIT))
    check(isinstance(line.get("cpu_s"), (int, float)) and line["cpu_s"] + 0.0005 >= own["cpu_s"] > 0,
          "org's cpu_s %r is not above zero and at least the %s s read inside before it (org keeps milliseconds)" % (line.get("cpu_s"), own["cpu_s"]))
    check(isinstance(line.get("processes"), int) and line["processes"] > 0, "org counted no processes")
    check(line.get("running") is True, "org did not see the container running")
    check(line.get("note") is None, "org's line says something could not be read: %s" % line.get("note"))
    say("they agree: the peak to within %d MiB (read right after), the kills, the limit; CPU time, processes and status read"
        % (MARGIN // MIB))


def lines(run_id):
    return telemetry.load(ledger, run_id)


ledger = Ledger(home)
ledger.init()
config = read_json(ledger.config_path)
config.setdefault("machines", {})["incus"] = {"base": base, "prefix": prefix, "memory": limit, "slots": 4}
write_json(ledger.config_path, config)


def after_finish(value):
    config = read_json(ledger.config_path)
    config["machines"]["after_finish"] = value
    write_json(ledger.config_path, config)
    say("machines.after_finish is now %s in %s" % (value, ledger.config_path))


run_ids = ["001-a-measured-run-r1"] + (["002-a-kept-run-r1"] if keep == "keep" else [])
step("what dispatch does for %d run(s), with machines.incus.memory %s; the records written by hand, as executing"
     % (len(run_ids), limit))
cfg = ledger.config()
machines = {}
for run_id in run_ids:
    machine, facts = BACKEND.choose(cfg, "incus", run_id), {}
    BACKEND.provision(machine, run_id, cfg)
    BACKEND.vet(machine, facts, cfg)
    machines[run_id] = machine
    ledger.run_dir(run_id).mkdir(parents=True, exist_ok=True)
    ledger.save_run(run_id, {"run_id": run_id, "spec_id": run_id.rsplit("-r", 1)[0], "state": "executing", "final": False,
                             "remote_dir": dispatch.remote_dir(run_id), "created_at": time.time(),
                             "dispatched_at": time.time(), "events_offset": 0, "machine": machine})
    say("%s: the record's machine is %s" % (run_id, json.dumps(machine, sort_keys=True)))
run_id = run_ids[0]
name = machines[run_id]["name"]
entry = Incus.info(name)
project = entry.get("project")
cgroup = Path("/sys/fs/cgroup") / (("lxc.payload.%s" % name) if project in (None, "", "default") else
                                   ("lxc.payload.%s_%s" % (project, name)))
say("Incus puts %s in the project %s; the kernel's directory for it is %s" % (name, project, cgroup))
check(cgroup.is_dir(), "%s is not there" % cgroup)

step("a process in %s holds %d MiB; read inside, then measured from outside by `telemetry.measure_due`" % (name, HELD // MIB))
out, rc = inside(name, "dd if=/dev/zero of=/dev/null bs=64M count=1 2>&1")
say("$ dd if=/dev/zero of=/dev/null bs=64M count=1: %s (status %d)" % (" / ".join(out.splitlines()), rc))
check(rc == 0, "dd did not run in %s" % name)
own = own_files(name)
measured = telemetry.measure_due(ledger, say)
check(sorted(measured) == sorted(run_ids), "measure_due measured %r, not %r" % (measured, run_ids))
first = lines(run_id)[-1]
agree(name, own, first, HELD)
check(first["oom_kill"] == 0, "the container counted a kill before anything took more than the limit")

step("a process in %s takes more than %s; measured again, as the next minute's pump would" % (name, limit))
hog = "head -c 400M /dev/zero | tail > /dev/null"
out, rc = inside(name, hog)
say("$ %s: %s(status %d)" % (hog, (out.replace("\n", " / ") + " ") if out else "", rc))
check(rc == 137, "the process that took more than the limit was not killed (status %d)" % rc)
own = own_files(name)
check(own["oom_kill"] >= 1, "the container's own memory.events counted no kill")
with ledger.run_lock(run_id) as held:  # what `measure_due` does for a run that is due, without waiting the minute
    check(held, "the run's lock was held elsewhere")
    rec = ledger.run(run_id)
    second = telemetry.take(ledger, run_id, rec)
    ledger.save_run(run_id, rec)
check(second is not None, "no measurement was taken")
agree(name, own, second, HELD)
check(ledger.run(run_id).get("oom_kill") == own["oom_kill"], "the record's oom_kill is %r" % ledger.run(run_id).get("oom_kill"))
say("the record's telemetry so far: %s" % json.dumps(ledger.run(run_id).get("telemetry"), sort_keys=True))
passed("measured from outside in the %s project%s: %s's peak %d MiB, %d kill(s), the limit %d MiB, %s CPU s, %d processes"
       % (where, "" if project in (None, "", "default") else " (%s, the kernel's directory %s)" % (project, cgroup.name),
          name, second["memory_peak_bytes"] // MIB, second["oom_kill"], LIMIT // MIB, second["cpu_s"], second["processes"]))
passed("in the %s project, org's peak and kills agreed with the container's own memory.peak and memory.events oom_kill, "
       "after a process held 64 MiB and after one was killed for memory" % where)

for k, state in zip(run_ids, ("done", "stopped")):
    rec = ledger.run(k)
    rec.update(state=state, final=True, ended_at=time.time())
    ledger.save_run(k, rec)
if keep == "keep":
    step("both runs made final (done, stopped), as sync would; with machines.after_finish keep, a pump deletes nothing")
    after_finish("keep")
    dispatch.pump(ledger, say)
    check(deletes == [], "the pump deleted %r with machines.after_finish keep" % deletes)
    for k in run_ids:
        check(Incus.info(machines[k]["name"]) is not None, "%s is gone" % machines[k]["name"])
        check(not ledger.run(k).get("machine_removed_at"), "%s's record says its machine was removed" % k)
        say("%s: final, %s; %s is still there, no machine_removed_at" % (k, ledger.run(k)["state"], machines[k]["name"]))
    after_finish("delete")
else:
    step("the run made final (done), as sync would")

step("a pump, with machines.after_finish %s: the last measurement, then the deletion"
     % ("delete" if keep == "keep" else "absent (delete)"))
own = own_files(name)
say("inside %s just before the pump: memory.peak %d (%d MiB), oom_kill %d"
    % (name, own["memory_peak_bytes"], own["memory_peak_bytes"] // MIB, own["oom_kill"]))
check(cgroup.is_dir(), "%s went before the pump" % cgroup)
dispatch.pump(ledger, lambda m: say("pump: " + m))
check(sorted(deletes) == sorted(machines[k]["name"] for k in run_ids), "the pump deleted %r" % deletes)
listed = subprocess.run(["incus", "list", "-f", "csv", "-c", "n"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        timeout=60).stdout.decode().split()
for k in run_ids:
    check(machines[k]["name"] not in listed, "`incus list` still lists %s" % machines[k]["name"])
say("`incus list` now lists: %s" % (", ".join(listed) or "nothing"))
check(not cgroup.exists(), "the kernel's directory %s is still there" % cgroup)
say("the kernel's directory %s is gone" % cgroup)
rec = ledger.run(run_id)
summary = rec.get("telemetry") or {}
last = lines(run_id)[-1]
say("the record: state %s, final %s, machine_removed_at %s, machine_removed_by %s, oom_kill %s"
    % (rec.get("state"), rec.get("final"), rec.get("machine_removed_at"), json.dumps(rec.get("machine_removed_by")),
       rec.get("oom_kill")))
say("its telemetry: %s" % json.dumps(summary, sort_keys=True))
say("the last line of %s: %s" % (telemetry.path(ledger, run_id), json.dumps(last, sort_keys=True)))
check(rec.get("machine_removed_at") and rec.get("machine_removed_by") == "teardown", "the record does not say the machine was removed")
check(last.get("last") is True, "the last line of machine.jsonl has no last: true")
check(own["memory_peak_bytes"] <= last.get("memory_peak_bytes", -1) <= own["memory_peak_bytes"] + MARGIN
      and last.get("oom_kill") == own["oom_kill"], "the last measurement does not agree with the container's own files")
check(summary.get("memory_peak_bytes") == max(line["memory_peak_bytes"] for line in lines(run_id))
      and summary["memory_peak_bytes"] >= own["memory_peak_bytes"], "the summary's peak is not the largest measured")
check(summary.get("oom_kill") == own["oom_kill"] == rec.get("oom_kill"), "the summary's kills are not the container's")
check(summary.get("samples") == 3 and summary.get("memory_limit_bytes") == LIMIT, "the summary has %r samples" % summary.get("samples"))
passed("in the %s project, a pump deleted the final run's container (`incus list` and the kernel's directory: gone) and the "
       "record kept machine_removed_at, machine_removed_by teardown and the summary (peak %d MiB, %d kill(s))"
       % (where, summary["memory_peak_bytes"] // MIB, summary["oom_kill"]))

step("`org review %s` after the deletion" % run_id)
p = subprocess.run([sys.executable, "-m", "org", "--home", home, "review", run_id], cwd=checkout, stdin=subprocess.DEVNULL,
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120, env=dict(os.environ, PYTHONPATH=checkout))
text = p.stdout.decode("utf-8", "replace")
block = text[text.find("machine: "):].splitlines() if "machine: " in text else []
for row in block:
    say("| " + row)
want = ["peak %d MiB of a %d MiB limit" % (summary["memory_peak_bytes"] // MIB, LIMIT // MIB),
        "%d process%s killed for memory" % (summary["oom_kill"], "" if summary["oom_kill"] == 1 else "es"),
        "when the run ended", "measured  3 times"]
check(p.returncode == 0 and block and all(w in text for w in want),
      "`org review` (status %d) does not show %r:\n%s" % (p.returncode, want, text))
passed("in the %s project, `org review` printed the block after the deletion: %s, %s" % (where, want[0], want[1]))

if keep == "keep":
    step("a second pump deletes nothing more; the template is still there")
    before = {k: ledger.run(k) for k in run_ids}
    deletes.clear()
    dispatch.pump(ledger, say)
    check(deletes == [], "the second pump called `incus delete` for %r" % deletes)
    check(all(ledger.run(k) == before[k] for k in run_ids), "the second pump changed a record")
    say("no `incus delete`, both records as they were")
    check(Incus.info(base) is not None, "the template %s is gone" % base)
    say("the template %s (outside the prefix %s) is still there" % (base, prefix))
    passed("machines.after_finish keep kept a final run's container through a pump until the setting was changed; a second "
           "pump made no `incus delete`; the template outlived the pumps")
PY

step "1. a stand-in for the template, with the card \`org up\` gives the template"
inc init -q images:debian/13 "$base" || fail "incus init $base"
inc config device override "$base" eth0 security.port_isolation=true security.ipv4_filtering=true >/dev/null || fail "the card's settings"
say "$base: Debian 13, stopped"

step "2. the default project, as root"
(cd "$tmp" && python3 "$tmp/live.py" "$root" "$tmp/ledger" "$base" "$prefix" "$limit" default keep "$tmp/proved") || exit 1
left=$(inc list -f csv -c n | grep "^$tag-" | tr '\n' ' ')
[ "$left" = "$base " ] || fail "after the pumps the default project holds [$left], not only the template"

step "3. a restricted project, as Incus gives a user in the \`incus\` group"
useradd -m -s /bin/bash -G incus "$user" || fail "useradd"
uid=$(id -u "$user")
home=$(getent passwd "$user" | cut -d: -f6)
# /root is not the user's to read: the code and the script go to its home.
mkdir "$home/checkout" && (cd "$root" && tar --exclude __pycache__ -cf - org) | tar -C "$home/checkout" -xf - || fail "could not copy org"
cp "$tmp/live.py" "$home/live.py"
chown -R "$user:$user" "$home"
su - "$user" -c 'incus list' </dev/null >/dev/null 2>&1 || fail "incus does not answer the user"
project="user-$uid"
inc project show "$project" | grep -q '^ *restricted: "true"' || fail "Incus did not give the user a restricted project"
# The image Incus already holds, copied into the user's project, so that it is not pulled again.
image=$(inc config get "$base" volatile.base_image)
# (`incus image copy` from one project to another of the same server wants it listening on the network.)
inc image export "$image" "$tmp/image" >/dev/null && inc image import "$tmp/image" "$tmp/image.root" --project "$project" --alias "$tag-debian" >/dev/null \
  || fail "could not copy the image into $project"
rm -f "$tmp/image" "$tmp/image.root"
say "the user $user (uid $uid), in the project $project, restricted; the image copied in as $tag-debian"
cat > "$home/restricted.sh" <<RESTRICTED
set -u
incus init -q $tag-debian $base </dev/null || { echo "FAIL: the user could not make its template"; exit 1; }
incus config device override $base eth0 security.port_isolation=true security.ipv4_filtering=true </dev/null >/dev/null \
  || { echo "FAIL: the card's settings, in $project"; exit 1; }
echo "    $base: made by $user in its project, stopped"
cd "\$HOME" && exec python3 "\$HOME/live.py" "\$HOME/checkout" "\$HOME/ledger" $base $prefix $limit restricted no "\$HOME/proved"
RESTRICTED
chown "$user:$user" "$home/restricted.sh"
su - "$user" -c 'bash "$HOME/restricted.sh"' </dev/null || exit 1
cat "$home/proved" >> "$tmp/proved"

proved=$(cat "$tmp/proved")
step "4. removing the rest: the template, the user, its project and bridge, the image"
cleanup
left=$(inc list -f csv -c n | grep -c "^$tag-"; inc project list -f csv | cut -d, -f1 | grep -c "^user-$uid\b"; inc network list -f csv | cut -d, -f1 | grep -c "^incusbr-$uid\$"; getent passwd "$user" | grep -c .)
[ "$(echo $left)" = "0 0 0 0" ] || fail "something of this test is left behind (containers, project, bridge, user: $(echo $left))"
uid=""
say "nothing is left"
[ "$(printf '%s\n' "$proved" | grep -c .)" = 9 ] || fail "not everything was proved"
printf '%s\n' "$proved" | sed 's/^/PASS: /'
