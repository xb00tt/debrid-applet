#!/usr/bin/env python3
"""Tray control panel for Torbox and Real-Debrid."""
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
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, quote, urlparse

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("Notify", "0.7")
gi.require_version("XApp", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Notify, Pango, XApp  # noqa: E402
try:
    gi.require_version("Secret", "1")
    from gi.repository import Secret  # noqa: E402
except (ValueError, ImportError):  # no libsecret: the keyring option stays greyed out
    Secret = None

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from debrid_api import AllDebrid, ApiError, RealDebrid, Torbox, human_size, magnet_hash  # noqa: E402
from debrid_local_api import LocalApi  # noqa: E402

APP_ID = "debrid-applet"  # WM_CLASS, icon name and .desktop basename all match
DBUS_ID = "io.github.xb00tt.DebridApplet"  # single-instance guard
PLAYERS = ("mpv", "vlc", "celluloid", "smplayer", "totem")
SUB_FLAG = {"mpv": "--sub-file={}", "celluloid": "--mpv-sub-file={}", "smplayer": "-sub {}", "vlc": "--sub-file={}"}
RESUME_FLAG = {"mpv": "--save-position-on-quit", "celluloid": "--mpv-save-position-on-quit"}  # resume where you stopped
SUB_EXT = (".srt", ".ass", ".vtt", ".sub")
SERVICES = ("Torbox", "Real-Debrid", "AllDebrid")
DASHBOARDS = {"Torbox": "https://torbox.app/dashboard", "Real-Debrid": "https://real-debrid.com/torrents",
              "AllDebrid": "https://alldebrid.com/magnets"}
SERVICE_COLORS = {"Torbox": "#e8761b", "Real-Debrid": "#3b82f6", "AllDebrid": "#a855f7"}  # Service column on the All tab
EXPIRY_WARN_DAYS = 7
VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".webm", ".mov", ".ts", ".wmv", ".flv")
GLib.set_prgname(APP_ID)

CONFIG_DIR = os.path.expanduser("~/.config/debrid-applet")
CONFIG = os.path.join(CONFIG_DIR, "config.json")
LOG_FILE = os.path.join(os.path.expanduser("~/.local/share/debrid-applet"), "activity.log")
LOG_MAX = 1_000_000  # default bytes; trimmed to the newest fifth when exceeded
LOG_LEVELS = (("off", "Off"), ("errors", "Errors only"), ("actions", "Actions and notifications"),
              ("verbose", "Verbose (also every poll)"))
LOG_SETTINGS = {"level": "actions", "max": LOG_MAX}  # mirrored from the config by App
EMPTY_CONFIG = {"torbox_key": "", "realdebrid_key": "", "alldebrid_key": "", "notify_added": True, "notify_done": True,
                "watch_clipboard": True, "columns": {}, "window": [],  # columns: per-service, see column_layout()
                "remember_filter": True, "filters": {},  # filters: last state-filter choice per service
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
                "config_version": 2,  # bumped when the layout changes; see migrate_config()
                "notified_expiry": {}}  # service -> expiry date already warned about
POLL_FAST, POLL_SLOW = 8, 60  # seconds between polls while something is active / when everything is idle
INVALID_KEY = "Invalid API key"


KEY_FIELDS = ("torbox_key", "realdebrid_key", "alldebrid_key")


def _import_keys_from_keyring(cfg):
    """One-time transition from the removed keyring option: copy any key the keyring still holds into the file
    and delete it there. Silently does nothing without libsecret or without entries."""
    if not Secret:
        return False
    try:
        schema = Secret.Schema.new("io.github.xb00tt.DebridApplet", Secret.SchemaFlags.NONE,
                                   {"key": Secret.SchemaAttributeType.STRING})
        moved = False
        for name in KEY_FIELDS:
            value = Secret.password_lookup_sync(schema, {"key": name}, None)
            if value:
                if not cfg.get(name):
                    cfg[name] = value
                    moved = True
                Secret.password_clear_sync(schema, {"key": name}, None)
        return moved
    except GLib.Error:
        return False


