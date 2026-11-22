"""The dashboard: `org status` as a local web page, with the manager's chat beside it.

A small HTTP server bound to 127.0.0.1. A background thread pulls from the engineers' machines
every few seconds; the page polls the server. Nothing here talks to an engineer's session.

The page can do two things that matter: approve a spec (which dispatches an engineer and spends
money) and stop a run. So every /api/ request must carry a token that only the person who started
`org dash` has (it is in the URL the command prints, after the #, where it is never sent to a
server or written to a log), and requests whose Host is not this machine are refused, which is
what stops another site's JavaScript from reaching the dashboard through the browser.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import dispatch, manager, spec, status
from .ledger import Ledger
from .stream import feed
from .sync import sync_all, sync_run
from .transport import TransportError
from .util import now, read_text

PAGE = Path(__file__).parent / "web" / "index.html"
MAX_BODY = 1_000_000


def load_token(ledger: Ledger) -> str:
    """One token per ledger, kept across restarts so the printed URL can be bookmarked."""
    path = ledger.chat / "dash-token"
    try:
        token = path.read_text().strip()
        if len(token) >= 32:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token


def document(ledger: Ledger, run_id: str, name: str) -> dict:
    """A run's documents, as Markdown for the page to render."""
    mirror = ledger.mirror_dir(run_id)
    rec = ledger.run(run_id)
    if name == "plan":
        text = read_text(mirror / "plan.frozen.md") or read_text(mirror / "plan.md")
        return {"title": "Plan as frozen before execution", "markdown": text or "_No plan yet. The engineer is still planning._"}
    if name == "plan-current":
        return {"title": "Plan with the engineer's amendments", "markdown": read_text(mirror / "plan.md") or "_No plan yet._"}
    if name == "report":
        return {"title": "Engineer's report", "markdown": read_text(mirror / "report.md") or "_No report yet. The run has not finished._"}
    if name == "review":
        return {"title": "Plan against outcome", "markdown": "```\n%s\n```" % status.scorecard(ledger, run_id)}
    if name == "spec":
        text = read_text(ledger.spec_dir(rec.get("spec_id") or "") / "spec.md")
        return {"title": "Approved spec", "markdown": text or "_The approved spec is not in the ledger._"}
    if name == "activity":
        lines = [line for _, line in feed(mirror / "events.jsonl")][-120:]
        return {"title": "Recent activity", "markdown": "```\n%s\n```" % ("\n".join(lines) or "Nothing yet.")}
    return {"title": name, "markdown": "_Unknown document._"}


