#!/usr/bin/env python3
"""Tray control panel for Torbox and Real-Debrid."""
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("Notify", "0.7")
gi.require_version("XApp", "1.0")
from gi.repository import Gdk, GLib, Gtk, Notify, Pango, XApp  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from debrid_api import ApiError, RealDebrid, Torbox, human_size  # noqa: E402

APP_ID = "debrid-applet"  # WM_CLASS, icon name and .desktop basename all match
GLib.set_prgname(APP_ID)

CONFIG_DIR = os.path.expanduser("~/.config/debrid-applet")
CONFIG = os.path.join(CONFIG_DIR, "config.json")
EMPTY_CONFIG = {"torbox_key": "", "realdebrid_key": "", "notify_added": True, "notify_done": True,
                "columns": {"order": [], "hidden": []}}
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


def notify(title, body):
    Notify.Notification.new(title, body, APP_ID).show()


def local_time(iso):
    """'2026-10-10T19:52:35.000Z' (UTC) -> '2026-10-10 21:52' in the local zone; '' when absent or odd."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


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
    ("added", "Added", 12, "text", 12),
    ("finished", "Finished", 13, "text", 13),
)
STORE_TYPES = (str, str, int, str, str, str, bool,  # 0 id, 1 name, 2 progress, 3 state, 4 size, 5 kind, 6 checked
               str, str, str, str, str, str, str,   # 7 dl, 8 ul, 9 seeds, 10 peers, 11 ratio, 12 added, 13 finished
               float, float, float, int, int, float)  # 14 size, 15 dl, 16 ul, 17 seeds, 18 peers, 19 ratio (raw)
CHECKED, STATUS_HOLD = 6, 8
NUMERIC = ("dl", "ul", "seeds", "peers", "ratio", "size")  # right-aligned
PALETTE = {"active": "#3b82f6", "done": "#22c55e", "seed": "#14b8a6", "warn": "#f59e0b", "error": "#ef4444"}
CSS = b"""
treeview.view.progressbar { background-color: #e8761b; border-color: #e8761b; color: #ffffff; }
treeview.view.progressbar:selected { background-color: #ffffff; border-color: #ffffff; color: #e8761b; }
treeview.view.trough:selected { background-color: rgba(0, 0, 0, 0.25); color: #ffffff; }
treeview.view { font-feature-settings: 'tnum'; }
"""


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
    if any(w in s for w in ("download", "metadl", "checking", "queued", "magnet", "waiting", "compressing")):
        return "active"
    return "done"  # store column of the checkbox; seconds an action message stays before polls overwrite it


class ServiceTab(Gtk.Box):
    def __init__(self, name, client, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        self.name, self.client, self.app = name, client, app
        self.done_ids = None  # None until first successful poll; then set of finished ids
        self.checked = set()  # ids with the checkbox on; survives refreshes
        self.torrents = []  # last poll, re-filtered on every keystroke
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
        self.links_btn = text_button("insert-link-symbolic", "Links ▾", self.on_links)
        self.copy_btn = text_button("edit-copy-symbolic", "Copy links", self.on_copy_links)
        self.del_btn = text_button("user-trash-symbolic", "Delete", self.on_delete)
        for b in (self.del_btn, self.copy_btn, self.links_btn):  # pack_end: last packed ends up leftmost
            row.pack_end(b, False, False, 0)
        self.search = Gtk.SearchEntry(placeholder_text="Filter", width_chars=30)
        self.search.connect("changed", lambda _e: self._fill())
        self.search.connect("stop-search", lambda e: e.set_text(""))  # Escape clears
        row.set_center_widget(self.search)
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

    # ----- column layout (shared by both tabs through the config) -----
    def apply_columns(self):
        cfg = self.app.cfg.get("columns") or {}
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
        self.app.set_columns(order=order)

    def on_sort_changed(self, store):
        if self._applying:
            return
        ok, idx, direction = store.get_sort_column_id()
        key = next((k for k, _t, _i, _k, s in COLUMNS if s == idx), None)
        if ok and key:
            self.app.set_columns(sort=[key, "desc" if direction == Gtk.SortType.DESCENDING else "asc"])

    def on_header_press(self, _btn, event):
        if event.button != 3:
            return False
        menu = Gtk.Menu()
        for key, title, _i, _k, _s in COLUMNS:
            if key in ("check", "name"):
                continue
            item = Gtk.CheckMenuItem(label=title, active=self.cols[key].get_visible())
            item.connect("toggled", lambda it, k=key: self.app.set_columns(hide=(k, not it.get_active())))
            menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _style_cell(self, _col, cell, model, it, key):
        """Zebra rows, state colored by kind, finished names dimmed. Selected rows keep the theme's look."""
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
        else:
            dim = key == "name" and kind in ("done", "seed")
            cell.set_property("foreground-rgba", Gdk.RGBA(fg.red, fg.green, fg.blue, 0.55 if dim else 0.85))

    # ----- checkboxes and targets -----
    def on_toggled(self, _rend, path):
        row = self.store[path]
        row[CHECKED] = not row[CHECKED]
        (self.checked.add if row[CHECKED] else self.checked.discard)(row[0])
        self._sync_checks()

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
        return {"id": r[0], "name": r[1], "kind": r[5]}

    def _selected(self):
        model, it = self.view.get_selection().get_selected()
        return None if it is None else self._row(model[it])

    def _targets(self):
        """Checked rows if any, else the highlighted row."""
        checked = [self._row(r) for r in self.store if r[CHECKED]]
        if checked:
            return checked
        sel = self._selected()
        return [sel] if sel else []

    def _update_buttons(self):
        n = len(self._targets()) if self.client is not None and not self._is_busy else 0
        self.links_btn.set_sensitive(n == 1)
        self.copy_btn.set_sensitive(n >= 1)
        self.del_btn.set_sensitive(n >= 1)

    def _busy(self, flag):
        self._is_busy = flag
        self._update_buttons()

    def say(self, msg, error=False):
        """Status line message that polls leave alone for a few seconds."""
        self.status.set_markup(red(msg) if error else GLib.markup_escape_text(msg))
        self._hold_until = time.monotonic() + STATUS_HOLD

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
        self.checked &= {t["id"] for t in torrents}
        self._fill()
        self.active = sum(1 for t in torrents if not t["done"])
        if time.monotonic() >= self._hold_until:
            self.status.set_text(f"Updated {time.strftime('%H:%M:%S')} · {self.active} active")

        prev = self.done_ids
        self.done_ids = {t["id"] for t in torrents if t["done"]}
        if prev is not None and self.app.cfg["notify_done"]:
            for t in torrents:
                if t["done"] and t["id"] not in prev:
                    notify(f"{self.name}: finished", t["name"])

    def _fill(self):
        """Rebuild the list from the last poll, keeping rows whose name or state matches the filter box."""
        q = self.search.get_text().strip().lower()
        selected = self._selected()
        self.store.clear()
        for t in self.torrents:
            if q and q not in t["name"].lower() and q not in t["state"].lower():
                continue
            self.store.append([
                t["id"], t["name"], int(max(0, min(100, t["progress"]))), t["state"], human_size(t["size"]), t["kind"],
                t["id"] in self.checked,
                fmt_speed(t["speed"]), fmt_speed(t["up_speed"]), str(t["seeds"]), na(t["peers"]), na(t["ratio"], "{:.2f}"),
                local_time(t["added"]), local_time(t["finished"]),
                float(t["size"]), float(t["speed"] or 0), float(t["up_speed"] or -1),
                int(t["seeds"]), -1 if t["peers"] is None else int(t["peers"]),
                -1.0 if t["ratio"] is None else float(t["ratio"]),
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
        view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.add(view)
        box.pack_start(hint, False, False, 0)
        box.pack_start(scroller, True, True, 0)
        dlg.show_all()
        view.grab_focus()
        ok = dlg.run() == Gtk.ResponseType.OK
        buf = view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        dlg.destroy()
        if ok:
            self.add_items([line.strip() for line in text.splitlines() if line.strip()])

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
            self.app.refresh()
        self._run(work, done)

    def on_links(self, btn):
        targets = self._targets()
        if len(targets) != 1:
            return
        t = targets[0]

        def show(links, err):
            if err:
                return
            if not links:
                self.say("No links yet (not finished?)")
                return
            menu = Gtk.Menu()
            for link in links:
                item = Gtk.MenuItem(label=link["name"][:80], sensitive=link["url"] is not None)
                item.connect("activate", lambda _i, url=link["url"]: open_url(url))
                menu.append(item)
            menu.append(Gtk.SeparatorMenuItem())
            copy_all = Gtk.MenuItem(label="Copy all links")
            copy_all.connect("activate", lambda _i: copy_text("\n".join(l["url"] for l in links if l["url"])))
            menu.append(copy_all)
            menu.show_all()
            menu.popup_at_widget(btn, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)
            self.say(f"{len(links)} link(s)")
        self._run(lambda: self.client.links(t["id"], t["kind"]), show)

    def on_copy_links(self, _btn):
        targets = self._targets()
        if not targets:
            return

        def work():
            urls, errors = [], []
            for t in targets:
                for link in self.client.links(t["id"], t["kind"]):
                    (urls if link["url"] else errors).append(link["url"] or link["name"])
            return urls, errors

        def done(res, err):
            if err:
                return
            urls, errors = res
            if urls:
                copy_text("\n".join(urls))
            msg = f"Copied {len(urls)} link(s)" if urls else "No links yet (not finished?)"
            if errors:
                msg += " · " + "; ".join(errors)
            self.say(msg, error=bool(errors))
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
    def __init__(self):
        Notify.init(APP_ID)
        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.IconTheme.get_default().append_search_path(HERE)  # debrid-applet.svg next to this file
        Gtk.Window.set_default_icon_name(APP_ID)
        self.cfg = load_config()
        self.lock = threading.Lock()
        self._settings_shown_for = None
        self.tabs = []

        # Cinnamon-native tray icon: left click toggles the window, right click opens the menu.
        self.icon = XApp.StatusIcon()
        self.icon.set_name(APP_ID)
        self.icon.set_icon_name(os.path.join(HERE, "debrid-applet.svg"))
        self.icon.set_tooltip_text("Debrid")
        self.icon.connect("activate", self.toggle_window)
        menu = Gtk.Menu()
        for label, cb in (("Open", self.toggle_window), ("Refresh", self.refresh),
                          ("Settings…", self.open_settings), ("Quit", Gtk.main_quit)):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", cb)
            menu.append(item)
        menu.show_all()
        self.icon.set_secondary_menu(menu)

        self.window = Gtk.Window(title="Debrid", default_width=1100, default_height=560)
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
            self.icon.set_label(f"{active}↓" if active else "")
        run_async(fetch, done)
        return True

    def set_columns(self, order=None, hide=None, sort=None):
        """Persist column order / visibility / sort and apply to both tabs."""
        cols = self.cfg.setdefault("columns", {"order": [], "hidden": []})
        if order is not None:
            cols["order"] = order
        if hide is not None:
            key, hidden = hide
            cols["hidden"] = sorted((set(cols.get("hidden") or []) | {key}) - (set() if hidden else {key}))
        if sort is not None:
            cols["sort"] = sort
        save_config(self.cfg)
        for tab in self.tabs:
            tab.apply_columns()

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
        checks = {}
        for i, (key, label) in enumerate((("notify_added", "Notify when torrent added"),
                                          ("notify_done", "Notify when download finishes on service")), start=2):
            check = Gtk.CheckButton(label=label, active=bool(self.cfg[key]))
            grid.attach(check, 0, i, 2, 1)
            checks[key] = check
        dlg.get_content_area().add(grid)
        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            self.cfg = {**{k: e.get_text().strip() for k, e in entries.items()},
                        **{k: c.get_active() for k, c in checks.items()}}
            save_config(self.cfg)
            self.build_tabs()
            self.refresh()
        dlg.destroy()


if __name__ == "__main__":
    App()
    Gtk.main()
