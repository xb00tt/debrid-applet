#!/usr/bin/env python3
"""Tray control panel for Torbox, Real-Debrid and AllDebrid."""
import collections
import ctypes
import functools
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
try:
    import defusedxml.ElementTree as ET  # python3-defusedxml: refuses entity tricks outright
except ImportError:  # a per-user install without the package: the stdlib parser plus the <!ENTITY guard in parse_feed
    import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import gi
import urllib3
from urllib3.util import parse_url  # the parser the fetch connects with, so the SSRF check sees the same host

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("Notify", "0.7")
gi.require_version("XApp", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Notify, Pango, XApp  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
from debrid_api import (DONE, DOWNLOADING, ERROR, EXPIRED, INVALID_KEY, PROCESSING, QUEUED, SEEDING, STALLED,  # noqa: E402
                        AllDebrid, ApiError, RealDebrid, Torbox, human_size, magnet_hash)
from debrid_local_api import LocalApi  # noqa: E402
from debrid_update import deb_command, download, install_mode, install_user, latest_release, verify_signature  # noqa: E402

VERSION = "1.0.7"
APP_ID = "debrid-applet"  # WM_CLASS, icon name and .desktop basename all match
DBUS_ID = "io.github.xb00tt.DebridApplet"  # single-instance guard
HOMEPAGE = "https://github.com/xb00tt/debrid-applet"
PLAYERS = ("mpv", "vlc", "celluloid", "smplayer", "totem")
SUB_FLAG = {"mpv": "--sub-file={}", "celluloid": "--mpv-sub-file={}", "smplayer": "-sub {}", "vlc": "--sub-file={}"}
RESUME_FLAG = {"mpv": "--save-position-on-quit", "celluloid": "--mpv-save-position-on-quit"}  # resume where you stopped
SUB_EXT = (".srt", ".ass", ".vtt", ".sub")
CLIENTS = ((Torbox, "torbox_key"), (RealDebrid, "realdebrid_key"), (AllDebrid, "alldebrid_key"))  # class, config key
SERVICES = tuple(c.name for c, _k in CLIENTS)
KEY_FIELDS = tuple(k for _c, k in CLIENTS)
SERVICE_SHORT = {c.name: c.short for c, _k in CLIENTS}  # Service column on the All tab, unless Settings asks for full names


def service_label(name, cfg):
    return name if cfg.get("service_full_names") else SERVICE_SHORT.get(name, name)
DASHBOARDS = {"Torbox": "https://torbox.app/dashboard", "Real-Debrid": "https://real-debrid.com/torrents",
              "AllDebrid": "https://alldebrid.com/magnets"}
SERVICE_COLORS = {"Torbox": "#e8761b", "Real-Debrid": "#3b82f6", "AllDebrid": "#a855f7"}  # Service column on the All tab
EXPIRY_WARN_DAYS = 7
VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".webm", ".mov", ".ts", ".wmv", ".flv")
GLib.set_prgname(APP_ID)

# Both directories can be redirected (tests, harnesses, a second profile) so nothing but the real applet touches them.
CONFIG_DIR = os.environ.get("DEBRID_APPLET_CONFIG_DIR") or os.path.expanduser("~/.config/debrid-applet")
CONFIG = os.path.join(CONFIG_DIR, "config.json")
DATA_DIR = os.environ.get("DEBRID_APPLET_DATA_DIR") or os.path.expanduser("~/.local/share/debrid-applet")  # logs
LOG_FILE = os.path.join(DATA_DIR, "activity.log")
LOG_MAX = 1_000_000  # default bytes; trimmed to the newest fifth when exceeded
LOG_LEVELS = (("off", "Off"), ("errors", "Errors only"), ("actions", "Actions and notifications"),
              ("verbose", "Verbose (also every poll)"))
LOG_SETTINGS = {"level": "actions", "max": LOG_MAX}  # mirrored from the config by App
EMPTY_CONFIG = {"torbox_key": "", "realdebrid_key": "", "alldebrid_key": "", "notify_added": True, "notify_done": True,
                "watch_clipboard": True, "columns": {}, "window": [],  # columns: per-service, see column_layout()
                "remember_filter": True, "filters": {},  # filters: last state-filter choice per service
                "time_filter": "week",  # default "added within" window of the toolbar dropdown; see TIME_FILTERS
                "dblclick_play": False,  # double-click plays videos instead of opening the file panel
                "dblclick_asked": False,  # the one-time offer to turn dblclick_play on has been shown
                "watch_dir": "", "watch_service": "Torbox",  # .torrent files dropped here get uploaded there
                "auto_download": False, "download_dir": "~/Downloads",  # finished items -> aria2c/wget into here
                "http_port": 0, "http_token": "",  # loopback API port (0 = off) and its required token
                "log_level": "actions", "log_max_kb": 1000,  # activity log verbosity and size cap
                "stop_seeding_when_done": False,  # Torbox: send stop_seeding as soon as an item finishes
                "auto_download_videos_only": False, "delete_after_download": False,  # auto-download rules
                "route_cached": False,  # Add box: send magnets Torbox already has to Torbox
                "feeds": "", "feed_interval_min": 15, "feed_seen": [],  # RSS: "url | regex | service" lines
                "player_command": "",  # custom Play target, e.g. "stremio {url}" or "kodi-send --action=PlayMedia({url})"
                "start_tab": "All",  # tab shown when the window opens
                "check_updates": True, "update_skip": "",  # daily GitHub release check; version the user chose to skip
                "service_full_names": False,  # All tab: "Torbox" instead of "TB" in the Service column
                "config_version": 2,  # bumped when the layout changes; see migrate_config()
                "notified_expiry": {}}  # service -> expiry date already warned about
POLL_FAST, POLL_SLOW = 8, 60  # seconds between polls while something is active / when everything is idle
POLL_FULL = 600  # idle cadence: timer polls ask for active items only and refetch the whole lists this often (see poll_kind)
try:  # glibc: pin M_MMAP_THRESHOLD, so a multi-megabyte service answer is mapped and unmapped instead of becoming a
    _LIBC = ctypes.CDLL("libc.so.6")  # hole in the heap once glibc's dynamic threshold has grown past it; see trim_memory
    _LIBC.mallopt(-3, 128 * 1024)  # (measured 20-25 MB of RSS; applies to every allocation, raise it if drawing gets slower)
except (OSError, AttributeError):  # not glibc
    _LIBC = None


def migrate_config(cfg):
    """Bring a raw config dict (as read from disk, before defaults) up to the current layout. Idempotent."""
    version = int(cfg.get("config_version") or 1)
    cols = cfg.get("columns") or {}
    if version < 2 and any(k in cols for k in ("order", "hidden", "sort")):  # one shared layout -> one per service
        flat = {k: cols.pop(k) for k in ("order", "hidden", "sort") if k in cols}
        for name in ("Torbox", "Real-Debrid"):  # the two services version 1 knew
            cols.setdefault(name, json.loads(json.dumps(flat)))
        cfg["columns"] = cols
    cfg.pop("keys_in_keyring", None)  # option removed
    cfg["config_version"] = EMPTY_CONFIG["config_version"]
    return cfg


def load_config():
    """config.json with defaults filled in. A file that is not a usable JSON object is moved to config.json.bad,
    so the next save cannot overwrite what may still hold the keys."""
    try:
        with open(CONFIG) as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("config is not a JSON object")
        return {**EMPTY_CONFIG, **migrate_config(raw)}
    except OSError:
        return dict(EMPTY_CONFIG)
    except (ValueError, TypeError):
        try:
            os.replace(CONFIG, CONFIG + ".bad")
            log_event("", f"{CONFIG} is not valid JSON; moved to config.json.bad, starting with defaults", "error")
        except OSError:
            pass
        return dict(EMPTY_CONFIG)


def save_config(cfg):
    """Write config.json with mode 600 in a 700 directory, atomically: temp file then rename, so a crash mid-write
    keeps the previous file. When an API key that is on disk would vanish (a cleared field, a stray script saving
    defaults), the old file is kept as config.json.bak first. Keys are kept here and nowhere else."""
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    tmp = CONFIG + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    try:
        with open(CONFIG) as f:
            old = json.load(f)
        if any(old.get(k) and not cfg.get(k) for k in KEY_FIELDS):
            os.replace(CONFIG, CONFIG + ".bak")
    except (OSError, ValueError, AttributeError):  # no previous file, or not a JSON object
        pass
    os.replace(tmp, CONFIG)


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


def is_http(url):
    """True for an http(s) URL string without whitespace: the only kind of link that may reach a player, aria2c/wget,
    xdg-open or a fetch (a URL never holds whitespace; one that does could smuggle an option line into aria2c's input)."""
    return isinstance(url, str) and url.lower().startswith(("http://", "https://")) and not any(c.isspace() for c in url)


def add_label(text):
    """Short human name for a just-added magnet, file path or link (for notifications and the log)."""
    if text.lower().startswith("magnet:"):
        dn = parse_qs(urlparse(text).query).get("dn")
        if dn:
            return dn[0][:60]
    if is_http(text):
        p = urlparse(text)
        text = p.path.rstrip("/") or p.netloc  # never the query: tracker passkeys live there
    return (os.path.basename(text.rstrip("/")) or text)[:60]


def open_url(url):
    """Hand an http(s) URL to the browser. Anything else (file:, javascript:, flags) is refused."""
    if not is_http(url):
        return False
    subprocess.Popen(["xdg-open", url])
    return True


def add_one(client, item):
    """Route one magnet, http(s) link or .torrent path to the client. Returns a direct URL if given one."""
    low = item.lower()
    if low.startswith("magnet:"):
        client.add_magnet(item)
    elif is_http(item) and low.split("?")[0].endswith(".torrent"):
        data = fetch_bytes(item, TORRENT_MAX)  # a .torrent URL: fetch it (public hosts only), upload the file
        with tempfile.NamedTemporaryFile(suffix=".torrent", delete=False) as f:
            f.write(data)
        try:
            client.add_torrent_file(f.name)
        finally:
            os.unlink(f.name)
    elif is_http(item):
        return client.add_link(item)
    elif low.endswith(".torrent"):
        client.add_torrent_file(item)
    else:
        raise ApiError("not a magnet, link or .torrent")
    return None


def find_player():
    return next((p for p in PLAYERS if shutil.which(p)), None)


def usable_links(links):
    """Entries of a links() result that have a URL, minus the zip bundle."""
    return [l for l in links if l["url"] and not l["name"].startswith("All files")]


def video_links(links, strict=False):
    """Playable entries: the video files; unless strict, any usable file when there is no video."""
    usable = usable_links(links)
    vids = [l for l in usable if l["name"].lower().endswith(VIDEO_EXT)]
    return vids if strict else vids or usable


def days_until(date_str):
    """Days from today to a YYYY-MM-DD date; None when empty or unparsable."""
    try:
        return (date.fromisoformat((date_str or "")[:10]) - date.today()).days
    except ValueError:
        return None


def pick_subtitle(video_name, links):
    """URL of the subtitle that matches the video's name; else the only subtitle; else None."""
    subs = [l for l in links if is_http(l.get("url")) and l["name"].lower().endswith(SUB_EXT)]  # else a bare {sub} could pass a flag
    stem = os.path.splitext(os.path.basename(video_name))[0].lower()
    for s in subs:
        if os.path.splitext(os.path.basename(s["name"]))[0].lower().startswith(stem):
            return s["url"]
    return subs[0]["url"] if len(subs) == 1 else None


def player_cmd(player, url, sub_url=None):
    cmd = [player] + ([RESUME_FLAG[player]] if player in RESUME_FLAG else [])
    if sub_url and player in SUB_FLAG:
        cmd += SUB_FLAG[player].format(sub_url).split(" ", 1) if player == "smplayer" else [SUB_FLAG[player].format(sub_url)]
    return cmd + [url]


def custom_cmd(template, url, sub_url=None):
    """argv from a user template: {url} and {sub} are substituted; a word that would hold an empty {sub} is
    dropped; a template without {url} gets the URL appended."""
    words = shlex.split(template)
    if not any("{url}" in w for w in words):
        words.append("{url}")
    out = []
    for w in words:
        if "{sub}" in w:
            if not sub_url:
                continue
            w = w.replace("{sub}", sub_url)
        out.append(w.replace("{url}", url))
    return out


def playlist_cmd(player, urls):
    """Queue several videos in one player instance (mpv, Celluloid, VLC and SMPlayer all take a list)."""
    return [player] + ([RESUME_FLAG[player]] if player in RESUME_FLAG else []) + list(urls)


class MergedClient:
    """The 'All' tab: every configured service's cached list, ids prefixed 'Service|id', actions routed back."""
    name = "All"

    def __init__(self, tabs):
        self._tabs = tabs  # callable returning the real service tabs
        self._copies = {}  # id(source dict) -> (source dict, its copy): light polls keep most source dicts, so most copies survive

    def _real(self):
        return [t for t in self._tabs() if t.client is not None and not isinstance(t.client, MergedClient)]

    def _route(self, tid):
        service, _, real_id = str(tid).partition("|")
        tab = next((t for t in self._real() if t.name == service), None)
        if tab is None:
            raise ApiError(f"{service}: not configured")
        return tab.client, real_id

    def account(self):
        tabs = self._real()
        return {"plan": "all services", "expires": "", "extra": f"{len(tabs)} configured: " + ", ".join(t.name for t in tabs)}

    def torrents(self, active_only=False, ids=()):
        out, copies = [], {}
        for tab in self._real():
            for t in tab.torrents:
                if active_only and t["done"]:
                    continue
                src, copy = self._copies.get(id(t), (None, None))
                if src is not t:  # a new source dict (the cache holds the old one, so its id cannot have been reused)
                    copy = {**t, "id": f"{tab.name}|{t['id']}", "service": tab.name}
                copies[id(t)] = (t, copy)
                out.append(copy)
        self._copies = copies
        return out

    def delete(self, tid, kind="torrent"):
        client, real_id = self._route(tid)
        client.delete(real_id, kind)

    def links(self, tid, kind="torrent"):
        client, real_id = self._route(tid)
        return client.links(real_id, kind)

    def _first(self):
        tabs = self._real()
        if not tabs:
            raise ApiError("No service configured")
        return tabs[0].client

    def add_magnet(self, magnet):
        self._first().add_magnet(magnet)

    def add_torrent_file(self, path):
        self._first().add_torrent_file(path)

    def add_link(self, url):
        return self._first().add_link(url)


def download_command(urls, dest, tool, via_stdin=False):
    """argv for a resumable bulk download into dest with aria2c (one file per URL, 8 connections each) or wget;
    [] without URLs. Only http(s) URLs are passed on: anything else from a service could be read as a flag.
    via_stdin: the tool reads the URLs from stdin (one per line) so they never show in ps; the caller feeds them."""
    urls = [u for u in urls if is_http(u)]
    if not urls:
        return []
    tail = ["-i", "-"] if via_stdin else urls
    if tool == "aria2c":
        return ["aria2c", "-c", "-Z", "-x", "8", "-s", "8", f"--dir={dest}", *tail]
    return ["wget", "-c", "-P", dest, *tail]


