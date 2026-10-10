"""Offline tests for debrid_api. Run: python3 test_debrid_api.py"""
import json
import requests

from debrid_api import ApiError, RealDebrid, Torbox, human_size, magnet_hash

RD = "https://api.real-debrid.com/rest/1.0"
TB = "https://api.torbox.app/v1/api"


def ok(data):
    return FakeResp(200, {"success": True, "error": None, "detail": "ok", "data": data})


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
        {"id": "A", "filename": "a.mkv", "progress": 42.5, "status": "downloading", "bytes": 1000, "seeders": 3, "speed": 50,
         "added": "2026-10-01T10:00:00.000Z", "hash": "abc123"},
        {"id": "B", "filename": "b.iso", "progress": 0, "status": "waiting_files_selection", "bytes": 10},
        {"id": "C", "filename": "c.zip", "progress": 100, "status": "downloaded", "bytes": 5, "ended": "2026-10-02T11:00:00.000Z"},
    ]), FakeResp(204))
    out = RealDebrid("t", s).torrents()
    assert out[0] == {"id": "A", "name": "a.mkv", "progress": 42.5, "state": "downloading", "done": False,
                      "size": 1000, "seeds": 3, "speed": 50, "kind": "torrent", "up_speed": None, "peers": None,
                      "ratio": None, "added": "2026-10-01T10:00:00.000Z", "finished": "", "hash": "abc123"}
    assert out[1]["hash"] == ""
    assert out[2]["finished"] == "2026-10-02T11:00:00.000Z" and out[2]["added"] == ""
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
        FakeResp(200, {"filename": "one.mkv", "download": "https://cdn/one", "filesize": 1000}),
        FakeResp(200, {"filename": "two.mkv", "download": "https://cdn/two"}),
    )
    out = RealDebrid("t", s).links("A")
    assert out == [{"name": "one.mkv", "url": "https://cdn/one", "size": 1000},
                   {"name": "two.mkv", "url": "https://cdn/two", "size": 0}]
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
    cases = [
        RealDebrid("t", FakeSession(FakeResp(401, {"error": "bad_token", "error_code": 8}))),
        Torbox("t", FakeSession(FakeResp(403, {"success": False, "error": "BAD_TOKEN", "detail": "bad", "data": None}))),
        Torbox("t", FakeSession(FakeResp(403, None))),
    ]
    for c in cases:
        try:
            c.account()
            assert False, "expected ApiError"
        except ApiError as e:
            assert str(e) == "Invalid API key"


def test_403_with_service_message_is_not_invalid_key():
    try:
        RealDebrid("t", FakeSession(FakeResp(403, {"error": "permission_denied", "error_code": 9}))).account()
        assert False
    except ApiError as e:
        assert str(e) == "permission_denied"


def test_rd_select_failure_keeps_list():
    s = FakeSession(FakeResp(200, [
        {"id": "B", "filename": "b.iso", "progress": 0, "status": "waiting_files_selection", "bytes": 10},
        {"id": "C", "filename": "c.zip", "progress": 100, "status": "downloaded", "bytes": 5},
    ]), FakeResp(403, {"error": "infringing_file", "error_code": 35}))
    out = RealDebrid("t", s).torrents()
    assert [t["id"] for t in out] == ["B", "C"]
    assert out[0]["state"] == "select failed: infringing_file"


def test_links_partial_on_rate_limit():
    s = FakeSession(
        FakeResp(200, {"id": "A", "links": ["https://rd/1", "https://rd/2", "https://rd/3"]}),
        FakeResp(200, {"filename": "one.mkv", "download": "https://cdn/one"}),
        FakeResp(429, {}),
    )
    out = RealDebrid("t", s).links("A")
    assert out[0] == {"name": "one.mkv", "url": "https://cdn/one", "size": 0}
    assert out[1]["url"] is None and "Rate limited" in out[1]["name"]
    assert len(out) == 2 and len(s.calls) == 3


def test_rate_limited():
    try:
        RealDebrid("t", FakeSession(FakeResp(429, {}))).account()
        assert False
    except ApiError as e:
        assert str(e) == "Rate limited, retrying next tick"


def test_http_error_message():
    try:
        RealDebrid("t", FakeSession(FakeResp(503, {"status": "down"}))).account()
        assert False
    except ApiError as e:
        assert str(e).startswith("HTTP 503")


