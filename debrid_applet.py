#!/usr/bin/env python3
"""Tray control panel for Torbox and Real-Debrid."""
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
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
from debrid_api import ApiError, RealDebrid, Torbox, human_size, magnet_hash  # noqa: E402

APP_ID = "debrid-applet"  # WM_CLASS, icon name and .desktop basename all match
DBUS_ID = "io.github.xb00tt.DebridApplet"  # single-instance guard
PLAYERS = ("mpv", "vlc", "celluloid", "smplayer", "totem")
VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".webm", ".mov", ".ts", ".wmv", ".flv")
GLib.set_prgname(APP_ID)

CONFIG_DIR = os.path.expanduser("~/.config/debrid-applet")
CONFIG = os.path.join(CONFIG_DIR, "config.json")
EMPTY_CONFIG = {"torbox_key": "", "realdebrid_key": "", "notify_added": True, "notify_done": True,
                "watch_clipboard": True, "columns": {}, "window": []}  # columns: per-service, see column_layout()
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


_NOTES = []  # notifications with buttons must stay referenced until clicked or closed


def notify(title, body, actions=()):
    """Desktop notification; actions = [(label, callback)] become buttons."""
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
PALETTE = {"active": "#3b82f6", "done": "#22c55e", "seed": "#14b8a6", "warn": "#f59e0b", "error": "#ef4444"}
STATE_FILTERS = (("all", "All states"), ("active", "Active"), ("done", "Done"), ("seed", "Seeding"),
                 ("warn", "Stalled"), ("error", "Error"))
CSS = b"""
treeview.view.progressbar { background-color: #e8761b; border-color: #e8761b; color: #ffffff; }
treeview.view.progressbar:selected { background-color: #ffffff; border-color: #ffffff; color: #e8761b; }
treeview.view.trough:selected { background-color: rgba(0, 0, 0, 0.25); color: #ffffff; }
treeview.view { font-feature-settings: 'tnum'; }
"""


DEFAULT_HIDDEN = {"Real-Debrid": ["ul", "peers", "ratio"]}  # columns that service never fills


