# To do

Built so far: everything from the UX review, three services verified live, and the whole power-user list
(hand-off to Stremio / Kodi / Jellyfin is covered by the custom play command).

Open notes:

- Keyring storage for API keys was removed after it lost keys twice on this machine; if it comes back it needs a cross-restart verification step before the file copy is dropped.
- Another service (Premiumize etc.): recipe in the README's Dev section; only with an account to test against.
- AllDebrid hoster-link unlock: request path verified live; a successful unlock of a real hoster link hasn't been observed yet.

Known ceilings from the 2026-10-11 security/refactor audit (accepted for now, each needs more code than it is worth today):

- `fetch_bytes` gives up after a 90 s total deadline, but the thread stuck on a trickling server lives until the server lets go.
- `parse_feed` falls back to the stdlib parser (with the `<!ENTITY` guard) when python3-defusedxml is missing, i.e. on
  `install.sh` installs that never installed it.
- AllDebrid's v4.1 `magnet/status` ignores its `status` filter (every status word returns the whole list, observed 2026-10), so
  AllDebrid has no light poll: its whole list every poll, harmless while AllDebrid libraries stay small. Torbox has no
  active-only query either: light polls fetch active items by id (at most `Torbox.LIGHT_MAX`, else the whole list) and probe
  the newest item, so an item deleted on torbox.app stays listed until the next full list (10 minutes when idle).
- `fetch_bytes` (feeds, `.torrent` URLs) ignores `http_proxy` / `https_proxy`: pinning an address and a proxy exclude each
  other. The service clients and the updater use requests and still honour them.
