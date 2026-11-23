"""Engineers as containers: dispatch through a (simulated) Docker client, the pool, and the queue."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from org import dispatch, spec, status, sync  # noqa: E402
from org.ledger import Ledger  # noqa: E402
from org.transport import Docker  # noqa: E402
from org.util import read_json, write_json  # noqa: E402
from test_manager_side import GOOD, TURNS  # noqa: E402  (which also keeps the real tokens out of every test)


class DockerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="org-docker-"))
        self.addCleanup(self._cleanup)
        self.old_env = dict(os.environ)
        home = self.tmp / "manager-home"
        home.mkdir()
        (home / ".gitconfig").write_text("[user]\n\tname = Ada Example\n\temail = ada@example.test\n")
        # The two tokens this side was handed, in a directory of the test's own (never the real /run/org).
        self.secrets = self.tmp / "run-org"
        self.secrets.mkdir(mode=0o700)
        (self.secrets / "claude-token").write_text("sk-ant-oat01-not-a-real-token\n")
        (self.secrets / "github-token").write_text("github_pat_not_a_real_token\n")
        bindir = self.tmp / "bin"
        bindir.mkdir()
        # The simulated incus is here so that nothing can reach a real one; these tests never call it.
        for name, target in (("docker", "fake_docker.py"), ("claude", "fake_claude.py"), ("incus", "fake_incus.py")):
            os.symlink(ROOT / "tests" / target, bindir / name)
        self.scen = self.tmp / "scen"
        self.scen.mkdir()
        os.environ.update(HOME=str(home), PATH=str(bindir) + os.pathsep + self.old_env["PATH"], ORG_SECRETS_DIR=str(self.secrets),
                          FAKE_DOCKER_ROOT=str(self.tmp / "docker"), FAKE_INCUS_ROOT=str(self.tmp / "incus"),
                          FAKE_CLAUDE_SCENARIO=str(self.scen / "scenario.json"))
        os.environ.pop("ORG_HOME", None)
        self.scenario(TURNS)
        self.ledger = Ledger(self.tmp / "ledger")
        self.ledger.init()
        self.configure(slots=2)

    def _cleanup(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        # Killed outright: a runner asked politely writes its last files, into a directory that is being removed.
        subprocess.run(["pkill", "-9", "-f", str(self.tmp)], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def configure(self, slots=2, nodes=None):
        nodes = nodes or {"local": {"host": None, "slots": slots}}
        # The built-in Incus backend has a pool of its own by default; here the only pool is Docker's.
        write_json(self.ledger.config_path, {"machines": {"plugins": ["org.backends.docker"], "default": "docker", "detach": "nohup",
                                                          "docker": {"nodes": nodes}, "incus": {"slots": None}},
                                             "engineer": {"retry_backoff_s": 0.05, "max_turn_retries": 1}})

    def scenario(self, turns):
        (self.scen / "scenario.json").write_text(json.dumps({"turns": turns}))

    def approved(self, title="CSV export"):
        spec_id = spec.new_draft(self.ledger, title, "acme")
        self.ledger.draft_path(spec_id).write_text(GOOD.replace("CSV export", title))
        spec.approve(self.ledger, spec_id, spec.draft_sha(self.ledger, spec_id)[:10])
        return spec_id

    def wait_final(self, run_id, seconds=40):
        end = time.time() + seconds
        while time.time() < end:
            rec = sync.sync_run(self.ledger, run_id)
            if rec.get("final"):
                return rec
            time.sleep(0.3)
        self.fail("run %s did not finish: %s" % (run_id, self.ledger.run(run_id)))

    def calls(self):
        return [json.loads(line) for line in (self.tmp / "docker" / "calls.log").read_text().splitlines()]

    def box(self, run_id):
        return self.tmp / "docker" / ("org-eng-" + run_id)

    # ------------------------------------------------------------ one engineer in a container
    def test_a_run_gets_a_container_and_everything_it_needs(self):
        log = []
        run_id, rec = dispatch.dispatch(self.ledger, self.approved(), log=log.append)
        machine = rec["machine"]
        self.assertEqual((machine["kind"], machine["node"], machine["name"]), ("docker", "local", "org-eng-001-csv-export-r1"))
        self.assertEqual(rec["state"], "dispatched")
        created = read_json(self.box(run_id) / "container.json")
        self.assertEqual(created["image"], "org:latest")
        self.assertEqual(created["cmd"], ["sleep", "infinity"], "the container idles; the runner is started in it like any machine")
        self.assertEqual(created["opts"]["--network"], ["org-engineers"])
        self.assertIn("org.run=" + run_id, created["opts"]["--label"])
        self.assertNotIn("-v", created["opts"], "an engineer is given no mounts: no socket, no host files")
        self.assertNotIn("-e", created["opts"], "and no secrets in its environment, where `docker inspect` would show them")
        network = (self.tmp / "docker" / "network-org-engineers").read_text()
        self.assertIn("enable_icc=false", network, "engineers cannot reach each other")
        home = self.box(run_id) / "home"
        # A new container is blank, so it is handed all three logins, each over stdin: the two
        # tokens into its own tokens directory (its /run/org), and the git identity.
        for name, value in (("claude-token", "sk-ant-oat01-not-a-real-token"), ("github-token", "github_pat_not_a_real_token")):
            self.assertEqual((home / ".run-org" / name).read_text().strip(), value)
            self.assertEqual((home / ".run-org" / name).stat().st_mode & 0o077, 0)
        self.assertEqual((home / ".run-org").stat().st_mode & 0o077, 0)
        self.assertEqual(sorted(p.name for p in self.secrets.iterdir()), ["claude-token", "github-token"], "this side's are as they were")
        self.assertIn("gh auth git-credential", (home / ".gitconfig").read_text(), "git asks gh, which reads the token the runner gives it")
        self.assertIn("Ada Example", (home / ".gitconfig").read_text())
        self.assertIn("ada@example.test", (home / ".gitconfig").read_text())
        self.assertIn("GitHub token copied in", log)
        for call in self.calls():
            self.assertNotIn("github_pat_", json.dumps(call), "no token on a command line")
            self.assertNotIn("sk-ant-oat01", json.dumps(call))
        self.assertTrue((home / "org-runs" / run_id / ".org" / "org" / "runner.py").exists())

        self.wait_final(run_id)
        view = status.collect(self.ledger)["runs"][0]
        self.assertEqual((view["state"], view["machine"]["kind"], view["machine"]["node"]), ("done", "docker", "local"))
        self.assertEqual(dispatch.pool(self.ledger), [{"kind": "docker", "node": "local", "host": None, "slots": 2, "used": 0}], "a finished run frees its slot")
        self.assertIn("deleted", dispatch.remove_machine(self.ledger, run_id))
        self.assertFalse(self.box(run_id).exists())
        self.assertTrue((self.ledger.mirror_dir(run_id) / "report.md").exists(), "the record outlives the container")

    def test_without_logins_dispatch_still_works_and_says_so(self):
        (self.secrets / "github-token").unlink()
        (self.secrets / "claude-token").unlink()
        (Path(os.environ["HOME"]) / ".gitconfig").unlink()
        log = []
        run_id, _ = dispatch.dispatch(self.ledger, self.approved(), log=log.append)
        self.assertTrue(any("no Claude token" in line for line in log))
        self.assertTrue(any("no GitHub token" in line for line in log))
        self.assertTrue(any("no git identity" in line for line in log))
        self.assertFalse((self.box(run_id) / "home" / ".run-org").exists(), "nothing was handed over")
        self.wait_final(run_id)

    # ------------------------------------------------------------ the pool and the queue
    def test_a_full_pool_queues_and_a_freed_slot_is_taken(self):
        self.configure(slots=1)
        slow = dict(TURNS, **{"milestone:M1": dict(TURNS["milestone:M1"], sleep=1.5)})
        self.scenario(slow)
        first, rec1 = dispatch.dispatch(self.ledger, self.approved("CSV export"), log=lambda m: None)
        log = []
        second, rec2 = dispatch.dispatch(self.ledger, self.approved("JSON export"), log=log.append)
        self.assertEqual(rec1["state"], "dispatched")
        self.assertEqual(rec2["state"], "queued")
        self.assertIn("no free slot on any node (local 1/1)", rec2["queue_reason"])
        self.assertFalse(self.box(second).exists(), "a queued run has no container")
        self.assertEqual(dispatch.pump(self.ledger), [], "nothing can start while the slot is held")
        state = status.collect(self.ledger)
        queued = [v for v in state["runs"] if v["run_id"] == second][0]
        self.assertEqual((queued["state"], queued["terminal"], queued["health"]), ("queued", False, "ok"))
        self.assertIn("no free slot", queued["detail"])
        text = status.render(state, width=220)
        self.assertIn("Pool: local 1/1 · 1 queued", text)
        self.assertIn("queued", text)

        self.wait_final(first)
        # looking at status is enough to move the queue: no scheduler process is needed
        status.collect(self.ledger, do_sync=True)
        self.assertEqual(self.ledger.run(second)["state"], "dispatched")
        self.assertTrue(self.box(second).exists())
        self.wait_final(second)
        self.assertEqual({v["state"] for v in status.collect(self.ledger)["runs"]}, {"done"})

    def test_runs_spread_over_nodes_and_a_named_node_is_respected(self):
        self.configure(nodes={"local": {"host": None, "slots": 1}, "bigbox": {"host": "ssh://me@bigbox", "slots": 2, "cpus": 8}})
        self.scenario(dict(TURNS, **{"milestone:M1": dict(TURNS["milestone:M1"], sleep=30)}))
        placed = []
        for title in ("One", "Two", "Three"):
            _, rec = dispatch.dispatch(self.ledger, self.approved(title), log=lambda m: None)
            placed.append(rec["machine"]["node"])
        self.assertEqual(sorted(placed), ["bigbox", "bigbox", "local"], "least loaded first, until every slot is used")
        _, rec = dispatch.dispatch(self.ledger, self.approved("Four"), log=lambda m: None)
        self.assertEqual(rec["state"], "queued")
        # a spec may ask for a node; it waits for that node even when another has room
        self.configure(nodes={"local": {"host": None, "slots": 5}, "bigbox": {"host": "ssh://me@bigbox", "slots": 2}})
        _, rec = dispatch.dispatch(self.ledger, self.approved("Five"), machine_override="docker:bigbox", log=lambda m: None)
        self.assertEqual((rec["state"], rec["machine"]["wanted_node"]), ("queued", "bigbox"))
        self.assertIn("no free slot on bigbox", rec["queue_reason"])
        with self.assertRaises(dispatch.DispatchError):
            dispatch.dispatch(self.ledger, self.approved("Six"), machine_override="docker:nowhere", log=lambda m: None)
        remote = [c for c in self.calls() if c["host"] == "ssh://me@bigbox" and c["argv"][0] == "run"]
        self.assertEqual(len(remote), 2, "containers on a remote node are created through that node's daemon")
        boxes = [read_json(p) for p in (self.tmp / "docker").glob("org-eng-*/container.json")]
        self.assertEqual([b["opts"].get("--cpus") for b in boxes if b["host"] == "ssh://me@bigbox"], [["8"], ["8"]],
                         "a node's limits apply to the containers on it")
        self.assertEqual([b["opts"].get("--cpus") for b in boxes if not b["host"]], [None])
        self.assertEqual(Docker("c", "ssh://me@bigbox").argv("true")[:3], ["docker", "-H", "ssh://me@bigbox"])

    def test_a_queued_run_can_be_stopped(self):
        self.configure(slots=1)
        self.scenario(dict(TURNS, **{"milestone:M1": dict(TURNS["milestone:M1"], sleep=30)}))
        dispatch.dispatch(self.ledger, self.approved("One"), log=lambda m: None)
        second, _ = dispatch.dispatch(self.ledger, self.approved("Two"), log=lambda m: None)
        self.assertEqual(dispatch.stop(self.ledger, second), "removed from the queue")
        view = [v for v in status.collect(self.ledger)["runs"] if v["run_id"] == second][0]
        self.assertEqual((view["state"], view["terminal"]), ("stopped", True))
        self.assertEqual(dispatch.pump(self.ledger), [])

    def test_a_failed_start_frees_the_slot(self):
        self.configure(slots=1)
        os.environ["FAKE_DOCKER_IMAGE"] = "something-else:latest"
        with self.assertRaises(dispatch.DispatchError) as ctx:
            dispatch.dispatch(self.ledger, self.approved("One"), log=lambda m: None)
        self.assertIn("Unable to find image", str(ctx.exception))
        self.assertEqual(dispatch.pool(self.ledger)[0]["used"], 0)
        os.environ.pop("FAKE_DOCKER_IMAGE")
        run_id, rec = dispatch.dispatch(self.ledger, self.approved("Two"), log=lambda m: None)
        self.assertEqual(rec["state"], "dispatched")
        self.wait_final(run_id)

    # ------------------------------------------------------------ the manager and an engineer's files
    def test_looking_at_and_acting_on_an_engineers_machine(self):
        def org(*args, ok=True):
            p = subprocess.run([sys.executable, "-m", "org", "--home", str(self.ledger.root)] + list(args),
                               capture_output=True, text=True, cwd=str(ROOT), timeout=60)
            if ok:
                self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
            return p

        run_id, _ = dispatch.dispatch(self.ledger, self.approved(), log=lambda m: None)
        self.wait_final(run_id)
        work = self.box(run_id) / "home" / "work" / run_id
        (work / "notes.txt").write_text("the engineer left this\n")
        self.assertIn("notes.txt", org("ls", "1").stdout)
        self.assertEqual(org("cat", "1", "notes.txt").stdout, "the engineer left this\n")
        self.assertIn("status.json", org("exec", "1", "--", "ls ~/org-runs/%s" % run_id).stdout)
        self.assertIn("no repositories in the workspace yet", org("diff", "1").stdout)
        # a path is data, not shell: it cannot smuggle a second command
        evil = org("cat", "1", "notes.txt; touch pwned", ok=False)
        self.assertNotEqual(evil.returncode, 0)
        self.assertFalse((work / "pwned").exists())
        # exec is the unrestricted one
        self.assertEqual(org("exec", "1", "--", "echo", "hello there").stdout, "hello there\n")
        org("exec", "1", "--", "echo changed > notes.txt")
        self.assertEqual((work / "notes.txt").read_text(), "changed\n")
        self.assertEqual(org("exec", "1", "--", "exit 7", ok=False).returncode, 7)
        self.assertIn("local", org("pool").stdout)
        self.assertIn("0 of 2", org("pool").stdout)
        org("machine", "rm", "1")
        gone = org("ls", "1", ok=False)
        self.assertIn("no machine to reach", gone.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
