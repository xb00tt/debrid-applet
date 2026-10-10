"""Offline tests for debrid_api. Run: python3 test_debrid_api.py"""
import json
import requests

from debrid_api import AllDebrid, ApiError, RealDebrid, Torbox, human_size, magnet_hash

RD = "https://api.real-debrid.com/rest/1.0"
TB = "https://api.torbox.app/v1/api"
AD = "https://api.alldebrid.com"


def ad_ok(data):
    return FakeResp(200, {"status": "success", "data": data})


def ad_err(code, message="bad"):
    return FakeResp(200, {"status": "error", "error": {"code": code, "message": message}})


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
    assert s.calls[0][2]["params"] == {"limit": 5000, "page": 1}
    assert s.calls[1][:2] == ("POST", RD + "/torrents/selectFiles/B")
    assert s.calls[1][2]["data"] == {"files": "all"}


def test_rd_torrents_paginates():
    full = [{"id": str(i), "filename": f"{i}", "status": "downloaded"} for i in range(5000)]
    s = FakeSession(FakeResp(200, full), FakeResp(200, full[:3]))
    out = RealDebrid("t", s).torrents()
    assert len(out) == 5003 and s.calls[1][2]["params"] == {"limit": 5000, "page": 2}
    assert len(s.calls) == 2


def test_rd_active_only():
    s = FakeSession(FakeResp(200, [{"id": "A", "filename": "a", "status": "downloading", "progress": 5}]))
    out = RealDebrid("t", s).torrents(active_only=True)
    assert len(out) == 1 and s.calls[0][2]["params"] == {"limit": 5000, "page": 1, "filter": "active"}


def test_ad_active_only():
    s = FakeSession(ad_ok({"magnets": [{"id": 1, "filename": "a", "statusCode": 1, "status": "Downloading", "size": 10}]}))
    out = AllDebrid("t", s).torrents(active_only=True)
    assert len(out) == 1 and s.calls[0][2]["data"] == {"status": "active"}


def test_merge_active():
    import debrid_applet as da
    full = [{"id": "1", "done": False, "name": "a"}, {"id": "2", "done": True, "name": "b"}, {"id": "3", "done": False, "name": "c"}]
    merged, need_full = da.merge_active(full, [{"id": "1", "done": False, "name": "a2"}, {"id": "3", "done": False, "name": "c"}])
    assert [t["name"] for t in merged] == ["a2", "b", "c"] and not need_full
    merged, need_full = da.merge_active(full, [{"id": "1", "done": False, "name": "a"}])  # 3 left the active set
    assert need_full
    merged, need_full = da.merge_active(full, [{"id": "1", "done": False, "name": "a"}, {"id": "3", "done": False, "name": "c"}, {"id": "9", "done": False, "name": "new"}])
    assert [t["id"] for t in merged] == ["9", "1", "2", "3"] and not need_full


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


def test_ad_account():
    s = FakeSession(ad_ok({"user": {"isPremium": True, "isTrial": True, "premiumUntil": 1760745600, "fidelityPoints": 12}}))
    assert AllDebrid("t", s).account() == {"plan": "trial", "expires": "2025-10-18", "extra": "12 points"}
    assert s.calls[0][:2] == ("GET", AD + "/v4/user")
    assert s.calls[0][2]["headers"] if "headers" in s.calls[0][2] else s.headers["Authorization"] == "Bearer t"
    assert AllDebrid("t", FakeSession(ad_ok({"user": {"isPremium": False, "premiumUntil": 0}}))).account()["plan"] == "free"


def test_ad_torrents_normalize():
    s = FakeSession(ad_ok({"magnets": [
        {"id": 11, "filename": "a.iso", "size": 1000, "status": "Downloading", "statusCode": 1, "downloaded": 250,
         "uploaded": 0, "seeders": 7, "downloadSpeed": 500, "uploadSpeed": 20, "uploadDate": 1760000000, "completionDate": 0},
        {"id": 12, "filename": "b.mkv", "size": 400, "status": "Ready", "statusCode": 4, "uploadDate": 1760000000, "completionDate": 1760003600},
        {"id": 13, "filename": "c", "size": 5, "status": "File too big", "statusCode": 8},
        {"id": 14, "filename": "d", "size": 5, "status": "In Queue", "statusCode": 0},
    ]}))
    out = AllDebrid("t", s).torrents()
    assert s.calls[0][:2] == ("POST", AD + "/v4.1/magnet/status")
    assert out[0] == {"id": "11", "name": "a.iso", "progress": 25.0, "state": "Downloading", "done": False, "size": 1000,
                      "seeds": 7, "speed": 500, "kind": "torrent", "up_speed": 20, "peers": None, "ratio": None,
                      "added": "2025-10-09T08:53:20Z", "finished": "", "hash": ""}
    assert out[1]["done"] is True and out[1]["progress"] == 100.0 and out[1]["finished"] == "2025-10-09T09:53:20Z"
    assert out[2]["state"] == "error: File too big" and out[2]["done"] is False
    assert out[3]["state"] == "In Queue"


