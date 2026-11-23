#!/usr/bin/env python3
"""A stand-in for OrbStack's `orb`, covering exactly the commands org (and `box`) use.

Each "machine" is a directory under $FAKE_ORB_ROOT with its own home, so a command run "in" a
machine sees a different $HOME from the test process and from other machines. That is enough to
exercise provisioning, the token copy, the bundle transfer, the detached start and sync.

    orb create [flags] <distro> <name>     orb clone <base> <name>     orb delete -f <name>
    orb start <name>   orb stop <name>     orb list                    orb status
    orb -m <name> <argv...>                run argv with HOME set to the machine's home

A clone copies its source's home, as the real one copies the whole machine. A machine is stopped
or running; a command in a stopped machine starts it.
"""
import json
import os
import shutil
import sys
from pathlib import Path

FLAGS_WITH_VALUE = ("--arch", "--memory", "--cpus", "--disk", "--mount", "-c", "--user-data", "-u", "--user")


def main(argv):
    root = Path(os.environ["FAKE_ORB_ROOT"])
    root.mkdir(parents=True, exist_ok=True)
    with open(root / "calls.log", "a") as f:
        f.write(" ".join(argv[:4]) + "\n")
    with open(root / "calls.jsonl", "a") as f:
        f.write(json.dumps(argv) + "\n")
    if not argv:
        return 2

    def state(name, value=None):
        path = root / name / "state"
        if value is None:
            return path.read_text().strip() if path.exists() else "running"
        path.write_text(value + "\n")

    if argv[0] == "status":
        return 0
    if argv[0] == "create":
        words, flags, i = [], [], 1
        while i < len(argv):
            if argv[i] in FLAGS_WITH_VALUE:
                flags += argv[i:i + 2]
                i += 2
            elif argv[i].startswith("-"):
                flags.append(argv[i])
                i += 1
            else:
                words.append(argv[i])
                i += 1
        if len(words) != 2:
            sys.stderr.write("usage: orb create [flags] <distro> <name>\n")
            return 2
        name = words[1]
        if (root / name).exists():
            sys.stderr.write("machine already exists: %s\n" % name)
            return 1
        (root / name / "home").mkdir(parents=True)
        (root / name / "created.json").write_text(json.dumps({"distro": words[0], "flags": flags}))
        state(name, "running")
        return 0
    if argv[0] == "clone" and len(argv) == 3:
        base, name = argv[1], argv[2]
        if (root / name).exists():
            sys.stderr.write("machine already exists: %s\n" % name)
            return 1
        if (root / base / "home").is_dir():
            (root / name).mkdir(parents=True)
            shutil.copytree(root / base / "home", root / name / "home", symlinks=True)
            if (root / base / "created.json").exists():
                shutil.copy(root / base / "created.json", root / name / "created.json")
        elif base == os.environ.get("FAKE_ORB_BASE", "base"):
            (root / name / "home").mkdir(parents=True)  # a template the test did not bother to make
        else:
            sys.stderr.write("machine not found: %s\n" % base)
            return 1
        state(name, "stopped")
        return 0
    if argv[0] in ("start", "stop") and len(argv) == 2:
        if not (root / argv[1]).is_dir():
            sys.stderr.write("machine not found: %s\n" % argv[1])
            return 1
        state(argv[1], "running" if argv[0] == "start" else "stopped")
        return 0
    if argv[0] == "delete":
        name = argv[-1]
        if not (root / name).is_dir():
            sys.stderr.write("machine not found: %s\n" % name)
            return 1
        shutil.rmtree(root / name)
        return 0
    if argv[0] == "list":
        print("NAME  STATE  DISTRO  ARCH")
        for p in sorted(root.iterdir()):
            if p.is_dir():
                print("%s  %s  arch  arm64" % (p.name, state(p.name)))
        return 0
    if argv[0] == "-m" and len(argv) >= 3:
        home = root / argv[1] / "home"
        if not home.is_dir():
            sys.stderr.write("machine not found: %s\n" % argv[1])
            return 1
        state(argv[1], "running")
        env = dict(os.environ, HOME=str(home))
        env.pop("ORG_HOME", None)       # a machine has its own ledger, if it has one at all
        env.pop("ORG_HOST_DIR", None)   # and it cannot see the host's files
        os.chdir(home)
        os.execvpe(argv[2], argv[2:], env)
    sys.stderr.write("fake orb: unsupported: %s\n" % " ".join(argv))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
