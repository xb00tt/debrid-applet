# Debrid Applet Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tray indicator for Linux Mint that shows Torbox and Real-Debrid account status and torrents, adds magnets/.torrent files/links, hands download links to the browser, deletes items, and notifies on completion.

**Architecture:** `debrid_api.py` holds two HTTP clients with one shared interface returning plain dicts; it has no GTK dependency and is tested offline with a fake session. `debrid_applet.py` builds an AppIndicator icon plus a GTK3 popup with one tab per service, polls both clients every 30 s on a worker thread, and pushes results to the main loop with `GLib.idle_add`.

**Tech Stack:** Python 3.12, requests, PyGObject (Gtk 3.0, Gdk, GLib, Pango, Notify 0.7, AyatanaAppIndicator3 0.1), xdg-open. All preinstalled on the target machine. No pytest: tests are plain `assert` scripts.

**Spec:** `docs/superpowers/specs/2026-10-10-debrid-applet-design.md`

## Global Constraints

- Python 3.12, no new dependencies beyond what is installed (`python3-gi`, `gir1.2-ayatanaappindicator3-0.1`, `gir1.2-notify-0.7`, `python3-requests`).
- Config at `~/.config/debrid-applet/config.json`, file mode `0600`, dir mode `0700`, keys `torbox_key`, `realdebrid_key`.
- HTTP timeout 15 s, header `User-Agent: debrid-applet`, `Authorization: Bearer <token>`.
- Real-Debrid base `https://api.real-debrid.com/rest/1.0`; Torbox base `https://api.torbox.app/v1/api`.
- Torbox token appears only in `requestdl` query params (Torbox requirement); never open or copy a URL containing the token.
- HTTP 401/403 and Torbox errors `NO_AUTH`, `BAD_TOKEN`, `AUTH_ERROR` map to exactly `ApiError("Invalid API key")`.
- Poll interval 30 s; one refresh in flight at a time.
- Download action is `xdg-open <direct url>`; no built-in downloader.
- Out of scope: pause/resume, partial file selection, usenet, streaming, keyring.
- Commits: normal prose messages, end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

1. Torbox `progress` is a 0–1 fraction; a value already in percent must not render as 10000 %. Clamp to 0–100 (Task 2 test `test_tb_progress_clamped`).
2. Asking for links of an unfinished Real-Debrid torrent returns an empty `links` list; the UI must say "No links yet" instead of crashing (Task 1 test `test_rd_links_empty`, Task 4 code path).
3. Torbox `mylist?id=` for a just-deleted item returns `data: null`; `links()` must return `[]` (Task 2 test `test_tb_links_missing_item`).
4. Network down must surface as `ApiError("Network error: …")`, not a traceback in the worker thread (Task 1 test `test_network_error`).
5. Real-Debrid `selectFiles` answers 204 with an empty body; JSON decoding must not be attempted on it (Task 1 test `test_rd_add_magnet_selects_all` asserts no exception on a 204).

---

### Task 1: API module with Real-Debrid client

**Files:**
- Create: `debrid_api.py`
- Create: `test_debrid_api.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class ApiError(Exception)`
  - `def human_size(n: int|float) -> str` e.g. `"1.5 GB"`
  - `class RealDebrid(token: str, session=None)` with `name = "Real-Debrid"` and methods
    - `account() -> {"plan": str, "expires": str, "extra": str}`
    - `torrents() -> list[dict]` each `{"id": str, "name": str, "progress": float, "state": str, "done": bool, "size": int, "seeds": int, "speed": int, "kind": "torrent"}`
    - `add_magnet(magnet: str) -> None`
    - `add_torrent_file(path: str) -> None`
    - `delete(tid, kind="torrent") -> None`
    - `links(tid, kind="torrent") -> list[{"name": str, "url": str}]`
    - `add_link(url: str) -> str` (direct download URL)
  - Test helpers in `test_debrid_api.py`: `FakeResp(status=200, body=None)`, `FakeSession(*responses)` with `.calls` list of `(method, url, kwargs)`.

- [ ] **Step 1: Write the failing tests**

