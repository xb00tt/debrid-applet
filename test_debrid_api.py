"""Offline tests for debrid_api. Run: python3 test_debrid_api.py"""
import json
import requests

from debrid_api import ApiError, RealDebrid, Torbox, human_size

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
    assert out[0] == {"name": "one.mkv", "url": "https://cdn/one"}
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


def test_config_roundtrip_and_mode(tmp_dir="/tmp/debrid-test-config"):
    import os
    import shutil
    import stat
    import debrid_applet
    shutil.rmtree(tmp_dir, ignore_errors=True)
    debrid_applet.CONFIG_DIR = tmp_dir
    debrid_applet.CONFIG = os.path.join(tmp_dir, "config.json")
    defaults = {"torbox_key": "", "realdebrid_key": "", "notify_added": True, "notify_done": True}
    assert debrid_applet.load_config() == defaults
    debrid_applet.save_config({"torbox_key": "a", "realdebrid_key": "b", "notify_done": False})
    assert stat.S_IMODE(os.stat(debrid_applet.CONFIG).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_dir).st_mode) == 0o700
    assert debrid_applet.load_config() == {**defaults, "torbox_key": "a", "realdebrid_key": "b", "notify_done": False}
    with open(debrid_applet.CONFIG, "w") as f:
        f.write("{not json")
    assert debrid_applet.load_config() == defaults


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
