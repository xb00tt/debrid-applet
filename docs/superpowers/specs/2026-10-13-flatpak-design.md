# Flatpak — Design

Date: 2026-10-13
Base: debrid-applet 1.1.2. Target version: 1.2.0 (code learns a "flatpak" install mode; deb and tarball unchanged in behaviour).

## Goal

Publish the applet on Flathub as `io.github.xb00tt.debrid_applet`, as light as the sandbox allows: freedesktop
runtime, one built Python module, no bundled players, narrow permissions. Every feature keeps working where the
sandbox allows it; where it cannot, the applet degrades visibly (browser instead of mpv, built-in wget instead of
host aria2c, a one-line instruction instead of autostart) and says so.

## Non-goals

- Bundling mpv, vlc or aria2 (100+ MB, kills the custom play command; rejected in the roadmap discussion).
- Renaming upstream files (rejected in the app-id spec; the self-updater's `FILES` and per-user installs depend on them).
- A GNOME-runtime variant, GTK4, or any change to the deb.

## Facts the design rests on (checked 2026-10-13)

- Flathub's linter flags `--talk-name=org.freedesktop.Flatpak` (host command access) as `finish-args-flatpak-spawn-access`,
  "granted with sufficient explanation" through a PR to the exceptions file in `flathub/flatpak-builder-lint`; it is
  called a temporary measure. `--filesystem=home` is `finish-args-home-filesystem-access`, also exception-only. GTK3
  wants `--socket=wayland --socket=fallback-x11 --share=ipc`; wildcard talk-names are never granted, exact ones are
  not flagged. Portals need no finish-arg. (docs.flathub.org linter and requirements pages.)
- The freedesktop runtime dropped PyGObject in 25.08; it ships GTK3, libnotify, GLib with `libgirepository-2.0`,
  Python 3.13, `xdg-open` (portal-aware), `flatpak-spawn` and `wget`; no `aria2c`, no players. Verified on this box's
  25.08 runtime. Flathub lists 25.08 and 26.08; Flathub requires the newest at submission time.
- `flathub/shared-modules` has `libayatana-appindicator/libayatana-appindicator-gtk3.json`, building libdbusmenu,
  ayatana-ido, libayatana-indicator and libayatana-appindicator 0.5.94 inline.
- `rename-icon` / `rename-desktop-file` rename files at the cleanup phase and update the metainfo launchable; no
  document says `rename-icon` rewrites the desktop file's `Icon=`. Flatpak's docs prefer renaming in the source. We do
  neither: the app module installs the desktop file, icon and metainfo under the app id itself (see "Module").
- The Background portal: `org.freedesktop.portal.Background.RequestBackground(parent_window, options)` with
  `reason`, `autostart`, `commandline`, `dbus-activatable`; the Response carries `autostart` and `background`. Cinnamon's
  `xdg-desktop-portal-xapp` has no Background backend, so on Mint the request fails and the fallback path is what runs.
- A StatusNotifier host resolves the item's `IconName` on the host side; a `IconThemePath` inside the sandbox is
  unreadable there. Flatpak exports `/app/share/icons/hicolor/*/apps/<app-id>.*` to the host, so inside the sandbox
  the indicator and the notifications must use the app id as the icon name.
- `flatpak run <id> --toggle` passes `--toggle` to the app. Submission: PR to `flathub/flathub` branch `new-pr`, manifest
  `<app-id>.yml` at the top level; test build with `org.flatpak.Builder` (installable without root); lint with
  `flatpak-builder-lint manifest` and `repo`.

## Decisions

| Topic | Decision | Why |
|---|---|---|
| Runtime | `org.freedesktop.Platform`, newest branch on Flathub when submitting (26.08 today) | 670 MB, yearly branches; GNOME runtime rejected earlier |
| PyGObject | built in the manifest from `download.gnome.org/sources/pygobject/3.58/`, meson, `-Dpycairo=disabled` | the runtime lacks `gi`; the code never touches cairo |
| Python deps | `requests`, `defusedxml` via `flatpak-pip-generator` into `python3-requirements.json` | vendored, no network at build |
| Tray | shared module `libayatana-appindicator-gtk3`, `--talk-name=org.kde.StatusNotifierWatcher` | XApp does not exist in the sandbox; the Ayatana path shipped in 1.1.0 |
| Host programs | `flatpak-spawn --host` behind one helper; `--talk-name=org.freedesktop.Flatpak` with an exception request | players and aria2c are the app's purpose; bundling rejected |
| Without host access | Play opens the stream in the browser (OpenURI portal); downloads use the runtime's `wget`; the custom play command reports it cannot run | the app stays useful while the exception is pending or on a locked-down install |
| Files | `--filesystem=xdg-download`, `xdg-config/debrid-applet:create`, `xdg-data/debrid-applet:create`; no `home` | default download folder, the existing config path shared with the deb, logs; `home` would need an exception |
| Watch folder | works inside Downloads; elsewhere the user adds `flatpak override --user --filesystem=<dir> io.github.xb00tt.debrid_applet` | one documented line instead of a blanket permission |
| Config | stays `~/.config/debrid-applet/config.json` through the granted path | a user moving between deb and Flatpak keeps keys and layout |
| Autostart | Background portal with `autostart` on first start; on refusal or no backend, one notification with the manual line | no `xdg-config/autostart` permission; Mint lacks the backend |
| Updates | self-updater off in flatpak mode; Flathub updates | the updater cannot write `/app` and must not try |
| Desktop file, icon, metainfo | installed by the app module under the app id with `sed`, `Exec=debrid-applet`, `Icon=<id>`, launchable `<id>.desktop` | deterministic; no reliance on `rename-*` semantics |
| Display | `--socket=wayland --socket=fallback-x11 --share=ipc` | linter's GTK3 set |
| Notifications | `--talk-name=org.freedesktop.Notifications` | libnotify talks to the daemon directly |

Download size: runtime shared; app about 7 MB (PyGObject ~3, Ayatana stack ~2, requests and friends ~1, the applet 0.3).

## Manifest

Kept in the repo at `packaging/io.github.xb00tt.debrid_applet.yml` for local builds and CI lint; the Flathub repo
gets a copy with the `shared-modules` submodule.

```yaml
app-id: io.github.xb00tt.debrid_applet
runtime: org.freedesktop.Platform
runtime-version: '26.08'
sdk: org.freedesktop.Sdk
command: debrid-applet
finish-args:
  - --share=network                       # the three services, the loopback API
  - --share=ipc
  - --socket=wayland
  - --socket=fallback-x11
  - --talk-name=org.kde.StatusNotifierWatcher   # tray icon
  - --talk-name=org.freedesktop.Notifications   # libnotify
  - --talk-name=org.freedesktop.Flatpak         # mpv/vlc/aria2c on the host; exception requested, degrades without
  - --filesystem=xdg-download                   # default download folder, watch folder inside it
  - --filesystem=xdg-config/debrid-applet:create   # config.json, shared with a deb install
  - --filesystem=xdg-data/debrid-applet:create     # activity and download logs
modules:
  - shared-modules/libayatana-appindicator/libayatana-appindicator-gtk3.json
  - name: pygobject
    buildsystem: meson
    config-opts: [-Dpycairo=disabled, -Dtests=false]
    sources:
      - type: archive
        url: https://download.gnome.org/sources/pygobject/3.58/pygobject-3.58.0.tar.xz
        sha256: <filled when written>
  - python3-requirements.json
  - name: debrid-applet
    buildsystem: simple
    build-commands:
      - install -Dm644 -t /app/share/debrid-applet debrid_api.py debrid_local_api.py debrid_update.py debrid_applet.py debrid-applet.svg
      - install -Dm644 debrid-applet.svg /app/share/icons/hicolor/scalable/apps/io.github.xb00tt.debrid_applet.svg
      - sed -e 's|@EXEC@|debrid-applet|' -e 's|^Icon=.*|Icon=io.github.xb00tt.debrid_applet|' debrid-applet.desktop > /app/share/applications/io.github.xb00tt.debrid_applet.desktop
      - sed 's|debrid-applet.desktop|io.github.xb00tt.debrid_applet.desktop|' io.github.xb00tt.debrid_applet.metainfo.xml > /app/share/metainfo/io.github.xb00tt.debrid_applet.metainfo.xml
      - printf '#!/bin/sh\nexec python3 /app/share/debrid-applet/debrid_applet.py "$@"\n' > /app/bin/debrid-applet && chmod 755 /app/bin/debrid-applet
    sources:
      - type: archive
        url: https://github.com/xb00tt/debrid-applet/releases/download/v1.2.0/debrid-applet-1.2.0.tar.gz
        sha256: <filled after the release workflow publishes the tarball>
```

`install -D` creates the target directories; `/app/share/applications` and `/app/share/metainfo` must be created
with `mkdir -p` before the two `sed` lines (plan detail). The shared module must produce the
`AyatanaAppIndicator3-0.1` typelib; if the JSON builds without introspection, the plan overrides the module with
the same sources plus `-DENABLE_INTROSPECTION=ON` style options for that stack (verified at first local build).

## Code changes (`debrid_applet.py`, `debrid_update.py`)

1. **Mode detection.** `debrid_update.install_mode(here)` returns `"flatpak"` when `/.flatpak-info` exists (checked
   first). `debrid_applet.IN_FLATPAK = install_mode(HERE) == "flatpak"` at import.
2. **Icon name.** `ICON_NAME = DBUS_ID if IN_FLATPAK else APP_ID`. Used by `notify()` (`Notify.Notification.new(...,
   ICON_NAME)`), `Gtk.Window.set_default_icon_name`, the About dialog's `logo_icon_name`, and the Ayatana indicator
   (`Indicator.new(APP_ID, ICON_NAME, …)`, `set_icon_full(ICON_NAME, "Debrid")`; `set_icon_theme_path(HERE)` only outside
   the sandbox, where the host can read it). `Tray` tries XApp only outside the sandbox (`XApp` is `None` there anyway).
3. **Host programs.** Two helpers next to `find_player()`:
   - `host_cmd(argv)` → `["flatpak-spawn", "--host", *argv]` when `IN_FLATPAK`, else `argv`.
   - `host_which(name)` → outside the sandbox `shutil.which(name)`; inside, `subprocess.run(host_cmd(["which", name]))`
     with a 5 s timeout, returning the path on exit 0, `None` on any failure (including a refused portal call, which is
     what "no exception yet" looks like). Result cached per name for the process lifetime.
   `find_player()` uses `host_which`; the auto-download tool is `"aria2c" if host_which("aria2c") else "wget"`, and the
   `wget` branch runs without `host_cmd` (the runtime ships it; it writes into the granted Downloads folder). Every
   `subprocess.Popen` for players and custom commands goes through `host_cmd`. `xdg-open` stays as is: the runtime's
   copy talks to the OpenURI portal.
4. **Degradation.** `play()` and `play_all()` with no player and no custom command: inside the sandbox, `open_url(url)`
   (browser via portal) with the status line "No host media player reachable, opened in the browser"; outside, the
   existing "No media player found" message. A custom command that fails with the portal error reports "Host commands
   are not permitted for this Flatpak (flatpak override --user --talk-name=org.freedesktop.Flatpak …)" once.
5. **Updater off.** In flatpak mode `check_updates()` returns after `self.say("Updates come from Flathub")` for manual
   checks and does nothing automatically; the Settings checkbox and the tray entry "Check for updates…" are hidden;
   About says "Installed from Flathub". `restart()` unchanged (unreachable).
6. **Autostart.** `request_autostart(on_result)` sends `RequestBackground("", {reason: "Keep the tray icon and the
   watch folder, RSS and auto-download running after login", autostart: true, commandline: ["debrid-applet"]})` and
   reports the Response's `autostart` flag. Called once at the first start in flatpak mode (config key
   `autostart_asked`, default `False`, set after the call). On `False` or any error: one notification "Autostart is
   not available through your desktop's portal. Add `flatpak run io.github.xb00tt.debrid_applet` to your startup
   applications." and a log line. The deb's `/etc/xdg/autostart` entry is untouched.