```python
# test_debrid_api.py
"""Offline tests for debrid_api. Run: python3 test_debrid_api.py"""
import json
import requests

from debrid_api import ApiError, RealDebrid, human_size

RD = "https://api.real-debrid.com/rest/1.0"


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body
        self.text = "" if body is None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.headers = {}
        self.queue = list(responses)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        resp = self.queue.pop(0) if self.queue else FakeResp(200, {})
        if isinstance(resp, Exception):
            raise resp
        return resp


def test_human_size():
    assert human_size(0) == "0 B"
    assert human_size(1536) == "1.5 KB"
    assert human_size(3 * 1024 ** 3) == "3.0 GB"


def test_rd_auth_header():
    s = FakeSession()
    RealDebrid("tok", s)
    assert s.headers["Authorization"] == "Bearer tok"
    assert s.headers["User-Agent"] == "debrid-applet"


def test_rd_account():
    s = FakeSession(FakeResp(200, {"type": "premium", "expiration": "2027-01-03T10:00:00.000Z", "points": 1234}))
    assert RealDebrid("t", s).account() == {"plan": "premium", "expires": "2027-01-03", "extra": "1234 points"}
    assert s.calls[0][:2] == ("GET", RD + "/user")
    assert s.calls[0][2]["timeout"] == 15


def test_rd_torrents_normalize_and_autoselect():
    s = FakeSession(FakeResp(200, [
        {"id": "A", "filename": "a.mkv", "progress": 42.5, "status": "downloading", "bytes": 1000, "seeders": 3, "speed": 50},
        {"id": "B", "filename": "b.iso", "progress": 0, "status": "waiting_files_selection", "bytes": 10},
        {"id": "C", "filename": "c.zip", "progress": 100, "status": "downloaded", "bytes": 5},
    ]), FakeResp(204))
    out = RealDebrid("t", s).torrents()
    assert out[0] == {"id": "A", "name": "a.mkv", "progress": 42.5, "state": "downloading", "done": False,
                      "size": 1000, "seeds": 3, "speed": 50, "kind": "torrent"}
    assert out[1]["seeds"] == 0 and out[1]["done"] is False
    assert out[2]["done"] is True
    assert s.calls[0][2]["params"] == {"limit": 100}
    assert s.calls[1][:2] == ("POST", RD + "/torrents/selectFiles/B")
    assert s.calls[1][2]["data"] == {"files": "all"}


def test_rd_add_magnet_selects_all():
    s = FakeSession(FakeResp(201, {"id": "X", "uri": "..."}), FakeResp(204))
    RealDebrid("t", s).add_magnet("magnet:?xt=urn:btih:abc")
    assert s.calls[0][:2] == ("POST", RD + "/torrents/addMagnet")
    assert s.calls[0][2]["data"] == {"magnet": "magnet:?xt=urn:btih:abc"}
    assert s.calls[1][:2] == ("POST", RD + "/torrents/selectFiles/X")


def test_rd_add_torrent_file(tmp_path="/tmp/debrid-test.torrent"):
    with open(tmp_path, "wb") as f:
        f.write(b"d8:announce0:e")
    s = FakeSession(FakeResp(201, {"id": "Y"}), FakeResp(204))
    RealDebrid("t", s).add_torrent_file(tmp_path)
    assert s.calls[0][:2] == ("PUT", RD + "/torrents/addTorrent")
    assert s.calls[0][2]["data"] == b"d8:announce0:e"
    assert s.calls[1][:2] == ("POST", RD + "/torrents/selectFiles/Y")


def test_rd_delete():
    s = FakeSession(FakeResp(204))
    RealDebrid("t", s).delete("A")
    assert s.calls[0][:2] == ("DELETE", RD + "/torrents/delete/A")


def test_rd_links_unrestricts_each():
    s = FakeSession(
        FakeResp(200, {"id": "A", "links": ["https://real-debrid.com/d/1", "https://real-debrid.com/d/2"]}),
        FakeResp(200, {"filename": "one.mkv", "download": "https://cdn/one"}),
        FakeResp(200, {"filename": "two.mkv", "download": "https://cdn/two"}),
    )
    out = RealDebrid("t", s).links("A")
    assert out == [{"name": "one.mkv", "url": "https://cdn/one"}, {"name": "two.mkv", "url": "https://cdn/two"}]
    assert s.calls[0][:2] == ("GET", RD + "/torrents/info/A")
    assert s.calls[1][:2] == ("POST", RD + "/unrestrict/link")
    assert s.calls[1][2]["data"] == {"link": "https://real-debrid.com/d/1"}


def test_rd_links_empty():
    s = FakeSession(FakeResp(200, {"id": "A", "links": []}))
    assert RealDebrid("t", s).links("A") == []


def test_rd_add_link():
    s = FakeSession(FakeResp(200, {"filename": "f.bin", "download": "https://cdn/f"}))
    assert RealDebrid("t", s).add_link("https://hoster/x") == "https://cdn/f"
    assert s.calls[0][2]["data"] == {"link": "https://hoster/x"}


def test_invalid_key():
    for code in (401, 403):
        try:
            RealDebrid("t", FakeSession(FakeResp(code, {"error": "bad"}))).account()
            assert False, "expected ApiError"
        except ApiError as e:
            assert str(e) == "Invalid API key"


def test_rate_limited():
    try:
        RealDebrid("t", FakeSession(FakeResp(429, {}))).account()
        assert False
    except ApiError as e:
        assert str(e) == "Rate limited, retrying next tick"


def test_http_error_message():
    try:
        RealDebrid("t", FakeSession(FakeResp(503, {"error": "down"}))).account()
        assert False
    except ApiError as e:
        assert str(e).startswith("HTTP 503")


def test_network_error():
    try:
        RealDebrid("t", FakeSession(requests.ConnectionError("boom"))).account()
        assert False
    except ApiError as e:
        assert str(e).startswith("Network error:")


if __name__ == "__main__":
    import sys
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} passed")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 test_debrid_api.py`
Expected: `ModuleNotFoundError: No module named 'debrid_api'`

- [ ] **Step 3: Write `debrid_api.py` with shared base and Real-Debrid client**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 test_debrid_api.py`
Expected: every line `ok test_…`, last line `14 passed`.

- [ ] **Step 5: Commit**

```bash
git add debrid_api.py test_debrid_api.py
git commit -m "Add Real-Debrid client with offline tests

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Torbox client

