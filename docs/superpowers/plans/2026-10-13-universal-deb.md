# Universal deb Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The same `.deb` runs on any Debian-based desktop (GNOME, KDE, XFCE, MATE, Cinnamon; X11 and Wayland) with a tray icon wherever a host exists and a visible, quittable window where none does.

**Architecture:** XApp and Ayatana become optional imports. A small `Tray` class in `debrid_applet.py` picks a backend once XApp has reported whether a host answered (its `state-changed` signal, or a 2 s timer), replaying the count onto whichever backend wins. `App` learns tray-less mode (window shown at start, one hint on first hide), gains Quit inside the window, and Settings hints that Wayland limits the clipboard watcher. The deb moves both tray libraries to Recommends.

**Tech Stack:** Python 3.12, PyGObject, GTK3, `XApp` 1.0 (optional), `AyatanaAppIndicator3` 0.1 (optional), libnotify, dpkg-deb. Tests: `python3 test_debrid_api.py` (plain asserts, no runner).

**Spec:** `docs/superpowers/specs/2026-10-13-universal-deb-design.md`

## Global Constraints

- No new hard dependency; `Depends` stays `python3 (>= 3.11), python3-gi, gir1.2-gtk-3.0, gir1.2-notify-0.7, python3-requests, python3-defusedxml`.
- GTK3 only; one process; no new files except the plan itself.
- Tray backend kinds, exact strings: `"xapp"`, `"ayatana"`, `"xapp-fallback"`, `"none"`.
- Env knob, exact name and values: `DEBRID_APPLET_TRAY=xapp|ayatana|none`.
- Hint texts, exact: tray-less hide notification title `Debrid Applet keeps running`, body `Open it from the menu or debrid-applet --toggle; Quit is in the ⋮ menu.`; Wayland note `On Wayland the clipboard is only read while this window is focused.`
- `VERSION = "1.1.0"` at the end, not before (the release workflow refuses a tag that does not match).
- The offline suite sets `DEBRID_APPLET_CONFIG_DIR` / `DEBRID_APPLET_DATA_DIR` before importing `debrid_applet`; any new script that imports the module must do the same (see `docs/DEVELOPING.md`, "Where things live at runtime").
- Commit messages: plain imperative subject, as in the existing history; body optional.
- Line numbers in "Files:" refer to the 1.0.9 tree before any task ran; earlier tasks shift later ones, so locate by the quoted code, not the number.

## Review Focus

1. `DEBRID_APPLET_TRAY` set to a typo or an unlisted word: the normal chain must run, nothing crashes. Pinned in Task 1's test.
2. `DEBRID_APPLET_TRAY=ayatana` or `=xapp` on a box without that library: result is `"none"`, the window shows at start. Pinned in Task 1's test.
3. XApp emits `state-changed` with `no-support` before `native` (measured: `no-support` at construction, `native` 1 ms later): an early `no-support` must not decide. Pinned by `Tray._on_state` ignoring `no-support`; checked manually in Task 2, step 6 (log line says `Tray: xapp` on Cinnamon).
4. `Esc` or the close button within the first 2 s, before the tray decided: no hint, no crash. Pinned by the `kind is not None` guard in `hide_window`; checked manually in Task 2, step 7.
5. A poll finishes before the backend is chosen (slow session bus): the count must land on the backend chosen later. Pinned by `Tray._count` replay in `_decide`; checked manually in Task 2, step 6 (label appears after the first poll on every backend).

---

### Task 1: Optional desktop imports and the pure decision helpers

**Files:**
- Modify: `debrid_applet.py:30-35` (imports), `debrid_applet.py:225-227` (after `find_player`)
- Test: `test_debrid_api.py` (append one test)

**Interfaces:**
- Consumes: nothing new.
- Produces: module globals `XApp` (module or `None`), `Ayatana` (module or `None`); `tray_choice(xapp_state, have_xapp, have_ayatana, forced="") -> str`; `is_wayland(display_type_name) -> bool`.

- [ ] **Step 1: Write the failing test**

