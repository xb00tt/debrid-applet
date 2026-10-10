#!/usr/bin/env python3
"""Tray control panel for Torbox and Real-Debrid."""
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, quote, urlparse

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("Notify", "0.7")
gi.require_version("XApp", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Notify, Pango, XApp  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from debrid_api import AllDebrid, ApiError, RealDebrid, Torbox, human_size, magnet_hash  # noqa: E402
from debrid_local_api import LocalApi  # noqa: E402

APP_ID = "debrid-applet"  # WM_CLASS, icon name and .desktop basename all match
DBUS_ID = "io.github.xb00tt.DebridApplet"  # single-instance guard
PLAYERS = ("mpv", "vlc", "celluloid", "smplayer", "totem")
SUB_FLAG = {"mpv": "--sub-file={}", "celluloid": "--mpv-sub-file={}", "smplayer": "-sub {}", "vlc": "--sub-file={}"}
SUB_EXT = (".srt", ".ass", ".vtt", ".sub")
SERVICES = ("Torbox", "Real-Debrid", "AllDebrid")
DASHBOARDS = {"Torbox": "https://torbox.app/dashboard", "Real-Debrid": "https://real-debrid.com/torrents",
              "AllDebrid": "https://alldebrid.com/magnets"}
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
                "notified_expiry": {}}  # service -> expiry date already warned about
POLL_FAST, POLL_SLOW = 8, 60  # seconds between polls while something is active / when everything is idle
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
    cmd = [player]
    if sub_url and player in SUB_FLAG:
        cmd += SUB_FLAG[player].format(sub_url).split(" ", 1) if player == "smplayer" else [SUB_FLAG[player].format(sub_url)]
    return cmd + [url]


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
               str, str, str)  # 20 added, 21 finished (absolute local time), 22 hash
