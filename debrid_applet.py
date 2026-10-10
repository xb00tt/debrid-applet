#!/usr/bin/env python3
"""Tray control panel for Torbox and Real-Debrid."""
import json
import os
import subprocess
import sys
import threading
import time
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
EMPTY_CONFIG = {"torbox_key": "", "realdebrid_key": "", "notify_added": True, "notify_done": True}
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


def notify(title, body):
    Notify.Notification.new(title, body, APP_ID).show()


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
        if prev is not None and self.app.cfg["notify_done"]:
            for t in torrents:
                if t["done"] and t["id"] not in prev:
                    notify(f"{self.name}: finished", t["name"])

    # ----- actions -----
    def on_add(self, _w):
        text = self.entry.get_text().strip()
        if not text or self.client is None:
            return
        label = add_label(text)
        if text.lower().startswith("magnet:"):
            self._run(lambda: self.client.add_magnet(text), lambda r, e: self._after_add(r, e, label))
        else:
            self._run(lambda: self.client.add_link(text), lambda r, e: self._after_add(r, e, label))

    def _after_add(self, result, error, label):
        if error:
            return
        self.entry.set_text("")
        if result:  # Real-Debrid returns the direct URL immediately
            copy_text(result)
            self.status.set_text("Link copied to clipboard")
        if self.app.cfg["notify_added"]:
            notify(f"{self.name}: added", label)
        self.app.refresh()

    def on_torrent_file(self, chooser):
        path = chooser.get_filename()
        chooser.unselect_all()
        if path and self.client is not None:
            label = add_label(path)
            self._run(lambda: self.client.add_torrent_file(path), lambda r, e: self._after_add(r, e, label))

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
                item = Gtk.MenuItem(label=link["name"][:80], sensitive=link["url"] is not None)
                item.connect("activate", lambda _i, url=link["url"]: open_url(url))
                menu.append(item)
            menu.append(Gtk.SeparatorMenuItem())
            copy_all = Gtk.MenuItem(label="Copy all links")
            copy_all.connect("activate", lambda _i: copy_text("\n".join(l["url"] for l in links if l["url"])))
            menu.append(copy_all)
            menu.show_all()
            menu.popup_at_widget(btn, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)
            self.status.set_text(f"{len(links)} link(s)")
        self._fetch_links(show)

    def on_copy_first(self, _btn):
        def copy(links, err):
            if err:
                return
            urls = [l["url"] for l in links if l["url"]]
            if urls:
                copy_text(urls[0])
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

class App:
    def __init__(self):
        Notify.init(APP_ID)
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
            self.icon.set_label(f"{active}↓" if active else "")
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
