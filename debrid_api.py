"""Thin clients for Real-Debrid and Torbox. No GTK here; safe to import anywhere."""
import os

import requests

TIMEOUT = 15
USER_AGENT = "debrid-applet"
INVALID_KEY = "Invalid API key"
TORBOX_AUTH_ERRORS = ("NO_AUTH", "BAD_TOKEN", "AUTH_ERROR")


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
        if r.status_code == 401:
            raise ApiError(INVALID_KEY)
        if r.status_code == 403:
            # Torbox uses 403 for bad tokens; Real-Debrid uses it for permission errors with a message.
            body = _json_or_none(r)
            if body is None or body.get("error") in TORBOX_AUTH_ERRORS:
                raise ApiError(INVALID_KEY)
            raise ApiError(_error_message(r))
        if r.status_code == 429:
            raise ApiError("Rate limited, retrying next tick")
        if r.status_code >= 400:
            raise ApiError(_error_message(r))
        return r


def _body(r, service):
    """Decode JSON or raise a readable ApiError (Cloudflare pages, maintenance HTML)."""
    try:
        return r.json()
    except ValueError:
        raise ApiError(f"Unexpected response from {service}") from None


def _direct_url(u):
    """Real-Debrid unrestrict: 'download', or the first 'alternative' for hosters without a direct file."""
    url = u.get("download") or (u.get("alternative") or [{}])[0].get("download")
    if not url:
        raise ApiError("No direct link returned")
    return url


def _json_or_none(r):
    try:
        body = r.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _error_message(r):
    """Prefer the service's own message (Torbox 'detail', Real-Debrid 'error') over raw body."""
    body = _json_or_none(r) or {}
    msg = body.get("detail") or body.get("error")
    return str(msg) if msg else f"HTTP {r.status_code}: {r.text[:200]}"


class RealDebrid(_Client):
    BASE = "https://api.real-debrid.com/rest/1.0"
    name = "Real-Debrid"

    def _json(self, method, path, **kw):
        r = self._req(method, path, **kw)
        return None if r.status_code == 204 or not r.text else _body(r, self.name)

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
            state = t.get("status", "")
            if state == "waiting_files_selection":
                try:
                    self._select_all(t["id"])
                except ApiError as e:
                    state = f"select failed: {e}"
            out.append({
                "id": str(t["id"]),
                "name": t.get("filename", ""),
                "progress": float(t.get("progress") or 0),
                "state": state,
                "done": t.get("status") == "downloaded",
                "size": int(t.get("bytes") or 0),
                "seeds": int(t.get("seeders") or 0),
                "speed": int(t.get("speed") or 0),
                "kind": "torrent",
                "up_speed": None,  # Real-Debrid does not report these three
                "peers": None,
                "ratio": None,
                "added": t.get("added") or "",
                "finished": t.get("ended") or "",
                "hash": t.get("hash") or "",
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
            try:
                u = self._json("POST", "/unrestrict/link", data={"link": link})
            except ApiError as e:
                out.append({"name": f"Failed: {e}", "url": None})
                break  # keep what we have; retrying immediately would only add more 429s
            try:
                out.append({"name": u.get("filename") or link, "url": _direct_url(u)})
            except ApiError as e:
                out.append({"name": f"{u.get('filename') or link}: {e}", "url": None})
        return out

    def add_link(self, url):
        return _direct_url(self._json("POST", "/unrestrict/link", data={"link": url}))


class Torbox(_Client):
    BASE = "https://api.torbox.app/v1/api"
    name = "Torbox"
    PLANS = {0: "Free", 1: "Essential", 2: "Pro", 3: "Standard"}
    def _json(self, method, path, **kw):
        body = _body(self._req(method, path, **kw), self.name)
        if not isinstance(body, dict):
            raise ApiError(f"Unexpected response from {self.name}")
        if not body.get("success"):
            if body.get("error") in TORBOX_AUTH_ERRORS:
                raise ApiError(INVALID_KEY)
            raise ApiError(body.get("detail") or body.get("error") or "Torbox error")
        return body.get("data")

    def account(self):
        u = self._json("GET", "/user/me") or {}
        return {
            "plan": self.PLANS.get(u.get("plan"), str(u.get("plan"))),
            "expires": (u.get("premium_expires_at") or "")[:10],
            "extra": f"{u.get('total_downloaded', 0)} downloads",
        }

    def torrents(self):
        out = []
        for kind, path in (("torrent", "/torrents/mylist"), ("webdl", "/webdl/mylist")):
            for t in self._json("GET", path, params={"bypass_cache": "true"}) or []:
                state = t.get("download_state") or ""
                done = bool(t.get("download_finished")) and bool(t.get("download_present"))
                out.append({
                    "id": str(t["id"]),
                    "name": t.get("name", ""),
                    "progress": min(100.0, float(t.get("progress") or 0) * 100),  # API gives 0..1
                    "state": state if kind == "torrent" else f"web: {state}",
                    "done": done,
                    "size": int(t.get("size") or 0),
                    "seeds": int(t.get("seeds") or 0),
                    "speed": int(t.get("download_speed") or 0),
                    "kind": kind,
                    "up_speed": int(t.get("upload_speed") or 0),
                    "peers": int(t.get("peers") or 0),
                    "ratio": float(t.get("ratio") or 0),
                    "added": t.get("created_at") or "",
                    "finished": (t.get("updated_at") or "") if done else "",  # no finish timestamp in the API
                    "hash": t.get("hash") or "",
                })
        return out

    def add_magnet(self, magnet):
        self._json("POST", "/torrents/createtorrent", files={"magnet": (None, magnet)})

    def add_torrent_file(self, path):
        with open(path, "rb") as f:
            self._json("POST", "/torrents/createtorrent", files={"file": (os.path.basename(path), f, "application/x-bittorrent")})

    def delete(self, tid, kind="torrent"):
        if kind == "webdl":
            self._json("POST", "/webdl/controlwebdownload", json={"webdl_id": int(tid), "operation": "delete"})
        else:
            self._json("POST", "/torrents/controltorrent", json={"torrent_id": int(tid), "operation": "delete"})

    def links(self, tid, kind="torrent"):
        if kind == "webdl":
            list_path, dl_path, id_key = "/webdl/mylist", "/webdl/requestdl", "web_id"
        else:
            list_path, dl_path, id_key = "/torrents/mylist", "/torrents/requestdl", "torrent_id"
        item = self._json("GET", list_path, params={"id": int(tid), "bypass_cache": "true"}) or {}
        files = item.get("files") or []
        out = []
        if len(files) > 1:  # one request for everything, before the per-file ones
            try:
                zip_url = self._json("GET", dl_path, params={"token": self.token, id_key: int(tid), "zip_link": "true"})
                out.append({"name": "All files (zip)", "url": zip_url})
            except ApiError as e:
                out.append({"name": f"Zip failed: {e}", "url": None})
        for f in files:
            try:
                url = self._json("GET", dl_path, params={"token": self.token, id_key: int(tid), "file_id": f["id"]})
            except ApiError as e:
                out.append({"name": f"Failed: {e}", "url": None})
                break
            out.append({"name": f.get("short_name") or f.get("name") or str(f["id"]), "url": url})
        return out

    def add_link(self, url):
        self._json("POST", "/webdl/createwebdownload", data={"link": url})
        return None
