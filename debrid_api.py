"""Thin clients for Torbox, Real-Debrid and AllDebrid. No GTK here; safe to import anywhere."""
import base64
import os
import re
from datetime import datetime, timezone

import requests

TIMEOUT = 15
USER_AGENT = "debrid-applet"
INVALID_KEY = "Invalid API key"
TORBOX_AUTH_ERRORS = ("NO_AUTH", "BAD_TOKEN")  # api-docs.torbox.app errors table: "no provided credentials" and "token
# invalid", client faults. Torbox's rule: a code ending in ERROR is the server's fault, so AUTH_ERROR ("error verifying
# the given authentication") is an outage, not a verdict on the key. A bad key answers HTTP 403 BAD_TOKEN (probed).


def is_invalid_key(message):
    """The service rejected the key: INVALID_KEY plus the evidence in brackets (status, code)."""
    return str(message).startswith(INVALID_KEY)

# One vocabulary for every service's download state; the service's own word travels along as "raw_state".
QUEUED, DOWNLOADING, PROCESSING, DONE, SEEDING, STALLED, EXPIRED, ERROR = (
    "Queued", "Downloading", "Processing online", "Done", "Seeding", "Stalled", "Expired", "Error")
RD_STATES = {"queued": QUEUED, "magnet_conversion": QUEUED, "waiting_files_selection": QUEUED, "downloading": DOWNLOADING,
             "compressing": PROCESSING, "uploading": PROCESSING, "downloaded": DONE,
             "error": ERROR, "magnet_error": ERROR, "virus": ERROR, "dead": ERROR}
TB_STATES = {"metadl": QUEUED, "checkingresumedata": QUEUED, "downloading": DOWNLOADING, "completed": DONE, "cached": DONE,
             "stopped seeding": DONE, "uploading": SEEDING, "stalled": STALLED, "paused": STALLED,
             "expired": EXPIRED, "failed": ERROR}
AD_STATES = {"in queue": QUEUED, "downloading": DOWNLOADING, "compressing / moving": PROCESSING, "uploading": PROCESSING,
             "ready": DONE}
# selectFiles answers that will not change by asking again; anything else (5xx, rate limit, "too many active downloads",
# a code this list does not know) is retried on the next poll.
RD_PERMANENT_SELECT_ERRORS = ("infringing_file", "file_not_allowed", "torrent_too_big", "torrent_file_invalid")


def state_label(raw, table):
    """The unified label for a service's state word. A parenthetical suffix ("uploading (no peers)") is ignored for
    the lookup and kept in raw_state. Words no table knows are shown as sent (first letter upper-cased), so an
    unexpected state never hides a row; a word mentioning an error or failure becomes "Error: <word>"."""
    raw = str(raw or "").strip()
    key = raw.lower().split(" (", 1)[0]
    if key in table:
        return table[key]
    if "error" in key or "fail" in key:
        return f"{ERROR}: {raw}"
    return raw[:1].upper() + raw[1:]


class ApiError(Exception):
    """User-facing error message from a service call."""


class NotFound(ApiError):
    """HTTP 404: the service does not know the item (deleted elsewhere, or never there)."""


_MAGNET_HASH = re.compile(r"xt=urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})")


def magnet_hash(magnet):
    """Info hash of a magnet link as lowercase hex, '' if absent. Base32 hashes are converted."""
    m = _MAGNET_HASH.search(magnet or "")
    if not m:
        return ""
    h = m.group(1)
    if len(h) == 32:
        h = base64.b32decode(h.upper()).hex()
    return h.lower()


def human_size(n):
    n = float(n or 0)
    if n < 1024:
        return f"{n:.0f} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PB"


