# Debrid Applet

Tray control panel for Torbox and Real-Debrid on Linux Mint (Cinnamon).
Shows account status and torrents, adds magnets / .torrent files / hoster links,
opens download links in the browser, deletes items, notifies when a download finishes.

## Install

    ./install.sh

Needs `python3-gi`, `gir1.2-xapp-1.0`, `gir1.2-notify-0.7`, `python3-requests`
(all present on a stock Mint 22 install). Autostarts on login; start it now with the command the installer prints.

## First run

Tray menu → Settings… → paste API keys from https://torbox.app/settings and
https://real-debrid.com/apitoken → Save. Keys live in `~/.config/debrid-applet/config.json` (mode 600).

## Use

- Left-click the tray icon to show/hide the popup (one tab per service); right-click for Refresh / Settings / Quit.
- `+` opens a box for magnets and hoster links, one per line. On the Torbox tab each magnet is marked cached / not cached as you type. Real-Debrid hoster links are unrestricted and the direct URLs are copied to the clipboard; Torbox queues them as web downloads.
- Folder icon picks one or more `.torrent` files. Dropping `.torrent` files, links or magnets onto a tab adds them too. Refresh and settings icons sit next to it.
- Tick rows (or just highlight one): **Links ▾** lists files of one item (click opens in browser), **Copy links** copies every direct URL of the ticked items, **Delete** removes them (asks when more than one).
- **Play** streams the item's video in mpv / VLC / Celluloid (whichever is installed); double-click a row does the same. Right-click a row for Play, Links, Copy links, Copy magnet, Delete.
- Keys: `Delete`, `Ctrl+A` tick all, `Ctrl+C` copy links, `Ctrl+F` filter, `Ctrl+R` refresh, `Esc` hide.
- Copy a magnet link anywhere and a notification offers "Add to Torbox / Real-Debrid" (toggle in Settings).
- Click a header to sort, drag headers to reorder, right-click a header to show/hide columns. Layout is remembered.
- Only one instance runs; launching again raises the window.
- Polls every 30 s. Desktop notification when an item finishes.

## Dev

    python3 test_debrid_api.py