def test_ad_add_magnet_and_file(tmp_path="/tmp/debrid-test.torrent"):
    s = FakeSession(ad_ok({"magnets": [{"magnet": "m", "id": 5, "ready": True}]}))
    AllDebrid("t", s).add_magnet("magnet:?xt=urn:btih:abc")
    assert s.calls[0][:2] == ("POST", AD + "/v4/magnet/upload")
    assert s.calls[0][2]["data"] == {"magnets[]": ["magnet:?xt=urn:btih:abc"]}
    try:
        AllDebrid("t", FakeSession(ad_ok({"magnets": [{"magnet": "m", "error": {"code": "MAGNET_INVALID_URI", "message": "Invalid magnet"}}]}))).add_magnet("x")
        assert False
    except ApiError as e:
        assert str(e) == "Invalid magnet"
    with open(tmp_path, "wb") as f:
        f.write(b"d8:announce0:e")
    s = FakeSession(ad_ok({"files": [{"file": "debrid-test.torrent", "id": 6}]}))
    AllDebrid("t", s).add_torrent_file(tmp_path)
    assert s.calls[0][:2] == ("POST", AD + "/v4/magnet/upload/file")
    name, fileobj, ctype = s.calls[0][2]["files"]["files[]"]
    assert name == "debrid-test.torrent" and ctype == "application/x-bittorrent" and fileobj.closed


def test_ad_delete_and_links():
    s = FakeSession(ad_ok({"message": "deleted"}))
    AllDebrid("t", s).delete("11")
    assert s.calls[0][:2] == ("POST", AD + "/v4/magnet/delete") and s.calls[0][2]["data"] == {"id": 11}
    s = FakeSession(
        ad_ok({"magnets": [{"id": "11", "files": [
            {"n": "Season", "e": [{"n": "e1.mkv", "s": 100, "l": "https://alldebrid.com/f/1"}]},
            {"n": "readme.txt", "s": 5, "l": "https://alldebrid.com/f/2"}]}]}),
        ad_ok({"link": "https://cdn/e1", "filename": "e1.mkv", "filesize": 100}),
        ad_ok({"link": "https://cdn/readme", "filename": "readme.txt", "filesize": 5}),
    )
    out = AllDebrid("t", s).links("11")
    assert out == [{"name": "Season/e1.mkv", "url": "https://cdn/e1", "size": 100},
                   {"name": "readme.txt", "url": "https://cdn/readme", "size": 5}]
    assert s.calls[0][:2] == ("POST", AD + "/v4/magnet/files") and s.calls[0][2]["data"] == {"id[]": [11]}
    assert s.calls[1][:2] == ("POST", AD + "/v4/link/unlock") and s.calls[1][2]["data"] == {"link": "https://alldebrid.com/f/1"}
    assert AllDebrid("t", FakeSession(ad_ok({"magnets": [{"id": "99", "error": {"code": "MAGNET_INVALID_ID", "message": "nope"}}]}))).links("99") == []