**Files:**
- Modify: `debrid_api.py` (append `Torbox` class)
- Modify: `test_debrid_api.py` (append Torbox tests, extend import)

**Interfaces:**
- Consumes: `_Client`, `ApiError`, `human_size` from Task 1.
- Produces: `class Torbox(token, session=None)` with `name = "Torbox"` and the same seven methods as `RealDebrid`. `torrents()` items carry `"kind": "torrent"` or `"kind": "webdl"`; `delete(tid, kind)` and `links(tid, kind)` route on `kind`. `add_link()` returns `None`.

- [ ] **Step 1: Write the failing tests**

Change the import line at the top of `test_debrid_api.py` to:

```python
from debrid_api import ApiError, RealDebrid, Torbox, human_size
```

Add after `RD = ...`:

```python
TB = "https://api.torbox.app/v1/api"


def ok(data):
    return FakeResp(200, {"success": True, "error": None, "detail": "ok", "data": data})
```

Append before `if __name__ == "__main__":`:

```python
def test_tb_account():
    s = FakeSession(ok({"plan": 2, "premium_expires_at": "2027-05-01T00:00:00Z", "total_downloaded": 3 * 1024 ** 3}))
    assert Torbox("t", s).account() == {"plan": "Pro", "expires": "2027-05-01", "extra": "3.0 GB downloaded"}
    assert s.calls[0][:2] == ("GET", TB + "/user/me")


def test_tb_torrents_merges_webdl():
    s = FakeSession(
        ok([{"id": 7, "name": "seven.mkv", "progress": 0.25, "download_state": "downloading",
             "download_finished": False, "download_present": False, "size": 2000, "seeds": 9, "download_speed": 300},
            {"id": 8, "name": "eight.iso", "progress": 1, "download_state": "uploading",
             "download_finished": True, "download_present": True, "size": 1}]),
        ok([{"id": 3, "name": "web.bin", "progress": 1, "download_state": "completed",
             "download_finished": True, "download_present": True, "size": 5}]),
    )
    out = Torbox("t", s).torrents()
    assert out[0] == {"id": "7", "name": "seven.mkv", "progress": 25.0, "state": "downloading", "done": False,
                      "size": 2000, "seeds": 9, "speed": 300, "kind": "torrent"}
    assert out[1]["done"] is True and out[1]["progress"] == 100.0
    assert out[2] == {"id": "3", "name": "web.bin", "progress": 100.0, "state": "web: completed", "done": True,
                      "size": 5, "seeds": 0, "speed": 0, "kind": "webdl"}
    assert s.calls[0][:2] == ("GET", TB + "/torrents/mylist")
    assert s.calls[0][2]["params"] == {"bypass_cache": "true"}
    assert s.calls[1][:2] == ("GET", TB + "/webdl/mylist")


def test_tb_done_requires_present():
    s = FakeSession(ok([{"id": 1, "name": "x", "progress": 1, "download_state": "completed",
                         "download_finished": True, "download_present": False}]), ok([]))
    assert Torbox("t", s).torrents()[0]["done"] is False


def test_tb_progress_clamped():
    s = FakeSession(ok([{"id": 1, "name": "x", "progress": 55.0, "download_state": "downloading"}]), ok([]))
    assert Torbox("t", s).torrents()[0]["progress"] == 100.0


def test_tb_add_magnet_multipart():
    s = FakeSession(ok({"torrent_id": 1}))
    Torbox("t", s).add_magnet("magnet:?xt=urn:btih:abc")
    assert s.calls[0][:2] == ("POST", TB + "/torrents/createtorrent")
    assert s.calls[0][2]["files"] == {"magnet": (None, "magnet:?xt=urn:btih:abc")}


def test_tb_add_torrent_file(tmp_path="/tmp/debrid-test.torrent"):
    with open(tmp_path, "wb") as f:
        f.write(b"d8:announce0:e")
    s = FakeSession(ok({"torrent_id": 1}))
    Torbox("t", s).add_torrent_file(tmp_path)
    name, fileobj = s.calls[0][2]["files"]["file"]
    assert name == "debrid-test.torrent"
    assert fileobj.closed


def test_tb_delete_routes_on_kind():
    s = FakeSession(ok(None), ok(None))
    tb = Torbox("t", s)
    tb.delete("7", "torrent")
    tb.delete("3", "webdl")
    assert s.calls[0][:2] == ("POST", TB + "/torrents/controltorrent")
    assert s.calls[0][2]["json"] == {"torrent_id": 7, "operation": "delete"}
    assert s.calls[1][:2] == ("POST", TB + "/webdl/controlwebdownload")
    assert s.calls[1][2]["json"] == {"webdl_id": 3, "operation": "delete"}


def test_tb_links_requestdl_per_file():
    s = FakeSession(
        ok({"id": 7, "files": [{"id": 0, "name": "dir/a.mkv", "short_name": "a.mkv"}, {"id": 1, "name": "b.srt"}]}),
        ok("https://cdn/a"),
        ok("https://cdn/b"),
    )
    out = Torbox("tok", s).links("7", "torrent")
    assert out == [{"name": "a.mkv", "url": "https://cdn/a"}, {"name": "b.srt", "url": "https://cdn/b"}]
    assert s.calls[0][:2] == ("GET", TB + "/torrents/mylist")
    assert s.calls[0][2]["params"] == {"id": 7, "bypass_cache": "true"}
    assert s.calls[1][:2] == ("GET", TB + "/torrents/requestdl")
    assert s.calls[1][2]["params"] == {"token": "tok", "torrent_id": 7, "file_id": 0}


def test_tb_links_webdl():
    s = FakeSession(ok({"id": 3, "files": [{"id": 0, "name": "w.bin"}]}), ok("https://cdn/w"))
    assert Torbox("tok", s).links("3", "webdl") == [{"name": "w.bin", "url": "https://cdn/w"}]
    assert s.calls[0][1] == TB + "/webdl/mylist"
    assert s.calls[1][1] == TB + "/webdl/requestdl"
    assert s.calls[1][2]["params"] == {"token": "tok", "web_id": 3, "file_id": 0}


def test_tb_links_missing_item():
    s = FakeSession(ok(None))
    assert Torbox("t", s).links("99", "torrent") == []


def test_tb_add_link():
    s = FakeSession(ok({"webdownload_id": 3}))
    assert Torbox("t", s).add_link("https://hoster/x") is None
    assert s.calls[0][:2] == ("POST", TB + "/webdl/createwebdownload")
    assert s.calls[0][2]["data"] == {"link": "https://hoster/x"}


def test_tb_success_false_raises_detail():
    s = FakeSession(FakeResp(200, {"success": False, "error": "ACTIVE_LIMIT", "detail": "Too many active downloads.", "data": None}))
    try:
        Torbox("t", s).torrents()
        assert False
    except ApiError as e:
        assert str(e) == "Too many active downloads."


def test_tb_bad_token_is_invalid_key():
    s = FakeSession(FakeResp(200, {"success": False, "error": "BAD_TOKEN", "detail": "The provided token is invalid.", "data": None}))
    try:
        Torbox("t", s).account()
        assert False
    except ApiError as e:
        assert str(e) == "Invalid API key"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 test_debrid_api.py`