def _num(v, cast=int):
    """int/float of a service field; 0 for missing, null or junk, so one odd row cannot sink a whole poll."""
    try:
        return cast(v or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


class _Client:
    BASE = ""
    name = ""
    LIGHT_COMPLETE = False  # True: a light poll plus stale() show every change there is, so the whole list is only
    # refetched every POLL_FULL even while something downloads (otherwise once a minute then, to catch what they miss)

    def __init__(self, token, session=None):
        self.token = token
        self.s = session or requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT})

    def _req(self, method, path, **kw):
        try:
            r = self.s.request(method, self.BASE + path, timeout=TIMEOUT, **kw)
        except requests.RequestException as e:  # the text embeds the URL, and Torbox puts the key in a query param
            raise ApiError(f"Network error: {str(e).replace(self.token, '***') if self.token else e}") from e
        if r.status_code in (401, 403):
            body = _json_or_none(r)
            code = _auth_code(body)
            if r.status_code == 401 or (body is not None and _is_auth_error(body)):
                raise ApiError(f"{INVALID_KEY} (HTTP {r.status_code}{' ' + code if code else ''})")
            # 403 with a service message (Real-Debrid permission errors) or without JSON (a CDN page): not a verdict on the key
            raise ApiError(_error_message(r))
        if r.status_code == 429:
            raise ApiError("Rate limited, retrying next tick")
        if r.status_code == 404:
            raise NotFound(_error_message(r))
        if r.status_code == 451:  # Real-Debrid's answer to a blocked hash: {"error": "infringing_file", "error_code": 35}
            raise ApiError(f"Refused for legal reasons ({_error_message(r)})")
        if r.status_code >= 400:
            raise ApiError(_error_message(r))
        return r

    def stale(self, cached):
        """After a light poll (torrents(active_only=True)) merged into cached: True when the service's list changed
        in a way the light poll cannot show (an item added or deleted elsewhere), as far as a cheap request can tell;
        the caller then fetches the whole list. Services that cannot tell say False. Every client's torrents()
        takes active_only and ids ((id, kind) of the cached items that can still change) and answers a light poll
        with the current state of the active items, or None when it has no cheaper query than the whole list (the
        caller then fetches that)."""
        return False


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


def _iso_from_ts(ts):
    """Unix seconds -> '2025-10-09T08:53:20Z'; '' for 0 / missing / junk."""
    try:
        ts = int(ts or 0)
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts > 0 else ""
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _auth_code(body):
    """The error code string of a service body: Torbox 'error': 'CODE', AllDebrid 'error': {'code': …}, Real-Debrid
    'error': 'bad_token'; '' when absent."""
    err = body.get("error") if isinstance(body, dict) else None
    code = err.get("code", "") if isinstance(err, dict) else err
    return str(code or "")


def _is_auth_error(body):
    """Torbox NO_AUTH / BAD_TOKEN, or AllDebrid's AUTH_* family (docs.alldebrid.com: AUTH_MISSING_APIKEY,
    AUTH_BAD_APIKEY, AUTH_BLOCKED, AUTH_USER_BANNED); never a code ending in ERROR, which both Torbox's convention and
    AllDebrid's naming reserve for server-side failures."""
    code = _auth_code(body)
    return code in TORBOX_AUTH_ERRORS or (code.startswith("AUTH_") and not code.endswith("ERROR"))


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
    if isinstance(msg, dict):  # AllDebrid: {"error": {"code", "message"}}
        msg = msg.get("message") or msg.get("code")
    return str(msg) if msg else f"HTTP {r.status_code}: {r.text[:200]}"