class Handler(BaseHTTPRequestHandler):
    server_version = "org"

    def log_message(self, fmt, *args):  # the terminal that started the dashboard stays quiet
        pass

    # ------------------------------------------------------------ plumbing
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def _local(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost", "::1"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            o = urlparse(origin)
            return o.hostname in ("127.0.0.1", "localhost", "::1") and o.port == self.server.server_port
        return True

    def _authorized(self) -> bool:
        return hmac.compare_digest(self.headers.get("X-Org-Token") or "", self.server.token)

    def _guard(self) -> bool:
        if not self._local():
            self._json(403, {"error": "this dashboard only answers requests addressed to this machine"})
            return False
        if not self._authorized():
            self._json(401, {"error": "missing or wrong token: open the URL that `org dash` printed"})
            return False
        return True

    # ------------------------------------------------------------ GET
    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        ledger: Ledger = self.server.ledger
        if url.path in ("/", "/index.html"):
            if not self._local():
                return self._json(403, {"error": "this dashboard only answers requests addressed to this machine"})
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if not url.path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        if not self._guard():
            return
        try:
            if url.path == "/api/state":
                state = status.collect(ledger)
                state["synced_at"] = self.server.synced_at
                state["sync_error"] = self.server.sync_error
                return self._json(200, state)
            if url.path == "/api/doc":
                run_id = ledger.resolve_run((query.get("run") or [""])[0])
                return self._json(200, document(ledger, run_id, (query.get("name") or [""])[0]))
            if url.path == "/api/draft":
                spec_id = ledger.resolve_spec((query.get("spec") or [""])[0])
                text = read_text(ledger.draft_path(spec_id))
                if text is None:
                    return self._json(404, {"error": "no draft for %s" % spec_id})
                errors, warnings = spec.check(text, spec.default_machine(ledger))
                return self._json(200, {"id": spec_id, "markdown": text, "sha": spec.draft_sha(ledger, spec_id),
                                        "errors": errors, "warnings": warnings, "state": spec.state_of(ledger, spec_id)})
        except LookupError as e:
            return self._json(404, {"error": str(e)})
        return self._json(404, {"error": "not found"})

    # ------------------------------------------------------------ POST
    def do_POST(self):
        url = urlparse(self.path)
        ledger: Ledger = self.server.ledger
        if not url.path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        if not self._guard():
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._json(413, {"error": "request too large"})
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
        except ValueError as e:
            return self._json(400, {"error": "bad request: %s" % e})
        try:
            if url.path == "/api/ask":
                return self._ask(str(body.get("question") or ""))
            if url.path == "/api/approve":
                spec_id = ledger.resolve_spec(str(body.get("spec") or ""))
                before = ledger.spec_meta(spec_id) or {}
                try:
                    meta = spec.approve(ledger, spec_id, str(body.get("sha") or ""))
                except spec.ApprovalError as e:
                    return self._json(400, {"error": str(e)})
                if before.get("sha256") == meta["sha256"] and ledger.runs_for(spec_id):
                    return self._json(200, {"approved": True, "version": meta["version"], "dispatched": False, "log": [],
                                            "error": "this text was already approved and has run (%s); `org dispatch` runs it again"
                                                     % ", ".join(ledger.runs_for(spec_id))})
                log = []
                try:
                    run_id, _ = dispatch.dispatch(ledger, spec_id, log=log.append)
                except dispatch.DispatchError as e:
                    return self._json(200, {"approved": True, "version": meta["version"], "dispatched": False, "error": str(e), "log": log})
                threading.Thread(target=sync_run, args=(ledger, run_id), daemon=True).start()
                return self._json(200, {"approved": True, "version": meta["version"], "dispatched": True, "run_id": run_id, "log": log})
            if url.path == "/api/stop":
                run_id = ledger.resolve_run(str(body.get("run") or ""))
                try:
                    return self._json(200, {"ok": True, "detail": dispatch.stop(ledger, run_id)})
                except TransportError as e:
                    return self._json(200, {"ok": False, "error": str(e)})
        except LookupError as e:
            return self._json(404, {"error": str(e)})
        return self._json(404, {"error": "not found"})

    def _ask(self, question: str) -> None:
        if not question.strip():
            return self._json(400, {"error": "empty question"})
        if not self.server.ask_lock.acquire(blocking=False):
            return self._json(409, {"error": "the manager is still answering the previous question"})
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def emit(obj) -> None:
                self.wfile.write(b"data: " + json.dumps(obj).encode("utf-8") + b"\n\n")
                self.wfile.flush()

            try:
                out = manager.ask(self.server.ledger, question, on_text=lambda chunk: emit({"t": chunk}))
                emit({"done": True, "error": out.get("error"), "cost_usd": out.get("cost_usd"), "text": out.get("text")})
            except (BrokenPipeError, ConnectionResetError):
                pass  # the page went away; the answer is still in the manager's session
        finally:
            self.server.ask_lock.release()


def _sync_loop(server) -> None:
    while not server.stopping:
        try:
            sync_all(server.ledger)
            server.sync_error = None
        except Exception as e:  # a bad machine must not stop the loop
            server.sync_error = "%s: %s" % (type(e).__name__, e)
        server.synced_at = now()
        for _ in range(int(max(2, server.interval) * 4)):
            if server.stopping:
                return
            time.sleep(0.25)


def make_server(ledger: Ledger, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    httpd.ledger = ledger
    httpd.token = load_token(ledger)
    httpd.ask_lock = threading.Lock()
    httpd.synced_at = None
    httpd.sync_error = None
    httpd.stopping = False
    httpd.interval = float((ledger.config().get("dash") or {}).get("sync_interval_s") or 10)
    return httpd


def serve(ledger: Ledger, port=None, open_browser: bool = True) -> int:
    ledger.init()
    port = int(port or (ledger.config().get("dash") or {}).get("port") or 8787)
    try:
        httpd = make_server(ledger, port)
    except OSError as e:
        print("org: cannot listen on 127.0.0.1:%d (%s). Is the dashboard already running? Try --port." % (port, e))
        return 1
    url = "http://127.0.0.1:%d/#t=%s" % (port, httpd.token)
    threading.Thread(target=_sync_loop, args=(httpd,), daemon=True).start()
    print("org dashboard: %s" % url, flush=True)
    print("ledger: %s   (ctrl-c stops the dashboard; engineers keep running)" % ledger.root, flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.stopping = True
        httpd.server_close()
    return 0
