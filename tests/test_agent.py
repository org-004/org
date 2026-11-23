"""A manager inside a machine: everything it does to engineers' machines goes through the host agent.

The Mac is this test process, running the agent against a stand-in `orb`. The manager's machine is
a directory with its own home, in which the agent starts the real bridge. The code under test is
all real; only OrbStack is simulated.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from org import agent, backends, dispatch, doctor, spec, status, sync  # noqa: E402
from org.backends.orb import OrbBackend  # noqa: E402
from org.ledger import Ledger  # noqa: E402
from org.util import write_json  # noqa: E402
from test_manager_side import GOOD, TURNS  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AgentCase(unittest.TestCase):
    dash_port = None

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="org-agent-"))
        self.addCleanup(self._cleanup)
        self.old_env = dict(os.environ)
        self.orb_root = self.tmp / "orb"
        bindir = self.tmp / "bin"
        bindir.mkdir()
        os.symlink(ROOT / "tests" / "fake_orb.py", bindir / "orb")
        os.symlink(ROOT / "tests" / "fake_claude.py", bindir / "claude")
        self.scen = self.tmp / "scen"
        self.scen.mkdir()
        (self.scen / "scenario.json").write_text(json.dumps({"turns": TURNS}))
        # The template: what every engineer starts as. The manager is a clone of it, with org installed.
        template = self.orb_root / "org-base" / "home"
        (template / ".claude").mkdir(parents=True)
        self.manager_home = self.orb_root / "org-manager" / "home"
        (self.manager_home / ".claude").mkdir(parents=True)
        (self.manager_home / ".claude" / "oauth-token").write_text("sk-ant-oat01-not-a-real-token\n")
        (self.manager_home / ".local" / "bin").mkdir(parents=True)
        os.symlink(ROOT / "bin" / "org", self.manager_home / ".local" / "bin" / "org")
        # This process plays both sides, so it carries the manager's home; the agent's own files are elsewhere.
        os.environ.update(HOME=str(self.manager_home), PATH=str(bindir) + os.pathsep + self.old_env["PATH"],
                          FAKE_ORB_ROOT=str(self.orb_root), FAKE_CLAUDE_SCENARIO=str(self.scen / "scenario.json"))
        os.environ.pop("ORG_HOME", None)
        self.ledger = Ledger(self.manager_home / ".org")
        self.ledger.init()
        self.configure()
        self.log = []
        self.agent = None
        self.start_agent()

    def configure(self, **orb):
        settings = dict({"via": "agent", "base": "org-base", "prefix": "org-eng-"}, **orb)
        write_json(self.ledger.config_path, {"machines": {"default": "orb", "detach": "nohup", "orb": settings},
                                             "engineer": {"retry_backoff_s": 0.05, "max_turn_retries": 1}})

    def start_agent(self, **over):
        cfg = dict({"orb": "orb", "manager": "org-manager", "template": "org-base", "prefix": "org-eng-",
                    "dash_port": self.dash_port}, **over)
        self.agent = agent.Agent(cfg, log=self.log.append)
        self.thread = threading.Thread(target=self.agent.run, daemon=True)
        self.thread.start()
        end = time.time() + 20
        while time.time() < end and not (self.agent.connected and agent.socket_path(self.ledger.root).exists()):
            time.sleep(0.05)
        self.assertTrue(self.agent.connected, "the agent did not connect: %s" % self.log)
        agent.Client._down.clear()

    def stop_agent(self):
        if self.agent:
            self.agent.stop()
            self.thread.join(15)
            self.agent = None

    def _cleanup(self):
        self.stop_agent()
        os.environ.clear()
        os.environ.update(self.old_env)
        subprocess.run(["pkill", "-f", str(self.tmp)], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self):
        return agent.Client(agent.socket_path(self.ledger.root))

    def approved(self, title="CSV export", text=GOOD):
        spec_id = spec.new_draft(self.ledger, title, "acme")
        self.ledger.draft_path(spec_id).write_text(text)
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

    def orb_calls(self):
        return [json.loads(line) for line in (self.orb_root / "calls.jsonl").read_text().splitlines()]


class ThroughTheAgent(AgentCase):
    def test_a_run_is_created_driven_and_removed_through_the_agent(self):
        log = []
        run_id, rec = dispatch.dispatch(self.ledger, self.approved(), log=log.append)
        machine = rec["machine"]
        self.assertEqual((machine["kind"], machine["via"], machine["name"], machine["base"]),
                         ("orb", "agent", "org-eng-001-csv-export-r1", "org-base"))
        self.assertEqual(rec["state"], "dispatched", log)
        home = self.orb_root / machine["name"] / "home"
        self.assertTrue((home / ".claude" / "oauth-token").read_text().startswith("sk-ant-oat01"), "the token is copied in")
        final = self.wait_final(run_id)
        self.assertEqual(final["state"], "done")
        self.assertIn("Delivered", (self.ledger.mirror_dir(run_id) / "report.md").read_text())
        # Every orb command came from the agent's side, and none named a machine outside its prefix.
        named = [c for c in self.orb_calls() if c[0] in ("clone", "delete", "-m")]
        self.assertIn(["clone", "org-base", machine["name"]], named)
        touched = {c[2] if c[0] == "clone" else c[1] for c in named}
        self.assertEqual(touched - {"org-manager"}, {machine["name"]})
        self.assertIn("deleted", dispatch.remove_machine(self.ledger, run_id))
        self.assertFalse((self.orb_root / machine["name"]).exists())

    def test_the_agent_acts_only_on_engineers_machines(self):
        client = self.client()
        before = len(self.orb_calls())
        for name in ("org-manager", "org-base", "base", "thread2", "org-eng-", "org-eng-../org-manager", "org-eng-A", "org-eng-x y",
                     "-m", "", None, "org-eng-" + "x" * 80):
            for op in ("exec", "delete", "create"):
                with self.assertRaises(agent.AgentError, msg="%s %r" % (op, name)) as ctx:
                    client.call({"op": op, "name": name, "script": "true"})
                self.assertIn("only acts on machines named org-eng-", str(ctx.exception))
        self.assertEqual(len(self.orb_calls()), before, "a refused request never reaches orb")
        self.assertTrue(any("refused" in line for line in self.log))
        with self.assertRaises(agent.AgentError):
            client.call({"op": "shell", "name": "org-eng-x"})
        self.assertEqual(client.info(), {"ok": True, "manager": "org-manager", "template": "org-base", "prefix": "org-eng-"})

    def test_the_agent_will_not_take_a_prefix_that_covers_the_manager_or_the_template(self):
        for bad in ({"prefix": "org-"}, {"prefix": "org-eng"}, {"prefix": ""}, {"manager": "org-eng-manager"}, {"template": "../x"}):
            with self.assertRaises(agent.AgentError, msg=bad):
                agent.check_config(dict(agent.DEFAULTS, **bad))

    def test_exec_carries_input_output_and_exit_status(self):
        client = self.client()
        client.create("org-eng-probe")
        payload = bytes(range(256)) * 400
        code, out, err, timed_out = client.exec("org-eng-probe", "cat; echo oops >&2; exit 3", stdin=payload)
        self.assertEqual((code, out, err.strip(), timed_out), (3, payload, b"oops", False))
        self.assertEqual(client.machines(), ["org-eng-probe"])
        tr = agent.AgentTransport(client, "org-eng-probe")
        self.assertEqual(tr.run('echo "$HOME"').stdout.decode().strip(), str(self.orb_root / "org-eng-probe" / "home"))
        with self.assertRaises(dispatch.TransportError) as ctx:
            tr.run("sleep 30", timeout=1)
        self.assertIn("no answer within 1s", str(ctx.exception))
        with self.assertRaises(dispatch.TransportError) as ctx:
            tr.run("echo broken >&2; exit 7")
        self.assertIn("exit 7: broken", str(ctx.exception))

    def test_several_requests_at_once(self):
        client = self.client()
        client.create("org-eng-many")
        results, errors = {}, []

        def one(i):
            try:
                results[i] = client.exec("org-eng-many", "sleep 0.%d; echo %d" % (i % 5, i))[1].strip()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=one, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(results, {i: str(i).encode() for i in range(12)}, "each caller gets its own answer")

    def test_without_the_agent_the_manager_says_so_and_recovers_when_it_returns(self):
        self.stop_agent()
        self.assertFalse(agent.socket_path(self.ledger.root).exists(), "the bridge takes its socket with it")
        spec_id = self.approved()
        began = time.time()
        with self.assertRaises(dispatch.DispatchError) as ctx:
            dispatch.dispatch(self.ledger, spec_id, log=lambda m: None)
        self.assertIn("host agent is not connected", str(ctx.exception))
        self.assertIn("org up", str(ctx.exception))
        self.assertEqual(self.ledger.run(spec_id + "-r1")["state"], "dispatch_failed")
        with self.assertRaises(agent.AgentError):
            self.client().info()
        self.assertLess(time.time() - began, 6, "a second caller does not wait again")
        rows = {r[1]: r for r in doctor.checks(self.ledger)}
        self.assertEqual(rows["host agent"][0], "fail")
        self.assertIn("org up", rows["host agent"][3])
        self.start_agent()
        run_id, rec = dispatch.dispatch(self.ledger, spec_id, log=lambda m: None)
        self.assertEqual((run_id, rec["state"]), (spec_id + "-r2", "dispatched"))
        self.assertEqual({r[1]: r for r in doctor.checks(self.ledger)}["host agent"][0], "ok")

    def test_the_bridge_reconnects_when_its_command_dies(self):
        subprocess.run(["pkill", "-f", "org agent bridge"], capture_output=True)
        end = time.time() + 20
        while time.time() < end and not any("trying again" in line for line in self.log):
            time.sleep(0.05)
        self.assertTrue(any("trying again" in line for line in self.log), self.log)
        end = time.time() + 20
        while time.time() < end:
            try:
                agent.Client._down.clear()
                self.assertEqual(self.client().info()["prefix"], "org-eng-")
                return
            except agent.AgentError:
                time.sleep(0.2)
        self.fail("the agent did not come back: %s" % self.log)

    def test_a_second_bridge_does_not_take_over(self):
        p = subprocess.run([str(self.manager_home / ".local" / "bin" / "org"), "agent", "bridge"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=30)
        self.assertEqual(p.returncode, 3)
        self.assertIn("another bridge", p.stderr)
        self.assertEqual(self.client().info()["prefix"], "org-eng-", "the first is undisturbed")

    def test_slots_bound_how_many_engineers_run_at_once(self):
        self.configure(slots=1)
        self.assertEqual(dispatch.pool(self.ledger), [{"kind": "orb", "node": "mac", "host": None, "slots": 1, "used": 0}])
        first, rec1 = dispatch.dispatch(self.ledger, self.approved("First"), log=lambda m: None)
        log = []
        second, rec2 = dispatch.dispatch(self.ledger, self.approved("Second"), log=log.append)
        self.assertEqual((rec1["state"], rec2["state"]), ("dispatched", "queued"))
        self.assertIn("no free slot", rec2["queue_reason"])
        self.assertIn("mac 1/1", status.render(status.collect(self.ledger, do_sync=False)))
        self.assertFalse((self.orb_root / rec2["machine"]["name"]).exists(), "a queued run has no machine yet")
        self.wait_final(first)
        self.assertEqual(dispatch.pump(self.ledger), [second])
        self.assertEqual(self.wait_final(second)["state"], "done")

    def test_a_machine_that_can_see_the_mac_is_refused(self):
        cfg = self.ledger.config()
        machine = {"kind": "orb", "name": "org-eng-x", "base": "org-base"}
        OrbBackend().vet(machine, {"mac_files": "no"}, cfg)
        with self.assertRaises(backends.BackendError) as ctx:
            OrbBackend().vet(machine, {"mac_files": "yes"}, cfg)
        self.assertIn("not an isolated machine", str(ctx.exception))
        self.configure(allow_shared=True)
        OrbBackend().vet(machine, {"mac_files": "yes"}, self.ledger.config())
        run_id, rec = dispatch.dispatch(self.ledger, self.approved(), log=lambda m: None)
        self.assertEqual(rec["facts"]["mac_files"], "no", "the check runs on every machine before it is used")
        self.assertIn(rec["facts"]["docker"], ("yes", "no"))


class Names(unittest.TestCase):
    def test_a_long_name_keeps_its_run_number(self):
        name = backends.machine_name("org-eng-", "012-" + "a-very-long-title-" * 6 + "end-r12")
        self.assertLessEqual(len(name), 60)
        self.assertTrue(name.startswith("org-eng-012-a-very-long-title"))
        self.assertTrue(name.endswith("-r12"))
        self.assertTrue(agent.Agent(dict(agent.DEFAULTS)).allowed(name))
        self.assertEqual(backends.machine_name("eng-", "001-CSV_export-r1"), "eng-001-csv-export-r1")

    def test_what_a_spec_may_ask_for(self):
        tmp = Path(tempfile.mkdtemp(prefix="org-kinds-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        ledger = Ledger(tmp)
        ledger.init()
        cases = {
            "machine: docker": "The Docker backend is a plugin",
            "machine: mainframe": "no backend for machine kind 'mainframe'",
            "machine: orb:big": "takes no name",
            "machine: ssh:gpu": "is not defined in config.json",
            "machine: local": "the local machine is disabled",
        }
        for header, expected in cases.items():
            errors, _ = spec.check_text(ledger, GOOD.replace("delivery: pr", "delivery: pr\n" + header))
            self.assertTrue(any(expected in e for e in errors), "%s: %r not in %r" % (header, expected, errors))
        self.assertEqual(spec.check_text(ledger, GOOD)[0], [])
        write_json(ledger.config_path, {"machines": {"plugins": ["org.backends.docker"], "ssh": {"gpu": {"host": "me@gpu"}}}})
        for header in ("machine: docker", "machine: docker:local", "machine: ssh:gpu", "machine: orb"):
            self.assertEqual(spec.check_text(ledger, GOOD.replace("delivery: pr", "delivery: pr\n" + header))[0], [], header)
        write_json(ledger.config_path, {"machines": {"plugins": ["org.backends.nothing_here"]}})
        with self.assertRaises(backends.BackendError):
            backends.load(ledger.config())


class Dashboard(AgentCase):
    dash_port = free_port()

    def test_the_bridge_serves_the_dashboard_and_the_agent_finds_it(self):
        end = time.time() + 15
        while time.time() < end and not any(line.startswith("dashboard:") for line in self.log):
            time.sleep(0.05)
        # Here the "machine" shares this host's loopback, which is what OrbStack's forwarding looks like from the Mac.
        self.assertIn("dashboard: http://127.0.0.1:%d (OrbStack forwards the port)" % self.dash_port, self.log)
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open("http://127.0.0.1:%d/" % self.dash_port, timeout=10) as r:
            self.assertIn(b"<title>org</title>", r.read())

    def test_the_agent_can_carry_the_port_itself(self):
        # Where nothing forwards the port, a browser's connection is carried through `orb -m` instead.
        front = socket.socket()
        front.bind(("127.0.0.1", 0))
        front.listen(4)
        self.addCleanup(front.close)

        def accept():
            for _ in range(2):
                conn, _ = front.accept()
                threading.Thread(target=self.agent._relay, args=(conn, self.dash_port), daemon=True).start()

        threading.Thread(target=accept, daemon=True).start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = "http://127.0.0.1:%d" % front.getsockname()[1]
        with opener.open(url + "/", timeout=20) as r:
            page = r.read()
        self.assertIn(b"<title>org</title>", page)
        self.assertGreater(len(page), 20000, "the whole page arrives, not its first chunk")
        self.assertIn("—".encode(), page, "bytes outside ASCII survive the trip")
        # The API still wants the dashboard's token, and still refuses a Host that is not this machine.
        req = urllib.request.Request(url + "/api/state", headers={"Host": "127.0.0.1:%d" % self.dash_port})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            opener.open(req, timeout=20)
        self.assertEqual(ctx.exception.code, 401)
        ctx.exception.close()
        self.assertTrue(any(c[:2] == ["-m", "org-manager"] and "agent pipe" in c[-1] for c in self.orb_calls()))


if __name__ == "__main__":
    unittest.main()
