"""A small local HTTP server that stands in for `notify.url`, so no test sends a message to a real service.

    with Receiver(statuses=[500, 500, 200]) as r:
        ... put r.url in the config ...
        r.requests  # [{"method", "path", "headers", "body"}, ...] in the order they arrived

Each request is answered with the next scripted status; once the script is used up every request gets
`default` (200). A status in `redirects` is answered with a `Location` header too, pointing at `location`.
"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Receiver:
    def __init__(self, statuses=(), default=200, location="/elsewhere"):
        self.statuses = list(statuses)
        self.default = default
        self.location = location
        self.requests = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(size).decode("utf-8", "replace")
                with outer._lock:
                    outer.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})
                    status = outer.statuses.pop(0) if outer.statuses else outer.default
                self.send_response(status)
                if 300 <= status < 400:
                    self.send_header("Location", outer.location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_PUT = do_GET = do_POST

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = None

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/topic" % self._server.server_address[1]

    def start(self):
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False