def test_http_error_uses_api_detail():
    tb = FakeSession(FakeResp(400, {"success": False, "error": "BOZO_TORRENT", "detail": "Torrent file is not valid.", "data": None}))
    try:
        Torbox("t", tb).add_magnet("magnet:?x")
        assert False
    except ApiError as e:
        assert str(e) == "Torrent file is not valid."
    rd = FakeSession(FakeResp(400, {"error": "bad_request", "error_code": 1}))
    try:
        RealDebrid("t", rd).account()
        assert False
    except ApiError as e:
        assert str(e) == "bad_request"


def test_network_error():
    try:
        RealDebrid("t", FakeSession(requests.ConnectionError("boom"))).account()
        assert False
    except ApiError as e:
        assert str(e).startswith("Network error:")


def test_tb_account():
    s = FakeSession(ok({"plan": 2, "premium_expires_at": "2027-05-01T00:00:00Z", "total_downloaded": 191}))
    assert Torbox("t", s).account() == {"plan": "Pro", "expires": "2027-05-01", "extra": "191 downloads"}
    assert s.calls[0][:2] == ("GET", TB + "/user/me")


def test_tb_torrents_merges_webdl():
    s = FakeSession(
        ok([{"id": 7, "name": "seven.mkv", "progress": 0.25, "download_state": "downloading",
             "download_finished": False, "download_present": False, "size": 2000, "seeds": 9, "download_speed": 300,
             "upload_speed": 10, "peers": 4, "ratio": 0.5, "created_at": "2026-10-02T00:00:00Z", "updated_at": "2026-10-03T00:00:00Z",
             "hash": "def456"},
            {"id": 8, "name": "eight.iso", "progress": 1, "download_state": "uploading",
             "download_finished": True, "download_present": True, "size": 1, "updated_at": "2026-10-04T00:00:00Z"}]),
        ok([{"id": 3, "name": "web.bin", "progress": 1, "download_state": "completed",
             "download_finished": True, "download_present": True, "size": 5}]),
    )
    out = Torbox("t", s).torrents()
    assert out[0] == {"id": "7", "name": "seven.mkv", "progress": 25.0, "state": "downloading", "done": False,
                      "size": 2000, "seeds": 9, "speed": 300, "kind": "torrent", "up_speed": 10, "peers": 4,
                      "ratio": 0.5, "added": "2026-10-02T00:00:00Z", "finished": "", "hash": "def456"}
    assert out[1]["done"] is True and out[1]["progress"] == 100.0 and out[1]["finished"] == "2026-10-04T00:00:00Z"
    assert out[2] == {"id": "3", "name": "web.bin", "progress": 100.0, "state": "web: completed", "done": True,
                      "size": 5, "seeds": 0, "speed": 0, "kind": "webdl", "up_speed": 0, "peers": 0, "ratio": 0.0,
                      "added": "", "finished": "", "hash": ""}
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
    name, fileobj, ctype = s.calls[0][2]["files"]["file"]
    assert name == "debrid-test.torrent"
    assert ctype == "application/x-bittorrent"
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


def test_tb_control_ops():
    s = FakeSession(FakeResp(200, {"success": True, "error": None, "detail": "Torrent will stop seeding.", "data": None}),
                    ok(None), ok(None))
    tb = Torbox("t", s)
    assert tb.control("7", "stop_seeding") == "Torrent will stop seeding."
    tb.control("7", "reannounce", "torrent")
    tb.control("3", "resume", "webdl")
    assert s.calls[0][:2] == ("POST", TB + "/torrents/controltorrent")
    assert s.calls[0][2]["json"] == {"torrent_id": 7, "operation": "stop_seeding"}
    assert s.calls[1][2]["json"] == {"torrent_id": 7, "operation": "reannounce"}
    assert s.calls[2][:2] == ("POST", TB + "/webdl/controlwebdownload")
    assert s.calls[2][2]["json"] == {"webdl_id": 3, "operation": "resume"}


