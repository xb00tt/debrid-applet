"""Tiny loopback HTTP API for the applet: add items, toggle the window, read status. No GTK here."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def parse_add(query, body):
    """(service, items) from ?service=&magnet=&link=&item= plus one item per body line."""
    q = parse_qs(query)
    items = q.get("magnet", []) + q.get("link", []) + q.get("item", [])
    items += [line.strip() for line in (body or "").splitlines() if line.strip()]
    return (q.get("service") or [""])[0], items


class LocalApi:
    """Routes: /add (GET query or POST body), /toggle, /status. Callbacks run on the server thread;
    the caller is responsible for hopping to its UI thread inside them."""

    def __init__(self, port, on_add, on_toggle, status):
        self.port = port
        self.on_add, self.on_toggle, self.status = on_add, on_toggle, status
        self.server = None

    def start(self):
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):  # keep the applet log quiet
                pass

            def _send(self, code, payload):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _route(self, body):
                url = urlparse(self.path)
                if url.path == "/add":
                    service, items = parse_add(url.query, body)
                    if not items:
                        return self._send(400, {"ok": False, "error": "no magnet, link or item given"})
                    queued = api.on_add(service, items)
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
                length = int(self.headers.get("Content-Length") or 0)
                self._route(self.rfile.read(length).decode("utf-8", "replace") if length else "")

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
