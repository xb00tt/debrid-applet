# App id and AppStream metainfo — Design

Date: 2026-10-13
Base: debrid-applet 1.1.0. Target version: 1.1.1 (no behaviour change; a new file, packaging, CI and a test).

## Goal

Give the applet the public identity app stores and Flathub need: a reverse-DNS app id and an AppStream metainfo file,
shipped by the deb and the per-user install, validated in CI, kept in step with each release. Everything else keeps
its name.

## Decision: nothing upstream is renamed

**Amendment 2026-10-13 (after the branch review):** the id is `io.github.xb00tt.debrid_applet`, not
`io.github.xb00tt.DebridApplet`. Flathub derives the repository URL from the last component of a code-hosting id,
converting underscores to dashes (its example: `io.github.example_foo.bar` → `github.com/example-foo/bar`), and the
URL must resolve; `DebridApplet` would have needed a manual exception. The D-Bus name, the metainfo id and file name
and the future Flatpak id all follow. Changed in 1.1.2 before any store listing existed, so no `<provides><id>`
compatibility entry is needed. The release workflow's `grep` for a `<release>` entry (§3) was dropped in 1.1.2: the
test suite runs first in that workflow and `test_metainfo` already requires the newest release to equal `VERSION`.
The summary was shortened to 34 characters and the screenshot scaled to 1000 pixels wide for Flathub's quality
guidelines, both now asserted by the test.

Two ways to get there were weighed.

**A. Keep `debrid-applet` as the file and WM_CLASS name; publish the reverse-DNS id in the metainfo only.** The Flatpak
step renames the desktop file and icon at build time with the manifest keys `rename-desktop-file`, `rename-icon` and
`rename-appdata-file`; Flathub documents this as the normal route and its guidelines say `rename-desktop-file` also
rewrites the metainfo `<launchable>` (docs.flathub.org, metainfo guidelines, "Launchable"; docs.flatpak.org,
manifests, "File renaming"). Chosen.

**B. Rename the desktop file, icon and prgname to `io.github.xb00tt.debrid_applet` in the repo.** Rejected:

- `debrid_update.FILES` names `debrid-applet.svg`; the self-updater in every 1.1.0 source install would refuse the
  next tarball with "Release tarball lacks debrid-applet.svg". A rename needs a two-release migration for no gain.
- Per-user installs keep `~/.config/autostart/debrid-applet.desktop` and `~/.local/share/applications/debrid-applet.desktop`
  next to the new ones: two menu entries, double autostart attempts.
- The window's WM_CLASS, the XApp bus name and the indicator path all change for nothing a user sees.

Consequences of A:

- `APP_ID = "debrid-applet"` stays the prgname, WM_CLASS, icon name and desktop basename.
- `DBUS_ID = "io.github.xb00tt.debrid_applet"` becomes the one public id: `Gtk.Application` (already), the metainfo
  `<id>` and file name, and later the Flatpak manifest `id`. Its comment in `debrid_applet.py:54` says so.
- AppStream allows a launchable that differs from the id; `debrid-applet.desktop` is what the deb installs, so the
  metainfo names it. On GNOME and Cinnamon the deb's window matches its desktop file by WM_CLASS as today.

## Files

### 1. `io.github.xb00tt.debrid_applet.metainfo.xml` (repo root, new)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>io.github.xb00tt.debrid_applet</id>
  <metadata_license>CC0-1.0</metadata_license>
  <project_license>MIT</project_license>
  <name>Debrid Applet</name>
  <summary>Torbox, Real-Debrid and AllDebrid in one tray window</summary>
  <description>
    <p>Shows account status and every torrent with live progress, adds magnets, .torrent files and hoster links,
       streams or downloads the results, and automates the rest: watch folder, RSS feeds, auto-download, local API.</p>
    <p>Tray icon on Cinnamon, KDE, XFCE, MATE and GNOME (with the AppIndicator extension); works as a plain window
       elsewhere.</p>
  </description>
  <launchable type="desktop-id">debrid-applet.desktop</launchable>
  <developer id="io.github.xb00tt"><name>xb00tt</name></developer>
  <url type="homepage">https://github.com/xb00tt/debrid-applet</url>
  <url type="bugtracker">https://github.com/xb00tt/debrid-applet/issues</url>
  <url type="vcs-browser">https://github.com/xb00tt/debrid-applet</url>
  <provides><binary>debrid-applet</binary></provides>
  <content_rating type="oars-1.1"/>
  <screenshots>
    <screenshot type="default">
      <caption>The All tab with items from every service</caption>
      <image>https://raw.githubusercontent.com/xb00tt/debrid-applet/v1.1.1/docs/screenshot.png</image>
    </screenshot>
  </screenshots>
  <releases>
    <release version="1.1.1" date="2026-10-13"><description><p>AppStream metainfo for app stores.</p></description></release>
    <release version="1.1.0" date="2026-10-13"><description><p>Runs on any Debian-based desktop: tray icon on Cinnamon, KDE, XFCE, MATE and GNOME, plain window elsewhere.</p></description></release>
    <release version="1.0.0" date="2026-10-11"><description><p>First release.</p></description></release>
  </releases>