def test_ad_add_link_and_errors():
    s = FakeSession(ad_ok({"link": "https://cdn/f", "filename": "f.bin"}))
    assert AllDebrid("t", s).add_link("https://hoster/x") == "https://cdn/f"
    for code in ("AUTH_BAD_APIKEY", "AUTH_MISSING_APIKEY"):
        try:
            AllDebrid("t", FakeSession(ad_err(code))).account()
            assert False
        except ApiError as e:
            assert str(e) == "Invalid API key"
    try:
        AllDebrid("t", FakeSession(ad_err("MAGNET_TOO_MANY_ACTIVE", "Too many active magnets"))).torrents()
        assert False
    except ApiError as e:
        assert str(e) == "Too many active magnets"
    try:
        AllDebrid("t", FakeSession(FakeResp(400, {"status": "error", "error": {"code": "LINK_HOST_NOT_SUPPORTED", "message": "Host not supported"}}))).add_link("x")
        assert False
    except ApiError as e:
        assert str(e) == "Host not supported"


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
    assert s.calls[0][2]["params"] == {"bypass_cache": "true", "limit": 5000}
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
    defaults = {"torbox_key": "", "realdebrid_key": "", "alldebrid_key": "", "notify_added": True, "notify_done": True,
                "watch_clipboard": True, "columns": {}, "window": [], "remember_filter": True, "filters": {},
                "dblclick_play": False, "dblclick_asked": False,
                "watch_dir": "", "watch_service": "Torbox", "auto_download": False, "download_dir": "~/Downloads",
                "http_port": 0, "http_token": "", "notified_expiry": {}, "log_level": "actions", "log_max_kb": 1000}
    assert debrid_applet.load_config() == defaults
    debrid_applet.save_config({"torbox_key": "a", "realdebrid_key": "b", "notify_done": False})
    assert debrid_applet.column_layout({}, "AllDebrid")["hidden"] == ["peers", "ratio"]
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
    assert k("In Queue") == "active" and k("Ready") == "done" and k("error: File too big") == "error"
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
    assert debrid_applet.strict_videos(links) == [{"name": "ep1.mkv", "url": "https://cdn/1"}]
    assert debrid_applet.strict_videos(links[:1] + links[2:]) == []
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


def test_power_helpers():
    import debrid_applet as da
    from datetime import date, timedelta
    today = date.today()
    assert da.days_until((today + timedelta(days=3)).isoformat()) == 3
    assert da.days_until(today.isoformat()) == 0 and da.days_until("") is None and da.days_until("junk") is None
    links = [{"name": "Show.S01E01.mkv", "url": "https://v1"}, {"name": "Show.S01E01.srt", "url": "https://s1"},
             {"name": "Show.S01E02.mkv", "url": "https://v2"}, {"name": "other.vtt", "url": "https://s9"}]
    assert da.pick_subtitle("Show.S01E01.mkv", links) == "https://s1"
    assert da.pick_subtitle("Show.S01E02.mkv", links) is None  # two candidates, none matching: skip
    assert da.pick_subtitle("x.mkv", [{"name": "only.srt", "url": "https://s"}]) == "https://s"
    assert da.player_cmd("mpv", "https://v", "https://s") == ["mpv", "--sub-file=https://s", "https://v"]
    assert da.player_cmd("celluloid", "https://v", "https://s") == ["celluloid", "--mpv-sub-file=https://s", "https://v"]
    assert da.player_cmd("vlc", "https://v", "https://s") == ["vlc", "--sub-file=https://s", "https://v"]
    assert da.player_cmd("totem", "https://v", "https://s") == ["totem", "https://v"]
    assert da.player_cmd("mpv", "https://v", None) == ["mpv", "https://v"]
    assert da.download_command(["https://a", "https://b"], "/d/x", "aria2c") == ["aria2c", "-c", "-x", "8", "-s", "8", "--dir=/d/x", "https://a", "https://b"]
    assert da.download_command(["https://a"], "/d/x", "wget") == ["wget", "-c", "-P", "/d/x", "https://a"]
    assert da.safe_dirname("Show: S01/E01 [x]?") == "Show_ S01_E01 [x]_"


