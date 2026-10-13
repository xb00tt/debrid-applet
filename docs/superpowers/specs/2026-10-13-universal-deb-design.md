# Universal deb — Design

Date: 2026-10-13
Base: debrid-applet 1.0.9. Target version: 1.1.0.

## Goal

The same `.deb` runs on any Debian-based desktop, not only Linux Mint Cinnamon: Debian and Ubuntu with GNOME, KDE Plasma,
XFCE, MATE, Cinnamon, and their derivatives, on X11 and Wayland. Every feature that works today keeps working where the
desktop allows it; where it cannot (no tray host, Wayland clipboard) the applet degrades visibly instead of silently.

Lightness is a constraint: no new hard dependencies, no second process, GTK3 stays.

## Non-goals (later specs)

- Reverse-DNS app id and AppStream metainfo (needed for Flathub; done with the Flatpak step).
- Flatpak manifest, RPM spec, AUR PKGBUILD.
- A daemon / frontend split.
- Making the clipboard watcher work on Wayland while the window is unfocused (compositors forbid it).

## What breaks today off Cinnamon

| Cause | Effect |
|---|---|
| `gi.require_version("XApp", "1.0")` is unconditional (`debrid_applet.py:34`) | ImportError before the window exists on any box without `gir1.2-xapp-1.0` |
| `XApp.StatusIcon` without an XApp host falls back to XEmbed `Gtk.StatusIcon` | icon visible on KDE / XFCE / MATE under X11, invisible on GNOME and on every Wayland session |
| Quit exists only in the tray menu (`debrid_applet.py:2065`) | no tray, no way to quit except kill |
| The window starts hidden | no tray, nothing visible, the app looks crashed |
| `Depends: gir1.2-xapp-1.0` in `packaging/build-deb.sh` | pulls a Mint library onto every desktop; without a host it does nothing |
| GTK3 clipboard `owner-change` fires only while focused on Wayland | "copy a magnet, get a notification" silently stops working |

Measured on this box (Cinnamon, X11): a fresh `XApp.StatusIcon` reports state `no-support`, then emits `state-changed`
with `native` about 1 ms later from the main loop. The backend decision therefore cannot be made synchronously after
construction.

## Design

### 1. Optional desktop imports

`XApp` and `AyatanaAppIndicator3` are imported the way `Secret` already is: `try: gi.require_version(...); from
gi.repository import ...` / `except (ValueError, ImportError): XApp = None`. Nothing else at import time changes.

### 2. Tray adapter

One small class, `Tray`, replaces the five direct `self.icon` uses in `App.__init__` and `App.refresh`:

```python
class Tray:
    """The status icon behind one interface: XApp where a host answers, Ayatana elsewhere, nothing as a last resort."""
    def __init__(self, app, menu, show_item):   # menu: Gtk.Menu; show_item: its "Show / hide window" entry
    kind: str                                   # "xapp" | "xapp-fallback" | "ayatana" | "none"
    def set_count(self, active, interval): ...  # label "N↓" and tooltip / title text
    def present(self) -> bool                   # True when a host is known to show the icon (kind in ("xapp", "ayatana"))
```

Backend selection:

1. `XApp` importable: build the `XApp.StatusIcon` exactly as today, connect `state-changed`, and arm a one-shot 2 s
   timer. The first of these decides:
   - `state-changed` → `NATIVE`: keep it, `kind = "xapp"`, cancel the timer.
   - `state-changed` → `FALLBACK` / `NO_SUPPORT`, or the timer fires without `NATIVE`: go to step 2.
2. `AyatanaAppIndicator3` importable: hide the XApp icon (`set_visible(False)`, drop the reference), then
   `Indicator.new(APP_ID, APP_ID, IndicatorCategory.APPLICATION_STATUS)`, `set_icon_theme_path(HERE)` (the SVG next to
   the code is named `debrid-applet.svg`, so the icon name resolves both from source and from the installed theme),
   `set_status(ACTIVE)`, `set_menu(menu)`, `set_secondary_activate_target(show_item)` (middle click toggles the window),
   `kind = "ayatana"`.
3. Neither works: keep the XApp icon if one exists (`kind = "xapp-fallback"`, visible on XEmbed trays, nowhere else),
   else `kind = "none"`.

`set_count` maps onto each backend: XApp `set_label` + `set_tooltip_text` as today; Ayatana `set_label(text, "")` +
`set_title(tooltip)` (indicators have no tooltip API; hosts that show labels show the count, KDE shows neither and relies
on the menu); fallback and none: no-op.

Menu: the tray menu gains "Show / hide window" as its first entry on every backend (XApp keeps left-click too). The
rest of the menu is unchanged: Refresh, Settings…, Check for updates…, About…, Quit.

Test knob: `DEBRID_APPLET_TRAY=xapp|ayatana|none` forces a backend. Cinnamon ships `xapp-sn-watcher`, so the Ayatana
path can be exercised on this machine; `none` exercises the tray-less mode without a GNOME VM.

### 3. Tray-less mode