</component>
```

Rules the file follows (Flathub metainfo guidelines and linter, checked 2026-10-13): id equals the future Flatpak id and
the file name; `metadata_license` and `project_license` SPDX; exactly one `launchable`; `developer` with an id (the
old `developer_name` is deprecated since AppStream 1.0); at least one screenshot over https; `releases` with no
future dates and the newest first; `content_rating` present; categories and keywords omitted because the desktop file
carries them. The `1.1.1` release date is the tag day; the implementer sets it when tagging.

The screenshot URL pins the tag of the release it describes, so the picture never drifts from the version a store
shows. That makes the file part of every release bump (see "Release process").

### 2. Packaging

- `packaging/build-deb.sh`: copy the file to `$STAGE/usr/share/metainfo/`. No new dependency.
- `install.sh`: copy it to `~/.local/share/metainfo/`. The per-user self-updater copies only `.py` and `.svg`
  members, so updated source installs keep the metainfo they were installed with; it is store data only, nothing the
  applet reads.
- The release tarball is `git archive`, so it carries the file automatically.

### 3. Validation in CI

- `.github/workflows/tests.yml`: add `appstream desktop-file-utils` to the apt line and one step:

  ```
  appstreamcli validate --no-net io.github.xb00tt.debrid_applet.metainfo.xml && desktop-file-validate debrid-applet.desktop
  ```

  Flathub treats validator warnings as fatal, so the step fails on any output. `--pedantic` is for the developer's
  terminal, not CI (style nits change between AppStream versions).
- `.github/workflows/release.yml`: next to the VERSION check, refuse a tag whose version has no `<release version="…"`
  entry in the metainfo:

  ```
  grep -q "<release version=\"$V\"" io.github.xb00tt.debrid_applet.metainfo.xml || { echo "metainfo lacks release $V"; exit 1; }
  ```

### 4. Offline test (`test_debrid_api.py`, new `test_metainfo`)

Parses the file with `xml.etree` (no network, no GTK) and asserts:

- the file is named `<id>.metainfo.xml` and `<id>` equals `debrid_applet.DBUS_ID`;
- `<launchable>` names `debrid-applet.desktop`, which exists in the repo, and that desktop file's `Icon=` equals
  `debrid_applet.APP_ID` with `APP_ID + ".svg"` present;
- the first `<release>` is `debrid_applet.VERSION`, no release date is after today, versions are listed newest first;
- the screenshot `<image>` is https and contains `/v{VERSION}/`.

The existing deb-layout test gains one assertion: `usr/share/metainfo/io.github.xb00tt.debrid_applet.metainfo.xml` is
in the built package.

### 5. Code

One comment change at `debrid_applet.py:54`: `DBUS_ID` is "the app id: D-Bus name, AppStream id, Flatpak id". No logic
changes.

### 6. Documentation

- `docs/DEVELOPING.md`: the metainfo file in "Repository layout"; in "Releases" add: bump `VERSION`, add a
  `<release>` entry with the tag date and one line, and point the screenshot URL at the new tag; the workflow refuses
  a tag otherwise. Mention `appstreamcli validate --no-net --pedantic` for local checks.
- `TODO.md`: roadmap item 2 no longer says "rename"; item 3 (Flatpak) gains the three manifest keys.

## Release process from now on

`VERSION`, the README's deb file name, the metainfo `<release>` list and the screenshot tag move together in the
"Release X.Y.Z" commit. CI enforces the metainfo half.

## Notes for the Flatpak step (next spec)

- Manifest `id: io.github.xb00tt.debrid_applet`, `rename-desktop-file: debrid-applet.desktop`,
  `rename-icon: debrid-applet`, `rename-appdata-file` not needed (the metainfo is already named by id).
- The Flathub linter wants the installed desktop file's `Icon=` to equal the app id; verify that `rename-icon`
  rewrites the key (the Haiku lookup found no statement either way), else the manifest edits it with `sed`.
- GNOME Shell matches a sandboxed app's windows by app id, so the renamed desktop file is what makes the dock
  association work there; `StartupWMClass` is not relied on inside Flatpak.

## Rejected

- `<icon type="stock">` in the metainfo: redundant with the desktop file's `Icon=`.
- Generating `<releases>` from git tags at build time: validators and stores read the file from the tree; a generated
  file would have to be committed anyway.
- Per-user `metainfo` cleanup in the self-updater: store data, never read by the applet.

## Testing

- `python3 test_debrid_api.py` → `test_metainfo` and the deb-layout assertion pass.
- `appstreamcli validate --no-net --pedantic io.github.xb00tt.debrid_applet.metainfo.xml` → exit 0, no output
  (appstreamcli 1.0.2 is installed here).
- `desktop-file-validate debrid-applet.desktop` → no output.
- Build the deb, `dpkg-deb -c` lists the metainfo under `usr/share/metainfo/`.
- After installing: `appstreamcli get io.github.xb00tt.debrid_applet` prints the component from the system pool.

## Size of the change

One XML file of about 40 lines, four one-line packaging and workflow edits, one offline test of about 25 lines, one
assertion in the deb test, one comment, two doc paragraphs. No runtime change.