def safe_dirname(name):
    return re.sub(r"[^\w.\- \[\]()]", "_", name).strip(" .")[:120] or "download"  # "." / ".." would escape the folder


def plan_color(plan):
    p = (plan or "").lower()
    if p == "trial":
        return PALETTE["warn"]
    if p == "free":
        return PALETTE["error"]
    return PALETTE["done"]


def expiry_text(expires):
    days = days_until(expires)
    if days is None:
        return "no expiry"
    if days < 0:
        return f"expired {expires}"
    when = "today" if days == 0 else f"in {days} day{'s' if days != 1 else ''}"
    return f"expires {expires} ({when})"


def totals_text(torrents, shown, ticked):
    if not torrents:
        return "0 items"
    down = sum(t["speed"] or 0 for t in torrents)
    up = sum(t["up_speed"] or 0 for t in torrents)
    active = sum(1 for t in torrents if is_active(t))
    size = sum(t["size"] or 0 for t in torrents)
    parts = [f"⬇ {human_size(down)}/s", f"⬆ {human_size(up)}/s", f"{active} active", f"{len(torrents)} items", human_size(size)]
    if shown != len(torrents):
        parts.append(f"showing {shown}")
    if ticked:
        parts.append(f"{ticked} ticked")
    return " · ".join(parts)


def parse_cli(args):
    """--toggle, --log, --service NAME, --add ITEM [ITEM…]."""
    out = {"toggle": False, "log": False, "service": "", "items": []}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--toggle":
            out["toggle"] = True
        elif a == "--log":
            out["log"] = True
        elif a == "--service" and i + 1 < len(args):
            i += 1
            out["service"] = args[i]
        elif a == "--add":
            while i + 1 < len(args) and not args[i + 1].startswith("--"):
                i += 1
                out["items"].append(args[i])
        i += 1
    return out


def write_magnets(path, torrents):
    """One 'magnet<TAB>name' line per item that has an info hash."""
    with open(path, "w", encoding="utf-8") as f:
        for t in torrents:
            if t.get("hash"):
                f.write(f"{magnet_for(t['hash'], t['name'])}\t{t['name'].replace(chr(9), ' ')}\n")


def read_magnets(path):
    """First tab-separated field of every non-empty, non-comment line: magnets or links."""
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line.split("\t", 1)[0].strip())
    return out


TORRENT_MAX = 5_000_000  # bytes; .torrent files are small, feeds too
FEED_MAX = 5_000_000


def resolve_host(host):
    """(addresses, public): every address of host in the resolver's order of preference, and whether all of them are
    public. (None, False) for an empty or unresolvable name. The fetch connects to these very addresses, so the answer
    that was checked is the answer that is used: a name that changes its mind (DNS rebinding) gains nothing."""
    if not host:
        return None, False
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None, False
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    if not addresses:
        return None, False
    public = all(ip.is_global and not ip.is_multicast and not ip.is_reserved  # is_global also excludes 100.64/10 (CGNAT)
                 for ip in map(ipaddress.ip_address, addresses))
    return addresses, public


def is_public_host(host):
    """False for loopback, private, link-local and unresolvable hosts: targets a fetch must not reach."""
    return resolve_host(host)[1]


def fetch_bytes(url, max_bytes, allow_private=False, deadline=90):
    """_fetch_bytes on its own daemon thread with a total deadline: the per-read timeout never fires on a server that
    trickles bytes, so after deadline seconds the caller gets an ApiError and the stuck thread is left to die with
    its connection (ponytail: one leaked thread per timed-out fetch; it ends when the server closes, a read stalls
    past 20 s, or max_bytes is exceeded)."""
    out = {}

    def work():
        try:
            out["data"] = _fetch_bytes(url, max_bytes, allow_private)
        except Exception as e:  # re-raised on the caller's thread
            out["err"] = e
    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(deadline)
    if t.is_alive():
        try:
            host = (parse_url(url).host or "").strip("[]")
        except ValueError:
            host = ""
        raise ApiError(f"{host}: no complete answer within {deadline:g} s")
    if "err" in out:
        raise out["err"]
    return out["data"]


def _fetch_bytes(url, max_bytes, allow_private=False):
    """GET a URL with a hard size cap. Redirects are followed by hand so every hop gets the same checks:
    http(s) only, and no private/loopback hosts unless the caller trusts the URL (feeds typed into Settings).
    The host is taken from urllib3's parser, the one the fetch connects with, so the two cannot disagree
    (urlparse reads 'http://127.0.0.1:9\\@public/x' as public, urllib3 connects to 127.0.0.1). The socket goes to the
    address resolve_host() checked, with the name kept for the Host header and the TLS certificate, so a name that
    answers differently on the second lookup (DNS rebinding) cannot redirect the fetch. Errors name the host only:
    tracker URLs carry passkeys. One attempt per address, no retries, no Retry-After sleeps; proxy environment
    variables do not apply here (pinning an address and a proxy exclude each other)."""
    host = ""
    try:
        for _hop in range(5):
            parts = parse_url(url)
            host = (parts.host or "").strip("[]")
            if parts.scheme not in ("http", "https"):
                raise ApiError(f"unsupported URL scheme {parts.scheme!r} at {host or 'unknown host'}")
            addresses, public = resolve_host(host)
            if addresses is None:
                raise ApiError(f"fetch failed: cannot resolve {host or 'the host'}")
            if not allow_private and not public:
                raise ApiError("refusing to fetch from a private or local address")
            allow_private = allow_private and not public  # a typed URL is trusted as typed; a public server's redirects are not
            https = parts.scheme == "https"
            port = parts.port or (443 if https else 80)
            name = f"[{host}]" if ":" in host else host  # Host and TLS carry the name; the socket gets the address
            headers = {"Host": name if port == (443 if https else 80) else f"{name}:{port}", "User-Agent": "debrid-applet"}
            if parts.auth:  # user:pass@host in a typed feed URL
                headers.update(urllib3.util.make_headers(basic_auth=unquote(parts.auth)))
            pool_kw = {"server_hostname": host.rstrip("."), "cert_reqs": "CERT_REQUIRED"} if https else {}
            path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            for i, address in enumerate(addresses):  # all vetted: on a refused connect, the next one
                pool = (urllib3.HTTPSConnectionPool if https else urllib3.HTTPConnectionPool)(address, port, timeout=20, **pool_kw)
                try:
                    r = pool.request("GET", path, headers=headers, redirect=False, retries=False, preload_content=False)
                    break
                except urllib3.exceptions.ConnectTimeoutError:  # NewConnectionError is one of these
                    pool.close()
                    if i == len(addresses) - 1:
                        raise
            try:
                if 300 <= r.status < 400 and r.headers.get("Location"):
                    url = urljoin(url, r.headers["Location"])
                    continue
                if r.status >= 400:
                    raise ApiError(f"fetch failed: HTTP {r.status} at {host}")
                data = bytearray()
                while chunk := r.read(65536):  # read(), not stream(): http.client caps a chunk-size line, stream() does not
                    data += chunk
                    if len(data) > max_bytes:
                        raise ApiError(f"{host}: file larger than {max_bytes // 1_000_000} MB")
                return bytes(data)
            finally:
                r.close()
                pool.close()
        raise ApiError("too many redirects")
    except ValueError as e:  # urllib3 could not parse the URL; its text repeats it whole
        raise ApiError(f"fetch failed: malformed URL ({host or 'no host'})") from e
    except (urllib3.exceptions.HTTPError, OSError) as e:  # connection, TLS, timeout: the type, plus OpenSSL's reason for TLS
        detail = f" ({e})" if isinstance(e, urllib3.exceptions.SSLError) else ""
        raise ApiError(f"fetch failed: {type(e).__name__} at {host}{detail}") from e


def parse_feed(xml_text):
    """RSS/Atom items -> (guid, title, link) where link is a magnet, a .torrent URL or the item link."""
    if "<!ENTITY" in xml_text:  # feeds need no DTD, and entity declarations are the only expansion primitive
        return []
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, ValueError):  # ValueError: defusedxml's refusals
        return []
    out = []
    for item in root.iter():
        if item.tag.split("}")[-1] not in ("item", "entry"):
            continue
        get = lambda name: next((c.text or "" for c in item if c.tag.split("}")[-1] == name), "").strip()
        title, link, guid = get("title"), get("link"), get("guid") or get("id")
        for c in item:
            if c.tag.split("}")[-1] == "enclosure" and c.get("url"):
                link = c.get("url").strip()
            elif c.tag.split("}")[-1] == "link" and c.get("href") and not link:
                link = c.get("href").strip()
        if link:
            out.append((guid or link, title, link))
    return out


def parse_feed_lines(text):
    """'url | regex | service' per line; regex and service optional; '#' comments."""
    feeds = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if is_http(parts[0]):
            feeds.append((parts[0], parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else ""))
    return feeds


def feed_matches(title, pattern):
    if not pattern:
        return True
    try:
        return re.search(pattern, title, re.I) is not None
    except re.error:
        return False


def retryable(message):
    return message.startswith(("Rate limited", "Network error"))


def route_items(items, checker):
    """Split items into {True: cached on the checker's service, False: the rest}. checker needs check_cached()."""
    hashes = {i: magnet_hash(i) for i in items if i.lower().startswith("magnet:")}
    cached = set()
    if checker is not None and any(hashes.values()):
        try:
            cached = checker.check_cached([h for h in hashes.values() if h])
        except ApiError:
            cached = set()
    return {True: [i for i in items if hashes.get(i) and hashes[i] in cached],
            False: [i for i in items if not (hashes.get(i) and hashes[i] in cached)]}


def poll_tabs(tabs, fetch_one):
    """fetch_one(tab) for every tab at the same time, one daemon thread each, so a slow service does not delay the
    others; results in tab order as (tab, account, torrents, error)."""
    results = [None] * len(tabs)

    def work(i, tab):
        try:
            results[i] = (tab, *fetch_one(tab), None)
        except ApiError as e:
            results[i] = (tab, None, None, str(e))
        except Exception as e:
            results[i] = (tab, None, None, f"{type(e).__name__}: {e}")
    threads = [threading.Thread(target=work, args=(i, t), daemon=True) for i, t in enumerate(tabs)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    return results


def poll_kind(cached, age, fast, scheduled):
    """What one poll asks a service for. 'full': the account and the whole list. 'light': the active items only
    (torrents(active_only=True)), merged into the cached list by merge_active, plus the service's cheap check for
    anything added or deleted elsewhere (stale). cached: a full list is in hand, age seconds old. Only a timer poll
    (scheduled) is light, and only on a cached list younger than POLL_SLOW on the fast cadence (progress every tick,
    the whole list once a minute, unless the client's light poll is complete: see _Client.LIGHT_COMPLETE) or
    POLL_FULL on the idle one; whatever the user triggers is full. The lists are what costs: one poll of a
    10 000-item library parses megabytes of JSON."""
    return "light" if cached and scheduled and age < (POLL_SLOW if fast else POLL_FULL) else "full"


def can_change(t):
    """An unfinished item that may still move on its own: active, stalled, or finished but not yet available (Torbox
    moves files after the download). Expired and failed items only change by hand. These are what a light poll asks
    a service without an active-only query to refetch."""
    return not t["done"] and label_of(t["state"]) not in (EXPIRED, ERROR)


def poll_one(tab, now, fast, scheduled):
    """One service's share of a poll, on a worker thread: (account, torrents) for the tab to render. A light poll
    (poll_kind) merges the service's active items into the cached list and keeps it, unless an active item left the
    set (need_full), the service reports a change elsewhere (stale) or it has no light query (None): then, as for a
    full poll, the account and the whole list."""
    client = tab.client
    cached = tab._account is not None and bool(tab.torrents)
    if poll_kind(cached, now - tab._last_full, fast and not client.LIGHT_COMPLETE, scheduled) == "light":
        active = client.torrents(active_only=True, ids=[(t["id"], t["kind"]) for t in tab.torrents if can_change(t)])
        if active is not None:
            merged, need_full = merge_active(tab.torrents, active)
            if not need_full and not client.stale(merged):
                return tab._account, merged
            log_event(tab.name, f"Light poll: {'an active item left' if need_full else 'the list changed elsewhere'}; fetching everything", "verbose")
    account, torrents = client.account(), client.torrents()
    tab._last_full = now
    return account, torrents


def trim_memory():
    """Hand freed heap back to the kernel after a poll: service lists come and go in multi-megabyte bursts that glibc
    would otherwise keep as resident free space. No-op without glibc."""
    if _LIBC is not None:
        _LIBC.malloc_trim(0)


def merge_active(full, active):
    """Overlay an active-only poll onto the last full list. Returns (merged, need_full): need_full is True when
    something that was active is no longer in the active set (it finished or failed), so a full poll is due."""
    by_id = {t["id"]: t for t in active}
    merged = [by_id.pop(t["id"], t) for t in full]
    merged = list(by_id.values()) + merged  # brand-new active items go first
    was_active = {t["id"] for t in full if is_active(t)}
    now_active = {t["id"] for t in active}
    return merged, bool(was_active - now_active)


def magnet_for(hash_, name):
    return f"magnet:?xt=urn:btih:{hash_}&dn={quote(name)}" if hash_ else ""


def drop_items(uris, text):
    """Items from a drag-and-drop: file URIs become paths, other URIs stay, otherwise one item per text line."""
    if uris:
        out = []
        for u in uris:
            if u.startswith("file:"):
                try:
                    out.append(GLib.filename_from_uri(u)[0])
                except GLib.Error:
                    pass
            else:
                out.append(u)
        return out
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def aria2_command(urls, dest):
    """download_command(..., "aria2c") as one shell line for the clipboard; '' without URLs."""
    return shlex.join(download_command(urls, dest, "aria2c"))


def icon_button(icon, tip, cb):
    btn = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
    btn.set_relief(Gtk.ReliefStyle.NONE)
    btn.set_tooltip_text(tip)
    btn.connect("clicked", cb)
    return btn


def text_button(icon, label, cb):
    btn = Gtk.Button(label=label, image=Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON),
                     always_show_image=True)
    btn.get_image().set_margin_end(6)  # gap between icon and text
    btn.connect("clicked", cb)
    return btn


def should_log(level, setting):
    rank = {"off": 0, "errors": 1, "actions": 2, "verbose": 3}
    return {"error": 1, "action": 2, "verbose": 3}[level] <= rank.get(setting, 2)


