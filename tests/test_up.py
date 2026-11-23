"""`org up` and `org down` on a Mac with OrbStack, and the `org` wrapper that forwards into the manager.

The Mac is a temporary home directory. OrbStack is tests/fake_orb.py, in which a machine is a
directory with a home of its own. The scripts under test are the real ones (orbstack/up.sh,
orbstack/bootstrap.sh, bin/org), and so are the agent and the bridge that `org up` starts; the
package installs are skipped (ORG_UP_NO_INSTALL), since the "machines" here are this computer.
"""
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_manager_side import GOOD, TURNS  # noqa: E402

ORG = str(ROOT / "bin" / "org")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class UpCase(unittest.TestCase):
    def setUp(self):
        # Under /tmp: the bridge's socket lives in the manager's ledger, and its path must stay short.
        self.tmp = Path(tempfile.mkdtemp(prefix="org-up-", dir="/tmp")).resolve()
        self.addCleanup(self._cleanup)
        self.orb_root = self.tmp / "orb"
        self.bindir = self.tmp / "bin"
        self.bindir.mkdir()
        for name, target in (("orb", "fake_orb.py"), ("claude", "fake_claude.py"), ("gh", "fake_gh.py")):
            os.symlink(ROOT / "tests" / target, self.bindir / name)
        # What the template is checked for and this computer may not have. docker answers no.
        for name, code in (("bwrap", 0), ("socat", 0), ("tmux", 1), ("docker", 1)):
            self.stub(name, code)
        self.mac = self.tmp / "mac"
        (self.mac / ".claude").mkdir(parents=True)
        (self.mac / ".claude" / "oauth-token").write_text("sk-ant-oat01-not-a-real-token\n")
        (self.mac / ".gitconfig").write_text("[user]\n\tname = Ada Example\n\temail = ada@example.test\n")
        self.host_dir = self.tmp / "host"
        scen = self.tmp / "scenario.json"
        scen.write_text(json.dumps({"turns": TURNS}))
        self.port = free_port()
        self.env = dict(os.environ, HOME=str(self.mac), PATH=str(self.bindir) + os.pathsep + os.environ["PATH"],
                        FAKE_ORB_ROOT=str(self.orb_root), FAKE_GH_TOKEN="gho_not_a_real_token", FAKE_CLAUDE_SCENARIO=str(scen),
                        ORG_HOST_DIR=str(self.host_dir), ORG_UP_NO_INSTALL="1", ORG_PORT=str(self.port))
        for key in ("ORG_HOME", "ORG_LOCAL", "ORG_IN_CONTAINER", "ORG_MANAGER", "ORG_TEMPLATE", "ORG_PREFIX", "XDG_CONFIG_HOME",
                    "GIT_CONFIG_GLOBAL"):
            self.env.pop(key, None)

    def stub(self, name: str, code: int) -> None:
        path = self.bindir / name
        path.write_text("#!/bin/sh\nexit %d\n" % code)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _cleanup(self):
        subprocess.run([ORG, "down", "--purge"], env=self.env, capture_output=True, timeout=120)
        subprocess.run(["pkill", "-f", str(self.tmp)], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def org(self, *args, ok=True, timeout=180, **env):
        p = subprocess.run([ORG] + list(args), env=dict(self.env, **env), capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        if ok:
            self.assertEqual(p.returncode, 0, "org %s\n%s\n%s" % (" ".join(args), p.stdout, p.stderr))
        return p

    def home(self, machine: str) -> Path:
        return self.orb_root / machine / "home"

    def calls(self):
        return [json.loads(line) for line in (self.orb_root / "calls.jsonl").read_text().splitlines()]

    def agent_pid(self) -> int:
        return int((self.host_dir / "agent.pid").read_text())

    @staticmethod
    def alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


class Up(UpCase):
    def test_up_makes_the_template_the_manager_and_the_agent_and_down_takes_them_away(self):
        out = self.org("up", "--slots", "2").stdout
        self.assertIn("ready.", out)
        self.assertIn("http://127.0.0.1:%d/#t=" % self.port, out)

        # The template: created isolated, with this Mac's logins in it.
        created = json.loads((self.orb_root / "org-base" / "created.json").read_text())
        self.assertEqual((created["distro"], created["flags"]), ("debian:trixie", ["--isolated"]))
        base = self.home("org-base")
        token = base / ".claude" / "oauth-token"
        self.assertEqual(token.read_text(), "sk-ant-oat01-not-a-real-token\n")
        self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)
        self.assertEqual((base / ".config" / "gh" / "token").read_text(), "gho_not_a_real_token")
        self.assertIn("ada@example.test", (base / ".gitconfig").read_text())
        self.assertFalse(json.loads((base / ".claude" / "settings.json").read_text())["autoMemoryEnabled"])
        self.assertEqual((self.orb_root / "org-base" / "state").read_text().strip(), "stopped", "it is copied, never run")

        # The manager: a clone of it, with org installed and the ledger pointed at the agent.
        mgr = self.home("org-manager")
        self.assertIn(["clone", "org-base", "org-manager"], self.calls())
        self.assertTrue((mgr / ".local" / "share" / "org" / "org" / "cli.py").is_file())
        self.assertTrue((mgr / ".local" / "share" / "org" / "LOCAL").exists(), "the copy knows it is the installation")
        self.assertEqual(os.readlink(mgr / ".local" / "bin" / "org"), str(mgr / ".local" / "share" / "org" / "bin" / "org"))
        self.assertEqual((mgr / ".config" / "gh" / "token").read_text(), "gho_not_a_real_token", "a clone inherits the login")
        cfg = json.loads((mgr / ".org" / "config.json").read_text())
        self.assertEqual(cfg["machines"]["default"], "orb")
        self.assertEqual(cfg["machines"]["orb"], {"via": "agent", "base": "org-base", "prefix": "org-eng-", "slots": 2})
        self.assertEqual((cfg["dash"]["port"], cfg["manager"]["own_claude"]), (self.port, True))
        self.assertEqual((cfg["manager"]["model"], cfg["manager"]["effort"]), ("fable", "high"),
                         "the manager chat is not left on a fresh install's default model")

        # The agent: one process on the Mac, connected.
        self.assertEqual(json.loads((self.host_dir / "agent.json").read_text()),
                         {"orb": "orb", "manager": "org-manager", "template": "org-base", "prefix": "org-eng-", "dash_port": self.port})
        first = self.agent_pid()
        self.assertTrue(self.alive(first))
        self.assertIn("host agent connected", self.org("agent", "status").stdout)
        doctor = self.org("doctor", ok=False).stdout
        self.assertIn("ok    host agent", doctor)
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open("http://127.0.0.1:%d/" % self.port, timeout=10) as r:
            self.assertIn(b"<title>org</title>", r.read())
        self.assertIn("#t=", self.org("dash", "--url").stdout)

        # Every command but up, down and `agent run` runs inside the manager: a spec goes from
        # draft to a finished engineer in a machine of its own, through the wrapper alone.
        self.org("config", "set", "machines.detach", "nohup")
        spec_id, draft = self.org("spec", "new", "CSV export", "--project", "acme").stdout.split()
        self.assertTrue(draft.startswith(str(mgr / ".org" / "desk" / "drafts")), draft)
        Path(draft).write_text(GOOD)
        sha = self.org("spec", "check", spec_id).stdout.split("sha256 ")[1].split()[0]
        approved = self.org("approve", spec_id, "--sha", sha).stdout
        self.assertIn("dispatched %s-r1" % spec_id, approved)
        engineer = "org-eng-%s-r1" % spec_id
        self.assertIn(["clone", "org-base", engineer], self.calls())
        end = time.time() + 60
        while time.time() < end:
            state = json.loads(self.org("status", "--json").stdout)
            run = [r for r in state["runs"] if r["run_id"] == spec_id + "-r1"][0]
            if run["terminal"]:
                break
            time.sleep(0.5)
        self.assertEqual(run["state"], "done", run)
        self.assertIn("Delivered", self.org("report", spec_id).stdout)
        self.assertEqual(self.org("exec", spec_id, "--", "echo", "two  words").stdout, "two  words\n", "arguments arrive as typed")

        # A second `org up` keeps the machines and the ledger, and replaces the agent.
        before = len([c for c in self.calls() if c[0] in ("create", "clone")])
        self.org("config", "set", "manager.model", "opus")
        out = self.org("up").stdout
        self.assertIn("ready.", out)
        self.assertEqual(len([c for c in self.calls() if c[0] in ("create", "clone")]), before, "nothing is created twice")
        self.assertIn(spec_id, self.org("spec", "ls").stdout)
        self.assertEqual(self.org("config", "get", "machines.orb.slots").stdout.strip(), "2", "a later up keeps the pool's size")
        self.assertEqual(self.org("config", "get", "manager.model").stdout.strip(), '"opus"', "and the model that was chosen")
        second = self.agent_pid()
        self.assertNotEqual(first, second)
        self.assertFalse(self.alive(first), "the earlier agent was stopped")
        self.assertTrue(self.alive(second))

        # down: the agent stops and the manager is stopped; engineers' machines stay unless asked.
        out = self.org("down").stdout
        self.assertIn("host agent stopped", out)
        self.assertIn("engineers' machines are left as they are: 1", out)
        self.assertFalse(self.alive(second))
        self.assertEqual((self.orb_root / "org-manager" / "state").read_text().strip(), "stopped")
        self.assertTrue((self.orb_root / engineer).is_dir())
        self.assertIn("deleted %s" % engineer, self.org("down", "--all").stdout)
        self.assertTrue((self.orb_root / "org-manager").is_dir(), "the ledger survives down --all")
        out = self.org("down", "--purge").stdout
        self.assertIn("deleted org-manager", out)
        self.assertIn("deleted org-base", out)
        self.assertFalse((self.host_dir / "agent.json").exists())
        self.assertFalse((self.orb_root / "org-manager").exists())

    def test_the_template_can_be_a_clone_of_a_machine_you_have(self):
        seed = self.home("mine")
        (seed / "projects").mkdir(parents=True)
        (seed / "projects" / "notes.txt").write_text("already set up\n")
        (seed / ".claude").mkdir()
        (seed / ".claude" / "settings.json").write_text('{"theme": "dark"}\n')
        out = self.org("up", "--from", "mine").stdout
        self.assertIn("template: org-base, cloned from mine", out)
        self.assertEqual([c for c in self.calls() if c[0] == "create"], [])
        self.assertTrue((self.home("org-base") / "projects" / "notes.txt").is_file())
        self.assertEqual((self.home("org-base") / ".claude" / "settings.json").read_text(), '{"theme": "dark"}\n',
                         "settings that were there are left alone")
        self.assertEqual(self.org("config", "get", "machines.orb.slots").stdout.strip(), "4", "the pool's default size")
        p = self.org("up", "--from", "nothing-here", ok=False, ORG_TEMPLATE="other-base")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no machine named nothing-here", p.stderr)

    def test_up_refuses_what_would_not_work(self):
        p = self.org("up", ok=False, ORG_PREFIX="org-")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("starts with the engineers' prefix", p.stderr)
        self.assertFalse(self.orb_root.exists() and (self.orb_root / "org-base").exists(), "nothing was created")
        # A template that lacks a tool is named, and nothing is built on it.
        (self.bindir / "socat").unlink()
        if shutil.which("socat", path=self.env["PATH"]):
            self.skipTest("this computer has socat, so the template cannot be made to lack it")
        p = self.org("up", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("org-base lacks: socat", p.stderr)
        self.assertFalse((self.orb_root / "org-manager").exists())
        self.assertFalse((self.host_dir / "agent.pid").exists())

    def test_the_wrapper_runs_in_place_without_an_installation(self):
        # No agent.json and no manager container: the package runs here, on this ledger.
        p = self.org("init", ORG_HOME=str(self.tmp / "ledger"), PATH=str(self.bindir) + os.pathsep + "/usr/bin:/bin")
        self.assertIn("created ledger at %s" % (self.tmp / "ledger"), p.stdout)
        self.assertEqual(self.org("up", "--backend", "cloud", ok=False).returncode, 2)


if __name__ == "__main__":
    unittest.main()