CHECKED, STATUS_HOLD = 6, 8
NUMERIC = ("dl", "ul", "seeds", "peers", "ratio", "size")  # right-aligned
WIDTHS = {"check": 28, "name": 120, "dl": 85, "ul": 85, "progress": 90, "seeds": 60, "peers": 60, "ratio": 60,
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
            col.set_visible(key not in hidden)
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
            if key in ("check", "name"):
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
        if key == "state":
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
        self.play_btn.set_sensitive(n == 1 and bool(self.app.player))
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
        if prev is not None:
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
            local_time(t["added"]), local_time(t["finished"]), t["hash"],
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

    def add_items(self, items, after=None):
        """Add magnets, links and .torrent paths sequentially in one worker; report counts in the status line.
        after(added_labels, errors) runs on the main loop when done."""
        if not items or self.client is None:
            return

        def work():
            added, urls, errors = [], [], []
            for item in items:
                try:
                    url = add_one(self.client, item)
                except ApiError as e:
                    errors.append(f"{add_label(item)}: {e}")
                    continue
                except OSError as e:  # unreadable .torrent path
                    errors.append(f"{add_label(item)}: {e.strerror or e}")
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
        entries = [("Play", self.on_play, n == 1 and bool(self.app.player)),
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
        others = [t for t in self.app.tabs if t is not self and t.client is not None]
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
        menu.show_all()
        menu.popup_at_pointer(None)

    def on_play(self, _w):
        targets = self._targets()
        if len(targets) != 1 or not self.app.player:
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

    def play(self, url, name="", links=()):
        """Hand a video URL to the player, with its subtitle file when the item has a matching one."""
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
        for tab in self.tabs:
            self.notebook.append_page(tab, Gtk.Label(label=tab.name))
        self.notebook.show_all()

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
            tab = self.tab_named(service) or next((t for t in self.tabs if t.client), None)
            if tab is None or tab.client is None:
                return 0
            log_event(tab.name, f"Local API: {len(items)} item(s) queued")
            if service:
                GLib.idle_add(tab.add_items, list(items))
            else:  # no service named: cached-on-Torbox routing, else the first configured tab
                GLib.idle_add(self.add_routed, list(items), tab)
            return len(items)

        def status():
            return {t.name: {"active": t.active, "items": len(t.torrents)} for t in self.tabs if t.client}
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
            active = sum(t.active for t in self.tabs)
            self.icon.set_label(f"{active}↓" if active else "")
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
        self.icon.set_tooltip_text(f"Debrid · polling every {seconds} s")
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
        dlg = Gtk.Dialog(title="Debrid settings", transient_for=self.window, modal=True)
        TEST = 1
        dlg.add_buttons("Test keys", TEST, "Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        grid = Gtk.Grid(row_spacing=6, column_spacing=6, margin=12)
        row = 0
        entries, checks = {}, {}
        for key, label, secret in (("torbox_key", "Torbox API key", True),
                                   ("realdebrid_key", "Real-Debrid API key", True),
                                   ("alldebrid_key", "AllDebrid API key", True),
                                   ("watch_dir", "Watch folder for .torrent files", False),
                                   ("download_dir", "Download folder (auto-download)", False)):
            entry = Gtk.Entry(text=str(self.cfg.get(key) or ""), visibility=not secret, width_chars=48)
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            grid.attach(entry, 1, row, 1, 1)
            entries[key] = entry
            row += 1
        watch_service = Gtk.ComboBoxText()
        for name in SERVICES:
            watch_service.append(name, name)
        watch_service.set_active_id(self.cfg.get("watch_service") or SERVICES[0])
        grid.attach(Gtk.Label(label="Upload watched files to", xalign=0), 0, row, 1, 1)
        grid.attach(watch_service, 1, row, 1, 1)
        row += 1
        port = Gtk.SpinButton.new_with_range(0, 65535, 1)
        port.set_value(int(self.cfg.get("http_port") or 0))
        port.set_tooltip_text("0 = off. Every call needs the token below: ?token=… or header X-Debrid-Token")
        grid.attach(Gtk.Label(label="Local API port (0 = off)", xalign=0), 0, row, 1, 1)
        grid.attach(port, 1, row, 1, 1)
        row += 1
        token = Gtk.Entry(text=self.cfg.get("http_token") or "(generated when the port is set)", editable=False, width_chars=48)
        token.set_tooltip_text("Random secret created on first use; delete it from config.json to get a new one")
        grid.attach(Gtk.Label(label="Local API token", xalign=0), 0, row, 1, 1)
        grid.attach(token, 1, row, 1, 1)
        row += 1
        log_level = Gtk.ComboBoxText()
        for key, label in LOG_LEVELS:
            log_level.append(key, label)
        log_level.set_active_id(self.cfg.get("log_level") or "actions")
        grid.attach(Gtk.Label(label="Activity log", xalign=0), 0, row, 1, 1)
        grid.attach(log_level, 1, row, 1, 1)
        row += 1
        log_max = Gtk.SpinButton.new_with_range(50, 100000, 50)
        log_max.set_value(int(self.cfg.get("log_max_kb") or 1000))
        grid.attach(Gtk.Label(label="Activity log size limit (KB)", xalign=0), 0, row, 1, 1)
        grid.attach(log_max, 1, row, 1, 1)
        row += 1
        for key, label in (("notify_added", "Notify when torrent added"),
                           ("notify_done", "Notify when download finishes on service"),
                           ("watch_clipboard", "Watch clipboard for magnet links"),
                           ("remember_filter", "Remember the state filter between runs"),
                           ("dblclick_play", "Double-click plays videos (otherwise opens Files)"),
                           ("auto_download", "Download finished items automatically (aria2c, else wget)"),
                           ("auto_download_videos_only", "    …video files only"),
                           ("delete_after_download", "    …then delete the item from the service"),
                           ("stop_seeding_when_done", "Torbox: stop seeding as soon as an item finishes")):
            check = Gtk.CheckButton(label=label, active=bool(self.cfg.get(key)))
            grid.attach(check, 0, row, 2, 1)
            checks[key] = check
            row += 1
        result = Gtk.Label(xalign=0, wrap=True)
        grid.attach(result, 0, row, 2, 1)
        dlg.get_content_area().add(grid)
        dlg.show_all()

        def test_keys():
            clients = [(cls, entries[key].get_text().strip())
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

        while (resp := dlg.run()) == TEST:
            test_keys()
        if resp == Gtk.ResponseType.OK:
            self.cfg.update({k: e.get_text().strip() for k, e in entries.items()})
            self.cfg.update({k: c.get_active() for k, c in checks.items()})
            self.cfg["watch_service"] = watch_service.get_active_id() or SERVICES[0]
            self.cfg["http_port"] = int(port.get_value())
            self.cfg["log_level"] = log_level.get_active_id() or "actions"
            self.cfg["log_max_kb"] = int(log_max.get_value())
            self.apply_log_settings()
            save_config(self.cfg)
            self.build_tabs()
            self.refresh()
            self.watcher.start()
            self.start_api()
        dlg.destroy()


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
            tab = app.tab_named(opts["service"]) or next((t for t in app.tabs if t.client), None)
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