def append_log(path, service, message, max_bytes=LOG_MAX):
    """One human-readable line per event; the file is trimmed to its newest fifth when it grows past max_bytes.
    Mode 600: the log holds item names, feed hosts and error text."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    try:
        if os.path.getsize(path) > max_bytes:
            with open(path, "rb") as f:
                f.seek(-max(1, max_bytes // 5), os.SEEK_END)
                tail = f.read().split(b"\n", 1)[-1]
            with open(path, "wb") as f:
                f.write(tail)
        os.chmod(path, 0o600)  # files made by older versions
    except OSError:
        pass
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {service:<12} {message}\n")


def read_log_tail(path, lines=500):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(collections.deque(f, maxlen=lines))
    except OSError:
        return ""


def log_event(service, message, level="action"):
    if not should_log(level, LOG_SETTINGS["level"]):
        return
    try:
        append_log(LOG_FILE, service, message, LOG_SETTINGS["max"])
    except OSError:
        pass


_NOTES = []  # notifications with buttons must stay referenced until clicked or closed


def notify(title, body, actions=()):
    """Desktop notification; actions = [(label, callback)] become buttons."""
    log_event("", f"{title}: {body}")
    note = Notify.Notification.new(title, body, APP_ID)
    for label, cb in actions:
        note.add_action(label.lower().replace(" ", "-"), label, lambda _n, _a, *_, cb=cb: cb())
    try:
        note.show()
    except GLib.Error as e:  # no notification daemon on this session; the log line above has the text
        log_event("", f"notification failed: {e}", "error")
    if actions:
        _NOTES.append(note)
        del _NOTES[:-20]
    return note


@functools.lru_cache(maxsize=8192)
def local_time(iso):
    """'2026-10-10T19:52:35.000Z' (UTC) -> '2026-10-10 21:52' in the local zone; '' when absent or odd."""
    dt = _parse_iso(iso) if iso else None
    return dt.astimezone().strftime("%Y-%m-%d %H:%M") if dt else ""


def _parse_iso(iso):
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'} ago"


def relative_time(iso, now=None):
    """'2 minutes ago', '12 hours ago', 'yesterday', '3 days ago', '2 weeks ago', '2 months ago', '2 years ago'."""
    dt = _parse_iso(iso) if iso else None
    if dt is None:
        return ""
    now = now or datetime.now(timezone.utc)
    s = max(0, (now - dt).total_seconds())
    if s < 45:
        return "just now"
    if s < 3600:
        return _plural(max(1, round(s / 60)), "minute")
    hours = max(1, round(s / 3600))
    if hours < 24:
        return _plural(hours, "hour")
    days = (now.astimezone().date() - dt.astimezone().date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return _plural(days, "day")
    if days < 30:
        return _plural(days // 7, "week")
    if days < 365:
        return _plural(days // 30, "month")
    return _plural(days // 365, "year")


def fmt_speed(n):
    return f"{human_size(n)}/s" if n else ""


def na(value, fmt="{}"):
    return "" if value is None else fmt.format(value)


def red(text):
    return f'<span foreground="red">{GLib.markup_escape_text(text)}</span>'


# key, title, display column, kind, sort column (raw numbers live in hidden store columns)
COLUMNS = (
    ("check", "", 6, "toggle", None),
    ("service", "Service", 23, "text", 23),
    ("name", "Name", 1, "text", 1),
    ("dl", "Down", 7, "text", 15),
    ("ul", "Up", 8, "text", 16),
    ("progress", "Progress", 2, "progress", 2),
    ("seeds", "Seeds", 9, "text", 17),
    ("peers", "Peers", 10, "text", 18),
    ("ratio", "Ratio", 11, "text", 19),
    ("state", "State", 3, "text", 3),
    ("size", "Size", 4, "text", 14),
    ("added", "Added", 12, "text", 20),
    ("finished", "Finished", 13, "text", 21),
)
TOOLTIP_COLUMNS = {"added": 20, "finished": 21, "state": 24}  # store columns shown on hover: full local time, raw state
STORE_TYPES = (str, str, int, str, str, str, bool,  # 0 id, 1 name, 2 progress, 3 state, 4 size, 5 kind, 6 checked
               str, str, str, str, str, str, str,   # 7 dl, 8 ul, 9 seeds, 10 peers, 11 ratio, 12 added, 13 finished
               float, float, float, int, int, float,  # 14 size, 15 dl, 16 ul, 17 seeds, 18 peers, 19 ratio (raw)
               str, str, str, str,  # 20 added, 21 finished (absolute local time), 22 hash, 23 service
               str)  # 24 the service's own state word, for the tooltip
CHECKED, STATUS_HOLD = 6, 8  # store column of the checkbox; seconds an action message stays before polls overwrite it
NUMERIC = ("dl", "ul", "seeds", "peers", "ratio", "size")  # right-aligned
WIDTHS = {"check": 28, "service": 95, "name": 120, "dl": 85, "ul": 85, "progress": 90, "seeds": 60, "peers": 60, "ratio": 60,
          "state": 140, "size": 85, "added": 115, "finished": 115}  # starting widths; Name grows to fill
PALETTE = {"active": "#3b82f6", "done": "#22c55e", "seed": "#14b8a6", "warn": "#f59e0b", "error": "#ef4444"}
CONTROL_LAG_NOTE = ("Torbox confirms at once but applies it on its own schedule; "
                    "the State column can keep its old value for a long while.")
TIME_FILTERS = (("all", "Any time"), ("day", "Last 24 hours"), ("week", "Last 7 days"), ("month", "Last 30 days"))
TIME_WINDOW = {"day": 1, "week": 7, "month": 30}  # days


@functools.lru_cache(maxsize=4)
def added_cutoff(window, now):
    """The oldest 'added' timestamp, as ISO text, that the window ("day", "week", "month") still shows; None for any
    other window. Cached: _fill asks once per row with the same now."""
    return (now - timedelta(days=TIME_WINDOW[window])).strftime("%Y-%m-%dT%H:%M:%S") if window in TIME_WINDOW else None


def added_within(iso, window, now=None):
    """True when the item was added inside the window ("all", "day", "week", "month"); unknown dates are never hidden.
    The timestamps compare as text, no parsing on a pass over 10 000 rows: every source gives ISO 8601 in UTC
    (Real-Debrid and Torbox 'Z', AllDebrid's _iso_from_ts, the placeholders' isoformat), so an offset never has to
    be honoured; a source with local offsets would need _parse_iso here again."""
    cutoff = added_cutoff(window, now or datetime.now(timezone.utc))
    return cutoff is None or not iso or not iso[:4].isdigit() or iso >= cutoff


ADDING = "Adding"  # placeholder rows, until the service lists the item
STATE_FILTERS = (("all", "All states"),) + tuple((l, l) for l in (QUEUED, DOWNLOADING, PROCESSING, DONE, SEEDING, STALLED, EXPIRED, ERROR))
LABEL_KIND = {QUEUED: "active", DOWNLOADING: "active", PROCESSING: "active", ADDING: "active", DONE: "done", SEEDING: "seed",
              STALLED: "warn", EXPIRED: "warn", ERROR: "error"}


def label_of(state):
    """The unified label inside a State cell: 'Error: File too big' -> 'Error', 'Done (web)' -> 'Done'."""
    return state.split(":", 1)[0].split(" (", 1)[0]


def state_matches(state, key):
    """Dropdown rule: 'all', or the cell's label is the chosen one."""
    return key == "all" or label_of(state) == key
CSS = b"""
treeview.view.progressbar { background-color: #e8761b; border-color: #e8761b; color: #ffffff; }
treeview.view.progressbar:selected { background-color: #ffffff; border-color: #ffffff; color: #e8761b; }
treeview.view.trough:selected { background-color: rgba(0, 0, 0, 0.25); color: #ffffff; }
treeview.view { font-feature-settings: 'tnum'; }
"""


DEFAULT_HIDDEN = {"Real-Debrid": ["ul", "peers", "ratio"], "AllDebrid": ["peers", "ratio"]}  # never filled by that service


def column_layout(columns, name):
    """Layout dict for one service inside cfg['columns'], created with that service's defaults."""
    layout = columns.setdefault(name, {"order": [], "hidden": list(DEFAULT_HIDDEN.get(name, []))})
    layout.setdefault("sort", ["added", "desc"])  # newest first until the user picks another header
    return layout


def state_kind(state):
    """PALETTE bucket of a State cell: by unified label, else by keywords for a word no table knew."""
    kind = LABEL_KIND.get(label_of(state))
    if kind:
        return kind
    s = state.lower()
    if any(w in s for w in ("error", "dead", "virus", "failed")):
        return "error"
    if "stalled" in s or "paused" in s:
        return "warn"
    if any(w in s for w in ("downloaded", "completed", "cached")):
        return "done"
    if "stopped" in s:  # Torbox "stopped seeding": finished, nothing moving
        return "done"
    if "upload" in s or "seed" in s:
        return "seed"
    if any(w in s for w in ("download", "metadl", "checking", "queue", "magnet", "waiting", "compressing", "adding")):
        return "active"
    return "done"  # unknown and finished-looking states ("Ready", "") count as done


def is_active(t):
    """Exactly what the Active dropdown shows: the footer count, the tray badge and the fast poll cadence follow it,
    so an expired, stalled or failed item (not finished, but not moving) never reads as active."""
    return not t["done"] and state_kind(t.get("state", "")) == "active"


