"""Offline tests for debrid_api. Run: python3 test_debrid_api.py"""
import json
import os
import requests

# Never let a test or a harness near the real ~/.config and ~/.local/share: debrid_applet reads these at import.
os.environ["DEBRID_APPLET_CONFIG_DIR"] = "/tmp/debrid-test-config"
os.environ["DEBRID_APPLET_DATA_DIR"] = "/tmp/debrid-test-data"

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
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.text = "" if body is None else json.dumps(body)
        self.headers = headers or {}

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
    assert RealDebrid("t", s).account() == {"plan": "Premium", "expires": "2027-01-03", "extra": "1234 points"}
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
    assert out[0] == {"id": "A", "name": "a.mkv", "progress": 42.5, "state": "Downloading", "done": False,
                      "size": 1000, "seeds": 3, "speed": 50, "kind": "torrent", "up_speed": None, "peers": None,
                      "ratio": None, "added": "2026-10-01T10:00:00.000Z", "finished": "", "hash": "abc123",
                      "raw_state": "downloading"}
    assert out[1]["state"] == "Queued" and out[1]["raw_state"] == "waiting_files_selection" and out[2]["state"] == "Done"
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
    s = FakeSession(*[FakeResp(200, full)] * 41)  # a server that never sends a short page
    assert len(RealDebrid("t", s).torrents()) == 40 * 5000 and len(s.calls) == 40  # bounded, never loops forever


def test_rd_active_only():
    s = FakeSession(FakeResp(200, [{"id": "A", "filename": "a", "status": "downloading", "progress": 5}], {"X-Total-Count": "3"}))
    rd = RealDebrid("t", s)
    out = rd.torrents(active_only=True)
    assert len(out) == 1 and s.calls[0][2]["params"] == {"filter": "active", "limit": 5000}  # the filter only works without page
    assert len(s.calls) == 1 and rd.total == 3
    assert not rd.stale([{"id": "A"}, {"id": "B"}, {"id": "C"}]) and rd.stale([{"id": "A"}, {"id": "B"}])  # one deleted elsewhere
    assert not RealDebrid("t", FakeSession()).stale([])  # no answer seen yet: nothing to compare


def test_tb_light_poll():
    item = {"id": 7, "name": "x", "download_state": "downloading", "progress": 0.5, "active": True}
    s = FakeSession(ok(item), FakeResp(404, {"success": True, "error": "ITEM_NOT_FOUND", "detail": "No torrents found for this user."}),
                    ok({**item, "id": 9, "download_state": "completed", "download_finished": True, "download_present": True, "active": False}))
    tb = Torbox("t", s)
    out = tb.torrents(active_only=True, ids=[("7", "torrent"), ("8", "torrent"), ("9", "webdl")])
    assert [(t["id"], t["state"], t["done"]) for t in out] == [("7", "Downloading", False), ("9", "Done (web)", True)]  # 8: deleted elsewhere, left out
    assert [c[1] for c in s.calls] == [TB + "/torrents/mylist", TB + "/torrents/mylist", TB + "/webdl/mylist"]
    assert s.calls[0][2]["params"] == {"id": 7, "bypass_cache": "true"} and s.calls[2][2]["params"] == {"id": 9, "bypass_cache": "true"}
    assert tb.stale([{"id": "7"}, {"id": "9"}]) and len(s.calls) == 3  # the missing 8 counts as changed elsewhere, no probe needed
    assert not Torbox("t", FakeSession(ok([{"id": 7}]), ok([]))).stale([{"id": "7"}])  # and only once
    s = FakeSession()
    assert Torbox("t", s).torrents(active_only=True, ids=[(str(i), "torrent") for i in range(Torbox.LIGHT_MAX + 1)]) is None and s.calls == []  # too many: the whole list instead
    s = FakeSession(ok([{"id": 5}]), ok([]), ok([{"id": 5}]), ok([{"id": 3}]), ok({"id": 5}), ok([]))
    tb = Torbox("t", s)
    assert not tb.stale([{"id": "5"}, {"id": "4"}]) and s.calls[0][2]["params"] == {"bypass_cache": "true", "limit": 1}
    assert tb.stale([{"id": "5"}]) and len(s.calls) == 4  # a web download added elsewhere
    assert not tb.stale([{"id": "4"}])  # an object where a list was expected: not a verdict


def test_poll_one():
    import debrid_applet as da
    from types import SimpleNamespace as NS

    class Client:
        LIGHT_COMPLETE = False
        def __init__(self, active=None, stale_=False, fail=False):
            self.active, self.stale_, self.fail, self.calls = active, stale_, fail, []
        def torrents(self, active_only=False, ids=()):
            self.calls.append(("torrents", active_only, list(ids)))
            if active_only:
                return self.active
            if self.fail:
                raise ApiError("boom")
            return [{"id": "1", "done": True, "state": "Done", "kind": "torrent"}, {"id": "2", "done": False, "state": "Downloading", "kind": "torrent"}]
        def account(self):
            self.calls.append(("account",)); return {"plan": "p"}
        def stale(self, cached):
            self.calls.append(("stale", len(cached))); return self.stale_
    cached = [{"id": "1", "done": True, "state": "Done", "kind": "torrent"}, {"id": "2", "done": False, "state": "Downloading", "kind": "torrent"},
              {"id": "3", "done": False, "state": "Stalled", "kind": "torrent"}, {"id": "4", "done": False, "state": "Error: dead", "kind": "torrent"}]
    def tab(client, torrents=cached, account={"plan": "old"}, last_full=1000):
        return NS(name="S", client=client, torrents=torrents, _account=account, _last_full=last_full)
    fresh = [{"id": "2", "done": False, "state": "Downloading", "kind": "torrent", "progress": 50}]
    c = Client(active=fresh); t = tab(c)
    assert da.poll_one(t, 1030, False, True) == ({"plan": "old"}, [cached[0], fresh[0], cached[2], cached[3]]) and t._last_full == 1000  # light: merged, cache kept
    assert c.calls == [("torrents", True, [("2", "torrent"), ("3", "torrent")]), ("stale", 4)]  # unfinished items that can still move
    c = Client(active=[]); t = tab(c)  # 2 left the active set: everything, and the list's age restarts
    assert da.poll_one(t, 1030, False, True)[1][1]["id"] == "2" and c.calls[-2:] == [("account",), ("torrents", False, [])] and t._last_full == 1030
    c = Client(active=fresh, stale_=True); t = tab(c)  # changed elsewhere: everything
    assert da.poll_one(t, 1030, False, True)[0] == {"plan": "p"} and ("account",) in c.calls
    c = Client(active=None); t = tab(c)  # no light query (AllDebrid, Torbox with too many active items): everything, no stale probe
    assert da.poll_one(t, 1030, False, True)[0] == {"plan": "p"} and not [x for x in c.calls if x[0] == "stale"]
    c = Client(active=fresh); c.LIGHT_COMPLETE = True; t = tab(c)  # a complete light poll: no minute refetch on the fast cadence
    assert da.poll_one(t, 1000 + da.POLL_SLOW, True, True)[0] == {"plan": "old"} and RealDebrid("t", FakeSession()).LIGHT_COMPLETE is True
    assert Torbox("t", FakeSession()).LIGHT_COMPLETE is False and AllDebrid("t", FakeSession()).LIGHT_COMPLETE is False
    assert da.can_change({"done": False, "state": "Stalled"}) and da.can_change({"done": False, "state": "Done (web)"})
    assert not da.can_change({"done": True, "state": "Done"}) and not da.can_change({"done": False, "state": "Expired"}) and not da.can_change({"done": False, "state": "Error: dead"})
    for fast, scheduled, age in ((False, False, 30), (True, False, 30), (False, True, da.POLL_FULL), (True, True, da.POLL_SLOW)):
        c = Client(active=fresh); t = tab(c, last_full=1000)
        assert da.poll_one(t, 1000 + age, fast, scheduled)[0] == {"plan": "p"} and c.calls[0] == ("account",), (fast, scheduled, age)  # user asked, or too old
    c = Client(active=fresh, stale_=True, fail=True); t = tab(c)  # a failing full poll leaves the cache and its age alone
    try:
        da.poll_one(t, 1030, False, True); assert False
    except ApiError:
        assert t.torrents is cached and t._last_full == 1000


def test_ad_active_only():
    s = FakeSession(ad_ok({"magnets": [{"id": 1, "filename": "a", "statusCode": 1, "status": "Downloading", "size": 10}]}))
    assert AllDebrid("t", s).torrents(active_only=True) is None and s.calls == []  # the API ignores its status filter: no light poll


def test_poll_tabs_runs_services_in_parallel():
    import time
    import debrid_applet as da

    class Tab:
        def __init__(self, name): self.name = name
    tabs = [Tab("a"), Tab("b"), Tab("c"), Tab("d")]

    def fetch_one(tab):
        time.sleep(0.3)
        if tab.name == "c":
            raise ApiError("Invalid API key")
        if tab.name == "d":
            raise KeyError("id")
        return {"plan": tab.name}, [tab.name]
    t0 = time.monotonic()
    results = da.poll_tabs(tabs, fetch_one)
    assert time.monotonic() - t0 < 0.9  # four 0.3 s fetches side by side, not 1.2 s in a row
    assert [r[0].name for r in results] == ["a", "b", "c", "d"]  # tab order kept whatever finished first
    assert results[0][1:] == ({"plan": "a"}, ["a"], None) and results[1][1:] == ({"plan": "b"}, ["b"], None)
    assert results[2][1:] == (None, None, "Invalid API key") and results[3][1:] == (None, None, "KeyError: 'id'")
    assert da.poll_tabs([], fetch_one) == []


def test_poll_kind():
    import debrid_applet as da
    assert da.poll_kind(False, 0, False, True) == "full"  # nothing cached yet
    assert da.poll_kind(True, 30, False, False) == "full"  # the user asked: always everything
    assert da.poll_kind(True, 30, False, True) == "light"  # idle timer: active items only, merged into the cache
    assert da.poll_kind(True, da.POLL_FULL, False, True) == "full"  # the cached list is due for a refetch
    assert da.poll_kind(True, 30, True, False) == "full"  # the user asked while something downloads: still everything
    assert da.poll_kind(True, 30, True, True) == "light" and da.poll_kind(True, da.POLL_SLOW, True, True) == "full"  # fast cadence: light within the minute
    assert not AllDebrid("t", FakeSession()).stale([]) and da.MergedClient(lambda: []).torrents(active_only=True, ids=[("1", "torrent")]) == []


