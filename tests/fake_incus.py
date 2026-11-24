#!/usr/bin/env python3
"""A stand-in for the `incus` client, covering the commands org and its installer use.

Each "container" is a directory under $FAKE_INCUS_ROOT with its own home, so a command run "in" a
container sees a different $HOME, and a different place for its tokens, from the test process
and from other containers. That is enough to exercise provisioning, the vetting of a container
from Incus's description of it, the token copy, the bundle transfer, the detached start and sync.

    incus query /1.0/instances/<name>       the container as JSON: status, expanded_config, expanded_devices
    incus copy [-q] <base> <name>           incus init [flags] <image> <name>
    incus start|stop [-q] <name>            incus delete [-q] [-f] <name>
    incus list [-f csv|json] [-c ns] [prefix]
    incus exec <name> [-T|-t] [--env K=V] -- <argv...>
    incus config … | network … | profile … | storage … | admin …      accepted, recorded, nothing done

Every call is appended to calls.log (its first four words) and calls.jsonl (all of it), so a test
can assert on exactly what would have been sent to Incus.

What a container looks like to `query` is an isolated one unless <container>/override.json says
otherwise: {"expanded_config": {...}, "expanded_devices": {...}} is laid over the default, key by
key, and a null removes a key. A test writes that file to make a container privileged, give it a
host path, or take the isolation off its network card. A copy carries its source's override, as
a real copy carries its source's configuration.

A copy also carries its source's home, but not its tokens: those are in memory in a real
container ($ORG_SECRETS_DIR here), so a copy starts with none and stopping a container loses
them. A container is stopped or running, a copy starts stopped, and a command in a stopped
container fails, as the real one does.
A container's /etc/bazel.bazelrc is <container>/home/etc-bazel.bazelrc ($ORG_BAZELRC), and its
address is what override.json says under "state", as `incus list --format json` reports it.
The template named by $FAKE_INCUS_BASE (default `base`) exists, stopped and empty, without the
test having to make it; any other name the test did not make is not found.
"""
import json
import os
import re
import shutil
import sys
from pathlib import Path

SECRETS = ".run-org"  # under a container's home: what /run/org is in a real one
BAZELRC = "etc-bazel.bazelrc"  # under a container's home: what /etc/bazel.bazelrc is in a real one
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")
NOTHING_DONE = ("config", "network", "profile", "storage", "admin")
INIT_FLAGS_WITH_VALUE = ("-c", "--config", "-d", "--device", "-p", "--profile", "-s", "--storage", "-n", "--network",
                         "-t", "--type", "--target", "--description")
EXEC_FLAGS_WITH_VALUE = ("--env", "--cwd", "--user", "--group", "--mode")
ISOLATED = {
    "root": {"type": "disk", "path": "/", "pool": "default"},
    "eth0": {"type": "nic", "name": "eth0", "network": "incusbr0",
             "security.port_isolation": "true", "security.ipv4_filtering": "true"},
}
IP_STUB = "#!/bin/sh\necho '2: eth0    inet 10.0.0.2/24 brd 10.0.0.255 scope global eth0'\n"


def words(args, with_value=()):
    """(the arguments that are not flags, the flags), for commands whose flags may come anywhere."""
    plain, flags, i = [], [], 0
    while i < len(args):
        if args[i] in with_value:
            flags += args[i:i + 2]
            i += 2
        elif args[i].startswith("-") and args[i] != "-":
            flags.append(args[i])
            i += 1
        else:
            plain.append(args[i])
            i += 1
    return plain, flags