7. **Texts.** The tray-less hint names `flatpak run io.github.xb00tt.debrid_applet --toggle` in flatpak mode. Settings →
   Automation shows under the watch folder, in flatpak mode: "Inside Downloads works as is; another folder needs
   `flatpak override --user --filesystem=<folder> io.github.xb00tt.debrid_applet`."
8. **Drag and drop.** `.torrent` files dropped from outside the granted folders are unreadable in the sandbox; the
   existing "unreadable .torrent path" error already names the file. MANUAL explains; no code.

## Build, lint and test on this box

```
flatpak install -y flathub org.flatpak.Builder
flatpak run --command=flathub-build org.flatpak.Builder --install packaging/io.github.xb00tt.debrid_applet.yml
flatpak run --command=flatpak-builder-lint org.flatpak.Builder manifest packaging/io.github.xb00tt.debrid_applet.yml
flatpak run --command=flatpak-builder-lint org.flatpak.Builder repo repo
flatpak run io.github.xb00tt.debrid_applet
```

For a local build before the 1.2.0 tarball exists, the app module's source is `type: dir, path: ..` (plan detail;
the committed manifest carries the archive source).

Checks here (Cinnamon, X11, xapp-sn-watcher present): the indicator shows with the exported icon; `Tray: ayatana` in
the activity log; config read from the existing `~/.config/debrid-applet`; Play launches host mpv through
`flatpak-spawn`; with `flatpak override --user --nofilesystem=…`-style removal of the Flatpak talk-name, Play opens the
browser and auto-download uses wget; the autostart request fails on Mint and the fallback notification appears once;
`flatpak run … --toggle` from a second shell toggles the window; the local API answers on its port.

