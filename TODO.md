# To do

Built so far: everything from the UX review, three services verified live, and the whole power-user list
(hand-off to Stremio / Kodi / Jellyfin is covered by the custom play command).

Open notes:

- Next tag (v1.0.1) carries the `--version` fix; nothing else pending for it.

- Keyring storage for API keys was removed after it lost keys twice on this machine; if it comes back it needs a cross-restart verification step before the file copy is dropped.
- Another service (Premiumize etc.): recipe in the README's Dev section; only with an account to test against.
- AllDebrid hoster-link unlock: request path verified live; a successful unlock of a real hoster link hasn't been observed yet.

Known ceilings from the 2026-10-11 security/refactor audit (accepted for now, each needs more code than it is worth today):

- `fetch_bytes` resolves a host name twice (check, then connect): a DNS-rebinding host can get one blind GET. Pinning the vetted
  IP needs a custom requests adapter for TLS.
- `fetch_bytes` has a 20 s per-read timeout but no total deadline: a server that trickles bytes can hold the worker thread.
- Services are polled sequentially in one worker; one slow service delays every tab's refresh by its latency.
- `parse_feed` uses the stdlib parser with a `<!ENTITY` refusal instead of defusedxml (not packaged as a dependency).
- Real-Debrid `selectFiles` is re-sent on every poll for an item that keeps failing it (harmless but noisy).
- The aria2c/wget URLs are visible in `ps` to other local users for the duration of a download.
