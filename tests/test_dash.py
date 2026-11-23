"""The dashboard server: what it serves, and who it refuses."""
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import demo_ledger  # noqa: E402
from org import dash, manager, spec  # noqa: E402
from org.util import read_json, write_json  # noqa: E402

PAGE = ROOT / "org" / "web" / "index.html"

# No test may meet the real tokens (/run/org where org is installed): until a test names a
# directory of its own, they are nowhere.
os.environ["ORG_SECRETS_DIR"] = os.path.join(tempfile.gettempdir(), "org-tests-no-secrets")


class DashCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="org-dash-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.old_env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.old_env)))
        scen = self.tmp / "scen"
        scen.mkdir()
        (scen / "scenario.json").write_text(json.dumps({"turns": {"ask": {"say": "The export run is on milestone 3 of 5.", "cost": 0.04}}}))
        # No real claude and no real incus: where this suite runs there may be an Incus with a
        # template in it, and an approval from the page must not make a container there. The
        # simulated one has no template, so a dispatch gets as far as asking for it.
        bindir = self.tmp / "bin"
        bindir.mkdir()
        os.symlink(ROOT / "tests" / "fake_incus.py", bindir / "incus")
        os.environ.update(FAKE_CLAUDE_SCENARIO=str(scen / "scenario.json"), HOME=str(self.tmp / "home"),
                          PATH=str(bindir) + os.pathsep + "/usr/bin:/bin:/usr/sbin", FAKE_INCUS_ROOT=str(self.tmp / "incus"),
                          ORG_SECRETS_DIR=str(self.tmp / "run-org"))
        os.environ.pop("FAKE_INCUS_BASE", None)
        (self.tmp / "home").mkdir()
        self.ledger = demo_ledger.build(self.tmp / "ledger")
        cfg = read_json(self.ledger.config_path)
        cfg["claude_bin"] = str(ROOT / "tests" / "fake_claude.py")
        write_json(self.ledger.config_path, cfg)
        self.server = dash.make_server(self.ledger, 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None, token=True, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"X-Org-Token": self.server.token} if token else {}
        if body is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        return r.status, raw, r.getheader("Content-Type") or ""

    def get(self, path, **kw):
        status, raw, _ = self.call("GET", path, **kw)
        return status, json.loads(raw)

    def post(self, path, body, **kw):
        status, raw, _ = self.call("POST", path, body=body, **kw)
        return status, json.loads(raw)

    # ------------------------------------------------------------ the page
    def test_page_is_one_self_contained_file(self):
        status, raw, ctype = self.call("GET", "/", token=False)
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        html = raw.decode()
        self.assertNotIn(self.server.token, html, "the token is never written into the page")
        # nothing is fetched from anywhere else: no CDN, no fonts, no analytics
        for attr in re.findall(r'(?:src|href)\s*=\s*["\']([^"\']+)', html):
            self.assertFalse(attr.startswith(("http:", "https:", "//")), attr)
        self.assertNotIn("@import", html)

    @unittest.skipUnless(shutil.which("node", path=os.environ.get("ORG_TEST_NODE_PATH", "") + os.pathsep + "/usr/bin:/usr/local/bin"), "node not installed")
    def test_page_script_parses(self):
        node = shutil.which("node", path=os.environ.get("ORG_TEST_NODE_PATH", "") + os.pathsep + "/usr/bin:/usr/local/bin")
        js = re.search(r"<script>(.*)</script>", PAGE.read_text(), re.S).group(1)
        script = self.tmp / "page.js"
        script.write_text(js)
        p = subprocess.run([node, "--check", str(script)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    # ------------------------------------------------------------ who gets in
    def test_api_needs_the_token(self):
        self.assertEqual(self.get("/api/state", token=False)[0], 401)
        self.assertEqual(self.get("/api/state", token=False, headers={"X-Org-Token": "guess"})[0], 401)
        self.assertEqual(self.post("/api/stop", {"run": "007"}, token=False)[0], 401)
        self.assertEqual(self.get("/api/state")[0], 200)

    def test_requests_not_addressed_to_this_machine_are_refused(self):
        # A page on another site can make the browser send requests here. It cannot make them
        # carry this machine's name as Host (DNS rebinding) or its own Origin as ours.
        self.assertEqual(self.get("/api/state", headers={"Host": "attacker.example.test"})[0], 403)
        self.assertEqual(self.call("GET", "/", token=False, headers={"Host": "attacker.example.test"})[0], 403)
        self.assertEqual(self.post("/api/stop", {"run": "007"}, headers={"Origin": "https://attacker.example.test"})[0], 403)
        self.assertEqual(self.post("/api/approve", {"spec": "001", "sha": "x" * 12}, headers={"Origin": "http://127.0.0.1:1"})[0], 403)
        ok = {"Origin": "http://127.0.0.1:%d" % self.port}
        self.assertEqual(self.post("/api/approve", {"spec": "001", "sha": "0" * 12}, headers=ok)[0], 400, "our own origin gets through to the real check")

    def test_token_is_private_and_stable(self):
        path = self.ledger.chat / "dash-token"
        self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertEqual(dash.load_token(self.ledger), self.server.token, "restarting keeps the URL valid")

    # ------------------------------------------------------------ what it serves
    def test_state_matches_the_ledger(self):
        status, state = self.get("/api/state")
        self.assertEqual(status, 200)
        by_id = {v["run_id"]: v for v in state["runs"]}
        self.assertEqual(len(by_id), 7)
        self.assertEqual(by_id["007-csv-export-r1"]["progress"]["done"], 2)
        self.assertEqual(by_id["010-flaky-tests-r1"]["health"], "unreachable")
        self.assertEqual(by_id["005-payments-webhooks-r1"]["state"], "partial")
        self.assertAlmostEqual(by_id["009-search-index-r1"]["weekly_pct"], 31.4 / 4.0)
        self.assertEqual([d["id"] for d in state["drafts"]], ["001-per-key-rate-limits-for-the", "002-make-onboarding-feel-faster"])
        self.assertEqual(state["budget"]["weekly"]["utilization"], 0.62)
        self.assertFalse(state["runs"][0]["terminal"], "live runs are listed first")

    def test_documents(self):
        self.assertIn("## Approach", self.get("/api/doc?run=007&name=plan")[1]["markdown"])
        self.assertIn("still planning", self.get("/api/doc?run=008&name=plan")[1]["markdown"])
        self.assertIn("## Outcome", self.get("/api/doc?run=006-readme-refresh-r1&name=report")[1]["markdown"])
        self.assertIn("has not finished", self.get("/api/doc?run=007&name=report")[1]["markdown"])
        self.assertEqual(self.get("/api/doc?run=nope&name=plan")[0], 404)
        status, draft = self.get("/api/draft?spec=1")
        self.assertEqual((status, draft["errors"], draft["sha"]), (200, [], spec.draft_sha(self.ledger, draft["id"])))
        self.assertTrue(self.get("/api/draft?spec=2")[1]["errors"])

    # ------------------------------------------------------------ what it can do
    def test_approval_is_bound_to_the_text_shown(self):
        _, draft = self.get("/api/draft?spec=1")
        status, out = self.post("/api/approve", {"spec": draft["id"], "sha": "0" * 12})
        self.assertEqual(status, 400)
        self.assertIn("sha mismatch", out["error"])
        self.assertIsNone(self.ledger.spec_meta(draft["id"]))
        status, out = self.post("/api/approve", {"spec": "2", "sha": self.get("/api/draft?spec=2")[1]["sha"]})
        self.assertEqual(status, 400, "a draft that fails its checks is not approvable from the page either")
        # The right hash approves. There is no template container here, so the dispatch then
        # fails, and the page is told both halves.
        status, out = self.post("/api/approve", {"spec": draft["id"], "sha": draft["sha"]})
        self.assertEqual((status, out["approved"], out["dispatched"]), (200, True, False))
        self.assertIn("the template container org-base does not exist", out["error"])
        made = [json.loads(line) for line in (self.tmp / "incus" / "calls.jsonl").read_text().splitlines()]
        self.assertEqual([c[0] for c in made], ["list"], "it asked for the template and made nothing")
        self.assertEqual(self.ledger.spec_meta(draft["id"])["sha256"], draft["sha"])
        runs = self.get("/api/state")[1]["runs"]
        self.assertEqual([v["state"] for v in runs if v["spec_id"] == draft["id"]], ["dispatch_failed"])

    def test_question_streams_and_is_read_only(self):
        self.assertEqual(self.post("/api/ask", {"question": "  "})[0], 400)
        status, raw, ctype = self.call("POST", "/api/ask", body={"question": "What is the export run doing?"})
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", ctype)
        frames = [json.loads(f[6:]) for f in raw.decode().split("\n\n") if f.startswith("data: ")]
        self.assertEqual("".join(f.get("t", "") for f in frames), "The export run is on milestone 3 of 5.")
        self.assertEqual(frames[-1]["done"], True)
        self.assertIsNone(frames[-1]["error"])
        self.assertAlmostEqual(frames[-1]["cost_usd"], 0.04)
        call = json.loads((self.tmp / "scen" / "invocations.jsonl").read_text().splitlines()[-1])
        self.assertEqual(call["opts"]["--permission-mode"], "dontAsk", "anything that would need permission is denied")
        self.assertIn("State as of", call["prompt"])
        self.assertIn("007-csv-export-r1", call["prompt"], "each question carries the current state")
        self.assertIn("$40.00", call["prompt"], "including each run's cap")
        allowed = json.loads(call["opts"]["--settings"])["permissions"]
        self.assertIn("Bash(org peek *)", allowed["allow"])
        self.assertFalse([r for r in allowed["allow"] if "approve" in r or "stop" in r or "Edit" in r or "dispatch" in r])
        # a second question continues the same conversation
        self.call("POST", "/api/ask", body={"question": "And the search index?"})
        second = json.loads((self.tmp / "scen" / "invocations.jsonl").read_text().splitlines()[-1])
        self.assertEqual(second["opts"].get("--resume"), call["opts"].get("--session-id"))


class ManagerSettings(unittest.TestCase):
    """The rules the manager's interactive session runs under."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="org-mgr-settings-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        from org.ledger import Ledger

        self.ledger = Ledger(self.tmp / "ledger")
        self.ledger.init()

    def test_talk_mode_confines_writes_to_the_desk(self):
        s = manager.settings(self.ledger, "talk", with_sandbox=True)
        p = s["permissions"]
        desk = str(self.ledger.desk)
        self.assertIn("Edit(/%s/**)" % desk, p["allow"])
        self.assertTrue(("Edit(/%s/**)" % desk).startswith("Edit(//"), "an absolute path in a rule carries a second slash")
        self.assertTrue(p["blockWritesOutsideWorkingDirectories"])
        self.assertIn("Edit(/%s/specs/**)" % self.ledger.root, p["deny"], "approved specs cannot be edited")
        self.assertEqual(s["sandbox"]["enabled"], True)
        self.assertEqual(s["sandbox"]["failIfUnavailable"], True, "a session started on the strength of the sandbox does not run without it")
        self.assertEqual(s["sandbox"]["allowUnsandboxedCommands"], False, "no escape hatch out of the sandbox")
        self.assertIn("org *", s["sandbox"]["excludedCommands"])

    def test_without_a_sandbox_the_session_asks_the_person_first(self):
        # With no sandbox, only a prompt stands between the manager and a write to a checkout,
        # and in Claude Code's auto mode a classifier would answer that prompt. Seen live: it
        # allowed a `git branch` in a repository outside the desk. So the mode is pinned.
        self.assertNotIn("sandbox", manager.settings(self.ledger, "talk", with_sandbox=False))
        self.assertEqual(manager.session_mode(self.ledger, with_sandbox=False), "manual")
        self.assertIsNone(manager.session_mode(self.ledger, with_sandbox=True), "with the sandbox, any mode is safe")
        write_json(self.ledger.config_path, {"manager": {"permission_mode": "auto"}})
        self.assertEqual(manager.session_mode(self.ledger, with_sandbox=False), "auto", "an explicit choice is the person's to make")
        write_json(self.ledger.config_path, {"manager": {"sandbox": False}})
        self.assertFalse(manager.sandboxed(self.ledger))

    def test_approval_always_asks_the_person(self):
        for mode in ("talk", "ask"):
            for with_sandbox in (True, False):
                p = manager.settings(self.ledger, mode, with_sandbox=with_sandbox)["permissions"]
                for cmd in ("approve", "dispatch", "stop", "machine rm", "exec"):
                    self.assertIn("Bash(org %s *)" % cmd, p["ask"], cmd)
                    self.assertNotIn("Bash(org %s *)" % cmd, p["allow"], cmd)
                self.assertIn("Bash(git push *)", p["deny"])
                self.assertIn("Bash(git * push *)", p["deny"], "the same push with options before the verb")
                self.assertIn("Bash(gh pr create *)", p["deny"])
                self.assertFalse([r for r in p["allow"] if "git -C" in r], "no allow rule with a wildcard before the git verb")
                self.assertTrue([r for r in p["deny"] if r.startswith("Read(/") and "oauth-token" in r], "the login token is not readable")
                self.assertIn("Read(/%s/**)" % os.environ["ORG_SECRETS_DIR"], p["deny"], "nor are the tokens this side was handed")
                # What stands between the chat and the machine that holds it, and any tool from outside.
                for rule in ("Bash(incus *)", "Bash(incus)", "Bash(sudo *)", "Bash(docker *)", "mcp__*"):
                    self.assertIn(rule, p["deny"], rule)
        self.assertNotIn("sandbox", manager.settings(self.ledger, "ask"), "question mode runs nothing that needs one")

    def test_persona_names_the_desk_and_the_state(self):
        text = manager.persona(self.ledger)
        self.assertIn(str(self.ledger.desk), text)
        self.assertIn("No runs yet.", text)
        self.assertNotIn("{{", text)

    def test_the_manager_can_fetch_a_repository_and_knows_what_the_sandbox_hides(self):
        talk = manager.settings(self.ledger, "talk", with_sandbox=True)["permissions"]
        self.assertIn("Bash(org clone *)", talk["allow"])
        self.assertIn("Bash(org project add *)", talk["allow"])
        self.assertNotIn("Bash(org clone *)", manager.settings(self.ledger, "ask")["permissions"]["allow"], "question mode writes nothing")
        plain, sandboxed = manager.persona(self.ledger), manager.persona(self.ledger, with_sandbox=True)
        for text in (plain, sandboxed):
            self.assertIn("`org clone <owner/name>` puts a public repository there, as `%s/scratch/<name>`" % self.ledger.desk, text)
            self.assertIn("org project add <name> --repo <owner/name>", text)
            self.assertNotIn("{{", text)
        # A sandboxed shell has no GitHub login, and an `org` command that is not the whole
        # command runs inside the sandbox too. The manager has to be told, or it reads the
        # failures as something else; where there is no sandbox there is nothing to tell.
        self.assertIn("Your shell commands run in a sandbox with no GitHub login", sandboxed)
        self.assertIn("An `org` command must be the whole command", sandboxed)
        self.assertNotIn("Your shell commands run in a sandbox", plain)

    def test_persona_offers_the_machines_this_installation_has(self):
        text = manager.persona(self.ledger)
        self.assertIn("`machine` is `incus` (a fresh container for the run, copied from the template), the default.", text)
        self.assertNotIn("docker", manager.machines_help(self.ledger.config()))
        self.assertNotIn("ssh:", manager.machines_help(self.ledger.config()))
        write_json(self.ledger.config_path, {"machines": {"plugins": ["org.backends.docker"], "default": "docker",
                                                           "ssh": {"gpu": {"host": "me@gpu"}}}})
        said = manager.machines_help(self.ledger.config())
        self.assertTrue(said.startswith("`machine` is `docker` (a fresh container"), said)
        self.assertIn("the default; or `incus`", said)
        self.assertIn("`ssh:gpu`", said)

    def test_first_run_screens_are_answered_only_in_a_manager_org_made(self):
        home = self.tmp / "home"
        home.mkdir()
        old = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old)))
        os.environ["HOME"] = str(home)
        for key in ("ORG_IN_CONTAINER", "CLAUDE_CONFIG_DIR"):
            os.environ.pop(key, None)
        manager.prepare_own_session(self.ledger, "true")
        self.assertFalse((home / ".claude.json").exists(), "on the person's own machine their Claude Code is left alone")
        write_json(self.ledger.config_path, {"manager": {"own_claude": True}})
        manager.prepare_own_session(self.ledger, "true")
        data = read_json(home / ".claude.json")
        self.assertTrue(data["hasCompletedOnboarding"])
        self.assertTrue(data["projects"][str(self.ledger.desk)]["hasTrustDialogAccepted"])
        self.assertEqual(os.stat(home / ".claude.json").st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