class RealDebrid(_Client):
    BASE = "https://api.real-debrid.com/rest/1.0"
    name = "Real-Debrid"
    short = "RD"
    LIGHT_COMPLETE = True  # the active filter shows progress and finishes, X-Total-Count adds and deletes

    def __init__(self, token, session=None):
        super().__init__(token, session)
        self._select_failed = {}  # id -> message of a permanent selectFiles failure; not re-sent on every poll
        self.total = None  # X-Total-Count of the last /torrents answer: every torrent, whatever the filter

    def _json(self, method, path, **kw):
        r = self._req(method, path, **kw)
        if path == "/torrents" and str(r.headers.get("X-Total-Count", "")).isdigit():
            self.total = int(r.headers["X-Total-Count"])
        return None if r.status_code == 204 or not r.text else _body(r, self.name)

    def stale(self, cached):
        return self.total is not None and self.total != len(cached)

    def account(self):
        u = self._json("GET", "/user") or {}
        return {
            "plan": str(u.get("type") or "?").capitalize(),
            "expires": (u.get("expiration") or "")[:10],
            "extra": f"{u.get('points', 0)} points",
        }

    def torrents(self, active_only=False, ids=()):
        """The whole list page by page, or with active_only the active items in one request: the filter only works
        without page (with it the whole page comes back, observed 2026-10)."""
        out = []
        for page in range(1, 41):  # 5000 is the API maximum per page; stop at the first short page (or 200 000 items)
            params = {"filter": "active", "limit": 5000} if active_only else {"limit": 5000, "page": page}
            chunk = self._json("GET", "/torrents", params=params) or []
            for t in chunk:  # page by page: the raw page is garbage before the next one is fetched
                if t.get("id") is None:
                    continue
                raw = t.get("status", "")
                state = state_label(raw, RD_STATES)
                if raw == "waiting_files_selection":
                    err = self._select_failed.get(t["id"])
                    if err is None:
                        try:
                            self._select_all(t["id"])
                        except ApiError as e:
                            err = str(e)
                            if any(code in err for code in RD_PERMANENT_SELECT_ERRORS):
                                self._select_failed[t["id"]] = err
                    if err:
                        state = f"{ERROR}: select failed: {err}"
                out.append({
                    "id": str(t["id"]),
                    "name": t.get("filename", ""),
                    "progress": _num(t.get("progress"), float),
                    "state": state,
                    "done": t.get("status") == "downloaded",
                    "size": _num(t.get("bytes")),
                    "seeds": _num(t.get("seeders")),
                    "speed": _num(t.get("speed")),
                    "kind": "torrent",
                    "up_speed": None,  # Real-Debrid does not report these three
                    "peers": None,
                    "ratio": None,
                    "added": t.get("added") or "",
                    "finished": t.get("ended") or "",
                    "hash": t.get("hash") or "",
                    "raw_state": raw,
                })
            if active_only or len(chunk) < 5000:
                break
            chunk = None  # this page goes before the next one is fetched and parsed
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
                out.append({"name": u.get("filename") or link, "url": _direct_url(u), "size": _num(u.get("filesize"))})
            except ApiError as e:
                out.append({"name": f"{u.get('filename') or link}: {e}", "url": None})
        return out

    def add_link(self, url):
        return _direct_url(self._json("POST", "/unrestrict/link", data={"link": url}))


