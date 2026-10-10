# To do

Built so far: everything from the UX review, three services verified live, and the first batch of power features
(watch folder, auto-download, local API, expiry warnings, subtitle-aware Play).

Remaining ideas from the power-user list, none started:

- **RSS / watch lists** with regex filters and auto-add.
- **Stremio / Kodi / Jellyfin hand-off** for Play.
- **Merged view** across services with one search box.
- **mpv resume position** (`--save-position-on-quit`) and **season-pack playlists** (all episodes queued).
- **Retry queue for adds** when a service rate-limits or is down.
- **Keyring storage** for API keys via libsecret.
- **Config schema version** with migration hooks.
- Another service (Premiumize etc.): recipe in the README's Dev section; only with an account to test against.
- AllDebrid hoster-link unlock: request path verified live; a successful unlock of a real hoster link hasn't been observed yet.
