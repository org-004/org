"""Running the installation itself: draining, upgrading, going back, and what the organization holds.

An upgrade is the moment code written by engineers starts running beside the tokens and the
ledger, so it is a decision of the person's and it is made on an exact commit:

  1. the commit must be on the default branch of the repository this copy was fetched from. A
     commit that exists only on some other branch, or in a fork, is refused.
  2. the person is shown what changed since the release in use, and says yes.
  3. the new commit's own tests run in a throwaway container that holds no token.
  4. dispatch is drained, the release is switched, the dashboard restarts, the drain is lifted.

Going back is moving a link: every release stays on disk.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple

from . import dispatch
from .backends import BackendError
from .backends.incus import WITHOUT_A_LIMIT, memory_limit
from .ledger import Ledger
from .transport import Incus, TransportError
from .util import now

SHARE = Path.home() / ".local" / "share" / "org"
SOURCE = Path.home() / "src" / "org"


class AdminError(Exception):
    pass


# ---------------------------------------------------------------- draining


def drain(ledger: Ledger, wait: bool = False, reason: str = "", log=print, poll: float = 5.0, timeout: Optional[float] = None) -> int:
    held = dispatch.drain(ledger, reason)
    log("draining since %s: approved work queues, nothing new starts. `org undrain` lifts it."
        % time.strftime("%a %H:%M", time.localtime(held.get("since") or now())))
    if not wait:
        left = dispatch.active(ledger)
        if left:
            log("still running: %s" % ", ".join(left))
        return 0
    from .sync import sync_all

    end = (now() + timeout) if timeout else None
    said = None
    while True:
        dispatch.reap(ledger)
        try:
            sync_all(ledger)
        except Exception:  # a machine that does not answer must not end the wait
            pass
        left = dispatch.active(ledger)
        if not left:
            log("drained: no run holds a machine")
            return 0
        if left != said:
            log("waiting for: %s" % ", ".join(left))
            said = left
        if end and now() > end:
            log("still running after the wait; the drain stays in force")
            return 1
        time.sleep(poll)


def undrain(ledger: Ledger, log=print) -> int:
    lifted = dispatch.undrain(ledger)
    log("drain lifted" if lifted else "no drain was in force")
    started = dispatch.pump(ledger, log=lambda m: None)
    if started:
        log("started: %s" % ", ".join(started))
    return 0


# ---------------------------------------------------------------- the organization's repositories


def repos(owner: str, timeout: float = 20) -> List[dict]:
    """The owner's public repositories, read without a login: the manager chat has none."""
    out: List[dict] = []
    for kind in ("orgs", "users"):
        url = "https://api.github.com/%s/%s/repos?per_page=100&type=public&sort=pushed" % (kind, owner)
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "org"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise AdminError("GitHub answered %d for %s" % (e.code, url))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise AdminError("could not reach GitHub: %s" % e)
        for r in data if isinstance(data, list) else []:
            out.append({"name": r.get("full_name"), "description": r.get("description") or "",
                        "default_branch": r.get("default_branch"), "pushed_at": r.get("pushed_at"), "archived": bool(r.get("archived"))})
        return out
    raise AdminError("GitHub has no organization or user named %s" % owner)


# ---------------------------------------------------------------- upgrading


def _git(args: List[str], check: bool = True, timeout: float = 120) -> str:
    p = subprocess.run(["git", "-C", str(SOURCE)] + args, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise AdminError("git %s: %s" % (" ".join(args[:3]), (p.stderr or p.stdout).strip()[-400:]))
    return p.stdout.strip()


def current_release() -> str:
    try:
        return (SHARE / "current" / "RELEASE").read_text().strip()
    except OSError:
        return ""


def _default_branch() -> str:
    head = _git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], check=False)
    if not head:
        _git(["remote", "set-head", "origin", "--auto"], check=False)
        head = _git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], check=False)
    if not head:
        raise AdminError("cannot tell the default branch of %s" % _git(["remote", "get-url", "origin"], check=False))
    return head.split("refs/remotes/", 1)[1]


def resolve(ref: str) -> Tuple[str, str]:
    """(full sha, default branch) for a ref that is on the default branch of the origin; else AdminError."""
    if not (SOURCE / ".git").is_dir():
        raise AdminError("%s is not a checkout: this copy was not installed from a repository (org-vm install <ref>)" % SOURCE)
    _git(["fetch", "--quiet", "--prune", "origin"], timeout=300)
    branch = _default_branch()
    sha = ""
    for candidate in ("refs/remotes/origin/%s" % ref, ref):
        sha = _git(["rev-parse", "--verify", "--quiet", candidate + "^{commit}"], check=False)
        if sha:
            break
    if not sha:
        raise AdminError("no commit named %s in %s" % (ref, _git(["remote", "get-url", "origin"], check=False)))
    # GitHub serves a commit pushed to any fork through the parent repository's address, so
    # "it has a sha here" proves nothing. Being reachable from the default branch does.
    if subprocess.run(["git", "-C", str(SOURCE), "merge-base", "--is-ancestor", sha, branch], capture_output=True).returncode != 0:
        raise AdminError("%s is not on %s. Only a commit that was merged there can be installed." % (sha[:12], branch))
    return sha, branch