def test_tb_links_zip_then_per_file():
    s = FakeSession(
        ok({"id": 7, "size": 1500, "files": [{"id": 0, "name": "dir/a.mkv", "short_name": "a.mkv", "size": 1400},
                                             {"id": 1, "name": "b.srt", "size": 100}]}),
        ok("https://cdn/zip"),
        ok("https://cdn/a"),
        ok("https://cdn/b"),
    )
    out = Torbox("tok", s).links("7", "torrent")
    assert out == [{"name": "All files (zip)", "url": "https://cdn/zip", "size": 1500},
                   {"name": "a.mkv", "url": "https://cdn/a", "size": 1400}, {"name": "b.srt", "url": "https://cdn/b", "size": 100}]
    assert s.calls[0][:2] == ("GET", TB + "/torrents/mylist")
    assert s.calls[0][2]["params"] == {"id": 7, "bypass_cache": "true"}
    assert s.calls[1][:2] == ("GET", TB + "/torrents/requestdl")
    assert s.calls[1][2]["params"] == {"token": "tok", "torrent_id": 7, "zip_link": "true"}
    assert s.calls[2][2]["params"] == {"token": "tok", "torrent_id": 7, "file_id": 0}


def test_non_json_200_is_api_error():
    def html():
        r = FakeResp(200, None)
        r.text = "<html>maintenance</html>"
        return r
    for client in (RealDebrid("t", FakeSession(html())), Torbox("t", FakeSession(html()))):
        try:
            client.account()
            assert False
        except ApiError as e:
            assert str(e) == f"Unexpected response from {client.name}"


def test_rd_unrestrict_alternative():
    s = FakeSession(FakeResp(200, {"filename": "yt", "alternative": [{"download": "https://cdn/alt"}]}))
    assert RealDebrid("t", s).add_link("https://yt/x") == "https://cdn/alt"
    try:
        RealDebrid("t", FakeSession(FakeResp(200, {"filename": "none"}))).add_link("https://yt/y")
        assert False
    except ApiError as e:
        assert str(e) == "No direct link returned"
    s = FakeSession(FakeResp(200, {"id": "A", "links": ["https://rd/1", "https://rd/2"]}),
                    FakeResp(200, {"filename": "none"}),
                    FakeResp(200, {"filename": "two", "download": "https://cdn/two"}))
    out = RealDebrid("t", s).links("A")
    assert out[0]["url"] is None and "No direct link" in out[0]["name"]
    assert out[1] == {"name": "two", "url": "https://cdn/two", "size": 0}


def test_tb_links_webdl():
    s = FakeSession(ok({"id": 3, "files": [{"id": 0, "name": "w.bin"}]}), ok("https://cdn/w"))
    assert Torbox("tok", s).links("3", "webdl") == [{"name": "w.bin", "url": "https://cdn/w", "size": 0}]
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


def test_magnet_hash():
    hex40 = "0123456789abcdef0123456789abcdef01234567"
    assert magnet_hash(f"magnet:?xt=urn:btih:{hex40.upper()}&dn=x") == hex40
    assert magnet_hash(f"magnet:?dn=x&xt=urn:btih:{hex40}") == hex40
    import base64
    b32 = base64.b32encode(bytes.fromhex(hex40)).decode()
    assert magnet_hash(f"magnet:?xt=urn:btih:{b32}") == hex40
    assert magnet_hash("magnet:?dn=nohash") == "" and magnet_hash("https://x") == "" and magnet_hash("") == ""


def test_tb_check_cached():
    a, b, c = "a" * 40, "b" * 40, "c" * 40
    s = FakeSession(ok({a.upper(): {"name": "A", "size": 1}, c: {"name": "C"}}))
    assert Torbox("t", s).check_cached([a, b, c]) == {a, c}
    assert s.calls[0][:2] == ("GET", TB + "/torrents/checkcached")
    assert s.calls[0][2]["params"] == {"hash": f"{a},{b},{c}", "format": "object", "list_files": "false"}
    assert Torbox("t", FakeSession(ok([{"hash": b}]))).check_cached([a, b]) == {b}
    assert Torbox("t", FakeSession(ok(None))).check_cached([a]) == set()
    empty = FakeSession()
    assert Torbox("t", empty).check_cached([]) == set() and empty.calls == []


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


