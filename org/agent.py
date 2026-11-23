"""The host agent: the one thing outside the machines that can create, enter and delete them.

A manager that is itself an isolated OrbStack machine cannot do any of that. It cannot run `orb`,
and it cannot reach the Mac over the network at all; that is what isolated means. The Mac can
always reach a machine. So the agent runs on the Mac and dials in: it keeps one command open
inside the manager's machine (the bridge), and the manager's requests travel up that pipe.

    the Mac                                         the manager's machine
    ┌──────────────────────────┐  orb -m org-manager … org agent bridge
    │ org agent run            │─────────────────────────▶┌────────────────────────────────┐
    │                          │◀── requests, a line each │ bridge                         │
    │   orb clone <template>   │─── answers ─────────────▶│   <ledger>/agent.sock          │
    │   orb -m <machine> …     │                          │   the dashboard, on 127.0.0.1  │
    │   orb delete <machine>   │                          └────────────────────────────────┘
    └────────────┬─────────────┘                             ▲ org dispatch, sync, status
                 │ only machines named <prefix>…             │ connect to the socket
                 ▼
         org-eng-<run>, …

What bounds it:
  - Nothing listens on the Mac for the manager. The only way in is the pipe the agent opened.
  - It clones one template and acts only on machines whose names start with its prefix. It
    cannot touch the template, the manager, or any other machine, and a request has no way to
    name a command to run on the Mac.
  - Inside the manager's machine the socket is the user's alone, and a caller from another
    machine is refused even if it could reach the file.

The dashboard is served inside the manager's machine on its loopback. OrbStack forwards a
machine's listening ports to the Mac's localhost; where it does not, the agent forwards the one
port itself, through the same kind of pipe.
"""
from __future__ import annotations

import base64
import itertools
import json
import os
import queue
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional, Tuple

from .transport import Transport, TransportError

DEFAULTS = {"orb": "orb", "manager": "org-manager", "template": "org-base", "prefix": "org-eng-", "dash_port": 8787}
MAX_LINE = 128 * 1024 * 1024
MAX_OUTPUT = 48 * 1024 * 1024
PING_EVERY = 15      # the agent tells the bridge it is alive this often, in seconds
PING_TIMEOUT = 75    # a bridge that hears nothing for this long takes the agent to be gone
BRIDGE = 'exec "$HOME/.local/bin/org" agent bridge'
PIPE = 'exec "$HOME/.local/bin/org" agent pipe %d'
NOT_CONNECTED = "the host agent is not connected to this machine. On the Mac: `org up` (its log is ~/.config/org/agent.log)."


class AgentError(Exception):
    pass


