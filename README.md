# Debrid Applet

Tray control panel for **Torbox**, **Real-Debrid** and **AllDebrid** on Linux Mint (Cinnamon).
One window, one tab per service: account status, every torrent with live progress,
add magnets / `.torrent` files / hoster links, stream or download the results, tidy up.

Python 3 + GTK 3, no dependencies beyond a stock Mint 22 install.
All three services are exercised against their live APIs: account, add (magnet, `.torrent`, link), list, files, play, delete.

## Install

```bash
./install.sh
```

Copies the applet to `~/.local/share/debrid-applet`, adds a menu entry and an autostart entry.
Start it right away with the command the installer prints. Needs `python3-gi`, `gir1.2-xapp-1.0`,
`gir1.2-notify-0.7`, `python3-requests`; a media player (mpv, VLC, Celluloid, SMPlayer or Totem)
is optional and enables **Play**.

## First run

Right-click the tray icon → **Settings…** → paste your API keys
(from <https://torbox.app/settings>, <https://real-debrid.com/apitoken>, <https://alldebrid.com/apikeys>) → **Test keys** → **Save**.
Leave a key empty and that tab just says "Not configured".
Keys are stored in `~/.config/debrid-applet/config.json` with mode 600 and never leave your machine
except in requests to the services.

## Tray icon

- Left-click: show / hide the window. Right-click: Refresh, Settings…, Quit.
- Label next to the icon shows how many items are still downloading.
- Only one instance runs; launching the applet again just raises the window.

## Adding things

- **+** opens a box for magnets and hoster links, one per line. On the Torbox tab each magnet is marked
  ✓ cached / ✗ not cached as you type. Real-Debrid hoster links are unrestricted on the spot and the direct
  URLs copied to the clipboard; Torbox queues them as web downloads; AllDebrid unlocks them like Real-Debrid.
- Folder icon picks one or more `.torrent` files.
- Drop `.torrent` files, links or magnet text anywhere on a tab.
- Copy a magnet link anywhere on the desktop: a notification offers **Add to Torbox / Add to Real-Debrid**
  (toggle in Settings).
- A placeholder row appears immediately; the real row replaces it on the next poll.

## Working with items

Tick rows with the checkboxes (shift-click ticks a range, header checkbox ticks all), or just highlight one.

| Action | Where | What it does |
|---|---|---|
| **Play** | toolbar, context menu | streams the video in your player; picker when there are several |
| **Files** | toolbar, context menu, double-click, finished notification | panel with every file and its size; tick some, then Download via browser / Play / Copy links / aria2c command |
| **Copy links** | toolbar, context menu, `Ctrl+C` | every direct URL of the ticked items |
| **Copy magnet** | context menu | magnet links rebuilt from the info hashes (not on AllDebrid, its API doesn't return them) |
| **Copy as aria2c command** | context menu | resumable 8-connection `aria2c` command into `~/Downloads` |
| **Resume / Stop seeding / Reannounce** | context menu, Torbox only | what Torbox lets you do with a torrent; Torbox confirms at once but the State column can lag |
| **Delete** | toolbar, context menu, `Delete` key | removes from the service; asks once when more than one is ticked |

## Finding things

- Filter box matches name or state as you type; the dropdown next to it narrows by state
  (Active / Done / Seeding / Stalled / Error); the dropdown choice is remembered per service (toggle in Settings). `Ctrl+F` focuses the box, `Esc` clears it.
- Lists start newest-first. Click a header to sort, drag headers to reorder or their edges to resize, right-click a header to show or hide columns.
  Layout, widths and sort are remembered per service. Columns a service never reports are hidden by default
  (Real-Debrid: Up / Peers / Ratio, AllDebrid: Peers / Ratio).
- Dates read "2 hours ago"; hover for the exact time.

## Keys

`Delete` · `Ctrl+A` tick all / none · `Ctrl+C` copy links · `Ctrl+F` filter · `Ctrl+R` refresh · `Esc` clear filter, then hide window

## Power features

- **Watch folder.** Point Settings at a folder; every `.torrent` that lands there is uploaded to the service you
  pick and moved to `done/` inside that folder. Files already there are picked up at start; a failed upload stays put.
- **Auto-download.** With "Download finished items automatically" on, each item that finishes on a service is
  pulled into `<download folder>/<item name>/` with `aria2c` (8 connections) if installed, else `wget -c`.
  Output goes to `download-<name>.log` next to the applet; a notification tells you when it starts.
- **Local API.** Set a port in Settings (0 = off); the applet listens on `127.0.0.1` only.

      curl 'http://127.0.0.1:PORT/add?service=Torbox&magnet=magnet:?xt=urn:btih:…'
      curl -X POST --data-binary @links.txt 'http://127.0.0.1:PORT/add?service=AllDebrid'   # one item per line
      curl http://127.0.0.1:PORT/toggle      # show / hide the window
      curl http://127.0.0.1:PORT/status      # active count and item count per service

  Bookmarklet for tracker pages (replace PORT):

      javascript:fetch('http://127.0.0.1:PORT/add?service=Torbox&magnet='+encodeURIComponent(location.href))

  `service` is optional; without it the first configured service is used.
- **Expiry warning.** One notification per service when premium ends within 7 days.
- **Subtitles.** Play hands a matching `.srt` / `.ass` / `.vtt` from the same item to mpv, Celluloid, SMPlayer
  or VLC (same file name as the video, or the item's only subtitle).

## Settings

Right-click the tray icon → **Settings…**, or the gear in the toolbar.

- Torbox, Real-Debrid and AllDebrid API keys, with **Test keys** to check them live.
- Watch folder and which service it uploads to; download folder; local API port.
- Notify when a torrent is added · Notify when a download finishes on the service (the "finished"
  notification carries an **Open files** button).
- Watch clipboard for magnet links.
- Remember the state filter between runs.
- Double-click plays videos (otherwise opens Files). Off by default; the first time you double-click an item with video files the applet asks once whether to turn it on. When on, items without video files still open Files.
- Download finished items automatically. Off by default.

The first four toggles are on by default.

## Behind the scenes

- Polls every 8 s while something is downloading, every 60 s when idle. Hover the tray icon to see the current interval.
- Red text in the status line is the service's own error message; hover the status line for the last few.
- Window size, column layout and widths, sort order and state filter persist across restarts, per service.
- Config lives in `~/.config/debrid-applet/config.json`; delete it to start over. To see the one-time double-click question again, set `dblclick_asked` to `false` there.

## Dev

```bash
python3 test_debrid_api.py
```

Offline tests with canned API responses, no GTK needed. `debrid_api.py` holds the three HTTP clients
behind one interface; `debrid_local_api.py` is the loopback HTTP API; `debrid_applet.py` is the GTK side. Design notes live in `docs/superpowers/`.

The GTK side has no unit tests; it is checked by rendering a `ServiceTab` with sample rows into a
`Gtk.OffscreenWindow` and by driving the running applet with `xdotool` plus screenshots. Note that
`xdotool type` mangles `[` on some keyboard layouts, so use bracket-free paths when scripting the file chooser.

Adding a service: one client class in `debrid_api.py` implementing `account`, `torrents`, `add_magnet`,
`add_torrent_file`, `add_link`, `links`, `delete` (optionally `check_cached` and `control`), a key field in
Settings, a line in `build_tabs()`, a default in `DEFAULT_HIDDEN` for columns it never reports.
See `TODO.md` for what is not planned.
