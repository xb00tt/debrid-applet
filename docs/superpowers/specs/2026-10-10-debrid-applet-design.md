# Debrid Applet — Design

Date: 2026-10-10
Target: Linux Mint 22.3 Cinnamon, Python 3.12, GTK3, AyatanaAppIndicator3, libnotify, requests (all preinstalled).

## Goal

Tray indicator that acts as a mini control panel for one Torbox account and one Real-Debrid account: see account status, watch torrent progress, add magnets / .torrent files / hoster links, get direct download links (open in browser or copy), delete items. Desktop notification when a torrent finishes.

## Decisions (from brainstorm)

| Topic | Decision |
|---|---|
| Form factor | Python AppIndicator tray icon + popup GTK window |
| Features | Account status; torrent list with progress; add magnet / .torrent / link; get links (open in browser / copy); delete |
| Key storage | `~/.config/debrid-applet/config.json`, mode 0600 |
| Polling | Every 30 s in background; notify on completion |
| Downloads | Hand direct URL to `xdg-open` (browser downloads) |
| Layout | One window, `Gtk.Notebook` with tabs Torbox and Real-Debrid |
| Autostart | `install.sh` writes `~/.config/autostart/debrid-applet.desktop` |
| Project | `~/github/debrid-applet`, git |

Out of scope (add only when asked): pause/resume, partial file selection, usenet, streaming, built-in downloader, keyring storage.

## Files

```
debrid_api.py         # RealDebrid and Torbox clients, normalized output, no GTK
debrid_applet.py      # indicator, popup window, polling, notifications, settings
test_debrid_api.py    # offline tests with canned JSON, plain asserts
install.sh            # copy to ~/.local/share/debrid-applet, write .desktop files
debrid-applet.desktop # menu + autostart entry (installed by install.sh)
README.md             # install + usage, 20 lines
```

## API layer: `debrid_api.py`

Both clients expose the same methods and return plain dicts/lists. GTK code never sees service-specific JSON.

```python
class ApiError(Exception): ...

class _Client:
    def __init__(self, token: str, session=None)   # session injectable for tests
    def account(self) -> dict      # {"plan": str, "expires": str|None, "extra": str}
    def torrents(self) -> list     # [{"id", "name", "progress": 0-100 float, "state": str,
                                   #   "done": bool, "size": int bytes, "seeds": int, "speed": int B/s}]
    def add_magnet(self, magnet: str) -> None
    def add_torrent_file(self, path: str) -> None
    def delete(self, torrent_id) -> None
    def links(self, torrent_id) -> list   # [{"name": str, "url": str}] direct CDN URLs
    def add_link(self, url: str) -> str|None  # hoster link; returns direct URL (RD) or None (Torbox, queued)
```

HTTP: `requests.Session`, timeout 15 s, `User-Agent: debrid-applet`. Any non-2xx, JSON `success: false`, or network exception raises `ApiError(user_message)`. HTTP 401/403 raises `ApiError("Invalid API key")`; UI treats that message specially (opens Settings).

### Real-Debrid

Base `https://api.real-debrid.com/rest/1.0`, header `Authorization: Bearer <token>`.

| Method | Call | Notes |
|---|---|---|
| `account` | `GET /user` | plan = `type`; expires = `expiration`; extra = `"{points} points"` |
| `torrents` | `GET /torrents?limit=100` | state = `status`; done = `status == "downloaded"`; size = `bytes`; seeds = `seeders` or 0; speed = `speed` or 0. Items with `status == "waiting_files_selection"` get `POST /torrents/selectFiles/{id}` `files=all` immediately. |
| `add_magnet` | `POST /torrents/addMagnet` form `magnet=` then `POST /torrents/selectFiles/{id}` `files=all` | |
| `add_torrent_file` | `PUT /torrents/addTorrent` raw bytes body, then selectFiles all | |
| `delete` | `DELETE /torrents/delete/{id}` | 204 |
| `links` | `GET /torrents/info/{id}` then `POST /unrestrict/link` `link=` per entry in `links` | name = `filename` from unrestrict; url = `download` |
| `add_link` | `POST /unrestrict/link` `link=` | returns `download` |

RD status values: `magnet_error, magnet_conversion, waiting_files_selection, queued, downloading, downloaded, error, virus, compressing, uploading, dead`.

### Torbox

Base `https://api.torbox.app/v1/api`, header `Authorization: Bearer <token>`. Every response is `{success, error, detail, data}`; `success == false` raises `ApiError(detail)`. Rate limit 300/min; `createtorrent` and `createwebdownload` 60/hour for uncached.

| Method | Call | Notes |
|---|---|---|
| `account` | `GET /user/me` | plan = `{0: "Free", 1: "Essential", 2: "Pro", 3: "Standard"}[plan]`; expires = `premium_expires_at`; extra = `"{total_downloaded} downloaded"` humanized |
| `torrents` | `GET /torrents/mylist?bypass_cache=true` | state = `download_state`; done = `download_finished and download_present`; size = `size`; seeds = `seeds`; speed = `download_speed`; progress = `progress * 100` (API gives 0–1) |
| `add_magnet` | `POST /torrents/createtorrent` multipart `magnet=` | |
| `add_torrent_file` | `POST /torrents/createtorrent` multipart `file=` | |
| `delete` | `POST /torrents/controltorrent` JSON `{"torrent_id": id, "operation": "delete"}` | |
| `links` | `GET /torrents/mylist?id=<id>` for `files`, then `GET /torrents/requestdl?token=&torrent_id=&file_id=` per file | url = `data` (CDN link). `redirect` left false so the token never reaches the browser. |
| `add_link` | `POST /webdl/createwebdownload` form `link=` | returns None; web downloads are listed via `GET /webdl/mylist` and merged into `torrents()` output with `state` prefixed `"web: "`. Delete for those uses `POST /webdl/controlwebdownload` `{"webdl_id", "operation": "delete"}`; links use `/webdl/requestdl?web_id=`. Torrent dicts carry a private `"kind": "torrent"|"webdl"` so the client can route. |