def migrate_config(cfg):
    """Bring a raw config dict (as read from disk, before defaults) up to the current layout. Idempotent."""
    version = int(cfg.get("config_version") or 1)
    cols = cfg.get("columns") or {}
    if version < 2 and any(k in cols for k in ("order", "hidden", "sort")):  # one shared layout -> one per service
        for name in SERVICES:
            column_layout(cols, name)
        cfg["columns"] = cols
    cfg.pop("keys_in_keyring", None)  # option removed
    cfg["config_version"] = EMPTY_CONFIG["config_version"]
    return cfg


def load_config():
    try:
        with open(CONFIG) as f:
            cfg = {**EMPTY_CONFIG, **migrate_config(json.load(f))}
    except (OSError, ValueError):
        return dict(EMPTY_CONFIG)
    if _import_keys_from_keyring(cfg):
        save_config(cfg)
    return cfg


def save_config(cfg):
    """Write config.json with mode 600 in a 700 directory. Keys are kept here and nowhere else."""
    on_disk = {k: v for k, v in cfg.items() if not k.startswith("_")}
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(on_disk, f)


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


def add_label(text):
    """Short human name for a just-added magnet, file path or link (for notifications)."""
    if text.lower().startswith("magnet:"):
        dn = parse_qs(urlparse(text).query).get("dn")
        if dn:
            return dn[0][:60]
    return (os.path.basename(text.rstrip("/")) or text)[:60]


def open_url(url):
    """Hand an http(s) URL to the browser. Anything else (file:, javascript:, flags) is refused."""
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        return False
    subprocess.Popen(["xdg-open", url])
    return True


def add_one(client, item):
    """Route one magnet, http(s) link or .torrent path to the client. Returns a direct URL if given one."""
    low = item.lower()
    if low.startswith("magnet:"):
        client.add_magnet(item)
    elif low.startswith(("http://", "https://")) and low.split("?")[0].endswith(".torrent"):
        data = fetch_bytes(item, TORRENT_MAX)  # a .torrent URL: fetch it (public hosts only), upload the file
        with tempfile.NamedTemporaryFile(suffix=".torrent", delete=False) as f:
            f.write(data)
        try:
            client.add_torrent_file(f.name)
        finally:
            os.unlink(f.name)
    elif low.startswith(("http://", "https://")):
        return client.add_link(item)
    elif low.endswith(".torrent"):
        client.add_torrent_file(item)
    else:
        raise ApiError("not a magnet, link or .torrent")
    return None


def find_player():
    return next((p for p in PLAYERS if shutil.which(p)), None)


def video_links(links):
    """Playable entries from a links() result: video files first, else any real file but the zip bundle."""
    usable = [l for l in links if l["url"] and not l["name"].startswith("All files")]
    return [l for l in usable if l["name"].lower().endswith(VIDEO_EXT)] or usable


def days_until(date_str):
    """Days from today to a YYYY-MM-DD date; None when empty or unparsable."""
    try:
        return (date.fromisoformat((date_str or "")[:10]) - date.today()).days
    except ValueError:
        return None


def pick_subtitle(video_name, links):
    """URL of the subtitle that matches the video's name; else the only subtitle; else None."""
    subs = [l for l in links if l.get("url") and l["name"].lower().endswith(SUB_EXT)]
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

    def torrents(self, active_only=False):
        out = []
        for tab in self._real():
            for t in tab.torrents:
                if active_only and t["done"]:
                    continue
                out.append({**t, "id": f"{tab.name}|{t['id']}", "service": tab.name})
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


def download_command(urls, dest, tool):
    """argv for a resumable bulk download into dest with aria2c (8 connections) or wget."""
    if tool == "aria2c":
        return ["aria2c", "-c", "-x", "8", "-s", "8", f"--dir={dest}", *urls]
    return ["wget", "-c", "-P", dest, *urls]


def safe_dirname(name):
    return re.sub(r"[^\w.\- \[\]()]", "_", name).strip()[:120] or "download"


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
    active = sum(1 for t in torrents if not t["done"])
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


