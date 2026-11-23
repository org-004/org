"""A web server an engineer is running, seen in the owner's browser through the dashboard's port.

An engineer runs with every permission bypassed, so the page it serves is untrusted and may be
hostile. What it must not reach is the dashboard: the token that approves specs and stops runs is
in the browser's storage for the dashboard's origin. So:

  - each (run, port) the owner opens gets an origin of its own, http://v-<id>.localhost:<port>/.
    Chrome and Firefox resolve *.localhost to this machine, and a hostname of its own is an
    origin and a site of its own: its scripts cannot read the dashboard's storage or another
    view's cookies.
  - opening one is the owner's act. POST /api/view on the dashboard's own origin, with its token,
    answers with a URL carrying a ticket that works once and for a minute. Visiting it sets a
    cookie on that hostname alone, and every later request there must carry it.
  - a request addressed to a view's hostname never reaches the dashboard's page or its /api/
    routes: dash.Handler.parse_request hands it to serve() before any of them is looked at.
  - the far end is never an address the container reports. It is `incus exec` into the container
    named in the run's own record, after the backend's check that the container is one of org's,
    to a port on that container's own loopback.
  - each request is read, authenticated and written out again, one to a connection on both
    sides. Bytes are copied blind only after the app answers 101 to an upgrade the browser asked
    for, which is what a WebSocket is.

Open views are kept in this process's memory and nowhere else: restarting the dashboard closes
every one, and the owner opens them again from the page.
"""
from __future__ import annotations

import hmac
import os
import re
import secrets
import selectors
import socket
import subprocess
import threading
import time
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs

from . import backends
from .backends import BackendError
from .ledger import Ledger
from .transport import Incus, TransportError
from .util import now

# A view's hostname and the dashboard's port, and nothing before or after: used with fullmatch.
HOST = re.compile(r"v-([0-9a-f]{20})\.localhost:([0-9]{1,5})")
OPEN_PATH = "/.org-view/open"  # where a ticket is exchanged for the cookie; never forwarded to the app
COOKIE = "org-view"
CSP = "connect-src 'self'; form-action 'self'; frame-src 'self'"
TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")  # a method or a header name
TARGET = re.compile(r"/[\x21-\x7e]*")  # a path on this server, in printable ASCII
STATUS = re.compile(r"HTTP/1\.[01] ([1-5][0-9][0-9])(?: [\t\x20-\x7e\x80-\xff]*)?")
CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")  # in a header's value: a line break to some parser, or nothing a browser sends
# Not passed on to the app: what belongs to one hop, what is rewritten below, and the dashboard's token.
DROPPED = frozenset(("host", "connection", "keep-alive", "proxy-connection", "proxy-authorization", "te", "trailer",
                     "upgrade", "expect", "content-length", "transfer-encoding", "x-org-token"))
NO_MACHINE = ("queued", "provisioning", "dispatch_failed")

TICKET_TTL_S = 60
VIEW_TTL_S = 12 * 3600  # since it was last used
MAX_VIEWS = 64
MAX_RELAYS = 32  # `incus exec` processes at once, over every view,
MAX_RELAYS_PER_RUN = 12  # and into any one run's container: one engineer's page cannot take them all
MAX_HEAD = 64 * 1024  # of a request, and of the app's answer
HEAD_TIMEOUT_S = 120  # for the app to begin its answer
IDLE_S = 300  # with nothing moving in either direction
CHUNK = 65536

WELCOME = (b'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=/">'
           b'<title>org view</title><a href="/">Continue</a>\n')


class ViewError(Exception):
    """A refusal, and the HTTP status that carries it."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def enabled(cfg: dict) -> bool:
    return (cfg.get("dash") or {}).get("view") is True


def target(ledger: Ledger, run_id: str) -> Incus:
    """The container a view of this run reaches, by the run's own record and nothing the container says."""
    rec = ledger.run(run_id)
    machine = rec.get("machine") or {}
    if machine.get("kind") != "incus":
        raise ViewError(409, "%s is not in an Incus container (%s): only a container's ports can be viewed" % (run_id, machine.get("kind")))
    if rec.get("machine_removed_at"):
        raise ViewError(409, "the container %s ran in has been deleted" % run_id)
    if rec.get("final") or rec.get("state") in NO_MACHINE:
        raise ViewError(409, "%s is not running (%s): only a run in progress can be viewed" % (run_id, rec.get("state")))
    cfg = ledger.config()
    try:
        # The backend's own check that the container is one of org's: the one every action on a container passes.
        return backends.get(cfg, "incus").transport(machine, cfg)
    except (BackendError, KeyError, TypeError) as e:
        raise ViewError(409, str(e))