class ServiceTab(Gtk.Box):
    def __init__(self, name, client, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        self.name, self.client, self.app = name, client, app
        self.is_merged = isinstance(client, MergedClient)
        self.done_ids = None  # None until first successful poll; then set of finished ids
        self.checked = set()  # ids with the checkbox on; survives refreshes
        self.torrents = []  # last poll, re-filtered on every keystroke
        self._pending = []  # (expiry, placeholder row) for items just added, until the service lists them
        self._known = set()
        self._anchor = None  # row index of the last checkbox click, for shift-click ranges
        self._iters = {}  # id -> TreeIter of rows currently in the store, for in-place updates
        self._vals = {}  # id -> the row values last written, so a poll only touches cells that changed
        self._shown_key = None  # (filter text, state filter) the store was last built for
        self.errors = []  # (time, message) of the last few failures, shown as the status line's tooltip
        self.active = 0
        self._is_busy = False
        self._applying = True  # suppress columns-changed while we build or restore the layout
        self._hold_until = 0
        self._width_timer = 0  # debounce for saving dragged column widths
        self._updated_at = 0  # time of the last successful poll, shown in the footer
        self._poll_every = app._interval  # seconds between polls, shown in the footer
        self._last_full = 0  # time.monotonic() of the last full-list poll
        self._account = None  # last account dict, reused on active-only polls
        self._stale = False  # a poll came while the tab was off screen (window hidden, another tab in front): its
        self.connect("map", lambda _w: self._stale and self._refill())  # list is current, the rows are not; fill on map

        row = Gtk.Box(spacing=2)
        for icon, tip, cb in (("list-add-symbolic", "Add magnets / links", self.on_add),
                              ("document-open-symbolic", "Add .torrent files", self.on_torrent_file),
                              ("view-refresh-symbolic", "Refresh", app.refresh),
                              ("emblem-system-symbolic", "Settings", app.open_settings),
                              ("document-open-recent-symbolic", "Activity log (Ctrl+L)", app.open_log)):
            row.pack_start(icon_button(icon, tip, cb), False, False, 0)
        more = Gtk.MenuButton(tooltip_text="More")
        more.add(Gtk.Image.new_from_icon_name("view-more-symbolic", Gtk.IconSize.BUTTON))
        more.set_relief(Gtk.ReliefStyle.NONE)
        more_menu = Gtk.Menu()
        for label, cb in (("Export magnets…", self.on_export), ("Import magnets or links…", self.on_import),
                          ("Copy all magnets", self.on_copy_all_magnets), ("About…", app.open_about)):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda _i, cb=cb: cb())
            more_menu.append(item)
        more_menu.show_all()
        more.set_popup(more_menu)
        row.pack_start(more, False, False, 0)
        self.play_btn = text_button("media-playback-start-symbolic", "Play", self.on_play)
        self.links_btn = text_button("view-list-symbolic", "Files", self.on_files)
        self.copy_btn = text_button("edit-copy-symbolic", "Copy links", self.on_copy_links)
        self.del_btn = text_button("user-trash-symbolic", "Delete", self.on_delete)
        for b in (self.del_btn, self.copy_btn, self.links_btn, self.play_btn):  # pack_end: last packed ends up leftmost
            row.pack_end(b, False, False, 0)
        self.search = Gtk.SearchEntry(placeholder_text="Filter", width_chars=30)
        self.search.connect("changed", lambda _e: self._fill())
        self.search.connect("stop-search", lambda e: e.set_text(""))  # Escape clears
        self.state_filter = Gtk.ComboBoxText()
        for key, label in STATE_FILTERS:
            self.state_filter.append(key, label)
        remembered = (app.cfg.get("filters") or {}).get(name, "all") if app.cfg.get("remember_filter") else "all"
        self.state_filter.set_active_id(remembered if remembered in dict(STATE_FILTERS) else "all")
        self.state_filter.connect("changed", self.on_state_filter)
        self.time_filter = Gtk.ComboBoxText(tooltip_text="Show items added within… (default in Settings)")
        for key, label in TIME_FILTERS:
            self.time_filter.append(key, label)
        default = app.cfg.get("time_filter") or "week"
        self.time_filter.set_active_id(default if default in dict(TIME_FILTERS) else "week")
        self.time_filter.connect("changed", lambda _c: self._fill())
        center = Gtk.Box(spacing=6)
        center.pack_start(self.search, False, False, 0)
        center.pack_start(self.state_filter, False, False, 0)
        center.pack_start(self.time_filter, False, False, 0)
        row.set_center_widget(center)
        self.pack_start(row, False, False, 0)

        self.store = Gtk.ListStore(*STORE_TYPES)
        self.view = Gtk.TreeView(model=self.store)
        self.cols = {}
        for key, title, idx, kind, sort_idx in COLUMNS:
            if kind == "toggle":
                rend = Gtk.CellRendererToggle()
                rend.connect("toggled", self.on_toggled)
                col = Gtk.TreeViewColumn("", rend, active=idx)
                self.all_check = Gtk.CheckButton()
                self.all_check.show()
                col.set_widget(self.all_check)
                col.set_clickable(True)
                col.connect("clicked", self.on_toggle_all)
            elif kind == "progress":
                col = Gtk.TreeViewColumn(title, Gtk.CellRendererProgress(), value=idx)
            else:
                ellipsize = Pango.EllipsizeMode.END if key == "name" else Pango.EllipsizeMode.NONE
                col = Gtk.TreeViewColumn(title, Gtk.CellRendererText(ellipsize=ellipsize), text=idx)
                col.set_resizable(True)
            # Fixed starting widths so an empty list lays out like a full one; all still user-resizable.
            col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            col.set_fixed_width(WIDTHS[key])
            if key == "name":
                col.set_expand(True)  # takes whatever the others leave
            if key in NUMERIC:
                col.get_cells()[0].set_property("xalign", 1.0)
                col.set_alignment(1.0)
            if kind == "text":
                col.set_cell_data_func(col.get_cells()[0], self._style_cell, key)
            if sort_idx is not None:
                col.set_sort_column_id(sort_idx)
            col.set_reorderable(True)
            self.view.append_column(col)
            col.get_button().connect("button-press-event", self.on_header_press)
            if key != "name":
                col.connect("notify::width", self.on_width_changed)
            self.cols[key] = col
        self.apply_columns()
        self.view.set_has_tooltip(True)
        self.view.set_enable_search(False)  # the filter box does this job; typeahead would steal keys
        self.view.set_fixed_height_mode(True)  # all columns are fixed-width, so GTK can skip per-row measuring
        self.view.connect("query-tooltip", self.on_query_tooltip)
        self.view.connect("button-press-event", self.on_row_press)
        self.view.connect("row-activated", self.on_row_activated)
        self.view.connect("columns-changed", self.on_columns_changed)
        self.store.connect("sort-column-changed", self.on_sort_changed)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.add(self.view)
        self.pack_start(scroller, True, True, 0)

        self.pack_start(self._build_footer(), False, False, 0)

        self.view.get_selection().connect("changed", lambda _s: self._update_buttons())
        self._update_buttons()

        # Drop .torrent files, links or magnets anywhere on the tab.
        self.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.drag_dest_add_uri_targets()
        self.drag_dest_add_text_targets()
        self.connect("drag-data-received",
                     lambda _w, _c, _x, _y, data, _i, _t: self.add_items(drop_items(data.get_uris(), data.get_text())))

    # ----- footer -----
    def _build_footer(self):
        foot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        foot.pack_start(Gtk.Separator(), False, False, 2)
        row1 = Gtk.Box(spacing=8)
        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, hexpand=True)
        self.spinner = Gtk.Spinner()
        self.poll_label = Gtk.Label(xalign=1)
        self.poll_label.get_style_context().add_class("dim-label")
        row1.pack_start(self.status, True, True, 0)
        row1.pack_end(self.poll_label, False, False, 0)
        row1.pack_end(self.spinner, False, False, 0)
        row2 = Gtk.Box(spacing=8)
        self.account_label = Gtk.Label(xalign=0, use_markup=True, ellipsize=Pango.EllipsizeMode.END)
        self.account_label.set_markup("Not configured" if self.client is None else "Loading…")
        self.totals_label = Gtk.Label(xalign=1)
        self.totals_label.get_style_context().add_class("dim-label")
        self.dash_btn = icon_button("web-browser-symbolic", f"Open {self.name} in the browser",
                                    lambda _b: open_url(DASHBOARDS.get(self.name, "")))
        row2.pack_start(self.account_label, True, True, 0)
        row2.pack_end(self.dash_btn, False, False, 0)
        row2.pack_end(self.totals_label, False, False, 0)
        foot.pack_start(row1, False, False, 0)
        foot.pack_start(row2, False, False, 0)
        return foot

    def set_polling(self, on):
        (self.spinner.start if on else self.spinner.stop)()

    def set_poll_interval(self, seconds):
        self._poll_every = seconds
        self._update_poll_label()

    def _update_poll_label(self):
        when = time.strftime("%H:%M:%S", time.localtime(self._updated_at)) if self._updated_at else "never"
        self.poll_label.set_text(f"updated {when} · every {self._poll_every} s")

    def _update_account(self, account):
        plan = GLib.markup_escape_text(account["plan"])
        self.account_label.set_markup(
            f'<span background="{plan_color(account["plan"])}" foreground="#ffffff"> {plan} </span>  '
            f"{GLib.markup_escape_text(expiry_text(account['expires']))} · {GLib.markup_escape_text(account['extra'])}")

    def _update_totals(self):
        self.totals_label.set_text(totals_text(self.torrents, len(self.store), len(self.checked & self._iters.keys())))

    # ----- column layout (one per service, kept in the config) -----
    def apply_columns(self):
        cfg = column_layout(self.app.cfg.setdefault("columns", {}), self.name)
        self._applying = True
        prev = None
        for key in cfg.get("order") or []:
            col = self.cols.get(key)
            if col is not None:
                self.view.move_column_after(col, prev)
                prev = col
        hidden = set(cfg.get("hidden") or [])
        for key, col in self.cols.items():
            col.set_visible(self.is_merged if key == "service" else key not in hidden)
        for key, width in (cfg.get("widths") or {}).items():
            if key in self.cols and key != "name" and int(width) > 0:
                self.cols[key].set_fixed_width(int(width))
        sort = cfg.get("sort")
        if sort:
            idx = next((s for k, _t, _i, _k, s in COLUMNS if k == sort[0]), None)
            if idx is not None:
                self.store.set_sort_column_id(idx, Gtk.SortType.DESCENDING if sort[1] == "desc" else Gtk.SortType.ASCENDING)
        self._applying = False

    def on_width_changed(self, _col, _pspec):
        """A header edge was dragged: save all widths once the drag settles."""
        if self._applying:
            return
        if self._width_timer:
            GLib.source_remove(self._width_timer)
        self._width_timer = GLib.timeout_add(800, self._save_widths)

    def _save_widths(self):
        self._width_timer = 0
        widths = {k: c.get_width() for k, c in self.cols.items() if k != "name" and c.get_visible() and c.get_width() > 0}
        self.app.set_columns(self, widths=widths)
        return False

    def on_columns_changed(self, _view):
        if self._applying:
            return
        order = [k for col in self.view.get_columns() for k, c in self.cols.items() if c is col]
        self.app.set_columns(self, order=order)

    def on_sort_changed(self, store):
        if self._applying:
            return
        idx, direction = store.get_sort_column_id()  # idx < 0 means unsorted
        key = next((k for k, _t, _i, _k, s in COLUMNS if s == idx), None)
        if key:
            self.app.set_columns(self, sort=[key, "desc" if direction == Gtk.SortType.DESCENDING else "asc"])

    def on_query_tooltip(self, view, x, y, keyboard, tooltip):
        """Hovering a relative date shows the full local time; hovering a State cell, the service's own word."""
        if keyboard:
            return False
        hit = view.get_path_at_pos(*view.convert_widget_to_bin_window_coords(x, y))
        if not hit:
            return False
        path, col = hit[0], hit[1]
        key = next((k for k, c in self.cols.items() if c is col), None)
        idx = TOOLTIP_COLUMNS.get(key)
        text = self.store[path][idx] if idx is not None else ""
        if not text:
            return False
        tooltip.set_text(text)
        view.set_tooltip_cell(tooltip, path, col, None)
        return True

    def on_header_press(self, _btn, event):
        if event.button != 3:
            return False
        menu = Gtk.Menu()
        for key, title, _i, _k, _s in COLUMNS:
            if key in ("check", "name", "service"):
                continue
            item = Gtk.CheckMenuItem(label=title, active=self.cols[key].get_visible())
            item.connect("toggled", lambda it, k=key: self.app.set_columns(self, hide=(k, not it.get_active())))
            menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _style_cell(self, _col, cell, model, it, key):
        """Zebra rows, state colored by kind, active names bold. Selected rows keep the theme's look."""
        if self.view.get_selection().iter_is_selected(it):
            cell.set_property("cell-background-set", False)
            cell.set_property("foreground-set", False)
            return
        fg = self.view.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        idx = model.get_path(it).get_indices()[0]
        cell.set_property("cell-background-rgba", Gdk.RGBA(fg.red, fg.green, fg.blue, 0.035 if idx % 2 else 0))
        kind = state_kind(model[it][3])
        if key == "service":
            cell.set_property("foreground", SERVICE_COLORS.get(model[it][23], PALETTE["done"]))
            cell.set_property("text", service_label(model[it][23], self.app.cfg))  # the store keeps the full name
            cell.set_property("weight", Pango.Weight.BOLD)
        elif key == "state":
            cell.set_property("foreground", PALETTE[kind])
        elif key == "name":
            cell.set_property("foreground-rgba", fg)
            cell.set_property("weight", Pango.Weight.BOLD if kind == "active" else Pango.Weight.NORMAL)
        else:
            cell.set_property("foreground-rgba", Gdk.RGBA(fg.red, fg.green, fg.blue, 0.9))

    # ----- checkboxes and targets -----
    def on_toggled(self, _rend, path):
        row = self.store[path]
        row[CHECKED] = not row[CHECKED]
        (self.checked.add if row[CHECKED] else self.checked.discard)(row[0])
        self._anchor = Gtk.TreePath.new_from_string(path).get_indices()[0]
        self._sync_checks()

    def _tick_range(self, index):
        """Shift-click on a checkbox: give every row between the last click and this one the anchor's state."""
        if self._anchor is None or self._anchor >= len(self.store):
            return False
        turn_on = self.store[self._anchor][CHECKED]
        lo, hi = sorted((self._anchor, index))
        for row in list(self.store)[lo:hi + 1]:
            row[CHECKED] = turn_on
            (self.checked.add if turn_on else self.checked.discard)(row[0])
        self._sync_checks()
        return True

    def on_toggle_all(self, _col):
        turn_on = not all(r[CHECKED] for r in self.store)
        for r in self.store:
            r[CHECKED] = turn_on
        self.checked = {r[0] for r in self.store} if turn_on else set()
        self._sync_checks()

    def _sync_checks(self):
        rows = len(self.store)
        self.all_check.set_active(rows > 0 and all(r[CHECKED] for r in self.store))
        self._update_buttons()
        self._update_totals()

    def _row(self, r):
        return {"id": r[0], "name": r[1], "kind": r[5], "hash": r[22]}

    def _selected(self):
        model, it = self.view.get_selection().get_selected()
        return None if it is None else self._row(model[it])

    def _targets(self):
        """Checked rows if any, else the highlighted row."""
        checked = [self._row(r) for r in self.store if r[CHECKED]]
        if not checked:
            sel = self._selected()
            checked = [sel] if sel else []
        return [t for t in checked if not t["id"].startswith("pending:")]

    def _update_buttons(self):
        n = len(self._targets()) if self.client is not None and not self._is_busy else 0
        self.play_btn.set_sensitive(n == 1 and bool(self.app.player or self.app.cfg.get("player_command")))
        self.links_btn.set_sensitive(n == 1)
        self.copy_btn.set_sensitive(n >= 1)
        self.del_btn.set_sensitive(n >= 1)

    def _busy(self, flag):
        self._is_busy = flag
        self.set_polling(flag)
        self._update_buttons()

    def say(self, msg, error=False):
        """Status line message that polls leave alone for a few seconds. Errors stay in the tooltip."""
        self.status.set_markup(red(msg) if error else GLib.markup_escape_text(msg))
        self._hold_until = time.monotonic() + STATUS_HOLD
        log_event(self.name, ("ERROR " if error else "") + msg, "error" if error else "action")
        if error and (not self.errors or self.errors[-1][1] != msg):
            self.errors = (self.errors + [(time.strftime("%H:%M:%S"), msg)])[-8:]
            self.status.set_tooltip_text("Recent errors:\n" + "\n".join(f"{t}  {m}" for t, m in self.errors))
        if error and msg == INVALID_KEY:
            self.app.open_settings_once()

    def _run(self, fn, cb):
        """Run a network action; show errors in the status label; then cb(result, error)."""
        self._busy(True)
        self.status.set_text("Working…")

        def done(res, err):
            self._busy(False)
            if err:
                self.say(err, error=True)
            cb(res, err)
        run_async(fn, done)

    # ----- rendering -----
    def render(self, account, torrents, error):
        if error:
            self.account_label.set_markup(red(error))
            self.say(error, error=True)  # also logged; an invalid key opens Settings from there
            return
        self._account = account
        self._update_account(account)
        days = days_until(account["expires"])
        warned = self.app.cfg.setdefault("notified_expiry", {})
        if days is not None and days <= EXPIRY_WARN_DAYS and warned.get(self.name) != account["expires"]:
            warned[self.name] = account["expires"]
            save_config(self.app.cfg)
            notify(f"{self.name}: premium ends in {days} day(s)", f"Expires {account['expires']}")
        self.torrents = torrents
        ids = {t["id"] for t in torrents}
        if ids - self._known:  # something new showed up: placeholders have done their job
            self._pending = []
        self._known = ids
        self.checked &= ids
        if self.get_mapped():
            self._fill()
        else:  # nobody is looking: the rows wait for the map signal (most of a tray app's life is spent hidden)
            self._stale = True
        self.active = sum(1 for t in torrents if is_active(t))
        log_event(self.name, f"Poll: {len(torrents)} items, {self.active} active", "verbose")
        self._updated_at = time.time()
        self._update_poll_label()

        prev = self.done_ids
        self.done_ids = {t["id"] for t in torrents if t["done"]}
        if prev is not None and not self.is_merged:  # the real tabs already notify and act
            for t in torrents:
                if t["done"] and t["id"] not in prev:
                    if self.app.cfg["notify_done"]:
                        notify(f"{self.name}: finished", t["name"],
                               [("Open files", lambda t=t: self.app.show_files(self, t))])
                    if self.app.cfg.get("auto_download"):
                        self.auto_download(t)
                    if self.app.cfg.get("stop_seeding_when_done") and t["kind"] == "torrent" and hasattr(self.client, "control"):
                        run_async(lambda t=t: self.client.control(t["id"], "stop_seeding", t["kind"]),
                                  lambda r, e, t=t: self.say(f"Stop seeding {t['name'][:40]}: {e or r}", error=bool(e)))

    def auto_download(self, t):
        """Fetch the finished item's links and start a resumable bulk download into its own folder."""
        tool = "aria2c" if shutil.which("aria2c") else "wget"
        dest = os.path.join(os.path.expanduser(self.app.cfg.get("download_dir") or "~/Downloads"), safe_dirname(t["name"]))

        def start(links, err):
            if err:
                return
            usable = video_links(links) if self.app.cfg.get("auto_download_videos_only") else usable_links(links)
            urls = [l["url"] for l in usable if is_http(l["url"])]
            if not urls:
                self.say(f"Auto-download: no links for {t['name'][:40]}", error=True)
                return
            os.makedirs(dest, exist_ok=True)
            os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
            log_path = os.path.join(DATA_DIR, f"download-{safe_dirname(t['name'])[:40]}.log")
            with open(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab") as log:
                proc = subprocess.Popen(download_command(urls, dest, tool, via_stdin=True), stdin=subprocess.PIPE,
                                        stdout=log, stderr=log)
            try:
                proc.stdin.write(("\n".join(urls) + "\n").encode())  # on stdin: other users' ps never sees the links
                proc.stdin.close()
            except BrokenPipeError:
                pass  # the tool quit at once; its exit code is reported below
            self.say(f"Auto-download: {len(urls)} file(s) via {tool} into {dest}")
            notify(f"{self.name}: downloading {len(urls)} file(s)", dest)

            def finished(code):
                if code == 0:
                    notify(f"{self.name}: download finished", dest)
                    if self.app.cfg.get("delete_after_download"):
                        run_async(lambda: self.client.delete(t["id"], t["kind"]),
                                  lambda _r, e: (self.say(f"Deleted after download: {t['name'][:40]}" if not e else f"Delete after download failed: {e}", error=bool(e)), self.app.refresh()))
                else:
                    self.say(f"Download of {t['name'][:40]} exited with code {code}, see the log next to the applet", error=True)
            threading.Thread(target=lambda: GLib.idle_add(finished, proc.wait()), daemon=True).start()
        self._run(lambda: self.client.links(t["id"], t["kind"]), start)

    def on_state_filter(self, combo):
        if self.app.cfg.get("remember_filter"):
            self.app.cfg.setdefault("filters", {})[self.name] = combo.get_active_id() or "all"
            save_config(self.app.cfg)
        self._fill()

    def _refill(self):
        """The tab came on screen with rows behind its list: the All tab rebuilds from the service tabs' lists,
        a service tab shows what it has."""
        self._stale = False
        if self.is_merged:
            self.render(self.client.account(), self.client.torrents(), None)
        else:
            self._fill()

    def _row_values(self, t):
        return [
            t["id"], t["name"], int(max(0, min(100, t["progress"]))), t["state"], human_size(t["size"]), t["kind"],
            t["id"] in self.checked,
            fmt_speed(t["speed"]), fmt_speed(t["up_speed"]), str(t["seeds"]), na(t["peers"]), na(t["ratio"], "{:.2f}"),
            relative_time(t["added"]), relative_time(t["finished"]),
            float(t["size"]), float(t["speed"] or 0), float(t["up_speed"] or -1),
            int(t["seeds"]), -1 if t["peers"] is None else int(t["peers"]),
            -1.0 if t["ratio"] is None else float(t["ratio"]),
            local_time(t["added"]), local_time(t["finished"]), t["hash"], t.get("service", self.name),
            t.get("raw_state", ""),
        ]

    def _fill(self):
        """Show the last poll through the filter box and the state / added-within dropdowns. Same filters as last
        time: update rows in place (keeps selection and scroll, cheap for thousands of rows); otherwise rebuild."""
        q = self.search.get_text().strip().lower()
        kind = self.state_filter.get_active_id() or "all"
        window = self.time_filter.get_active_id() or "all"
        now = time.monotonic()
        utc_now = datetime.now(timezone.utc)
        self._pending = [(exp, row) for exp, row in self._pending if now < exp]
        rows = [t for t in [row for _exp, row in self._pending] + self.torrents  # placeholders first
                if not (q and q not in t["name"].lower() and q not in t["state"].lower())
                and state_matches(t["state"], kind)
                and added_within(t["added"], window, utc_now)]
        if self._shown_key == (q, kind, window) and self._iters:
            wanted = {t["id"] for t in rows}
            for tid in [tid for tid in self._iters if tid not in wanted]:
                self.store.remove(self._iters.pop(tid))
                self._vals.pop(tid, None)
            for t in rows:
                values = self._row_values(t)
                old, self._vals[t["id"]] = self._vals.get(t["id"]), values
                it = self._iters.get(t["id"])
                if it is None:
                    self._iters[t["id"]] = self.store.append(values)
                    continue
                changed = [i for i, v in enumerate(values) if i != CHECKED and (old is None or old[i] != v)]
                if changed:
                    self.store.set(it, changed, [values[i] for i in changed])
        else:
            selected = self._selected()
            self.store.clear()
            self._iters, self._vals = {}, {}
            self._anchor = None
            for t in rows:
                self._vals[t["id"]] = values = self._row_values(t)
                self._iters[t["id"]] = self.store.append(values)
            if selected and selected["id"] in self._iters:
                self.view.get_selection().select_iter(self._iters[selected["id"]])
            self._shown_key = (q, kind, window)
        self._sync_checks()

    # ----- actions -----
    def on_add(self, _w):
        if self.client is None:
            return
        dlg = Gtk.Dialog(title=f"Add to {self.name}", transient_for=self.app.window, modal=True,
                         default_width=560, default_height=320)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Add", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        box = dlg.get_content_area()
        box.set_spacing(6)
        box.set_property("margin", 12)
        hint = Gtk.Label(xalign=0, label="One per line:  magnet:?xt=urn:btih:…   https://…   http://…")
        hint.get_style_context().add_class("dim-label")
        torbox = self.app.tab_named("Torbox")
        route = Gtk.CheckButton(label="Send magnets Torbox already has cached to Torbox instead",
                                active=bool(self.app.cfg.get("route_cached")))
        route.set_sensitive(bool(torbox and torbox.client) and torbox is not self)
        view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, left_margin=8, right_margin=8,
                            top_margin=6, bottom_margin=6)
        buf = view.get_buffer()
        scroller = Gtk.ScrolledWindow(vexpand=True, shadow_type=Gtk.ShadowType.IN)
        scroller.add(view)
        result = Gtk.Label(xalign=0, wrap=True)
        box.pack_start(hint, False, False, 0)
        box.pack_start(scroller, True, True, 0)
        box.pack_start(result, False, False, 0)
        if route.get_sensitive():
            box.pack_start(route, False, False, 0)
        if hasattr(self.client, "check_cached"):
            self._live_cache_check(buf, result)
        dlg.show_all()
        view.grab_focus()
        ok = dlg.run() == Gtk.ResponseType.OK
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        use_route = route.get_sensitive() and route.get_active()
        dlg.destroy()
        if not ok:
            return
        items = [line.strip() for line in text.splitlines() if line.strip()]
        if route.get_sensitive() and self.app.cfg.get("route_cached") != use_route:
            self.app.cfg["route_cached"] = use_route
            save_config(self.app.cfg)
        if use_route:
            self.app.add_routed(items, self)
        else:
            self.add_items(items)

    def _live_cache_check(self, buf, result):
        """While the Add box is open: 500 ms after the last edit, ask the service which magnets are cached."""
        state = {"timer": 0, "alive": True}
        result.connect("destroy", lambda _w: state.update(alive=False))

        def text_lines():
            text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
            return [line.strip() for line in text.splitlines() if line.strip()]

        def show(magnets, cached, err):
            if not state["alive"]:
                return
            if err:
                result.set_markup(red(f"Cache check failed: {err}"))
                return
            parts = []
            for line, h in magnets:
                name = GLib.markup_escape_text(add_label(line)[:60])
                if not h:
                    parts.append(f"<span foreground='{PALETTE['warn']}'>? no hash</span>  {name}")
                elif h in cached:
                    parts.append(f"<span foreground='{PALETTE['done']}'>✓ cached</span>  {name}")
                else:
                    parts.append(f"<span foreground='{PALETTE['error']}'>✗ not cached</span>  {name}")
            result.set_markup("\n".join(parts))

        def check():
            state["timer"] = 0
            magnets = [(l, magnet_hash(l)) for l in text_lines() if l.lower().startswith("magnet:")]
            if not magnets or not state["alive"]:
                result.set_text("")
                return False
            result.set_text("Checking cache…")
            hashes = [h for _, h in magnets if h]
            run_async(lambda: self.client.check_cached(hashes), lambda cached, err: show(magnets, cached or set(), err))
            return False

        def schedule(_buf):
            if state["timer"]:
                GLib.source_remove(state["timer"])
            state["timer"] = GLib.timeout_add(500, check)
        buf.connect("changed", schedule)

    def on_torrent_file(self, _w):
        if self.client is None:
            return
        dlg = Gtk.FileChooserNative.new("Add .torrent files", self.app.window,
                                        Gtk.FileChooserAction.OPEN, "Add", "Cancel")
        dlg.set_select_multiple(True)
        flt = Gtk.FileFilter()
        flt.set_name("Torrent files")
        flt.add_pattern("*.torrent")
        dlg.add_filter(flt)
        paths = dlg.get_filenames() if dlg.run() == Gtk.ResponseType.ACCEPT else []
        dlg.destroy()
        self.add_items(paths)

    def add_items(self, items, after=None, attempt=1):
        """Add magnets, links and .torrent paths sequentially in one worker; report counts in the status line.
        after(added_labels, errors) runs on the main loop when done. Rate-limit and network failures are retried
        every 60 s, three times."""
        if not items or self.client is None:
            return
        retry = []

        def work():
            added, urls, errors = [], [], []
            for item in items:
                try:
                    url = add_one(self.client, item)
                except ApiError as e:
                    errors.append(f"{add_label(item)}: {e}")
                    if retryable(str(e)):
                        retry.append(item)
                    continue
                except OSError as e:  # unreadable .torrent path or a failed .torrent URL fetch
                    errors.append(f"{add_label(item)}: {getattr(e, 'strerror', None) or e}")
                    continue
                except Exception as e:  # anything else while fetching a .torrent URL
                    errors.append(f"{add_label(item)}: {e}")
                    continue
                added.append(add_label(item))
                if url:
                    urls.append(url)
            return added, urls, errors

        def done(res, err):
            if err:
                return
            added, urls, errors = res
            if urls:  # Real-Debrid hands back direct URLs right away
                copy_text("\n".join(urls))
            if self.app.cfg["notify_added"]:
                for name in added:
                    notify(f"{self.name}: added", name)
            msg = f"Added {len(added)}" + (", direct link(s) copied" if urls else "")
            if errors:
                msg += " · failed: " + "; ".join(errors)
            if retry and attempt < 3:
                msg += f" · retrying {len(retry)} in 60 s ({attempt}/3)"
                GLib.timeout_add_seconds(60, lambda: self.add_items(list(retry), after, attempt + 1) and False)
            self.say(msg, error=bool(errors))
            for name in added:  # show something at once; the real row replaces it on the next poll
                self._pending.append((time.monotonic() + 30, {
                    "id": f"pending:{name}", "name": name, "progress": 0, "state": ADDING, "done": False,
                    "size": 0, "seeds": 0, "speed": 0, "kind": "torrent", "up_speed": None, "peers": None,
                    "ratio": None, "added": datetime.now(timezone.utc).isoformat(), "finished": "", "hash": ""}))
            self._fill()
            for row in self.store:  # bring the first placeholder into view
                if row[0].startswith("pending:"):
                    self.view.scroll_to_cell(row.path, None, True, 0.0, 0.0)
                    break
            self.app.refresh()
            GLib.timeout_add_seconds(3, lambda: self.app.refresh() and False)  # services list new items with a lag
            if after and not (retry and attempt < 3):  # with a retry pending, report once, after the last attempt
                after(added, errors)
        self._run(work, done)

    def on_row_press(self, view, event):
        """Shift-click on a checkbox ticks a range; right-click shows the context menu."""
        hit = view.get_path_at_pos(int(event.x), int(event.y))
        if not hit:
            return False
        if event.button == 1 and event.state & Gdk.ModifierType.SHIFT_MASK and hit[1] is self.cols["check"]:
            return self._tick_range(hit[0].get_indices()[0])
        if event.button != 3:
            return False
        if not any(r[CHECKED] for r in self.store):
            view.get_selection().select_path(hit[0])
        n = len(self._targets())
        entries = [("Play", self.on_play, n == 1 and bool(self.app.player or self.app.cfg.get("player_command"))),
                   ("Files…", self.on_files, n == 1),
                   ("Copy links", self.on_copy_links, n >= 1),
                   ("Copy magnet", self.on_copy_magnet, n >= 1),
                   ("Copy as aria2c command", self.on_copy_aria2, n >= 1)]
        if hasattr(self.client, "control"):
            entries += [(None, None, True)] + [(label, lambda _w, op=op: self.on_control(op), n >= 1)
                                               for label, op in (("Resume", "resume"), ("Stop seeding", "stop_seeding"),
                                                                 ("Reannounce", "reannounce"))]
        entries += [(None, None, True), ("Delete", self.on_delete, n >= 1)]
        menu = Gtk.Menu()
        others = [t for t in self.app.tabs if t is not self and t.client is not None and not t.is_merged]
        if others:
            send = Gtk.MenuItem(label="Send to", sensitive=n >= 1 and all(t["hash"] for t in self._targets()))
            sub_menu = Gtk.Menu()
            for other in others:
                item = Gtk.MenuItem(label=other.name)
                item.connect("activate", lambda _i, o=other: self.on_send_to(o))
                sub_menu.append(item)
            send.set_submenu(sub_menu)
            menu.append(send)
        for label, cb, ok in entries:
            if label is None:
                menu.append(Gtk.SeparatorMenuItem())
                continue
            item = Gtk.MenuItem(label=label, sensitive=ok)
            if label in ("Resume", "Stop seeding", "Reannounce"):
                item.set_tooltip_text(CONTROL_LAG_NOTE)
            item.connect("activate", lambda _i, cb=cb: cb(None))
            menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def on_export(self):
        if self.client is None:
            return
        dlg = Gtk.FileChooserNative.new("Export magnets", self.app.window, Gtk.FileChooserAction.SAVE, "Export", "Cancel")
        dlg.set_current_name(f"{self.name.lower()}-magnets.txt")
        dlg.set_do_overwrite_confirmation(True)
        path = dlg.get_filename() if dlg.run() == Gtk.ResponseType.ACCEPT else None
        dlg.destroy()
        if not path:
            return
        try:
            write_magnets(path, self.torrents)
            n = sum(1 for t in self.torrents if t.get("hash"))
            self.say(f"Exported {n} magnet(s) to {path}" + (" (AllDebrid gives no hashes)" if n == 0 and self.name == "AllDebrid" else ""))
        except OSError as e:
            self.say(f"Export failed: {e.strerror}", error=True)

    def on_import(self):
        if self.client is None:
            return
        dlg = Gtk.FileChooserNative.new("Import magnets or links", self.app.window, Gtk.FileChooserAction.OPEN, "Import", "Cancel")
        path = dlg.get_filename() if dlg.run() == Gtk.ResponseType.ACCEPT else None
        dlg.destroy()
        if not path:
            return
        try:
            items = read_magnets(path)
        except OSError as e:
            self.say(f"Import failed: {e.strerror}", error=True)
            return
        if not items:
            self.say("Nothing to import in that file")
            return
        self.add_items(items)

    def on_copy_all_magnets(self):
        magnets = [magnet_for(t["hash"], t["name"]) for t in self.torrents if t.get("hash")]
        if magnets:
            copy_text("\n".join(magnets))
        self.say(f"Copied {len(magnets)} magnet(s)")

    def on_send_to(self, target):
        """Re-add the targets' magnets on another service."""
        magnets = [magnet_for(t["hash"], t["name"]) for t in self._targets() if t["hash"]]
        if not magnets:
            self.say("No hash known for the selection")
            return
        target.say(f"{len(magnets)} item(s) sent from {self.name}")
        target.add_items(magnets)
        self.say(f"Sent {len(magnets)} item(s) to {target.name}")

    def on_copy_magnet(self, _w):
        magnets = [magnet_for(t["hash"], t["name"]) for t in self._targets()]
        magnets = [m for m in magnets if m]
        if magnets:
            copy_text("\n".join(magnets))
        self.say(f"Copied {len(magnets)} magnet link(s)" if magnets else "No hash known for the selection")

    def _play_one_of(self, vids, links=()):
        """One candidate plays at once; several show a picker. links lets play() find a matching subtitle."""
        if len(vids) == 1:
            self.play(vids[0]["url"], vids[0]["name"], links)
            return
        menu = Gtk.Menu()
        for v in vids:
            item = Gtk.MenuItem(label=v["name"][:80])
            item.connect("activate", lambda _i, v=v: self.play(v["url"], v["name"], links))
            menu.append(item)
        menu.append(Gtk.SeparatorMenuItem())
        play_all = Gtk.MenuItem(label=f"Play all {len(vids)} in order")
        play_all.connect("activate", lambda _i: self.play_all([v["url"] for v in vids]))
        menu.append(play_all)
        menu.show_all()
        menu.popup_at_pointer(None)

    def on_play(self, _w):
        targets = self._targets()
        if len(targets) != 1 or not (self.app.player or self.app.cfg.get("player_command")):
            return
        t = targets[0]

        def pick(links, err):
            if err:
                return
            vids = video_links(links)
            if vids:
                self._play_one_of(vids, links)
            else:
                self.say("Nothing playable yet (not finished?)")
        self._run(lambda: self.client.links(t["id"], t["kind"]), pick)

    def on_row_activated(self, *_):
        """Double-click: the file panel, or (if enabled in Settings) play when the item has video files.
        The first time a double-clicked item has videos and a player exists, offer to turn that on."""
        targets = self._targets()
        if len(targets) != 1:
            return
        t = targets[0]
        cfg = self.app.cfg

        def decide(links, err):
            if err:
                return
            vids = video_links(links, strict=True) if self.app.player else []
            if vids and cfg.get("dblclick_play"):
                self._play_one_of(vids, links)
            elif vids and not cfg.get("dblclick_asked"):
                self._offer_dblclick_play(t, links, vids)
            else:
                self._files_dialog(t, links)
        self._run(lambda: self.client.links(t["id"], t["kind"]), decide)

    def _offer_dblclick_play(self, t, links, vids):
        """One-time question; either answer is remembered and can be changed in Settings."""
        dlg = Gtk.MessageDialog(transient_for=self.app.window, modal=True, message_type=Gtk.MessageType.QUESTION,
                                text="Play videos on double-click?")
        dlg.format_secondary_text(
            f"This item has video files and {self.app.player} is installed.\n"
            "Double-click can play them straight away instead of opening the file panel.\n\n"
            "You can change this any time in Settings. This question is shown only once.")
        dlg.add_buttons("Open Files", Gtk.ResponseType.NO, "Play on double-click", Gtk.ResponseType.YES)
        dlg.set_default_response(Gtk.ResponseType.YES)
        answer = dlg.run()
        dlg.destroy()
        self.app.cfg["dblclick_asked"] = True
        self.app.cfg["dblclick_play"] = answer == Gtk.ResponseType.YES
        save_config(self.app.cfg)
        if answer == Gtk.ResponseType.YES:
            self._play_one_of(vids, links)
        else:
            self._files_dialog(t, links)

    def play_all(self, urls):
        custom = (self.app.cfg.get("player_command") or "").strip()
        urls = [u for u in urls if is_http(u)]
        if custom:
            try:
                for u in urls:  # one invocation per file; the target decides how to queue
                    subprocess.Popen(custom_cmd(custom, u))
            except (OSError, ValueError) as e:
                self.say(f"Custom play command failed: {e}", error=True)
                return
            self.say(f"Sent {len(urls)} files to: {custom.split()[0]}")
            return
        if not self.app.player:
            self.say("No media player found (mpv, vlc, celluloid, smplayer or totem)")
            return
        subprocess.Popen(playlist_cmd(self.app.player, urls))
        self.say(f"Playing {len(urls)} files in {self.app.player}")

    def play(self, url, name="", links=()):
        """Hand a video URL to the player, with its subtitle file when the item has a matching one.
        A custom command in Settings (Stremio, Kodi, Jellyfin, anything) replaces the detected player."""
        custom = (self.app.cfg.get("player_command") or "").strip()
        if custom:
            if not is_http(url):
                return
            sub_url = pick_subtitle(name, links) if links else None
            try:
                subprocess.Popen(custom_cmd(custom, url, sub_url))
                self.say(f"Sent to: {custom.split()[0]}")
            except (OSError, ValueError) as e:
                self.say(f"Custom play command failed: {e}", error=True)
            return
        if not self.app.player:
            self.say("No media player found (mpv, vlc, celluloid, smplayer or totem)")
            return
        if not is_http(url):
            return
        sub_url = pick_subtitle(name, links) if links else None
        subprocess.Popen(player_cmd(self.app.player, url, sub_url))
        self.say(f"Playing in {self.app.player}" + (" with subtitles" if sub_url else ""))

    def on_files(self, _w):
        targets = self._targets()
        if len(targets) == 1:
            self.open_files_for(targets[0])

    def open_files_for(self, t):
        """Fetch the item's links, then show the file panel."""
        self._run(lambda: self.client.links(t["id"], t["kind"]),
                  lambda links, err: None if err else self._files_dialog(t, links))

    def _files_dialog(self, t, links):
        if not links:
            self.say("No files yet (not finished?)")
            return
        dlg = Gtk.Dialog(title=f"Files · {t['name'][:70]}", transient_for=self.app.window,
                         default_width=680, default_height=380)
        box = dlg.get_content_area()
        box.set_spacing(6)
        box.set_property("margin", 8)
        store = Gtk.ListStore(bool, str, str, str)  # checked, name, size, url
        for link in links:
            store.append([False, link["name"], human_size(link["size"]) if link.get("size") else "", link["url"] or ""])
        view = Gtk.TreeView(model=store)
        view.set_enable_search(False)
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", lambda _r, path: store.set_value(store.get_iter(path), 0, not store[path][0]))
        view.append_column(Gtk.TreeViewColumn("", toggle, active=0))
        name_col = Gtk.TreeViewColumn("Name", Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.MIDDLE), text=1)
        name_col.set_expand(True)
        view.append_column(name_col)
        size_col = Gtk.TreeViewColumn("Size", Gtk.CellRendererText(xalign=1.0), text=2)
        size_col.set_alignment(1.0)
        view.append_column(size_col)
        scroller = Gtk.ScrolledWindow(vexpand=True, shadow_type=Gtk.ShadowType.IN)
        scroller.add(view)
        box.pack_start(scroller, True, True, 0)

        def chosen():
            """Ticked rows, else the highlighted row, else everything; only entries that have a URL."""
            rows = [r for r in store if r[0]]
            if not rows:
                _m, it = view.get_selection().get_selected()
                rows = [store[it]] if it else list(store)
            return [r[3] for r in rows if is_http(r[3])]

        def copy():
            urls = chosen()
            copy_text("\n".join(urls))
            self.say(f"Copied {len(urls)} link(s)")

        actions = Gtk.Box(spacing=6)
        def copy_aria2():
            urls = chosen()
            copy_text(aria2_command(urls, os.path.expanduser("~/Downloads")))
            self.say(f"aria2c command for {len(urls)} file(s) copied")

        for icon, label, cb in (("web-browser-symbolic", "Download via browser", lambda: [open_url(u) for u in chosen()]),
                                ("media-playback-start-symbolic", "Play", lambda: chosen() and self.play(chosen()[0], "", links)),
                                ("edit-copy-symbolic", "Copy links", copy),
                                ("utilities-terminal-symbolic", "aria2c command", copy_aria2)):
            actions.pack_start(text_button(icon, label, lambda _b, cb=cb: cb()), False, False, 0)
        box.pack_start(actions, False, False, 0)
        dlg.add_button("Close", Gtk.ResponseType.CLOSE)
        dlg.connect("response", lambda d, _r: d.destroy())
        view.connect("row-activated", lambda v, path, _c: self.play(store[path][3], store[path][1], links) if self.app.player else open_url(store[path][3]))
        dlg.show_all()
        self.say(f"{len(links)} file(s)")

    def _collect_links(self, done):
        """Fetch every file URL of all targets, then done(urls, errors)."""
        targets = self._targets()
        if not targets:
            return

        def work():
            urls, errors = [], []
            for t in targets:
                for link in self.client.links(t["id"], t["kind"]):
                    if link["name"].startswith("All files"):
                        continue
                    if is_http(link["url"]):
                        urls.append(link["url"])
                    else:
                        errors.append(link["name"])
            return urls, errors
        self._run(work, lambda res, err: None if err else done(*res))

    def on_copy_links(self, _btn):
        def done(urls, errors):
            if urls:
                copy_text("\n".join(urls))
            msg = f"Copied {len(urls)} link(s)" if urls else "No links yet (not finished?)"
            if errors:
                msg += " · " + "; ".join(errors)
            self.say(msg, error=bool(errors))
        self._collect_links(done)

    def on_copy_aria2(self, _w):
        def done(urls, errors):
            if urls:
                copy_text(aria2_command(urls, os.path.expanduser("~/Downloads")))
            msg = f"aria2c command for {len(urls)} file(s) copied" if urls else "No links yet (not finished?)"
            if errors:
                msg += " · " + "; ".join(errors)
            self.say(msg, error=bool(errors))
        self._collect_links(done)

    def on_control(self, op):
        """Torbox resume / stop seeding / reannounce on every target."""
        targets = self._targets()
        if not targets:
            return

        def work():
            replies, errors = [], []
            for t in targets:
                try:
                    replies.append(self.client.control(t["id"], op, t["kind"]))
                except ApiError as e:
                    errors.append(f"{t['name'][:40]}: {e}")
            return replies, errors

        def done(res, err):
            if err:
                return
            replies, errors = res
            parts = sorted(set(replies)) + errors
            msg = f"{op.replace('_', ' ').capitalize()}: " + "; ".join(parts)
            if replies and op != "reannounce":
                msg += " · state may lag on Torbox's side"
            self.say(msg, error=bool(errors))
            self.app.refresh()
            GLib.timeout_add_seconds(5, lambda: self.app.refresh() and False)
        self._run(work, done)

    def on_delete(self, _btn):
        targets = self._targets()
        if not targets:
            return
        if len(targets) > 1:
            dlg = Gtk.MessageDialog(transient_for=self.app.window, modal=True, message_type=Gtk.MessageType.QUESTION,
                                    buttons=Gtk.ButtonsType.OK_CANCEL, text=f"Delete {len(targets)} items from {self.name}?")
            ok = dlg.run() == Gtk.ResponseType.OK
            dlg.destroy()
            if not ok:
                return

        def work():
            errors = []
            for t in targets:
                try:
                    self.client.delete(t["id"], t["kind"])
                except ApiError as e:
                    errors.append(f"{t['name'][:40]}: {e}")
            return errors

        def done(errors, err):
            if err:
                return
            self.checked -= {t["id"] for t in targets}
            if errors:
                self.say("Delete failed: " + "; ".join(errors), error=True)
            else:
                self.say(f"Deleted {len(targets)}")
            self.app.refresh()
        self._run(work, done)


