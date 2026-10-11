"""Tiny loopback HTTP API for the applet: add items, toggle the window, read status. No GTK here.

Every request must carry the token (header X-Debrid-Token or ?token=), the Host header must be loopback, and
no CORS headers are sent, so a web page cannot drive the applet without the token."""
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def parse_add(query, body):
    """(service, items) from ?service=&magnet=&link=&item= plus one item per body line."""
    q = parse_qs(query)
    items = q.get("magnet", []) + q.get("link", []) + q.get("item", [])
    items += [line.strip() for line in (body or "").splitlines() if line.strip()]
    return (q.get("service") or [""])[0], items


def _remote_ok(item):
    return item.lower().startswith(("magnet:", "http://", "https://"))


class LocalApi:
    """Routes: /add (GET query or POST body), /toggle, /status. Callbacks run on the server thread;
    the caller is responsible for hopping to its UI thread inside them."""

    def __init__(self, port, token, on_add, on_toggle, status):
        self.port = port
        self.token = token
        self.on_add, self.on_toggle, self.status = on_add, on_toggle, status
        self.server = None

    def start(self):
        api = self

        class Handler(BaseHTTPRequestHandler):
            timeout = 10  # a client that stalls mid-request frees its thread instead of pinning it

            def log_message(self, *_):  # keep the applet log quiet
                pass

            def _send(self, code, payload):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self, query):
                host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
                if host not in LOOPBACK_HOSTS:
                    self._send(403, {"ok": False, "error": "loopback only"})
                    return False
                given = self.headers.get("X-Debrid-Token") or (parse_qs(query).get("token") or [""])[0]
                if not api.token or not hmac.compare_digest(given.encode(), api.token.encode()):  # str form rejects non-ASCII
                    self._send(401, {"ok": False, "error": "missing or wrong token (X-Debrid-Token header or ?token=)"})
                    return False
                return True

            def _route(self, body):
                url = urlparse(self.path)
                if not self._authorized(url.query):
                    return
                if url.path == "/add":
                    service, items = parse_add(url.query, body)
                    bad = [i for i in items if not _remote_ok(i)]
                    if not items or bad:
                        return self._send(400, {"ok": False, "error": "give magnet: or http(s):// items only"})
                    queued = api.on_add(service, items)
                    if not queued:
                        return self._send(400, {"ok": False, "error": f"unknown or unconfigured service {service!r}"})
                    return self._send(200, {"ok": True, "queued": queued})
                if url.path == "/toggle":
                    api.on_toggle()
                    return self._send(200, {"ok": True})
                if url.path == "/status":
                    return self._send(200, api.status())
                self._send(404, {"ok": False, "error": "unknown route; use /add, /toggle or /status"})

            def do_GET(self):
                self._route("")

            def do_POST(self):
                try:
                    length = max(0, int(self.headers.get("Content-Length") or 0))
                except ValueError:
                    return self._send(400, {"ok": False, "error": "bad Content-Length"})
                self._route(self.rfile.read(min(length, 1_000_000)).decode("utf-8", "replace") if length else "")

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