GNOME and KDE VMs (user's matrix): autostart portal dialog appears and `autostart` comes back `True`; indicator with
the extension / natively; `Icon` resolves in the dock.

## Flathub submission

1. Tag 1.2.0 (code changes above, manifest in `packaging/` with the real tarball sha256 filled in after the release
   workflow runs, so the manifest commit follows the release: "Release 1.2.0", then "Manifest: 1.2.0 sha256").
2. Fork `flathub/flathub`, branch from `new-pr`, add `io.github.xb00tt.debrid_applet.yml`, the `shared-modules`
   submodule and `python3-requirements.json`; open the PR "Add io.github.xb00tt.debrid_applet".
3. Open the exception PR in `flathub/flatpak-builder-lint` for `finish-args-flatpak-spawn-access`: "launches the user's
   own media player (mpv, vlc, celluloid, smplayer, totem, or a custom command such as Stremio or Kodi) and
   downloader (aria2c) on the host; the app degrades to the browser and built-in wget without it; bundling players
   was rejected to keep the app at 7 MB."
4. On `bot, build` and reviewer feedback, iterate in the PR. After publication, each release bumps the tarball URL and
   sha256 in the Flathub repo (a PR per release; Flathub's bot can open it from the GitHub release feed, set up later
   if the manual bump gets old).

## Release process additions

- `packaging/io.github.xb00tt.debrid_applet.yml`: tarball URL and sha256 per release, committed after the workflow
  publishes the tarball. `tests.yml` lints the manifest (`flatpak-builder-lint` via `org.flatpak.Builder` is too heavy
  for CI; a YAML parse and a check that the URL version equals the previous `VERSION` or the current one is enough).
- `python3-requirements.json`: regenerated when `requests` or `defusedxml` change.
- Metainfo `<release>` as before.

## Testing

Offline (`test_debrid_api.py`):

- `install_mode` returns `"flatpak"` for a `here` under `/app` when a fake `/.flatpak-info` check is injected (the
  function takes an optional `flatpak_info="/.flatpak-info"` argument for the test).
- `host_cmd(argv)` with `IN_FLATPAK` patched both ways; `host_which` outside the sandbox equals `shutil.which`.
- `download_command` unchanged; the tool choice function `download_tool(host_has_aria2c)` is pure and tested.
- The manifest YAML parses, its `app-id` equals `DBUS_ID`, its `finish-args` contain no wildcard talk-name and no
  `--filesystem=home` or `host`, and its app-module tarball URL version matches `VERSION`.

Manual: the matrix above, here and in the VMs.

## Risks and ceilings

- The exception may be refused or take weeks; the app ships usable without it (browser playback, wget downloads).
- `flatpak-spawn --host` runs the player outside the sandbox with the user's full environment; that is the point,
  and the justification text says so.
- Runtime branch churn is yearly; a bump is one line plus a rebuild.
- `xdg-desktop-portal-xapp` has no Background backend: Mint Flatpak users get the manual autostart line. The deb is the
  better choice on Mint anyway and the README says so.
- The shared Ayatana module's introspection support is unverified until the first build; the plan has the override.
- Drag-and-drop of files from outside Downloads fails inside the sandbox; FileChooserNative (portal) is the way in.

## Rejected

- `--filesystem=home`: exception-only on Flathub; the three narrow grants cover defaults and the override covers the rest.
- Bundled mpv/aria2; GNOME runtime; `rename-*` manifest keys (undocumented `Icon=` behaviour); honouring
  `XDG_CONFIG_HOME` for a separate sandbox config (would split the config between deb and Flatpak for no gain).
- A Background-portal toggle in Settings: one request at first start plus the manual line is enough; revisit if users ask.

## Size of the change

Manifest about 45 lines, pip JSON generated, ~80 lines of Python (mode, icon name, two helpers, degradation branches,
updater gating, autostart request, two texts), four or five offline tests, docs (README install section gains
Flathub; MANUAL gains a Flatpak section; DEVELOPING gains the build/lint commands and the release addition).