Applies whenever `tray.present()` is False after the decision above (GNOME without the AppIndicator extension, any
Wayland session with only XApp's XEmbed fallback, no tray library at all):

- The window is shown at start instead of hidden. The decision arrives up to 2 s after `App.__init__`; until then the
  window stays hidden as today, then `show_all` + `present` if no host turned up.
- Closing the window still hides it (watch folder, RSS and auto-download keep running). The first hide in a session
  without a tray posts one notification: "Debrid Applet keeps running. Open it from the menu or `debrid-applet --toggle`;
  Quit is in the ⋮ menu." Per-session flag, nothing in the config.
- Relaunching from the application menu already raises the running instance (single-instance `Gtk.Application`); no new
  code.

### 4. Quit inside the window (all desktops)

- "Quit" at the end of the ⋮ menu built in `ServiceTab.__init__` (`debrid_applet.py:995`), after "About…".
- `Ctrl+Q` in `App.on_key`. Documented next to the other shortcuts.

Both regardless of tray backend: cheap and expected of any desktop app.

### 5. Wayland hint for the clipboard watcher

`SettingsForm` (`debrid_applet.py:2628`): when `type(Gdk.Display.get_default()).__name__` contains `Wayland`, the
"Watch the clipboard for magnet links" checkbox gets the hint "On Wayland the clipboard is only read while this window
is focused". The option stays available; it still works while focused.

### 6. Packaging and desktop file

`packaging/build-deb.sh`:

```
Depends: python3 (>= 3.11), python3-gi, gir1.2-gtk-3.0, gir1.2-notify-0.7, python3-requests, python3-defusedxml
Recommends: gir1.2-ayatanaappindicator3-0.1, gir1.2-xapp-1.0, mpv | vlc | celluloid, aria2
```

Both tray libraries are Recommends: apt installs Recommends by default, both are small, and whichever host the desktop
has gets used. The package description drops "Built for Linux Mint Cinnamon (XApp status icon)" for "Tray icon on
Cinnamon, KDE, XFCE, MATE and GNOME (with the AppIndicator extension); works as a plain window elsewhere."

`debrid-applet.desktop`: add `Keywords=debrid;torrent;magnet;torbox;real-debrid;alldebrid;` (menu search on every
desktop). `X-GNOME-Autostart-enabled` stays; `/etc/xdg/autostart` is honoured by all five desktops.

`install.sh`, `.github/workflows/tests.yml`: unchanged (the CI image keeps `gir1.2-xapp-1.0`; the optional import makes
it irrelevant to the tests either way).

`VERSION = "1.1.0"`.

### 7. Documentation

- README: the install line says "any Debian-based distro"; a "No tray icon?" line under Trouble? pointing at the
  manual. One sentence for other distros: install `python3-gobject`, GTK3, libnotify, requests and defusedxml from
  the distro repos and run `./install.sh` from the release tarball.
- MANUAL: tray section gains the backend matrix (which desktops show the icon, label and left-click); Quit and
  `Ctrl+Q` added to the shortcuts line; troubleshooting entry "No tray icon" (GNOME: install the "AppIndicator and
  KStatusNotifierItem Support" extension; otherwise the window is shown at start and Quit lives in the ⋮ menu).
- DEVELOPING: the tray adapter and the `DEBRID_APPLET_TRAY` knob.

## Testing

Offline (`test_debrid_api.py`, no GTK):

- `tray_choice(xapp_state, have_ayatana, have_xapp)` → `"xapp" | "ayatana" | "xapp-fallback" | "none"`: the decision
  table above as one pure function, one test over its rows.
- `is_wayland(display_type_name)`: one assert each way.

Manual, before tagging:

| Where | Check |
|---|---|
| This box, Cinnamon X11, default | XApp native: icon, label, left-click toggle, menu, Quit, Ctrl+Q |
| This box, `DEBRID_APPLET_TRAY=ayatana` | indicator via xapp-sn-watcher: icon, menu with Show / hide first, middle click toggles, label or title shows the count |
| This box, `DEBRID_APPLET_TRAY=none` | window shown at start, close hides with the one-time notification, relaunch raises, Quit works |
| Box without `gir1.2-xapp-1.0` (container or VM) | import succeeds, Ayatana or none chosen |
| VM: GNOME with and without the extension | indicator with extension; tray-less without |
| VM: KDE Plasma X11 and Wayland | indicator, menu, middle click; label ignored (expected) |
| VM: XFCE | indicator in the Status Tray plugin |
| Any Wayland session | Settings shows the clipboard hint |
| Fresh deb install on Ubuntu GNOME | both Recommends pulled, no error on first start |

## Risks and ceilings

- The 2 s timer is a guess: Cinnamon answers in 1 ms; a slow session bus on a cold login could exceed it and land on
  Ayatana even where XApp would work. Harmless (both show on Cinnamon), and the knob overrides it.
- Ayatana label support differs per host; the count is a convenience, the menu and tooltip carry the same number.
- Wayland and GNOME-without-extension users live in tray-less mode. Acceptable; documented.
- No automated GUI test covers the switch; the knob plus the manual table above is the verification.

## Size of the change

About 120 lines of Python (adapter, tray-less mode, Quit, hint, two helpers), one control-file edit, one desktop-file
line, two tests, documentation. No new runtime dependency; two existing optional ones.
