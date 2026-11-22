#!/usr/bin/env python3
"""A stand-in for `gh`, so tests never read or copy a real GitHub login.

    gh auth token                 prints $FAKE_GH_TOKEN (exit 1 when unset)
    gh auth login --with-token    stores the token read from stdin in $HOME/.config/gh/token
    gh auth setup-git             records that it ran
"""
import os
import sys
from pathlib import Path


def main(argv):
    if argv[:2] == ["auth", "token"]:
        token = os.environ.get("FAKE_GH_TOKEN")
        if not token:
            return 1
        print(token)
        return 0
    conf = Path(os.environ["HOME"]) / ".config" / "gh"
    if argv[:2] == ["auth", "login"]:
        conf.mkdir(parents=True, exist_ok=True)
        (conf / "token").write_text(sys.stdin.read().strip())
        return 0
    if argv[:2] == ["auth", "setup-git"]:
        conf.mkdir(parents=True, exist_ok=True)
        (conf / "setup-git").write_text("done")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