def test_merge_active():
    import debrid_applet as da
    full = [{"id": "1", "done": False, "name": "a", "state": "downloading"}, {"id": "2", "done": True, "name": "b"},
            {"id": "3", "done": False, "name": "c", "state": "downloading"}]
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
    from debrid_api import is_invalid_key
    cases = [
        (RealDebrid("t", FakeSession(FakeResp(401, {"error": "bad_token", "error_code": 8}))), "Invalid API key (HTTP 401 bad_token)"),
        (Torbox("t", FakeSession(FakeResp(403, {"success": False, "error": "BAD_TOKEN", "detail": "bad", "data": None}))), "Invalid API key (HTTP 403 BAD_TOKEN)"),
        (RealDebrid("t", FakeSession(FakeResp(401, None))), "Invalid API key (HTTP 401)"),
    ]
    for c, expected in cases:
        try:
            c.account()
            assert False, "expected ApiError"
        except ApiError as e:
            assert str(e) == expected and is_invalid_key(str(e))
    try:  # a 403 without a JSON body is a CDN or proxy page, not a verdict on the key
        Torbox("t", FakeSession(FakeResp(403, None))).account()
        assert False
    except ApiError as e:
        assert str(e).startswith("HTTP 403") and not is_invalid_key(str(e))


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
    rd = RealDebrid("t", s)
    out = rd.torrents()
    assert [t["id"] for t in out] == ["B", "C"]
    assert out[0]["state"] == "Error: select failed: infringing_file"
    s.queue.append(FakeResp(200, [{"id": "B", "filename": "b.iso", "progress": 0, "status": "waiting_files_selection", "bytes": 10}]))
    assert rd.torrents()[0]["state"] == "Error: select failed: infringing_file" and len(s.calls) == 3  # no second POST for a permanent failure
    s = FakeSession(FakeResp(200, [{"id": "B", "filename": "b.iso", "status": "waiting_files_selection"}]), FakeResp(429, {}),
                    FakeResp(200, [{"id": "B", "filename": "b.iso", "status": "waiting_files_selection"}]), FakeResp(204))
    rd = RealDebrid("t", s)
    assert rd.torrents()[0]["state"].startswith("Error: select failed: Rate limited")
    assert rd.torrents()[0]["state"] == "Queued" and len(s.calls) == 4  # a transient failure is retried on the next poll
    item = [{"id": "B", "filename": "b.iso", "status": "waiting_files_selection"}]
    for bad in (FakeResp(503, {}), FakeResp(502, None), FakeResp(503, {"error": "service_unavailable", "error_code": 25}),
                FakeResp(403, {"error": "too_many_active_downloads", "error_code": 21}), FakeResp(400, {"error": "unknown_new_code"})):
        s = FakeSession(FakeResp(200, item), bad, FakeResp(200, item), FakeResp(204))
        rd = RealDebrid("t", s)
        assert rd.torrents()[0]["state"].startswith("Error: select failed")
        assert rd.torrents()[0]["state"] == "Queued" and len(s.calls) == 4, bad.status_code  # anything not known-permanent is retried


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
    try:  # requests embeds the URL; Torbox's requestdl carries the key in the query string
        Torbox("sekretkey", FakeSession(requests.ConnectionError("... with url: /requestdl?token=sekretkey&torrent_id=7"))).account()
        assert False
    except ApiError as e:
        assert "sekretkey" not in str(e) and "token=***" in str(e)


def test_ad_account():
    s = FakeSession(ad_ok({"user": {"isPremium": True, "isTrial": True, "premiumUntil": 1760745600, "fidelityPoints": 12}}))
    assert AllDebrid("t", s).account() == {"plan": "Trial", "expires": "2025-10-18", "extra": "12 points"}
    assert s.calls[0][:2] == ("GET", AD + "/v4/user")
    assert s.calls[0][2]["headers"] if "headers" in s.calls[0][2] else s.headers["Authorization"] == "Bearer t"
    assert AllDebrid("t", FakeSession(ad_ok({"user": {"isPremium": False, "premiumUntil": 0}}))).account()["plan"] == "Free"


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
                      "added": "2025-10-09T08:53:20Z", "finished": "", "hash": "", "raw_state": "Downloading"}
    assert out[1]["done"] is True and out[1]["progress"] == 100.0 and out[1]["finished"] == "2025-10-09T09:53:20Z"
    assert out[1]["state"] == "Done" and out[1]["raw_state"] == "Ready"
    assert out[2]["state"] == "Error: File too big" and out[2]["done"] is False
    assert out[3]["state"] == "Queued" and out[3]["raw_state"] == "In Queue"


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
            assert str(e) == f"Invalid API key ({code})"
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
    assert out[0] == {"id": "7", "name": "seven.mkv", "progress": 25.0, "state": "Downloading", "done": False,
                      "size": 2000, "seeds": 9, "speed": 300, "kind": "torrent", "up_speed": 10, "peers": 4,
                      "ratio": 0.5, "added": "2026-10-02T00:00:00Z", "finished": "", "hash": "def456",
                      "raw_state": "downloading"}
    assert out[1]["done"] is True and out[1]["progress"] == 100.0 and out[1]["finished"] == "2026-10-04T00:00:00Z"
    assert out[1]["state"] == "Seeding"  # Torbox "uploading" on a finished torrent is seeding, not an upload to the service
    assert out[2] == {"id": "3", "name": "web.bin", "progress": 100.0, "state": "Done (web)", "done": True,
                      "size": 5, "seeds": 0, "speed": 0, "kind": "webdl", "up_speed": 0, "peers": 0, "ratio": 0.0,
                      "added": "", "finished": "", "hash": "", "raw_state": "completed"}
    s = FakeSession(ok([{"id": 1, "name": "a", "download_state": "metaDL"}, {"id": 2, "name": "b", "download_state": "stalled (no seeds)"},
                        {"id": 3, "name": "c", "download_state": "expired", "download_finished": True},
                        {"id": 4, "name": "d", "download_state": "somethingNew"}, {"id": 5, "name": "e", "download_state": "missing files, failed"},
                        {"id": 6, "name": "f", "download_state": "uploading (no peers)", "download_finished": True, "download_present": True},
                        {"id": 7, "name": "g", "download_state": "downloading (no peers)"}]), ok([]))
    out = Torbox("t", s).torrents()
    assert [t["state"] for t in out] == ["Queued", "Stalled", "Expired", "SomethingNew", "Error: missing files, failed", "Seeding", "Downloading"]
    assert out[5]["raw_state"] == "uploading (no peers)"  # the suffix survives for the tooltip
    assert s.calls[0][:2] == ("GET", TB + "/torrents/mylist")
    assert s.calls[0][2]["params"] == {"bypass_cache": "true", "limit": 5000}
    assert s.calls[1][:2] == ("GET", TB + "/webdl/mylist")


def test_odd_rows_do_not_sink_the_poll():
    s = FakeSession(ok([{"name": "no id"}, {"id": 2, "name": "junk", "size": "unknown", "progress": None, "seeds": [1]}]), ok([]))
    out = Torbox("t", s).torrents()
    assert [t["id"] for t in out] == ["2"] and out[0]["size"] == 0 and out[0]["progress"] == 0.0 and out[0]["seeds"] == 0
    from debrid_api import _iso_from_ts
    assert _iso_from_ts(10 ** 20) == "" and _iso_from_ts("x") == "" and _iso_from_ts(0) == ""
    s = FakeSession(FakeResp(200, [{"status": "waiting_files_selection"}, {"id": "B", "status": "downloading", "bytes": "junk"}]))
    out = RealDebrid("t", s).torrents()
    assert [t["id"] for t in out] == ["B"] and out[0]["size"] == 0 and len(s.calls) == 1  # no selectFiles for the id-less row
    s = FakeSession(ad_ok({"magnets": [{"filename": "noid"}, {"id": 5, "statusCode": "x", "size": "big"}]}))
    assert [t["id"] for t in AllDebrid("t", s).torrents()] == ["5"]


def test_tb_inactive_items_report_no_speed():
    s = FakeSession(ok([{"id": 1, "name": "x", "progress": 1, "download_state": "stopped seeding", "active": False,
                         "download_finished": True, "download_present": True, "download_speed": 1190345, "upload_speed": 1141680}]), ok([]))
    t = Torbox("t", s).torrents()[0]
    assert t["speed"] == 0 and t["up_speed"] == 0 and t["done"] is True  # Torbox keeps stale speeds on stopped items


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
        assert str(e) == "Invalid API key (BAD_TOKEN)"


