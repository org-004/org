"""Engineers on more than one machine: machines.incus.nodes, against a simulated `incus`.

A node with `remote` is another machine's Incus, reached as `<remote>:<name>`. In the simulated
Incus a remote is a directory of its own (.remote-<remote>), so a container made there is not one
made here, and a test can see on which machine everything happened.
"""
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from org import cache, dispatch, spec  # noqa: E402
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

    def test_a_remote_that_does_not_answer_fails_the_dispatch_and_doctor_says_so(self):
        os.environ["FAKE_INCUS_DOWN"] = "w1"
        with self.assertRaises(dispatch.DispatchError) as ctx:
            dispatch.dispatch(self.ledger, self.approved(), machine_override="incus:w1", log=lambda m: None)
        self.assertIn("connection refused", str(ctx.exception))
        rec = self.ledger.run("001-csv-export-r1")
        self.assertEqual((rec["state"], rec["machine"]["node"]), ("dispatch_failed", "w1"))
        self.assertEqual(dispatch.pool(self.ledger)[1]["used"], 0, "a dispatch that failed holds no slot")
        rows = self.rows()
        self.assertEqual(rows["node w1 (remote w1)"][0], "FAIL")
        self.assertIn("connection refused", rows["node w1 (remote w1)"][1])
        os.environ.pop("FAKE_INCUS_DOWN")
        rows = self.rows()
        self.assertEqual(rows["node w1: template `org-base`"], ("ok", "stopped, isolated"))
        self.assertEqual(rows["node w1"], ("ok", "remote w1, 1 at once, 5120MiB each"))

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