class Views:
    """The views that are open: id -> run, port, cookie secret, when made and last used, unspent tickets.

    In memory only. A restart of the dashboard forgets them all, which closes them.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._views: Dict[str, dict] = {}
        self._relays: Dict[str, int] = {}  # run -> connections into its container that are open now

    def _expire(self, t: float) -> None:
        for vid in [k for k, v in self._views.items() if t - v["used"] > VIEW_TTL_S]:
            del self._views[vid]

    def open(self, run_id: str, port: int) -> Tuple[str, str]:
        """(id, a new ticket). The same run and port keep their hostname for as long as the view lives."""
        t = now()
        with self._lock:
            self._expire(t)
            view = next((v for v in self._views.values() if (v["run"], v["port"]) == (run_id, port)), None)
            if view is None:
                while len(self._views) >= MAX_VIEWS:
                    del self._views[min(self._views, key=lambda k: self._views[k]["used"])]
                view = {"id": secrets.token_hex(10), "run": run_id, "port": port, "secret": secrets.token_urlsafe(32),
                        "created": t, "used": t, "tickets": {}}
                self._views[view["id"]] = view
            fresh = sorted(((exp, k) for k, exp in view["tickets"].items() if exp > t))[-7:]
            view["tickets"] = {k: exp for exp, k in fresh}
            ticket = secrets.token_urlsafe(32)
            view["tickets"][ticket] = t + TICKET_TTL_S
            view["used"] = t
            return view["id"], ticket

    def get(self, vid: str) -> Optional[dict]:
        t = now()
        with self._lock:
            self._expire(t)
            view = self._views.get(vid)
            if view is not None:
                view["used"] = t
            return view

    def redeem(self, vid: str, ticket: str) -> Optional[str]:
        """The view's cookie secret, for a ticket that is its own, unspent and in time. It is spent by this."""
        t = now()
        with self._lock:
            view = self._views.get(vid)
            if view is None:
                return None
            found = None
            for known in view["tickets"]:
                if hmac.compare_digest(known.encode(), ticket.encode("utf-8", "replace")):
                    found = known
            if found is None or view["tickets"].pop(found) <= t:
                return None
            return view["secret"]

    def drop(self, vid: str) -> None:
        with self._lock:
            self._views.pop(vid, None)

    def relays(self) -> int:
        with self._lock:
            return sum(self._relays.values())

    def take_relay(self, run_id: str) -> bool:
        with self._lock:
            mine = self._relays.get(run_id, 0)
            if mine >= MAX_RELAYS_PER_RUN or sum(self._relays.values()) >= MAX_RELAYS:
                return False
            self._relays[run_id] = mine + 1
            return True

    def give_relay(self, run_id: str) -> None:
        with self._lock:
            left = self._relays.get(run_id, 0) - 1
            if left > 0:
                self._relays[run_id] = left
            else:
                self._relays.pop(run_id, None)


def open_view(server, run_ref, port) -> dict:
    """What POST /api/view answers. Raises ViewError, or LookupError for a run that is not known."""
    ledger: Ledger = server.ledger
    if not enabled(ledger.config()):
        raise ViewError(403, "viewing is off. An engineer's page is untrusted content shown in your browser; "
                             "`org config set dash.view true` turns it on")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ViewError(400, "port must be a whole number from 1 to 65535")
    run_id = ledger.resolve_run(str(run_ref or ""))
    target(ledger, run_id)
    vid, ticket = server.views.open(run_id, port)
    host = "v-%s.localhost:%d" % (vid, server.public_port)
    return {"run": run_id, "port": port, "host": host, "url": "http://%s%s?ticket=%s" % (host, OPEN_PATH, ticket)}


# ---------------------------------------------------------------- requests addressed to a view


def addressed(headers) -> bool:
    """True for anything that names a view as its Host, well-formed or not: none of it is the dashboard's."""
    return any(h.strip().lower().startswith("v-") for h in headers.get_all("Host") or [])


