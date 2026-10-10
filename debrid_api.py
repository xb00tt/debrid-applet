"""Thin clients for Real-Debrid and Torbox. No GTK here; safe to import anywhere."""
import os

import requests

TIMEOUT = 15
USER_AGENT = "debrid-applet"


class ApiError(Exception):
    """User-facing error message from a service call."""


def human_size(n):
    n = float(n or 0)
    if n < 1024:
        return f"{n:.0f} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PB"


class _Client:
    BASE = ""
    name = ""

    def __init__(self, token, session=None):
        self.token = token
        self.s = session or requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT})

    def _req(self, method, path, **kw):
        try:
            r = self.s.request(method, self.BASE + path, timeout=TIMEOUT, **kw)
        except requests.RequestException as e:
            raise ApiError(f"Network error: {e}") from e
        if r.status_code in (401, 403):
            raise ApiError("Invalid API key")
        if r.status_code == 429:
            raise ApiError("Rate limited, retrying next tick")
        if r.status_code >= 400:
            raise ApiError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r


class RealDebrid(_Client):
    BASE = "https://api.real-debrid.com/rest/1.0"
    name = "Real-Debrid"

    def _json(self, method, path, **kw):
        r = self._req(method, path, **kw)
        return None if r.status_code == 204 or not r.text else r.json()

    def account(self):
        u = self._json("GET", "/user") or {}
        return {
            "plan": u.get("type", "?"),
            "expires": (u.get("expiration") or "")[:10],
            "extra": f"{u.get('points', 0)} points",
        }

    def torrents(self):
        out = []
        for t in self._json("GET", "/torrents", params={"limit": 100}) or []:
            if t.get("status") == "waiting_files_selection":
                self._select_all(t["id"])
            out.append({
                "id": str(t["id"]),
                "name": t.get("filename", ""),
                "progress": float(t.get("progress") or 0),
                "state": t.get("status", ""),
                "done": t.get("status") == "downloaded",
                "size": int(t.get("bytes") or 0),
                "seeds": int(t.get("seeders") or 0),
                "speed": int(t.get("speed") or 0),
                "kind": "torrent",
            })
        return out

    def _select_all(self, tid):
        self._req("POST", f"/torrents/selectFiles/{tid}", data={"files": "all"})

    def add_magnet(self, magnet):
        r = self._json("POST", "/torrents/addMagnet", data={"magnet": magnet})
        self._select_all(r["id"])

    def add_torrent_file(self, path):
        with open(path, "rb") as f:
            r = self._json("PUT", "/torrents/addTorrent", data=f.read())
        self._select_all(r["id"])

    def delete(self, tid, kind="torrent"):
        self._req("DELETE", f"/torrents/delete/{tid}")

    def links(self, tid, kind="torrent"):
        info = self._json("GET", f"/torrents/info/{tid}") or {}
        out = []
        for link in info.get("links") or []:
            u = self._json("POST", "/unrestrict/link", data={"link": link})
            out.append({"name": u.get("filename") or link, "url": u["download"]})
        return out

    def add_link(self, url):
        return self._json("POST", "/unrestrict/link", data={"link": url})["download"]