Torbox `download_state` values seen: `downloading, uploading, stalled (no seeds), paused, completed, cached, metaDL, checkingResumeData`, plus other qBittorrent states. Do not use `completed` for done.

The `requestdl` endpoints require the token as a query parameter. This is Torbox's design; the request goes over HTTPS to Torbox only and the resulting CDN URL is what gets opened or copied.

## UI: `debrid_applet.py`

### Indicator
`AyatanaAppIndicator3.Indicator`, id `debrid-applet`, icon `folder-download-symbolic`, category APPLICATION_STATUS. Menu: **Open**, **Refresh**, **Settings…**, **Quit**. Label shows active count (e.g. `2↓`) when any torrent is not done, empty otherwise. Clicking the icon (secondary activate / Open) toggles the popup.

### Popup window
`Gtk.Window`, title "Debrid", 640×480, hides on close (delete-event returns True). Contains a `Gtk.Notebook` with two pages built by one `ServiceTab` class.

`ServiceTab(name, client)`:
- Header: `Gtk.Label` with account line: `Premium · expires 2027-01-03 · 1234 points` or `Not configured` or error text in red.
- Add row: `Gtk.Entry` (placeholder "magnet:, hoster link, or paste"), **Add** button, **.torrent…** `Gtk.FileChooserButton` filtered to `*.torrent`. Add decides: starts with `magnet:` → `add_magnet`; else → `add_link`. If `add_link` returns a URL, it is copied to clipboard and a toast label says "Link copied". Entry clears on success.
- List: `Gtk.TreeView` on `Gtk.ListStore(id:str, name:str, progress:int, state:str, size:str, kind:str)`. Columns: Name (expand, ellipsize), Progress (`Gtk.CellRendererProgress`), State, Size. Row selection enables action buttons.
- Action row: **Links ▾** (builds a `Gtk.Menu` of files, each item opens via `xdg-open`; one extra item **Copy all** puts URLs newline-joined on clipboard), **Copy first link**, **Delete** (no confirm dialog; the service has its own trash semantics and delete is cheap to redo).
- Status label at bottom: last refresh time or last error.

### Polling and threading
- `GLib.timeout_add_seconds(30, self.refresh)` plus manual Refresh.
- `refresh()` starts a `threading.Thread(daemon=True)` that, per configured service, calls `account()` and `torrents()`, then `GLib.idle_add(tab.render, account, torrents, error)`.
- Any user action that hits the network (add, delete, links) runs in a thread the same way; buttons disable while it runs; result or `ApiError` message lands in the status label through `idle_add`.
- Completion detection: each tab keeps `done_ids: set`. After render, new ids in done-set that were previously known-and-not-done trigger `Notify.Notification.new("Torbox: finished", name)`. First poll only seeds the set, no notifications.
- Only one refresh in flight at a time (a `threading.Lock` try-acquire; skip tick if busy).

### Settings
`Gtk.Dialog` with two `Gtk.Entry` (visibility off) for Torbox key and Real-Debrid key, **Save**. Saves config, rebuilds clients, triggers refresh. Opened automatically on startup if both keys are empty, and when an `ApiError("Invalid API key")` arrives.

Config file: `~/.config/debrid-applet/config.json`
```json
{"torbox_key": "", "realdebrid_key": ""}
```
Written with `os.open(..., 0o600)`; directory created with `0o700`. A service with an empty key shows "Not configured" and is skipped by polling.

### Clipboard
`Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(url, -1)` and `store()`.

## Error handling

| Case | Behaviour |
|---|---|
| Network down / timeout | `ApiError("Network error: …")` → red status label; next tick retries; no notification spam |
| Invalid key (401/403, `BAD_TOKEN`) | status "Invalid API key", Settings dialog opens once per key change |
| Torbox `success: false` | `ApiError(detail)` shown verbatim (Torbox says detail is user-safe) |
| Rate limit (429) | `ApiError("Rate limited, retrying next tick")` |
| Malformed .torrent | service error shown verbatim |
| Exceptions in worker thread | caught at thread boundary, converted to status text; never crash the main loop |

## Install: `install.sh`

```
~/.local/share/debrid-applet/{debrid_api.py,debrid_applet.py}
~/.local/share/applications/debrid-applet.desktop   # Exec=python3 ~/.local/share/debrid-applet/debrid_applet.py
~/.config/autostart/debrid-applet.desktop            # same file, copied
```
Idempotent. Prints "Installed. Log out/in or run: python3 ~/.local/share/debrid-applet/debrid_applet.py".

## Testing

`test_debrid_api.py`, run with `python3 test_debrid_api.py`, no network, no GTK:
- Fake session object recording `(method, url, kwargs)` and returning queued canned responses.
- RD: `torrents()` normalizes a canned `/torrents` list; a `waiting_files_selection` item triggers `selectFiles all`; `add_magnet` posts then selects; `links()` unrestricts each link; 401 raises "Invalid API key".
- Torbox: `torrents()` normalizes `mylist` plus `webdl/mylist` (progress ×100, done rule, kind routing); `delete` sends correct JSON per kind; `links()` calls `requestdl` with token param and returns `data`; `success: false` raises `ApiError(detail)`.

Manual check after implementation: run applet with real keys, confirm both tabs populate, add a magnet, see notification on completion, open a link in browser.
