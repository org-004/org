"""A web server in an engineer's container, seen through the dashboard: who gets to it, and what it gets.

The dashboard, the proxy and the relay are real. Incus is simulated (tests/fake_incus.py): its
`exec` runs the relay on this machine, so "a port on the container's loopback" is a server this
test starts on 127.0.0.1.
"""
import contextlib
import http.client
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from org import cli, dash, manager, view  # noqa: E402
from org.ledger import DEFAULT_CONFIG, Ledger  # noqa: E402
from org.util import write_json  # noqa: E402

# No test may meet the real tokens (/run/org where org is installed): until a test names a
# directory of its own, they are nowhere.
os.environ["ORG_SECRETS_DIR"] = os.path.join(tempfile.gettempdir(), "org-tests-no-secrets")

# The kernel's count of memory kills is read from $ORG_CGROUP_DIR: this machine's own count may be above zero, and no test may depend on it.
os.environ["ORG_CGROUP_DIR"] = os.path.join(tempfile.gettempdir(), "org-tests-no-cgroup")

RUN = "001-viewer-r1"


class App(BaseHTTPRequestHandler):
    """What an engineer might be running. It keeps every request it is sent."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _keep(self, body=b""):
        self.server.seen.append({"method": self.command, "path": self.path, "headers": list(self.headers.items()), "body": body})

    def _reply(self, code, body, headers=()):
        self.send_response(code)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._keep()
        if self.path == "/own-policy":
            return self._reply(200, b"strict", [("Content-Security-Policy", "default-src 'self'"), ("Referrer-Policy", "same-origin"),
                                                ("X-Content-Type-Options", "nosniff")])
        if self.path == "/cookies":
            return self._reply(200, b"ok", [("Set-Cookie", "org-view=taken-over; Path=/"), ("Set-Cookie", "session=abc; Path=/")])
        if self.path == "/stream":  # no length: the body is whatever comes before the connection closes
            self.send_response(200)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"part one, ")
            self.wfile.flush()
            time.sleep(0.2)
            self.wfile.write(b"part two")
            return
        self._reply(200, b"the engineer's app at " + self.path.encode())

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._keep(body)
        self._reply(200, b"got " + body)


def header(seen, name):
    return [v for k, v in seen["headers"] if k.lower() == name.lower()]


class ViewCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="org-view-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.old_env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.old_env)))
        bindir = self.tmp / "bin"
        bindir.mkdir()
        os.symlink(ROOT / "tests" / "fake_incus.py", bindir / "incus")
        self.incus = self.tmp / "incus"
        os.environ.update(HOME=str(self.tmp / "home"), PATH=str(bindir) + os.pathsep + "/usr/bin:/bin:/usr/sbin",
                          FAKE_INCUS_ROOT=str(self.incus), ORG_SECRETS_DIR=str(self.tmp / "run-org"))
        for name in ("ORG_HOME", "FAKE_INCUS_BASE", "ORG_DASH_PUBLIC_PORT", "ORG_IN_CONTAINER"):
            os.environ.pop(name, None)
        (self.tmp / "home").mkdir()
        self.ledger = Ledger(self.tmp / "ledger")
        self.ledger.init()
        self.configure(True)
        self.run_record(RUN)
        self.container("org-eng-" + RUN)

        self.app = ThreadingHTTPServer(("127.0.0.1", 0), App)
        self.app.daemon_threads = True
        self.app.seen = []
        self.app_port = self.app.server_address[1]
        threading.Thread(target=self.app.serve_forever, daemon=True).start()
        self.addCleanup(self.app.server_close)
        self.addCleanup(self.app.shutdown)

        self.server = dash.make_server(self.ledger, 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    # ------------------------------------------------------------ what the test sets up
    def configure(self, on, **incus):
        write_json(self.ledger.config_path, {"dash": {"view": on}, "machines": {"incus": incus}})

    def run_record(self, run_id, kind="incus", name=None, **over):
        rec = {"run_id": run_id, "spec_id": run_id.rsplit("-r", 1)[0], "spec_version": 1, "title": run_id, "state": "executing",
               "machine": {"kind": kind, "name": name or "org-eng-" + run_id}, "remote_dir": "$HOME/org-runs/" + run_id,
               "dispatched_at": time.time() - 60, "final": False}
        rec.update(over)
        self.ledger.save_run(run_id, rec)

    def container(self, name):
        (self.incus / name / "home").mkdir(parents=True)
        (self.incus / name / "state").write_text("running\n")

    def execs(self):
        """Every `incus exec` that was made: [container, the command's last two words]."""
        path = self.incus / "calls.jsonl"
        calls = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [[c[1], c[-2:]] for c in calls if c and c[0] == "exec"]

    def raw_upstream(self, serve):
        """A listener that hands each connection to `serve`, for what an HTTP library will not do."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        self.addCleanup(srv.close)

        def loop():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                threading.Thread(target=serve, args=(conn,), daemon=True).start()

        threading.Thread(target=loop, daemon=True).start()
        return srv.getsockname()[1]

    # ------------------------------------------------------------ what a browser does
    def call(self, method, path, host=None, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = dict(headers or {})
        if host:
            h["Host"] = host
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        return r.status, r.msg, raw

    def api(self, path, body=None, host=None, token=True):
        h = {"X-Org-Token": self.server.token} if token else {}
        if body is not None:
            h["Content-Type"] = "application/json"
        status, _, raw = self.call("POST" if body is not None else "GET", path, host=host, headers=h,
                                   body=json.dumps(body) if body is not None else None)
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw

    def opened(self, run=RUN, port=None):
        """(the view's host, the path that carries its ticket), as the dashboard's page is given them."""
        status, out = self.api("/api/view", {"run": run, "port": port or self.app_port})
        self.assertEqual(status, 200, out)
        url = urlsplit(out["url"])
        self.assertEqual((url.scheme, url.netloc), ("http", out["host"]))
        return url.netloc, url.path + "?" + url.query

    def entered(self, run=RUN, port=None):
        """(the view's host, the Cookie header a browser would now send it)."""
        host, ticket_path = self.opened(run, port)
        status, msg, _ = self.call("GET", ticket_path, host=host)
        self.assertEqual(status, 200)
        return host, msg["Set-Cookie"].split(";")[0]

    def relays_released(self):
        end = time.time() + 10
        while time.time() < end and self.server.views.relays():
            time.sleep(0.05)
        return self.server.views.relays() == 0


class MemoryKills(ViewCase):
    LINE = "%s: 2 processes were killed for memory in its container (limit 2219MiB)" % RUN

    def killed(self, count, limit="2219MiB"):
        self.run_record(RUN, oom_kill=count, machine={"kind": "incus", "name": "org-eng-" + RUN, "limits": {"memory": limit} if limit else {}})

    def org(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["--home", str(self.ledger.root)] + list(argv))
        return code, out.getvalue()

    def test_a_run_with_kills_is_flagged_in_status_and_peek_and_carried_in_json(self):
        self.killed(2)
        code, out = self.org("status", "--no-sync")
        self.assertEqual(code, 0)
        self.assertIn("  ! " + self.LINE, out.splitlines())
        code, out = self.org("peek", RUN)
        self.assertIn("warning:   2 processes were killed for memory in its container (limit 2219MiB)", out.splitlines())
        code, out = self.org("status", "--no-sync", "--json")
        run = json.loads(out)["runs"][0]
        self.assertEqual((run["oom_kill"], run["memory_limit"]), (2, "2219MiB"))

    def test_one_kill_is_in_the_singular_and_an_unknown_limit_leaves_out_the_parenthesis(self):
        self.killed(1, limit=None)
        _, out = self.org("status", "--no-sync")
        self.assertIn("  ! %s: 1 process was killed for memory in its container" % RUN, out.splitlines())
        self.assertNotIn("(limit", out)
        self.assertIsNone(json.loads(self.org("status", "--no-sync", "--json")[1])["runs"][0]["memory_limit"])

    def test_no_kills_or_no_count_print_no_line_and_zero_stays_zero_in_json(self):
        for count in (0, None):
            self.killed(count)
            for argv in (("status", "--no-sync"), ("peek", RUN)):
                self.assertNotIn("killed for memory", self.org(*argv)[1])
            self.assertEqual(json.loads(self.org("status", "--no-sync", "--json")[1])["runs"][0]["oom_kill"], count)
        self.run_record(RUN)  # a record from before the count existed
        self.assertIsNone(json.loads(self.org("status", "--no-sync", "--json")[1])["runs"][0]["oom_kill"])


class WhoGetsIn(ViewCase):
    def test_it_is_off_unless_turned_on(self):
        self.assertIs(DEFAULT_CONFIG["dash"]["view"], False)
        host, cookie = self.entered()
        self.assertIs(self.api("/api/state")[1]["view"], True)
        self.configure(False)
        self.assertIs(self.api("/api/state")[1]["view"], False, "the page is told, and offers nothing")
        status, out = self.api("/api/view", {"run": RUN, "port": self.app_port})
        self.assertEqual(status, 403)
        self.assertIn("dash.view", out["error"])
        self.assertEqual(self.call("GET", "/", host=host, headers={"Cookie": cookie})[0], 404, "a view opened earlier is closed too")
        self.assertEqual(self.call("GET", "/", host="v-%s.localhost:%d" % ("0" * 20, self.port))[0], 404)
        self.assertEqual(self.execs(), [], "nothing was sent into a container")
        write_json(self.ledger.config_path, {"dash": {"view": "yes"}})
        self.assertEqual(self.api("/api/view", {"run": RUN, "port": self.app_port})[0], 403, "only true turns it on")

    def test_a_ticket_works_once(self):
        host, ticket_path = self.opened()
        self.assertRegex(host, r"^v-[0-9a-f]{20}\.localhost:%d$" % self.port)
        self.assertLess(len(host.split(".")[0]), 63)
        self.assertEqual(self.call("POST", ticket_path, host=host, body=b"")[0], 403, "a ticket is spent by visiting, not by a form")
        status, msg, raw = self.call("GET", ticket_path, host=host)
        self.assertEqual(status, 200)
        # A page of the view's own that goes to /, not a redirect: see view.serve for why.
        self.assertIn(b'http-equiv="refresh" content="0;url=/"', raw)
        self.assertEqual(msg["Referrer-Policy"], "no-referrer", "the ticket's URL is not passed on to the app")
        self.assertEqual(msg["Cache-Control"], "no-store")
        cookie = msg.get_all("Set-Cookie")
        self.assertEqual(len(cookie), 1)
        name, _, value = cookie[0].split(";")[0].partition("=")
        attrs = sorted(a.strip().lower() for a in cookie[0].split(";")[1:])
        self.assertEqual(name, "org-view")
        self.assertGreaterEqual(len(value), 32)
        self.assertEqual(attrs, ["httponly", "path=/", "samesite=strict"], "host-only: no Domain")
        status, msg, raw = self.call("GET", ticket_path, host=host)
        self.assertEqual((status, raw, msg.get_all("Set-Cookie")), (403, b"", None), "the second visit gets nothing")
        self.assertEqual(self.app.seen, [], "and none of this reached the app")

    def test_a_ticket_expires_and_belongs_to_one_view(self):
        host, ticket_path = self.opened()
        other = self.raw_upstream(lambda conn: conn.close())
        other_host, other_path = self.opened(port=other)
        self.assertNotEqual(host, other_host, "each (run, port) has a hostname of its own")
        self.assertEqual(self.call("GET", other_path, host=host)[0], 403, "another view's ticket")
        self.assertEqual(self.call("GET", view.OPEN_PATH + "?ticket=guess", host=host)[0], 403)
        self.assertEqual(self.call("GET", view.OPEN_PATH, host=host)[0], 403)
        tickets = self.server.views.get(host[2:22])["tickets"]
        self.assertEqual(len(tickets), 1)
        for known in tickets:
            self.assertLessEqual(tickets[known] - time.time(), 60)
            tickets[known] = time.time() - 1  # a minute later
        self.assertEqual(self.call("GET", ticket_path, host=host)[0], 403)
        # Asking again for the same run and port gives the same hostname and a new ticket.
        again, fresh = self.opened()
        self.assertEqual(again, host)
        self.assertNotEqual(fresh, ticket_path)
        self.assertEqual(self.call("GET", fresh, host=host)[0], 200)

    def test_opening_takes_the_dashboards_token_on_the_dashboards_own_host(self):
        body = {"run": RUN, "port": self.app_port}
        self.assertEqual(self.api("/api/view", body, token=False)[0], 401)
        status, _, _ = self.call("POST", "/api/view", body=json.dumps(body),
                                 headers={"X-Org-Token": self.server.token, "Origin": "http://v-%s.localhost:%d" % ("a" * 20, self.port)})
        self.assertEqual(status, 403, "a view's page is another origin, and the dashboard's check of Origin is as it was")
        self.assertEqual(self.api("/api/view", body, host="attacker.example.test")[0], 403)
        self.assertEqual(self.api("/api/view", {"run": "nope", "port": self.app_port})[0], 404)

    def test_without_the_cookie_nothing_is_relayed(self):
        host, cookie = self.entered()
        for headers in ({}, {"Cookie": "org-view=guess"}, {"Cookie": "other=" + cookie.split("=", 1)[1]},
                        {"X-Org-Token": self.server.token}, {"Cookie": "org-view=" + self.server.token}):
            status, _, raw = self.call("GET", "/", host=host, headers=headers)
            self.assertEqual((status, raw), (403, b""), headers)
        self.assertEqual(self.call("POST", "/submit", host=host, body=b"x=1")[0], 403)
        self.assertEqual((self.app.seen, self.execs()), ([], []))

    def test_a_made_up_or_misspelt_host_is_not_found(self):
        host, cookie = self.entered()
        vid = host[2:22]
        made_up = ["v-%s.localhost:%d" % ("0" * 20, self.port),             # no such view
                   "v-%s.localhost.attacker.test:%d" % (vid, self.port),    # the pattern is anchored at its end
                   "v-%s.localhost:%d" % (vid, 1 if self.port != 1 else 2),  # not this dashboard's port
                   "v-%s.localhost" % vid, "V-%s.LOCALHOST:%d" % (vid.upper(), self.port),
                   "v-%s.localhost:%d" % (vid[:-1], self.port), "v-.localhost:%d" % self.port, "v-"]
        for name in made_up:
            for path in ("/", "/api/state", view.OPEN_PATH):
                status, _, raw = self.call("GET", path, host=name, headers={"Cookie": cookie, "X-Org-Token": self.server.token})
                self.assertEqual((status, raw), (404, b""), (name, path))
        # At its start too: this is no view's name, so it is the dashboard's to refuse, as it does any stranger.
        self.assertEqual(self.api("/api/state", host="xv-%s.localhost:%d" % (vid, self.port))[0], 403)
        self.assertEqual((self.app.seen, self.execs()), ([], []))

    def test_only_a_running_run_in_one_of_orgs_containers(self):
        self.run_record("002-finished-r1", state="done", final=True)
        self.run_record("003-deleted-r1", machine_removed_at=time.time())
        self.run_record("004-elsewhere-r1", kind="ssh", name="bigbox")
        self.run_record("005-stranger-r1", name="somebody-elses-container")
        self.run_record("006-queued-r1", state="queued")
        self.run_record("007-local-r1", kind="local", name="localhost")
        self.run_record("008-template-r1", name="org-base")
        for run_id, why in (("002-finished-r1", "not running"), ("003-deleted-r1", "deleted"), ("004-elsewhere-r1", "not in an Incus container"),
                            ("005-stranger-r1", "org only acts on containers named org-eng-"), ("006-queued-r1", "not running"),
                            ("007-local-r1", "not in an Incus container"), ("008-template-r1", "org only acts on containers named")):
            self.container(self.ledger.run(run_id)["machine"]["name"])  # it is there to be reached, and is not
            status, out = self.api("/api/view", {"run": run_id, "port": self.app_port})
            self.assertEqual(status, 409, run_id)
            self.assertIn(why, out["error"], run_id)
        self.assertEqual(self.execs(), [])
        # The prefix is the configuration's, read when the view is asked for.
        self.configure(True, prefix="other-")
        self.assertEqual(self.api("/api/view", {"run": RUN, "port": self.app_port})[0], 409)

    def test_a_view_ends_when_its_run_does(self):
        host, cookie = self.entered()
        self.assertEqual(self.call("GET", "/", host=host, headers={"Cookie": cookie})[0], 200)
        self.run_record(RUN, state="done", final=True)
        self.assertEqual(self.call("GET", "/", host=host, headers={"Cookie": cookie})[0], 404)
        self.run_record(RUN)
        self.assertEqual(self.call("GET", "/", host=host, headers={"Cookie": cookie})[0], 404, "and does not come back with it")
        self.assertEqual(len(self.execs()), 1)

    def test_the_port_is_a_number_in_range(self):
        for port in (0, -1, 65536, 10 ** 9, "80", str(self.app_port), 80.5, True, None, [80], {"port": 80}, "80; rm -rf /"):
            status, out = self.api("/api/view", {"run": RUN, "port": port})
            self.assertEqual(status, 400, repr(port))
            self.assertIn("1 to 65535", out["error"])
        self.assertEqual(self.api("/api/view", {"run": RUN})[0], 400)
        self.assertEqual(self.api("/api/view", {"run": RUN, "port": 65535})[0], 200)
        self.assertEqual(self.execs(), [])


class WhatIsRelayed(ViewCase):
    def test_a_get_and_a_post_reach_the_app_without_what_is_not_its_to_see(self):
        host, cookie = self.entered()
        secret = cookie.split("=", 1)[1]
        status, msg, raw = self.call("GET", "/models/ship.glb?lod=2", host=host, headers={
            "Cookie": "session=abc; %s; theme=dark" % cookie, "X-Org-Token": self.server.token, "Accept": "model/gltf-binary"})
        self.assertEqual((status, raw), (200, b"the engineer's app at /models/ship.glb?lod=2"))
        self.assertEqual(msg["Connection"], "close")
        body = os.urandom(300_000)  # more than one read, and more than a pipe holds
        status, _, raw = self.call("POST", "/submit", host=host, body=body, headers={"Cookie": cookie, "Content-Type": "application/octet-stream"})
        self.assertEqual((status, raw), (200, b"got " + body))
        got, posted = self.app.seen
        self.assertEqual((got["method"], got["path"], posted["method"], posted["path"], posted["body"]),
                         ("GET", "/models/ship.glb?lod=2", "POST", "/submit", body))
        for seen in (got, posted):
            self.assertEqual(header(seen, "Host"), ["127.0.0.1:%d" % self.app_port])
            self.assertEqual(header(seen, "Connection"), ["close"])
            self.assertEqual(header(seen, "X-Org-Token"), [])
            self.assertNotIn(secret, repr(seen["headers"]))
            self.assertNotIn(self.server.token, repr(seen["headers"]))
        self.assertEqual(header(got, "Cookie"), ["session=abc; theme=dark"], "the app's own cookies are its own")
        self.assertEqual(header(got, "Accept"), ["model/gltf-binary"])
        self.assertEqual(header(posted, "Cookie"), [], "a Cookie header that held only ours is not sent at all")
        self.assertEqual(header(posted, "Content-Length"), [str(len(body))])
        # Where it went: the container in the run's record, a port, and nothing the app said.
        self.assertEqual(self.execs(), [["org-eng-" + RUN, [str(self.app_port), str(view.IDLE_S + 60)]]] * 2)
        self.assertTrue(self.relays_released())

    def test_the_policy_is_added_and_the_apps_own_is_kept(self):
        host, cookie = self.entered()
        _, msg, _ = self.call("GET", "/", host=host, headers={"Cookie": cookie})
        self.assertEqual(msg.get_all("Content-Security-Policy"), ["connect-src 'self'; form-action 'self'; frame-src 'self'"])
        self.assertEqual(msg.get_all("Referrer-Policy"), ["no-referrer"])
        self.assertEqual(msg.get_all("X-Content-Type-Options"), ["nosniff"])
        _, msg, raw = self.call("GET", "/own-policy", host=host, headers={"Cookie": cookie})
        self.assertEqual(raw, b"strict")
        self.assertEqual(msg.get_all("Content-Security-Policy"), ["default-src 'self'", "connect-src 'self'; form-action 'self'; frame-src 'self'"],
                         "both are sent, and a browser enforces both")
        self.assertEqual(msg.get_all("Referrer-Policy"), ["same-origin"], "added only where the app says nothing")
        self.assertEqual(msg.get_all("X-Content-Type-Options"), ["nosniff"])

    def test_the_app_cannot_replace_the_cookie_that_lets_a_browser_in(self):
        host, cookie = self.entered()
        _, msg, _ = self.call("GET", "/cookies", host=host, headers={"Cookie": cookie})
        self.assertEqual(msg.get_all("Set-Cookie"), ["session=abc; Path=/"])

    def test_a_views_host_never_reaches_the_dashboard(self):
        host, cookie = self.entered()
        token = {"X-Org-Token": self.server.token}
        # The dashboard's token, on a view's host, without the view's cookie: refused, and not by the API.
        for method, path in (("GET", "/api/state"), ("GET", "/"), ("GET", "/index.html"), ("POST", "/api/stop"), ("POST", "/api/view")):
            status, _, raw = self.call(method, path, host=host, headers=token, body=b"{}" if method == "POST" else None)
            self.assertEqual((status, raw), (403, b""), path)
        # With the cookie it is the app that answers, and it is not shown the token.
        status, _, raw = self.call("GET", "/api/state", host=host, headers=dict(token, Cookie=cookie))
        self.assertEqual((status, raw), (200, b"the engineer's app at /api/state"))
        status, _, raw = self.call("GET", "/", host=host, headers=dict(token, Cookie=cookie))
        self.assertEqual(raw, b"the engineer's app at /")
        status, _, raw = self.call("POST", "/api/stop", host=host, headers=dict(token, Cookie=cookie), body=json.dumps({"run": RUN}))
        self.assertEqual(raw, b"got " + json.dumps({"run": RUN}).encode())
        self.assertEqual(self.ledger.run(RUN)["state"], "executing")
        self.assertEqual([header(s, "X-Org-Token") for s in self.app.seen], [[], [], []])
        # Two Host headers, the dashboard's first: still not the dashboard's to answer.
        s = socket.create_connection(("127.0.0.1", self.port), timeout=20)
        self.addCleanup(s.close)
        s.sendall(("GET /api/state HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nHost: %s\r\nX-Org-Token: %s\r\nCookie: %s\r\n\r\n"
                   % (self.port, host, self.server.token, cookie)).encode())
        answer = b""
        while True:
            more = s.recv(65536)
            if not more:
                break
            answer += more
        self.assertTrue(answer.startswith(b"HTTP/1.0 404 "), answer[:80])
        self.assertNotIn(b"runs", answer)
        # And the dashboard itself is where it was.
        self.assertEqual(self.api("/api/state")[0], 200)
        self.assertIn(b"<title>org</title>", self.call("GET", "/")[2])

    def test_an_upgrade_becomes_a_pipe_both_ways(self):
        heads = []

        def websocket(conn):
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    more = conn.recv(65536)
                    if not more:
                        return
                    data += more
                heads.append(data)
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\nready")
                while True:
                    more = conn.recv(65536)
                    if not more:
                        return
                    conn.sendall(more.upper())

        port = self.raw_upstream(websocket)
        host, cookie = self.entered(port=port)
        s = socket.create_connection(("127.0.0.1", self.port), timeout=20)
        self.addCleanup(s.close)

        def read(n=None, until=None):
            got = b""
            while len(got) < n if until is None else until not in got:
                more = s.recv(65536)
                self.assertTrue(more, "closed after %r" % got[-80:])
                got += more
            return got

        s.sendall(("GET /socket HTTP/1.1\r\nHost: %s\r\nConnection: keep-alive, Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Key: abc\r\n"
                   "Cookie: %s\r\nX-Org-Token: %s\r\n\r\nhi" % (host, cookie, self.server.token)).encode())
        answer = read(until=b"HI")
        head, _, rest = answer.partition(b"\r\n\r\n")
        self.assertEqual(head.split(b"\r\n"), [b"HTTP/1.1 101 Switching Protocols", b"Upgrade: websocket", b"Connection: Upgrade"])
        # What the app said with its 101 arrives, and so does what the browser sent on the heels of its request.
        self.assertEqual(rest, b"readyHI")
        s.sendall(b"ping")
        self.assertEqual(read(4), b"PING")
        big = b"abcdefgh" * 40_000  # more than any one buffer on the way, sent while the answer is read
        threading.Thread(target=s.sendall, args=(big,), daemon=True).start()
        self.assertEqual(read(len(big)), big.upper())
        asked = heads[0].decode().split("\r\n")
        self.assertEqual(asked[0], "GET /socket HTTP/1.1")
        self.assertIn("Host: 127.0.0.1:%d" % port, asked)
        self.assertIn("Connection: Upgrade", asked)
        self.assertIn("Upgrade: websocket", asked)
        self.assertIn("Sec-WebSocket-Key: abc", asked)
        self.assertFalse([line for line in asked if line.lower().startswith(("cookie", "x-org-token"))], asked)
        # While that is open it counts against what one run's pages may hold open at once.
        self.addCleanup(setattr, view, "MAX_RELAYS_PER_RUN", view.MAX_RELAYS_PER_RUN)
        view.MAX_RELAYS_PER_RUN = 1
        status, msg, _ = self.call("GET", "/socket", host=host, headers={"Cookie": cookie})
        self.assertEqual((status, msg["Retry-After"]), (503, "2"))
        self.assertEqual((len(heads), self.server.views.relays()), (1, 1))
        s.close()
        self.assertTrue(self.relays_released(), "closing the tab ends the relay")
        # An app that switches protocols unasked is not given a pipe.
        status, _, raw = self.call("GET", "/socket", host=host, headers={"Cookie": cookie})
        self.assertEqual(status, 502)
        self.assertNotIn(b"ready", raw)

    def test_a_body_without_a_length_is_refused_and_a_body_without_an_end_is_streamed(self):
        host, cookie = self.entered()
        status, _, raw = self.call("POST", "/submit", host=host, body=iter([b"one", b"two"]), headers={"Cookie": cookie})
        self.assertEqual(status, 411, "http.client sends an iterable chunked")
        self.assertEqual(self.app.seen, [])
        status, msg, raw = self.call("GET", "/stream", host=host, headers={"Cookie": cookie})
        self.assertEqual((status, raw), (200, b"part one, part two"))
        self.assertEqual(msg.get_all("Content-Security-Policy"), [view.CSP])
        status, msg, raw = self.call("HEAD", "/", host=host, headers={"Cookie": cookie})
        self.assertEqual(status, 501, "the app's own refusal comes through as it is")
        self.assertTrue(self.relays_released())

    def test_what_the_proxy_itself_refuses(self):
        host, cookie = self.entered()
        s = socket.create_connection(("127.0.0.1", self.port), timeout=20)
        self.addCleanup(s.close)
        s.sendall(("POST /submit HTTP/1.1\r\nHost: %s\r\nCookie: %s\r\nContent-Length: 3\r\nContent-Length: 30\r\n\r\nabc" % (host, cookie)).encode())
        self.assertTrue(s.recv(65536).startswith(b"HTTP/1.0 400 "), "two lengths")
        status, _, _ = self.call("GET", "/", host=host, headers={"Cookie": cookie, "X-A": "a" * 30_000, "X-B": "b" * 30_000, "X-C": "c" * 30_000})
        self.assertEqual(status, 431)
        self.assertEqual((self.app.seen, self.execs()), ([], []))

    def test_nothing_listening_and_nothing_said(self):
        quiet = socket.socket()
        quiet.bind(("127.0.0.1", 0))
        closed = quiet.getsockname()[1]
        quiet.close()
        host, cookie = self.entered(port=closed)
        status, _, raw = self.call("GET", "/", host=host, headers={"Cookie": cookie})
        self.assertEqual(status, 502)
        self.assertIn(b"nothing answered on that port", raw)
        # Something that takes the connection and never answers.
        held = []
        silent = self.raw_upstream(held.append)
        self.addCleanup(lambda: [c.close() for c in held])
        host, cookie = self.entered(port=silent)
        old = view.HEAD_TIMEOUT_S
        self.addCleanup(setattr, view, "HEAD_TIMEOUT_S", old)
        view.HEAD_TIMEOUT_S = 0.5
        self.assertEqual(self.call("GET", "/", host=host, headers={"Cookie": cookie})[0], 504)
        # Something that is not a web server at all.
        noise = self.raw_upstream(lambda conn: (conn.sendall(b"SSH-2.0-OpenSSH_9.6\r\n\r\n"), conn.close()))
        host, cookie = self.entered(port=noise)
        self.assertEqual(self.call("GET", "/", host=host, headers={"Cookie": cookie})[0], 502)
        self.assertTrue(self.relays_released())


class TheCommand(ViewCase):
    def org(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                code = cli.main(["--home", str(self.ledger.root)] + list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_org_view_asks_the_running_dashboard(self):
        code, out, _ = self.org("view", "001", str(self.app_port), "--dash-port", str(self.port))
        self.assertEqual(code, 0)
        url = urlsplit(out.strip())
        self.assertRegex(url.netloc, r"^v-[0-9a-f]{20}\.localhost:%d$" % self.port)
        status, msg, _ = self.call("GET", url.path + "?" + url.query, host=url.netloc)
        self.assertEqual(status, 200)
        status, _, raw = self.call("GET", "/", host=url.netloc, headers={"Cookie": msg["Set-Cookie"].split(";")[0]})
        self.assertEqual((status, raw), (200, b"the engineer's app at /"))
        # What the dashboard refuses, the command reports.
        code, out, err = self.org("view", "001", "0", "--dash-port", str(self.port))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("1 to 65535", err)

    def test_org_view_refuses_when_viewing_is_off_or_no_dashboard_is_running(self):
        self.configure(False)
        code, out, err = self.org("view", "001", str(self.app_port), "--dash-port", str(self.port))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("org config set dash.view true", err)
        self.configure(True)
        nobody = socket.socket()
        nobody.bind(("127.0.0.1", 0))
        port = nobody.getsockname()[1]
        nobody.close()
        code, out, err = self.org("view", "001", str(self.app_port), "--dash-port", str(port))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("the dashboard is not answering", err)

    def test_the_manager_chat_must_ask_the_person(self):
        for mode in ("talk", "ask"):
            p = manager.settings(self.ledger, mode, with_sandbox=False)["permissions"]
            self.assertIn("Bash(org view *)", p["ask"])
            self.assertNotIn("Bash(org view *)", p["allow"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