def test_config_roundtrip_and_mode(tmp_dir="/tmp/debrid-test-config"):
    import os
    import shutil
    import stat
    import debrid_applet
    shutil.rmtree(tmp_dir, ignore_errors=True)
    assert debrid_applet.CONFIG_DIR == tmp_dir and debrid_applet.CONFIG == os.path.join(tmp_dir, "config.json")  # env override
    assert debrid_applet.DATA_DIR == "/tmp/debrid-test-data" and debrid_applet.LOG_FILE.startswith("/tmp/debrid-test-data/")
    defaults = {"torbox_key": "", "realdebrid_key": "", "alldebrid_key": "", "notify_added": True, "notify_done": True,
                "watch_clipboard": True, "columns": {}, "window": [], "remember_filter": True, "filters": {},
                "dblclick_play": False, "dblclick_asked": False,
                "watch_dir": "", "watch_service": "Torbox", "auto_download": False, "download_dir": "~/Downloads",
                "http_port": 0, "http_token": "", "notified_expiry": {}, "log_level": "actions", "log_max_kb": 1000,
                "stop_seeding_when_done": False, "auto_download_videos_only": False, "delete_after_download": False,
                "route_cached": False, "config_version": 2,
                "feeds": "", "feed_interval_min": 15, "feed_seen": [], "player_command": "", "start_tab": "All",
                "time_filter": "week", "check_updates": True, "update_skip": "", "service_full_names": False,
                "autostart_asked": False}
    assert debrid_applet.load_config() == defaults
    debrid_applet.save_config({"torbox_key": "a", "realdebrid_key": "b", "notify_done": False})
    assert debrid_applet.column_layout({}, "AllDebrid")["hidden"] == ["peers", "ratio"]
    assert stat.S_IMODE(os.stat(debrid_applet.CONFIG).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_dir).st_mode) == 0o700
    assert debrid_applet.load_config() == {**defaults, "torbox_key": "a", "realdebrid_key": "b", "notify_done": False}
    try:
        debrid_applet.save_config({"torbox_key": object()})  # json.dump fails half-way through
    except TypeError:
        pass
    assert debrid_applet.load_config()["torbox_key"] == "a"  # the previous file survived the failed write
    debrid_applet.save_config({**defaults, "realdebrid_key": "b"})  # a key vanishes (cleared field, stray harness): keep the old file
    assert json.load(open(debrid_applet.CONFIG + ".bak"))["torbox_key"] == "a"
    debrid_applet.save_config({**defaults, "realdebrid_key": "b", "window": [1, 1]})  # no key vanishes now: .bak untouched
    assert json.load(open(debrid_applet.CONFIG + ".bak"))["torbox_key"] == "a"
    debrid_applet.save_config(dict(defaults))  # the last key goes too: .bak now holds the file that still had it
    assert json.load(open(debrid_applet.CONFIG + ".bak"))["realdebrid_key"] == "b"
    for bad in ("{not json", "null", "[]", '{"columns": 5}'):
        with open(debrid_applet.CONFIG, "w") as f:
            f.write(bad)
        assert debrid_applet.load_config() == defaults
        assert open(debrid_applet.CONFIG + ".bad").read() == bad  # moved aside, never overwritten
        assert not os.path.exists(debrid_applet.CONFIG)


def test_open_url_only_http():
    import debrid_applet
    calls = []
    real_popen = debrid_applet.subprocess.Popen  # the module attribute is the global subprocess module: restore it
    debrid_applet.subprocess.Popen = lambda args, **kw: calls.append(args)
    try:
        assert debrid_applet.open_url("https://cdn.example/file.mkv") is True
        assert debrid_applet.open_url("http://cdn.example/f") is True
        for bad in ("file:///etc/passwd", "javascript:alert(1)", "-x", "", None):
            assert debrid_applet.open_url(bad) is False
    finally:
        debrid_applet.subprocess.Popen = real_popen
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
    assert k("Queued") == "active" and k("Downloading") == "active" and k("Processing online") == "active" and k("Adding") == "active"
    assert k("Done") == "done" and k("Done (web)") == "done" and k("Seeding") == "seed"
    assert k("Stalled") == "warn" and k("Expired") == "warn"
    assert k("Error") == "error" and k("Error: File too big") == "error" and k("Error: select failed: x") == "error"
    # words no table knows fall back to keyword rules, so an unexpected service state never hides a row
    assert k("metaDL") == "active" and k("stalled (no seeds)") == "warn" and k("uploading (no peers)") == "seed"
    assert k("Ready") == "done" and k("") == "done" and k("virus") == "error"
    assert debrid_applet.is_active({"done": False, "state": "Downloading"}) and not debrid_applet.is_active({"done": True})
    assert debrid_applet.is_active({"done": False, "state": "Processing online"})
    assert not debrid_applet.is_active({"done": False, "state": "Error: File too big"})  # never finishes, needs no fast polls
    assert not debrid_applet.is_active({"done": False, "state": "Expired"})  # Torbox removed the files: nothing moving
    assert not debrid_applet.is_active({"done": False, "state": "Stalled"})  # not moving either
    assert [l for _key, l in debrid_applet.STATE_FILTERS] == ["All states", "Queued", "Downloading", "Processing online",
                                                              "Done", "Seeding", "Stalled", "Expired", "Error"]
    m = debrid_applet.state_matches  # the dropdown matches the label, ignoring an error detail or the (web) suffix
    assert m("Error: File too big", "Error") and m("Done (web)", "Done") and m("Done", "Done") and m("anything", "all")
    assert not m("Done", "Error") and not m("Downloading", "Done") and not m("Adding", "Downloading")
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
    assert debrid_applet.video_links(links, strict=True) == [{"name": "ep1.mkv", "url": "https://cdn/1"}]
    assert debrid_applet.video_links(links[:1] + links[2:], strict=True) == []
    assert debrid_applet.usable_links(links) == links[1:3]
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
    cols = debrid_applet.migrate_config({"columns": cols})["columns"]
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
    cmd = debrid_applet.aria2_command(["https://cdn/a&b.mkv", "https://cdn/c"], "/home/u/Downloads")
    assert cmd == "aria2c -c -Z -x 8 -s 8 --dir=/home/u/Downloads 'https://cdn/a&b.mkv' https://cdn/c"  # shell-safe quoting
    assert debrid_applet.aria2_command(["https://cdn/a b.mkv"], "/x") == ""  # a URL with a space is not a URL
    assert debrid_applet.aria2_command([], "/x") == ""
    assert debrid_applet.aria2_command(["--dir=/etc", "ftp://x"], "/x") == ""  # only http(s) URLs reach the shell line


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
    assert da.pick_subtitle("x.mkv", [{"name": "only.srt", "url": "--log-file=/home/u/.bashrc"}]) is None  # a flag, not a URL
    assert da.player_cmd("mpv", "https://v", "https://s") == ["mpv", "--save-position-on-quit", "--sub-file=https://s", "https://v"]
    assert da.player_cmd("celluloid", "https://v", "https://s") == ["celluloid", "--mpv-save-position-on-quit", "--mpv-sub-file=https://s", "https://v"]
    assert da.player_cmd("vlc", "https://v", "https://s") == ["vlc", "--sub-file=https://s", "https://v"]
    assert da.player_cmd("totem", "https://v", "https://s") == ["totem", "https://v"]
    assert da.player_cmd("mpv", "https://v", None) == ["mpv", "--save-position-on-quit", "https://v"]
    assert da.playlist_cmd("mpv", ["https://1", "https://2"]) == ["mpv", "--save-position-on-quit", "https://1", "https://2"]
    assert da.playlist_cmd("vlc", ["https://1"]) == ["vlc", "https://1"]
    assert da.custom_cmd("kodi-send --action='PlayMedia({url})'", "https://v", "https://s") == ["kodi-send", "--action=PlayMedia(https://v)"]
    assert da.custom_cmd("mpv {url} --sub-file={sub}", "https://v", None) == ["mpv", "https://v"]
    assert da.custom_cmd("mpv {url} --sub-file={sub}", "https://v", "https://s") == ["mpv", "https://v", "--sub-file=https://s"]
    assert da.custom_cmd("stremio", "https://v", None) == ["stremio", "https://v"]
    assert da.download_command(["https://a", "https://b"], "/d/x", "aria2c") == ["aria2c", "-c", "-Z", "-x", "8", "-s", "8", "--dir=/d/x", "https://a", "https://b"]
    assert da.download_command(["https://a"], "/d/x", "wget") == ["wget", "-c", "-P", "/d/x", "https://a"]
    assert da.download_command(["--output-document=/home/u/.bashrc", "https://a", None], "/d/x", "wget") == ["wget", "-c", "-P", "/d/x", "https://a"]
    assert da.download_command(["-e", "ftp://a"], "/d/x", "aria2c") == []  # service junk never becomes a flag
    assert da.download_command(["https://a", "https://b"], "/d/x", "wget", via_stdin=True) == ["wget", "-c", "-P", "/d/x", "-i", "-"]
    assert da.download_command(["https://a"], "/d/x", "aria2c", via_stdin=True) == ["aria2c", "-c", "-Z", "-x", "8", "-s", "8", "--dir=/d/x", "-i", "-"]
    assert da.download_command(["ftp://a"], "/d/x", "wget", via_stdin=True) == []  # nothing to feed: no process
    assert not da.is_http("https://x\n  dir=/etc") and not da.is_http("https://a b")  # one URL per input line, no option lines
    assert da.is_http("https://a/b?c=d%20e")
    assert da.safe_dirname("Show: S01/E01 [x]?") == "Show_ S01_E01 [x]_"
    assert all(da.safe_dirname(n) == "download" for n in (".", "..", "...", " .. ", ""))  # would escape the download folder
    assert da.safe_dirname("..hidden..") == "hidden"


