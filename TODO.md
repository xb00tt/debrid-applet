# To do

Built so far: everything from the UX review, three services verified live, and the whole power-user list
(hand-off to Stremio / Kodi / Jellyfin is covered by the custom play command).

Open notes:

- Keyring storage for API keys was removed after it lost keys twice on this machine; if it comes back it needs a cross-restart verification step before the file copy is dropped.
- Another service (Premiumize etc.): recipe in the README's Dev section; only with an account to test against.
- AllDebrid hoster-link unlock: observed 2026-10-13 through the applet (`--service AllDebrid --add …`): a 1fichier and a
  mega link both unlocked, "Added 2, direct link(s) copied". Closed.
- Torbox auth codes, closed 2026-10-13 against api-docs.torbox.app (errors table): `NO_AUTH` "no provided credentials"
  and `BAD_TOKEN` "token invalid" are the key verdicts (`TORBOX_AUTH_ERRORS`); a code ending in `ERROR` is the server's
  fault by Torbox's own rule, so `AUTH_ERROR` ("error verifying the given authentication", the outage case) shows as
  Torbox's message and never opens Settings. Probed live: no key is `HTTP 401`, a bad key `HTTP 403 BAD_TOKEN`. Since
  v1.2.1 Settings opens only after a second rejection and the log names status and code.

Roadmap (decided 2026-10-13, from the "full-fledged Linux app" discussion; goal is reach for other users, kept light).
Items 1-4 shipped by v1.2.1; 3 is built but unpublished by decision; 5 is deferred:

1. **Universal deb** (v1.1.0): spec in `docs/superpowers/specs/2026-10-13-universal-deb-design.md`. Tray adapter (XApp
   where a host answers, Ayatana elsewhere, tray-less mode as last resort), Quit in the window and `Ctrl+Q`, Wayland hint
   on the clipboard watcher, both tray libraries as Recommends. Shipped.
2. **App id and AppStream metainfo** (v1.1.1, id corrected in v1.1.2 and again in v1.2.0): spec in
   `docs/superpowers/specs/2026-10-13-app-id-metainfo-design.md`. The AppStream id and metainfo file name follow the
   D-Bus id, now `io.github.xb00tt.debrid-applet`; nothing upstream is renamed (the self-updater's `FILES` and per-user
   autostart entries would break); the Flatpak installs renamed copies of the desktop file, icon and metainfo from its
   build commands. CI validates the metainfo; the test suite ties the newest release entry to `VERSION`.
3. **Flatpak** (v1.2.0): built and verified locally, spec in `docs/superpowers/specs/2026-10-13-flatpak-design.md`.
   **Flathub publication is parked (decided 2026-10-13); not any time soon.** The manifest in `packaging/` stays
   maintained with each release (the test moves its `tag:` with `VERSION`). When publication is back on: the Flathub
   PR (`flathub/flathub`, branch `new-pr`, manifest with `commit:` next to `tag:`, `python3-requirements.json`, the
   `shared-modules` submodule) and the `finish-args-flatpak-spawn-access` exception PR in
   `flathub/flatpak-builder-lint` (text in the spec). Until that exception, the Flatpak plays in the browser and
   downloads with the built-in wget.
4. **Tarball for other distros**: works via `install.sh`; the README has a dependency line each for Fedora, Arch and
   openSUSE (package names from the distros' indexes, not tried on them: no VMs here). RPM spec / AUR PKGBUILD only if
   someone asks.
5. **Daemon + frontend split**: deferred, no user has asked and Flatpak cannot ship a systemd user unit anyway. If it
   ever comes: first pull a `Service` model (client, account, torrents, pending, done set, retries, finished-item
   rules) out of `ServiceTab`, which is worth doing alone for testability; then a GLib-only daemon reusing the loopback
   HTTP API with per-item revisions (12k-item lists must not cross IPC whole every 8 s).

Verification on other desktops (GNOME, KDE, XFCE, Wayland) and other distros (Fedora, Arch): **no VMs on this machine
(decided 2026-10-13)**; everything beyond Cinnamon on X11 is covered by library-source reading, unit tests and user
reports. Do not propose a VM matrix again.

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
