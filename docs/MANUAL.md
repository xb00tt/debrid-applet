# Debrid Applet manual

Everything the applet does, in detail. For installing and the basics see the [README](../README.md).

## Tray icon

- Left-click: show / hide the window. Right-click: Refresh, Settings…, About…, Quit. About… (also in the toolbar ⋮
  menu) shows the version, licence and the project page.
- Label next to the icon (e.g. `32↓`) is how many items across all services are still downloading (what the Active filter shows); hover for details.
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
- The second dropdown shows only items added within the last 24 hours / 7 days / 30 days, or any time. Each run,
  and every Settings save, starts at the default from Settings (7 days unless you change it); items whose added date
  is unknown always show.
- Lists start newest-first. Click a header to sort, drag headers to reorder or their edges to resize, right-click a header to show or hide columns.
  Layout, widths and sort are remembered per service. Columns a service never reports are hidden by default
  (Real-Debrid: Up / Peers / Ratio, AllDebrid: Peers / Ratio).
- Dates read "2 hours ago"; hover for the exact time.

## Keyboard shortcuts

`Delete` · `Ctrl+A` tick all / none · `Ctrl+C` copy links · `Ctrl+F` filter · `Ctrl+R` refresh · `Ctrl+L` activity log · `Esc` clear filter, then hide window

## Power features

### Automation

- **Watch folder.** Point Settings at a folder; every `.torrent` that lands there is uploaded to the service you
  pick and moved to `done/` inside that folder. Files already there are picked up at start; a failed upload stays put.
- **Auto-download.** With "Download finished items automatically" on, each item that finishes on a service is
  pulled into `<download folder>/<item name>/` with `aria2c` (8 connections) if installed, else `wget -c`.
  Output goes to `download-<name>.log` next to the applet; notifications tell you when it starts and ends.
  Options: videos only; delete the item from the service once the local download finished.
- **Stop seeding when done** (Torbox): sends `stop_seeding` the moment an item finishes.
- **Prefer cached.** A checkbox in the Add box (and the default for API calls that name no service) sends magnets
  Torbox already has cached to Torbox and the rest to the current tab.
- **RSS watch lists.** Settings takes feeds, one per line as `url | regex | service` (regex and service optional;
  the regex is matched case-insensitively against the item title). Every N minutes (default 15) new matching items
  whose link is a magnet or a `.torrent` file are added to that service (a service without a key is skipped until
  you add one); `.torrent` URLs are fetched and uploaded.
  Seen items are remembered so nothing is added twice. Fetches are capped at 5 MB; `.torrent` URLs are only
  fetched from public hosts (feeds you configure may live on your LAN). The activity log never records a feed or
  .torrent URL's query string, where tracker passkeys live.

### Integration

- **Local API.** Set a port in Settings (0 = off); the applet listens on `127.0.0.1` only and generates a
  random token, shown in Settings. Every call must carry it as `?token=…` or an `X-Debrid-Token` header, so a
  web page cannot drive the applet behind your back. `/add` accepts `magnet:` and `http(s)://` items only; a
  `service` that is unknown or has no key gets a 400.

      curl 'http://127.0.0.1:PORT/add?token=TOKEN&service=Torbox&magnet=magnet:?xt=urn:btih:…'
      curl -H 'X-Debrid-Token: TOKEN' -X POST --data-binary @links.txt 'http://127.0.0.1:PORT/add?service=AllDebrid'
      curl "http://127.0.0.1:PORT/toggle?token=TOKEN"      # show / hide the window
      curl "http://127.0.0.1:PORT/status?token=TOKEN"      # active count and item count per service

  Bookmarklet for tracker pages (replace PORT and TOKEN; keep it to yourself, it contains the token):

      javascript:fetch('http://127.0.0.1:PORT/add?token=TOKEN&service=Torbox&magnet='+encodeURIComponent(location.href),{mode:'no-cors'})

  `service` is optional; without it magnets cached on Torbox go to Torbox and the rest to the first configured service.
- **Command line.** The installed script forwards flags to the running instance:

      python3 ~/.local/share/debrid-applet/debrid_applet.py --toggle
      python3 ~/.local/share/debrid-applet/debrid_applet.py --log
      python3 ~/.local/share/debrid-applet/debrid_applet.py --service Torbox --add 'magnet:?xt=…' 'https://hoster/file'
      debrid-applet --version          # the .deb puts `debrid-applet` on PATH

  Without `--service`, cached-on-Torbox routing applies; `--service` naming a service without a key adds nothing and
  exits 1. Bind `--toggle` to a Cinnamon keyboard shortcut for a global hotkey.