def changes(old: str, new: str) -> str:
    """What the person is shown before saying yes."""
    base = old.split("-dirty-")[0]
    known = bool(base) and not base.startswith("copy-") and bool(_git(["rev-parse", "--verify", "--quiet", base + "^{commit}"], check=False))
    if not known:
        return ("the release in use (%s) is not a commit of this repository, so there is nothing to compare with.\n"
                "This installs %s as it stands:\n\n%s" % (old or "none", new[:12], _git(["log", "--oneline", "-15", new])))
    if _git(["rev-parse", base]) == new:
        return "that is the release in use"
    lines = ["commits:", _git(["log", "--oneline", "--no-decorate", "%s..%s" % (base, new)]) or "  (none: this goes back)",
             "", "files:", _git(["diff", "--stat", base, new]) or "  (no change)"]
    touchy = _git(["diff", "--name-only", base, new, "--", "incus", "bin", "org/secrets.py", "org/dispatch.py", "org/manager.py",
                   "org/backends", "org/admin.py", "org/sync.py"])
    if touchy:
        lines += ["", "among them, the files that hold the walls and the tokens:", "\n".join("  " + f for f in touchy.splitlines())]
    lines += ["", "the whole diff:  git -C %s diff %s %s" % (SOURCE, base[:12], new[:12])]
    return "\n".join(lines)


def test_commit(sha: str, base: str, log=print, limit: Optional[str] = None) -> None:
    """Run the commit's unit suite in a throwaway copy of the template: no token is in it, and
    it is not this user, so code nobody has run yet does not run beside the ledger."""
    name = "org-test-%s" % sha[:12]
    if Incus.info(name) is not None:
        Incus.delete(name)
    tar = subprocess.run(["git", "-C", str(SOURCE), "archive", "--format=tar", sha], capture_output=True, timeout=120)
    if tar.returncode != 0:
        raise AdminError("git archive %s failed" % sha[:12])
    log("testing %s in a container with no tokens (%s)" % (sha[:12], name))
    try:
        # The same limit an engineer's container gets: a suite that needs more fails here, not there.
        try:
            tr = Incus.create(base, name, {"limits.memory": limit} if limit else None)
        except TransportError as e:
            if not limit:
                raise
            raise AdminError("%s. The copy was asked for limits.memory=%s; if Incus will not take that key here, %s."
                             % (str(e).rstrip("."), limit, WITHOUT_A_LIMIT))
        tr.run("mkdir -p /root/t && tar -xf - -C /root/t", input=tar.stdout, timeout=120)
        p = tr.run("cd /root/t && python3 -m unittest discover -s tests 2>&1 | tail -25", timeout=1800, check=False)
        out = p.stdout.decode("utf-8", "replace").strip()
        last = out.splitlines()[-1] if out else ""
        if not last.startswith("OK"):
            raise AdminError("the tests of %s did not pass:\n%s" % (sha[:12], out or "(no output)"))
        log(out.splitlines()[-3] if len(out.splitlines()) >= 3 else out)
    finally:
        try:
            Incus.delete(name)
        except TransportError:
            pass


def _install(log=print) -> None:
    up = SOURCE / "incus" / "up.sh"
    p = subprocess.run([str(up)], stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        raise AdminError("%s exited %d; the release in use is %s" % (up, p.returncode, current_release() or "unknown"))


def upgrade(ledger: Ledger, ref: str, yes: bool = False, skip_tests: bool = False, log=print) -> int:
    sha, branch = resolve(ref)
    old = current_release()
    log("upgrade to %s (on %s); in use: %s\n" % (sha[:12], branch, old or "nothing"))
    said = changes(old, sha)
    log(said + "\n")
    if said == "that is the release in use":
        return 0
    if not yes:
        if not sys.stdin.isatty():
            raise AdminError("an upgrade is confirmed by a person at a terminal (or pass --yes having read the diff)")
        if input("Install %s? Type yes: " % sha[:12]).strip().lower() != "yes":
            log("nothing changed")
            return 1
    cfg = ledger.config()
    base = ((cfg.get("machines") or {}).get("incus") or {}).get("base") or "org-base"
    if not skip_tests:
        try:
            limit = memory_limit(cfg)
        except BackendError as e:
            raise AdminError(str(e))
        test_commit(sha, base, log, limit)
    ours = dispatch.draining(ledger) is None
    dispatch.drain(ledger, "upgrading to %s" % sha[:12])
    try:
        # A dispatch in flight belongs to a process of the old release; let it finish its copy.
        end = now() + 300
        while any(ledger.run(r).get("state") == "provisioning" for r in ledger.run_ids()) and now() < end:
            dispatch.reap(ledger)
            time.sleep(1)
        _git(["checkout", "--quiet", "--detach", sha])
        _install(log)
    finally:
        if ours:
            dispatch.undrain(ledger)
    log("\nnow running %s. Engineers already at work keep the code they started with. To go back: org upgrade --rollback"
        % (current_release() or sha[:12]))
    return 0


def rollback(ledger: Ledger, log=print) -> int:
    prev = SHARE / "previous"
    if not prev.is_symlink():
        raise AdminError("there is no earlier release to go back to")
    target = os.readlink(prev)
    now_ = os.readlink(SHARE / "current")
    if not (SHARE / target).is_dir():
        raise AdminError("the earlier release %s is no longer on disk" % target)
    tmp = SHARE / ".current.new"
    if tmp.is_symlink():
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, SHARE / "current")
    tmp2 = SHARE / ".previous.new"
    if tmp2.is_symlink():
        tmp2.unlink()
    os.symlink(now_, tmp2)
    os.replace(tmp2, prev)
    subprocess.run(["systemctl", "--user", "restart", "org-dash.service"], capture_output=True)
    log("back on %s (was %s); the dashboard was restarted" % (current_release() or target, now_.replace("releases/", "")))
    return 0
