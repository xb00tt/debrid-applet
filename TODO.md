# To do

Built so far: everything from the UX review, three services verified live, and the first batch of power features
(watch folder, auto-download, local API, expiry warnings, subtitle-aware Play).

Remaining ideas from the power-user list, none started:

- **Rules on add**: per-service defaults such as videos only, stop seeding when done (Torbox), delete from the service after the local download.
- **Prefer-cached routing**: one add box that checks caches and sends the magnet to whichever service already has it.
- **RSS / watch lists** with regex filters and auto-add.
- **Stremio / Kodi / Jellyfin hand-off** for Play.
- **CLI flags** (`--add <magnet>`, `--toggle`) via `Gio.Application` command-line handling, so a Cinnamon hotkey can drive it; the local API already covers the shell use case.
- **Send to another service**: copy an item's magnet from one tab and add it to another in one click.
- **Totals row**: summed down/up speed in the status line and tray tooltip.
- **Merged view** across services with one search box.
- **Export / import** of magnets (hash + name) per service.
- **mpv resume position** (`--save-position-on-quit`) and **season-pack playlists** (all episodes queued).
- **Retry queue for adds** when a service rate-limits or is down.
- **Lighter fast polls for huge Real-Debrid lists**: poll `/torrents?filter=active` on the 8 s cadence and the full (paged, 10k+) list only on the 60 s one.
- **Keyring storage** for API keys via libsecret.
- **Config schema version** with migration hooks.
- Another service (Premiumize etc.): recipe in the README's Dev section; only with an account to test against.
- AllDebrid hoster-link unlock: request path verified live; a successful unlock of a real hoster link hasn't been observed yet.
