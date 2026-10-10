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
- Paste a `magnet:` link or hoster URL, press Enter. Real-Debrid hoster links are unrestricted and the direct URL is copied to the clipboard; Torbox queues them as web downloads.
- `.torrent` button uploads a file.
- Select a row: **Links ▾** lists files (click opens in browser, or copy all), **Copy first link**, **Delete**.
- Polls every 30 s. Desktop notification when an item finishes.

## Dev

    python3 test_debrid_api.py
