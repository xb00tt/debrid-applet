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
| **Play** | toolbar, context menu, double-click | streams the video in your player; picker when there are several |
| **Files** | toolbar, context menu, finished notification | panel with every file and its size; tick some, then Download via browser / Play / Copy links / aria2c command |
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

## Settings

Right-click the tray icon → **Settings…**, or the gear in the toolbar.

- Torbox, Real-Debrid and AllDebrid API keys, with **Test keys** to check them live.
- Notify when a torrent is added · Notify when a download finishes on the service (the "finished"
  notification carries an **Open files** button).
- Watch clipboard for magnet links.
- Remember the state filter between runs.

All four toggles are on by default.

## Behind the scenes

- Polls every 8 s while something is downloading, every 60 s when idle. Hover the tray icon to see the current interval.
- Red text in the status line is the service's own error message; hover the status line for the last few.
- Window size, column layout and widths, sort order and state filter persist across restarts, per service.
- Config lives in `~/.config/debrid-applet/config.json`; delete it to start over.

## Dev

```bash
python3 test_debrid_api.py
```

Offline tests with canned API responses, no GTK needed. `debrid_api.py` holds the two HTTP clients
behind one interface; `debrid_applet.py` is the GTK side. Design notes live in `docs/superpowers/`.
