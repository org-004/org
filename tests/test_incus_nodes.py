"""Engineers on more than one machine: machines.incus.nodes, against a simulated `incus`.

A node with `remote` is another machine's Incus, reached as `<remote>:<name>`. In the simulated
Incus a remote is a directory of its own (.remote-<remote>), so a container made there is not one
made here, and a test can see on which machine everything happened.
"""
import json
import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from org import cache, dispatch, power, spec  # noqa: E402
from org.backends import BackendError  # noqa: E402
from org.backends.incus import IncusBackend, memory_limit, node_entries  # noqa: E402
from org.transport import Incus, TransportError  # noqa: E402
from org.util import write_json  # noqa: E402
from test_cache import network  # noqa: E402
from test_incus import TEMPLATE, IncusCase  # noqa: E402
from test_manager_side import GOOD, TURNS  # noqa: E402

TWO = {"vm": {"slots": 1}, "w1": {"remote": "w1", "slots": 1, "memory_total": "8GiB"}}


class Nodes(IncusCase):
    def setUp(self):
        super().setUp()
        power._ready.clear()
        self.remote_container("w1", TEMPLATE)
        (self.remote_box("w1", TEMPLATE) / "home" / "from-w1s-template").write_text("w1\n")
        self.configure(nodes=TWO)

    def remote_box(self, remote, name):
        return self.root / (".remote-" + remote) / name

    def remote_container(self, remote, name, running=False, **description):
        box = self.remote_box(remote, name)
        (box / "home").mkdir(parents=True)
        (box / "state").write_text("running\n" if running else "stopped\n")
        if description:
            (box / "override.json").write_text(json.dumps(description))

    def test_a_second_run_goes_to_the_other_machine_and_everything_of_it_happens_there(self):
        self.assertEqual(dispatch.pool(self.ledger), [
            {"kind": "incus", "node": "vm", "host": None, "slots": 1, "used": 0},
            {"kind": "incus", "node": "w1", "host": "remote w1", "slots": 1, "used": 0}])
        self.scenario(dict(TURNS, **{"milestone:M1": dict(TURNS["milestone:M1"], sleep=1.0)}))
        first, rec1 = dispatch.dispatch(self.ledger, self.approved("First"), log=lambda m: None)
        second, rec2 = dispatch.dispatch(self.ledger, self.approved("Second"), log=lambda m: None)
        third, rec3 = dispatch.dispatch(self.ledger, self.approved("Third"), log=lambda m: None)
        self.assertEqual((rec1["machine"]["node"], rec2["machine"]["node"], rec3["state"]), ("vm", "w1", "queued"))
        self.assertNotIn("remote", rec1["machine"])
        self.assertEqual(rec2["machine"]["remote"], "w1")
        self.assertIn("no free slot on any node (vm 1/1, w1 1/1)", rec3["queue_reason"])
        name = rec2["machine"]["name"]
        # The copy is of w1's own template, made on w1; its limit is w1's memory less the reserve, for one.
        self.assertEqual(rec2["machine"]["limits"], {"memory": "5120MiB"})
        copies = [c for c in self.calls() if c[0] == "copy"]
        self.assertIn(["copy", "-q", "w1:" + TEMPLATE, "w1:" + name, "-c", "limits.memory=5120MiB"], copies)
        self.assertTrue((self.remote_box("w1", name) / "home" / "from-w1s-template").exists())
        self.assertFalse(self.box(name).exists(), "nothing of it is on this machine")
        # Every command for it named it on w1.
        for c in self.calls():
            if name in " ".join(c):
                self.assertTrue(any(w in ("w1:" + name, "w1:") for w in c), c)
        self.assertEqual(self.wait_final(second)["state"], "done")
        self.assertIn("Delivered", (self.ledger.mirror_dir(second) / "report.md").read_text())
        self.assertEqual(self.wait_final(first)["state"], "done")
        self.assertEqual(sorted(dispatch.pump(self.ledger)), [third])
        self.assertEqual(self.wait_final(third)["state"], "done")
        # A finished run's container is deleted on w1, where it is, and nothing here is touched.
        self.assertIn(["delete", "-q", "-f", "w1:" + name], self.calls())
        self.assertFalse(self.remote_box("w1", name).exists())
        self.assertEqual(IncusBackend().remove(rec2["machine"], self.ledger.config()), "w1:%s was already gone" % name)

    def test_a_spec_may_ask_for_one_node(self):
        spec_id = spec.new_draft(self.ledger, "Pinned", "acme")
        self.ledger.draft_path(spec_id).write_text(GOOD.replace("CSV export", "Pinned").replace("delivery: merge", "delivery: merge\nmachine: incus:w1"))
        spec.approve(self.ledger, spec_id, spec.draft_sha(self.ledger, spec_id)[:10])
        run_id, rec = dispatch.dispatch(self.ledger, spec_id, log=lambda m: None)
        self.assertEqual((rec["machine"]["node"], rec["machine"]["remote"]), ("w1", "w1"))
        self.wait_final(run_id)
        errors, _ = spec.check_text(self.ledger, GOOD.replace("delivery: merge", "delivery: merge\nmachine: incus:w9"))
        self.assertTrue(any("takes no name" in e and "vm, w1" in e for e in errors), errors)

    def test_the_build_cache_is_the_one_beside_the_engineer(self):
        self.container("org-cache", running=True, state=network("10.9.9.9"))
        self.remote_container("w1", "org-cache", running=True, state=network("127.0.0.1"))
        os.environ["FAKE_INCUS_LOSES"] = ""
        run_id, rec = dispatch.dispatch(self.ledger, self.approved(), machine_override="incus:w1", log=lambda m: None)
        self.assertEqual(rec["machine"]["node"], "w1")
        self.assertIn(["list", "--format", "json", "w1:", "^org-cache$"], self.calls())
        self.assertEqual(self.ledger.run(run_id).get("build_cache", {}).get("address"), "127.0.0.1",
                         "w1's own cache, not this machine's at 10.9.9.9")
        self.wait_final(run_id)
        self.assertEqual(cache.look(self.ledger.config(), "w1")["name"], "w1:org-cache")

    def test_automatic_memory_on_a_remote_is_what_its_incus_says_else_what_the_node_says(self):
        nodes = dict(TWO, w1={"remote": "w1", "slots": 2})
        cfg = {"machines": {"incus": {"nodes": nodes}}}
        os.environ["FAKE_INCUS_MEMORY_MIB"] = "16384"
        self.assertEqual(memory_limit(cfg, node="w1"), "%dMiB" % ((16384 - 3072) // 2))
        os.environ.pop("FAKE_INCUS_MEMORY_MIB")
        with self.assertRaises(BackendError) as ctx:
            memory_limit(cfg, node="w1")
        self.assertIn("machines.incus.nodes.w1.memory_total", str(ctx.exception))
        nodes["w1"]["memory"] = "6GiB"
        self.assertEqual(memory_limit(cfg, node="w1"), "6GiB", "a size is taken as given")
        nodes["w1"].update(memory="auto", memory_total="12GiB", memory_reserve="2GiB")
        self.assertEqual(memory_limit(cfg, node="w1"), "%dMiB" % ((12288 - 2048) // 2))
        # This machine's node is sized from this machine (tests/meminfo), with its own slots.
        self.assertEqual(memory_limit(cfg, node="vm"), memory_limit({"machines": {"incus": {"slots": 1}}}))

    def test_nodes_written_wrong_are_refused_before_anything_is_placed(self):
        for nodes, words in (
                ({"w1": {"remote": "W 1"}}, "not the name of an Incus remote"),
                ({"w1": {"remote": "w1", "slots": 0}}, "slots must be a whole number"),
                ({"a": {"remote": "w1"}, "b": {"remote": "w1"}}, "two nodes are the remote w1"),
                ({"a": {}, "b": {}}, "two nodes are this machine"),
                ({"Big": {}}, "a node is a lowercase name"),
                (["vm"], "must map a node's name")):
            with self.assertRaises(BackendError, msg=repr(nodes)) as ctx:
                node_entries({"machines": {"incus": {"nodes": nodes}}})
            self.assertIn(words, str(ctx.exception))
        # Without nodes, this machine alone, as before.
        self.assertEqual(node_entries({"machines": {"incus": {"slots": 3}}}), {"vm": {"remote": None, "slots": 3}})
        self.assertEqual(node_entries({"machines": {"incus": {"slots": None}}}), {})
        with self.assertRaises(TransportError):
            Incus.at("not a remote", "org-eng-x")

    def test_a_remote_that_does_not_answer_is_passed_over_and_doctor_says_so(self):
        os.environ["FAKE_INCUS_DOWN"] = "w1"
        run_id, rec = dispatch.dispatch(self.ledger, self.approved(), machine_override="incus:w1", log=lambda m: None)
        self.assertEqual(rec["state"], "queued", "a node that does not answer is not failed on")
        self.assertIn("no free slot on w1", rec["queue_reason"])
        self.assertNotIn("starting", rec["queue_reason"], "a node without power is not started")
        rows = self.rows()
        self.assertEqual(rows["node w1 (remote w1)"][0], "FAIL")
        self.assertIn("connection refused", rows["node w1 (remote w1)"][1])
        os.environ.pop("FAKE_INCUS_DOWN")
        power.forget("w1")
        self.assertEqual(dispatch.pump(self.ledger), [run_id], "it starts once the node answers")
        rows = self.rows()
        self.assertEqual(rows["node w1: template `org-base`"], ("ok", "stopped, isolated"))
        self.assertEqual(rows["node w1"], ("ok", "remote w1, 1 at once, 5120MiB each"))
        self.wait_final(run_id)

    def powered(self, idle_min=20):
        marker = self.root / ".remote-w1" / ".." / ".down-w1"
        nodes = json.loads(json.dumps(TWO))
        nodes["w1"]["power"] = {"start": ["rm", "-f", str(marker)], "stop": ["touch", str(marker)], "idle_stop_min": idle_min}
        self.configure(nodes=nodes)
        return marker

    def test_a_run_for_a_stopped_node_starts_it_and_waits_for_it(self):
        marker = self.powered()
        marker.touch()
        run_id, rec = dispatch.dispatch(self.ledger, self.approved(), machine_override="incus:w1", log=lambda m: None)
        self.assertEqual(rec["state"], "queued")
        self.assertIn("starting w1", rec["queue_reason"])
        for _ in range(50):
            if not marker.exists():
                break
            time.sleep(0.1)
        self.assertFalse(marker.exists(), "the start command ran")
        self.assertTrue((self.ledger.root / "logs" / "power-w1.log").read_text().count("$ rm -f"), 1)
        # Asked once: another pump within the wake interval does not ask again.
        marker.touch()
        power.forget("w1")
        dispatch.pump(self.ledger)
        time.sleep(0.3)
        self.assertTrue(marker.exists(), "not woken twice in a row")
        marker.unlink()
        power.forget("w1")
        self.assertEqual(dispatch.pump(self.ledger), [run_id])
        self.assertEqual(self.wait_final(run_id)["state"], "done")
        self.assertEqual(self.rows()["node w1 (remote w1)"][0] if "node w1 (remote w1)" in self.rows() else "ok", "ok")

    def test_an_idle_node_is_stopped_once_and_a_busy_one_is_not(self):
        marker = self.powered(idle_min=0.01)  # under a second
        self.scenario(dict(TURNS, **{"milestone:M1": dict(TURNS["milestone:M1"], sleep=1.5)}))
        run_id, _ = dispatch.dispatch(self.ledger, self.approved(), machine_override="incus:w1", log=lambda m: None)
        time.sleep(0.8)
        dispatch.pump(self.ledger)
        self.assertFalse(marker.exists(), "not stopped while it holds a run")
        self.wait_final(run_id)
        dispatch.pump(self.ledger)  # the run's machine is torn down; the node is idle from its last busy pump
        time.sleep(1.0)
        power.forget("w1")
        dispatch.pump(self.ledger)
        for _ in range(50):
            if marker.exists():
                break
            time.sleep(0.1)
        self.assertTrue(marker.exists(), "an idle node is stopped")
        log = (self.ledger.root / "logs" / "power-w1.log").read_text()
        power.forget("w1")
        dispatch.pump(self.ledger)
        time.sleep(0.3)
        self.assertEqual((self.ledger.root / "logs" / "power-w1.log").read_text(), log, "stopped once, not at every pump")
        rows = self.rows()
        self.assertEqual(rows["node w1 (remote w1)"], ("ok", "not answering: started when a run is queued for it"))
        self.assertNotIn("vm", json.loads((self.ledger.root / "power.json").read_text()), "this machine has no power and is left alone")

    def rows(self):
        import re
        import subprocess
        p = subprocess.run([sys.executable, "-m", "org", "--home", str(self.ledger.root), "doctor"], capture_output=True, text=True,
                           cwd=str(ROOT), stdin=subprocess.DEVNULL, timeout=60)
        return {m.group(2): (m.group(1), m.group(3)) for m in re.finditer(r"^(ok|warn|FAIL) +(.+?) {2,}(.*)$", p.stdout, re.M)}

    def test_a_ledger_without_nodes_works_as_before(self):
        self.configure(slots=2)
        run_id, rec = dispatch.dispatch(self.ledger, self.approved(), log=lambda m: None)
        self.assertEqual(rec["machine"]["node"], "vm")
        self.assertNotIn("remote", rec["machine"])
        self.assertFalse([c for c in self.calls() if any(":" in w for w in c[:4] if not w.startswith("^"))])
        self.wait_final(run_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