class Torbox(_Client):
    BASE = "https://api.torbox.app/v1/api"
    name = "Torbox"
    short = "TB"
    PLANS = {0: "Free", 1: "Essential", 2: "Pro", 3: "Standard"}
    LIGHT_MAX = 10  # items fetched one by one per light poll; above that the whole list is the cheaper request

    def __init__(self, token, session=None):
        super().__init__(token, session)
        self._gone = False  # a light poll met an id the service no longer knows: deleted elsewhere, stale() says so

    def _call(self, method, path, **kw):
        """Full Torbox envelope after success checks."""
        body = _body(self._req(method, path, **kw), self.name)
        if not isinstance(body, dict):
            raise ApiError(f"Unexpected response from {self.name}")
        if not body.get("success"):
            if _is_auth_error(body):
                raise ApiError(f"{INVALID_KEY} ({_auth_code(body)})")
            raise ApiError(body.get("detail") or body.get("error") or "Torbox error")
        return body

    def _json(self, method, path, **kw):
        return self._call(method, path, **kw).get("data")

    def account(self):
        u = self._json("GET", "/user/me") or {}
        return {
            "plan": self.PLANS.get(u.get("plan"), str(u.get("plan"))),
            "expires": (u.get("premium_expires_at") or "")[:10],
            "extra": f"{u.get('total_downloaded', 0)} downloads",
        }

    def torrents(self, active_only=False, ids=()):
        """The whole list (every file of every item travels with it: megabytes for a big library), or with
        active_only the items in ids ((id, kind) pairs) one by one, the API having no active-only query. An id the
        service no longer knows is left out, so the caller's merge notices and asks for the whole list. More than
        LIGHT_MAX ids: None, the whole list is the cheaper request then (300 requests a minute is the limit)."""
        out = []
        if active_only:
            if len(ids) > self.LIGHT_MAX:
                return None
            for tid, kind in ids:
                try:
                    t = self._json("GET", "/webdl/mylist" if kind == "webdl" else "/torrents/mylist",
                                   params={"id": int(tid), "bypass_cache": "true"})
                except NotFound:
                    self._gone = True
                    continue
                if isinstance(t, dict) and t.get("id") is not None:
                    out.append(self._item(t, kind))
            return out
        for kind, path in (("torrent", "/torrents/mylist"), ("webdl", "/webdl/mylist")):
            for t in self._json("GET", path, params={"bypass_cache": "true", "limit": 5000}) or []:
                if t.get("id") is not None:
                    out.append(self._item(t, kind))
        return out

    def stale(self, cached):
        """An id the light poll could not find (deleted elsewhere), else the newest item of each list (Torbox lists
        newest first) against the cached ids: an item added elsewhere shows up on the next poll; one deleted elsewhere
        waits for the next full list unless the light poll asked for it."""
        if self._gone:
            self._gone = False
            return True
        ids = {t["id"] for t in cached}
        for path in ("/torrents/mylist", "/webdl/mylist"):
            newest = self._json("GET", path, params={"bypass_cache": "true", "limit": 1})
            if isinstance(newest, list) and newest and str(newest[0].get("id")) not in ids:
                return True
        return False

    @staticmethod
    def _item(t, kind):
        raw = t.get("download_state") or ""
        label = state_label(raw, TB_STATES)
        done = bool(t.get("download_finished")) and bool(t.get("download_present"))
        moving = t.get("active") is not False  # Torbox keeps the last speeds on stopped / finished items
        return {
            "id": str(t["id"]),
            "name": t.get("name", ""),
            "progress": min(100.0, _num(t.get("progress"), float) * 100),  # API gives 0..1
            "state": label if kind == "torrent" else f"{label} (web)",
            "done": done,
            "size": _num(t.get("size")),
            "seeds": _num(t.get("seeds")),
            "speed": _num(t.get("download_speed")) if moving else 0,
            "kind": kind,
            "up_speed": _num(t.get("upload_speed")) if moving else 0,
            "peers": _num(t.get("peers")),
            "ratio": _num(t.get("ratio"), float),
            "added": t.get("created_at") or "",
            "finished": (t.get("updated_at") or "") if done else "",  # no finish timestamp in the API
            "hash": t.get("hash") or "",
            "raw_state": raw,
        }

    def add_magnet(self, magnet):
        self._json("POST", "/torrents/createtorrent", files={"magnet": (None, magnet)})

    def add_torrent_file(self, path):
        with open(path, "rb") as f:
            self._json("POST", "/torrents/createtorrent", files={"file": (os.path.basename(path), f, "application/x-bittorrent")})

    def control(self, tid, op, kind="torrent"):
        """op: resume, stop_seeding, reannounce or delete (Torbox has no pause). Returns Torbox's reply text."""
        if kind == "webdl":
            body = self._call("POST", "/webdl/controlwebdownload", json={"webdl_id": int(tid), "operation": op})
        else:
            body = self._call("POST", "/torrents/controltorrent", json={"torrent_id": int(tid), "operation": op})
        return str(body.get("detail") or "ok")

    def delete(self, tid, kind="torrent"):
        self.control(tid, "delete", kind)

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
                out.append({"name": "All files (zip)", "url": zip_url, "size": _num(item.get("size"))})
            except ApiError as e:
                out.append({"name": f"Zip failed: {e}", "url": None})
        for f in files:
            if f.get("id") is None:
                continue
            try:
                url = self._json("GET", dl_path, params={"token": self.token, id_key: int(tid), "file_id": f["id"]})
            except ApiError as e:
                out.append({"name": f"Failed: {e}", "url": None})
                break
            out.append({"name": f.get("short_name") or f.get("name") or str(f["id"]), "url": url,
                        "size": _num(f.get("size"))})
        return out

    def add_link(self, url):
        self._json("POST", "/webdl/createwebdownload", data={"link": url})
        return None

    def check_cached(self, hashes):
        """Subset of the given info hashes (lowercase hex) that Torbox already holds. No request when empty."""
        hashes = [h.lower() for h in hashes if h]
        if not hashes:
            return set()
        data = self._json("GET", "/torrents/checkcached",
                          params={"hash": ",".join(hashes), "format": "object", "list_files": "false"})
        if isinstance(data, dict):
            found = {k.lower() for k in data}
        elif isinstance(data, list):
            found = {str(i.get("hash", "")).lower() for i in data if isinstance(i, dict)}
        else:
            found = set()
        return {h for h in hashes if h in found}


