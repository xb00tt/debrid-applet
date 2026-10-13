# To do

Built so far: everything from the UX review, three services verified live, and the whole power-user list
(hand-off to Stremio / Kodi / Jellyfin is covered by the custom play command).

Open notes:

- Keyring storage for API keys was removed after it lost keys twice on this machine; if it comes back it needs a cross-restart verification step before the file copy is dropped.
- Another service (Premiumize etc.): recipe in the README's Dev section; only with an account to test against.
- AllDebrid hoster-link unlock: request path verified live; a successful unlock of a real hoster link hasn't been observed yet.
- One `Invalid API key` answer opens Settings (`ServiceTab.say` → `App.open_settings_once`). Observed 2026-10-13: Torbox
  returned it once in the middle of an outage (timeouts and "error processing your request" for twenty minutes before),
  with a valid key, so the dialog popped up for nothing. Fix: open Settings only after two consecutive auth errors
  from the same service, or after a confirming `account()` call fails the same way; a lone one stays a status-line
  error. Torbox's `AUTH_ERROR` code (`debrid_api.TORBOX_AUTH_ERRORS`) is the suspect.

Roadmap (decided 2026-10-13, from the "full-fledged Linux app" discussion; goal is reach for other users, kept light):

1. **Universal deb** (v1.1.0): spec in `docs/superpowers/specs/2026-10-13-universal-deb-design.md`. Tray adapter (XApp
   where a host answers, Ayatana elsewhere, tray-less mode as last resort), Quit in the window and `Ctrl+Q`, Wayland hint
   on the clipboard watcher, both tray libraries as Recommends. Next: implementation plan.
2. **App id and AppStream metainfo** (v1.1.1): spec in `docs/superpowers/specs/2026-10-13-app-id-metainfo-design.md`.
   `io.github.xb00tt.debrid-applet` (already the D-Bus id) becomes the AppStream id and metainfo file name; nothing
   upstream is renamed (the self-updater's `FILES` and per-user autostart entries would break); the Flatpak renames
   the desktop file and icon at build time with `rename-desktop-file` / `rename-icon`. CI validates the metainfo and
   refuses a tag without a `<release>` entry.
3. **Flatpak on Flathub** (v1.2.0): spec in `docs/superpowers/specs/2026-10-13-flatpak-design.md`. Freedesktop runtime (670 MB, yearly branches; ships GTK3, libnotify, typelibs and Python but
   not `gi`), PyGObject built in the manifest with pycairo disabled (the code never touches cairo), requests and
   defusedxml vendored. Host commands via `flatpak-spawn --host` for players, aria2c, wget and xdg-open (one helper,
   four call sites, plus a host `which` for `find_player()` and the aria2c check); `--filesystem=home`; autostart via the
   Background portal; self-updater off (`install_mode()` learns "flatpak" from `/.flatpak-info`). Tray is Ayatana only
   inside the sandbox, so Cinnamon loses the left-click toggle in the Flatpak build; the deb keeps it. Rejected: GNOME
   runtime (1.1 GB, six-monthly branches, this box carries three of them) and bundling mpv/aria2 (100+ MB, kills the
   custom play command). Manifest keys: `rename-desktop-file: debrid-applet.desktop`, `rename-icon: debrid-applet`;
   the metainfo is already named by id and within Flathub's quality guidelines (summary length, screenshot size are
   asserted by `test_metainfo`).
4. **Tarball for other distros**: already works via `install.sh`; add one dependency line per distro (Fedora, Arch,
   openSUSE) to the README. RPM spec / AUR PKGBUILD only if someone asks; Flatpak covers those distros.
5. **Daemon + frontend split**: deferred, no user has asked and Flatpak cannot ship a systemd user unit anyway. If it
   ever comes: first pull a `Service` model (client, account, torrents, pending, done set, retries, finished-item
   rules) out of `ServiceTab`, which is worth doing alone for testability; then a GLib-only daemon reusing the loopback
   HTTP API with per-item revisions (12k-item lists must not cross IPC whole every 8 s).

Not planned: GTK4 / libadwaita (rewrite, heavier, foreign on Cinnamon), AppImage and Snap (bundling PyGObject and GTK by
hand for no extra audience).

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
