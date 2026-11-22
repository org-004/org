#!/usr/bin/env python3
"""A stand-in for OrbStack's `orb`, covering exactly the commands crew (and `box`) use.

Each "machine" is a directory under $FAKE_ORB_ROOT with its own home, so a command run "in" a
machine sees a different $HOME from the test process and from other machines. That is enough to
exercise provisioning, the token copy, the bundle transfer, the detached start and sync.

    orb clone <base> <name>      orb start <name>      orb delete -f <name>      orb list
    orb -m <name> <argv...>      run argv with HOME set to the machine's home
"""
import os
import shutil
import sys
from pathlib import Path


def main(argv):
    root = Path(os.environ["FAKE_ORB_ROOT"])
    root.mkdir(parents=True, exist_ok=True)
    with open(root / "calls.log", "a") as f:
        f.write(" ".join(argv[:4]) + "\n")
    if not argv:
        return 2
    if argv[0] == "clone" and len(argv) == 3:
        base, name = argv[1], argv[2]
        if base != os.environ.get("FAKE_ORB_BASE", "base"):
            sys.stderr.write("machine not found: %s\n" % base)
            return 1
        if (root / name).exists():
            sys.stderr.write("machine already exists: %s\n" % name)
            return 1
        (root / name / "home").mkdir(parents=True)
        return 0
    if argv[0] == "start" and len(argv) == 2:
        return 0 if (root / argv[1]).is_dir() else 1
    if argv[0] == "delete":
        name = argv[-1]
        if not (root / name).is_dir():
            sys.stderr.write("machine not found: %s\n" % name)
            return 1
        shutil.rmtree(root / name)
        return 0
    if argv[0] == "list":
        for p in sorted(root.iterdir()):
            if p.is_dir():
                print("%s  running  arch  arm64" % p.name)
        return 0
    if argv[0] == "-m" and len(argv) >= 3:
        home = root / argv[1] / "home"
        if not home.is_dir():
            sys.stderr.write("machine not found: %s\n" % argv[1])
            return 1
        env = dict(os.environ, HOME=str(home))
        os.chdir(home)
        os.execvpe(argv[2], argv[2:], env)
    sys.stderr.write("fake orb: unsupported: %s\n" % " ".join(argv))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