Expected: `ImportError: cannot import name 'Torbox'`

- [ ] **Step 3: Append `Torbox` to `debrid_api.py`**

```python
class Torbox(_Client):
    BASE = "https://api.torbox.app/v1/api"
    name = "Torbox"
    PLANS = {0: "Free", 1: "Essential", 2: "Pro", 3: "Standard"}
    AUTH_ERRORS = ("NO_AUTH", "BAD_TOKEN", "AUTH_ERROR")

    def _json(self, method, path, **kw):
        body = self._req(method, path, **kw).json()
        if not body.get("success"):
            if body.get("error") in self.AUTH_ERRORS:
                raise ApiError("Invalid API key")
            raise ApiError(body.get("detail") or body.get("error") or "Torbox error")
        return body.get("data")

    def account(self):
        u = self._json("GET", "/user/me") or {}
        return {
            "plan": self.PLANS.get(u.get("plan"), str(u.get("plan"))),
            "expires": (u.get("premium_expires_at") or "")[:10],
            "extra": f"{human_size(u.get('total_downloaded', 0))} downloaded",
        }

    def torrents(self):
        out = []
        for kind, path in (("torrent", "/torrents/mylist"), ("webdl", "/webdl/mylist")):
            for t in self._json("GET", path, params={"bypass_cache": "true"}) or []:
                state = t.get("download_state") or ""
                out.append({
                    "id": str(t["id"]),
                    "name": t.get("name", ""),
                    "progress": min(100.0, float(t.get("progress") or 0) * 100),  # API gives 0..1
                    "state": state if kind == "torrent" else f"web: {state}",
                    "done": bool(t.get("download_finished")) and bool(t.get("download_present")),
                    "size": int(t.get("size") or 0),
                    "seeds": int(t.get("seeds") or 0),
                    "speed": int(t.get("download_speed") or 0),
                    "kind": kind,
                })
        return out

    def add_magnet(self, magnet):
        self._json("POST", "/torrents/createtorrent", files={"magnet": (None, magnet)})

    def add_torrent_file(self, path):
        with open(path, "rb") as f:
            self._json("POST", "/torrents/createtorrent", files={"file": (os.path.basename(path), f)})

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
        out = []
        for f in item.get("files") or []:
            url = self._json("GET", dl_path, params={"token": self.token, id_key: int(tid), "file_id": f["id"]})
            out.append({"name": f.get("short_name") or f.get("name") or str(f["id"]), "url": url})
        return out

    def add_link(self, url):
        self._json("POST", "/webdl/createwebdownload", data={"link": url})
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 test_debrid_api.py`
Expected: last line `27 passed`.

- [ ] **Step 5: Commit**