def serve(h) -> None:
    """Answer one request whose Host is a view's. `h` is the dashboard's handler, its head already parsed."""
    server = h.server
    h.close_connection = True
    try:
        h.connection.settimeout(IDLE_S)
        hosts = h.headers.get_all("Host") or []
        m = HOST.fullmatch(hosts[0].strip()) if len(hosts) == 1 else None
        if m is None or int(m.group(2)) not in server.ports or not enabled(server.ledger.config()):
            return _answer(h, 404)
        view = server.views.get(m.group(1))
        if view is None:
            return _answer(h, 404)
        path, _, query = (h.path or "").partition("?")
        if path == OPEN_PATH:
            ticket = (parse_qs(query).get("ticket") or [""])[0]
            secret = server.views.redeem(view["id"], ticket) if h.command == "GET" else None
            if secret is None:
                return _answer(h, 403)
            # Not a 3xx: the browser came here from the dashboard, another site, and it would not
            # send a SameSite=Strict cookie on a redirect that follows from that. This page is the
            # view's own, so the request it makes for / carries the cookie.
            return _answer(h, 200, WELCOME, "text/html; charset=utf-8",
                           (("Set-Cookie", "%s=%s; Path=/; HttpOnly; SameSite=Strict" % (COOKIE, secret)),))
        if not _authorized(h.headers, view["secret"]):
            return _answer(h, 403)
        try:
            container = target(server.ledger, view["run"])
        except ViewError:
            server.views.drop(view["id"])  # the run finished or its container went: the view went with it
            return _answer(h, 404)
        _proxy(h, view, container)
    except OSError:
        pass  # the browser went away, or stopped reading


