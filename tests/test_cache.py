"""The build cache (org/cache.py, the cache step of incus/up.sh), against the simulated `incus`.

Everything here reaches only tests/fake_incus.py. `org up` is the real incus/up.sh, run from a
copy of this checkout in which incus/cache.sh is a stub: the real one installs software as root,
and in the stand-in a container's commands run on this machine. Commands "in" a container really
run here, so a cache at 127.0.0.1 with a port this test listens on is one an engineer can reach.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_incus as base  # noqa: E402  (the module, not its classes: unittest would run those twice)
from org import cache, dispatch  # noqa: E402
from org.backends import BackendError  # noqa: E402
from org.backends.incus import IncusBackend  # noqa: E402

ROOT = base.ROOT
CACHE = "org-cache"
LOOKUP = ["list", "--format", "json", "^%s$" % CACHE]
WAIT = 'for i in $(seq 1 60); do ip -4 -o addr show scope global 2>/dev/null | grep -q inet && exit 0; sleep 0.5; done; exit 1'
STUB = """#!/bin/sh
# A stand-in for incus/cache.sh: it says what the real one says, and keeps what it was given.
echo "$ORG_CACHE_PORT $ORG_CACHE_MAX_GB" > "$HOME/was-given"
echo version=2.6.4
echo sha256=ok
echo server=%s
"""
TOOLS = {
    "id": '#!/bin/sh\ncase "$1" in -u) echo 1000 ;; -un) echo org ;; *) exec /usr/bin/id "$@" ;; esac\n',
    "uname": "#!/bin/sh\necho Linux\n",
    "systemctl": "#!/bin/sh\nexit 1\n",
}


def network(*addresses):
    return {"network": {"eth0": {"addresses": [{"family": "inet6", "address": "fd42::1", "netmask": "64", "scope": "global"}]
                                 + [{"family": "inet", "address": a, "netmask": "24", "scope": "global"} for a in addresses]}}}


# The cache's card as `org up` leaves it: a pinned address and no port isolation. The stand-in lays a
# device over its default whole, so the card is given in full; a null inside it would remove nothing.
CARD = {"type": "nic", "name": "eth0", "network": "incusbr0", "security.ipv4_filtering": "true"}


class CacheCase(base.IncusCase):
    def cache_container(self, name=CACHE, address="127.0.0.1", running=True, devices=None, **more):
        """A cache as `org up` leaves it: running, with an address, and a card without port isolation."""
        self.container(name, running=running, state=network(address),
                       expanded_devices=dict(eth0=CARD, **(devices or {})), **more)

    def listening(self, host="127.0.0.1"):
        """A port that accepts connections until the test ends."""
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, 0))
        s.listen(16)
        self.addCleanup(s.close)
        return s.getsockname()[1]

    def closed_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def bazelrc_of(self, container):
        return self.box(container) / "home" / "etc-bazel.bazelrc"

    def about_cache(self, calls=None, name=CACHE):
        return [c for c in (self.calls() if calls is None else calls) if any(name in word for word in c)]

    def only_looked_at(self, name=CACHE):
        # The cache is never created, entered, started or deleted on behalf of a run.
        self.assertEqual([c for c in self.about_cache(name=name) if c[0] != "list"], [])

    def up(self, server="up", refuse="", args=()):
        """`org up` as its user runs it, from a copy of this checkout whose cache.sh is a stub.
        `server` counts on the first call of a test only: a release made in the same second is reused.
        `refuse` names an `incus` command that fails in this call, as when Incus cannot do it."""
        scratch, tools = self.tmp / "checkout", self.tmp / "tools"
        if not scratch.exists():
            for part in ("org", "bin", "incus", "tests"):
                shutil.copytree(ROOT / part, scratch / part, ignore=shutil.ignore_patterns("__pycache__"))
            (scratch / "incus" / "cache.sh").write_text(STUB % server)
            tools.mkdir()
            for tool, text in TOOLS.items():
                (tools / tool).write_text(text)
                (tools / tool).chmod(0o755)
        wrapper = tools / "incus"
        if wrapper.exists():
            wrapper.unlink()
        if refuse:
            wrapper.write_text('#!/bin/sh\ncase "$1" in %s) echo "Error: refused for the test" >&2; exit 1 ;; esac\nexec "%s" "$@"\n'
                               % (refuse, self.bindir / "incus"))
            wrapper.chmod(0o755)
        # The `org` that up.sh installs reads $ORG_HOME, else ~/.org: here it is this test's ledger.
        env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"], ORG_HOME=str(self.ledger.root))
        return subprocess.run(["bash", str(scratch / "incus" / "up.sh")] + list(args), cwd=str(scratch), env=env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=120)

    def dispatched(self, title="CSV export"):
        log = []
        run_id, rec = dispatch.dispatch(self.ledger, self.approved(title), log=log.append)
        self.assertEqual(rec["state"], "dispatched", log)
        return run_id, rec, log


class OrgUpMakesTheCache(CacheCase):
    def made(self, name, port, gb):
        """Every call that names a cache `org up` had to make, word for word and in order."""
        lookup = ["list", "--format", "json", "^%s$" % name]
        return [lookup,
                ["init", "-q", "images:debian/13", name, "-c", "boot.autostart=true"],
                ["config", "device", "override", name, "eth0", "security.ipv4_filtering=true"],
                ["start", "-q", name],
                ["exec", name, "-T", "--", "sh", "-c", WAIT],
                ["exec", name, "-T", "--env", "ORG_CACHE_PORT=%d" % port, "--env", "ORG_CACHE_MAX_GB=%d" % gb, "--", "sh", "-s"],
                lookup]  # the last is the doctor's, at the end of `org up`

    def test_org_up_makes_a_missing_cache_with_exactly_these_calls(self):
        p = self.up()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("template: %s is here" % base.TEMPLATE, p.stdout)
        self.assertIn("build cache: org-cache ready (bazel-remote 2.6.4, port 9092, at most 20 GB)", p.stdout)
        self.assertEqual(self.about_cache(), self.made(CACHE, 9092, 20))
        for c in self.calls():
            if CACHE in " ".join(c):
                for flag in ("security.port_isolation", "security.nesting", "security.privileged"):
                    self.assertFalse(any(flag in word for word in c), c)
        self.assertEqual(json.loads((self.box(CACHE) / "created.json").read_text()),
                         {"image": "images:debian/13", "flags": ["-q", "-c", "boot.autostart=true"]})
        self.assertEqual(self.state(CACHE), "running", "it is left running")
        self.assertEqual((self.box(CACHE) / "home" / "was-given").read_text(), "9092 20\n")

    def test_org_up_hands_the_cache_its_configured_name_port_and_size(self):
        self.configure(cache={"name": "build-cache", "port": 9500, "max_gb": 7})
        p = self.up()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.about_cache(name="build-cache"), self.made("build-cache", 9500, 7))
        self.assertEqual(self.about_cache(), [], "the default name is not used beside the configured one")
        self.assertEqual((self.box("build-cache") / "home" / "was-given").read_text(), "9500 7\n")

    def test_a_cache_that_is_there_is_left_as_it_is_and_a_stopped_one_is_only_started(self):
        self.assertEqual(self.up().returncode, 0)
        (self.box(CACHE) / "home" / "kept").write_text("what it holds\n")
        before = len(self.calls())
        p = self.up()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("build cache: org-cache is here", p.stdout)
        self.assertEqual(self.about_cache(self.calls()[before:]), [LOOKUP] * 3, "two looks by up.sh, one by its doctor")
        self.state(CACHE, "stopped")
        before = len(self.calls())
        self.assertEqual(self.up().returncode, 0)
        self.assertEqual(self.about_cache(self.calls()[before:]), [LOOKUP, LOOKUP, ["start", "-q", CACHE], LOOKUP])
        self.assertEqual(self.state(CACHE), "running")
        self.assertEqual((self.box(CACHE) / "home" / "kept").read_text(), "what it holds\n")

    def test_a_server_that_did_not_come_up_is_removed_and_org_up_fails_after_the_rest(self):
        p = self.up(server="down")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("the server did not come up in org-cache (sha256: ok), so it was removed", p.stderr)
        self.assertIn("default machine", p.stdout, "the doctor ran first: the rest of `org up` happened")
        self.assertFalse(self.box(CACHE).exists())
        self.assertEqual(self.about_cache()[-2:], [["delete", "-q", "-f", CACHE], LOOKUP])

    def test_a_cache_switched_off_is_not_made_and_not_named(self):
        self.configure(cache={"enabled": False})
        p = self.up()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.about_cache(), [])
        self.assertFalse(self.box(CACHE).exists())

    def test_a_cache_named_inside_the_engineers_prefix_is_refused_by_org_up(self):
        self.configure(cache={"name": "org-eng-cache"})
        p = self.up()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("the build cache `org-eng-cache` starts with the engineers' prefix `org-eng-`", p.stderr)
        self.assertEqual([c for c in self.calls() if c[0] in ("init", "launch", "copy")], [])
        self.assertFalse(self.box("org-eng-cache").exists())


    def test_a_cache_incus_will_not_make_or_start_does_not_stop_the_rest_of_org_up(self):
        p = self.up(refuse="init")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("build cache: incus could not make org-cache", p.stderr)
        self.assertIn("default machine", p.stdout, "the doctor, the last step of `org up`, still ran")
        self.assertFalse(self.box(CACHE).exists())
        # Made but not started: it is removed, so that the next `org up` does not take half a cache for the cache.
        p = self.up(refuse="start")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("build cache: org-cache could not be set up and started, so it was removed", p.stderr)
        self.assertIn("default machine", p.stdout)
        self.assertEqual([c for c in self.about_cache() if c[0] in ("init", "delete")],
                         [["init", "-q", "images:debian/13", CACHE, "-c", "boot.autostart=true"], ["delete", "-q", "-f", CACHE]])
        self.assertFalse(self.box(CACHE).exists())

    def test_a_cache_that_is_there_and_will_not_start_is_said_and_never_removed(self):
        self.cache_container(running=False)
        (self.box(CACHE) / "home" / "kept").write_text("what it holds\n")
        p = self.up(refuse="start")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("build cache: org-cache is here but did not start", p.stderr)
        self.assertIn("default machine", p.stdout)
        self.assertEqual((self.box(CACHE) / "home" / "kept").read_text(), "what it holds\n")
        self.assertEqual([c for c in self.about_cache() if c[0] != "list"], [], "the refused start never reached Incus, and nothing else was tried")

    def test_org_up_reads_the_settings_as_dispatch_does_and_makes_nothing_under_a_bad_one(self):
        for bad, said in (({"name": "org_cache"}, "is not a container's name"), ({"port": 8080}, "must be a port"), ({"port": 0}, "must be a port"),
                          ({"port": 70000}, "must be a port"), ({"port": "9092; true"}, "must be a port"), ({"max_gb": "many"}, "whole number")):
            with self.subTest(bad=bad):
                self.configure(cache=bad)
                p = self.up()
                self.assertNotEqual(p.returncode, 0)
                self.assertIn(said, p.stderr)
                self.assertEqual([c for c in self.calls() if c[0] in ("init", "launch", "copy", "exec")], [])
        # A name that is null and a port written as a string are the default name and that port, on both sides.
        odd = {"name": None, "port": "9500"}
        self.configure(cache=odd)
        p = self.up()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual((self.box(CACHE) / "home" / "was-given").read_text(), "9500 20\n")
        seen = cache.settings({"machines": {"incus": {"cache": odd}}})
        self.assertEqual((seen["name"], seen["port"]), (CACHE, 9500))


class OrgUpSetsTheMemoryLimit(CacheCase):
    def test_org_up_memory_writes_the_setting_and_refuses_what_dispatch_would_refuse_before_anything(self):
        for value in ("lots", "05GiB", "4GB", "0MiB", "Auto", ""):
            p = self.up(args=("--memory", value))
            self.assertEqual(p.returncode, 1, value)
            self.assertIn("org: --memory %s is not auto, none or a size" % value, p.stderr)
            self.assertEqual(self.calls(), [], "refused before incus hears of it")
            self.assertNotIn("memory", self.ledger.config()["machines"]["incus"])
        p = self.up(args=("--memory", "4GiB"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.ledger.config()["machines"]["incus"]["memory"], "4GiB")
        self.assertEqual(self.up(args=("--memory", "none")).returncode, 0)
        self.assertEqual(self.ledger.config()["machines"]["incus"]["memory"], "none")


class DispatchOffersTheCache(CacheCase):
    def test_a_cache_incus_describes_oddly_never_stops_a_dispatch(self):
        port = self.listening()
        self.configure(cache={"port": port})
        for i, odd in enumerate((dict(state={"network": {"eth0": {"addresses": [None, "x"]}}}), dict(state="x"),
                                 dict(state=network("127.0.0.1"), expanded_devices=[]), dict(state=network("127.0.0.1"), expanded_config=[]))):
            with self.subTest(odd=odd):
                shutil.rmtree(self.box(CACHE), ignore_errors=True)
                self.container(CACHE, running=True, **odd)
                run_id, rec, log = self.dispatched("Oddly described %d" % i)
                self.assertNotEqual(rec["build_cache"]["state"], "reachable", log)
                self.assertFalse(self.bazelrc_of(rec["machine"]["name"]).exists())

    def test_whatever_goes_wrong_with_the_cache_is_a_line_in_the_log_and_not_an_error(self):
        class Recorder:
            ran = []

            def run(self, script, **how):
                self.ran.append(script)

        def broken(cfg, remote=None):
            raise AttributeError("'NoneType' object has no attribute 'get'")

        real, cache.look = cache.look, broken
        self.addCleanup(setattr, cache, "look", real)
        for again in (False, True):
            log, tr = [], Recorder()
            tr.ran = []
            self.assertEqual(cache.offer(tr, {}, log.append, again), {"state": "error"})
            self.assertEqual(log, ["build cache not used: 'NoneType' object has no attribute 'get'"])
            self.assertEqual(tr.ran, [cache._FORGET] if again else [], "a resumed run is not left with settings for a cache nobody can vouch for")
        rows = []
        cache.doctor({}, lambda ok, name, found, fix="", soft=False: rows.append((ok, name)))
        self.assertEqual(rows, [(False, "build cache `org-cache`")], "the doctor says so in its row")
        self.assertIsNone(cache.pool_line({}))

    def test_a_reachable_cache_is_written_into_the_engineers_bazel_settings(self):
        for i, (host, address) in enumerate((("127.0.0.1", "127.0.0.1"), ("0.0.0.0", "127.0.0.2"))):
            with self.subTest(address=address):
                port = self.listening(host)
                if i:
                    shutil.rmtree(self.box(CACHE))
                self.cache_container(address=address)
                self.configure(cache={"port": port})
                run_id, rec, log = self.dispatched("Run %d" % i)
                name = rec["machine"]["name"]
                said = [line for line in log if "build cache" in line]
                self.assertEqual(said, ["build cache reachable at %s:%d" % (address, port)])
                self.assertLess(log.index("machine: incus %s on vm" % name), log.index(said[0]))
                self.assertLess(log.index(said[0]), log.index("Claude token copied in"))
                text = self.bazelrc_of(name).read_text()
                self.assertEqual(text, cache.bazelrc(address, port))
                lines = text.splitlines()
                self.assertTrue(lines[0].startswith(cache.MARK))
                self.assertEqual([line for line in lines if not line.startswith("#")],
                                 ["build --remote_cache=grpc://%s:%d" % (address, port), "build --remote_upload_local_results=true",
                                  "build --remote_local_fallback", "build --incompatible_remote_local_fallback_for_remote_cache"])
                self.assertNotIn("disk_cache", text)
                self.assertNotIn("jobs", text)
                self.assertEqual(self.ledger.run(run_id)["build_cache"], {"state": "reachable", "address": address, "port": port})
                self.only_looked_at()

    def test_an_unreachable_cache_is_said_and_not_written(self):
        port = self.closed_port()
        self.cache_container()
        self.configure(cache={"port": port})
        run_id, rec, log = self.dispatched()
        said = [line for line in log if "build cache" in line]
        self.assertEqual(len(said), 1, log)
        self.assertIn("not reachable at 127.0.0.1:%d, so builds will not use it" % port, said[0])
        self.assertIn("the VM's firewall", said[0])
        self.assertIn("port isolation", said[0])
        self.assertFalse(self.bazelrc_of(rec["machine"]["name"]).exists())
        self.assertEqual(self.ledger.run(run_id)["build_cache"], {"state": "unreachable", "address": "127.0.0.1", "port": port})
        self.only_looked_at()

    def test_a_stopped_cache_is_said_and_not_started(self):
        self.cache_container(running=False)
        self.configure(cache={"port": self.listening()})
        run_id, rec, log = self.dispatched()
        self.assertEqual([line for line in log if "build cache" in line], ["build cache not used: org-cache is stopped (`org up` starts it)"])
        self.assertFalse(self.bazelrc_of(rec["machine"]["name"]).exists())
        self.assertEqual(self.ledger.run(run_id)["build_cache"], {"state": "stopped"})
        self.assertEqual(self.state(CACHE), "stopped")
        self.only_looked_at()

    def test_an_installation_without_a_cache_behaves_as_before(self):
        run_id, rec, log = self.dispatched()
        self.assertEqual([line for line in log if "cache" in line], [])
        self.assertFalse(self.bazelrc_of(rec["machine"]["name"]).exists())
        self.assertEqual(self.ledger.run(run_id)["build_cache"], {"state": "missing"})
        self.assertEqual(self.about_cache(), [LOOKUP])

    def test_a_cache_switched_off_is_not_even_looked_at(self):
        # A running, reachable cache is there, and still nothing is asked of it.
        self.cache_container()
        self.configure(cache={"enabled": False, "port": self.listening()})
        run_id, rec, log = self.dispatched()
        self.assertEqual([line for line in log if "cache" in line], [])
        self.assertFalse(self.bazelrc_of(rec["machine"]["name"]).exists())
        self.assertNotIn("build_cache", self.ledger.run(run_id))
        self.assertEqual(self.about_cache(), [])

    def test_a_cache_named_inside_the_prefix_is_not_used_and_not_named(self):
        self.cache_container(name="org-eng-cache")
        self.configure(cache={"name": "org-eng-cache", "port": self.listening()})
        self.assertEqual(cache.look(self.ledger.config())["state"], "misnamed")
        run_id, rec, log = self.dispatched()
        self.assertEqual([line for line in log if "build cache" in line],
                         ["build cache not used: its name `org-eng-cache` starts with the engineers' prefix `org-eng-`"])
        self.assertEqual(self.about_cache(name="org-eng-cache"), [])
        self.assertFalse(self.bazelrc_of(rec["machine"]["name"]).exists())
        self.assertEqual(self.ledger.run(run_id)["build_cache"], {"state": "misnamed"})

    def test_a_container_that_fails_vet_is_refused_before_the_cache_is_looked_at(self):
        self.cache_container()
        self.configure(cache={"port": self.listening()})
        self.describe(base.TEMPLATE, expanded_config={"security.privileged": "true"})
        log = []
        with self.assertRaises(dispatch.DispatchError):
            dispatch.dispatch(self.ledger, self.approved(), log=log.append)
        self.assertEqual([line for line in log if "cache" in line], [])
        self.assertEqual(self.about_cache(), [])
        self.assertFalse(self.bazelrc_of("org-eng-001-csv-export-r1").exists())
        self.assertNotIn("build_cache", self.ledger.run("001-csv-export-r1"))

    def test_no_token_reaches_the_cache_container(self):
        self.assertEqual(self.up().returncode, 0)
        self.describe(CACHE, state=network("127.0.0.1"), expanded_devices={"eth0": CARD})
        self.configure(cache={"port": self.listening()})
        run_id, rec, log = self.dispatched()
        self.assertEqual(self.ledger.run(run_id)["build_cache"]["state"], "reachable")
        self.assertIn(base.CLAUDE_TOKEN, (self.box(rec["machine"]["name"]) / "home" / ".run-org" / "claude-token").read_text())
        for path in self.box(CACHE).rglob("*"):
            if path.is_file():
                text = path.read_bytes().decode("utf-8", "replace")
                for token in (base.CLAUDE_TOKEN, base.GITHUB_TOKEN):
                    self.assertNotIn(token, text, path)
        self.assertFalse((self.box(CACHE) / "home" / ".run-org").exists())
        entered = [c for c in self.about_cache() if c[0] == "exec"]
        self.assertEqual(len(entered), 2, "the two of `org up`, and none of the dispatch")
        for c in entered:
            self.assertFalse(any(token in word for word in c for token in (base.CLAUDE_TOKEN, base.GITHUB_TOKEN)), c)


class TheCacheOutlivesRuns(CacheCase):
    def test_removing_a_runs_container_leaves_the_cache_alone(self):
        self.cache_container()
        (self.box(CACHE) / "home" / "marker").write_text("built by an earlier engineer\n")
        self.configure(cache={"port": self.listening()})
        run_id, rec, _ = self.dispatched()
        name = rec["machine"]["name"]
        self.wait_final(run_id)
        self.assertEqual(dispatch.remove_machine(self.ledger, run_id), "deleted " + name)
        self.assertFalse(self.box(name).exists())
        self.assertEqual(self.state(CACHE), "running")
        self.assertEqual((self.box(CACHE) / "home" / "marker").read_text(), "built by an earlier engineer\n")
        self.assertEqual([c for c in self.calls() if c[0] == "delete"], [["delete", "-q", "-f", name]])
        self.only_looked_at()

    def test_a_run_record_naming_the_cache_is_refused_before_incus_hears_of_it(self):
        self.cache_container()
        cfg, machine = self.ledger.config(), {"kind": "incus", "name": CACHE, "base": base.TEMPLATE}
        for action in ("remove", "transport", "before_resume"):
            with self.assertRaises(BackendError, msg=action) as ctx:
                getattr(IncusBackend(), action)(machine, cfg)
            self.assertIn("org only acts on containers named org-eng-<run>", str(ctx.exception))
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.state(CACHE), "running")

    def test_a_resumed_run_is_told_where_the_cache_is_now_or_that_it_is_gone(self):
        first = self.listening()
        self.cache_container()
        self.configure(cache={"port": first})
        run_id, rec, _ = self.dispatched()
        name, rc = rec["machine"]["name"], self.bazelrc_of(rec["machine"]["name"])
        self.wait_final(run_id)
        self.assertIn(":%d\n" % first, rc.read_text())

        # The cache moved to another port: the file follows it, and so does the record.
        moved = self.listening()
        self.configure(cache={"port": moved})
        log = []
        dispatch.resume(self.ledger, run_id, log=log.append)
        self.assertEqual(rc.read_text(), cache.bazelrc("127.0.0.1", moved))
        self.assertEqual(self.ledger.run(run_id)["build_cache"], {"state": "reachable", "address": "127.0.0.1", "port": moved})
        self.assertIn("build cache reachable at 127.0.0.1:%d" % moved, log)
        self.wait_final(run_id)

        # It stopped: what org wrote is taken away, so that no build waits on an address nothing answers at.
        self.state(CACHE, "stopped")
        dispatch.resume(self.ledger, run_id, log=lambda m: None)
        self.assertFalse(rc.exists())
        self.assertEqual(self.ledger.run(run_id)["build_cache"], {"state": "stopped"})
        self.wait_final(run_id)

        # A file org did not write is not org's to remove.
        self.state(CACHE, "running")
        self.configure(cache={"port": self.closed_port()})
        rc.write_text("# the engineer's own\nbuild --jobs=2\n")
        dispatch.resume(self.ledger, run_id, log=lambda m: None)
        self.assertEqual(rc.read_text(), "# the engineer's own\nbuild --jobs=2\n")
        self.assertEqual(self.ledger.run(run_id)["build_cache"]["state"], "unreachable")
        self.wait_final(run_id)
        self.only_looked_at()
        self.assertEqual(self.state(CACHE), "running")
        self.assertTrue(self.box(name).exists())


class TheCacheInDoctorAndPool(CacheCase):
    ROW = "build cache `org-cache`"

    def org(self, *command):
        p = subprocess.run([sys.executable, "-m", "org", "--home", str(self.ledger.root)] + list(command), capture_output=True, text=True,
                           cwd=str(ROOT), stdin=subprocess.DEVNULL, timeout=60)
        return p.stdout

    def rows(self):
        """`org doctor` as a person runs it: {what was checked: (ok, warn or FAIL, what was found)}."""
        self.out = self.org("doctor")
        rows = {m.group(2): (m.group(1), m.group(3)) for m in re.finditer(r"^(ok|warn|FAIL) +(.+?) {2,}(.*)$", self.out, re.M)}
        self.assertIn("default machine", rows, self.out)
        return rows

    def pool_lines(self):
        return [line for line in self.org("pool").splitlines() if line.startswith("build cache")]

    def test_the_doctor_has_one_row_for_the_cache_in_each_state(self):
        rows = self.rows()
        self.assertEqual(rows[self.ROW], ("FAIL", "missing"))
        self.assertRegex(self.out, r"build cache `org-cache` +missing\n +→ org up\n")
        self.configure(cache={"enabled": False})
        self.cache_container(address="10.1.2.3")
        rows = self.rows()
        self.assertEqual(rows["build cache"], ("ok", "switched off (machines.incus.cache.enabled)"))
        self.assertNotIn(self.ROW, rows)
        self.assertEqual(self.about_cache(), [LOOKUP], "only the first doctor looked")
        self.configure()
        self.assertEqual(self.rows()[self.ROW], ("ok", "running at 10.1.2.3:9092"))
        self.state(CACHE, "stopped")
        self.assertEqual(self.rows()[self.ROW][0], "warn")
        self.state(CACHE, "running")
        for description, expected in (({"expanded_config": {"security.privileged": "true"}}, "it is privileged"),
                                      ({"devices": {"share": {"type": "disk", "path": "/mnt", "source": "/home"}}}, "device share (disk)"),
                                      ({"devices": {"door": {"type": "proxy", "listen": "tcp:0.0.0.0:80"}}}, "device door (proxy)")):
            with self.subTest(expected=expected):
                shutil.rmtree(self.box(CACHE))
                self.cache_container(address="10.1.2.3", **description)
                found = self.rows()[self.ROW]
                self.assertEqual(found[0], "FAIL")
                self.assertIn(expected, found[1])
        self.configure(cache={"name": "org-eng-cache"})
        found = self.rows()["build cache `org-eng-cache`"]
        self.assertEqual(found[0], "FAIL")
        self.assertIn("engineers' prefix `org-eng-`", found[1])
        self.assertEqual([c for c in self.calls() if c[0] != "list"], [], "doctor only looks")

    def test_org_pool_names_the_cache_and_where_it_is(self):
        self.assertEqual(self.pool_lines(), ["build cache: org-cache missing (`org up` makes it)"])
        self.cache_container(address="10.1.2.3")
        self.assertEqual(self.pool_lines(), ["build cache: org-cache running at 10.1.2.3:9092"])
        self.state(CACHE, "stopped")
        self.assertEqual(self.pool_lines(), ["build cache: org-cache stopped (`org up` starts it)"])
        self.configure(cache={"enabled": False})
        self.assertEqual(self.pool_lines(), [])
        self.assertEqual([c for c in self.calls() if c[0] != "list"], [])


class ThePureParts(unittest.TestCase):
    def test_the_cache_address_is_its_global_ipv4_address_and_nothing_else(self):
        lo = {"addresses": [{"family": "inet", "address": "127.0.0.1", "scope": "global"}]}
        self.assertEqual(cache.address({"state": {"network": dict(lo=lo, **network("10.1.2.3")["network"])}}), "10.1.2.3")
        self.assertEqual(cache.address({"state": {"network": {"lo": lo}}}), None, "the loopback card is not an address")
        self.assertEqual(cache.address({"state": network()}), None, "IPv6 alone is not one")
        link = {"eth0": {"addresses": [{"family": "inet", "address": "169.254.1.1", "scope": "link"},
                                       {"family": "inet", "address": "not-an-address", "scope": "global"},
                                       {"family": "inet", "address": "10.9.9.9", "scope": "global"}]}}
        self.assertEqual(cache.address({"state": {"network": link}}), "10.9.9.9")
        for nothing in ({}, {"state": None}, {"state": {"network": {}}}, {"state": {"network": {"eth0": None}}}):
            self.assertIsNone(cache.address(nothing), nothing)

    def test_a_plain_disk_and_a_card_without_port_isolation_are_no_fault(self):
        fine = {"expanded_config": {}, "expanded_devices": {"root": {"type": "disk", "path": "/", "pool": "default"},
                                                            "eth0": {"type": "nic", "network": "incusbr0", "security.ipv4_filtering": "true"}}}
        self.assertEqual(cache.faults(fine), [])
        shared = dict(fine, expanded_devices=dict(fine["expanded_devices"], root={"type": "disk", "path": "/", "source": "/srv"}))
        self.assertEqual(len(cache.faults(shared)), 1)

    def test_the_settings_have_these_defaults(self):
        self.assertEqual(cache.settings({}), {"enabled": True, "name": "org-cache", "port": 9092, "max_gb": 20})
        self.assertEqual(cache.settings({"machines": {"incus": {"cache": {"port": 1}}}})["port"], 1)
        self.assertEqual(cache.settings({"machines": {"incus": {"cache": "nonsense"}}})["name"], "org-cache")

    def test_the_settings_are_read_as_org_up_reads_them(self):
        def read(**given):
            return cache.settings({"machines": {"incus": {"cache": given}}})

        # `org up` skips the cache only for a plain false, so only that switches it off here.
        self.assertEqual([read(enabled=v)["enabled"] for v in (False, True, None, 0, "", "false")], [False, True, True, True, True, True])
        self.assertEqual((read(name=None)["name"], read(name="")["name"], read(port="9500")["port"], read(port=None)["port"]),
                         ("org-cache", "org-cache", 9500, 9092))
        for nothing in ({"machines": None}, {"machines": {"incus": None}}, {"machines": {"incus": []}}, {"machines": {"incus": {"cache": None}}}):
            self.assertEqual(cache.settings(nothing), cache.DEFAULTS, nothing)
        for name in ("org_cache", "-cache", "a b", "x" * 64, "$(reboot)"):
            self.assertEqual(cache.look({"machines": {"incus": {"cache": {"name": name}}}})["state"], "misnamed", name)

    def test_whatever_shape_incus_gives_is_an_answer_and_never_an_error(self):
        for odd in ({"state": "x"}, {"state": {"network": "x"}}, {"state": {"network": {"eth0": "x"}}}, {"state": {"network": {"eth0": {"addresses": "x"}}}},
                    {"state": {"network": {"eth0": {"addresses": [None, 3, {"family": "inet", "scope": "global", "address": None}]}}}}):
            self.assertIsNone(cache.address(odd), odd)
        for odd in ({}, {"expanded_config": [], "expanded_devices": {}}, {"expanded_config": {}, "expanded_devices": []}):
            self.assertEqual(len(cache.faults(odd)), 1, odd)
        self.assertEqual(len(cache.faults({"expanded_config": {}, "expanded_devices": {"a": None, "b": "x"}})), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
