"""Engineers in Incus containers, the default backend, against a simulated `incus`.

The code under test is all real: the backend, the transport, dispatch, the queue. Only Incus is
simulated (tests/fake_incus.py): each container is a directory with a home and a tokens directory
of its own, and what `incus query` says about it is what the test wrote.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from org import backends, dispatch, spec, status, sync  # noqa: E402
from org.backends import BackendError  # noqa: E402
from org.backends.incus import IncusBackend, problems  # noqa: E402
from org.ledger import Ledger  # noqa: E402
from org.transport import Incus, TransportError  # noqa: E402
from org.util import read_json, write_json  # noqa: E402
from test_manager_side import GOOD, TURNS  # noqa: E402  (which also keeps the real tokens out of every test)

CLAUDE_TOKEN = "sk-ant-oat01-not-a-real-token"
GITHUB_TOKEN = "github_pat_not_a_real_token"
TEMPLATE = "org-base"


class IncusCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="org-incus-"))
        self.addCleanup(self._cleanup)
        self.old_env = dict(os.environ)
        home = self.tmp / "manager-home"
        home.mkdir()
        (home / ".gitconfig").write_text("[user]\n\tname = Ada Example\n\temail = ada@example.test\n")
        # The two tokens this side was handed, in a directory of the test's own: never the real /run/org.
        self.secrets = self.tmp / "run-org"
        self.secrets.mkdir(mode=0o700)
        (self.secrets / "claude-token").write_text(CLAUDE_TOKEN + "\n")
        (self.secrets / "github-token").write_text(GITHUB_TOKEN + "\n")
        self.bindir = self.tmp / "bin"
        self.bindir.mkdir()
        os.symlink(ROOT / "tests" / "fake_incus.py", self.bindir / "incus")
        os.symlink(ROOT / "tests" / "fake_claude.py", self.bindir / "claude")
        self.scen = self.tmp / "scen"
        self.scen.mkdir()
        self.root = self.tmp / "incus"
        os.environ.update(HOME=str(home), PATH=str(self.bindir) + os.pathsep + self.old_env["PATH"], ORG_SECRETS_DIR=str(self.secrets),
                          FAKE_INCUS_ROOT=str(self.root), FAKE_CLAUDE_SCENARIO=str(self.scen / "scenario.json"))
        for name in ("ORG_HOME", "FAKE_INCUS_BASE"):
            os.environ.pop(name, None)
        # The template: what every engineer starts as. Stopped, isolated, and holding no login.
        self.container(TEMPLATE)
        (self.box(TEMPLATE) / "home" / "from-the-template").write_text("installed once\n")
        self.scenario(TURNS)
        self.ledger = Ledger(self.tmp / "ledger")
        self.ledger.init()
        self.configure()

    def _cleanup(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        # Killed outright: a runner asked politely writes its last files, into a directory that is being removed.
        subprocess.run(["pkill", "-9", "-f", str(self.tmp)], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def configure(self, **incus):
        """The ledger's own defaults (org-base, org-eng-, four at once), with what a test changes."""
        write_json(self.ledger.config_path, {"machines": {"detach": "nohup", "incus": incus},
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

    def wait_executing(self, run_id, seconds=20):
        end, st = time.time() + seconds, {}
        while time.time() < end and st.get("state") != "executing":
            sync.sync_run(self.ledger, run_id)
            st = read_json(self.ledger.mirror_dir(run_id) / "status.json") or {}
            time.sleep(0.3)
        self.assertEqual(st.get("state"), "executing")
        return st

    # What the simulated Incus holds. These write its files, so they leave no call behind them.
    def box(self, name):
        return self.root / name

    def container(self, name, running=False, **description):
        """Make a container by hand. `description` is laid over what `incus query` says of an isolated one."""
        (self.box(name) / "home").mkdir(parents=True)
        self.state(name, "running" if running else "stopped")
        if description:
            self.describe(name, **description)

    def describe(self, name, **description):
        (self.box(name) / "override.json").write_text(json.dumps(description))

    def state(self, name, value=None):
        path = self.box(name) / "state"
        if value is None:
            return path.read_text().strip()
        path.write_text(value + "\n")

    def calls(self):
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def incus(self, *args):
        return subprocess.run([str(self.bindir / "incus")] + list(args), capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)


class ARun(IncusCase):
    def test_a_run_gets_a_copy_of_the_template_and_both_tokens(self):
        cfg = self.ledger.config()
        self.assertEqual((cfg["machines"]["default"], cfg["machines"]["incus"]),
                         ("incus", {"base": TEMPLATE, "prefix": "org-eng-", "push_token": True, "slots": 4}))
        log = []
        run_id, rec = dispatch.dispatch(self.ledger, self.approved(), log=log.append)
        machine, name = rec["machine"], "org-eng-001-csv-export-r1"
        self.assertEqual((run_id, machine["kind"], machine["name"], machine["base"], machine["node"]),
                         ("001-csv-export-r1", "incus", name, TEMPLATE, "vm"))
        self.assertEqual(rec["state"], "dispatched", log)
        self.assertEqual(rec["provisioner"]["pid"], os.getpid(), "who was setting it up is on record, for `reap`")
        calls = self.calls()
        self.assertEqual([c for c in calls if c[0] in ("copy", "init", "launch", "start", "stop", "delete")],
                         [["copy", "-q", TEMPLATE, name], ["start", "-q", name]])
        self.assertEqual(self.state(name), "running")
        home = self.box(name) / "home"
        self.assertEqual((home / "from-the-template").read_text(), "installed once\n", "the container is a copy of the template")

        # Both tokens, in the container's own tokens directory (its /run/org), readable by its user alone.
        for filename, token in (("claude-token", CLAUDE_TOKEN), ("github-token", GITHUB_TOKEN)):
            path = home / ".run-org" / filename
            self.assertEqual(path.read_text(), token + "\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, filename)
        self.assertEqual(stat.S_IMODE((home / ".run-org").stat().st_mode), 0o700)
        self.assertIn("Claude token copied in", log)
        self.assertIn("GitHub token copied in", log)
        # They went in over stdin: nothing that was said to incus holds either.
        said = (self.root / "calls.jsonl").read_text() + (self.root / "calls.log").read_text()
        for secret in (CLAUDE_TOKEN, GITHUB_TOKEN, "sk-ant-", "github_pat_"):
            self.assertNotIn(secret, said, "a token on a command line")
        # Ours are as they were, and the template has none: a login is never baked in.
        self.assertEqual({p.name: p.read_text() for p in self.secrets.iterdir()},
                         {"claude-token": CLAUDE_TOKEN + "\n", "github-token": GITHUB_TOKEN + "\n"})
        self.assertFalse((self.box(TEMPLATE) / "home" / ".run-org").exists())
        self.assertEqual(self.state(TEMPLATE), "stopped", "the template is only ever copied")
        # A blank container is also told who commits, and that git gets its password from gh.
        gitconfig = (home / ".gitconfig").read_text()
        self.assertIn("Ada Example", gitconfig)
        self.assertIn("ada@example.test", gitconfig)
        self.assertIn("gh auth git-credential", gitconfig)

        # Everything done in the container was `incus exec <its name> -T -- bash -c <script>`,
        # and nothing but it and the template was ever named.
        named = set()
        for c in calls:
            if c[0] == "exec":
                self.assertEqual(c[1:6], [name, "-T", "--", "bash", "-c"])
                self.assertEqual(len(c), 7)
            named.add(c[-1].strip("^$") if c[0] == "list" else c[1].rsplit("/", 1)[-1] if c[0] == "query" else c[1] if c[0] == "exec" else c[-1])
            if c[0] == "copy":
                named.add(c[-2])
        self.assertEqual(named, {TEMPLATE, name})

        self.assertTrue((home / "org-runs" / run_id / ".org" / "org" / "runner.py").exists())
        self.assertEqual(dispatch.pool(self.ledger), [{"kind": "incus", "node": "vm", "host": None, "slots": 4, "used": 1}])
        self.assertEqual(self.wait_final(run_id)["state"], "done")
        self.assertIn("Delivered", (self.ledger.mirror_dir(run_id) / "report.md").read_text())
        self.assertTrue((home / "work" / run_id).is_dir(), "the workspace is in the container's home, not ours")
        self.assertEqual(dispatch.pool(self.ledger)[0]["used"], 0, "a finished run frees its slot")

    def test_slots_bound_how_many_engineers_run_at_once(self):
        self.configure(slots=1)
        self.assertEqual(dispatch.pool(self.ledger), [{"kind": "incus", "node": "vm", "host": None, "slots": 1, "used": 0}])
        self.scenario(dict(TURNS, **{"milestone:M1": dict(TURNS["milestone:M1"], sleep=1.5)}))
        first, rec1 = dispatch.dispatch(self.ledger, self.approved("First"), log=lambda m: None)
        second, rec2 = dispatch.dispatch(self.ledger, self.approved("Second"), log=lambda m: None)
        self.assertEqual((rec1["state"], rec2["state"]), ("dispatched", "queued"))
        self.assertIn("no free slot on any node (vm 1/1)", rec2["queue_reason"])
        self.assertIn("Pool: vm 1/1 · 1 queued", status.render(status.collect(self.ledger, do_sync=False), width=200))
        self.assertFalse(self.box(rec2["machine"]["name"]).exists(), "a queued run has no container yet")
        self.assertEqual(dispatch.pump(self.ledger), [], "nothing can start while the slot is held")
        self.wait_final(first)
        self.assertEqual(dispatch.pump(self.ledger), [second])
        self.assertEqual(self.wait_final(second)["state"], "done")
        self.configure(slots=None)
        self.assertEqual(dispatch.pool(self.ledger), [], "no number, no limit")

    def test_a_command_in_a_container_carries_input_output_and_exit_status(self):
        tr = Incus.create(TEMPLATE, "org-eng-probe")
        self.assertEqual((tr.kind, tr.describe(), self.state("org-eng-probe")), ("incus", "incus:org-eng-probe", "running"))
        self.assertEqual(tr.argv("echo hi"), ["incus", "exec", "org-eng-probe", "-T", "--", "bash", "-c", "echo hi"])
        self.assertEqual(tr.attach_hint("001-x-r1"), "incus exec org-eng-probe -t -- tmux attach -t org-001-x-r1")
        payload = bytes(range(256)) * 400
        p = tr.run("cat; echo oops >&2; exit 3", input=payload, check=False)
        self.assertEqual((p.returncode, p.stdout, p.stderr.strip()), (3, payload, b"oops"))
        self.assertEqual(tr.run("cat").stdout, b"", "a command is never left reading this process's own stdin")
        home = self.box("org-eng-probe") / "home"
        self.assertEqual(tr.run('echo "$HOME"; echo "$ORG_SECRETS_DIR"').stdout.decode().split(), [str(home), str(home / ".run-org")])
        with self.assertRaises(TransportError) as ctx:
            tr.run("echo broken >&2; exit 7")
        self.assertIn("exit 7: broken", str(ctx.exception))
        with self.assertRaises(TransportError) as ctx:
            tr.run("sleep 30", timeout=1)
        self.assertIn("no answer within 1s", str(ctx.exception))
        # What is not there is said to be not there.
        self.assertIsNone(Incus.info("org-eng-nobody"))
        self.assertEqual(Incus.info("org-eng-probe")["status"], "Running")
        with self.assertRaises(TransportError) as ctx:
            Incus.start("org-eng-nobody")
        self.assertIn("no container named org-eng-nobody", str(ctx.exception))
        with self.assertRaises(TransportError) as ctx:
            Incus.create(TEMPLATE, "org-eng-probe")
        self.assertIn("incus copy org-base org-eng-probe failed", str(ctx.exception))
        os.environ["PATH"] = str(self.tmp / "nothing-here")
        with self.assertRaises(TransportError) as ctx:
            Incus.info("org-eng-probe")
        self.assertIn("`incus` is not installed", str(ctx.exception))


class OnlyItsOwn(IncusCase):
    """The prefix is the whole of what keeps org's hands off every other container on the machine."""

    ACTIONS = ("transport", "provision", "vet", "remove", "before_resume")

    def act(self, action, machine, cfg):
        backend = IncusBackend()
        if action == "provision":
            return backend.provision(machine, "001-x-r1", cfg)
        if action == "vet":
            return backend.vet(machine, {}, cfg)
        return getattr(backend, action)(machine, cfg)

    def test_a_name_outside_the_prefix_is_refused_before_incus_hears_of_it(self):
        cfg = self.ledger.config()
        self.container("org-manager", running=True)
        self.container("somebody-elses", running=True)
        for name in (TEMPLATE, "org-manager", "somebody-elses", "base", "eng-001-x-r1", "xorg-eng-001-x-r1", "org-eng-", "org-eng-../org-base",
                     "org-eng-A", "org-eng-x y", "org-eng-x;id", "org-eng--f", "-f", "", None, 7, "org-eng-" + "x" * 80):
            for action in self.ACTIONS:
                with self.assertRaises(BackendError, msg="%s %r" % (action, name)) as ctx:
                    self.act(action, {"kind": "incus", "name": name, "base": TEMPLATE}, cfg)
                self.assertIn("org only acts on containers named org-eng-<run>", str(ctx.exception))
        self.assertEqual(self.calls(), [], "a refused name never reaches incus")
        for name in (TEMPLATE, "org-manager", "somebody-elses"):
            self.assertTrue((self.box(name) / "home").is_dir(), name)
        # The same check accepts every name a run is given, however long its title.
        long = backends.machine_name("org-eng-", "012-" + "a-very-long-title-" * 6 + "end-r12")
        self.assertEqual(IncusBackend()._own(cfg, long), long)
        self.assertEqual(IncusBackend().transport({"name": "org-eng-001-x-r1"}, cfg).name, "org-eng-001-x-r1")

    def test_a_run_record_cannot_point_org_at_another_container(self):
        # run.json is the manager's own file, but nothing that reads it trusts the name in it.
        self.container("org-manager", running=True)
        for i, name in enumerate((TEMPLATE, "org-manager")):
            run_id = "00%d-tampered-r1" % i
            self.ledger.run_dir(run_id).mkdir(parents=True)
            self.ledger.save_run(run_id, {"run_id": run_id, "state": "dispatched", "remote_dir": "$HOME/org-runs/" + run_id,
                                          "machine": {"kind": "incus", "name": name, "base": TEMPLATE}, "events_offset": 0})
            for act in (dispatch.remove_machine, dispatch.resume, dispatch.stop, lambda ledger, r: dispatch.machine_run(ledger, r, "true")):
                with self.assertRaises(BackendError):
                    act(self.ledger, run_id)
            rec = sync.sync_run(self.ledger, run_id)
            self.assertEqual((rec["reachable"], rec.get("machine_removed_at")), (False, None))
            self.assertIn("org only acts on containers named", rec["last_error"])
            self.assertTrue((self.box(name) / "home").is_dir())
        self.assertEqual(self.calls(), [])

    def test_a_template_inside_the_prefix_is_refused(self):
        # Were the template named like an engineer's container, a run could be asked to delete it.
        self.container("org-eng-base")
        self.configure(base="org-eng-base")
        cfg = self.ledger.config()
        with self.assertRaises(BackendError) as ctx:
            IncusBackend().choose(cfg, "incus", "001-x-r1")
        self.assertIn("the template 'org-eng-base' starts with the engineers' prefix 'org-eng-'", str(ctx.exception))
        with self.assertRaises(dispatch.DispatchError):
            dispatch.choose_machine(cfg, {}, None, "001-x-r1")
        errors, _ = spec.check_text(self.ledger, GOOD)
        self.assertTrue(any("starts with the engineers' prefix" in e for e in errors), errors)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.ledger.run_ids(), [])

    def test_a_prefix_must_be_one(self):
        for bad in ("org-eng", "", "Org-Eng-", "-eng-", "org eng-", "9eng-", "org-eng-*"):
            self.configure(prefix=bad)
            for action in self.ACTIONS:
                with self.assertRaises(BackendError, msg="%s with prefix %r" % (action, bad)) as ctx:
                    self.act(action, {"kind": "incus", "name": bad + "001-x-r1", "base": TEMPLATE}, self.ledger.config())
                self.assertIn("machines.incus.prefix", str(ctx.exception))
        self.assertEqual(self.calls(), [])


class ABoxOrNot(IncusCase):
    """An engineer runs with every permission bypassed. What Incus says of its container decides whether it may."""

    NOT_A_BOX = (
        ({"expanded_config": {"security.privileged": "true"}}, "it is privileged"),
        ({"expanded_devices": {"host": {"type": "disk", "source": "/home/org", "path": "/mnt/host"}}}, "device host shares /home/org"),
        ({"expanded_devices": {"root": {"type": "disk", "path": "/", "pool": "default", "source": "/srv/rootfs"}}}, "device root shares /srv/rootfs"),
        ({"expanded_devices": {"scratch": {"type": "disk", "path": "/scratch", "pool": "default"}}}, "device scratch shares /scratch"),
        ({"expanded_devices": {"eth0": {"type": "nic", "network": "incusbr0", "security.ipv4_filtering": "true"}}},
         "network card eth0 has no security.port_isolation"),
        ({"expanded_devices": {"eth0": {"type": "nic", "network": "incusbr0", "security.port_isolation": "true"}}},
         "network card eth0 has no security.ipv4_filtering"),
        ({"expanded_devices": {"eth1": {"type": "nic", "network": "incusbr0", "security.port_isolation": "false",
                                        "security.ipv4_filtering": "true"}}}, "network card eth1 has no security.port_isolation"),
        ({"expanded_devices": {"daemon": {"type": "proxy", "listen": "unix:/run/incus.sock", "connect": "unix:/var/lib/incus/unix.socket"}}},
         "device daemon (proxy) gives it part of this machine"),
        ({"expanded_devices": {"gpu": {"type": "gpu"}}}, "device gpu (gpu) gives it part of this machine"),
    )

    def test_what_incus_says_of_a_container_decides(self):
        backend, cfg = IncusBackend(), self.ledger.config()
        machine = {"kind": "incus", "name": "org-eng-001-x-r1", "base": TEMPLATE}
        self.container(machine["name"], running=True)
        self.assertEqual(problems(Incus.info(machine["name"])), [])
        backend.vet(machine, {}, cfg)  # an isolated container passes
        for description, expected in self.NOT_A_BOX:
            self.describe(machine["name"], **description)
            found = problems(Incus.info(machine["name"]))
            self.assertEqual(len(found), 1, (description, found))
            self.assertIn(expected, found[0])
            with self.assertRaises(BackendError, msg=expected) as ctx:
                backend.vet(machine, {}, cfg)
            self.assertIn(expected, str(ctx.exception))
            self.assertIn("org-eng-001-x-r1 is not a box for an engineer", str(ctx.exception))
        # Every fault is named, not only the first.
        self.describe(machine["name"], expanded_config={"security.privileged": "true"},
                      expanded_devices={"eth0": {"type": "nic", "network": "incusbr0"}, "host": {"type": "disk", "source": "/", "path": "/host"}})
        self.assertEqual(len(problems(Incus.info(machine["name"]))), 4)
        # The person may decide otherwise, in config, which the chat cannot write.
        self.configure(allow_open=True)
        backend.vet(machine, {}, self.ledger.config())
        # A container that is not there has nothing to say for itself.
        with self.assertRaises(BackendError) as ctx:
            backend.vet({"kind": "incus", "name": "org-eng-gone"}, {}, cfg)
        self.assertIn("org-eng-gone is gone", str(ctx.exception))

    def test_no_engineer_starts_in_a_container_that_is_not_a_box(self):
        # A copy is what its template is: a privileged template makes privileged engineers.
        self.describe(TEMPLATE, expanded_config={"security.privileged": "true"})
        self.configure(slots=1)
        with self.assertRaises(dispatch.DispatchError) as ctx:
            dispatch.dispatch(self.ledger, self.approved(), log=lambda m: None)
        self.assertIn("it is privileged: its root is this machine's root", str(ctx.exception))
        rec = self.ledger.run("001-csv-export-r1")
        self.assertEqual(rec["state"], "dispatch_failed")
        self.assertIn("is not a box for an engineer", rec["error"])
        home = self.box("org-eng-001-csv-export-r1") / "home"
        self.assertFalse((home / ".run-org").exists(), "it was refused before any token was handed to it")
        self.assertFalse((home / "org-runs").exists(), "and before anything was started in it")
        self.assertEqual(dispatch.pool(self.ledger)[0]["used"], 0, "a refused run holds no slot")
        # With the template put right, the next run of the same spec goes ahead.
        (self.box(TEMPLATE) / "override.json").unlink()
        run_id, rec = dispatch.dispatch(self.ledger, "001-csv-export", log=lambda m: None)
        self.assertEqual((run_id, rec["state"]), ("001-csv-export-r2", "dispatched"))
        self.assertEqual(self.wait_final(run_id)["state"], "done")


class ItsLife(IncusCase):
    def test_remove_deletes_the_runs_container_and_no_other(self):
        self.container("somebody-elses", running=True)
        run_id, rec = dispatch.dispatch(self.ledger, self.approved(), log=lambda m: None)
        name = rec["machine"]["name"]
        self.wait_final(run_id)
        self.assertEqual(dispatch.remove_machine(self.ledger, run_id), "deleted " + name)
        self.assertFalse(self.box(name).exists())
        self.assertTrue(self.ledger.run(run_id)["machine_removed_at"])
        self.assertTrue((self.ledger.mirror_dir(run_id) / "report.md").exists(), "the record outlives the container")
        # Gone already is said, not done again.
        self.assertEqual(IncusBackend().remove(rec["machine"], self.ledger.config()), name + " was already gone")
        for other in (TEMPLATE, "somebody-elses"):
            with self.assertRaises(BackendError):
                IncusBackend().remove(dict(rec["machine"], name=other), self.ledger.config())
            self.assertTrue((self.box(other) / "home").is_dir(), other)
        self.assertEqual([c for c in self.calls() if c[0] == "delete"], [["delete", "-q", "-f", name]])
        with self.assertRaises(dispatch.DispatchError) as ctx:
            dispatch.machine_run(self.ledger, run_id, "true")
        self.assertIn("no machine to reach (removed)", str(ctx.exception))

    def test_a_stopped_container_is_started_before_its_runner_is_resumed(self):
        self.scenario(dict(TURNS, **{"milestone:M1": dict(TURNS["milestone:M1"], sleep=60)}))
        run_id, rec = dispatch.dispatch(self.ledger, self.approved(), log=lambda m: None)
        name, cfg = rec["machine"]["name"], self.ledger.config()
        st = self.wait_executing(run_id)
        self.assertEqual(dispatch.interrupted(self.ledger), [])
        # The machine that holds the containers restarts: every process in them ends, they come
        # back stopped, and what was in their memory, the tokens, is gone.
        tokens = self.box(name) / "home" / ".run-org"
        self.assertEqual(sorted(p.name for p in tokens.iterdir()), ["claude-token", "github-token"])
        os.kill(st["pid"], 9)
        subprocess.run(["pkill", "-f", str(self.tmp)], capture_output=True)
        self.assertEqual(self.incus("stop", "-q", name).returncode, 0)
        self.assertEqual((self.state(name), tokens.exists()), ("stopped", False))
        with self.assertRaises(TransportError) as ctx:
            Incus(name).run("true")
        self.assertIn("not running", str(ctx.exception))
        self.assertFalse(sync.sync_run(self.ledger, run_id)["reachable"])
        self.assertEqual(dispatch.interrupted(self.ledger), [run_id])
        self.assertEqual(dispatch.active(self.ledger), [run_id], "it still holds its container and its slot")

        IncusBackend().before_resume(rec["machine"], cfg)
        self.assertEqual(self.state(name), "running")
        self.assertEqual(Incus(name).run("echo up").stdout, b"up\n")
        starts = [c for c in self.calls() if c[0] == "start"]
        self.assertEqual(starts, [["start", "-q", name]] * 2, "once when it was made, once now")
        # A container that is running is left as it is. It is handed the tokens again, or the
        # engineer could not make a request, and the run goes on from where it stood.
        self.scenario(TURNS)
        log = []
        dispatch.resume(self.ledger, run_id, log=log.append)
        self.assertEqual([c for c in self.calls() if c[0] == "start"], starts)
        for filename, token in (("claude-token", CLAUDE_TOKEN), ("github-token", GITHUB_TOKEN)):
            self.assertEqual((tokens / filename).read_text(), token + "\n")
            self.assertEqual(stat.S_IMODE((tokens / filename).stat().st_mode), 0o600)
        self.assertEqual(log, ["Claude token copied in", "GitHub token copied in"])
        self.assertNotIn("sk-ant-", (self.root / "calls.jsonl").read_text())
        self.assertEqual(self.wait_final(run_id)["state"], "done")
        labels = [json.loads(line)["label"] for line in (self.scen / "invocations.jsonl").read_text().splitlines()]
        self.assertEqual(labels.count("plan"), 1, "the plan was not redone")
        self.assertEqual(dispatch.interrupted(self.ledger), [])
        # One that is gone cannot be started, and says so.
        with self.assertRaises(TransportError) as ctx:
            IncusBackend().before_resume(dict(rec["machine"], name="org-eng-gone"), cfg)
        self.assertIn("no container named org-eng-gone", str(ctx.exception))


class Draining(IncusCase):
    def test_while_draining_approved_work_queues_and_it_starts_when_the_drain_is_lifted(self):
        self.assertIsNone(dispatch.draining(self.ledger))
        held = dispatch.drain(self.ledger, "upgrading org")
        self.assertEqual((held["reason"], dispatch.draining(self.ledger)), ("upgrading org", held))
        self.assertEqual(dispatch.drain(self.ledger, "again"), held, "a drain in force is not replaced by a later one")
        log = []
        run_id, rec = dispatch.dispatch(self.ledger, self.approved(), log=log.append)
        self.assertEqual(rec["state"], "queued")
        self.assertIn("draining since", rec["queue_reason"])
        self.assertIn("upgrading org", rec["queue_reason"])
        self.assertTrue(any("queued: draining" in line for line in log), log)
        for _ in range(2):
            self.assertEqual(dispatch.pump(self.ledger), [], "nothing starts while the drain is in force")
        rec = self.ledger.run(run_id)
        self.assertEqual(rec["state"], "queued")
        self.assertIn("draining", rec["queue_reason"])
        self.assertEqual(self.calls(), [], "incus was asked for nothing")
        self.assertFalse(self.box(rec["machine"]["name"]).exists())
        self.assertEqual((dispatch.active(self.ledger), dispatch.node_load(self.ledger)), ([], {}), "a queued run is not what a drain waits for")
        view = [v for v in status.collect(self.ledger, do_sync=True)["runs"] if v["run_id"] == run_id][0]
        self.assertEqual((view["state"], view["terminal"]), ("queued", False), "looking at the ledger does not start it either")
        self.assertIn("draining", view["detail"])

        self.assertTrue(dispatch.undrain(self.ledger))
        self.assertIsNone(dispatch.draining(self.ledger))
        self.assertFalse(dispatch.undrain(self.ledger), "there was nothing left to lift")
        self.assertEqual(dispatch.pump(self.ledger), [run_id])
        rec = self.ledger.run(run_id)
        self.assertEqual((rec["state"], rec["queue_reason"]), ("dispatched", None))
        self.assertEqual(self.state(rec["machine"]["name"]), "running")
        self.assertEqual(dispatch.active(self.ledger), [run_id])
        self.assertEqual(self.wait_final(run_id)["state"], "done")
        self.assertEqual(dispatch.active(self.ledger), [])

    def test_a_drain_leaves_what_is_running_alone(self):
        self.scenario(dict(TURNS, **{"milestone:M1": dict(TURNS["milestone:M1"], sleep=1.5)}))
        run_id, _ = dispatch.dispatch(self.ledger, self.approved(), log=lambda m: None)
        dispatch.drain(self.ledger)
        self.assertEqual(dispatch.active(self.ledger), [run_id])
        self.assertEqual(self.wait_final(run_id)["state"], "done")
        self.assertEqual(dispatch.active(self.ledger), [], "which is when a drain has nothing left to wait for")


class Reaping(IncusCase):
    def provisioning(self, run_id, provisioner):
        """A run as `start` leaves it between taking a slot and finishing the container."""
        machine = dispatch.choose_machine(self.ledger.config(), {}, None, run_id)
        machine["node"] = "vm"
        self.ledger.run_dir(run_id).mkdir(parents=True)
        self.ledger.save_run(run_id, {"run_id": run_id, "spec_id": run_id.rsplit("-r", 1)[0], "machine": machine, "state": "provisioning",
                                      "remote_dir": dispatch.remote_dir(run_id), "created_at": time.time(), "events_offset": 0,
                                      "provisioner": provisioner})
        return machine["name"]

    def test_a_dispatch_whose_process_died_gives_its_slot_back(self):
        self.configure(slots=1)
        first = self.approved("First")
        ended = subprocess.Popen(["true"])
        ended.wait()
        # The process that was setting the run up is gone, half way: the copy exists and was never started.
        name = self.provisioning(first + "-r1", dict(dispatch._me(), pid=ended.pid))
        self.container(name)
        self.assertEqual(dispatch.node_load(self.ledger), {("incus", "vm"): 1}, "it holds the only slot")
        second, rec = dispatch.dispatch(self.ledger, self.approved("Second"), log=lambda m: None)
        self.assertEqual(rec["state"], "queued")
        self.assertEqual(dispatch.active(self.ledger), [first + "-r1"], "and a drain would wait for it for ever")

        log = []
        self.assertEqual(dispatch.reap(self.ledger, log.append), [first + "-r1"])
        rec = self.ledger.run(first + "-r1")
        self.assertEqual(rec["state"], "dispatch_failed")
        self.assertIn("ended before it finished", rec["error"])
        self.assertEqual((dispatch.node_load(self.ledger), dispatch.active(self.ledger)), ({}, []))
        self.assertFalse(self.box(name).exists(), "what was made of its container is removed")
        self.assertTrue(any("interrupted" in line for line in log), log)
        self.assertEqual(dispatch.reap(self.ledger), [], "once")
        self.assertEqual(dispatch.pump(self.ledger), [second], "the slot goes to the run that was waiting")
        self.assertEqual(self.wait_final(second)["state"], "done")

    def test_only_a_dispatch_that_is_really_over_is_reaped(self):
        # One whose process is alive is still being set up, however long it takes.
        ours = self.provisioning("001-ours-r1", dispatch._me())
        self.container(ours)
        self.assertEqual(dispatch.reap(self.ledger), [])
        self.assertEqual(dispatch.pump(self.ledger), [])
        self.assertEqual(self.ledger.run("001-ours-r1")["state"], "provisioning")
        self.assertEqual(dispatch.node_load(self.ledger), {("incus", "vm"): 1})
        self.assertTrue(self.box(ours).exists())
        self.assertEqual(self.calls(), [])
        # A process id from before this machine last started belongs to nobody now, even if one has it.
        self.provisioning("002-earlier-r1", dict(dispatch._me(), boot="a-boot-before-this-one"))
        # And so does a record that names no process at all.
        self.provisioning("003-nobody-r1", {})
        self.assertEqual(dispatch.pump(self.ledger), [], "pump reaps, and neither was queued")
        self.assertEqual([self.ledger.run(r)["state"] for r in self.ledger.run_ids()], ["provisioning", "dispatch_failed", "dispatch_failed"])
        self.assertEqual(dispatch.node_load(self.ledger), {("incus", "vm"): 1})


class Names(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="org-kinds-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ledger = Ledger(self.tmp)
        self.ledger.init()

    def test_a_long_name_keeps_its_run_number(self):
        name = backends.machine_name("org-eng-", "012-" + "a-very-long-title-" * 6 + "end-r12")
        self.assertLessEqual(len(name), 60)
        self.assertTrue(name.startswith("org-eng-012-a-very-long-title"))
        self.assertTrue(name.endswith("-r12"))
        self.assertEqual(backends.machine_name("eng-", "001-CSV_export-r1"), "eng-001-csv-export-r1")
        chosen = IncusBackend().choose(self.ledger.config(), "incus", "012-" + "a-very-long-title-" * 6 + "end-r12")
        self.assertEqual((chosen["name"], chosen["base"], chosen["push_token"]), (name, "org-base", True))

    def test_what_a_spec_may_ask_for(self):
        self.assertEqual(backends.kinds(self.ledger.config()), ["incus", "local", "ssh"])
        cases = {
            "machine: docker": "The Docker backend is a plugin",
            "machine: mainframe": "no backend for machine kind 'mainframe'",
            "machine: orb": "no backend for machine kind 'orb'",
            "machine: incus:big": "takes no name",
            "machine: ssh:gpu": "is not defined in config.json",
            "machine: local": "the local machine is disabled",
        }
        for header, expected in cases.items():
            errors, _ = spec.check_text(self.ledger, GOOD.replace("delivery: pr", "delivery: pr\n" + header))
            self.assertTrue(any(expected in e for e in errors), "%s: %r not in %r" % (header, expected, errors))
        self.assertEqual(spec.check_text(self.ledger, GOOD)[0], [])
        write_json(self.ledger.config_path, {"machines": {"plugins": ["org.backends.docker"], "ssh": {"gpu": {"host": "me@gpu"}}}})
        for header in ("machine: docker", "machine: docker:local", "machine: ssh:gpu", "machine: incus"):
            self.assertEqual(spec.check_text(self.ledger, GOOD.replace("delivery: pr", "delivery: pr\n" + header))[0], [], header)
        write_json(self.ledger.config_path, {"machines": {"plugins": ["org.backends.nothing_here"]}})
        with self.assertRaises(backends.BackendError):
            backends.load(self.ledger.config())


class Doctor(IncusCase):
    def rows(self):
        """`org doctor` as a person runs it: {what was checked: (ok, warn or FAIL, what was found)}."""
        p = subprocess.run([sys.executable, "-m", "org", "--home", str(self.ledger.root), "doctor"], capture_output=True, text=True,
                           cwd=str(ROOT), stdin=subprocess.DEVNULL, timeout=60)
        rows = {m.group(2): (m.group(1), m.group(3)) for m in re.finditer(r"^(ok|warn|FAIL) +(.+?) {2,}(.*)$", p.stdout, re.M)}
        self.assertIn("default machine", rows, p.stdout + p.stderr)
        return rows

    def test_doctor_reads_the_template_as_incus_describes_it(self):
        template = "template container `org-base`"
        rows = self.rows()
        self.assertEqual(rows["default machine"], ("ok", "incus"))
        self.assertEqual(rows["incus"], ("ok", str(self.bindir / "incus")))
        self.assertEqual(rows[template], ("ok", "stopped, isolated"))
        self.assertEqual(rows["engineers at once"], ("ok", "4"))
        self.describe(TEMPLATE, expanded_config={"security.privileged": "true"})
        self.configure(slots=None)
        rows = self.rows()
        self.assertEqual(rows[template][0], "FAIL")
        self.assertIn("it is privileged", rows[template][1])
        self.assertEqual(rows["engineers at once"], ("ok", "no limit set (machines.incus.slots)"))
        shutil.rmtree(self.box(TEMPLATE))
        self.assertEqual(self.rows()[template], ("FAIL", "missing"))
        self.assertFalse([c for c in self.calls() if c[0] != "list"], "doctor only looks")


class TheSimulatedIncus(IncusCase):
    """The stand-in itself, where the installer leans on it and nothing above does."""

    def test_what_the_installer_calls(self):
        said = [
            ["init", "images:debian/13", "org-new", "-c", "security.nesting=true", "--storage", "default"],
            ["config", "device", "override", "org-new", "eth0", "security.port_isolation=true", "security.ipv4_filtering=true"],
            ["config", "device", "set", "org-new", "eth0", "security.ipv6_filtering", "true"],
            ["config", "device", "get", "org-new", "eth0", "security.port_isolation"],
            ["config", "set", "org-new", "boot.autostart", "false"],
            ["config", "get", "org-new", "security.privileged"],
            ["network", "set", "incusbr0", "ipv6.address", "none"],
            ["profile", "show", "default"],
            ["start", "-q", "org-new"],
            ["exec", "org-new", "-T", "--env", "DEBIAN_FRONTEND=noninteractive", "--", "sh", "-c",
             'echo "$DEBIAN_FRONTEND"; echo "$HOME"; echo "${ORG_HOME:-unset}"; echo installed > tool'],
            ["stop", "-q", "org-new"],
            ["copy", "-q", "org-new", "org-eng-copy"],
        ]
        out = [self.incus(*argv) for argv in said]
        self.assertEqual([(p.returncode, p.stderr) for p in out], [(0, "")] * len(said))
        self.assertEqual(self.calls(), said, "every word of every call is on record")
        home = self.box("org-new") / "home"
        self.assertEqual(out[9].stdout.split(), ["noninteractive", str(home), "unset"])
        self.assertEqual(read_json(self.box("org-new") / "created.json"),
                         {"image": "images:debian/13", "flags": ["-c", "security.nesting=true", "--storage", "default"]})
        self.assertEqual((self.box("org-eng-copy") / "home" / "tool").read_text(), "installed\n", "a copy has what its source had")
        self.assertEqual(self.incus("list", "-f", "csv", "-c", "ns").stdout.split(),
                         ["org-base,STOPPED", "org-eng-copy,STOPPED", "org-new,STOPPED"])
        self.assertEqual(self.incus("list", "-f", "csv", "-c", "n", "org-eng-").stdout.split(), ["org-eng-copy"])
        self.assertEqual([i["name"] for i in json.loads(self.incus("list", "--format", "json").stdout)], ["org-base", "org-eng-copy", "org-new"])
        info = json.loads(self.incus("query", "/1.0/instances/org-new").stdout)
        self.assertEqual((info["status"], info["expanded_config"], sorted(info["expanded_devices"])), ("Stopped", {}, ["eth0", "root"]))
        self.assertEqual(info["expanded_devices"]["root"], {"type": "disk", "path": "/", "pool": "default"})
        self.assertEqual(problems(info), [])

    def test_what_it_refuses_as_the_real_one_does(self):
        self.container("org-eng-up", running=True)
        self.container("org-eng-down")
        for argv, expected in (
            (["query", "/1.0/instances/org-eng-nobody"], "not found"),
            (["copy", "-q", "no-such-template", "org-eng-new"], "not found"),
            (["copy", "-q", TEMPLATE, "org-eng-up"], "already exists"),
            (["init", "images:debian/13", "org-eng-up"], "already exists"),
            (["start", "-q", "org-eng-nobody"], "not found"),
            (["start", "-q", "org-eng-up"], "already running"),
            (["stop", "-q", "org-eng-down"], "already stopped"),
            (["exec", "org-eng-down", "-T", "--", "true"], "not running"),
            (["exec", "org-eng-nobody", "-T", "--", "true"], "not found"),
            (["exec", "org-eng-../org-base", "-T", "--", "true"], "not found"),
            (["delete", "-q", "org-eng-up"], "currently running"),
            (["delete", "-q", "-f", "org-eng-nobody"], "not found"),
        ):
            p = self.incus(*argv)
            self.assertEqual(p.returncode, 1, argv)
            self.assertIn(expected, p.stderr, argv)
        self.assertEqual(self.incus("snapshot", "create", "org-eng-up").returncode, 2, "what org does not use is not pretended")
        self.assertEqual(self.incus("exec", "org-eng-up", "-T", "--", "sh", "-c", "exit 5").returncode, 5)
        self.assertEqual(self.incus("delete", "-q", "org-eng-down").returncode, 0)
        self.assertEqual(self.incus("delete", "-q", "-f", "org-eng-up").returncode, 0)
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if (p / "home").is_dir()), [TEMPLATE])
        # A template nobody made exists only under the name $FAKE_INCUS_BASE gives it.
        self.assertEqual(self.incus("query", "/1.0/instances/base").returncode, 0)
        os.environ["FAKE_INCUS_BASE"] = "another"
        self.assertEqual(self.incus("copy", "-q", "another", "org-eng-from-another").returncode, 0)
        self.assertEqual(self.incus("query", "/1.0/instances/elsewhere").returncode, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