```bash
git add debrid_api.py test_debrid_api.py
git commit -m "Add Torbox client with web download routing

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Applet shell: config, indicator, window, tabs, polling, notifications

**Files:**
- Create: `debrid_applet.py`
- Modify: `test_debrid_api.py` (append config tests)

**Interfaces:**
- Consumes: `ApiError`, `RealDebrid`, `Torbox`, `human_size` from `debrid_api`.
- Produces (used by Task 4 inside the same file):
  - `load_config() -> dict`, `save_config(cfg: dict) -> None`
  - `run_async(fn, on_done)`: runs `fn()` on a thread, calls `on_done(result, error_str_or_None)` on the GTK main loop.
  - `copy_text(text: str)`
  - `class ServiceTab(Gtk.Box)` with attributes `name`, `client` (may be `None`), `app`, `entry`, `store` (`Gtk.ListStore(str id, str name, int progress, str state, str size, str kind)`), `view`, `status` (`Gtk.Label`), `active: int`; methods `render(account, torrents, error)`, `_selected() -> dict|None` with keys `id`, `name`, `kind`, `_run(fn, cb)`, and button handlers `on_add`, `on_torrent_file`, `on_links`, `on_copy_first`, `on_delete` (stubs here, filled in Task 4).
  - `class App` with `refresh()`, `toggle_window()`, `open_settings()`, `open_settings_once()`, `build_tabs()`, attribute `tabs: list[ServiceTab]`.

- [ ] **Step 1: Write the failing config tests**

Append to `test_debrid_api.py` before `if __name__ == "__main__":`:

```python
def test_config_roundtrip_and_mode(tmp_dir="/tmp/debrid-test-config"):
    import os
    import shutil
    import stat
    import debrid_applet
    shutil.rmtree(tmp_dir, ignore_errors=True)
    debrid_applet.CONFIG_DIR = tmp_dir
    debrid_applet.CONFIG = os.path.join(tmp_dir, "config.json")
    assert debrid_applet.load_config() == {"torbox_key": "", "realdebrid_key": ""}
    debrid_applet.save_config({"torbox_key": "a", "realdebrid_key": "b"})
    assert stat.S_IMODE(os.stat(debrid_applet.CONFIG).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_dir).st_mode) == 0o700
    assert debrid_applet.load_config() == {"torbox_key": "a", "realdebrid_key": "b"}
    with open(debrid_applet.CONFIG, "w") as f:
        f.write("{not json")
    assert debrid_applet.load_config() == {"torbox_key": "", "realdebrid_key": ""}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 test_debrid_api.py`
Expected: `ModuleNotFoundError: No module named 'debrid_applet'`

- [ ] **Step 3: Write `debrid_applet.py`**

```python
#!/usr/bin/env python3
"""Tray control panel for Torbox and Real-Debrid."""
import json
import os
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("Notify", "0.7")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import Gdk, GLib, Gtk, Notify, Pango  # noqa: E402
from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from debrid_api import ApiError, RealDebrid, Torbox, human_size  # noqa: E402

CONFIG_DIR = os.path.expanduser("~/.config/debrid-applet")
CONFIG = os.path.join(CONFIG_DIR, "config.json")
EMPTY_CONFIG = {"torbox_key": "", "realdebrid_key": ""}
POLL_SECONDS = 30
INVALID_KEY = "Invalid API key"


def load_config():
    try:
        with open(CONFIG) as f:
            return {**EMPTY_CONFIG, **json.load(f)}
    except (OSError, ValueError):
        return dict(EMPTY_CONFIG)


def save_config(cfg):
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)


def run_async(fn, on_done):
    """Run fn() on a worker thread; deliver (result, error) to on_done on the main loop."""
    def worker():
        try:
            res, err = fn(), None
        except ApiError as e:
            res, err = None, str(e)
        except Exception as e:  # never let a worker kill the main loop
            res, err = None, f"{type(e).__name__}: {e}"
        GLib.idle_add(on_done, res, err)
    threading.Thread(target=worker, daemon=True).start()


def copy_text(text):
    cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    cb.set_text(text, -1)
    cb.store()


def red(text):
    return f'<span foreground="red">{GLib.markup_escape_text(text)}</span>'