def _say(message: str) -> None:
    print("%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------- on the Mac


def host_dir() -> Path:
    """Where the Mac keeps the agent's files: its config, pid and log."""
    return Path(os.environ.get("ORG_HOST_DIR") or (Path.home() / ".config" / "org"))


def check_config(cfg: dict) -> None:
    prefix = str(cfg.get("prefix") or "")
    if not re.fullmatch(r"[a-z][a-z0-9-]*-", prefix):
        raise AgentError("prefix %r must be lowercase letters, digits and dashes, and end in a dash" % prefix)
    for key in ("manager", "template"):
        name = str(cfg.get(key) or "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            raise AgentError("%s %r is not a machine name" % (key, name))
        if name.startswith(prefix):
            raise AgentError("the %s machine %r starts with the engineers' prefix %r, so the agent could be asked to delete it"
                             % (key, name, prefix))


def load_config(path=None) -> dict:
    path = Path(path) if path else host_dir() / "agent.json"
    try:
        saved = json.loads(path.read_text())
    except OSError:
        saved = {}
    except ValueError as e:
        raise AgentError("%s is not valid JSON: %s" % (path, e))
    cfg = dict(DEFAULTS, **saved)
    check_config(cfg)
    return cfg


def _is_dash(port: int) -> bool:
    """Does the dashboard already answer on this machine's localhost?"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never by way of a proxy
    try:
        with opener.open("http://127.0.0.1:%d/" % port, timeout=2) as r:
            return (r.headers.get("Server") or "").startswith("org")
    except (OSError, ValueError):
        return False


class Agent:
    def __init__(self, cfg: dict, log: Optional[Callable[[str], None]] = None):
        check_config(cfg)
        self.cfg = cfg
        self.log = log or _say
        self.stopping = False
        self.connected = False
        self._proc = None
        self._dash_lock = threading.Lock()

    # ------------------------------------------------------------ what it will do
    def allowed(self, name) -> bool:
        """Is this a machine the agent may act on? The whole of its safety is this check."""
        prefix = self.cfg["prefix"]
        return (isinstance(name, str) and name.startswith(prefix) and len(name) <= 63
                and re.fullmatch(r"[a-z0-9][a-z0-9-]*", name[len(prefix):]) is not None)

    def orb(self, args, input: Optional[bytes] = None, timeout: float = 180) -> Tuple[int, bytes, bytes, bool]:
        """Run `orb` with these arguments. Returns (exit status, stdout, stderr, timed out)."""
        try:
            p = subprocess.run([self.cfg["orb"]] + list(args), input=input, capture_output=True, timeout=timeout)
            return p.returncode, p.stdout, p.stderr, False
        except FileNotFoundError:
            return 127, b"", ("`%s` is not installed on the host" % self.cfg["orb"]).encode(), False
        except subprocess.TimeoutExpired as e:
            return 124, e.stdout or b"", ("no answer within %ds" % timeout).encode(), True

    def machines(self) -> list:
        code, out, _, _ = self.orb(["list"], timeout=30)
        if code != 0:
            return []
        names = [line.split()[0] for line in out.decode("utf-8", "replace").splitlines() if line.split()]
        return [n for n in names if self.allowed(n)]

    def handle(self, req: dict) -> dict:
        op = req.get("op")
        cfg = self.cfg
        if op == "info":
            return {"ok": True, "manager": cfg["manager"], "template": cfg["template"], "prefix": cfg["prefix"]}
        if op == "list":
            return {"ok": True, "machines": self.machines()}
        name = req.get("name")
        if not self.allowed(name):
            self.log("refused: %s on %r" % (op, name))
            return {"error": "the host agent only acts on machines named %s<run>, not %r" % (cfg["prefix"], name)}

        def tail(err: bytes) -> str:
            return err.decode("utf-8", "replace").strip()[-300:] or "(no message)"

        if op == "create":
            code, _, err, _ = self.orb(["clone", cfg["template"], name])
            if code == 0:
                code, _, err, _ = self.orb(["start", name])
            self.log("create %s: %s" % (name, "ok" if code == 0 else tail(err)))
            if code != 0:
                return {"error": "could not create %s from %s: %s" % (name, cfg["template"], tail(err))}
            return {"ok": True, "name": name}
        if op == "delete":
            code, _, err, _ = self.orb(["delete", "-f", name])
            self.log("delete %s: %s" % (name, "ok" if code == 0 else tail(err)))
            if code != 0:
                return {"error": "could not delete %s: %s" % (name, tail(err))}
            return {"ok": True}
        if op == "exec":
            try:
                stdin = base64.b64decode(req.get("stdin") or "")
                timeout = max(1.0, min(float(req.get("timeout") or 120), 3600.0))
            except (ValueError, TypeError) as e:
                return {"error": "bad request: %s" % e}
            code, out, err, timed_out = self.orb(["-m", name, "bash", "-c", str(req.get("script") or "")], stdin, timeout)
            return {"ok": True, "rc": code, "timed_out": timed_out, "stdout": _b64(out[:MAX_OUTPUT]), "stderr": _b64(err[-65536:])}
        return {"error": "unknown request %r" % op}

    # ------------------------------------------------------------ the link to the manager
    def session(self) -> int:
        """Open the bridge in the manager's machine and serve it until it ends."""
        cfg = self.cfg
        script = BRIDGE + ((" --dash-port %d" % int(cfg["dash_port"])) if cfg.get("dash_port") else "")
        try:
            proc = subprocess.Popen([cfg["orb"], "-m", cfg["manager"], "bash", "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        except FileNotFoundError:
            self.log("`%s` is not installed on this machine" % cfg["orb"])
            return 127
        self._proc = proc
        wlock = threading.Lock()

        def send(obj: dict) -> bool:
            data = json.dumps(obj).encode("utf-8") + b"\n"
            try:
                with wlock:
                    proc.stdin.write(data)
                    proc.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

        def answer(req: dict) -> None:
            try:
                out = self.handle(req)
            except Exception as e:  # one bad request must not end the link
                out = {"error": "%s: %s" % (type(e).__name__, e)}
            out["id"] = req.get("id")
            send(out)

        def ping() -> None:
            while proc.poll() is None and not self.stopping:
                if not send({"ping": int(time.time())}):
                    return
                for _ in range(PING_EVERY * 4):
                    if proc.poll() is not None or self.stopping:
                        return
                    time.sleep(0.25)

        threading.Thread(target=ping, daemon=True).start()
        try:
            for raw in proc.stdout:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    text = raw.decode("utf-8", "replace").strip()
                    if text:
                        self.log("from %s: %s" % (cfg["manager"], text[:300]))
                    continue
                if not isinstance(msg, dict):
                    continue
                if "hello" in msg:
                    hello = msg["hello"] or {}
                    self.connected = True
                    self.log("connected to %s (ledger %s)" % (cfg["manager"], hello.get("ledger")))
                    if hello.get("dash_port"):
                        threading.Thread(target=self._dash, args=(int(hello["dash_port"]),), daemon=True).start()
                elif "id" in msg:
                    threading.Thread(target=answer, args=(msg,), daemon=True).start()
        finally:
            self.connected = False
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            proc.stdout.close()
        return proc.returncode

    def run(self) -> int:
        cfg = self.cfg
        self.log("org agent: manager %s; engineers %s<run>, cloned from %s" % (cfg["manager"], cfg["prefix"], cfg["template"]))
        delay = 1.0
        while not self.stopping:
            began = time.time()
            code = self.session()
            if self.stopping:
                break
            if time.time() - began > 60:
                delay = 1.0
            self.log("the link to %s ended (exit %s); trying again in %ds" % (cfg["manager"], code, delay))
            until = time.time() + delay
            while time.time() < until and not self.stopping:
                time.sleep(0.2)
            delay = min(delay * 2, 30.0)
        return 0

    def stop(self) -> None:
        self.stopping = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    # ------------------------------------------------------------ the dashboard's port
    def _dash(self, port: int) -> None:
        """Make the manager's dashboard reachable at this machine's localhost, if it is not already."""
        if not self._dash_lock.acquire(blocking=False):
            return  # already forwarding, or another thread is finding out
        try:
            for _ in range(8):
                if _is_dash(port):
                    self.log("dashboard: http://127.0.0.1:%d (OrbStack forwards the port)" % port)
                    return
                time.sleep(0.5)
            try:
                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind(("127.0.0.1", port))
                srv.listen(64)
            except OSError as e:
                self.log("dashboard: cannot forward 127.0.0.1:%d on this machine (%s); `org up --port <n>` picks another" % (port, e))
                return
            self.log("dashboard: http://127.0.0.1:%d (forwarded by this agent)" % port)
            srv.settimeout(1.0)
            while not self.stopping:
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(target=self._relay, args=(conn, port), daemon=True).start()
            srv.close()
        finally:
            self._dash_lock.release()

    def _relay(self, conn: socket.socket, port: int) -> None:
        """One browser connection, carried to the dashboard inside the manager's machine."""
        cfg = self.cfg
        try:
            proc = subprocess.Popen([cfg["orb"], "-m", cfg["manager"], "bash", "-c", PIPE % port],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            conn.close()
            return

        def up() -> None:  # browser -> dashboard, a base64 line per chunk
            try:
                while True:
                    data = conn.recv(65536)
                    if not data:
                        break
                    proc.stdin.write(base64.b64encode(data) + b"\n")
                    proc.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                proc.stdin.close()
            except OSError:
                pass

        threading.Thread(target=up, daemon=True).start()
        try:
            for line in proc.stdout:  # dashboard -> browser
                conn.sendall(base64.b64decode(line))
        except (OSError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdout.close()


def serve(config_path: Optional[str] = None) -> int:
    """`org agent run`, on the Mac."""
    try:
        agent = Agent(load_config(config_path))
    except AgentError as e:
        print("org agent: %s" % e, file=sys.stderr)
        return 2

    def stop(signum, frame):
        agent.stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return agent.run()


# ---------------------------------------------------------------- in the manager's machine


def socket_path(home) -> Path:
    return Path(home) / "agent.sock"


def _address(path: str):
    """A unix socket address for path. A path too long for one goes by way of its directory."""
    if len(os.fsencode(path)) < 100:
        return path, None
    fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
    return "/proc/self/fd/%d/%s" % (fd, os.path.basename(path)), fd


def _peer_ok(conn: socket.socket) -> bool:
    """Is the caller this user, on this machine? A process in another machine has no pid here."""
    try:
        pid, uid, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    except (AttributeError, OSError):
        return True  # not Linux: the socket file's mode is the gate
    return uid == os.getuid() and pid > 0


def bridge(ledger, dash_port: Optional[int] = None) -> int:
    """`org agent bridge`: the manager's end of the link. The host agent starts it and is its stdin and stdout."""
    import fcntl

    channel = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)             # nothing else in this process may write to the channel
    sys.stdout = sys.stderr
    inp = os.fdopen(os.dup(0), "rb")
    ledger.init()
    lock = open(ledger.root / "agent.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("org: another bridge is already running for %s" % ledger.root, file=sys.stderr)
        return 3
    path = str(socket_path(ledger.root))
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    addr, fd = _address(path)
    old = os.umask(0o177)
    try:
        server.bind(addr)
    finally:
        os.umask(old)
        if fd is not None:
            os.close(fd)
    server.listen(64)

    pending: dict = {}
    plock, wlock = threading.Lock(), threading.Lock()
    counter = itertools.count(1)
    heard = [time.time()]
    done = threading.Event()

    def send(obj: dict) -> None:
        data = json.dumps(obj).encode("utf-8") + b"\n"
        with wlock:
            channel.write(data)

    def shutdown(why: str) -> None:
        if done.is_set():
            return
        done.set()
        with plock:
            waiting = list(pending.values())
        for q in waiting:
            try:
                q.put_nowait({"error": "the host agent went away (%s)" % why})
            except queue.Full:
                pass
        try:
            os.unlink(path)
        except OSError:
            pass
        try:
            server.close()
        except OSError:
            pass

    def reader() -> None:
        try:
            for raw in inp:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                heard[0] = time.time()
                if "id" in msg:
                    with plock:
                        q = pending.get(msg["id"])
                    if q is not None:
                        q.put(msg)
        except OSError:
            pass
        shutdown("its pipe closed")

    def watchdog() -> None:
        while not done.wait(5):
            if time.time() - heard[0] > PING_TIMEOUT:
                shutdown("nothing heard for %ds" % PING_TIMEOUT)

    def serve_client(conn: socket.socket) -> None:
        with conn:
            if not _peer_ok(conn):
                return
            f = conn.makefile("rwb")
            try:
                raw = f.readline(MAX_LINE + 1)
                if not raw or len(raw) > MAX_LINE:
                    return
                req = json.loads(raw)
                if not isinstance(req, dict):
                    return
            except (OSError, ValueError):
                return
            rid, q = next(counter), queue.Queue(1)
            with plock:
                pending[rid] = q
            req["id"] = rid
            wait = float(req.get("timeout") or 120) + 60
            try:
                send(req)
                ans = q.get(timeout=wait)
            except queue.Empty:
                ans = {"error": "the host agent did not answer within %ds" % wait}
            except OSError as e:
                ans = {"error": "the host agent went away (%s)" % e}
            finally:
                with plock:
                    pending.pop(rid, None)
            ans.pop("id", None)
            try:
                f.write(json.dumps(ans).encode("utf-8") + b"\n")
                f.flush()
            except OSError:
                pass

    if dash_port:
        from . import dash

        try:
            httpd = dash.make_server(ledger, int(dash_port), "127.0.0.1")
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            threading.Thread(target=dash._sync_loop, args=(httpd,), daemon=True).start()
        except OSError as e:
            print("org: the dashboard was not started: cannot listen on 127.0.0.1:%s (%s)" % (dash_port, e), file=sys.stderr)
            dash_port = None

    def terminated(signum, frame):
        shutdown("it was stopped")  # so the next caller is told at once, not after trying a dead socket
        os._exit(0)

    signal.signal(signal.SIGTERM, terminated)
    signal.signal(signal.SIGHUP, terminated)
    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    send({"hello": {"version": 1, "ledger": str(ledger.root), "dash_port": dash_port}})
    while not done.is_set():
        try:
            conn, _ = server.accept()
        except OSError:
            break
        threading.Thread(target=serve_client, args=(conn,), daemon=True).start()
    shutdown("the socket closed")
    return 0


def pipe(port: int) -> int:
    """`org agent pipe <port>`: carry one connection to a port on this machine's loopback.

    Bytes travel as base64 lines in both directions, so nothing depends on the command that
    carries them being binary-clean. The host agent uses this for the dashboard's port.
    """
    try:
        s = socket.create_connection(("127.0.0.1", int(port)), timeout=10)
    except OSError as e:
        print("org: nothing is listening on 127.0.0.1:%s here (%s)" % (port, e), file=sys.stderr)
        return 1
    s.settimeout(None)
    out = os.fdopen(os.dup(1), "wb", buffering=0)

    def down() -> None:
        try:
            while True:
                data = s.recv(65536)
                if not data:
                    break
                out.write(base64.b64encode(data) + b"\n")
        except OSError:
            pass
        os._exit(0)

    threading.Thread(target=down, daemon=True).start()
    try:
        for line in os.fdopen(os.dup(0), "rb"):
            s.sendall(base64.b64decode(line))
    except (OSError, ValueError):
        pass
    try:
        s.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    threading.Event().wait(3600)  # the reply is still arriving; down() ends the process
    return 0


class Client:
    """What an org process in the manager's machine uses to ask the host agent for something."""

    _down: dict = {}  # socket path -> the time until which it is taken to be down

    def __init__(self, path):
        self.path = str(path)

    def _connect(self) -> socket.socket:
        if Client._down.get(self.path, 0) > time.time():
            raise AgentError(NOT_CONNECTED)
        deadline = time.time() + 3  # a bridge that is reconnecting is back within a second or two
        while True:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            fd = None
            try:
                addr, fd = _address(self.path)
                s.connect(addr)
                return s
            except OSError:
                s.close()
                if time.time() >= deadline:
                    Client._down[self.path] = time.time() + 5  # so twenty runs do not each wait in turn
                    raise AgentError(NOT_CONNECTED)
                time.sleep(0.25)
            finally:
                if fd is not None:
                    os.close(fd)

    def call(self, req: dict, timeout: float = 60) -> dict:
        s = self._connect()
        try:
            s.settimeout(timeout + 90)
            f = s.makefile("rwb")
            f.write(json.dumps(dict(req, timeout=timeout)).encode("utf-8") + b"\n")
            f.flush()
            raw = f.readline()
        except OSError as e:
            raise AgentError("the host agent did not answer (%s)" % (e or type(e).__name__))
        finally:
            s.close()
        try:
            ans = json.loads(raw) if raw else None
        except ValueError:
            ans = None
        if not isinstance(ans, dict):
            raise AgentError("the host agent closed the connection without an answer")
        if ans.get("error"):
            raise AgentError(str(ans["error"]))
        return ans

    def info(self) -> dict:
        return self.call({"op": "info"}, timeout=15)

    def machines(self) -> list:
        return self.call({"op": "list"}, timeout=40).get("machines") or []

    def create(self, name: str) -> None:
        self.call({"op": "create", "name": name}, timeout=400)

    def delete(self, name: str) -> None:
        self.call({"op": "delete", "name": name}, timeout=200)

    def exec(self, name: str, script: str, stdin: Optional[bytes] = None, timeout: float = 120) -> Tuple[int, bytes, bytes, bool]:
        out = self.call({"op": "exec", "name": name, "script": script, "stdin": _b64(stdin or b"")}, timeout=timeout)
        return (int(out.get("rc", 1)), base64.b64decode(out.get("stdout") or ""), base64.b64decode(out.get("stderr") or ""),
                bool(out.get("timed_out")))


class AgentTransport(Transport):
    """An OrbStack machine reached through the host agent instead of the `orb` command."""

    kind = "orb"

    def __init__(self, client: Client, machine: str):
        self.client = client
        self.name = machine

    def argv(self, script: str):
        return ["orb", "-m", self.name, "bash", "-c", script]  # what the agent runs, on the Mac

    def run(self, script: str, input: Optional[bytes] = None, timeout: float = 120, check: bool = True):
        try:
            code, out, err, timed_out = self.client.exec(self.name, script, input, timeout)
        except AgentError as e:
            raise TransportError("%s: %s" % (self.describe(), e))
        if timed_out:
            raise TransportError("%s: no answer within %ds" % (self.describe(), timeout))
        if check and code != 0:
            raise TransportError("%s: exit %d: %s" % (self.describe(), code, err.decode("utf-8", "replace").strip()[-600:] or "(no stderr)"))
        return subprocess.CompletedProcess(self.argv("…"), code, out, err)

    def attach_hint(self, run_id: str) -> str:
        return "orb -m %s tmux attach -t org-%s" % (self.name, run_id)  # typed on the Mac