def test_local_api():
    import json, urllib.request
    from debrid_local_api import LocalApi, parse_add
    assert parse_add("service=Torbox&magnet=magnet%3A%3Fxt%3Da&link=https%3A%2F%2Fh%2Ff", "https://body/1\n\n") == \
        ("Torbox", ["magnet:?xt=a", "https://h/f", "https://body/1"])
    assert parse_add("", "") == ("", [])
    calls = []
    api = LocalApi(0, "sekret", on_add=lambda s, items: calls.append(("add", s, items)) or len(items),
                   on_toggle=lambda: calls.append(("toggle",)), status=lambda: {"Torbox": 2})
    api.start()
    base = f"http://127.0.0.1:{api.port}"

    def call(path, data=None, headers=None, host=None):
        req = urllib.request.Request(base + path, data=data, method="POST" if data is not None else "GET",
                                     headers=headers or {})
        if host:
            req.add_unredirected_header("Host", host)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read()), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), e.headers
    assert call("/add?magnet=magnet:?xt=a")[0] == 401 and not calls  # no token
    assert call("/add?magnet=magnet:?xt=a", headers={"X-Debrid-Token": "wrong"})[0] == 401
    code, body, headers = call("/add?service=Torbox&magnet=magnet:?xt=urn:btih:abc&token=sekret")
    assert (code, body) == (200, {"ok": True, "queued": 1}) and calls[-1] == ("add", "Torbox", ["magnet:?xt=urn:btih:abc"])
    assert "Access-Control-Allow-Origin" not in headers
    code, body, _ = call("/add?service=AllDebrid", data=b"https://x/1\nhttps://x/2", headers={"X-Debrid-Token": "sekret"})
    assert body["queued"] == 2 and calls[-1][2] == ["https://x/1", "https://x/2"]
    code, body, _ = call("/add?token=sekret&item=/etc/passwd&magnet=magnet:?xt=b")
    assert code == 400 and "magnet" in body["error"] and calls[-1][2] == ["magnet:?xt=urn:btih:abc"] or calls[-1][2] == ["https://x/1", "https://x/2"]
    assert call("/toggle?token=sekret")[1] == {"ok": True} and calls[-1] == ("toggle",)
    assert call("/status?token=sekret")[1] == {"Torbox": 2}
    assert call("/add?token=sekret&magnet=magnet:?xt=c", host="evil.example:80")[0] == 403
    assert call("/add?token=sekret")[0] == 400
    assert call("/nope?token=sekret")[0] == 404
    api.stop()


def test_activity_log(path="/tmp/debrid-test-activity.log"):
    import os
    import debrid_applet as da
    if os.path.exists(path):
        os.remove(path)
    da.append_log(path, "Torbox", "Added 1")
    da.append_log(path, "", "Local API started on port 1")
    lines = open(path).read().splitlines()
    assert len(lines) == 2 and lines[0].endswith("  Torbox       Added 1") and "Local API" in lines[1]
    assert len(lines[0].split("  ")[0]) == 19  # YYYY-MM-DD HH:MM:SS
    with open(path, "w") as f:
        f.write("x" * 2_000_000 + "\n")
    da.append_log(path, "AllDebrid", "after rotation")
    data = open(path).read()
    assert len(data) < 300_000 and data.endswith("after rotation\n")
    assert da.read_log_tail(path, 1).strip().endswith("after rotation")
    assert da.read_log_tail("/nonexistent/x.log", 5) == ""


def test_log_levels():
    import debrid_applet as da
    s = da.should_log
    assert not s("error", "off") and not s("action", "off") and not s("verbose", "off")
    assert s("error", "errors") and not s("action", "errors") and not s("verbose", "errors")
    assert s("error", "actions") and s("action", "actions") and not s("verbose", "actions")
    assert s("error", "verbose") and s("action", "verbose") and s("verbose", "verbose")
    assert s("action", "garbage")  # unknown setting behaves like the default


def test_footer_helpers():
    import debrid_applet as da
    assert da.plan_color("premium") == da.PALETTE["done"] and da.plan_color("Essential") == da.PALETTE["done"]
    assert da.plan_color("trial") == da.PALETTE["warn"] and da.plan_color("free") == da.PALETTE["error"]
    assert da.expiry_text("") == "no expiry"
    from datetime import date, timedelta
    soon = (date.today() + timedelta(days=3)).isoformat()
    assert da.expiry_text(soon) == f"expires {soon} (in 3 days)"
    assert da.expiry_text(date.today().isoformat()) == f"expires {date.today().isoformat()} (today)"
    past = (date.today() - timedelta(days=2)).isoformat()
    assert da.expiry_text(past) == f"expired {past}"
    rows = [{"speed": 1000, "up_speed": 200, "done": False, "size": 10}, {"speed": 0, "up_speed": None, "done": True, "size": 20}]
    assert da.totals_text(rows, shown=2, ticked=0) == "⬇ 1000 B/s · ⬆ 200 B/s · 1 active · 2 items · 30 B"
    assert da.totals_text(rows, shown=1, ticked=1) == "⬇ 1000 B/s · ⬆ 200 B/s · 1 active · 2 items · 30 B · showing 1 · 1 ticked"
    assert da.totals_text([], shown=0, ticked=0) == "0 items"


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
