#!/usr/bin/env python3
"""A stand-in for the `docker` client, covering the commands org uses.

Each "container" is a directory under $FAKE_DOCKER_ROOT with its own home, so a command run "in"
one sees a different $HOME from the test process and from other containers. Every invocation is
appended to calls.log, with the -H host when one was given, so tests can assert on what would
have been sent to which daemon.

    docker [-H host] run -d [flags] --name <c> <image> sleep infinity
    docker [-H host] exec -i <c> <argv...>
    docker [-H host] rm -f <c>           docker [-H host] start <c>
    docker [-H host] inspect -f <fmt> <c>
    docker [-H host] network inspect <n> | network create [flags] <n>
"""
import json
import os
import shutil
import sys
from pathlib import Path

VALUE_FLAGS = {"--name", "--hostname", "--label", "--network", "--cpus", "--memory", "-e", "-v", "-p", "-o", "--driver"}


def main(argv):
    root = Path(os.environ["FAKE_DOCKER_ROOT"])
    root.mkdir(parents=True, exist_ok=True)
    host = ""
    if argv[:1] == ["-H"]:
        host, argv = argv[1], argv[2:]
    with open(root / "calls.log", "a") as f:
        f.write(json.dumps({"host": host, "argv": argv[:12]}) + "\n")
    if not argv:
        return 2
    cmd = argv[0]
    if cmd == "network":
        net = root / ("network-" + argv[-1])
        if argv[1] == "inspect":
            return 0 if net.exists() else 1
        if argv[1] == "create":
            net.write_text(" ".join(argv[2:]))
            return 0
    if cmd == "run":
        opts, rest, i = {}, [], 1
        while i < len(argv):
            a = argv[i]
            if a in VALUE_FLAGS:
                opts.setdefault(a, []).append(argv[i + 1])
                i += 2
            elif a.startswith("-"):
                i += 1
            else:
                rest = argv[i:]
                break
        name, image = opts["--name"][0], rest[0]
        if image != os.environ.get("FAKE_DOCKER_IMAGE", "org:latest"):
            sys.stderr.write("Unable to find image '%s' locally\n" % image)
            return 125
        if (root / name).exists():
            sys.stderr.write('Conflict. The container name "/%s" is already in use\n' % name)
            return 125
        (root / name / "home").mkdir(parents=True)
        (root / name / "container.json").write_text(json.dumps({"image": image, "host": host, "opts": opts, "cmd": rest[1:]}))
        print("f" * 12)
        return 0
    name = argv[-1] if cmd in ("rm", "start", "inspect") else (argv[2] if cmd == "exec" and argv[1] == "-i" else argv[1])
    box = root / name
    if cmd == "inspect":
        if not box.is_dir():
            sys.stderr.write("Error: No such object: %s\n" % name)
            return 1
        print("running")
        return 0
    if not box.is_dir():
        sys.stderr.write("Error response from daemon: No such container: %s\n" % name)
        return 1
    if cmd == "rm":
        shutil.rmtree(box)
        return 0
    if cmd == "start":
        return 0
    if cmd == "exec":
        rest = argv[3:] if argv[1] == "-i" else argv[2:]
        env = dict(os.environ, HOME=str(box / "home"))
        os.chdir(box / "home")
        os.execvpe(rest[0], rest, env)
    sys.stderr.write("fake docker: unsupported: %s\n" % " ".join(argv))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
