"""Rendering a JavaScript page in headless Chromium, honestly and safely (D68a).

The browser runs in a child process (renderchild.py) with an empty environment, so it holds none of the
container's secrets. It reaches the network only through SafeProxy, a small proxy in this process that resolves
each host once with the crawler's resolver, refuses the request unless every address is public, and connects to
the checked address (the same rule as the fetcher, so DNS tricks cannot point the browser at a private address).
The child navigates only within approved hosts and skips images, media and fonts. It sends the honest user agent.
No fingerprint spoofing, challenge solving or proxy rotation: a site that refuses an honest browser stays refused.
"""
import json
import os
import signal
import socket
import socketserver
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

RENDER_TIMEOUT_SECONDS = 90
NAVIGATION_TIMEOUT_MS = 30_000
MAX_OUTPUT_BYTES = 20 * 1024 * 1024
MAX_HEAD_BYTES = 64 * 1024
CONNECT_TIMEOUT = 10
IDLE_TIMEOUT = 30


class RenderError(Exception):
    pass


@dataclass
class Rendered:
    status: int
    content_type: str
    html: bytes
    final_url: str
    refused: list


class _Refused(Exception):
    pass


class SafeProxy:
    """An HTTP proxy on 127.0.0.1 for one render: CONNECT for https, absolute-URI GET/HEAD for http."""

    def __init__(self, resolver, ip_allowed):
        self.resolver = resolver
        self.ip_allowed = ip_allowed
        self.refused = []
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                proxy._handle(self.request)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def _connect(self, host, port):
        try:
            ips = list(self.resolver(host))
        except Exception as exc:
            raise _Refused(f"{host}: no address ({exc})") from exc
        if not ips or not all(self.ip_allowed(ip) for ip in ips):
            self.refused.append(host)
            raise _Refused(f"{host}: not a public address")
        upstream = socket.create_connection((ips[0], port), timeout=CONNECT_TIMEOUT)
        upstream.settimeout(IDLE_TIMEOUT)
        return upstream

    @staticmethod
    def _read_head(sock):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_HEAD_BYTES:
                raise _Refused("request head too large")
        head, _, rest = data.partition(b"\r\n\r\n")
        return head, rest

    def _handle(self, client):
        client.settimeout(IDLE_TIMEOUT)
        upstream = None
        try:
            head, rest = self._read_head(client)
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = (lines[0].split(" ") + ["", "", ""])[:3]
            if method == "CONNECT":
                host, _, port = target.rpartition(":")
                upstream = self._connect(host.strip("[]"), int(port or 443))
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
                self._pipe(client, upstream)
                return
            if method not in ("GET", "HEAD") or not target.startswith("http://"):
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                return
            hostport, _, path = target[len("http://"):].partition("/")
            host, _, port = hostport.rpartition(":") if ":" in hostport else (hostport, "", "80")
            upstream = self._connect(host.strip("[]"), int(port or 80))
            keep = [l for l in lines[1:] if l.split(":", 1)[0].strip().lower() not in ("proxy-connection", "connection",
                                                                                       "keep-alive")]
            request = "\r\n".join([f"{method} /{path} HTTP/1.1", *keep, "Connection: close", "", ""]).encode("latin-1")
            upstream.sendall(request)
            self._copy(upstream, client)
        except _Refused:
            try:
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
        except (OSError, ValueError):
            pass
        finally:
            for s in (client, upstream):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    @staticmethod
    def _copy(src, dst):
        try:
            while True:
                chunk = src.recv(65536)
                if not chunk:
                    break
                dst.sendall(chunk)
        except OSError:
            pass

    def _pipe(self, a, b):
        other = threading.Thread(target=self._copy, args=(b, a), daemon=True)
        other.start()
        self._copy(a, b)
        try:
            b.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        other.join(IDLE_TIMEOUT)


class Renderer:
    """Renders one URL per call. A fresh browser and proxy each time: nothing carries over between pages."""

    def __init__(self, timeout=RENDER_TIMEOUT_SECONDS):
        self.timeout = timeout

    def render(self, url, *, resolver, ip_allowed, approved_hosts, user_agent):
        child = Path(__file__).with_name("renderchild.py")
        with SafeProxy(resolver, ip_allowed) as proxy:
            config = {"url": url, "proxy": f"http://127.0.0.1:{proxy.port}", "approved": list(approved_hosts),
                      "user_agent": user_agent, "timeout_ms": NAVIGATION_TIMEOUT_MS}
            env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/tmp",
                   "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright")}
            try:
                proc = subprocess.Popen([sys.executable, "-I", str(child)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=env, start_new_session=True)
            except OSError as exc:
                raise RenderError(f"could not start the browser: {exc}") from exc
            try:
                out, err = proc.communicate(json.dumps(config).encode(), timeout=self.timeout)
            except subprocess.TimeoutExpired as exc:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                raise RenderError("the page took too long to render") from exc
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
            refused = list(proxy.refused)
        if proc.returncode != 0 or len(out) > MAX_OUTPUT_BYTES:
            raise RenderError("the browser could not render the page: " + err.decode("utf-8", "replace")[-300:])
        try:
            answer = json.loads(out)
            return Rendered(int(answer["status"]), str(answer.get("content_type") or ""), answer["html"].encode("utf-8"),
                            str(answer["final_url"]), refused)
        except (ValueError, KeyError, TypeError) as exc:
            raise RenderError("the browser gave an unusable answer") from exc
