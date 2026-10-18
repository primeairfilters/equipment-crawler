"""A fake web server and a fake clock for the fetcher tests. Everything stays in this process.

The server listens on 127.0.0.1 only. Tests reach it through hostnames like ruud.test that an
injected resolver maps to 127.0.0.1, so the Host header carries the "site" and one server plays
every manufacturer. Every request is recorded with the FAKE time at which it arrived, which is
how the tests check the 10 second gap without ever sleeping.
"""
import threading
import zlib
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeClock:
    def __init__(self):
        self.t = datetime(2026, 1, 1, 12, 0, 0)
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += timedelta(seconds=seconds)

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


def gzip_zeros(n_bytes, prefix=b""):
    """A gzip stream that decodes to prefix + n_bytes of zeros. Tiny on the wire, huge decoded."""
    comp = zlib.compressobj(9, zlib.DEFLATED, 31)
    out = [comp.compress(prefix)] if prefix else []
    chunk = b"\0" * (1024 * 1024)
    left = n_bytes
    while left > 0:
        piece = chunk[: min(left, len(chunk))]
        out.append(comp.compress(piece))
        left -= len(piece)
    out.append(comp.flush())
    return b"".join(out)


def gzip_bytes(data):
    comp = zlib.compressobj(9, zlib.DEFLATED, 31)
    return comp.compress(data) + comp.flush()


class FakeSite:
    def __init__(self, clock):
        self.clock = clock
        self.routes = {}
        self.requests = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
                rec = {
                    "t": site.clock.now(),
                    "host": host,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                }
                site.requests.append(rec)
                bare = self.path.split("?", 1)[0]  # a route for "/x" also answers "/x?anything"
                route = (site.routes.get((host, self.path)) or site.routes.get(("*", self.path))
                         or site.routes.get((host, bare)) or site.routes.get(("*", bare)))
                if route is None:
                    status, headers, body = 404, {}, b"not found"
                elif callable(route):
                    status, headers, body = route(rec)
                else:
                    status, headers, body = route
                try:
                    self.send_response(status)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if status != 304:
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def route(self, path, body=b"", status=200, headers=None, host="*"):
        """A fixed answer, or pass body=<callable(request_record) -> (status, headers, body)>."""
        if callable(body):
            self.routes[(host, path)] = body
        else:
            self.routes[(host, path)] = (status, dict(headers or {}), body)

    def url(self, host, path):
        return f"http://{host}:{self.port}{path}"

    def hits(self, path=None, host=None):
        return [
            r for r in self.requests
            if (path is None or r["path"] == path) and (host is None or r["host"] == host)
        ]

    def page_hits(self):
        return [r for r in self.requests if r["path"] != "/robots.txt"]