- **Updates.** Once a day (and 60 s after start) the applet asks GitHub for the latest release and, when it is newer,
  shows a notification with Update / Release notes / Skip this version. Right-click the tray icon → **Check for
  updates…** (or the button in About) checks at once. Update downloads the release, verifies it against the checksum
  GitHub publishes, and installs it: the `.deb` through `apt-get` behind one password prompt (the checksum is checked
  again as root, on a private copy, right before the install), a `./install.sh` copy by replacing its files, a git
  checkout only tells you to `git pull`. Then it offers to restart. Turn the daily check off under Settings →
  Advanced. Nothing is ever installed without your click; a cancelled password prompt just stops, and an `apt-get`
  failure leaves the file in `~/.local/share/debrid-applet/updates/` and offers the `sudo apt install` line to copy.
- **Send to.** Right-click → Send to → another service re-adds the ticked items there by magnet (not from AllDebrid, which gives no hashes).
- **Export / import.** The ⋮ menu in the toolbar exports every item's magnet (`magnet<TAB>name` per line) to a file,
  imports such a file (or any file with one magnet or link per line), or copies all magnets to the clipboard.
- **Custom play target.** Settings → "Custom play command": `{url}` is the stream, `{sub}` an optional subtitle,
  e.g. `stremio {url}`, `kodi-send --action=PlayMedia({url})`, `mpv --fs {url} --sub-file={sub}`. Empty = the detected player.

### Playback

- **Resume and playlists.** mpv and Celluloid resume where you stopped; a pack's Play picker has "Play all N in order".
- **Subtitles.** Play hands a matching `.srt` / `.ass` / `.vtt` from the same item to mpv, Celluloid, SMPlayer
  or VLC (same file name as the video, or the item's only subtitle).

### Visibility

- **All tab.** With more than one service configured, the first tab lists everything together with a colored
  **Service** column; Files, Play, Copy, Delete and friends act on the owning service. Adds from that tab go to the
  first configured service. Settings → Behaviour picks which tab the window opens on (All by default).
- **Expiry warning.** One notification per service when premium ends within 7 days.
- **Activity log.** Clock icon in the toolbar (or `Ctrl+L`): status messages and notifications with a timestamp,
  newest last, with Refresh / Open file / Clear. Kept in `~/.local/share/debrid-applet/activity.log`. Settings choose
  the level (Off / Errors only / Actions and notifications / Verbose with every poll) and the size cap (default 1000 KB).

## Settings

Right-click the tray icon → **Settings…**, or the gear in the toolbar. Four pages:

- **Accounts**: the three API keys with **Test keys**.
- **Behaviour**: notifications (added, finished), tab on start-up, clipboard watcher, remembered state filter,
  default added-within window, double-click plays videos, custom play command.
- **Automation**: watch folder and its target service; RSS feeds and their interval; what happens when an item
  finishes (auto-download, videos only, delete afterwards, download folder; Torbox stop seeding).
- **Advanced**: local API port and token; daily update check; activity log level and size cap.

Notifications, clipboard watcher and remembered filter are on by default; everything else is off until you turn it on.

## Behind the scenes

- Polls every 8 s while something is downloading, every 60 s when idle. Hover the tray icon to see the current interval.
- On the fast cadence Real-Debrid and AllDebrid are asked for active items only and the result is merged into the cached list; the full list (Real-Debrid paged 5000 at a time) is fetched once a minute or as soon as an active item disappears. Rows update in place, so lists with 10k+ items stay usable.
- The footer has two rows: the last action or error (red is the service's own message; hover for the last few,
  or open the activity log), a spinner while anything is in flight, and "updated … · every N s"; below that a
  colored plan badge (green paid, amber trial, red free), expiry with a countdown, points or downloads, live totals
  (down/up speed, active, items, total size, "showing N" under a filter, "K ticked") and a button to the service's
  web dashboard.
- Window size, column layout and widths, sort order and state filter persist across restarts, per service.
- Adds that fail with a rate limit or a network error are retried every 60 s, up to three times.
- Config lives in `~/.config/debrid-applet/config.json` (versioned, migrated on load); delete it to start over. To see the one-time double-click question again, set `dblclick_asked` to `false` there.

## Troubleshooting

- **Tabs say "Not configured"**: open Settings → Accounts and check the keys; Test keys tells you which one fails.
  If the keys vanished, `~/.config/debrid-applet/config.json.bak` is the last version that still had them.
- **Two tray icons**: one is a leftover from an instance that was killed mid-poll; it disappears on the next restart
  of the applet, or restart Cinnamon (Ctrl+Alt+Esc on Mint).
- **Window gone**: left-click the tray icon, run the script again, or `--toggle`.
- **Something failed silently**: `Ctrl+L` opens the activity log; the process's own stderr lands in the session log
  (`journalctl --user -b` on systemd sessions, `~/.xsession-errors` on others).