def main(argv):
    root = Path(os.environ["FAKE_INCUS_ROOT"])
    root.mkdir(parents=True, exist_ok=True)
    with open(root / "calls.log", "a") as f:
        f.write(" ".join(argv[:4]) + "\n")
    with open(root / "calls.jsonl", "a") as f:
        f.write(json.dumps(argv) + "\n")
    if not argv:
        return 2

    def fail(message, code=1):
        sys.stderr.write("Error: %s\n" % message)
        return code

    def state(name, value=None):
        path = root / name / "state"
        if value is None:
            return path.read_text().strip() if path.exists() else "stopped"
        path.write_text(value + "\n")

    def box(name):
        """The container's directory, or None when there is no such container."""
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            return None
        path = root / name
        if (path / "home").is_dir():
            return path
        if name == os.environ.get("FAKE_INCUS_BASE", "base"):
            (path / "home").mkdir(parents=True, exist_ok=True)  # a template the test did not bother to make
            state(name, "stopped")
            return path
        return None

    def names():
        return sorted(p.name for p in root.iterdir() if NAME_RE.fullmatch(p.name) and (p / "home").is_dir())

    def describe(name):
        running = state(name) == "running"
        info = {"name": name, "type": "container", "status": "Running" if running else "Stopped",
                "status_code": 103 if running else 102, "profiles": ["default"], "config": {}, "devices": {},
                "expanded_config": {}, "expanded_devices": json.loads(json.dumps(ISOLATED))}
        try:
            override = json.loads((root / name / "override.json").read_text())
        except (OSError, ValueError):
            override = {}
        for key, value in override.items():
            if key in ("expanded_config", "expanded_devices") and isinstance(value, dict):
                for k, v in value.items():
                    if v is None:
                        info[key].pop(k, None)
                    else:
                        info[key][k] = v
            else:
                info[key] = value
        return info

    cmd, args = argv[0], argv[1:]

    if cmd in NOTHING_DONE:
        return 0

    if cmd == "query":
        plain, flags = words(args, ("-X", "--request", "-d", "--data"))
        if flags or len(plain) != 1:
            return fail("fake incus: only `query <path>` is supported, not %s" % " ".join(args), 2)
        path = plain[0].rstrip("/")
        if path == "/1.0":
            print(json.dumps({"api_version": "1.0", "environment": {"server": "incus", "server_version": "fake"}}))
            return 0
        if path == "/1.0/instances":
            print(json.dumps(["/1.0/instances/" + n for n in names()]))
            return 0
        if path.startswith("/1.0/instances/") and box(path[len("/1.0/instances/"):]):
            print(json.dumps(describe(path[len("/1.0/instances/"):])))
            return 0
        return fail("Instance not found" if path.startswith("/1.0/instances/") else "not found")

    if cmd == "list":
        plain, flags = words(args, ("-f", "--format", "-c", "--columns"))
        fmt, columns = "table", "ns"
        for i, flag in enumerate(flags):
            if flag in ("-f", "--format"):
                fmt = flags[i + 1]
            elif flag.startswith("--format="):
                fmt = flag.split("=", 1)[1]
            elif flag in ("-c", "--columns"):
                columns = flags[i + 1]
            elif flag.startswith("--columns="):
                columns = flag.split("=", 1)[1]
        # An exact name may be the template a test did not bother to make: asking makes it, as `query` does.
        for p in plain:
            if p.startswith("^") and p.endswith("$"):
                box(p[1:-1])
        # A filter is a pattern on the name, as in the real client; a plain word matches as a prefix.
        found = [n for n in names() if not plain or any(re.search(p if p.startswith("^") else "^" + re.escape(p), n) for p in plain)]
        if fmt == "json":
            print(json.dumps([describe(n) for n in found]))
            return 0
        if fmt != "csv":
            print("NAME  STATE")
        for n in found:
            row = [{"n": n, "s": state(n).upper()}.get(c, "") for c in columns]
            print((",".join(row)) if fmt == "csv" else "%s  %s" % (n, state(n).upper()))
        return 0

    if cmd in ("init", "launch"):
        plain, flags = words(args, INIT_FLAGS_WITH_VALUE)
        if len(plain) != 2:
            return 0  # an image and no name: recorded, and nothing a test could look for
        image, name = plain
        if not NAME_RE.fullmatch(name):
            return fail("Invalid instance name: %s" % name)
        if (root / name).exists():
            return fail('Instance "%s" already exists' % name)
        (root / name / "home").mkdir(parents=True)
        (root / name / "created.json").write_text(json.dumps({"image": image, "flags": flags}))
        state(name, "running" if cmd == "launch" else "stopped")
        return 0

    if cmd == "copy":
        plain, _ = words(args)
        if len(plain) != 2:
            return fail("usage: incus copy [flags] <source> <destination>", 2)
        base, name = plain
        source = box(base)
        if not source:
            return fail("Instance not found: %s" % base)
        if not NAME_RE.fullmatch(name):
            return fail("Invalid instance name: %s" % name)
        if (root / name).exists():
            return fail('Instance "%s" already exists' % name)
        (root / name).mkdir(parents=True)
        home = source / "home"
        shutil.copytree(home, root / name / "home", symlinks=True,
                        ignore=lambda directory, entries: [SECRETS] if Path(directory) == home else [])
        for extra in ("override.json", "created.json"):
            if (source / extra).exists():
                shutil.copy(source / extra, root / name / extra)
        state(name, "stopped")
        return 0

    if cmd in ("start", "stop"):
        plain, _ = words(args)
        if len(plain) != 1:
            return fail("usage: incus %s [flags] <instance>" % cmd, 2)
        if not box(plain[0]):
            return fail("Instance not found: %s" % plain[0])
        want = "running" if cmd == "start" else "stopped"
        if state(plain[0]) == want:
            return fail("The instance is already %s" % want)
        state(plain[0], want)
        if cmd == "stop":
            shutil.rmtree(root / plain[0] / "home" / SECRETS, ignore_errors=True)  # its memory goes with it
        return 0

    if cmd == "delete":
        plain, flags = words(args)
        if not plain:
            return fail("usage: incus delete [flags] <instance>", 2)
        force = any(f in ("-f", "--force") or (re.fullmatch(r"-[a-z]+", f) and "f" in f) for f in flags)
        for name in plain:
            if not box(name):
                return fail("Instance not found: %s" % name)
            if state(name) == "running" and not force:
                return fail("The instance is currently running, stop it first or pass --force")
            shutil.rmtree(root / name)
        return 0

    if cmd == "exec":
        before, command = (args[:args.index("--")], args[args.index("--") + 1:]) if "--" in args else (args[:1], args[1:])
        plain, flags = words(before, EXEC_FLAGS_WITH_VALUE)
        if len(plain) != 1 or not command:
            return fail("usage: incus exec <instance> [flags] -- <command>", 2)
        name = plain[0]
        if not box(name):
            return fail("Instance not found: %s" % name)
        if state(name) != "running":
            return fail("Instance is not running")
        home = root / name / "home"
        env = dict(os.environ, HOME=str(home))
        env.pop("ORG_HOME", None)       # a container has its own ledger, if it has one at all,
        env.pop("FAKE_GH_TOKEN", None)  # no login of this side's,
        env["ORG_SECRETS_DIR"] = str(home / SECRETS)  # and its own memory for the tokens it is handed
        env["ORG_BAZELRC"] = str(home / BAZELRC)      # and its own /etc/bazel.bazelrc, never this machine's
        for i, flag in enumerate(flags):
            pair = flags[i + 1] if flag == "--env" and i + 1 < len(flags) else (flag[6:] if flag.startswith("--env=") else "")
            if "=" in pair:
                env[pair.split("=", 1)[0]] = pair.split("=", 1)[1]
        # A started container has an address. `ip` says so here on any system, macOS included.
        stubs = root / ".bin"
        if not (stubs / "ip").exists():
            stubs.mkdir(exist_ok=True)
            fresh = stubs / (".ip.%d" % os.getpid())
            fresh.write_text(IP_STUB)
            fresh.chmod(0o755)
            os.replace(fresh, stubs / "ip")  # whole or absent: another call may be about to run it
        env["PATH"] = str(stubs) + os.pathsep + env.get("PATH", "")
        os.chdir(home)
        try:
            os.execvpe(command[0], command, env)
        except OSError as e:
            return fail("fake incus: cannot run %s: %s" % (command[0], e), 127)

    sys.stderr.write("fake incus: unsupported: %s\n" % " ".join(argv))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