def _answer(h, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8", extra=()) -> None:
    h.send_response(code)
    for name, value in (("Content-Type", ctype), ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
                        ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                        ("Content-Security-Policy", "default-src 'none'"), ("Connection", "close")) + tuple(extra):
        h.send_header(name, value)
    h.end_headers()
    if h.command != "HEAD":
        h.wfile.write(body)
    if h.headers.get("Transfer-Encoding") or (h.headers.get("Content-Length") or "0").strip() not in ("", "0"):
        _linger(h.connection)


def _linger(sock, limit: int = 1 << 20, seconds: float = 1.0) -> None:
    """After an answer to a request whose body was not read: take a little of what the browser is
    still sending before closing. Closing on unread data resets the connection, and a browser
    shown a reset never reads the answer."""
    try:
        sock.shutdown(socket.SHUT_WR)
        end = time.monotonic() + seconds
        while limit > 0 and time.monotonic() < end:
            sock.settimeout(max(0.01, end - time.monotonic()))
            data = sock.recv(CHUNK)
            if not data:
                break
            limit -= len(data)
    except OSError:
        pass


def _authorized(headers, secret: str) -> bool:
    ok = False
    for raw in headers.get_all("Cookie") or []:
        for pair in raw.split(";"):
            name, _, value = pair.strip().partition("=")
            if name.strip() == COOKIE and hmac.compare_digest(value.strip().encode("latin-1", "replace"), secret.encode()):
                ok = True
    return ok


def _request_head(h, port: int) -> Tuple[bytes, int, bool]:
    """(the head to send to the app, the length of the body that follows, whether an upgrade is asked for).

    The head is written again from what was parsed, so the app is sent exactly what was checked here.
    """
    if not TOKEN.fullmatch(h.command or "") or h.request_version not in ("HTTP/1.0", "HTTP/1.1") or not TARGET.fullmatch(h.path or ""):
        raise ViewError(400, "bad request")
    items = h.headers.items()
    if len(h.raw_requestline) + sum(len(k) + len(v) + 4 for k, v in items) > MAX_HEAD:
        raise ViewError(431, "request head too large")
    for name, value in items:
        if not TOKEN.fullmatch(name) or CONTROL.search(value):
            raise ViewError(400, "bad request header")
    # A chunked request body is refused, to keep this simple: a body is forwarded by its
    # Content-Length and by nothing else. Browsers send one for everything but a streamed upload.
    if h.headers.get_all("Transfer-Encoding"):
        raise ViewError(411, "a request body must come with a Content-Length: chunked uploads are not relayed")
    lengths = h.headers.get_all("Content-Length") or []
    if len(lengths) > 1 or (lengths and not re.fullmatch(r"[0-9]{1,15}", lengths[0].strip())):
        raise ViewError(400, "bad Content-Length")
    length = int(lengths[0]) if lengths else 0
    named = {t.strip().lower() for v in h.headers.get_all("Connection") or [] for t in v.split(",") if t.strip()}
    upgrade = h.headers.get_all("Upgrade") or []
    upgrading = "upgrade" in named and len(upgrade) == 1
    lines = ["%s %s HTTP/1.1" % (h.command, h.path), "Host: 127.0.0.1:%d" % port]
    for name, value in items:
        low = name.lower()
        if low in DROPPED or low in named:
            continue
        if low == "cookie":  # the app is not shown the cookie that lets a browser in here
            pairs = [p.strip() for p in value.split(";")]
            value = "; ".join(p for p in pairs if p and p.partition("=")[0].strip() != COOKIE)
            if not value:
                continue
        lines.append("%s: %s" % (name, value))
    if lengths:
        lines.append("Content-Length: %d" % length)
    lines += ["Connection: Upgrade", "Upgrade: %s" % upgrade[0].strip()] if upgrading else ["Connection: close"]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"), length, upgrading


def _response_head(raw: bytes, method: str, upgrading: bool) -> Tuple[bytes, int, Optional[int]]:
    """(the head to send to the browser, the status, the body's length or None for "until the app closes")."""
    lines = [line[:-1] if line.endswith("\r") else line for line in raw.decode("latin-1").split("\n")]
    m = STATUS.fullmatch(lines[0])
    if not m:
        raise ViewError(502, "what answered on that port does not speak HTTP")
    status = int(m.group(1))
    if status == 101 and not upgrading:
        raise ViewError(502, "the app switched protocols without being asked to")
    out, seen, lengths = [lines[0]], set(), []
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        low = name.lower()
        if not colon or not TOKEN.fullmatch(name) or CONTROL.search(value):
            raise ViewError(502, "the app's answer has a malformed header")
        if status != 101 and low in ("connection", "keep-alive", "proxy-connection"):
            continue
        if low == "set-cookie" and value.strip().partition("=")[0].strip() == COOKIE:
            continue  # the app does not get to replace the cookie that lets a browser in here
        if low == "content-length":
            lengths.append(value.strip())
        seen.add(low)
        out.append(line)
    if status == 101:
        return ("\r\n".join(out) + "\r\n\r\n").encode("latin-1"), status, None
    # Ours is added beside any policy of the app's, never in its place: a browser enforces every
    # policy it is sent, so the app can tighten this and cannot loosen it.
    out.append("Content-Security-Policy: " + CSP)
    if "referrer-policy" not in seen:
        out.append("Referrer-Policy: no-referrer")
    if "x-content-type-options" not in seen:
        out.append("X-Content-Type-Options: nosniff")
    out.append("Connection: close")
    body: Optional[int] = None
    if method == "HEAD" or status in (204, 304) or status < 200:
        body = 0
    elif "transfer-encoding" in seen:
        if lengths:
            raise ViewError(502, "the app's answer gives its length two ways")
    elif lengths:
        if len(set(lengths)) != 1 or not re.fullmatch(r"[0-9]{1,18}", lengths[0]):
            raise ViewError(502, "the app's answer has a bad Content-Length")
        body = int(lengths[0])
    return ("\r\n".join(out) + "\r\n\r\n").encode("latin-1"), status, body


def _wait(fd: int, writing: bool, timeout: float) -> bool:
    with selectors.DefaultSelector() as sel:
        sel.register(fd, selectors.EVENT_WRITE if writing else selectors.EVENT_READ)
        return bool(sel.select(max(0.0, timeout)))


def _write(fd: int, data: bytes, timeout: float) -> None:
    """Write all of it to a pipe that does not block, giving up when the far end stops reading."""
    left = memoryview(data)
    while len(left):
        if not _wait(fd, True, timeout):
            raise ViewError(504, "the app stopped reading")
        try:
            left = left[os.write(fd, left[:CHUNK]):]
        except BlockingIOError:
            pass


def _read_head(fd: int, buf: bytearray, deadline: float) -> bytes:
    """The app's next response head, without its blank line. What follows it stays in buf."""
    while True:
        end = re.search(rb"\r?\n\r?\n", buf)
        if end:
            head = bytes(buf[:end.start()])
            del buf[:end.end()]
            return head
        if len(buf) > MAX_HEAD:
            raise ViewError(502, "the app's answer has no end to its head")
        if not _wait(fd, False, deadline - time.monotonic()):
            raise ViewError(504, "the app did not answer in time")
        data = os.read(fd, CHUNK)
        if not data:
            raise ViewError(502, "the app closed the connection before answering" if buf else "nothing answered on that port in the container")
        buf += data


def _proxy(h, view: dict, container: Incus) -> None:
    try:
        head, length, upgrading = _request_head(h, view["port"])
    except ViewError as e:
        return _answer(h, e.code, (str(e) + "\n").encode())
    views = h.server.views
    if not views.take_relay(view["run"]):
        return _answer(h, 503, b"too many connections to engineers' apps are open at once\n", extra=(("Retry-After", "2"),))
    proc = None
    try:
        try:
            proc = container.relay(view["port"], IDLE_S + 60)
        except (TransportError, OSError) as e:
            raise ViewError(502, str(e))
        inp, out = proc.stdin.fileno(), proc.stdout.fileno()
        os.set_blocking(inp, False)
        if length and h.request_version == "HTTP/1.1" and (h.headers.get("Expect") or "").strip().lower() == "100-continue":
            h.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")
        try:
            _write(inp, head, IDLE_S)
            left = length
            while left:
                data = h.rfile.read(min(CHUNK, left))
                if not data:
                    return  # the browser went away in the middle of its request
                _write(inp, data, IDLE_S)
                left -= len(data)
        except BrokenPipeError:
            pass  # the app may have answered early and gone; what it said is still to be read
        buf, deadline = bytearray(), time.monotonic() + HEAD_TIMEOUT_S
        while True:
            answer, status, body = _response_head(_read_head(out, buf, deadline), h.command, upgrading)
            if status == 101 or status >= 200:  # 100, 102 and 103 are for this hop, and dropped
                break
        h.wfile.write(answer)
        if status == 101:
            _tunnel(h, proc, bytes(buf))
        else:
            _stream(h, out, bytes(buf), body)
        try:
            h.connection.shutdown(socket.SHUT_WR)  # the browser has its answer before the relay is waited for
        except OSError:
            pass
    except ViewError as e:  # raised only before anything was sent to the browser
        _answer(h, e.code, (str(e) + "\n").encode())
    finally:
        if proc is not None:
            _reap(proc)
        views.give_relay(view["run"])


def _stream(h, out: int, data: bytes, length: Optional[int]) -> None:
    """Copy the app's body to the browser: `length` bytes of it, or all there is until the app closes."""
    left = length
    while True:
        if left is not None:
            data = data[:left]
            left -= len(data)
        if data:
            h.wfile.write(data)
        if left == 0 or not _wait(out, False, IDLE_S):
            return
        data = os.read(out, CHUNK)
        if not data:
            return


def _tunnel(h, proc, early: bytes) -> None:
    """After a 101: bytes both ways until either side closes, or nothing has moved for IDLE_S."""
    sock, inp, out = h.connection, proc.stdin.fileno(), proc.stdout.fileno()
    # Whatever the browser sent after its request is in the handler's buffer, not on the socket.
    sock.setblocking(False)
    try:
        held = h.rfile.read1(CHUNK) or b""
    except OSError:
        held = b""
    sock.settimeout(IDLE_S)
    moved, stop, tick = [time.monotonic()], threading.Event(), min(IDLE_S, 30.0)

    def idle() -> bool:
        return time.monotonic() - moved[0] > IDLE_S

    def up() -> None:
        try:
            if held:
                _write(inp, held, IDLE_S)
            while not stop.is_set():
                if not _wait(sock.fileno(), False, tick):
                    if idle():
                        break
                    continue
                data = sock.recv(CHUNK)
                if not data:
                    break
                moved[0] = time.monotonic()
                _write(inp, data, IDLE_S)
        except (OSError, ValueError, ViewError):
            pass
        finally:
            stop.set()
            try:
                proc.stdin.close()  # the relay ends when its input does, and so does the loop below
            except OSError:
                pass

    pump = threading.Thread(target=up, daemon=True)
    pump.start()
    try:
        data = early
        while not stop.is_set():
            if data:
                sock.sendall(data)
                moved[0] = time.monotonic()
            if not _wait(out, False, tick):
                if idle():
                    break
                data = b""
                continue
            data = os.read(out, CHUNK)
            if not data:
                break
    except (OSError, ValueError):
        pass
    finally:
        stop.set()
        try:
            sock.shutdown(socket.SHUT_RDWR)  # which wakes the other direction if it is waiting for the browser,
        except OSError:
            pass
        proc.kill()  # and this if it is waiting for the relay: it must be gone before the pipes are closed
        pump.join(tick + 5)


def _reap(proc) -> None:
    for pipe in (proc.stdin, proc.stdout):
        try:
            pipe.close()
        except OSError:
            pass
    try:
        proc.wait(2)  # the relay ends by itself once its input or its output is closed
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
