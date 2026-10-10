# Debrid Applet

Tray control panel for **Torbox** and **Real-Debrid** on Linux Mint (Cinnamon).
One window, one tab per service: account status, every torrent with live progress,
add magnets / `.torrent` files / hoster links, stream or download the results, tidy up.

Python 3 + GTK 3, no dependencies beyond a stock Mint 22 install.

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
(from <https://torbox.app/settings> and <https://real-debrid.com/apitoken>) → **Test keys** → **Save**.
Keys are stored in `~/.config/debrid-applet/config.json` with mode 600 and never leave your machine
except in requests to the two services.

## Tray icon

- Left-click: show / hide the window. Right-click: Refresh, Settings…, Quit.
- Label next to the icon shows how many items are still downloading.
- Only one instance runs; launching the applet again just raises the window.

## Adding things

- **+** opens a box for magnets and hoster links, one per line. On the Torbox tab each magnet is marked
  ✓ cached / ✗ not cached as you type. Real-Debrid hoster links are unrestricted on the spot and the direct
  URLs copied to the clipboard; Torbox queues them as web downloads.
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
| **Copy magnet** | context menu | magnet links rebuilt from the info hashes |
| **Copy as aria2c command** | context menu | resumable 8-connection `aria2c` command into `~/Downloads` |
| **Resume / Stop seeding / Reannounce** | context menu, Torbox only | what Torbox lets you do with a torrent; Torbox confirms at once but the State column can lag |
| **Delete** | toolbar, context menu, `Delete` key | removes from the service; asks once when more than one is ticked |

## Finding things

- Filter box matches name or state as you type; the dropdown next to it narrows by state
  (Active / Done / Seeding / Stalled / Error); the dropdown choice is remembered per service (toggle in Settings). `Ctrl+F` focuses the box, `Esc` clears it.
- Lists start newest-first. Click a header to sort, drag headers to reorder, right-click a header to show or hide columns.
  Layout and sort are remembered per service. Real-Debrid hides Up / Peers / Ratio by default since it never reports them.
- Dates read "2 hours ago"; hover for the exact time.

## Keys

`Delete` · `Ctrl+A` tick all / none · `Ctrl+C` copy links · `Ctrl+F` filter · `Ctrl+R` refresh · `Esc` clear filter, then hide window

## Behind the scenes

- Polls every 8 s while something is downloading, every 60 s when idle. Hover the tray icon to see the current interval.
- Notifications when an item is added and when it finishes on the service (both toggles in Settings).
  The "finished" one carries an **Open files** button.
- Red text in the status line is the service's own error message; hover the status line for the last few.
- Window size, column layout and sort order persist across restarts.

## Dev

```bash
python3 test_debrid_api.py
```

Offline tests with canned API responses, no GTK needed. `debrid_api.py` holds the two HTTP clients
behind one interface; `debrid_applet.py` is the GTK side. Design notes live in `docs/superpowers/`.