class App:
    def __init__(self, gapp):
        self.gapp = gapp
        self.player = find_player()
        Notify.init(APP_ID)
        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.IconTheme.get_default().append_search_path(HERE)  # debrid-applet.svg next to this file
        Gtk.Window.set_default_icon_name(APP_ID)
        self.cfg = load_config()
        self.apply_log_settings()
        try:  # logs live here; keep them to this user (also fixes directories made by older versions)
            os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
            os.chmod(DATA_DIR, 0o700)
        except OSError:
            pass
        self._polling = False  # a poll is in flight; refresh() then only asks for another one afterwards
        self.restart_requested = False  # set by restart(); main() re-execs this file after the main loop ends
        self._updating = ""  # version an update_to() is working on, until it finishes or fails
        self._installed = ""  # version installed in this session but not yet running (the user chose "Not now")
        self._again = None  # "light" / "full": a refresh asked for while a poll was running
        self._timer = 0
        self._interval = POLL_SLOW
        self._settings_shown_for = None
        self.tabs = []

        # Cinnamon-native tray icon: left click toggles the window, right click opens the menu.
        self.icon = XApp.StatusIcon()
        self.icon.set_name(APP_ID)
        self.icon.set_icon_name(os.path.join(HERE, "debrid-applet.svg"))
        self.icon.set_tooltip_text("Debrid")
        self.icon.connect("activate", self.toggle_window)
        menu = Gtk.Menu()
        for label, cb in (("Refresh", self.refresh), ("Settings…", self.open_settings),
                          ("Check for updates…", lambda *_: self.check_updates(True)), ("About…", self.open_about),
                          ("Quit", lambda *_: self.gapp.quit())):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", cb)
            menu.append(item)
        menu.show_all()
        self.icon.set_secondary_menu(menu)
        self.watcher = WatchFolder(self)
        self.feeds = FeedWatcher(self)
        self.api = None

        w, h = (self.cfg.get("window") or [1100, 560])[:2]
        self.window = Gtk.Window(title="Debrid", default_width=w, default_height=h)
        self.gapp.add_window(self.window)
        self.window.connect("delete-event", lambda w, _e: self.hide_window() or True)
        self.window.connect("key-press-event", self.on_key)
        self.clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        self.clipboard.connect("owner-change", lambda clip, _e: self.cfg.get("watch_clipboard") and clip.request_text(self.on_clipboard_text))
        self._last_clip = None
        self.notebook = Gtk.Notebook()
        self.window.add(self.notebook)
        self.build_tabs()

        if not any(self.cfg[k] for k in KEY_FIELDS):
            self.open_settings()
        self.refresh()  # every poll schedules the next one (see _schedule)
        self.watcher.start()
        self.feeds.start()
        self.start_api()
        GLib.timeout_add_seconds(60, lambda: self._check_updates_auto() and False)  # once, shortly after start
        GLib.timeout_add_seconds(86400, self._check_updates_auto)  # then daily while running

    def build_tabs(self):
        for page in list(self.notebook.get_children()):
            self.notebook.remove(page)
        self.tabs = [ServiceTab(cls.name, cls(self.cfg[key]) if self.cfg[key] else None, self) for cls, key in CLIENTS]
        if sum(1 for t in self.tabs if t.client) > 1:
            self.tabs.insert(0, ServiceTab("All", MergedClient(lambda: self.tabs), self))
        for tab in self.tabs:
            self.notebook.append_page(tab, Gtk.Label(label=tab.name))
        self.notebook.show_all()
        wanted = self.cfg.get("start_tab") or "All"
        self.notebook.set_current_page(next((i for i, t in enumerate(self.tabs) if t.name == wanted), 0))

    def current_tab(self):
        return self.tabs[self.notebook.get_current_page()]

    def apply_log_settings(self):
        LOG_SETTINGS["level"] = self.cfg.get("log_level") or "actions"
        LOG_SETTINGS["max"] = max(50, int(self.cfg.get("log_max_kb") or 1000)) * 1000

    def add_routed(self, items, default_tab):
        """Magnets Torbox already has go to Torbox; everything else to default_tab. Runs the cache check off-thread."""
        torbox = self.tab_named("Torbox")
        checker = torbox.client if torbox and torbox.client and torbox is not default_tab else None

        def done(routed, err):
            if err:
                default_tab.say(f"Cache check failed, adding here: {err}", error=True)
                routed = {True: [], False: items}
            if routed[True]:
                torbox.say(f"{len(routed[True])} cached magnet(s) routed here")
                torbox.add_items(routed[True])
            if routed[False]:
                default_tab.add_items(routed[False])
        run_async(lambda: route_items(items, checker), done)

    def tab_named(self, name):
        """The configured tab called name; None when there is no such tab or it has no key."""
        return next((t for t in self.tabs if t.name == name and t.client), None)

    def start_api(self):
        if self.api:
            self.api.stop()
            self.api = None
        port = int(self.cfg.get("http_port") or 0)
        if not port:
            return
        if not self.cfg.get("http_token"):
            self.cfg["http_token"] = secrets.token_urlsafe(24)
            save_config(self.cfg)

        def on_add(service, items):
            """Server thread: only check that a target exists; everything else hops to the main loop."""
            tab = self.tab_named(service) if service else next((t for t in self.tabs if t.client and not t.is_merged), None)
            if tab is None:
                return 0  # unknown or unconfigured service: refuse rather than guess

            def go():
                target = self.tab_named(tab.name) or tab  # Settings may have rebuilt the tabs meanwhile
                log_event(target.name, f"Local API: {len(items)} item(s) queued")
                if service:
                    target.add_items(list(items))
                else:  # no service named: cached-on-Torbox routing, else the first configured tab
                    self.add_routed(list(items), target)
            GLib.idle_add(go)
            return len(items)

        def status():
            return {t.name: {"active": t.active, "items": len(t.torrents)} for t in self.tabs if t.client and not t.is_merged}
        try:
            self.api = LocalApi(port, self.cfg["http_token"], on_add, lambda: GLib.idle_add(self.toggle_window), status)
            self.api.start()
            log_event("", f"Local API listening on 127.0.0.1:{port}")
        except OSError as e:
            self.api = None
            notify("Local API not started", f"Port {port}: {e}")

    def on_key(self, _w, event):
        tab = self.current_tab()
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        key = Gdk.keyval_to_lower(event.keyval)
        if key == Gdk.KEY_Escape and self.window.get_focus() is tab.search and tab.search.get_text():
            tab.search.set_text("")  # first Esc clears the filter, a second one hides the window
        elif key == Gdk.KEY_Escape:
            self.hide_window()
        elif ctrl and key == Gdk.KEY_f:
            tab.search.grab_focus()
        elif ctrl and key == Gdk.KEY_r:
            self.refresh()
        elif ctrl and key == Gdk.KEY_l:
            self.open_log()
        elif self.window.get_focus() is tab.view and key == Gdk.KEY_Delete:
            tab.on_delete(None)
        elif self.window.get_focus() is tab.view and ctrl and key == Gdk.KEY_a:
            tab.on_toggle_all(None)
        elif self.window.get_focus() is tab.view and ctrl and key == Gdk.KEY_c:
            tab.on_copy_links(None)
        else:
            return False
        return True

    def on_clipboard_text(self, _clip, text):
        """A magnet landed on the clipboard: offer to add it, one button per configured service."""
        text = (text or "").strip()
        if not text.lower().startswith("magnet:") or text == self._last_clip:
            return
        self._last_clip = text
        tabs = [t for t in self.tabs if t.client is not None]
        if not tabs:
            return
        notify("Magnet copied", add_label(text), [(f"Add to {t.name}", lambda t=t: t.add_items([text])) for t in tabs])

    def show_files(self, tab, t):
        """From a notification: raise the window on that tab and open the item's file panel."""
        self.window.show_all()
        self.window.present()
        self.notebook.set_current_page(self.tabs.index(tab))
        tab.open_files_for(t)

    def hide_window(self):
        self.cfg["window"] = list(self.window.get_size())
        save_config(self.cfg)
        self.window.hide()

    def toggle_window(self, *_):
        if self.window.is_visible():
            self.hide_window()
        else:
            self.window.show_all()
            self.window.present()

    def refresh(self, *_, scheduled=False):
        """Poll every configured service once; scheduled: the timer asked, not the user (see poll_kind). Returns True
        so GLib keeps the timer."""
        if self._polling:
            self._again = "light" if scheduled and self._again != "full" else "full"  # a poll is running; go again right after it
            return True
        self._polling, self._again = True, None
        tabs = [t for t in self.tabs if t.client is not None]
        for t in tabs:
            t.set_polling(True)

        fast, now = self._interval == POLL_FAST, time.monotonic()  # monotonic: a clock jump must not age or rejuvenate the lists

        def fetch():  # merged tabs are built from the other tabs' lists once those are rendered
            return poll_tabs([t for t in tabs if not t.is_merged], lambda t: poll_one(t, now, fast, scheduled))

        def done(results, _err):
            self._polling = False
            for t in tabs:
                t.set_polling(False)
            try:
                for t, acc, tors, e in results or []:
                    if t in self.tabs:  # not a tab that Settings replaced while the poll ran
                        t.render(acc, tors, e)
                for t in self.tabs:
                    if t.is_merged and t.get_mapped():  # no network: merges the lists rendered just above
                        t.render(t.client.account(), t.client.torrents(), None)
                    elif t.is_merged:  # off screen: rebuilt on map (_refill); 12k copies not made for nobody
                        t._stale = True
            finally:  # a failing render must never stop the polling cycle
                active = sum(t.active for t in self.tabs if not t.is_merged)  # the All tab mirrors the others
                self.icon.set_label(f"{active}↓" if active else "")
                self.icon.set_tooltip_text(f"Debrid · {active} item(s) still downloading · polling every {self._interval} s")
                self._schedule(POLL_FAST if active or any(t._pending for t in self.tabs) else POLL_SLOW)
                trim_memory()
                if self._again:
                    self.refresh(scheduled=self._again == "light")
        run_async(fetch, done)
        return True

    def _schedule(self, seconds):
        """Adaptive polling: fast while downloads run, slow when idle. Replaces any pending timer."""
        if self._timer:
            GLib.source_remove(self._timer)
        self._interval = seconds
        self._timer = GLib.timeout_add_seconds(seconds, self._tick)
        for t in self.tabs:
            t.set_poll_interval(seconds)

    def _tick(self):
        self._timer = 0
        self.refresh(scheduled=True)
        return False

    def set_columns(self, tab, order=None, hide=None, sort=None, widths=None):
        """Persist one tab's column order / visibility / sort / widths and re-apply it there."""
        cols = column_layout(self.cfg.setdefault("columns", {}), tab.name)
        if order is not None:
            cols["order"] = order
        if widths is not None:
            cols["widths"] = widths
        if hide is not None:
            key, hidden = hide
            cols["hidden"] = sorted((set(cols.get("hidden") or []) | {key}) - (set() if hidden else {key}))
        if sort is not None:
            cols["sort"] = sort
        save_config(self.cfg)
        tab.apply_columns()

    def open_settings_once(self):
        key = tuple(self.cfg[k] for k in KEY_FIELDS)
        if self._settings_shown_for != key:
            self._settings_shown_for = key
            self.open_settings()

    # ----- updates -----
    def _dialog(self, title, text):
        dlg = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.INFO,
                                buttons=Gtk.ButtonsType.CLOSE, text=title)
        dlg.format_secondary_text(text)
        dlg.run()
        dlg.destroy()

    def _ask(self, text, yes_label):
        dlg = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.QUESTION, text=text)
        dlg.add_buttons("Not now", Gtk.ResponseType.NO, yes_label, Gtk.ResponseType.YES)
        dlg.set_default_response(Gtk.ResponseType.YES)
        answer = dlg.run()
        dlg.destroy()
        return answer == Gtk.ResponseType.YES

    def _skip(self, v):
        self.cfg["update_skip"] = v
        save_config(self.cfg)

    def _update_dialog(self, release, v):
        dlg = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.INFO,
                                text=f"Debrid Applet {v} is available")
        dlg.format_secondary_text(f"You have {VERSION}. Update downloads the release and installs it"
                                  + (" (one password prompt)." if install_mode(HERE) == "deb" else "."))
        dlg.add_buttons("Release notes", 1, "Close", Gtk.ResponseType.CLOSE, "Update", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        answer = dlg.run()
        dlg.destroy()
        if answer == 1:
            open_url(release.notes_url)
        elif answer == Gtk.ResponseType.OK:
            self.update_to(release)

    def check_updates(self, manual=True):
        """Ask GitHub for the latest release. Manual checks always report in a dialog; automatic ones only notify
        about a newer version the user has not skipped, and log everything else at verbose level."""
        if self._installed:  # already installed this session; only the restart is missing, no need to ask GitHub
            if manual and self._ask(f"Debrid Applet {self._installed} is installed. Restart now?", "Restart"):
                self.restart()
            return

        def done(release, err):
            if err:
                if manual:
                    self._dialog("Update check failed", err)
                else:
                    log_event("", f"Update check: {err}", "verbose")
                return
            if release is None:
                if manual:
                    self._dialog("Up to date", f"Debrid Applet {VERSION} is the latest version.")
                else:
                    log_event("", "Update check: up to date", "verbose")
                return
            v = ".".join(map(str, release.version))
            log_event("", f"Update check: {v} available")
            if manual:
                self._update_dialog(release, v)
            elif v != self.cfg.get("update_skip"):
                notify(f"Debrid Applet {v} available", f"You have {VERSION}.",
                       [("Update", lambda: self.update_to(release)), ("Release notes", lambda: open_url(release.notes_url)),
                        ("Skip this version", lambda: self._skip(v))])
        run_async(lambda: latest_release(VERSION), done)

    def _check_updates_auto(self):
        if self.cfg.get("check_updates"):
            self.check_updates(manual=False)
        return True

    def update_to(self, release):
        """Download the asset for this kind of install and install it; the user confirms the restart.
        .deb: pkexec + apt-get (one password prompt). Per-user copy: files replaced in place. Git checkout: told to pull."""
        v = ".".join(map(str, release.version))
        mode = install_mode(HERE)
        tab = self.current_tab()
        if self._updating:  # a second Update click while the first runs would download onto the same file
            tab.say(f"Update to {self._updating} already in progress")
            return
        if self._installed:  # installed earlier this session: offer the restart again instead of a second download
            if self._ask(f"Debrid Applet {self._installed} is installed. Restart now?", "Restart"):
                self.restart()
            return
        if mode == "git":
            self._dialog(f"Debrid Applet {v} is available", f"This is a git checkout: run git pull in {HERE}.")
            return
        asset, sig = (release.deb, release.deb_sig) if mode == "deb" else (release.tar, release.tar_sig)
        if asset is None:
            self._dialog("No package in this release", f"Release {v} has no {'.deb' if mode == 'deb' else 'tarball'} for this install.")
            return
        if sig is None:
            self._dialog("Update failed", f"Release {v} is not signed; not installing.")
            return
        updates = os.path.join(DATA_DIR, "updates")
        self._updating = v
        tab.say(f"Downloading update {v}…")

        def fetch():  # the file and its signature, checked before anything else touches them
            path = download(asset, updates)
            verify_signature(path, download(sig, updates), v)
            return path

        def installed(err=None, path=None):
            self._updating = ""
            if err:
                tab.say(f"Update failed: {err}", error=True)
                self._dialog("Update failed", err)
                return
            for leftover in ((path, path + ".sig") if path else ()):
                try:
                    os.unlink(leftover)
                except OSError:
                    pass
            self._installed = v
            tab.say(f"Debrid Applet {v} installed")
            if self._ask(f"Debrid Applet {v} is installed. Restart now?", "Restart"):
                self.restart()
            else:  # the window may be hidden and automatic checks stay quiet now: leave one reminder with a button
                notify(f"Debrid Applet {v} installed", "Restart to use it.", [("Restart", self.restart)])

        def after_download(path, err):
            if err:
                installed(err)
                return
            if mode == "user":
                run_async(lambda: install_user(path, HERE), lambda _r, e: installed(e, path))
                return
            tab.say(f"Installing update {v}… (password prompt)")
            log_path = os.path.join(updates, "apt.log")
            try:
                with open(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab") as log:
                    proc = subprocess.Popen(deb_command(path, asset.digest), stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            except OSError as e:
                installed(f"cannot run pkexec: {e.strerror}")
                return

            def finished(code):
                if code == 0:
                    installed(None, path)
                    return
                self._updating = ""
                if code == 126:  # pkexec: the prompt was dismissed
                    tab.say("Update cancelled: authentication did not complete", error=True)
                elif code == 127:  # pkexec: not authorised (wrong password, no polkit agent, not an admin); the
                    tab.say("Update failed: not authorised", error=True)  # window may be hidden, so also notify
                    notify(f"Debrid Applet {v} not installed",
                           f"Authentication failed. Try again from the tray menu, or run: sudo apt install {path}")
                else:
                    tab.say(f"Update failed: apt-get exited {code}, see {log_path}", error=True)
                    if self._ask(f"apt-get exited with code {code} (details in {log_path}).\n"
                                 "Copy the command to install it yourself?", "Copy sudo command"):
                        copy_text(f"sudo apt install {shlex.quote(path)}")
            threading.Thread(target=lambda: GLib.idle_add(finished, proc.wait()), daemon=True).start()
        run_async(fetch, after_download)

    def restart(self):
        """Quit and let main() exec this file again: same PID, the bus name is freed by the exec, so the new
        process becomes the primary instance instead of being forwarded to a dying one."""
        self.hide_window()
        self.restart_requested = True
        self.gapp.quit()

    def open_about(self, *_):
        dlg = Gtk.AboutDialog(transient_for=self.window, modal=True, program_name="Debrid Applet", version=VERSION,
                              comments="Tray control panel for Torbox, Real-Debrid and AllDebrid.",
                              website=HOMEPAGE, website_label="GitHub: xb00tt/debrid-applet",
                              license_type=Gtk.License.MIT_X11, authors=["xb00tt"], logo_icon_name=APP_ID)
        dlg.add_button("Check for updates", 1)
        dlg.connect("response", lambda d, r: (d.destroy(), self.check_updates(True)) if r == 1 else d.destroy())
        dlg.show()

    def open_log(self, *_):
        """Human-readable activity log: every status message and notification, newest at the bottom."""
        dlg = Gtk.Dialog(title="Activity log", transient_for=self.window, default_width=900, default_height=500)
        box = dlg.get_content_area()
        box.set_property("margin", 8)
        view = Gtk.TextView(editable=False, monospace=True, left_margin=6, right_margin=6, cursor_visible=False)
        scroller = Gtk.ScrolledWindow(vexpand=True, shadow_type=Gtk.ShadowType.IN)
        scroller.add(view)
        box.pack_start(scroller, True, True, 0)

        def load(*_):
            text = read_log_tail(LOG_FILE) or "(nothing logged yet)"
            view.get_buffer().set_text(text)
            GLib.idle_add(lambda: view.scroll_to_iter(view.get_buffer().get_end_iter(), 0, False, 0, 1) and False)

        def clear(*_):
            try:
                os.truncate(LOG_FILE, 0)
            except OSError:
                pass
            load()
        actions = Gtk.Box(spacing=6)
        actions.pack_start(text_button("view-refresh-symbolic", "Refresh", load), False, False, 0)
        actions.pack_start(text_button("document-open-symbolic", "Open file", lambda *_: subprocess.Popen(["xdg-open", LOG_FILE])), False, False, 0)
        actions.pack_start(text_button("edit-clear-symbolic", "Clear", clear), False, False, 0)
        box.pack_start(actions, False, False, 0)
        dlg.add_button("Close", Gtk.ResponseType.CLOSE)
        dlg.connect("response", lambda d, _r: d.destroy())
        dlg.show_all()
        load()

    def open_settings(self, *_):
        dlg = Gtk.Dialog(title="Debrid settings", transient_for=self.window, modal=True, default_width=720)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        form = SettingsForm(self.cfg)
        dlg.get_content_area().pack_start(form.widget, True, True, 0)
        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            self.cfg.update(form.values())
            self.apply_log_settings()
            save_config(self.cfg)
            self.build_tabs()
            self.refresh()
            self.watcher.start()
            self.feeds.start()
            self.start_api()
        dlg.destroy()


class SettingsForm:
    """The Settings pages. widget is a Gtk.Notebook; values() returns the edited config fields."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.entries, self.checks, self.combos, self.spins, self.texts = {}, {}, {}, {}, {}
        self.widget = Gtk.Notebook()
        self._accounts()
        self._behaviour()
        self._automation()
        self._advanced()

    # -- building blocks
    def _page(self, title):
        grid = Gtk.Grid(row_spacing=8, column_spacing=10, margin=14)
        grid.set_column_homogeneous(False)
        self.widget.append_page(grid, Gtk.Label(label=title))
        grid._row = 0
        return grid

    def _heading(self, grid, text):
        label = Gtk.Label(xalign=0, use_markup=True, label=f"<b>{GLib.markup_escape_text(text)}</b>")
        label.set_margin_top(6 if grid._row else 0)
        grid.attach(label, 0, grid._row, 2, 1)
        grid._row += 1

    def _entry(self, grid, key, label, secret=False, hint=""):
        entry = Gtk.Entry(text=str(self.cfg.get(key) or ""), visibility=not secret, width_chars=40, hexpand=True)
        if hint:
            entry.set_placeholder_text(hint)
        grid.attach(Gtk.Label(label=label, xalign=0), 0, grid._row, 1, 1)
        grid.attach(entry, 1, grid._row, 1, 1)
        grid._row += 1
        self.entries[key] = entry
        return entry

    def _check(self, grid, key, label, tooltip="", sensitive=True):
        check = Gtk.CheckButton(label=label, active=bool(self.cfg.get(key)), sensitive=sensitive)
        if tooltip:
            check.set_tooltip_text(tooltip)
        grid.attach(check, 0, grid._row, 2, 1)
        grid._row += 1
        self.checks[key] = check
        return check

    def _combo(self, grid, key, label, options):
        combo = Gtk.ComboBoxText()
        for value, text in options:
            combo.append(value, text)
        if not combo.set_active_id(str(self.cfg.get(key) or "")):  # unset or unlisted value: the real default, as the tabs do
            combo.set_active_id(EMPTY_CONFIG.get(key) or options[0][0])
        grid.attach(Gtk.Label(label=label, xalign=0), 0, grid._row, 1, 1)
        grid.attach(combo, 1, grid._row, 1, 1)
        grid._row += 1
        self.combos[key] = combo
        return combo

    def _spin(self, grid, key, label, lo, hi, step=1, tooltip=""):
        spin = Gtk.SpinButton.new_with_range(lo, hi, step)
        spin.set_value(int(self.cfg.get(key) or 0))
        if tooltip:
            spin.set_tooltip_text(tooltip)
        grid.attach(Gtk.Label(label=label, xalign=0), 0, grid._row, 1, 1)
        grid.attach(spin, 1, grid._row, 1, 1)
        grid._row += 1
        self.spins[key] = spin
        return spin

    def _text(self, grid, key, label, height=80):
        view = Gtk.TextView(wrap_mode=Gtk.WrapMode.NONE, left_margin=6, right_margin=6, top_margin=4, bottom_margin=4)
        view.get_buffer().set_text(self.cfg.get(key) or "")
        scroller = Gtk.ScrolledWindow(shadow_type=Gtk.ShadowType.IN, min_content_height=height, hexpand=True)
        scroller.add(view)
        grid.attach(Gtk.Label(label=label, xalign=0, yalign=0), 0, grid._row, 1, 1)
        grid.attach(scroller, 1, grid._row, 1, 1)
        grid._row += 1
        self.texts[key] = view
        return view

    def _note(self, grid, text):
        label = Gtk.Label(xalign=0, wrap=True, label=text)
        label.get_style_context().add_class("dim-label")
        grid.attach(label, 0, grid._row, 2, 1)
        grid._row += 1
        return label

    # -- pages
    def _accounts(self):
        g = self._page("Accounts")
        self._heading(g, "API keys")
        self._entry(g, "torbox_key", "Torbox", secret=True, hint="torbox.app → Settings → API")
        self._entry(g, "realdebrid_key", "Real-Debrid", secret=True, hint="real-debrid.com/apitoken")
        self._entry(g, "alldebrid_key", "AllDebrid", secret=True, hint="alldebrid.com/apikeys")
        test = Gtk.Button(label="Test keys")
        test.set_halign(Gtk.Align.START)
        g.attach(test, 1, g._row, 1, 1)
        g._row += 1
        result = self._note(g, "Keys stay on this machine in ~/.config/debrid-applet/config.json (mode 600).")
        test.connect("clicked", lambda _b: self.test_keys(result))

    def _behaviour(self):
        g = self._page("Behaviour")
        self._heading(g, "Notifications")
        self._check(g, "notify_added", "When a torrent is added")
        self._check(g, "notify_done", "When a download finishes on the service")
        self._heading(g, "Convenience")
        self._combo(g, "start_tab", "Tab on start-up", [("All", "All")] + [(s, s) for s in SERVICES])
        self._check(g, "watch_clipboard", "Watch the clipboard for magnet links")
        self._check(g, "remember_filter", "Remember the state filter between runs")
        self._check(g, "service_full_names", "Full service names in the All tab (otherwise TB / RD / AD)")
        self._combo(g, "time_filter", "Show items added within", list(TIME_FILTERS))
        self._heading(g, "Playback")
        self._check(g, "dblclick_play", "Double-click plays videos (otherwise opens Files)")
        self._entry(g, "player_command", "Custom play command", hint="stremio {url}   or   mpv --fs {url} --sub-file={sub}")
        self._note(g, "Empty = the first of mpv, VLC, Celluloid, SMPlayer, Totem found on this machine.")

    def _automation(self):
        g = self._page("Automation")
        self._heading(g, "Watch folder")
        self._entry(g, "watch_dir", "Folder", hint="~/Downloads/torrents")
        self._combo(g, "watch_service", "Upload new .torrent files to", [(s, s) for s in SERVICES])
        self._heading(g, "RSS feeds")
        self._text(g, "feeds", "One per line:\nurl | regex | service")
        self._spin(g, "feed_interval_min", "Check every (minutes)", 1, 1440)
        self._heading(g, "When an item finishes")
        self._check(g, "auto_download", "Download it automatically (aria2c, else wget)")
        self._check(g, "auto_download_videos_only", "    …video files only")
        self._check(g, "delete_after_download", "    …then delete it from the service")
        self._entry(g, "download_dir", "Download folder", hint="~/Downloads")
        self._check(g, "stop_seeding_when_done", "Torbox: stop seeding right away")

    def _advanced(self):
        g = self._page("Advanced")
        self._heading(g, "Local API")
        self._spin(g, "http_port", "Port (0 = off)", 0, 65535, tooltip="curl 'http://127.0.0.1:PORT/add?token=…&magnet=…'")
        token = Gtk.Entry(text=self.cfg.get("http_token") or "(generated when the port is set)", editable=False, hexpand=True)
        g.attach(Gtk.Label(label="Token", xalign=0), 0, g._row, 1, 1)
        g.attach(token, 1, g._row, 1, 1)
        g._row += 1
        self._note(g, "Every call needs ?token=… or an X-Debrid-Token header. Delete http_token in config.json for a new one.")
        self._heading(g, "Updates")
        self._check(g, "check_updates", "Check for updates daily (GitHub releases)")
        self._heading(g, "Activity log")
        self._combo(g, "log_level", "Level", list(LOG_LEVELS))
        self._spin(g, "log_max_kb", "Size limit (KB)", 50, 100000, 50)
        self._note(g, f"File: {LOG_FILE}\nDebrid Applet {VERSION}")

    # -- results
    def values(self):
        out = {k: e.get_text().strip() for k, e in self.entries.items()}
        out.update({k: c.get_active() for k, c in self.checks.items()})
        out.update({k: (c.get_active_id() or "") for k, c in self.combos.items()})
        out.update({k: int(s.get_value()) for k, s in self.spins.items()})
        for k, v in self.texts.items():
            buf = v.get_buffer()
            out[k] = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        return out

    def test_keys(self, result):
        clients = [(cls, self.entries[key].get_text().strip()) for cls, key in CLIENTS]

        def work():
            parts = []
            for cls, token in clients:
                if not token:
                    continue
                try:
                    a = cls(token).account()
                    parts.append((cls.name, f"{a['plan']}, expires {a['expires'] or '—'}", False))
                except ApiError as e:
                    parts.append((cls.name, str(e), True))
            return parts

        def show(parts, err):
            if err:
                result.set_markup(red(err))
                return
            result.set_markup("\n".join(
                (red(f"{n}: {m}") if bad else f"{GLib.markup_escape_text(n)}: {GLib.markup_escape_text(m)}")
                for n, m, bad in parts) or "No keys entered")
        result.set_text("Testing…")
        run_async(work, show)


class FeedWatcher:
    """Polls RSS feeds from cfg['feeds'] every cfg['feed_interval_min'] minutes; new matching items are added."""

    def __init__(self, app):
        self.app = app
        self._timer = self._first = 0

    def start(self):
        for timer in (self._timer, self._first):  # saving Settings twice must not leave two passes running
            if timer:
                GLib.source_remove(timer)
        self._timer = self._first = 0
        feeds = parse_feed_lines(self.app.cfg.get("feeds"))
        if not feeds:
            return
        minutes = max(1, int(self.app.cfg.get("feed_interval_min") or 15))
        self._timer = GLib.timeout_add_seconds(minutes * 60, self._tick)
        self._first = GLib.timeout_add_seconds(5, self._first_pass)  # first pass shortly after start

    def _tick(self):
        self.check()
        return True

    def _first_pass(self):
        self._first = 0
        self.check()
        return False

    def check(self):
        feeds = parse_feed_lines(self.app.cfg.get("feeds"))
        seen = dict.fromkeys(self.app.cfg.get("feed_seen") or [])  # insertion-ordered, so the trim below drops the oldest

        def work():
            found, errors = [], []
            for url, pattern, service in feeds:
                try:  # feeds are typed into Settings by the user, so local servers are allowed; size is capped
                    text = fetch_bytes(url, FEED_MAX, allow_private=True).decode("utf-8", "replace")
                except ApiError as e:
                    errors.append(f"{urlparse(url).hostname}: {e}")
                    continue
                for guid, title, link in parse_feed(text):
                    key = f"{url}#{guid}"
                    if key in seen or not feed_matches(title, pattern):
                        continue
                    low = link.lower()
                    if not (low.startswith("magnet:") or (is_http(link) and low.split("?")[0].endswith(".torrent"))):
                        continue  # plain page links can't be added; a local path must not come from a feed
                    found.append((key, title, link, service))
            return found, errors

        def done(res, err):
            if err:
                log_event("", f"RSS: {err}", "error")
                return
            found, errors = res
            for e in errors:
                log_event("", f"RSS: {e}", "error")
            if not found:
                log_event("", f"RSS: checked {len(feeds)} feed(s), nothing new", "verbose")
                return
            by_tab = {}
            for key, title, link, service in found:
                tab = self.app.tab_named(service) if service else next((t for t in self.app.tabs if t.client and not t.is_merged), None)
                if tab is None:  # named service without a key: left unseen, picked up once the key is set
                    log_event("", f"RSS: {service or 'no service'} is not configured, skipped {title}", "verbose")
                    continue
                by_tab.setdefault(tab, []).append(link)
                seen[key] = None
                log_event(tab.name, f"RSS: {title}")
            if not by_tab:
                return
            self.app.cfg["feed_seen"] = list(seen)[-2000:]
            save_config(self.app.cfg)
            for tab, links in by_tab.items():
                tab.add_items(links)
            notify("RSS", f"{sum(len(l) for l in by_tab.values())} new item(s) matched")
        run_async(work, done)
        return True


class WatchFolder:
    """Uploads every .torrent that appears in cfg['watch_dir'] to cfg['watch_service'], then moves it to done/."""

    def __init__(self, app):
        self.app = app
        self.monitor = None
        self.dir = ""
        self.busy = set()

    def start(self):
        self.stop()
        self.dir = os.path.expanduser(self.app.cfg.get("watch_dir") or "")
        if not self.dir or not os.path.isdir(self.dir):
            return
        self.monitor = Gio.File.new_for_path(self.dir).monitor_directory(Gio.FileMonitorFlags.NONE, None)
        self.monitor.connect("changed", self.on_change)
        for name in sorted(os.listdir(self.dir)):
            self.consider(os.path.join(self.dir, name))

    def stop(self):
        if self.monitor:
            self.monitor.cancel()
            self.monitor = None

    def on_change(self, _monitor, gfile, _other, event):
        if event in (Gio.FileMonitorEvent.CHANGES_DONE_HINT, Gio.FileMonitorEvent.MOVED_IN, Gio.FileMonitorEvent.CREATED):
            path = gfile.get_path()
            GLib.timeout_add(1500, lambda: self.consider(path) and False)  # let the writer finish first

    def consider(self, path):
        if not path or not path.lower().endswith(".torrent") or not os.path.isfile(path) or path in self.busy:
            return False
        tab = self.app.tab_named(self.app.cfg.get("watch_service"))
        if tab is None or tab.client is None:
            return False
        self.busy.add(path)
        label = add_label(path)
        log_event(tab.name, f"Watch folder: uploading {label}")

        def after(_added, errors):
            self.busy.discard(path)
            if any(e.startswith(label + ":") for e in errors):
                return  # left in place; fix the file or the key and it is retried on next start
            done = os.path.join(self.dir, "done")
            os.makedirs(done, exist_ok=True)
            try:
                shutil.move(path, os.path.join(done, os.path.basename(path)))
            except OSError as e:
                tab.say(f"Watch folder: could not move {label}: {e.strerror}", error=True)
        tab.add_items([path], after=after)
        return True


def main():
    """One instance only: a second launch is forwarded to the running one. With no flags it raises the window;
    --toggle, --log, --service NAME and --add ITEM… drive it from the shell or a hotkey."""
    if "--version" in sys.argv:  # answered locally, no need to reach a running instance
        print(f"debrid-applet {VERSION}")
        return
    gapp = Gtk.Application(application_id=DBUS_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
    state = {}

    def on_cmdline(a, cmdline):
        opts = parse_cli(cmdline.get_arguments()[1:])
        first = "app" not in state
        if first:
            state["app"] = App(a)
            a.hold()  # keep running while the window is hidden
            log_event("", f"Debrid Applet {VERSION} started")
        app = state["app"]
        if opts["toggle"]:
            app.toggle_window()
        elif not first and not opts["items"] and not opts["log"]:
            app.window.show_all()
            app.window.present()
        if opts["log"]:
            app.open_log()
        if opts["items"]:
            tab = app.tab_named(opts["service"]) if opts["service"] else next((t for t in app.tabs if t.client and not t.is_merged), None)
            if tab is None:
                log_event("", f"Command line: {opts['service'] or 'no service'} is not configured, {len(opts['items'])} item(s) dropped", "error")
                return 1
            log_event(tab.name, f"Command line: {len(opts['items'])} item(s)")
            if opts["service"]:
                tab.add_items(opts["items"])
            else:
                app.add_routed(opts["items"], tab)
        return 0
    gapp.connect("command-line", on_cmdline)
    gapp.run(sys.argv)
    if "app" in state and state["app"].restart_requested:  # an update was installed: become the new version
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])


if __name__ == "__main__":
    main()
