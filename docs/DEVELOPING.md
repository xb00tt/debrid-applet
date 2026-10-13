# Developing

## Tests

```bash
python3 test_debrid_api.py
```

Offline tests with canned API responses, no GTK needed (install `python3-defusedxml` too to run the feed parser the package
uses); the same suite runs on GitHub Actions for every push
(`.github/workflows/tests.yml`). The GTK side has no unit tests: it is checked by rendering a `ServiceTab` with sample
rows into a `Gtk.OffscreenWindow` and by driving the running applet with `xdotool` plus screenshots. `xdotool type`
mangles `[` and `|` on some keyboard layouts, so use the clipboard or bracket-free paths when scripting dialogs.

## Repository layout

- `debrid_api.py`: the three service clients behind one interface, plus helpers (`magnet_hash`, `human_size`).
- `debrid_local_api.py`: the loopback HTTP API.
- `debrid_update.py`: update check and self-update (GitHub releases API, verified download, apt / per-user install).
- `debrid_applet.py`: everything GTK: tabs, footer, settings form, watchers, notifications, command line, and the
  `Tray` adapter (XApp or Ayatana, both optional imports; `tray_choice()` is the pure decision, `DEBRID_APPLET_TRAY`
  forces a backend for testing: Cinnamon's xapp-sn-watcher shows the Ayatana path on one box).
- `test_debrid_api.py`: the offline suite; run it directly, no test runner needed.
- `install.sh`, `debrid-applet.desktop`, `debrid-applet.svg`: per-user install.
- `io.github.xb00tt.DebridApplet.metainfo.xml`: AppStream metainfo for app stores and Flathub; installed to
  `/usr/share/metainfo` by the package and `~/.local/share/metainfo` by `install.sh`. The id is `DBUS_ID`; the desktop
  file and icon keep the `debrid-applet` name (a Flatpak renames them at build time).
- `packaging/build-deb.sh`: builds the Debian package the release workflow publishes.
- `.github/workflows/`: `tests.yml` on every push, `release.yml` on `v*` tags.
- `docs/MANUAL.md`: the user manual; `docs/superpowers/`: the original design spec and plan.

## Where things live at runtime

- Config: `~/.config/debrid-applet/config.json` (mode 600; versioned and migrated on load). `config.json.bak` is the
  last version that still held an API key the current file lacks.
- Activity log and download logs: `~/.local/share/debrid-applet/`.
- `DEBRID_APPLET_CONFIG_DIR` and `DEBRID_APPLET_DATA_DIR` redirect both. The test suite sets them before importing
  `debrid_applet`; **any script or harness that imports the module must do the same**: a `ServiceTab` built on a
  stand-in app saves the real config as soon as its state filter changes (this once wiped a live set of keys).
- Per-user install: `~/.local/share/debrid-applet/`; package install: `/usr/share/debrid-applet/`, where the package's
  `postinst` compiles the bytecode into `__pycache__` (the launcher imports the module rather than running the file, so
  starts skip the compile) and `prerm` removes it.

## Releases

To release: bump `VERSION` in `debrid_applet.py` and the deb file name in the README, add a `<release>` entry (newest
first, the tag's date, one line) to `io.github.xb00tt.DebridApplet.metainfo.xml` and point its screenshot URL at the
new tag, then tag `vX.Y.Z` and push the tag. `python3 test_debrid_api.py` checks the file against `VERSION`;
`appstreamcli validate --no-net --pedantic io.github.xb00tt.DebridApplet.metainfo.xml` is the local lint. The release
workflow runs the tests, refuses a tag that doesn't match `VERSION` or has no release entry, builds the `.deb` (via
`packaging/build-deb.sh`) and a source tarball, and publishes a GitHub release with generated notes.
`packaging/build-deb.sh` also works locally on any Debian-based box.

Releases are signed: the workflow signs the `.deb` and the tarball with OpenSSH signatures (`ssh-keygen -Y sign`,
namespace `debrid-applet-X.Y.Z`, so a signed asset of one release cannot be re-published as another) using the
`RELEASE_SIGNING_KEY` repository secret (the private half of
`~/.ssh/debrid-release-key` on the release machine; keep a backup, nothing else can sign a release the applet accepts),
publishes the `.sig` files next to the assets and checks them against `debrid_update.SIGNING_KEYS` before publishing.
The applet refuses an update without a valid signature. To rotate the key: add the new public key to `SIGNING_KEYS`,
release that with the old key, then replace the secret and drop the old key in a later release.

The applet's update check reads `releases/latest`: keep the tag `vX.Y.Z` and the asset names
`debrid-applet_X.Y.Z_all.deb` and `debrid-applet-X.Y.Z.tar.gz` plus their `.sig` files (all produced by the workflow);
GitHub computes the `digest` the applet verifies. Drafts and prereleases are invisible to it. A per-user install updates from the tarball:
it takes every `.py` and `.svg` directly under the archive root (tests excluded), so a new module travels with the
release; `debrid_update.FILES` is the required minimum and must match the copy list in `install.sh` (a test checks).

## Adding a service

One client class in `debrid_api.py` with `name` and `short` (the All tab's two-letter label) implementing `account`,
`torrents`, `add_magnet`, `add_torrent_file`, `add_link`, `links`, `delete` (optionally `check_cached` and `control`), then in `debrid_applet.py`: an entry in
`CLIENTS` (class and config key; tabs, `SERVICES` and `KEY_FIELDS` derive from it), the key's default in `EMPTY_CONFIG`,
a key field in the Settings form, and a default in `DEFAULT_HIDDEN` for columns it never reports. Verify it against the live API
before shipping; every service so far needed fixes that only real keys exposed. See `../TODO.md` for what is not planned.