class ServiceTab(Gtk.Box):
    def __init__(self, name, client, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        self.name, self.client, self.app = name, client, app
        self.done_ids = None  # None until first successful poll; then set of finished ids
        self.active = 0
        self._is_busy = False

        self.account_label = Gtk.Label(xalign=0, label="Not configured" if client is None else "Loading…")
        self.pack_start(self.account_label, False, False, 0)

        row = Gtk.Box(spacing=6)
        self.entry = Gtk.Entry(placeholder_text="magnet: or hoster link", hexpand=True)
        self.entry.connect("activate", self.on_add)
        add_btn = Gtk.Button(label="Add")
        add_btn.connect("clicked", self.on_add)
        self.chooser = Gtk.FileChooserButton(title="Add .torrent")
        flt = Gtk.FileFilter()
        flt.set_name("Torrent files")
        flt.add_pattern("*.torrent")
        self.chooser.add_filter(flt)
        self.chooser.connect("file-set", self.on_torrent_file)
        row.pack_start(self.entry, True, True, 0)
        row.pack_start(add_btn, False, False, 0)
        row.pack_start(self.chooser, False, False, 0)
        self.pack_start(row, False, False, 0)

        self.store = Gtk.ListStore(str, str, int, str, str, str)  # id, name, progress, state, size, kind
        self.view = Gtk.TreeView(model=self.store)
        name_col = Gtk.TreeViewColumn("Name", Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END), text=1)
        name_col.set_expand(True)
        self.view.append_column(name_col)
        self.view.append_column(Gtk.TreeViewColumn("Progress", Gtk.CellRendererProgress(), value=2))
        self.view.append_column(Gtk.TreeViewColumn("State", Gtk.CellRendererText(), text=3))
        self.view.append_column(Gtk.TreeViewColumn("Size", Gtk.CellRendererText(), text=4))
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.add(self.view)
        self.pack_start(scroller, True, True, 0)

        actions = Gtk.Box(spacing=6)
        self.links_btn = Gtk.Button(label="Links ▾")
        self.links_btn.connect("clicked", self.on_links)
        self.copy_btn = Gtk.Button(label="Copy first link")
        self.copy_btn.connect("clicked", self.on_copy_first)
        self.del_btn = Gtk.Button(label="Delete")
        self.del_btn.connect("clicked", self.on_delete)
        for b in (self.links_btn, self.copy_btn, self.del_btn):
            actions.pack_start(b, False, False, 0)
        self.pack_start(actions, False, False, 0)

        self.status = Gtk.Label(xalign=0)
        self.pack_start(self.status, False, False, 0)

        self.view.get_selection().connect("changed", lambda _s: self._update_buttons())
        self._update_buttons()

    # ----- state helpers -----
    def _selected(self):
        model, it = self.view.get_selection().get_selected()
        if it is None:
            return None
        return {"id": model[it][0], "name": model[it][1], "kind": model[it][5]}

    def _update_buttons(self):
        ok = self.client is not None and not self._is_busy and self._selected() is not None
        for b in (self.links_btn, self.copy_btn, self.del_btn):
            b.set_sensitive(ok)

    def _busy(self, flag):
        self._is_busy = flag
        self._update_buttons()

    def _run(self, fn, cb):
        """Run a network action; show errors in the status label; then cb(result, error)."""
        self._busy(True)
        self.status.set_text("Working…")

        def done(res, err):
            self._busy(False)
            if err:
                self.status.set_markup(red(err))
                if err == INVALID_KEY:
                    self.app.open_settings_once()
            cb(res, err)
        run_async(fn, done)

    # ----- rendering -----
    def render(self, account, torrents, error):
        if error:
            self.account_label.set_markup(red(error))
            self.status.set_markup(red(error))
            if error == INVALID_KEY:
                self.app.open_settings_once()
            return
        self.account_label.set_text(
            f"{account['plan']} · expires {account['expires'] or '—'} · {account['extra']}")
        selected = self._selected()
        self.store.clear()
        for t in torrents:
            self.store.append([t["id"], t["name"], int(max(0, min(100, t["progress"]))),
                               t["state"], human_size(t["size"]), t["kind"]])
        if selected:
            for row in self.store:
                if row[0] == selected["id"]:
                    self.view.get_selection().select_iter(row.iter)
        self.active = sum(1 for t in torrents if not t["done"])
        self.status.set_text(f"Updated {time.strftime('%H:%M:%S')} · {self.active} active")

        prev = self.done_ids
        self.done_ids = {t["id"] for t in torrents if t["done"]}
        if prev is not None:
            for t in torrents:
                if t["done"] and t["id"] not in prev:
                    Notify.Notification.new(f"{self.name}: finished", t["name"], "folder-download").show()

    # ----- actions (Task 4) -----
    def on_add(self, _w):
        pass

    def on_torrent_file(self, _chooser):
        pass

    def on_links(self, _btn):
        pass

    def on_copy_first(self, _btn):
        pass

    def on_delete(self, _btn):
        pass