def column_layout(columns, name):
    """Layout dict for one service inside cfg['columns']; migrates the old shared format in place."""
    if any(k in columns for k in ("order", "hidden", "sort")):
        flat = {k: columns.pop(k) for k in ("order", "hidden", "sort") if k in columns}
        for n in ("Torbox", "Real-Debrid"):
            columns.setdefault(n, json.loads(json.dumps(flat)))
    return columns.setdefault(name, {"order": [], "hidden": list(DEFAULT_HIDDEN.get(name, []))})


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
    if any(w in s for w in ("download", "metadl", "checking", "queued", "magnet", "waiting", "compressing", "adding")):
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
        self.errors = []  # (time, message) of the last few failures, shown as the status line's tooltip
        self.active = 0
        self._is_busy = False
        self._applying = True  # suppress columns-changed while we build or restore the layout
        self._hold_until = 0

        self.account_label = Gtk.Label(xalign=0, label="Not configured" if client is None else "Loading…")

        row = Gtk.Box(spacing=2)
        for icon, tip, cb in (("list-add-symbolic", "Add magnets / links", self.on_add),
                              ("document-open-symbolic", "Add .torrent files", self.on_torrent_file),
                              ("view-refresh-symbolic", "Refresh", app.refresh),
                              ("emblem-system-symbolic", "Settings", app.open_settings)):
            row.pack_start(icon_button(icon, tip, cb), False, False, 0)
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
        self.state_filter.set_active_id("all")
        self.state_filter.connect("changed", lambda _c: self._fill())
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
            if key == "name":
                col.set_expand(True)
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
            self.cols[key] = col
        self.apply_columns()
        self.view.set_has_tooltip(True)
        self.view.set_enable_search(False)  # the filter box does this job; typeahead would steal keys
        self.view.connect("query-tooltip", self.on_query_tooltip)
        self.view.connect("button-press-event", self.on_row_press)
        self.view.connect("row-activated", lambda *_: self.on_play(None) if self.app.player else self.on_files(None))
        self.view.connect("columns-changed", self.on_columns_changed)
        self.store.connect("sort-column-changed", self.on_sort_changed)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.add(self.view)
        self.pack_start(scroller, True, True, 0)

        self.status = Gtk.Label(xalign=0)
        self.pack_start(self.status, False, False, 0)
        self.pack_start(self.account_label, False, False, 0)

        self.view.get_selection().connect("changed", lambda _s: self._update_buttons())
        self._update_buttons()

        # Drop .torrent files, links or magnets anywhere on the tab.
        self.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.drag_dest_add_uri_targets()
        self.drag_dest_add_text_targets()
        self.connect("drag-data-received",
                     lambda _w, _c, _x, _y, data, _i, _t: self.add_items(drop_items(data.get_uris(), data.get_text())))

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
        sort = cfg.get("sort")
        if sort:
            idx = next((s for k, _t, _i, _k, s in COLUMNS if k == sort[0]), None)
            if idx is not None:
                self.store.set_sort_column_id(idx, Gtk.SortType.DESCENDING if sort[1] == "desc" else Gtk.SortType.ASCENDING)
        self._applying = False

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
        self._update_buttons()

    def say(self, msg, error=False):
        """Status line message that polls leave alone for a few seconds. Errors stay in the tooltip."""
        self.status.set_markup(red(msg) if error else GLib.markup_escape_text(msg))
        self._hold_until = time.monotonic() + STATUS_HOLD
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
            self.say(error, error=True)
            if error == INVALID_KEY:
                self.app.open_settings_once()
            return
        self.account_label.set_text(
            f"{account['plan']} · expires {account['expires'] or '—'} · {account['extra']}")
        self.torrents = torrents
        ids = {t["id"] for t in torrents}
        if ids - self._known:  # something new showed up: placeholders have done their job
            self._pending = []
        self._known = ids
        self.checked &= ids
        self._fill()
        self.active = sum(1 for t in torrents if not t["done"])
        if time.monotonic() >= self._hold_until:
            self.status.set_text(f"Updated {time.strftime('%H:%M:%S')} · {self.active} active")

        prev = self.done_ids
        self.done_ids = {t["id"] for t in torrents if t["done"]}
        if prev is not None and self.app.cfg["notify_done"]:
            for t in torrents:
                if t["done"] and t["id"] not in prev:
                    notify(f"{self.name}: finished", t["name"],
                           [("Open files", lambda t=t: self.app.show_files(self, t))])

    def _fill(self):
        """Rebuild the list from the last poll, keeping rows whose name or state matches the filter box."""
        q = self.search.get_text().strip().lower()
        kind = self.state_filter.get_active_id() or "all"
        selected = self._selected()
        self.store.clear()
        self._anchor = None
        now = time.monotonic()
        self._pending = [(exp, row) for exp, row in self._pending if now < exp]
        for t in [row for _exp, row in self._pending] + self.torrents:  # placeholders first
            if q and q not in t["name"].lower() and q not in t["state"].lower():
                continue
            if kind != "all" and state_kind(t["state"]) != kind:
                continue
            self.store.append([
                t["id"], t["name"], int(max(0, min(100, t["progress"]))), t["state"], human_size(t["size"]), t["kind"],
                t["id"] in self.checked,
                fmt_speed(t["speed"]), fmt_speed(t["up_speed"]), str(t["seeds"]), na(t["peers"]), na(t["ratio"], "{:.2f}"),
                relative_time(t["added"]), relative_time(t["finished"]),
                float(t["size"]), float(t["speed"] or 0), float(t["up_speed"] or -1),
                int(t["seeds"]), -1 if t["peers"] is None else int(t["peers"]),
                -1.0 if t["ratio"] is None else float(t["ratio"]),
                local_time(t["added"]), local_time(t["finished"]), t["hash"],
            ])
        if selected:
            for row in self.store:
                if row[0] == selected["id"]:
                    self.view.get_selection().select_iter(row.iter)
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
        view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, left_margin=8, right_margin=8,
                            top_margin=6, bottom_margin=6)
        buf = view.get_buffer()
        scroller = Gtk.ScrolledWindow(vexpand=True, shadow_type=Gtk.ShadowType.IN)
        scroller.add(view)
        result = Gtk.Label(xalign=0, wrap=True)
        box.pack_start(hint, False, False, 0)
        box.pack_start(scroller, True, True, 0)
        box.pack_start(result, False, False, 0)
        if hasattr(self.client, "check_cached"):
            self._live_cache_check(buf, result)
        dlg.show_all()
        view.grab_focus()
        ok = dlg.run() == Gtk.ResponseType.OK
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        dlg.destroy()
        if ok:
            self.add_items([line.strip() for line in text.splitlines() if line.strip()])

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

    def add_items(self, items):
        """Add magnets, links and .torrent paths sequentially in one worker; report counts in the status line."""
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
        for label, cb, ok in entries:
            if label is None:
                menu.append(Gtk.SeparatorMenuItem())
                continue
            item = Gtk.MenuItem(label=label, sensitive=ok)
            item.connect("activate", lambda _i, cb=cb: cb(None))
            menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def on_copy_magnet(self, _w):
        magnets = [magnet_for(t["hash"], t["name"]) for t in self._targets()]
        magnets = [m for m in magnets if m]
        if magnets:
            copy_text("\n".join(magnets))
        self.say(f"Copied {len(magnets)} magnet link(s)" if magnets else "No hash known for the selection")

    def on_play(self, _w):
        targets = self._targets()
        if len(targets) != 1 or not self.app.player:
            return
        t = targets[0]

        def pick(links, err):
            if err:
                return
            vids = video_links(links)
            if not vids:
                self.say("Nothing playable yet (not finished?)")
            elif len(vids) == 1:
                self.play(vids[0]["url"])
            else:
                menu = Gtk.Menu()
                for v in vids:
                    item = Gtk.MenuItem(label=v["name"][:80])
                    item.connect("activate", lambda _i, url=v["url"]: self.play(url))
                    menu.append(item)
                menu.show_all()
                menu.popup_at_pointer(None)
        self._run(lambda: self.client.links(t["id"], t["kind"]), pick)

    def play(self, url):
        if not self.app.player:
            self.say("No media player found (mpv, vlc, celluloid, smplayer or totem)")
            return
        if not url or not url.lower().startswith(("http://", "https://")):
            return
        subprocess.Popen([self.app.player, url])
        self.say(f"Playing in {self.app.player}")

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
                                ("media-playback-start-symbolic", "Play", lambda: chosen() and self.play(chosen()[0])),
                                ("edit-copy-symbolic", "Copy links", copy),
                                ("utilities-terminal-symbolic", "aria2c command", copy_aria2)):
            actions.pack_start(text_button(icon, label, lambda _b, cb=cb: cb()), False, False, 0)
        box.pack_start(actions, False, False, 0)
        dlg.add_button("Close", Gtk.ResponseType.CLOSE)
        dlg.connect("response", lambda d, _r: d.destroy())
        view.connect("row-activated", lambda v, path, _c: self.play(store[path][3]) if self.app.player else open_url(store[path][3]))
        dlg.show_all()

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
            errors = []
            for t in targets:
                try:
                    self.client.control(t["id"], op, t["kind"])
                except ApiError as e:
                    errors.append(f"{t['name'][:40]}: {e}")
            return errors

        def done(errors, err):
            if err:
                return
            self.say(f"{op.replace('_', ' ').capitalize()}: {len(targets) - len(errors)} ok" + (" · " + "; ".join(errors) if errors else ""),
                     error=bool(errors))
            self.app.refresh()
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
        self.lock = threading.Lock()
        self._refresh_again = False
        self._timer = 0
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

        if not (self.cfg["torbox_key"] or self.cfg["realdebrid_key"]):
            self.open_settings()
        self.refresh()  # every poll schedules the next one (see _schedule)

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

    def current_tab(self):
        return self.tabs[self.notebook.get_current_page()]

    def on_key(self, _w, event):
        tab = self.current_tab()
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        key = Gdk.keyval_to_lower(event.keyval)
        if key == Gdk.KEY_Escape:
            self.hide_window()
        elif ctrl and key == Gdk.KEY_f:
            tab.search.grab_focus()
        elif ctrl and key == Gdk.KEY_r:
            self.refresh()
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
        self._timer = GLib.timeout_add_seconds(seconds, self._tick)
        self.icon.set_tooltip_text(f"Debrid · polling every {seconds} s")

    def _tick(self):
        self._timer = 0
        self.refresh()
        return False

    def set_columns(self, tab, order=None, hide=None, sort=None):
        """Persist one tab's column order / visibility / sort and re-apply it there."""
        cols = column_layout(self.cfg.setdefault("columns", {}), tab.name)
        if order is not None:
            cols["order"] = order
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

    def open_settings(self, *_):
        dlg = Gtk.Dialog(title="Debrid settings", transient_for=self.window, modal=True)
        TEST = 1
        dlg.add_buttons("Test keys", TEST, "Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        grid = Gtk.Grid(row_spacing=6, column_spacing=6, margin=12)
        entries = {}
        for i, (key, label) in enumerate((("torbox_key", "Torbox API key"),
                                          ("realdebrid_key", "Real-Debrid API key"))):
            entry = Gtk.Entry(text=self.cfg[key], visibility=False, width_chars=48)
            grid.attach(Gtk.Label(label=label, xalign=0), 0, i, 1, 1)
            grid.attach(entry, 1, i, 1, 1)
            entries[key] = entry
        checks = {}
        for i, (key, label) in enumerate((("notify_added", "Notify when torrent added"),
                                          ("notify_done", "Notify when download finishes on service"),
                                          ("watch_clipboard", "Watch clipboard for magnet links")), start=2):
            check = Gtk.CheckButton(label=label, active=bool(self.cfg[key]))
            grid.attach(check, 0, i, 2, 1)
            checks[key] = check
        result = Gtk.Label(xalign=0, wrap=True)
        grid.attach(result, 0, 5, 2, 1)
        dlg.get_content_area().add(grid)
        dlg.show_all()

        def test_keys():
            clients = [(cls, entries[key].get_text().strip())
                       for cls, key in ((Torbox, "torbox_key"), (RealDebrid, "realdebrid_key"))]

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
            self.cfg = {**{k: e.get_text().strip() for k, e in entries.items()},
                        **{k: c.get_active() for k, c in checks.items()}}
            save_config(self.cfg)
            self.build_tabs()
            self.refresh()
        dlg.destroy()


def main():
    """One instance only: a second launch just raises the existing window."""
    gapp = Gtk.Application(application_id=DBUS_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
    state = {}

    def activate(a):
        if "app" not in state:
            state["app"] = App(a)
            a.hold()  # keep running while the window is hidden
        else:
            state["app"].window.show_all()
            state["app"].window.present()
    gapp.connect("activate", activate)
    gapp.run(sys.argv)


if __name__ == "__main__":
    main()