def test_config_roundtrip_and_mode(tmp_dir="/tmp/debrid-test-config"):
    import os
    import shutil
    import stat
    import debrid_applet
    shutil.rmtree(tmp_dir, ignore_errors=True)
    debrid_applet.CONFIG_DIR = tmp_dir
    debrid_applet.CONFIG = os.path.join(tmp_dir, "config.json")
    defaults = {"torbox_key": "", "realdebrid_key": "", "notify_added": True, "notify_done": True,
                "watch_clipboard": True, "columns": {}, "window": []}
    assert debrid_applet.load_config() == defaults
    debrid_applet.save_config({"torbox_key": "a", "realdebrid_key": "b", "notify_done": False})
    assert stat.S_IMODE(os.stat(debrid_applet.CONFIG).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_dir).st_mode) == 0o700
    assert debrid_applet.load_config() == {**defaults, "torbox_key": "a", "realdebrid_key": "b", "notify_done": False}
    with open(debrid_applet.CONFIG, "w") as f:
        f.write("{not json")
    assert debrid_applet.load_config() == defaults


def test_open_url_only_http():
    import debrid_applet
    calls = []
    debrid_applet.subprocess.Popen = lambda args, **kw: calls.append(args)
    assert debrid_applet.open_url("https://cdn.example/file.mkv") is True
    assert debrid_applet.open_url("http://cdn.example/f") is True
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "-x", "", None):
        assert debrid_applet.open_url(bad) is False
    assert calls == [["xdg-open", "https://cdn.example/file.mkv"], ["xdg-open", "http://cdn.example/f"]]


def test_add_one_routes_by_kind():
    import debrid_applet

    class Fake:
        calls = []
        def add_magnet(self, m): self.calls.append(("magnet", m))
        def add_link(self, u): self.calls.append(("link", u)); return "https://cdn/x"
        def add_torrent_file(self, p): self.calls.append(("file", p))
    c = Fake()
    assert debrid_applet.add_one(c, "MAGNET:?xt=urn:btih:abc") is None
    assert debrid_applet.add_one(c, "https://host/f") == "https://cdn/x"
    assert debrid_applet.add_one(c, "http://host/g") == "https://cdn/x"
    assert debrid_applet.add_one(c, "/tmp/a.torrent") is None
    assert c.calls == [("magnet", "MAGNET:?xt=urn:btih:abc"), ("link", "https://host/f"),
                       ("link", "http://host/g"), ("file", "/tmp/a.torrent")]
    try:
        debrid_applet.add_one(c, "ftp://nope")
        assert False
    except ApiError as e:
        assert "not a magnet" in str(e)


def test_local_time_and_cell_formats():
    import debrid_applet
    assert debrid_applet.local_time("") == ""
    assert debrid_applet.local_time("garbage") == ""
    s = debrid_applet.local_time("2026-10-10T19:52:35.000Z")
    assert len(s) == 16 and s[4] == "-" and s[10] == " "
    assert debrid_applet.local_time("2026-10-10T19:52:35Z")[:10] == s[:10]
    assert debrid_applet.fmt_speed(0) == "" and debrid_applet.fmt_speed(None) == ""
    assert debrid_applet.fmt_speed(1536) == "1.5 KB/s"
    assert debrid_applet.na(None) == "" and debrid_applet.na(4) == "4" and debrid_applet.na(0.5, "{:.2f}") == "0.50"


def test_state_kind_buckets():
    import debrid_applet
    k = debrid_applet.state_kind
    assert k("downloading") == "active" and k("metaDL") == "active" and k("queued") == "active"
    assert k("waiting_files_selection") == "active" and k("magnet_conversion") == "active"
    assert k("adding…") == "active"
    assert k("downloaded") == "done" and k("cached") == "done" and k("web: completed") == "done"
    assert k("uploading (no peers)") == "seed"
    assert k("stalled (no seeds)") == "warn" and k("paused") == "warn"
    assert k("error") == "error" and k("magnet_error") == "error" and k("dead") == "error" and k("virus") == "error"
    assert k("") == "done"
    assert set(debrid_applet.PALETTE) == {"active", "done", "seed", "warn", "error"}