def is_public_host(host):
    """False for loopback, private, link-local and unresolvable hosts: targets a fetch must not reach."""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return bool(infos)


def fetch_bytes(url, max_bytes, allow_private=False):
    """GET a URL with a hard size cap. Redirects are followed by hand so every hop gets the same checks:
    http(s) only, and no private/loopback hosts unless the caller trusts the URL (feeds typed into Settings)."""
    import requests
    from urllib.parse import urljoin
    try:
        for _hop in range(5):
            parts = urlparse(url)
            if parts.scheme not in ("http", "https"):
                raise ApiError(f"unsupported URL scheme in {url[:60]}")
            if not allow_private and not is_public_host(parts.hostname or ""):
                raise ApiError("refusing to fetch from a private or local address")
            with requests.get(url, timeout=20, stream=True, allow_redirects=False,
                              headers={"User-Agent": "debrid-applet"}) as r:
                if r.is_redirect or r.is_permanent_redirect:
                    url = urljoin(url, r.headers.get("Location", ""))
                    continue
                r.raise_for_status()
                data = b""
                for chunk in r.iter_content(65536):
                    data += chunk
                    if len(data) > max_bytes:
                        raise ApiError(f"{url} is larger than {max_bytes // 1_000_000} MB")
                return data
        raise ApiError("too many redirects")
    except requests.RequestException as e:
        raise ApiError(f"fetch failed: {e}") from e