class App:
    def __init__(self):
        Notify.init("debrid-applet")
        self.cfg = load_config()
        self.lock = threading.Lock()
        self._settings_shown_for = None
        self.tabs = []

        self.indicator = AppIndicator.Indicator.new(
            "debrid-applet", "folder-download-symbolic", AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        menu = Gtk.Menu()
        for label, cb in (("Open", self.toggle_window), ("Refresh", self.refresh),
                          ("Settings…", self.open_settings), ("Quit", Gtk.main_quit)):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", cb)
            menu.append(item)
        menu.show_all()
        self.indicator.set_menu(menu)
        self.indicator.set_secondary_activate_target(menu.get_children()[0])

        self.window = Gtk.Window(title="Debrid", default_width=640, default_height=480)
        self.window.connect("delete-event", lambda w, _e: w.hide() or True)
        self.notebook = Gtk.Notebook()
        self.window.add(self.notebook)
        self.build_tabs()

        if not (self.cfg["torbox_key"] or self.cfg["realdebrid_key"]):
            self.open_settings()
        self.refresh()
        GLib.timeout_add_seconds(POLL_SECONDS, self.refresh)

    def build_tabs(self):
        for page in list(self.notebook.get_children()):
            self.notebook.remove(page)
        self.tabs = [
            ServiceTab("Torbox", Torbox(self.cfg["torbox_key"]) if self.cfg["torbox_key"] else None, self),
            ServiceTab("Real-Debrid",
                       RealDebrid(self.cfg["realdebrid_key"]) if self.cfg["realdebrid_key"] else None, self),
        ]
        for tab in self.tabs:
            self.notebook.append_page(tab, Gtk.Label(label=tab.name))
        self.notebook.show_all()

    def toggle_window(self, *_):
        if self.window.is_visible():
            self.window.hide()
        else:
            self.window.show_all()
            self.window.present()

    def refresh(self, *_):
        """Poll every configured service once. Returns True so GLib keeps the timer."""
        if not self.lock.acquire(blocking=False):
            return True
        tabs = [t for t in self.tabs if t.client is not None]

        def fetch():
            results = []
            for t in tabs:
                try:
                    results.append((t, t.client.account(), t.client.torrents(), None))
                except ApiError as e:
                    results.append((t, None, None, str(e)))
                except Exception as e:
                    results.append((t, None, None, f"{type(e).__name__}: {e}"))
            return results

        def done(results, _err):
            self.lock.release()
            for t, acc, tors, e in results or []:
                t.render(acc, tors, e)
            active = sum(t.active for t in self.tabs)
            self.indicator.set_label(f"{active}↓" if active else "", "")
        run_async(fetch, done)
        return True

    def open_settings_once(self):
        key = (self.cfg["torbox_key"], self.cfg["realdebrid_key"])
        if self._settings_shown_for != key:
            self._settings_shown_for = key
            self.open_settings()

    def open_settings(self, *_):
        dlg = Gtk.Dialog(title="Debrid settings", transient_for=self.window, modal=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        grid = Gtk.Grid(row_spacing=6, column_spacing=6, margin=12)
        entries = {}
        for i, (key, label) in enumerate((("torbox_key", "Torbox API key"),
                                          ("realdebrid_key", "Real-Debrid API key"))):
            entry = Gtk.Entry(text=self.cfg[key], visibility=False, width_chars=48)
            grid.attach(Gtk.Label(label=label, xalign=0), 0, i, 1, 1)
            grid.attach(entry, 1, i, 1, 1)
            entries[key] = entry
        dlg.get_content_area().add(grid)
        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            self.cfg = {k: e.get_text().strip() for k, e in entries.items()}
            save_config(self.cfg)
            self.build_tabs()
            self.refresh()
        dlg.destroy()


if __name__ == "__main__":
    App()
    Gtk.main()
```

- [ ] **Step 4: Run tests and a smoke launch**

Run: `python3 test_debrid_api.py`
Expected: last line `28 passed`.

Run (smoke, 3 s then kill): `timeout 3 python3 debrid_applet.py; echo "exit=$?"`
Expected: `exit=124` (killed by timeout, no traceback). A tray icon appears briefly and the Settings dialog opens because no keys exist yet.

- [ ] **Step 5: Commit**

```bash
git add debrid_applet.py test_debrid_api.py
git commit -m "Add tray applet shell with polling, settings and notifications

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Tab actions: add, links menu, copy, delete

**Files:**
- Modify: `debrid_applet.py` (replace the five stub methods in `ServiceTab`)

**Interfaces:**
- Consumes: `ServiceTab._run(fn, cb)`, `ServiceTab._selected()`, `App.refresh()`, `copy_text()`, client methods `add_magnet`, `add_link`, `add_torrent_file`, `links(tid, kind)`, `delete(tid, kind)`.
- Produces: working buttons. No new public names.

- [ ] **Step 1: Replace the stubs**

Replace the block from `# ----- actions (Task 4) -----` through the end of `ServiceTab` with:

```python
    # ----- actions -----
    def on_add(self, _w):
        text = self.entry.get_text().strip()
        if not text or self.client is None:
            return
        if text.lower().startswith("magnet:"):
            self._run(lambda: self.client.add_magnet(text), self._after_add)
        else:
            self._run(lambda: self.client.add_link(text), self._after_add)

    def _after_add(self, result, error):
        if error:
            return
        self.entry.set_text("")
        if result:  # Real-Debrid returns the direct URL immediately
            copy_text(result)
            self.status.set_text("Link copied to clipboard")
        self.app.refresh()

    def on_torrent_file(self, chooser):
        path = chooser.get_filename()
        chooser.unselect_all()
        if path and self.client is not None:
            self._run(lambda: self.client.add_torrent_file(path),
                      lambda _r, err: None if err else self.app.refresh())

    def _fetch_links(self, cb):
        sel = self._selected()
        if sel is None:
            return
        self._run(lambda: self.client.links(sel["id"], sel["kind"]), cb)

    def on_links(self, btn):
        def show(links, err):
            if err:
                return
            if not links:
                self.status.set_text("No links yet (not finished?)")
                return
            menu = Gtk.Menu()
            for link in links:
                item = Gtk.MenuItem(label=link["name"][:80])
                item.connect("activate", lambda _i, url=link["url"]: subprocess.Popen(["xdg-open", url]))
                menu.append(item)
            menu.append(Gtk.SeparatorMenuItem())
            copy_all = Gtk.MenuItem(label="Copy all links")
            copy_all.connect("activate", lambda _i: copy_text("\n".join(l["url"] for l in links)))
            menu.append(copy_all)
            menu.show_all()
            menu.popup_at_widget(btn, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)
            self.status.set_text(f"{len(links)} link(s)")
        self._fetch_links(show)

    def on_copy_first(self, _btn):
        def copy(links, err):
            if err:
                return
            if links:
                copy_text(links[0]["url"])
                self.status.set_text("Link copied to clipboard")
            else:
                self.status.set_text("No links yet (not finished?)")
        self._fetch_links(copy)

    def on_delete(self, _btn):
        sel = self._selected()
        if sel is None:
            return
        self._run(lambda: self.client.delete(sel["id"], sel["kind"]),
                  lambda _r, err: None if err else self.app.refresh())
```

- [ ] **Step 2: Syntax and import check**

Run: `python3 -m py_compile debrid_applet.py && python3 test_debrid_api.py | tail -1`
Expected: `28 passed`.

- [ ] **Step 3: Manual check with real keys**

Run: `python3 debrid_applet.py &`
Then, as the user: open Settings from the tray menu, paste both API keys, Save. Confirm:
- both tabs show the account line (plan, expiry) within a few seconds;
- torrent lists populate with progress bars;
- pasting a magnet and pressing Enter adds it and the list refreshes;
- selecting a finished torrent and clicking **Links ▾** shows the file menu; clicking a file opens the browser download;
- **Delete** removes the item from the list on next refresh;
- the status line shows a red message when a bogus key is entered, and the Settings dialog opens once.
Kill with the tray **Quit** item.

- [ ] **Step 4: Commit**

```bash
git add debrid_applet.py
git commit -m "Wire add, links, copy and delete actions in service tabs

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Installer, desktop entry, README

**Files:**
- Create: `install.sh`
- Create: `debrid-applet.desktop`
- Create: `README.md`
- Create: `.gitignore`

**Interfaces:**
- Consumes: `debrid_api.py`, `debrid_applet.py`.
- Produces: `~/.local/share/debrid-applet/`, `~/.local/share/applications/debrid-applet.desktop`, `~/.config/autostart/debrid-applet.desktop`.

- [ ] **Step 1: Write `debrid-applet.desktop`**

```ini
[Desktop Entry]
Type=Application
Name=Debrid Applet
Comment=Torbox and Real-Debrid control panel
Exec=python3 @DIR@/debrid_applet.py
Icon=folder-download
Terminal=false
Categories=Network;
X-GNOME-Autostart-enabled=true
```

- [ ] **Step 2: Write `install.sh`**

```sh
#!/bin/sh
# Install debrid-applet for the current user and enable autostart. Idempotent.
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DIR="$HOME/.local/share/debrid-applet"
APPS="$HOME/.local/share/applications"
AUTO="$HOME/.config/autostart"
mkdir -p "$DIR" "$APPS" "$AUTO"
cp "$SRC/debrid_api.py" "$SRC/debrid_applet.py" "$DIR/"
sed "s|@DIR@|$DIR|" "$SRC/debrid-applet.desktop" > "$APPS/debrid-applet.desktop"
cp "$APPS/debrid-applet.desktop" "$AUTO/debrid-applet.desktop"
echo "Installed to $DIR. Autostarts on next login. Start now:"
echo "  python3 $DIR/debrid_applet.py &"
```

- [ ] **Step 3: Write `README.md` and `.gitignore`**

`README.md`:

```markdown
# Debrid Applet

Tray control panel for Torbox and Real-Debrid on Linux Mint (Cinnamon).
Shows account status and torrents, adds magnets / .torrent files / hoster links,
opens download links in the browser, deletes items, notifies when a download finishes.

## Install

    ./install.sh

Needs `python3-gi`, `gir1.2-ayatanaappindicator3-0.1`, `gir1.2-notify-0.7`, `python3-requests`
(all present on a stock Mint 22 install). Autostarts on login; start it now with the command the installer prints.

## First run

Tray menu → Settings… → paste API keys from https://torbox.app/settings and
https://real-debrid.com/apitoken → Save. Keys live in `~/.config/debrid-applet/config.json` (mode 600).

## Use

- Tray icon click or **Open**: popup with one tab per service.
- Paste a `magnet:` link or hoster URL, press Enter. Real-Debrid hoster links are unrestricted and the direct URL is copied to the clipboard; Torbox queues them as web downloads.
- `.torrent` button uploads a file.
- Select a row: **Links ▾** lists files (click opens in browser, or copy all), **Copy first link**, **Delete**.
- Polls every 30 s. Desktop notification when an item finishes.

## Dev

    python3 test_debrid_api.py
```

`.gitignore`:

```
__pycache__/
```

- [ ] **Step 4: Run the installer and verify**

Run: `chmod +x install.sh && ./install.sh && ls -l ~/.local/share/debrid-applet ~/.config/autostart/debrid-applet.desktop && grep Exec ~/.local/share/applications/debrid-applet.desktop`
Expected: both `.py` files listed, autostart entry exists, `Exec=python3 /home/<user>/.local/share/debrid-applet/debrid_applet.py`.

Run: `timeout 3 python3 ~/.local/share/debrid-applet/debrid_applet.py; echo "exit=$?"`
Expected: `exit=124`, no traceback (installed copy imports `debrid_api` from its own directory).

- [ ] **Step 5: Commit**

```bash
git add install.sh debrid-applet.desktop README.md .gitignore
git commit -m "Add installer, desktop entry and README

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```