Append to `test_debrid_api.py`, before the `if __name__ == "__main__":` block:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 test_debrid_api.py`
Expected: `AttributeError: module 'debrid_applet' has no attribute 'tray_choice'` (the suite stops at the first failing test; earlier tests print `ok`).

- [ ] **Step 3: Make the two desktop imports optional**

In `debrid_applet.py`, replace lines 30-35:

```python
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("Notify", "0.7")
gi.require_version("XApp", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Notify, Pango, XApp  # noqa: E402
```

with:

```python
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("Notify", "0.7")
from gi.repository import Gdk, Gio, GLib, Gtk, Notify, Pango  # noqa: E402
try:  # status icon hosts are optional: Cinnamon / XFCE / MATE answer XApp, StatusNotifier desktops answer Ayatana
    gi.require_version("XApp", "1.0")
    from gi.repository import XApp  # noqa: E402
except (ValueError, ImportError):
    XApp = None
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as Ayatana  # noqa: E402
except (ValueError, ImportError):
    Ayatana = None
```

- [ ] **Step 4: Add the pure helpers**

In `debrid_applet.py`, directly after `find_player()` (line 225-226), add:

```python
TRAY_KINDS = ("xapp", "ayatana", "xapp-fallback", "none")


def tray_choice(xapp_state, have_xapp, have_ayatana, forced=""):
    """Which status icon to keep. xapp_state: XApp's state nick once a host has answered ('native', 'fallback',
    'no-support') or None when XApp is not installed. Ayatana wherever XApp is not native; XApp's own XEmbed
    fallback only when Ayatana is missing too (visible on XEmbed trays under X11, nowhere else). forced: the
    DEBRID_APPLET_TRAY knob, honoured when that backend exists, else none; other words are ignored."""
    if forced == "none":
        return "none"
    if forced == "ayatana":
        return "ayatana" if have_ayatana else "none"
    if forced == "xapp":
        return "xapp" if have_xapp else "none"
    if have_xapp and xapp_state == "native":
        return "xapp"
    if have_ayatana:
        return "ayatana"
    if have_xapp and xapp_state == "fallback":
        return "xapp-fallback"
    return "none"


def is_wayland(display_type_name):
    """type(Gdk.Display.get_default()).__name__ is 'GdkWaylandDisplay' there, 'X11Display' under X11."""
    return "Wayland" in display_type_name
```

- [ ] **Step 5: Run the suite to verify it passes**

Run: `python3 test_debrid_api.py`
Expected: every line `ok …`, last line `N passed` (one more than before).

- [ ] **Step 6: Confirm the module still imports without XApp**

Run: `DEBRID_APPLET_CONFIG_DIR=/tmp/debrid-test-config DEBRID_APPLET_DATA_DIR=/tmp/debrid-test-data python3 -c "import sys, gi; gi.require_version('Gtk','3.0'); import gi.repository; sys.modules['gi.repository.XApp'] = None; import debrid_applet; print('XApp is', debrid_applet.XApp, '/ Ayatana is', debrid_applet.Ayatana)"`
Expected: `XApp is None / Ayatana is <IntrospectionModule 'AyatanaAppIndicator3' …>` (the `sys.modules` entry set to `None` makes `from gi.repository import XApp` raise ImportError, as on a box without the typelib).

- [ ] **Step 7: Commit**

```bash
git add debrid_applet.py test_debrid_api.py
git commit -m "Tray libraries are optional imports; pure backend choice and Wayland helpers"
```

---

### Task 2: The Tray adapter and tray-less mode

**Files:**
- Modify: `debrid_applet.py` (new class `Tray` directly above `class App:`, line 2029), `debrid_applet.py:2056-2070` (App.__init__ tray block), `debrid_applet.py:2214-2217` (`hide_window`), `debrid_applet.py:2257-2258` (`refresh` done tail)

**Interfaces:**
- Consumes: `XApp`, `Ayatana`, `tray_choice` from Task 1; `notify(title, body)`, `log_event(service, message, level)`, `APP_ID`, `HERE`, `POLL_SLOW`.
- Produces: `class Tray` with `__init__(self, app, menu, show_item, on_decided)`, attribute `kind` (`None` until decided, then one of `TRAY_KINDS`), methods `set_count(self, active, interval)`, `present(self) -> bool`; `App.tray`, `App._on_tray_decided(self, kind)`, `App._hide_hint_shown`.

- [ ] **Step 1: Add the Tray class**

Insert directly above `class App:`:

```python
class Tray:
    """The status icon behind one interface. XApp where a host answers (Cinnamon, XFCE and MATE with the xapp
    applet), Ayatana / StatusNotifier elsewhere (KDE, GNOME with its AppIndicator extension, any desktop running
    xapp-sn-watcher), nothing as a last resort. XApp reports 'no-support' at construction and the real state on
    'state-changed' (about 1 ms later on Cinnamon), so the choice waits for that signal or a 2 s timer, then
    on_decided(kind) runs once on the main loop. The count is kept and replayed onto whichever backend wins."""

    def __init__(self, app, menu, show_item, on_decided):
        self.app, self.menu, self.show_item, self.on_decided = app, menu, show_item, on_decided
        self.kind = None
        self.icon = self.indicator = None
        self._count = (0, POLL_SLOW)
        self._timer = 0
        forced = os.environ.get("DEBRID_APPLET_TRAY", "")
        if XApp is not None and forced in ("", "xapp"):
            self.icon = XApp.StatusIcon()
            self.icon.set_name(APP_ID)
            self.icon.set_icon_name(os.path.join(HERE, APP_ID + ".svg"))
            self.icon.set_tooltip_text("Debrid")
            self.icon.connect("activate", app.toggle_window)  # left click; the menu's first entry does the same
            self.icon.set_secondary_menu(menu)
            self.icon.connect("state-changed", self._on_state)
            self._timer = GLib.timeout_add_seconds(2, lambda: self._decide(self.icon.get_state().value_nick) and False)
        else:
            GLib.idle_add(lambda: self._decide(None) and False)  # after App.__init__, like the signal would be

    def _on_state(self, _icon, state):
        if state.value_nick != "no-support":  # native or fallback: the host has answered; no-support may still change
            self._decide(state.value_nick)

    def _decide(self, xapp_state):
        if self.kind is not None:  # the signal and the timer both end here; the first one wins
            return
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        kind = tray_choice(xapp_state, self.icon is not None, Ayatana is not None, os.environ.get("DEBRID_APPLET_TRAY", ""))
        if kind not in ("xapp", "xapp-fallback") and self.icon is not None:
            self.icon.set_visible(False)
            self.icon = None
        if kind == "ayatana":
            ind = Ayatana.Indicator.new(APP_ID, APP_ID, Ayatana.IndicatorCategory.APPLICATION_STATUS)
            ind.set_icon_theme_path(HERE)  # debrid-applet.svg next to the code; the icon theme has it when packaged
            ind.set_icon_full(APP_ID, "Debrid")
            ind.set_status(Ayatana.IndicatorStatus.ACTIVE)
            ind.set_menu(self.menu)
            ind.set_secondary_activate_target(self.show_item)  # middle click toggles the window; left click is the menu
            self.indicator = ind
        self.kind = kind
        self.set_count(*self._count)
        log_event("", f"Tray: {kind}")
        self.on_decided(kind)

    def set_count(self, active, interval):
        """Label 'N↓' and tooltip; indicators have no tooltip, their title carries the text."""
        self._count = (active, interval)
        label = f"{active}↓" if active else ""
        tip = f"Debrid · {active} item(s) still downloading · polling every {interval} s"
        if self.icon is not None:
            self.icon.set_label(label)
            self.icon.set_tooltip_text(tip)
        elif self.indicator is not None:
            self.indicator.set_label(label, "")
            self.indicator.set_title(tip)

    def present(self):
        """True when a host is known to show the icon."""
        return self.kind in ("xapp", "ayatana")
```

- [ ] **Step 2: Build the menu with Show / hide first and hand it to Tray**

In `App.__init__`, replace the block from `# Cinnamon-native tray icon:` through `self.icon.set_secondary_menu(menu)` (lines 2056-2070) with:

```python
        menu = Gtk.Menu()  # right-click on XApp, any click on an indicator; "Show / hide" first for hosts without left-click
        show_item = None
        for label, cb in (("Show / hide window", self.toggle_window), ("Refresh", self.refresh),
                          ("Settings…", self.open_settings), ("Check for updates…", lambda *_: self.check_updates(True)),
                          ("About…", self.open_about), ("Quit", lambda *_: self.gapp.quit())):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", cb)
            menu.append(item)
            show_item = show_item or item
        menu.show_all()
        self._hide_hint_shown = False  # tray-less mode: the first hide of the session explains how to come back
        self.tray = Tray(self, menu, show_item, self._on_tray_decided)
```

- [ ] **Step 3: Tray-less mode: show the window when no host turned up**

Add to `App`, directly after `toggle_window`:

```python
    def _on_tray_decided(self, _kind):
        """No host shows the icon: the window is the only handle on the applet, so it starts visible."""
        if not self.tray.present() and not self.window.is_visible():
            self.window.show_all()
            self.window.present()
```

- [ ] **Step 4: The one-time hint on hide**

Replace `hide_window`:

```python
    def hide_window(self):
        self.cfg["window"] = list(self.window.get_size())
        save_config(self.cfg)
        self.window.hide()
        if self.tray.kind is not None and not self.tray.present() and not self._hide_hint_shown:
            self._hide_hint_shown = True
            notify("Debrid Applet keeps running", "Open it from the menu or debrid-applet --toggle; Quit is in the ⋮ menu.")
```

- [ ] **Step 5: Route the count through the adapter**

In `App.refresh`, inside `done()`'s `finally:`, replace:

```python
                self.icon.set_label(f"{active}↓" if active else "")
                self.icon.set_tooltip_text(f"Debrid · {active} item(s) still downloading · polling every {self._interval} s")
```

with:

```python
                self.tray.set_count(active, self._interval)
```

Then confirm no direct use remains: `grep -n 'self\.icon\b' debrid_applet.py` must list only lines inside `class Tray`.

- [ ] **Step 6: Run the suite, then the three backends on this box**

Run: `python3 test_debrid_api.py` → all `ok`.

Quit the installed applet first (tray icon → Quit), otherwise a launch from the repo just forwards to it. Then, one at a time, from the repo:

1. `python3 debrid_applet.py` → Cinnamon panel shows the icon; left-click toggles the window; right-click menu starts with "Show / hide window"; after the first poll the label reads `N↓` (or nothing when idle). `grep 'Tray:' ~/.local/share/debrid-applet/activity.log | tail -1` says `Tray: xapp`. Quit.
2. `DEBRID_APPLET_TRAY=ayatana python3 debrid_applet.py` → the icon appears through xapp-sn-watcher; any click opens the menu; middle-click toggles the window; the label or the hover title shows the count after the first poll; log says `Tray: ayatana`. Quit via the menu.
3. `DEBRID_APPLET_TRAY=none python3 debrid_applet.py` → no icon; the window appears by itself within 2 s; log says `Tray: none`. Press `Esc`: the window hides and the notification "Debrid Applet keeps running" appears once. `python3 debrid_applet.py --toggle` brings it back; `Esc` again: no second notification. Leave it running for Task 3.

- [ ] **Step 7: Early hide before the decision**

With the `none` instance from step 6 quit (Task 3 adds Quit to the window; until then `pkill -f debrid_applet.py`), run `python3 debrid_applet.py --toggle` from the repo and press `Esc` immediately, within a second of the window appearing. Expected: the window hides, no notification, no traceback in `~/.local/share/debrid-applet/applet.log` or the session log; the icon appears normally afterwards. Quit.

- [ ] **Step 8: Commit**

```bash
git add debrid_applet.py
git commit -m "Tray adapter: XApp where a host answers, Ayatana elsewhere, tray-less mode with a window and a hint"
```

---

### Task 3: Quit inside the window

**Files:**
- Modify: `debrid_applet.py:996-997` (ServiceTab ⋮ menu), `debrid_applet.py:2183-2184` (`App.on_key`)

**Interfaces:**
- Consumes: `app.gapp.quit`.
- Produces: nothing new.

- [ ] **Step 1: Add Quit to the ⋮ menu**

In `ServiceTab.__init__`, replace:

```python
        for label, cb in (("Export magnets…", self.on_export), ("Import magnets or links…", self.on_import),
                          ("Copy all magnets", self.on_copy_all_magnets), ("About…", app.open_about)):
```

with:

```python
        for label, cb in (("Export magnets…", self.on_export), ("Import magnets or links…", self.on_import),
                          ("Copy all magnets", self.on_copy_all_magnets), ("About…", app.open_about),
                          ("Quit (Ctrl+Q)", app.gapp.quit)):
```

(The loop calls `cb()` with no arguments; `Gio.Application.quit` takes none.)

- [ ] **Step 2: Add Ctrl+Q**

In `App.on_key`, after the `Ctrl+L` branch:

```python
        elif ctrl and key == Gdk.KEY_l:
            self.open_log()
        elif ctrl and key == Gdk.KEY_q:
            self.gapp.quit()
```

- [ ] **Step 3: Check by hand**

Run: `python3 test_debrid_api.py` → all `ok`. Quit the installed applet, run `python3 debrid_applet.py --toggle` from the repo: ⋮ menu on any tab ends with "Quit (Ctrl+Q)" and quits the process (tray icon gone, `pgrep -f debrid_applet.py` empty). Start again, press `Ctrl+Q` in the window: same.

- [ ] **Step 4: Commit**

```bash
git add debrid_applet.py
git commit -m "Quit in the toolbar menu and Ctrl+Q"
```

---

### Task 4: Wayland hint on the clipboard watcher

**Files:**
- Modify: `debrid_applet.py:2628` (`SettingsForm._behaviour`)

**Interfaces:**
- Consumes: `is_wayland` from Task 1; `SettingsForm._note(grid, text)`.
- Produces: nothing new.

- [ ] **Step 1: Add the note**

In `SettingsForm._behaviour`, replace:

```python
        self._check(g, "watch_clipboard", "Watch the clipboard for magnet links")
```

with:

```python
        self._check(g, "watch_clipboard", "Watch the clipboard for magnet links")
        if is_wayland(type(Gdk.Display.get_default()).__name__):
            self._note(g, "On Wayland the clipboard is only read while this window is focused.")
```

- [ ] **Step 2: Check by hand**

Under this X11 session: open Settings → Behaviour, no note under the checkbox. Under Wayland (Mint 22.3: log out, pick "Cinnamon on Wayland" at the greeter; or a VM later in Task 7's matrix): the note shows. Run `python3 test_debrid_api.py` → all `ok`.

- [ ] **Step 3: Commit**

```bash
git add debrid_applet.py
git commit -m "Settings: Wayland note on the clipboard watcher"
```

---

### Task 5: Packaging and desktop file

**Files:**
- Modify: `packaging/build-deb.sh:43-50`, `debrid-applet.desktop`

**Interfaces:**
- Consumes: nothing.
- Produces: `dist/debrid-applet_<VERSION>_all.deb` with the new control file.

- [ ] **Step 1: Control file**

In `packaging/build-deb.sh`, replace:

```
Depends: python3 (>= 3.11), python3-gi, gir1.2-gtk-3.0, gir1.2-xapp-1.0, gir1.2-notify-0.7, python3-requests, python3-defusedxml
Recommends: mpv | vlc | celluloid, aria2
```

with:

```
Depends: python3 (>= 3.11), python3-gi, gir1.2-gtk-3.0, gir1.2-notify-0.7, python3-requests, python3-defusedxml
Recommends: gir1.2-ayatanaappindicator3-0.1, gir1.2-xapp-1.0, mpv | vlc | celluloid, aria2
```

and replace the description's last line:

```
 Built for Linux Mint Cinnamon (XApp status icon).
```

with:

```
 Tray icon on Cinnamon, KDE, XFCE, MATE and GNOME (with the AppIndicator
 extension); works as a plain window elsewhere.
```

- [ ] **Step 2: Desktop file**

In `debrid-applet.desktop`, after `Categories=Network;` add:

```
Keywords=debrid;torrent;magnet;torbox;real-debrid;alldebrid;
```

- [ ] **Step 3: Build and inspect**

Run: `packaging/build-deb.sh && dpkg-deb -I dist/debrid-applet_1.0.9_all.deb | grep -E '^ (Depends|Recommends|Description)' && dpkg-deb -c dist/debrid-applet_1.0.9_all.deb | grep -c debrid-applet.desktop`
Expected: `Depends` without `gir1.2-xapp-1.0`; `Recommends` starting with `gir1.2-ayatanaappindicator3-0.1, gir1.2-xapp-1.0`; `2` desktop files (applications and autostart). Then `dpkg-deb --fsys-tarfile dist/debrid-applet_1.0.9_all.deb | tar -xO ./usr/share/applications/debrid-applet.desktop | grep Keywords` prints the Keywords line. (The file name still says 1.0.9; Task 7 bumps it.) Validate with `desktop-file-validate debrid-applet.desktop` if `desktop-file-utils` is installed; expected: no output.

- [ ] **Step 4: Commit**

```bash
git add packaging/build-deb.sh debrid-applet.desktop
git commit -m "Package: tray libraries as Recommends, desktop keywords, description for every desktop"
```

---

### Task 6: Documentation

**Files:**
- Modify: `README.md:3`, `README.md` (Install, Trouble?), `docs/MANUAL.md` (Tray icon, Keyboard shortcuts, Troubleshooting), `docs/DEVELOPING.md:20` and the "Where things live at runtime" list

**Interfaces:** none.

- [ ] **Step 1: README**

Line 3: replace `A tray app for Linux Mint that puts` with `A tray app for Linux that puts`.

In "Install", replace the last paragraph:

```
Prefer running from source? Clone the repo and run `./install.sh` (`sudo apt install python3-requests python3-defusedxml` first).
```

with:

```
Works on any Debian-based desktop: the tray icon shows on Cinnamon, KDE, XFCE and MATE, and on GNOME with the
"AppIndicator and KStatusNotifierItem Support" extension; without a tray the window simply opens at start.

Prefer running from source? Clone the repo and run `./install.sh` (`sudo apt install python3-requests python3-defusedxml` first).
On Fedora: `sudo dnf install python3-gobject gtk3 libnotify python3-requests python3-defusedxml`; on Arch:
`sudo pacman -S python-gobject gtk3 libnotify python-requests python-defusedxml`; then `./install.sh` from the release tarball.
```

In "Trouble?", add a second paragraph:

```
No tray icon? Your desktop has no status-icon host (GNOME without the AppIndicator extension, some Wayland sessions): the
window opens at start instead, Quit lives in its ⋮ menu, and the manual's [tray section](docs/MANUAL.md#tray-icon) says what each desktop shows.
```

- [ ] **Step 2: MANUAL**

Replace the "Tray icon" section with:

```
## Tray icon

- Left-click: show / hide the window. Right-click: Show / hide window, Refresh, Settings…, Check for updates…, About…, Quit.
  About… (also in the toolbar ⋮ menu) shows the version, licence and the project page.
- Label next to the icon (e.g. `32↓`) is how many items across all services are still downloading (what the Active filter shows); hover for details.
- Only one instance runs; launching the applet again just raises the window.
- What each desktop shows. The applet uses the XApp status icon where a host answers (Cinnamon; XFCE and MATE with the
  xapp status plugin) and a StatusNotifier indicator elsewhere (KDE Plasma, GNOME with the "AppIndicator and
  KStatusNotifierItem Support" extension, any desktop running xapp-sn-watcher). Indicators open the menu on any click,
  middle-click toggles the window, and some hosts (KDE) do not show the label. Without any host the window opens at
  start, closing it hides it (everything keeps running), a notification says so once, and Quit is in the ⋮ menu or
  `Ctrl+Q`. `DEBRID_APPLET_TRAY=xapp|ayatana|none` in the environment forces a backend.
```

In "Keyboard shortcuts", append ` · \`Ctrl+Q\` quit` after `Ctrl+L\` activity log`.

In "Troubleshooting", add after the "Two tray icons" entry:

```
- **No tray icon**: the desktop has no status-icon host. GNOME: install the "AppIndicator and KStatusNotifierItem
  Support" extension and restart the applet. Otherwise the window is shown at start and Quit is in the ⋮ menu.
```

Also change the "Window gone" entry to: `- **Window gone**: left-click (or middle-click an indicator) the tray icon, run the script again, or \`--toggle\`.`

- [ ] **Step 3: DEVELOPING**

Line 20, replace with:

```
- `debrid_applet.py`: everything GTK: tabs, footer, settings form, watchers, notifications, command line, and the
  `Tray` adapter (XApp or Ayatana, both optional imports; `tray_choice()` is the pure decision, `DEBRID_APPLET_TRAY`
  forces a backend for testing: Cinnamon's xapp-sn-watcher shows the Ayatana path on one box).
```

- [ ] **Step 4: Check and commit**

Run: `grep -n 'Linux Mint' README.md` → nothing but the Mint-specific troubleshooting hint, if any. `grep -c 'Ctrl+Q' docs/MANUAL.md` → `2`.

```bash
git add README.md docs/MANUAL.md docs/DEVELOPING.md
git commit -m "Docs: tray backends per desktop, tray-less mode, other distros"
```

---

### Task 7: Version bump and the release checklist

**Files:**
- Modify: `debrid_applet.py:43`, `README.md:14`

**Interfaces:** none.

- [ ] **Step 1: Bump**

`debrid_applet.py` line 43: `VERSION = "1.1.0"`. `README.md` line 14: `sudo apt install ./debrid-applet_1.1.0_all.deb`.

- [ ] **Step 2: Full check**

Run: `python3 test_debrid_api.py && packaging/build-deb.sh && python3 debrid_applet.py --version`
Expected: all `ok`, `dist/debrid-applet_1.1.0_all.deb` printed, `debrid-applet 1.1.0`.

- [ ] **Step 3: Install the built deb here and run the default backend once more**

Run: `sudo apt install ./dist/debrid-applet_1.1.0_all.deb` then start "Debrid Applet" from the menu. Expected: XApp icon, label after the first poll, `Tray: xapp` in the activity log, Quit from the ⋮ menu works.

- [ ] **Step 4: Commit (no tag)**

```bash
git add debrid_applet.py README.md
git commit -m "Release 1.1.0"
```

The tag and push are the maintainer's: `git tag v1.1.0 && git push origin main v1.1.0` runs the release workflow.

- [ ] **Step 5: Manual matrix before pushing the tag (VMs, not automatable here)**

| Where | Check |
|---|---|
| Box or container without `gir1.2-xapp-1.0` | import succeeds; log says `Tray: ayatana` or `Tray: none` |
| GNOME with the AppIndicator extension | indicator shows; menu; middle-click toggles |
| GNOME without it | window at start, hint on first hide, Quit in ⋮ |
| KDE Plasma, X11 and Wayland | indicator; label ignored (expected); middle-click toggles |
| XFCE, Status Tray plugin | indicator shows |
| Any Wayland session | Settings → Behaviour shows the clipboard note |
| Fresh install on Ubuntu GNOME | both Recommends pulled; no error on first start |

Record anything unexpected in `TODO.md` under the roadmap's step 1 before tagging.