def parse_feed(xml_text):
    """RSS/Atom items -> (guid, title, link) where link is a magnet, a .torrent URL or the item link."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
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
        if parts[0].lower().startswith(("http://", "https://")):
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


def merge_active(full, active):
    """Overlay an active-only poll onto the last full list. Returns (merged, need_full): need_full is True when
    something that was active is no longer in the active set (it finished or failed), so a full poll is due."""
    by_id = {t["id"]: t for t in active}
    merged = [by_id.pop(t["id"], t) for t in full]
    merged = list(by_id.values()) + merged  # brand-new active items go first
    was_active = {t["id"] for t in full if not t["done"]}
    now_active = {t["id"] for t in active}
    return merged, bool(was_active - now_active)


def strict_videos(links):
    """Only real video files with a URL (no fallback to other files)."""
    return [l for l in links if l["url"] and l["name"].lower().endswith(VIDEO_EXT)]


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
    """Shell command that downloads the URLs with aria2c, resumable, 8 connections each."""
    if not urls:
        return ""
    return f"aria2c -c -x 8 -s 8 --dir={shlex.quote(dest)} " + " ".join(shlex.quote(u) for u in urls)


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
    """One human-readable line per event; the file is trimmed to its newest fifth when it grows past max_bytes."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        if os.path.getsize(path) > max_bytes:
            with open(path, "rb") as f:
                f.seek(-max(1, max_bytes // 5), os.SEEK_END)
                tail = f.read().split(b"\n", 1)[-1]
            with open(path, "wb") as f:
                f.write(tail)
    except OSError:
        pass
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {service:<12} {message}\n")


def read_log_tail(path, lines=500):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
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
    note.show()
    if actions:
        _NOTES.append(note)
        del _NOTES[:-20]
    return note


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
ABSOLUTE = {"added": 20, "finished": 21}  # store columns holding the full local time for the tooltip
STORE_TYPES = (str, str, int, str, str, str, bool,  # 0 id, 1 name, 2 progress, 3 state, 4 size, 5 kind, 6 checked
               str, str, str, str, str, str, str,   # 7 dl, 8 ul, 9 seeds, 10 peers, 11 ratio, 12 added, 13 finished
               float, float, float, int, int, float,  # 14 size, 15 dl, 16 ul, 17 seeds, 18 peers, 19 ratio (raw)
               str, str, str, str)  # 20 added, 21 finished (absolute local time), 22 hash, 23 service
CHECKED, STATUS_HOLD = 6, 8
NUMERIC = ("dl", "ul", "seeds", "peers", "ratio", "size")  # right-aligned
WIDTHS = {"check": 28, "service": 95, "name": 120, "dl": 85, "ul": 85, "progress": 90, "seeds": 60, "peers": 60, "ratio": 60,
          "state": 140, "size": 85, "added": 115, "finished": 115}  # starting widths; Name grows to fill
PALETTE = {"active": "#3b82f6", "done": "#22c55e", "seed": "#14b8a6", "warn": "#f59e0b", "error": "#ef4444"}
CONTROL_LAG_NOTE = ("Torbox confirms at once but applies it on its own schedule; "
                    "the State column can keep its old value for a long while.")
STATE_FILTERS = (("all", "All states"), ("active", "Active"), ("done", "Done"), ("seed", "Seeding"),
                 ("warn", "Stalled"), ("error", "Error"))
CSS = b"""
treeview.view.progressbar { background-color: #e8761b; border-color: #e8761b; color: #ffffff; }
treeview.view.progressbar:selected { background-color: #ffffff; border-color: #ffffff; color: #e8761b; }
treeview.view.trough:selected { background-color: rgba(0, 0, 0, 0.25); color: #ffffff; }
treeview.view { font-feature-settings: 'tnum'; }
"""


DEFAULT_HIDDEN = {"Real-Debrid": ["ul", "peers", "ratio"], "AllDebrid": ["peers", "ratio"]}  # never filled by that service


def column_layout(columns, name):
    """Layout dict for one service inside cfg['columns']; migrates the old shared format in place."""
    if any(k in columns for k in ("order", "hidden", "sort")):
        flat = {k: columns.pop(k) for k in ("order", "hidden", "sort") if k in columns}
        for n in ("Torbox", "Real-Debrid"):
            columns.setdefault(n, json.loads(json.dumps(flat)))
    layout = columns.setdefault(name, {"order": [], "hidden": list(DEFAULT_HIDDEN.get(name, []))})
    layout.setdefault("sort", ["added", "desc"])  # newest first until the user picks another header
    return layout


def state_kind(state):
    """Bucket a raw service state string into a PALETTE key."""
    s = state.lower()
    if any(w in s for w in ("error", "dead", "virus")):
        return "error"
    if "stalled" in s or "paused" in s:
        return "warn"
    if any(w in s for w in ("downloaded", "completed", "cached")):
        return "done"
    if "upload" in s or "seed" in s:
        return "seed"
    if any(w in s for w in ("download", "metadl", "checking", "queue", "magnet", "waiting", "compressing", "adding")):
        return "active"
    return "done"  # store column of the checkbox; seconds an action message stays before polls overwrite it


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
        self._shown_key = None  # (filter text, state filter) the store was last built for
        self.errors = []  # (time, message) of the last few failures, shown as the status line's tooltip
        self.active = 0
        self._is_busy = False
        self._applying = True  # suppress columns-changed while we build or restore the layout
        self._hold_until = 0
        self._width_timer = 0  # debounce for saving dragged column widths
        self._updated_at = 0  # time of the last successful poll, shown in the footer
        self._last_full = 0  # time of the last full-list poll
        self._account = None  # last account dict, reused on active-only polls

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
                          ("Copy all magnets", self.on_copy_all_magnets)):
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
        center = Gtk.Box(spacing=6)
        center.pack_start(self.search, False, False, 0)
        center.pack_start(self.state_filter, False, False, 0)
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
        self.poll_label.set_text(f"updated {when} · every {getattr(self, '_poll_every', POLL_SLOW)} s")

    def _update_account(self, account):
        plan = GLib.markup_escape_text(account["plan"])
        self.account_label.set_markup(
            f'<span background="{plan_color(account["plan"])}" foreground="#ffffff"> {plan} </span>  '
            f"{GLib.markup_escape_text(expiry_text(account['expires']))} · {GLib.markup_escape_text(account['extra'])}")

    def _update_totals(self):
        visible_ids = {r[0] for r in self.store}
        self.totals_label.set_text(totals_text(self.torrents, len(self.store), len(self.checked & visible_ids)))

    # ----- column layout (shared by both tabs through the config) -----
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
        """Hovering a relative date shows the full local time."""
        if keyboard:
            return False
        hit = view.get_path_at_pos(*view.convert_widget_to_bin_window_coords(x, y))
        if not hit:
            return False
        path, col = hit[0], hit[1]
        key = next((k for k, c in self.cols.items() if c is col), None)
        idx = ABSOLUTE.get(key)
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

    def _run(self, fn, cb):
        """Run a network action; show errors in the status label; then cb(result, error)."""
        self._busy(True)
        self.status.set_text("Working…")

        def done(res, err):
            self._busy(False)
            if err:
                self.say(err, error=True)
                if err == INVALID_KEY:
                    self.app.open_settings_once()
            cb(res, err)
        run_async(fn, done)

    # ----- rendering -----
    def render(self, account, torrents, error):
        if error:
            self.account_label.set_markup(red(error))
            self.say(error, error=True)  # also logged as an error
            if error == INVALID_KEY:
                self.app.open_settings_once()
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
        self._fill()
        self.active = sum(1 for t in torrents if not t["done"])
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
            usable = [l for l in links if l["url"] and not l["name"].startswith("All files")]
            if self.app.cfg.get("auto_download_videos_only"):
                usable = [l for l in usable if l["name"].lower().endswith(VIDEO_EXT)] or usable
            urls = [l["url"] for l in usable]
            if not urls:
                self.say(f"Auto-download: no links for {t['name'][:40]}", error=True)
                return
            os.makedirs(dest, exist_ok=True)
            log = open(os.path.join(HERE, f"download-{safe_dirname(t['name'])[:40]}.log"), "ab")
            proc = subprocess.Popen(download_command(urls, dest, tool), stdout=log, stderr=log)
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
        ]

    def _fill(self):
        """Show the last poll through the filter box and state dropdown. Same filter as last time: update rows
        in place (keeps selection and scroll, cheap for thousands of rows); otherwise rebuild."""
        q = self.search.get_text().strip().lower()
        kind = self.state_filter.get_active_id() or "all"
        now = time.monotonic()
        self._pending = [(exp, row) for exp, row in self._pending if now < exp]
        rows = [t for t in [row for _exp, row in self._pending] + self.torrents  # placeholders first
                if not (q and q not in t["name"].lower() and q not in t["state"].lower())
                and not (kind != "all" and state_kind(t["state"]) != kind)]
        if self._shown_key == (q, kind) and self._iters:
            wanted = {t["id"] for t in rows}
            for tid in [tid for tid in self._iters if tid not in wanted]:
                self.store.remove(self._iters.pop(tid))
            for t in rows:
                values = self._row_values(t)
                it = self._iters.get(t["id"])
                if it is None:
                    self._iters[t["id"]] = self.store.append(values)
                    continue
                current = self.store[it]
                changed = [i for i, v in enumerate(values) if i != CHECKED and current[i] != v]
                if changed:
                    self.store.set(it, changed, [values[i] for i in changed])
        else:
            selected = self._selected()
            self.store.clear()
            self._iters = {}
            self._anchor = None
            for t in rows:
                self._iters[t["id"]] = self.store.append(self._row_values(t))
            if selected and selected["id"] in self._iters:
                self.view.get_selection().select_iter(self._iters[selected["id"]])
            self._shown_key = (q, kind)
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
                except Exception as e:  # requests errors while fetching a .torrent URL
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
                GLib.timeout_add_seconds(60, lambda: self.add_items(list(retry), None, attempt + 1) and False)
            self.say(msg, error=bool(errors))
            for name in added:  # show something at once; the real row replaces it on the next poll
                self._pending.append((time.monotonic() + 30, {
                    "id": f"pending:{name}", "name": name, "progress": 0, "state": "adding…", "done": False,
                    "size": 0, "seeds": 0, "speed": 0, "kind": "torrent", "up_speed": None, "peers": None,
                    "ratio": None, "added": datetime.now(timezone.utc).isoformat(), "finished": "", "hash": ""}))
            self._fill()
            for row in self.store:  # bring the first placeholder into view
                if row[0].startswith("pending:"):
                    self.view.scroll_to_cell(row.path, None, True, 0.0, 0.0)
                    break
            self.app.refresh()
            GLib.timeout_add_seconds(3, lambda: self.app.refresh() and False)  # services list new items with a lag
            if after:
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
            vids = strict_videos(links) if self.app.player else []
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
        urls = [u for u in urls if u and u.lower().startswith(("http://", "https://"))]
        if custom:
            for u in urls:  # one invocation per file; the target decides how to queue
                subprocess.Popen(custom_cmd(custom, u))
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
            if not url or not url.lower().startswith(("http://", "https://")):
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
        if not url or not url.lower().startswith(("http://", "https://")):
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
            return [r[3] for r in rows if r[3]]

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
                    (urls if link["url"] else errors).append(link["url"] or link["name"])
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
        """Torbox pause / resume / reannounce on every target."""
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
        self.lock = threading.Lock()
        self._refresh_again = False
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

        if not any(self.cfg[k] for k in ("torbox_key", "realdebrid_key", "alldebrid_key")):
            self.open_settings()
        self.refresh()  # every poll schedules the next one (see _schedule)
        self.watcher.start()
        self.feeds.start()
        self.start_api()

    def build_tabs(self):
        for page in list(self.notebook.get_children()):
            self.notebook.remove(page)
        self.tabs = [
            ServiceTab("Torbox", Torbox(self.cfg["torbox_key"]) if self.cfg["torbox_key"] else None, self),
            ServiceTab("Real-Debrid",
                       RealDebrid(self.cfg["realdebrid_key"]) if self.cfg["realdebrid_key"] else None, self),
            ServiceTab("AllDebrid", AllDebrid(self.cfg["alldebrid_key"]) if self.cfg["alldebrid_key"] else None, self),
        ]
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
        return next((t for t in self.tabs if t.name == name), None)

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
            tab = self.tab_named(service) or next((t for t in self.tabs if t.client and not t.is_merged), None)
            if tab is None or tab.client is None:
                return 0
            log_event(tab.name, f"Local API: {len(items)} item(s) queued")
            if service:
                GLib.idle_add(tab.add_items, list(items))
            else:  # no service named: cached-on-Torbox routing, else the first configured tab
                GLib.idle_add(self.add_routed, list(items), tab)
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

    def refresh(self, *_):
        """Poll every configured service once. Returns True so GLib keeps the timer."""
        if not self.lock.acquire(blocking=False):
            self._refresh_again = True  # a poll is running; go again right after it
            return True
        self._refresh_again = False
        tabs = [t for t in self.tabs if t.client is not None]
        for t in tabs:
            t.set_polling(True)

        fast = self._interval == POLL_FAST
        now = time.time()

        def fetch_one(t):
            """Full poll, or on the fast cadence an active-only poll merged into the cached list."""
            light = (fast and t._account is not None and t.torrents and type(t.client) is not Torbox
                     and now - t._last_full < POLL_SLOW)
            if light:
                merged, need_full = merge_active(t.torrents, t.client.torrents(active_only=True))
                if not need_full:
                    return t._account, merged
            account, torrents = t.client.account(), t.client.torrents()
            t._last_full = now
            return account, torrents

        def fetch():
            results = []
            for t in tabs:
                try:
                    results.append((t, *fetch_one(t), None))
                except ApiError as e:
                    results.append((t, None, None, str(e)))
                except Exception as e:
                    results.append((t, None, None, f"{type(e).__name__}: {e}"))
            return results

        def done(results, _err):
            self.lock.release()
            for t in tabs:
                t.set_polling(False)
            for t, acc, tors, e in results or []:
                t.render(acc, tors, e)
            active = sum(t.active for t in self.tabs if not t.is_merged)  # the All tab mirrors the others
            self.icon.set_label(f"{active}↓" if active else "")
            self.icon.set_tooltip_text(f"Debrid · {active} item(s) still downloading · polling every {self._interval} s")
            self._schedule(POLL_FAST if active or any(t._pending for t in self.tabs) else POLL_SLOW)
            if self._refresh_again:
                self.refresh()
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
        self.refresh()
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
        key = (self.cfg["torbox_key"], self.cfg["realdebrid_key"])
        if self._settings_shown_for != key:
            self._settings_shown_for = key
            self.open_settings()

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
                open(LOG_FILE, "w").close()
            except OSError:
                pass
            load()
        actions = Gtk.Box(spacing=6)
        actions.pack_start(text_button("view-refresh-symbolic", "Refresh", load), False, False, 0)
        actions.pack_start(text_button("document-open-symbolic", "Open file", lambda *_: open_url("file://" + LOG_FILE) or subprocess.Popen(["xdg-open", LOG_FILE])), False, False, 0)
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
        combo.set_active_id(self.cfg.get(key) or options[0][0])
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
        self._heading(g, "Activity log")
        self._combo(g, "log_level", "Level", list(LOG_LEVELS))
        self._spin(g, "log_max_kb", "Size limit (KB)", 50, 100000, 50)
        self._note(g, f"File: {LOG_FILE}")

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
        clients = [(cls, self.entries[key].get_text().strip())
                   for cls, key in ((Torbox, "torbox_key"), (RealDebrid, "realdebrid_key"), (AllDebrid, "alldebrid_key"))]

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
        self._timer = 0

    def start(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        feeds = parse_feed_lines(self.app.cfg.get("feeds"))
        if not feeds:
            return
        minutes = max(1, int(self.app.cfg.get("feed_interval_min") or 15))
        self._timer = GLib.timeout_add_seconds(minutes * 60, self._tick)
        GLib.timeout_add_seconds(5, lambda: self.check() and False)  # first pass shortly after start

    def _tick(self):
        self.check()
        return True

    def check(self):
        feeds = parse_feed_lines(self.app.cfg.get("feeds"))
        seen = set(self.app.cfg.get("feed_seen") or [])

        def work():
            found, errors = [], []
            for url, pattern, service in feeds:
                try:  # feeds are typed into Settings by the user, so local servers are allowed; size is capped
                    text = fetch_bytes(url, FEED_MAX, allow_private=True).decode("utf-8", "replace")
                except ApiError as e:
                    errors.append(f"{url}: {e}")
                    continue
                for guid, title, link in parse_feed(text):
                    key = f"{url}#{guid}"
                    if key in seen or not feed_matches(title, pattern):
                        continue
                    if not (link.lower().startswith("magnet:") or link.lower().split("?")[0].endswith(".torrent")):
                        continue  # plain page links can't be added
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
                tab = self.app.tab_named(service) or next((t for t in self.app.tabs if t.client and not t.is_merged), None)
                if tab is None:
                    continue
                by_tab.setdefault(tab, []).append(link)
                seen.add(key)
                log_event(tab.name, f"RSS: {title}")
            self.app.cfg["feed_seen"] = list(seen)[-2000:]
            save_config(self.app.cfg)
            for tab, links in by_tab.items():
                tab.add_items(links)
            notify("RSS", f"{len(found)} new item(s) matched")
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
    gapp = Gtk.Application(application_id=DBUS_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
    state = {}

    def on_cmdline(a, cmdline):
        opts = parse_cli(cmdline.get_arguments()[1:])
        first = "app" not in state
        if first:
            state["app"] = App(a)
            a.hold()  # keep running while the window is hidden
        app = state["app"]
        if opts["toggle"]:
            app.toggle_window()
        elif not first and not opts["items"] and not opts["log"]:
            app.window.show_all()
            app.window.present()
        if opts["log"]:
            app.open_log()
        if opts["items"]:
            tab = app.tab_named(opts["service"]) or next((t for t in app.tabs if t.client and not t.is_merged), None)
            if tab is None:
                return 1
            log_event(tab.name, f"Command line: {len(opts['items'])} item(s)")
            if opts["service"]:
                tab.add_items(opts["items"])
            else:
                app.add_routed(opts["items"], tab)
        return 0
    gapp.connect("command-line", on_cmdline)
    gapp.run(sys.argv)


if __name__ == "__main__":
    main()