def test_relative_time():
    from datetime import datetime, timezone, timedelta
    import debrid_applet
    now = datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc)
    def rel(**delta):
        return debrid_applet.relative_time((now - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ"), now)
    assert rel(seconds=10) == "just now"
    assert rel(minutes=1) == "1 minute ago"
    assert rel(minutes=2) == "2 minutes ago"
    assert rel(minutes=59) == "59 minutes ago"
    assert rel(hours=1) == "1 hour ago"
    assert rel(hours=12) == "12 hours ago"
    assert rel(hours=23, minutes=50) == "yesterday"
    assert rel(hours=30) == "yesterday"
    assert rel(days=3) == "3 days ago"
    assert rel(days=9) == "1 week ago"
    assert rel(days=20) == "2 weeks ago"
    assert rel(days=70) == "2 months ago"
    assert rel(days=800) == "2 years ago"
    assert debrid_applet.relative_time("", now) == "" and debrid_applet.relative_time("junk", now) == ""
    assert debrid_applet.relative_time((now + timedelta(minutes=5)).isoformat(), now) == "just now"


def test_video_links_and_magnet():
    import debrid_applet
    links = [{"name": "All files (zip)", "url": "https://cdn/zip"}, {"name": "ep1.mkv", "url": "https://cdn/1"},
             {"name": "ep1.srt", "url": "https://cdn/s"}, {"name": "Failed: x", "url": None}]
    assert debrid_applet.video_links(links) == [{"name": "ep1.mkv", "url": "https://cdn/1"}]
    assert debrid_applet.video_links(links[:1] + links[2:]) == [{"name": "ep1.srt", "url": "https://cdn/s"}]
    assert debrid_applet.video_links([]) == []
    assert debrid_applet.magnet_for("ABC123", "My Show S01") == "magnet:?xt=urn:btih:ABC123&dn=My%20Show%20S01"
    assert debrid_applet.magnet_for("", "x") == ""


def test_drop_items():
    import debrid_applet
    uris = ["file:///tmp/a%20b.torrent", "https://host/x", "file://nowhere:%%bad"]
    assert debrid_applet.drop_items(uris, "ignored") == ["/tmp/a b.torrent", "https://host/x"]
    assert debrid_applet.drop_items([], " magnet:?xt=a \n\nhttps://y\n") == ["magnet:?xt=a", "https://y"]
    assert debrid_applet.drop_items([], None) == []


def test_column_layout_per_tab_and_migration():
    import debrid_applet
    cols = {"order": ["name", "check"], "hidden": ["peers"], "sort": ["added", "desc"]}  # old shared format
    tb = debrid_applet.column_layout(cols, "Torbox")
    assert tb == {"order": ["name", "check"], "hidden": ["peers"], "sort": ["added", "desc"]}
    assert cols["Real-Debrid"] == tb and "order" not in cols
    tb["hidden"].append("ul")
    assert "ul" not in cols["Real-Debrid"]["hidden"]  # independent copies
    fresh = {}
    assert debrid_applet.column_layout(fresh, "Real-Debrid") == {"order": [], "hidden": ["ul", "peers", "ratio"], "sort": ["added", "desc"]}
    assert debrid_applet.column_layout(fresh, "Torbox") == {"order": [], "hidden": [], "sort": ["added", "desc"]}
    never_sorted = {"Torbox": {"order": [], "hidden": []}}
    assert debrid_applet.column_layout(never_sorted, "Torbox")["sort"] == ["added", "desc"]
    assert set(fresh) == {"Torbox", "Real-Debrid"}


def test_aria2_command():
    import debrid_applet
    cmd = debrid_applet.aria2_command(["https://cdn/a b.mkv", "https://cdn/c"], "/home/u/Downloads")
    assert cmd == "aria2c -c -x 8 -s 8 --dir=/home/u/Downloads 'https://cdn/a b.mkv' https://cdn/c"
    assert debrid_applet.aria2_command([], "/x") == ""


def test_add_label():
    import debrid_applet
    assert debrid_applet.add_label("magnet:?xt=urn:btih:abc&dn=Some+Name+S01") == "Some Name S01"
    assert debrid_applet.add_label("magnet:?xt=urn:btih:abcdef").startswith("magnet:?xt=urn:btih:abcdef")
    assert debrid_applet.add_label("/tmp/x/thing.torrent") == "thing.torrent"
    assert debrid_applet.add_label("https://hoster/dir/file.bin") == "file.bin"
    assert len(debrid_applet.add_label("x" * 500)) == 60


if __name__ == "__main__":
    import sys
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} passed")