def test_local_api():
    import json, urllib.request
    from debrid_local_api import LocalApi, parse_add
    assert parse_add("service=Torbox&magnet=magnet%3A%3Fxt%3Da&link=https%3A%2F%2Fh%2Ff", "https://body/1\n\n") == \
        ("Torbox", ["magnet:?xt=a", "https://h/f", "https://body/1"])
    assert parse_add("", "") == ("", [])
    calls = []
    api = LocalApi(0, "sekret", on_add=lambda s, items: calls.append(("add", s, items)) or (0 if s == "Nope" else len(items)),
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
    assert code == 400 and "magnet" in body["error"] and calls[-1][2] == ["https://x/1", "https://x/2"]  # nothing new was queued
    assert call("/add?magnet=magnet:?xt=a", headers={"X-Debrid-Token": "sekr\u00e9t"})[0] == 401  # non-ASCII: still a 401, no trace
    assert call("/add?token=sekret&magnet=magnet:?xt=a", data=b"x", headers={"Content-Length": "abc"})[0] == 400
    assert call("/toggle?token=sekret")[1] == {"ok": True} and calls[-1] == ("toggle",)
    assert call("/status?token=sekret")[1] == {"Torbox": 2}
    assert call("/add?token=sekret&magnet=magnet:?xt=c", host="evil.example:80")[0] == 403
    assert call("/add?token=sekret")[0] == 400
    assert call("/add?token=sekret&service=Nope&magnet=magnet:?xt=a")[0] == 400  # on_add refused: not a silent 200
    assert call("/nope?token=sekret")[0] == 404
    api.stop()


def test_activity_log(path="/tmp/debrid-test-activity.log"):
    import os
    import debrid_applet as da
    if os.path.exists(path):
        os.remove(path)
    da.append_log(path, "Torbox", "Added 1")
    da.append_log(path, "", "Local API started on port 1")
    import stat
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600  # item names and error text stay with this user
    lines = open(path).read().splitlines()
    assert len(lines) == 2 and lines[0].endswith("  Torbox       Added 1") and "Local API" in lines[1]
    assert len(lines[0].split("  ")[0]) == 19  # YYYY-MM-DD HH:MM:SS
    with open(path, "w") as f:
        f.write("x" * 2_000_000 + "\n")
    os.chmod(path, 0o644)  # as an older version left it
    da.append_log(path, "AllDebrid", "after rotation")
    data = open(path).read()
    assert len(data) < 300_000 and data.endswith("after rotation\n")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600  # a file made by an older version is tightened too
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
    rows = [{"speed": 1000, "up_speed": 200, "done": False, "size": 10, "state": "downloading"}, {"speed": 0, "up_speed": None, "done": True, "size": 20}]
    assert da.totals_text(rows, shown=2, ticked=0) == "⬇ 1000 B/s · ⬆ 200 B/s · 1 active · 2 items · 30 B"
    assert da.totals_text(rows, shown=1, ticked=1) == "⬇ 1000 B/s · ⬆ 200 B/s · 1 active · 2 items · 30 B · showing 1 · 1 ticked"
    assert da.totals_text([], shown=0, ticked=0) == "0 items"


def test_route_items():
    import debrid_applet as da

    class Checker:
        def check_cached(self, hashes):
            return {h for h in hashes if h.startswith("a")}
    a, b = "a" * 40, "b" * 40
    items = [f"magnet:?xt=urn:btih:{a}", f"magnet:?xt=urn:btih:{b}", "https://host/file", "magnet:?dn=nohash"]
    routed = da.route_items(items, Checker())
    assert routed == {True: [f"magnet:?xt=urn:btih:{a}"], False: [f"magnet:?xt=urn:btih:{b}", "https://host/file", "magnet:?dn=nohash"]}
    assert da.route_items(items, None) == {True: [], False: items}


def test_parse_cli():
    import debrid_applet as da
    assert da.parse_cli([]) == {"toggle": False, "log": False, "service": "", "items": []}
    assert da.parse_cli(["--toggle"])["toggle"] is True
    out = da.parse_cli(["--service", "AllDebrid", "--add", "magnet:?xt=a", "https://x", "--log"])
    assert out == {"toggle": False, "log": True, "service": "AllDebrid", "items": ["magnet:?xt=a", "https://x"]}
    assert da.parse_cli(["--add"])["items"] == []


def test_magnet_export_import(path="/tmp/debrid-test-magnets.txt"):
    import debrid_applet as da
    rows = [{"hash": "abc", "name": "A"}, {"hash": "", "name": "B"}, {"hash": "def", "name": "C\tD"}]
    da.write_magnets(path, rows)
    text = open(path).read()
    assert text == "magnet:?xt=urn:btih:abc&dn=A\tA\nmagnet:?xt=urn:btih:def&dn=C%09D\tC D\n"
    assert da.read_magnets(path) == ["magnet:?xt=urn:btih:abc&dn=A", "magnet:?xt=urn:btih:def&dn=C%09D"]
    with open(path, "w") as f:
        f.write("# comment\n\nmagnet:?xt=urn:btih:zzz\nhttps://host/f\n")
    assert da.read_magnets(path) == ["magnet:?xt=urn:btih:zzz", "https://host/f"]


def test_merged_client():
    import debrid_applet as da

    class Tab:
        def __init__(self, name, torrents):
            self.name, self.torrents, self.client = name, torrents, self
            self.calls = []
        def delete(self, tid, kind="torrent"): self.calls.append(("delete", tid, kind))
        def links(self, tid, kind="torrent"): return [{"name": f"{self.name}-{tid}", "url": "u", "size": 1}]
        def add_magnet(self, m): self.calls.append(("magnet", m))
    tb = Tab("Torbox", [{"id": "7", "name": "x", "done": False, "hash": "h", "kind": "torrent"}])
    rd = Tab("Real-Debrid", [{"id": "A", "name": "y", "done": True, "hash": "", "kind": "torrent"}])
    m = da.MergedClient(lambda: [tb, rd])
    rows = m.torrents()
    assert [r["id"] for r in rows] == ["Torbox|7", "Real-Debrid|A"] and rows[0]["name"] == "x"
    assert [r["service"] for r in rows] == ["Torbox", "Real-Debrid"]
    again = m.torrents()
    assert again[0] is rows[0] and again[1] is rows[1]  # unchanged source dicts keep their copies: no 12k new dicts a poll
    tb.torrents = [dict(tb.torrents[0])]  # a fresh dict for the same item: that copy is rebuilt, the other kept
    assert m.torrents()[0] is not rows[0] and m.torrents()[1] is rows[1]
    assert [r["id"] for r in m.torrents(active_only=True)] == ["Torbox|7"]
    m.delete("Real-Debrid|A", "torrent"); assert rd.calls == [("delete", "A", "torrent")]
    assert m.links("Torbox|7", "torrent") == [{"name": "Torbox-7", "url": "u", "size": 1}]
    m.add_magnet("magnet:?x"); assert tb.calls == [("magnet", "magnet:?x")]  # first configured service
    assert m.account()["plan"] == "all services" and m.name == "All"


def test_retryable():
    import debrid_applet as da
    assert da.retryable("Rate limited, retrying next tick") and da.retryable("Network error: timeout")
    assert not da.retryable("Invalid magnet") and not da.retryable("Invalid API key")


def test_config_migration():
    import debrid_applet as da
    old = {"torbox_key": "x", "columns": {"order": ["name"], "hidden": ["ul"]}}  # as written by version 1
    cfg = da.migrate_config(old)
    assert cfg["config_version"] == 2
    assert cfg["columns"]["Torbox"]["hidden"] == ["ul"] and cfg["columns"]["Real-Debrid"]["order"] == ["name"]
    assert da.migrate_config({})["config_version"] == 2
    assert da.migrate_config({"config_version": 2, "columns": {}})["columns"] == {}
    assert "keys_in_keyring" not in da.migrate_config({"keys_in_keyring": True})


FEED_XML = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Show.S01E01.1080p</title><link>magnet:?xt=urn:btih:aaa&amp;dn=x</link><guid>g1</guid></item>
<item><title>Show.S01E02.720p</title><enclosure url="https://tr/f.torrent" type="application/x-bittorrent"/><guid>g2</guid></item>
<item><title>Other</title><link>https://site/page</link></item>
<item><title>NoLink</title><guid>g4</guid></item>
</channel></rss>"""


def test_feeds():
    import debrid_applet as da
    items = da.parse_feed(FEED_XML)
    assert items == [("g1", "Show.S01E01.1080p", "magnet:?xt=urn:btih:aaa&dn=x"),
                     ("g2", "Show.S01E02.720p", "https://tr/f.torrent"),
                     ("https://site/page", "Other", "https://site/page")]
    assert da.parse_feed("not xml") == []
    import importlib.util
    import xml.etree.ElementTree as std
    bomb = ('<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title>'
            '<link>magnet:?xt=urn:btih:b</link></item></channel></rss>')
    real = da.ET
    try:
        for da.ET in (real, std):  # the package's parser and the install.sh fallback both refuse entity declarations
            assert da.parse_feed(bomb) == []
    finally:
        da.ET = real
    if importlib.util.find_spec("defusedxml"):
        assert da.ET.__name__ == "defusedxml.ElementTree"
    feeds = da.parse_feed_lines("https://a/rss | 1080p | Torbox\n# comment\nhttps://b/rss\nbad line without url | x | y\nhttps://c/rss | | AllDebrid")
    assert feeds == [("https://a/rss", "1080p", "Torbox"), ("https://b/rss", "", ""), ("https://c/rss", "", "AllDebrid")]
    assert da.feed_matches("Show.S01E01.1080p", "1080p") and da.feed_matches("x", "") and not da.feed_matches("x", "[")  # bad regex never matches
    assert not da.feed_matches("Show.720p", "1080p")


def test_fetch_bytes_guards():
    import threading, http.server, os
    import debrid_applet as da
    assert not da.is_public_host("127.0.0.1") and not da.is_public_host("10.1.2.3") and not da.is_public_host("localhost")
    assert da.is_public_host("8.8.8.8") and not da.is_public_host("")
    assert not da.is_public_host("100.64.0.1") and not da.is_public_host("::1") and not da.is_public_host("::ffff:127.0.0.1")
    assert da.resolve_host("8.8.8.8") == (["8.8.8.8"], True) and da.resolve_host("") == (None, False)
    addresses, public = da.resolve_host("localhost")
    assert addresses and set(addresses) <= {"127.0.0.1", "::1"} and public is False
    root = "/tmp/debrid-test-http"
    os.makedirs(root, exist_ok=True)
    open(os.path.join(root, "small.torrent"), "wb").write(b"d8:announce0:e")
    open(os.path.join(root, "big.bin"), "wb").write(b"x" * 300_000)
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            if self.path in ("/host", "/auth"):  # echo Host (the name travels while the socket goes to the pinned IP) or Authorization
                body = (self.headers.get("Host" if self.path == "/host" else "Authorization") or "").encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
            if self.path == "/503":  # a server asking to come back later: one attempt, no sleeping
                self.send_response(503); self.send_header("Retry-After", "5"); self.send_header("Content-Length", "0"); self.end_headers(); return
            if self.path == "/to-private":  # a public-looking URL bouncing the fetch onto the LAN
                self.send_response(302); self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/host"); self.end_headers(); return
            if self.path == "/drip":  # 10 bytes, one every half second, Content-Length known: a slow-drip server
                import time
                self.send_response(200); self.send_header("Content-Length", "10"); self.end_headers()
                for _ in range(10):
                    try:
                        self.wfile.write(b"x"); self.wfile.flush(); time.sleep(0.5)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                return
            if self.path in ("/redir", "/redir-bad", "/loop"):  # -> small file / file: URL / itself
                target = {"/redir": "/small.torrent", "/redir-bad": "file:///etc/passwd", "/loop": "/loop"}[self.path]
                self.send_response(302); self.send_header("Location", target); self.end_headers(); return
            super().do_GET()
        def copyfile(self, source, outputfile):
            try:
                super().copyfile(source, outputfile)
            except (BrokenPipeError, ConnectionResetError):  # the client aborts the oversized download on purpose
                pass
    handler = lambda *a, **k: Quiet(*a, directory=root, **k)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        da.fetch_bytes(base + "/small.torrent", 1000)
        assert False, "loopback must be refused by default"
    except ApiError as e:
        assert "private or local" in str(e)
    assert da.fetch_bytes(base + "/small.torrent", 1000, allow_private=True) == b"d8:announce0:e"
    try:  # urlparse sees host 8.8.8.8 here, urllib3 connects to 127.0.0.1
        da.fetch_bytes(base + "\\@8.8.8.8/small.torrent", 1000)
        assert False, "backslash authority trick must be refused"
    except ApiError as e:
        assert "private or local" in str(e)
    try:
        da.fetch_bytes(base + "/big.bin", 100_000, allow_private=True)
        assert False
    except ApiError as e:
        assert "larger than" in str(e) and "/big.bin" not in str(e)
    try:
        da.fetch_bytes(base + "/missing.torrent?passkey=SECRET", 1000, allow_private=True)
        assert False
    except ApiError as e:
        assert "404" in str(e) and "SECRET" not in str(e) and "127.0.0.1" in str(e)  # host only, never the URL
    try:
        da.fetch_bytes("http://tracker.example:99999/dl.torrent?passkey=SECRET", 1000)  # port out of range: parser error
        assert False
    except ApiError as e:
        assert "malformed URL" in str(e) and "SECRET" not in str(e)
    import time
    t0 = time.monotonic()
    try:
        da.fetch_bytes(base + "/503", 1000, allow_private=True)
        assert False
    except ApiError as e:
        assert "503" in str(e) and time.monotonic() - t0 < 1, (str(e), time.monotonic() - t0)  # no urllib3 retries, no Retry-After sleep
    port = srv.server_address[1]
    real_resolve = da.resolve_host
    calls, answers = [], {"rebind.test": (["127.0.0.1"], True)}  # DNS answers we control
    da.resolve_host = lambda host: (calls.append(host), answers[host.rstrip(".")])[1] if host.rstrip(".") in answers else real_resolve(host)
    try:  # the socket must go to the address that passed the check, with the original name in Host (and in TLS for https)
        assert da.fetch_bytes(f"http://rebind.test:{port}/host", 1000) == f"rebind.test:{port}".encode()
        assert calls == ["rebind.test"]  # looked up once: the address that was checked is the address that was used
        answers["rebind.test"] = (["127.0.0.2", "127.0.0.1"], True)  # the first vetted address refuses: the next one is tried
        assert da.fetch_bytes(f"http://rebind.test:{port}/host", 1000) == f"rebind.test:{port}".encode()
        assert da.fetch_bytes(f"http://user:p%40ss@rebind.test:{port}/auth", 1000) == b"Basic dXNlcjpwQHNz"  # URL userinfo -> Basic auth
        answers["rebind.test"] = (["127.0.0.1"], True)
        try:  # a feed URL is trusted as typed; where its server redirects is not
            da.fetch_bytes(f"http://rebind.test:{port}/to-private", 1000, allow_private=True)
            assert False
        except ApiError as e:
            assert "private or local" in str(e)
        answers["rebind.test"] = (["127.0.0.1"], False)
        try:
            da.fetch_bytes(f"http://rebind.test:{port}/host", 1000)
            assert False
        except ApiError as e:
            assert "private or local" in str(e)
        answers["rebind.test"] = (None, False)
        try:
            da.fetch_bytes(f"http://rebind.test:{port}/host", 1000)
            assert False
        except ApiError as e:
            assert "cannot resolve" in str(e)
        seen = {}

        class StubPool:  # https: the pool opens on the checked address and verifies the certificate for the name
            def __init__(self, host, port, **kw): seen.update(host=host, port=port, **kw)
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def close(self): pass
            def request(self, *a, **kw):
                raise da.urllib3.exceptions.SSLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired")
        real_pool, da.urllib3.HTTPSConnectionPool = da.urllib3.HTTPSConnectionPool, StubPool
        answers["rebind.test"] = (["127.0.0.1"], True)
        try:
            da.fetch_bytes("https://rebind.test./x?passkey=SECRET", 10)  # trailing-dot FQDN: the name is used without it
            assert False
        except ApiError as e:
            assert "SSLError" in str(e) and "certificate has expired" in str(e) and "SECRET" not in str(e) and "/x" not in str(e)
        finally:
            da.urllib3.HTTPSConnectionPool = real_pool
        assert seen["host"] == "127.0.0.1" and seen["port"] == 443 and seen["server_hostname"] == "rebind.test"
        assert seen["cert_reqs"] == "CERT_REQUIRED" and "assert_hostname" not in seen  # OpenSSL's own hostname check
    finally:
        da.resolve_host = real_resolve
    import time
    t0 = time.monotonic()
    try:
        da.fetch_bytes(base + "/drip", 1000, allow_private=True, deadline=1)  # the per-read timeout never fires on a drip
        assert False
    except ApiError as e:
        assert "within 1 s" in str(e) and time.monotonic() - t0 < 3, (str(e), time.monotonic() - t0)
    assert da.fetch_bytes(base + "/redir", 1000, allow_private=True) == b"d8:announce0:e"
    for path, text in (("/redir-bad", "unsupported"), ("/loop", "too many redirects")):
        try:
            da.fetch_bytes(base + path, 1000, allow_private=True)
            assert False, path
        except ApiError as e:
            assert text in str(e), (path, str(e))
    srv.shutdown()


def test_added_within():
    import debrid_applet as da
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    iso = lambda **delta: (now - timedelta(**delta)).isoformat()
    assert da.added_within(iso(days=3), "week", now) and not da.added_within(iso(days=8), "week", now)
    assert da.added_within(iso(hours=23), "day", now) and not da.added_within(iso(hours=25), "day", now)
    assert da.added_within(iso(days=29), "month", now) and not da.added_within(iso(days=31), "month", now)
    assert da.added_within(iso(days=400), "all", now)
    assert da.added_within("", "week", now) and da.added_within("junk", "week", now)  # unknown date: never hidden
    assert da.added_within("2026-10-11T11:00:00.000Z", "day", now)  # service formats: milliseconds and Z (Real-Debrid), Z (Torbox)
    assert not da.added_within("2026-10-03T11:00:00.000Z", "week", now) and not da.added_within("2026-10-03T11:00:00Z", "week", now)
    assert [k for k, _l in da.TIME_FILTERS] == ["all", "day", "week", "month"] and da.EMPTY_CONFIG["time_filter"] == "week"


RELEASE_JSON = {
    "tag_name": "v9.9.9", "html_url": "https://github.com/xb00tt/debrid-applet/releases/tag/v9.9.9",
    "assets": [
        {"name": "debrid-applet-9.9.9.tar.gz", "url": "https://api.github.com/repos/xb00tt/debrid-applet/releases/assets/1",
         "size": 1000, "digest": "sha256:" + "a" * 64},
        {"name": "debrid-applet_9.9.9_all.deb", "url": "https://api.github.com/repos/xb00tt/debrid-applet/releases/assets/2",
         "size": 2000, "digest": "sha256:" + "b" * 64},
        {"name": "debrid-applet_9.9.9_all.deb.sig", "url": "https://api.github.com/repos/xb00tt/debrid-applet/releases/assets/3",
         "size": 300, "digest": "sha256:" + "c" * 64},
        {"name": "debrid-applet-9.9.9.tar.gz.sig", "url": "https://api.github.com/repos/xb00tt/debrid-applet/releases/assets/4",
         "size": 300, "digest": "sha256:" + "d" * 64},
    ],
}


def test_update_parse_version_and_latest_release():
    import debrid_update as du
    assert du.parse_version("v1.2.3") == (1, 2, 3) and du.parse_version("1.2.3") == (1, 2, 3)
    assert du.parse_version("v1.2") is None and du.parse_version("v1.2.3-rc1") is None and du.parse_version("") is None
    assert du.parse_version("latest") is None and du.parse_version(None) is None
    s = FakeSession(FakeResp(200, RELEASE_JSON))
    rel = du.latest_release("1.0.3", s)
    assert rel.version == (9, 9, 9) and rel.tag == "v9.9.9" and rel.notes_url.endswith("/tag/v9.9.9")
    assert rel.deb.name == "debrid-applet_9.9.9_all.deb" and rel.deb.size == 2000 and rel.deb.digest == "sha256:" + "b" * 64
    assert rel.deb.url.endswith("/assets/2") and rel.tar.name == "debrid-applet-9.9.9.tar.gz"
    assert rel.deb_sig.name == "debrid-applet_9.9.9_all.deb.sig" and rel.tar_sig.name == "debrid-applet-9.9.9.tar.gz.sig"
    unsigned = du.latest_release("1.0.3", FakeSession(FakeResp(200, {**RELEASE_JSON, "assets": RELEASE_JSON["assets"][:2]})))
    assert unsigned.deb is not None and unsigned.deb_sig is None and unsigned.tar_sig is None
    method, url, kw = s.calls[0]
    assert (method, url) == ("GET", du.API) and kw["headers"]["Accept"] == "application/vnd.github+json"
    assert kw["headers"]["User-Agent"] == "debrid-applet/1.0.3" and kw["timeout"] == 15
    assert du.latest_release("9.9.9", FakeSession(FakeResp(200, RELEASE_JSON))) is None  # equal: nothing to do
    assert du.latest_release("10.0.0", FakeSession(FakeResp(200, RELEASE_JSON))) is None  # newer installed: never downgrade
    rel = du.latest_release("1.0.3", FakeSession(FakeResp(200, {**RELEASE_JSON, "assets": [RELEASE_JSON["assets"][0]]})))
    assert rel.deb is None and rel.tar is not None  # a release without a .deb is still reported
    for body, text in (({**RELEASE_JSON, "tag_name": "v1.0.4-rc1"}, "unexpected tag"), ({**RELEASE_JSON, "tag_name": "latest"}, "unexpected tag")):
        try:
            du.latest_release("1.0.3", FakeSession(FakeResp(200, body)))
            assert False, body["tag_name"]
        except ApiError as e:
            assert text in str(e)
    for status, text in ((403, "rate limit"), (429, "rate limit"), (500, "HTTP 500"), (404, "HTTP 404")):
        try:
            du.latest_release("1.0.3", FakeSession(FakeResp(status, {"message": "x"})))
            assert False, status
        except ApiError as e:
            assert text in str(e)
    try:
        du.latest_release("1.0.3", FakeSession(requests.ConnectionError("boom https://api.github.com/x")))
        assert False
    except ApiError as e:
        assert str(e) == "Update check failed: ConnectionError"  # type only, never the URL
    try:
        du.latest_release("1.0.3", FakeSession(FakeResp(200, None)))  # non-JSON body
        assert False
    except ApiError as e:
        assert "unexpected response" in str(e)


def test_update_verify_signature():
    import os, shutil, subprocess
    import debrid_update as du
    root = "/tmp/debrid-test-sign"
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    for name in ("good", "other"):  # throwaway ed25519 keys, like the release key but never leaving this test
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", os.path.join(root, name)], check=True)
    key = open(os.path.join(root, "good.pub")).read().strip()
    assert du.SIGNING_KEYS and all(k.startswith("ssh-ed25519 ") for k in du.SIGNING_KEYS)  # the project key is embedded
    path = os.path.join(root, "release.deb")
    open(path, "wb").write(b"deb-bytes")
    assert du.sign_namespace("9.9.9") == "debrid-applet-9.9.9"  # the signature names the version it belongs to
    subprocess.run(["ssh-keygen", "-Y", "sign", "-f", os.path.join(root, "good"), "-n", du.sign_namespace("9.9.9"), path], check=True, capture_output=True)
    du.verify_signature(path, path + ".sig", "9.9.9", keys=(key,))  # a good signature passes quietly
    du.verify_signature(path, path + ".sig", "9.9.9", keys=("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA old", key))  # any embedded key
    for label, fn, version in (("re-tagged as another version", lambda: None, "9.9.10"),  # a signed old asset cannot pose as a newer release
                               ("tampered file", lambda: open(path, "ab").write(b"!"), "9.9.9"),
                               ("another key", lambda: open(path, "wb").write(b"deb-bytes"), "9.9.9")):
        fn()
        keys = (open(os.path.join(root, "other.pub")).read().strip(),) if label == "another key" else (key,)
        try:
            du.verify_signature(path, path + ".sig", version, keys=keys)
            assert False, label
        except ApiError as e:
            assert "not signed by the project key" in str(e), (label, str(e))
    try:
        du.verify_signature(path, os.path.join(root, "missing.sig"), "9.9.9", keys=(key,))
        assert False
    except ApiError as e:
        assert "not signed by the project key" in str(e)
    try:
        du.verify_signature(path, path + ".sig", "9.9.9", keys=(key,), tool="/nonexistent/ssh-keygen")
        assert False
    except ApiError as e:
        assert "Cannot verify" in str(e)


def test_update_install_mode_and_deb_command():
    import os, shutil
    import debrid_update as du
    assert du.install_mode("/usr/share/debrid-applet") == "deb"
    root = "/tmp/debrid-test-mode"
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(os.path.join(root, "git", ".git"))
    os.makedirs(os.path.join(root, "user"))
    assert du.install_mode(os.path.join(root, "git")) == "git" and du.install_mode(os.path.join(root, "user")) == "user"
    os.makedirs(os.path.join(root, "wt"))
    open(os.path.join(root, "wt", ".git"), "w").write("gitdir: /somewhere/.git/worktrees/wt\n")  # a git worktree: .git is a file
    assert du.install_mode(os.path.join(root, "wt")) == "git"
    argv = du.deb_command("/home/u/.local/share/debrid-applet/updates/x.deb", "sha256:" + "c" * 64)
    assert argv[:6] == ["pkexec", "env", "DEBIAN_FRONTEND=noninteractive", "sh", "-c", du.DEB_SCRIPT]
    assert argv[6:] == ["sh", "/home/u/.local/share/debrid-applet/updates/x.deb", "c" * 64]
    assert "$1" in du.DEB_SCRIPT and "$2" in du.DEB_SCRIPT and "x.deb" not in du.DEB_SCRIPT  # parameters, not interpolation
    # the script itself, with apt-get faked: installs a root-side copy only when the copy's sha256 matches
    import hashlib, subprocess
    root = "/tmp/debrid-test-deb"
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(os.path.join(root, "bin"))
    fake_apt = os.path.join(root, "bin", "apt-get")
    open(fake_apt, "w").write('#!/bin/sh\necho "$@" > ' + os.path.join(root, "apt-args") + '\ncp "$4" ' + os.path.join(root, "installed") + '\n')
    os.chmod(fake_apt, 0o755)
    deb = os.path.join(root, "x.deb")
    open(deb, "wb").write(b"deb-bytes")
    env = {**os.environ, "PATH": os.path.join(root, "bin") + ":" + os.environ["PATH"]}
    run = lambda digest: subprocess.run(du.deb_command(deb, digest)[3:], env=env, capture_output=True, text=True)
    r = run("sha256:" + hashlib.sha256(b"deb-bytes").hexdigest())
    assert r.returncode == 0, r.stderr
    args = open(os.path.join(root, "apt-args")).read().split()
    assert args[:3] == ["install", "-y", "--no-install-recommends"] and args[3].endswith(".deb") and args[3] != deb  # a copy
    assert open(os.path.join(root, "installed"), "rb").read() == b"deb-bytes" and not os.path.exists(args[3])  # copy removed after
    os.remove(os.path.join(root, "apt-args"))
    r = run("sha256:" + "0" * 64)
    assert r.returncode != 0 and not os.path.exists(os.path.join(root, "apt-args"))  # wrong digest: apt-get never runs
    assert du.FILES == ("debrid_api.py", "debrid_local_api.py", "debrid_update.py", "debrid_applet.py", "debrid-applet.svg")


def test_update_download_verifies_digest():
    import hashlib, http.server, os, shutil, stat, threading
    import debrid_update as du
    root, dest = "/tmp/debrid-test-update-src", "/tmp/debrid-test-update-dest"
    for d in (root, dest):
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(root)
    payload = b"\x1f\x8b" + b"x" * 5000
    open(os.path.join(root, "good.deb"), "wb").write(payload)
    open(os.path.join(root, "huge.deb"), "wb").write(b"y" * 1_500_000)
    good = "sha256:" + hashlib.sha256(payload).hexdigest()

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            if self.path == "/redir":  # GitHub answers the API asset URL with a redirect to its CDN
                self.send_response(302); self.send_header("Location", "/good.deb"); self.end_headers(); return
            assert self.headers.get("Accept") == "application/octet-stream", self.headers.get("Accept")
            super().do_GET()
        def copyfile(self, source, outputfile):
            try:
                super().copyfile(source, outputfile)
            except (BrokenPipeError, ConnectionResetError):
                pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lambda *a, **k: Quiet(*a, directory=root, **k))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    asset = lambda name, path, size, digest: du.Asset(name=name, url=base + path, size=size, digest=digest)
    path = du.download(asset("good.deb", "/good.deb", len(payload), good), dest)
    assert path == os.path.join(dest, "good.deb") and open(path, "rb").read() == payload
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and stat.S_IMODE(os.stat(dest).st_mode) == 0o700
    assert not os.path.exists(path + ".part")
    assert du.download(asset("good.deb", "/redir", len(payload), good), dest) == path  # redirect followed, digest still checked
    try:
        du.download(asset("bad.deb", "/good.deb", len(payload), "sha256:" + "0" * 64), dest)
        assert False
    except ApiError as e:
        assert "checksum" in str(e) and not os.path.exists(os.path.join(dest, "bad.deb")) and not os.path.exists(os.path.join(dest, "bad.deb.part"))
    s = FakeSession()
    try:
        du.download(asset("nodigest.deb", "/good.deb", len(payload), ""), dest, s)  # refused before any request
        assert False
    except ApiError as e:
        assert "no checksum" in str(e) and s.calls == []
    try:
        du.download(asset("huge.deb", "/huge.deb", 100, "sha256:" + "0" * 64), dest)  # body far larger than declared
        assert False
    except ApiError as e:
        assert "larger" in str(e) and not os.path.exists(os.path.join(dest, "huge.deb.part"))
    try:
        du.download(asset("missing.deb", "/missing.deb", 10, good), dest)
        assert False
    except ApiError as e:
        assert str(e) == "Download failed: HTTPError" and not os.path.exists(os.path.join(dest, "missing.deb.part"))
    os.path.exists("/tmp/escaped.deb") and os.remove("/tmp/escaped.deb")
    path = du.download(asset("../../tmp/escaped.deb", "/good.deb", len(payload), good), dest)  # name from JSON: basename only
    assert path == os.path.join(dest, "escaped.deb") and not os.path.exists("/tmp/escaped.deb")
    try:
        du.download(asset("", "/good.deb", len(payload), good), dest)
        assert False
    except ApiError as e:
        assert "name" in str(e)
    srv.shutdown()


def test_deb_package():
    """packaging/build-deb.sh: the launcher imports the module with the working directory off the path (so the bytecode
    the postinst compiles is used and a stray module there cannot shadow ours), postinst compiles, prerm cleans up."""
    import shutil
    import subprocess
    import tempfile
    import debrid_applet as da
    import debrid_update as du
    env = {**os.environ, "DEBRID_APPLET_CONFIG_DIR": "/tmp/debrid-test-config", "DEBRID_APPLET_DATA_DIR": "/tmp/debrid-test-data"}
    cmd = ["python3", "-P", "-c", 'import sys; sys.path.insert(0, "."); import debrid_applet; debrid_applet.main()', "--version"]
    assert subprocess.run(cmd, capture_output=True, text=True, env=env).stdout.strip() == f"debrid-applet {da.VERSION}"  # the launcher's form of main()
    if not shutil.which("dpkg-deb"):
        print("  (no dpkg-deb: package contents not checked)")
        return
    deb = subprocess.run(["sh", "packaging/build-deb.sh"], capture_output=True, text=True, check=True).stdout.strip()
    assert deb.endswith(f"/dist/debrid-applet_{da.VERSION}_all.deb") and os.path.exists(deb)
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["dpkg-deb", "-R", deb, tmp], check=True, capture_output=True)
        launcher = open(os.path.join(tmp, "usr/bin/debrid-applet")).read()
        assert "exec python3 -P -c 'import sys; sys.path.insert(0, \"/usr/share/debrid-applet\"); import debrid_applet; debrid_applet.main()' \"$@\"" in launcher
        postinst = open(os.path.join(tmp, "DEBIAN/postinst")).read()
        assert "python3 -m compileall -q /usr/share/debrid-applet" in postinst and postinst.startswith("#!/bin/sh")
        assert "rm -rf /usr/share/debrid-applet/__pycache__" in open(os.path.join(tmp, "DEBIAN/prerm")).read()
        assert all(os.access(os.path.join(tmp, "DEBIAN", s), os.X_OK) for s in ("postinst", "prerm"))
        assert sorted(os.listdir(os.path.join(tmp, "usr/share/debrid-applet"))) == sorted(du.FILES)
        assert os.path.exists(os.path.join(tmp, "usr/share/metainfo", METAINFO))  # app stores read it from here


def test_update_install_user_from_tarball():
    import io, os, shutil, stat, tarfile
    import debrid_update as du
    root = "/tmp/debrid-test-update-user"
    shutil.rmtree(root, ignore_errors=True)
    here = os.path.join(root, "here")
    os.makedirs(here)
    for name in du.FILES:
        open(os.path.join(here, name), "w").write("old")

    def build(tar_path, names, extra=()):
        with tarfile.open(tar_path, "w:gz") as tar:
            for name in names:
                data = f"new {name}".encode()
                info = tarfile.TarInfo(f"debrid-applet-9.9.9/{name}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            for info in extra:
                tar.addfile(info, io.BytesIO(b"evil") if info.isfile() else None)
    good = os.path.join(root, "good.tar.gz")
    build(good, du.FILES + ("debrid_extra.py", "test_debrid_api.py", "docs/MANUAL.md", "packaging/build-deb.sh"))
    du.install_user(good, here)
    for name in du.FILES + ("debrid_extra.py",):  # a module added by the new version travels with it
        assert open(os.path.join(here, name)).read() == f"new {name}" and not os.path.exists(os.path.join(here, name + ".tmp"))
    assert sorted(os.listdir(here)) == sorted(du.FILES + ("debrid_extra.py",))  # tests, docs, packaging: not installed
    assert all(f'"$SRC/{n}"' in open("install.sh").read() for n in du.FILES)  # the per-user copy list ships the same files
    # a member that escapes the prefix and a symlink member are ignored: only the five names are ever read
    evil = tarfile.TarInfo("debrid-applet-9.9.9/../evil.py"); evil.size = 4
    link = tarfile.TarInfo("debrid-applet-9.9.9/debrid_api.py"); link.type = tarfile.SYMTYPE; link.linkname = "/etc/passwd"
    bad = os.path.join(root, "bad.tar.gz")
    build(bad, [n for n in du.FILES if n != "debrid_api.py"], extra=(evil, link))
    try:
        du.install_user(bad, here)
        assert False
    except ApiError as e:
        assert "debrid_api.py" in str(e)
    assert not os.path.exists(os.path.join(root, "evil.py")) and open(os.path.join(here, "debrid_api.py")).read() == "new debrid_api.py"
    os.makedirs(os.path.join(here, "debrid_update.py.tmp"))  # something in the way of the third file's staging write
    try:
        du.install_user(good, here)
        assert False
    except ApiError as e:
        assert "debrid_update.py" in str(e)
    assert all(open(os.path.join(here, n)).read() == f"new {n}" for n in du.FILES)  # all-or-nothing: nothing replaced
    assert not any(n.endswith(".tmp") and n != "debrid_update.py.tmp" for n in os.listdir(here))  # staged files cleaned up
    os.rmdir(os.path.join(here, "debrid_update.py.tmp"))
    ro = os.path.join(root, "ro")
    os.makedirs(ro)
    os.chmod(ro, 0o500)
    try:
        du.install_user(good, ro)
        assert False
    except ApiError as e:
        assert "Cannot write" in str(e)
    finally:
        os.chmod(ro, 0o700)
    assert os.listdir(ro) == []


def test_service_label():
    import debrid_applet as da
    assert [c.short for c, _k in da.CLIENTS] == ["TB", "RD", "AD"]
    assert da.service_label("Torbox", {}) == "TB" and da.service_label("Real-Debrid", {}) == "RD" and da.service_label("AllDebrid", {}) == "AD"
    assert da.service_label("Torbox", {"service_full_names": True}) == "Torbox"
    assert da.service_label("Premiumize", {}) == "Premiumize"  # a service without a short form keeps its name


def test_add_label():
    import debrid_applet
    assert debrid_applet.add_label("magnet:?xt=urn:btih:abc&dn=Some+Name+S01") == "Some Name S01"
    assert debrid_applet.add_label("magnet:?xt=urn:btih:abcdef").startswith("magnet:?xt=urn:btih:abcdef")
    assert debrid_applet.add_label("/tmp/x/thing.torrent") == "thing.torrent"
    assert debrid_applet.add_label("https://hoster/dir/file.bin") == "file.bin"
    assert debrid_applet.add_label("https://t/download.php?id=1&passkey=abc") == "download.php"  # query never shown
    assert debrid_applet.add_label("https://tracker.example/") == "tracker.example"
    assert len(debrid_applet.add_label("x" * 500)) == 60


def test_tray_choice_and_wayland():
    import debrid_applet
    c = debrid_applet.tray_choice
    assert c("native", True, True) == "xapp" and c("native", True, False) == "xapp"
    assert c("fallback", True, True) == "ayatana" and c("no-support", True, True) == "ayatana"
    assert c(None, False, True) == "ayatana"  # no XApp installed at all
    assert c("fallback", True, False) == "xapp-fallback"  # XEmbed: visible on KDE / XFCE / MATE under X11, nowhere else
    assert c("no-support", True, False) == "none" and c(None, False, False) == "none"
    # the DEBRID_APPLET_TRAY knob: forces a backend that exists, falls to none when it does not, ignores typos
    assert c("native", True, True, forced="ayatana") == "ayatana" and c("native", True, True, forced="none") == "none"
    assert c("fallback", True, True, forced="xapp") == "xapp"
    assert c("native", True, False, forced="ayatana") == "none" and c(None, False, True, forced="xapp") == "none"
    assert c("fallback", True, True, forced="ayatna") == "ayatana" and c("native", True, True, forced="") == "xapp"
    assert debrid_applet.is_wayland("GdkWaylandDisplay") and not debrid_applet.is_wayland("X11Display")


def test_tray_helpers():
    import debrid_applet as da
    assert da.has_bus_name("surely.nobody.owns.this.name") is False  # with or without a session bus
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        assert da.has_bus_name("org.freedesktop.DBus") is True
    assert da.try_xapp("") and da.try_xapp("xapp") and da.try_xapp("xap")  # a typo falls through to the normal chain
    assert not da.try_xapp("ayatana") and not da.try_xapp("none")


def test_tray_adapter_policy():
    """The adapter around tray_choice: XApp is tried unless the knob says otherwise, only 'native' decides before the
    timer, and Ayatana counts only while a StatusNotifier host owns the bus name. Stubs stand in for XApp (a display
    is needed for the real one); Ayatana is a sentinel that must never be touched."""
    import debrid_applet as da

    class State:
        def __init__(self, nick):
            self.value_nick = nick

    class FakeIcon:  # the XApp.StatusIcon surface Tray touches
        def __init__(self):
            self.visible, self.label = True, None

        def set_name(self, *_): pass
        def set_icon_name(self, *_): pass
        def set_tooltip_text(self, *_): pass
        def set_secondary_menu(self, *_): pass
        def connect(self, *_): pass
        def set_label(self, text): self.label = text
        def set_visible(self, flag): self.visible = flag
        def get_state(self): return State("fallback")

    class FakeXApp:
        StatusIcon = FakeIcon

    saved = da.XApp, da.Ayatana, da.has_bus_name, os.environ.get("DEBRID_APPLET_TRAY")
    decided = []
    try:
        da.XApp, da.Ayatana, da.has_bus_name = FakeXApp, None, lambda name: False
        os.environ["DEBRID_APPLET_TRAY"] = "xap"  # a typo must not switch XApp off
        tray = da.Tray(None, None, lambda *_: None, decided.append)
        assert tray.icon is not None and tray.kind is None
        tray._on_state(None, State("fallback"))  # the panel may still be loading at login: the timer decides, not this
        assert tray.kind is None and decided == []
        tray._on_state(None, State("native"))
        assert tray.kind == "xapp" and decided == ["xapp"] and tray.present()
        tray.set_count(3, 8)
        assert tray.icon.label == "3↓"

        os.environ["DEBRID_APPLET_TRAY"] = "ayatana"
        da.Ayatana = object()  # importable, but nobody owns org.kde.StatusNotifierWatcher: unusable
        tray = da.Tray(None, None, lambda *_: None, decided.append)
        assert tray.icon is None
        tray._decide(None)
        assert tray.kind == "none" and not tray.present()

        os.environ["DEBRID_APPLET_TRAY"] = ""
        tray = da.Tray(None, None, lambda *_: None, decided.append)
        tray._decide("fallback")  # timer: XApp on XEmbed, no host anywhere: keep that icon, but the window must show
        assert tray.kind == "xapp-fallback" and not tray.present() and tray.icon is not None

        da.has_bus_name = lambda name: name == da.SNI_WATCHER  # a host appears: Ayatana wins over XEmbed
        tray = da.Tray(None, None, lambda *_: None, decided.append)
        try:
            tray._decide("fallback")
        except AttributeError:  # the sentinel has no Indicator: reaching it is the proof the branch was taken
            pass
        assert tray.icon is None  # the XApp icon was dropped before Ayatana was built
    finally:
        da.XApp, da.Ayatana, da.has_bus_name = saved[:3]
        if saved[3] is None:
            os.environ.pop("DEBRID_APPLET_TRAY", None)
        else:
            os.environ["DEBRID_APPLET_TRAY"] = saved[3]


METAINFO = "io.github.xb00tt.debrid-applet.metainfo.xml"


def test_metainfo():
    """The AppStream file stays consistent with VERSION, the desktop file and the icon (see the 2026-10-13 app-id spec)."""
    import datetime
    import re
    import xml.etree.ElementTree as ET
    import debrid_applet as da
    root = ET.parse(METAINFO).getroot()
    assert root.tag == "component" and root.get("type") == "desktop-application"
    assert root.findtext("id") == da.DBUS_ID and METAINFO == da.DBUS_ID + ".metainfo.xml"
    launchable = root.find("launchable")
    assert launchable.get("type") == "desktop-id" and launchable.text == "debrid-applet.desktop"
    desktop = open("debrid-applet.desktop").read()
    assert f"\nIcon={da.APP_ID}\n" in desktop and os.path.exists(da.APP_ID + ".svg")
    assert root.findtext("metadata_license") and root.findtext("project_license") == "MIT"
    assert root.find("developer").get("id") and root.find("developer").findtext("name")
    assert root.find("content_rating").get("type") == "oars-1.1"
    releases = root.findall("releases/release")
    assert releases and releases[0].get("version") == da.VERSION
    # one day of slack: the release workflow runs in UTC, a tag made before 03:00 here carries tomorrow's UTC date
    latest = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    versions = [tuple(int(p) for p in r.get("version").split(".")) for r in releases]
    assert all(r.get("date") <= latest for r in releases), "a release is dated in the future"
    assert versions == sorted(versions, reverse=True) and len(set(versions)) == len(versions), "releases must be newest first"
    image = root.findtext("screenshots/screenshot/image")
    assert image.startswith("https://") and f"/v{da.VERSION}/" in image, image
    assert len(root.findtext("summary")) <= 35, "Flathub quality guideline: summary at most 35 characters"
    import struct
    with open("docs/screenshot.png", "rb") as f:  # the image the URL points at, once the tag exists
        assert f.read(8) == b"\x89PNG\r\n\x1a\n" and f.read(8)[4:] == b"IHDR"
        width, height = struct.unpack(">II", f.read(8))
    assert width <= 1000 and height <= 700, f"Flathub quality guideline: screenshot at most 1000x700, got {width}x{height}"
    assert not re.search(r"<developer_name>", open(METAINFO).read())  # deprecated since AppStream 1.0


def test_flatpak_helpers():
    import subprocess
    import debrid_applet as da
    import debrid_update as du
    assert du.install_mode("/app/share/debrid-applet", flatpak_info="/nonexistent") == "user"
    assert du.install_mode("/app/share/debrid-applet", flatpak_info="/etc/hostname") == "flatpak"  # any existing file stands in
    assert du.install_mode("/usr/share/debrid-applet", flatpak_info="/etc/hostname") == "flatpak"  # the sandbox wins over the path
    assert da.icon_name(False) == da.APP_ID and da.icon_name(True) == da.DBUS_ID  # the host resolves exported icons by app id
    assert da.host_cmd(["mpv", "--fs", "u"], in_flatpak=False) == ["mpv", "--fs", "u"]
    assert da.host_cmd(["mpv", "--fs", "u"], in_flatpak=True) == ["flatpak-spawn", "--host", "mpv", "--fs", "u"]
    assert da.host_which("sh", in_flatpak=False) == subprocess.run(["which", "sh"], capture_output=True, text=True).stdout.strip()
    calls = []

    def fake_run(argv, **kw):  # the host answers
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="/usr/bin/mpv\n", stderr="")
    da._host_which_cache.clear()
    assert da.host_which("mpv", in_flatpak=True, run=fake_run) == "/usr/bin/mpv"
    assert da.host_which("mpv", in_flatpak=True, run=fake_run) == "/usr/bin/mpv" and len(calls) == 1  # cached
    assert calls[0] == ["flatpak-spawn", "--host", "sh", "-c", 'command -v "$1"', "sh", "mpv"]  # `which` is not on every host

    def refused(argv, **kw):  # no exception granted: flatpak-spawn exits 1 (portal call failed)
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Portal call failed")
    da._host_which_cache.clear()
    assert da.host_which("mpv", in_flatpak=True, run=refused) is None

    def exploding(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 5)
    da._host_which_cache.clear()
    assert da.host_which("mpv", in_flatpak=True, run=exploding) is None
    da._host_which_cache.clear()
    assert da.download_tool(True) == "aria2c" and da.download_tool(False) == "wget"
    assert da.default_download_dir(False) == "~/Downloads"
    # inside the sandbox everything outside the granted dirs is the private tmpfs root: refuse before writing there
    class St:
        def __init__(self, dev): self.st_dev = dev
    devs = {"/": 67, "/home/u": 67, "/home/u/Downloads": 66306, "/home/u/Downloads/x": 66306}
    fake_stat = lambda path: St(devs[path]) if path in devs else (_ for _ in ()).throw(FileNotFoundError(path))
    assert da.sandbox_private("/home/u/Videos/new", stat=fake_stat)  # nearest existing parent is the tmpfs home
    assert not da.sandbox_private("/home/u/Downloads/x/new/deeper", stat=fake_stat)  # under the granted bind mount
    assert "flatpak override --user --talk-name=org.freedesktop.Flatpak io.github.xb00tt.debrid-applet" in da.HOST_DENIED
    assert "flatpak run io.github.xb00tt.debrid-applet --toggle" in da.toggle_hint(True)
    assert da.config_dir({}, False) == os.path.expanduser("~/.config/debrid-applet")
    assert da.config_dir({"XDG_CONFIG_HOME": "/x/cfg"}, False) == os.path.expanduser("~/.config/debrid-applet")  # deb: unchanged
    assert da.config_dir({"XDG_CONFIG_HOME": "/x/cfg"}, True) == "/x/cfg/debrid-applet"  # flatpak: ~/.var/app/<id>/config
    assert da.config_dir({}, True) == os.path.expanduser("~/.config/debrid-applet")
    assert da.data_dir({"XDG_DATA_HOME": "/x/data"}, True) == "/x/data/debrid-applet"
    assert da.data_dir({"XDG_DATA_HOME": "/x/data"}, False) == os.path.expanduser("~/.local/share/debrid-applet")
    assert "debrid-applet --toggle" in da.toggle_hint(False) and "flatpak" not in da.toggle_hint(False)


def test_host_launch_sites():
    src = open("debrid_applet.py").read()
    assert src.count("subprocess.Popen(host_cmd(") == 4, "players, playlist and custom command x2 go through host_cmd"
    assert "argv = host_cmd(argv)" in src, "the aria2c download goes through host_cmd; wget stays in the sandbox"
    for bare in ("subprocess.Popen(player_cmd(", "subprocess.Popen(playlist_cmd(", "subprocess.Popen(custom_cmd(",
                 "subprocess.Popen(download_command("):
        assert bare not in src, bare
    assert 'shutil.which("aria2c")' not in src and "download_tool(bool(host_which(" in src
    assert "Notify.Notification.new(title, body, ICON_NAME)" in src and "set_default_icon_name(ICON_NAME)" in src
    assert "logo_icon_name=ICON_NAME" in src and "Ayatana.Indicator.new(APP_ID, ICON_NAME" in src


def test_flatpak_degradation():
    import debrid_applet as da
    assert da.EMPTY_CONFIG["autostart_asked"] is False
    assert da.browser_fallback(None, "", True) and not da.browser_fallback(None, "", False)
    assert not da.browser_fallback("mpv", "", True) and not da.browser_fallback(None, "stremio {url}", True)
    assert da.AUTOSTART_FALLBACK == ("Autostart is not available through your desktop's portal",
                                     'Add "flatpak run io.github.xb00tt.debrid-applet" to your startup applications.')


def test_flatpak_manifest():
    import yaml
    import debrid_applet as da
    m = yaml.safe_load(open("packaging/io.github.xb00tt.debrid-applet.yml"))
    assert m["app-id"] == da.DBUS_ID and m["command"] == "debrid-applet"
    assert m["runtime"] == "org.freedesktop.Platform" and m["sdk"] == "org.freedesktop.Sdk"
    args = m["finish-args"]
    assert "--filesystem=home" not in args and "--filesystem=host" not in args
    assert not any(a.endswith(".*") for a in args), "wildcard talk-names are never granted"
    for a in ("--share=network", "--share=ipc", "--socket=wayland", "--socket=fallback-x11",
              "--talk-name=org.kde.StatusNotifierWatcher", "--talk-name=org.freedesktop.Notifications",
              "--talk-name=org.freedesktop.Flatpak", "--filesystem=xdg-download"):
        assert a in args, a
    assert not any("xdg-config" in a or "xdg-data" in a for a in args), "the sandbox's own config and data dirs are used"
    assert "--socket=x11" not in args
    app = next(mod for mod in m["modules"] if isinstance(mod, dict) and mod["name"] == "debrid-applet")
    src = app["sources"][0]
    assert src["type"] == "git" and src["tag"] == f"v{da.VERSION}"
    assert any(isinstance(mod, str) and mod.endswith("python3-requirements.json") for mod in m["modules"])
    assert os.path.exists("packaging/python3-requirements.json")
    cmds = "\n".join(app["build-commands"])
    assert f"Icon={da.DBUS_ID}" in cmds and f"/app/share/metainfo/{da.DBUS_ID}.metainfo.xml" in cmds


def test_invalid_key_evidence():
    import debrid_api as api
    import debrid_applet as da
    assert api.is_invalid_key("Invalid API key") and api.is_invalid_key("Invalid API key (HTTP 403 BAD_TOKEN)")
    assert not api.is_invalid_key("HTTP 403: <html>") and not api.is_invalid_key("Network error: x")
    assert api._auth_code({"error": "BAD_TOKEN"}) == "BAD_TOKEN" and api._auth_code({"error": {"code": "AUTH_X"}}) == "AUTH_X"
    assert api._auth_code({"error": None}) == "" and api._auth_code({}) == ""
    assert not da.retryable("Invalid API key (HTTP 401)")  # a bad key is never retried


def test_auth_confirmation_policy():
    import debrid_applet as da
    assert da.should_confirm_auth(None, 1000.0)  # first error: always check
    assert not da.should_confirm_auth(1000.0, 1100.0)  # inside the window: show the error, no second request
    assert da.should_confirm_auth(1000.0, 1000.0 + da.AUTH_CONFIRM_EVERY)
    assert da.should_confirm_auth(1000.0, 1010.0, every=5)
    assert da.auth_confirmed("Invalid API key (HTTP 403 BAD_TOKEN)")
    assert not da.auth_confirmed("Network error: timeout") and not da.auth_confirmed(None)


if __name__ == "__main__":
    import sys
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} passed")