def _flatten_files(nodes, prefix=""):
    """AllDebrid folder tree ({n, e} folders, {n, s, l} files) -> (path, size, link)."""
    for node in nodes or []:
        name = f"{prefix}{node.get('n', '')}"
        if "e" in node:
            yield from _flatten_files(node["e"], name + "/")
        elif node.get("l"):
            yield name, _num(node.get("s")), node["l"]


class AllDebrid(_Client):
    BASE = "https://api.alldebrid.com"
    name = "AllDebrid"
    short = "AD"

    def _call(self, method, path, **kw):
        """Envelope: {"status": "success", "data": ...} or {"status": "error", "error": {"code", "message"}}."""
        body = _body(self._req(method, path, **kw), self.name)
        if not isinstance(body, dict):
            raise ApiError(f"Unexpected response from {self.name}")
        if body.get("status") != "success":
            if _is_auth_error(body):
                raise ApiError(f"{INVALID_KEY} ({_auth_code(body)})")
            err = body.get("error") or {}
            raise ApiError((err.get("message") or err.get("code") or "AllDebrid error") if isinstance(err, dict) else str(err))
        return body.get("data") or {}

    @staticmethod
    def _item_error(item):
        err = item.get("error") if isinstance(item, dict) else None
        if err:
            raise ApiError(err.get("message") or err.get("code") or "AllDebrid error")

    def account(self):
        u = self._call("GET", "/v4/user").get("user") or {}
        plan = "Trial" if u.get("isTrial") else "Premium" if u.get("isPremium") else "Free"
        return {"plan": plan, "expires": _iso_from_ts(u.get("premiumUntil"))[:10],
                "extra": f"{u.get('fidelityPoints', 0)} points"}

    def torrents(self, active_only=False, ids=()):
        if active_only:  # v4.1 ignores its status filter (observed 2026-10): no cheaper query than the whole list
            return None
        out = []
        for m in self._call("POST", "/v4.1/magnet/status").get("magnets") or []:
            if m.get("id") is None:
                continue
            code = _num(m.get("statusCode"))
            size = _num(m.get("size"))
            done = code == 4
            progress = 100.0 if done else (100.0 * _num(m.get("downloaded")) / size if size else 0.0)
            status = m.get("status") or ""
            out.append({
                "id": str(m["id"]),
                "name": m.get("filename", ""),
                "progress": min(100.0, progress),
                "state": f"{ERROR}: {status}" if code >= 5 else state_label(status, AD_STATES),
                "done": done,
                "size": size,
                "seeds": _num(m.get("seeders")),
                "speed": _num(m.get("downloadSpeed")),
                "kind": "torrent",
                "up_speed": _num(m.get("uploadSpeed")),
                "peers": None,
                "ratio": None,
                "added": _iso_from_ts(m.get("uploadDate")),
                "finished": _iso_from_ts(m.get("completionDate")),
                "hash": "",  # not part of the v4.1 status object
                "raw_state": status,
            })
        return out

    def add_magnet(self, magnet):
        items = self._call("POST", "/v4/magnet/upload", data={"magnets[]": [magnet]}).get("magnets") or []
        for item in items:
            self._item_error(item)

    def add_torrent_file(self, path):
        with open(path, "rb") as f:
            data = self._call("POST", "/v4/magnet/upload/file",
                              files={"files[]": (os.path.basename(path), f, "application/x-bittorrent")})
        for item in data.get("files") or []:
            self._item_error(item)

    def delete(self, tid, kind="torrent"):
        self._call("POST", "/v4/magnet/delete", data={"id": int(tid)})

    def links(self, tid, kind="torrent"):
        magnets = self._call("POST", "/v4/magnet/files", data={"id[]": [int(tid)]}).get("magnets") or []
        item = magnets[0] if magnets else {}
        if not isinstance(item, dict) or item.get("error"):
            return []
        out = []
        for name, size, link in _flatten_files(item.get("files")):
            try:
                u = self._call("POST", "/v4/link/unlock", data={"link": link})
            except ApiError as e:
                out.append({"name": f"Failed: {e}", "url": None})
                break
            out.append({"name": name, "url": u.get("link"), "size": _num(u.get("filesize")) or size})
        return out

    def add_link(self, url):
        direct = self._call("POST", "/v4/link/unlock", data={"link": url}).get("link")
        if not direct:
            raise ApiError("No direct link returned")
        return direct
