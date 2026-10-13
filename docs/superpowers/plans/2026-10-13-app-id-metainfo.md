# App id and AppStream metainfo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship an AppStream metainfo file under the reverse-DNS id `io.github.xb00tt.DebridApplet`, installed by the deb and `install.sh`, validated in CI, and tied to every release, without renaming anything upstream.

**Architecture:** One static XML file at the repo root, copied into `/usr/share/metainfo` by `packaging/build-deb.sh` and into `~/.local/share/metainfo` by `install.sh`. An offline test keeps the file consistent with `VERSION`, the desktop file and the icon. CI runs `appstreamcli validate` and `desktop-file-validate`; the release workflow refuses a tag whose version has no `<release>` entry. `APP_ID` stays `debrid-applet`; `DBUS_ID` is the public id.

**Tech Stack:** AppStream 1.0 metainfo, appstreamcli 1.0.2, desktop-file-utils, dpkg-deb, GitHub Actions, Python `xml.etree` for the test. Tests: `python3 test_debrid_api.py`.

**Spec:** `docs/superpowers/specs/2026-10-13-app-id-metainfo-design.md`

**Execution:** Native (chosen by the user). Final whole-branch review on `opus` (small, mostly static change; the user asked for lower models where possible).

## Global Constraints

- No file, constant or WM_CLASS is renamed: `APP_ID = "debrid-applet"`, `debrid-applet.desktop`, `debrid-applet.svg` stay. The spec rejects the upstream rename.
- Public id, exact: `io.github.xb00tt.DebridApplet`, equal to `debrid_applet.DBUS_ID`; metainfo file name, exact: `io.github.xb00tt.DebridApplet.metainfo.xml` at the repo root.
- Metainfo `<launchable type="desktop-id">` is `debrid-applet.desktop`.
- `<releases>` newest first, no future dates, first entry equals `VERSION`; screenshot `<image>` is `https://raw.githubusercontent.com/xb00tt/debrid-applet/v<VERSION>/docs/screenshot.png`.
- `VERSION` becomes `1.1.1` only in the last task; until then the metainfo's first release is `1.1.0` so the test stays green at every commit.
- CI validation is `appstreamcli validate --no-net` (no `--pedantic` in CI).
- The offline suite sets `DEBRID_APPLET_CONFIG_DIR` / `DEBRID_APPLET_DATA_DIR` before importing `debrid_applet`; any new script that imports the module must do the same. Never run `install.sh` against the real `$HOME` on this box (it would add a per-user copy next to the deb install).
- Line numbers in "Files:" refer to the 1.1.0 tree plus the spec commit; locate by the quoted text.
- Commit messages: plain imperative subject as in the history; body optional.

## Review Focus

1. A `<release>` dated in the future, or releases out of order: Flathub rejects both. Pinned in Task 1's test (date ≤ today, versions strictly descending).
2. Screenshot URL pointing at a tag that does not exist yet: `--no-net` validation never fetches, so only the test's `/v<VERSION>/` check guards it; the tag is pushed with the same commit. Pinned in Task 1's test.
3. Desktop file `Icon=` drifting from the icon file name: a store shows a blank icon. Pinned in Task 1's test (`Icon=` equals `APP_ID`, `APP_ID + ".svg"` exists).
4. appstreamcli on the CI runner newer or older than 1.0.2 here, with different warnings (`developer` vs `developer_name`): CI fails on a rule this box never showed. Not testable offline; Task 3 runs the exact CI command locally and the first push shows the rest. Ruling if it differs.
5. Release workflow grep matching a substring of another version (`1.1.1` inside `1.1.10`): the grep includes the closing quote, `<release version="1.1.1"`. Pinned in Task 3, step 3 (negative check with `1.1.10`).

---

### Task 1: The metainfo file and its test

**Files:**
- Create: `io.github.xb00tt.DebridApplet.metainfo.xml`
- Test: `test_debrid_api.py` (append `test_metainfo` before `if __name__ == "__main__":`)

**Interfaces:**
- Consumes: `debrid_applet.VERSION`, `debrid_applet.APP_ID`, `debrid_applet.DBUS_ID`, repo files `debrid-applet.desktop`, `debrid-applet.svg`.
- Produces: the metainfo file; `METAINFO = "io.github.xb00tt.DebridApplet.metainfo.xml"` module constant in the test file, reused by Task 2's deb assertion.

- [ ] **Step 1: Write the failing test**

Append to `test_debrid_api.py` before the `if __name__ == "__main__":` block:

```python
METAINFO = "io.github.xb00tt.DebridApplet.metainfo.xml"


def test_metainfo():
    """The AppStream file stays consistent with VERSION, the desktop file and the icon (see the 2026-10-13 app-id spec)."""
    import datetime
    import re
    import xml.etree.ElementTree as ET
    import debrid_applet as da
    root = ET.parse(METAINFO).getroot()
    assert root.tag == "component" and root.get("type") == "desktop-application"
    assert root.findtext("id") == da.DBUS_ID and METAINFO == da.DBUS_ID + ".metainfo.xml"
    launchable = root.find("launchable")
    assert launchable.get("type") == "desktop-id" and launchable.text == "debrid-applet.desktop"
    desktop = open("debrid-applet.desktop").read()
    assert f"\nIcon={da.APP_ID}\n" in desktop and os.path.exists(da.APP_ID + ".svg")
    assert root.findtext("metadata_license") and root.findtext("project_license") == "MIT"
    assert root.find("developer").get("id") and root.find("developer").findtext("name")
    assert root.find("content_rating").get("type") == "oars-1.1"
    releases = root.findall("releases/release")
    assert releases and releases[0].get("version") == da.VERSION
    today = datetime.date.today().isoformat()
    versions = [tuple(int(p) for p in r.get("version").split(".")) for r in releases]
    assert all(r.get("date") <= today for r in releases), "a release is dated in the future"
    assert versions == sorted(versions, reverse=True) and len(set(versions)) == len(versions), "releases must be newest first"
    image = root.findtext("screenshots/screenshot/image")
    assert image.startswith("https://") and f"/v{da.VERSION}/" in image, image
    assert not re.search(r"<developer_name>", open(METAINFO).read())  # deprecated since AppStream 1.0


```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 test_debrid_api.py`
Expected: `FileNotFoundError: [Errno 2] No such file or directory: 'io.github.xb00tt.DebridApplet.metainfo.xml'` from `test_metainfo` (earlier tests print `ok`).

- [ ] **Step 3: Write the metainfo file**

Create `io.github.xb00tt.DebridApplet.metainfo.xml` with exactly this content (first release is the current `VERSION`, 1.1.0; Task 5 adds 1.1.1):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>io.github.xb00tt.DebridApplet</id>
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
      <image>https://raw.githubusercontent.com/xb00tt/debrid-applet/v1.1.0/docs/screenshot.png</image>
    </screenshot>
  </screenshots>
  <releases>
    <release version="1.1.0" date="2026-10-13"><description><p>Runs on any Debian-based desktop: tray icon on Cinnamon, KDE, XFCE, MATE and GNOME, plain window elsewhere.</p></description></release>
    <release version="1.0.0" date="2026-10-11"><description><p>First release.</p></description></release>
  </releases>
</component>
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 test_debrid_api.py`
Expected: every line `ok …`, last line `87 passed`.

- [ ] **Step 5: Validate with the real tools**

Run: `appstreamcli validate --no-net --pedantic io.github.xb00tt.DebridApplet.metainfo.xml; echo "exit=$?"`
Expected: `exit=0`. If the tool prints a `W` or `P` line, fix the file until it prints nothing; a `developer_name` suggestion means the installed appstreamcli predates 1.0, which this box does not (1.0.2).

Run: `desktop-file-validate debrid-applet.desktop; echo "exit=$?"`
Expected: `exit=0`, no output.

- [ ] **Step 6: Commit**

```bash
git add io.github.xb00tt.DebridApplet.metainfo.xml test_debrid_api.py
git commit -m "AppStream metainfo under io.github.xb00tt.DebridApplet, with an offline consistency test"
```

---

### Task 2: Packaging

**Files:**
- Modify: `packaging/build-deb.sh:10-13` (mkdir and cp lines), `install.sh:8-12`
- Test: `test_debrid_api.py` (the deb-layout test, line 1507 area)

**Interfaces:**
- Consumes: `METAINFO` from Task 1's test section.
- Produces: `usr/share/metainfo/io.github.xb00tt.DebridApplet.metainfo.xml` inside the deb; `~/.local/share/metainfo/` copy from `install.sh`.

- [ ] **Step 1: Write the failing assertion**

In the deb-layout test, directly after the line

```python
        assert sorted(os.listdir(os.path.join(tmp, "usr/share/debrid-applet"))) == sorted(du.FILES)
```

add:

```python
        assert os.path.exists(os.path.join(tmp, "usr/share/metainfo", METAINFO))  # app stores read it from here
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 test_debrid_api.py`
Expected: `AssertionError` on that line (the deb test builds the package with `packaging/build-deb.sh`, which does not copy the file yet).

- [ ] **Step 3: Install the file from the deb**

In `packaging/build-deb.sh`, extend the `mkdir -p` line with `"$STAGE/usr/share/metainfo"`:

```sh
mkdir -p "$APP" "$STAGE/usr/bin" "$STAGE/usr/share/applications" "$STAGE/etc/xdg/autostart" \
         "$STAGE/usr/share/icons/hicolor/scalable/apps" "$STAGE/usr/share/doc/$PKG" "$STAGE/usr/share/metainfo" "$STAGE/DEBIAN"
```

and after the `cp "$SRC/debrid-applet.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/"` line add:

```sh
cp "$SRC/io.github.xb00tt.DebridApplet.metainfo.xml" "$STAGE/usr/share/metainfo/"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 test_debrid_api.py`
Expected: `87 passed`. Then `dpkg-deb -c dist/debrid-applet_1.1.0_all.deb | grep metainfo` prints one line ending in `usr/share/metainfo/io.github.xb00tt.DebridApplet.metainfo.xml`.

- [ ] **Step 5: Per-user install**

In `install.sh`, change the `mkdir -p "$ICONS"` line and add one copy:

```sh
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
META="$HOME/.local/share/metainfo"
mkdir -p "$ICONS" "$META"
cp "$SRC/debrid_api.py" "$SRC/debrid_local_api.py" "$SRC/debrid_update.py" "$SRC/debrid_applet.py" "$SRC/debrid-applet.svg" "$DIR/"
cp "$SRC/debrid-applet.svg" "$ICONS/debrid-applet.svg"
cp "$SRC/io.github.xb00tt.DebridApplet.metainfo.xml" "$META/"
```

Check against a throwaway home, never the real one:

Run: `H=$(mktemp -d) && HOME="$H" sh install.sh >/dev/null && ls "$H/.local/share/metainfo" && rm -rf "$H"`
Expected: `io.github.xb00tt.DebridApplet.metainfo.xml`.

- [ ] **Step 6: Commit**

```bash
git add packaging/build-deb.sh install.sh test_debrid_api.py
git commit -m "Package and install.sh ship the metainfo"
```

---

### Task 3: CI validation and the release gate

**Files:**
- Modify: `.github/workflows/tests.yml:14-19`, `.github/workflows/release.yml:20-23`

**Interfaces:**
- Consumes: the metainfo file; `VERSION` regex already used in `release.yml`.
- Produces: a failing `tests` workflow on invalid metainfo or desktop file; a refused release tag without a `<release>` entry.

- [ ] **Step 1: tests.yml**

Replace the two steps:

```yaml
      - name: Install dependencies
        run: |
          sudo apt-get update -q
          sudo apt-get install -y -q python3-gi gir1.2-gtk-3.0 gir1.2-notify-0.7 gir1.2-xapp-1.0 python3-requests python3-defusedxml appstream desktop-file-utils
      - name: Validate the AppStream metainfo and the desktop file
        run: |
          appstreamcli validate --no-net io.github.xb00tt.DebridApplet.metainfo.xml
          desktop-file-validate debrid-applet.desktop
      - name: Run the offline test suite
        run: python3 test_debrid_api.py
```

- [ ] **Step 2: release.yml**

Replace the "Check the tag matches VERSION" step with:

```yaml
      - name: Check the tag matches VERSION and the metainfo lists the release
        run: |
          V=$(python3 -c "import re;print(re.search(r'VERSION = \"([^\"]+)\"', open('debrid_applet.py').read()).group(1))")
          test "v$V" = "${GITHUB_REF_NAME}" || { echo "tag ${GITHUB_REF_NAME} != VERSION $V"; exit 1; }
          grep -q "<release version=\"$V\"" io.github.xb00tt.DebridApplet.metainfo.xml || { echo "metainfo lacks release $V"; exit 1; }
```

- [ ] **Step 3: Run the exact commands locally**

Run:
```bash
appstreamcli validate --no-net io.github.xb00tt.DebridApplet.metainfo.xml && desktop-file-validate debrid-applet.desktop && echo "validators ok"
V=1.1.0; grep -q "<release version=\"$V\"" io.github.xb00tt.DebridApplet.metainfo.xml && echo "gate passes for $V"
V=1.1.10; grep -q "<release version=\"$V\"" io.github.xb00tt.DebridApplet.metainfo.xml || echo "gate refuses $V"
python3 -c "import yaml,sys; [yaml.safe_load(open(f)) for f in ('.github/workflows/tests.yml','.github/workflows/release.yml')]; print('yaml ok')"
```
Expected: `validators ok`, `gate passes for 1.1.0`, `gate refuses 1.1.10`, `yaml ok` (if PyYAML is missing, `python3 -c "import yaml"` fails: skip that line and rely on the push).

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/tests.yml .github/workflows/release.yml
git commit -m "CI: validate the metainfo and desktop file; release refuses a tag without a release entry"
```

---

### Task 4: Comment and docs

**Files:**
- Modify: `debrid_applet.py:54`, `docs/DEVELOPING.md:24-27` and `:43-45`

**Interfaces:** none.

- [ ] **Step 1: The id comment**

Replace line 54:

```python
DBUS_ID = "io.github.xb00tt.DebridApplet"  # the app id: D-Bus name (single instance), AppStream id, Flatpak id
```

- [ ] **Step 2: DEVELOPING layout**

Replace

```
- `install.sh`, `debrid-applet.desktop`, `debrid-applet.svg`: per-user install.
```

with

```
- `install.sh`, `debrid-applet.desktop`, `debrid-applet.svg`: per-user install.
- `io.github.xb00tt.DebridApplet.metainfo.xml`: AppStream metainfo for app stores and Flathub; installed to
  `/usr/share/metainfo` by the package and `~/.local/share/metainfo` by `install.sh`. The id is `DBUS_ID`; the desktop
  file and icon keep the `debrid-applet` name (a Flatpak renames them at build time).
```

- [ ] **Step 3: DEVELOPING releases**

Replace the paragraph starting `Bump \`VERSION\` in \`debrid_applet.py\`, tag` with:

```
To release: bump `VERSION` in `debrid_applet.py` and the deb file name in the README, add a `<release>` entry (newest
first, the tag's date, one line) to `io.github.xb00tt.DebridApplet.metainfo.xml` and point its screenshot URL at the
new tag, then tag `vX.Y.Z` and push the tag. `python3 test_debrid_api.py` checks the file against `VERSION`;
`appstreamcli validate --no-net --pedantic io.github.xb00tt.DebridApplet.metainfo.xml` is the local lint. The release
workflow runs the tests, refuses a tag that doesn't match `VERSION` or has no release entry, builds the `.deb` (via
`packaging/build-deb.sh`) and a source tarball, and publishes a GitHub release with generated notes.
`packaging/build-deb.sh` also works locally on any Debian-based box.
```

- [ ] **Step 4: Check and commit**

Run: `python3 test_debrid_api.py | tail -1 && grep -c 'metainfo' docs/DEVELOPING.md`
Expected: `87 passed`, a count of at least `4`.

```bash
git add debrid_applet.py docs/DEVELOPING.md
git commit -m "Docs: metainfo in the layout and the release checklist; DBUS_ID is the app id"
```

---

### Task 5: Release 1.1.1

**Files:**
- Modify: `debrid_applet.py:52` (`VERSION`), `README.md:14`, `io.github.xb00tt.DebridApplet.metainfo.xml` (release entry, screenshot tag)

**Interfaces:** none.

- [ ] **Step 1: Make the test demand 1.1.1**

Edit `debrid_applet.py`: `VERSION = "1.1.1"`. Edit `README.md`: `sudo apt install ./debrid-applet_1.1.1_all.deb`.

Run: `python3 test_debrid_api.py`
Expected: `AssertionError` in `test_metainfo` at `releases[0].get("version") == da.VERSION` (the file still leads with 1.1.0).

- [ ] **Step 2: Add the release and move the screenshot tag**

In the metainfo, insert as the first child of `<releases>` (date = the day you tag; today if tagging now):

```xml
    <release version="1.1.1" date="2026-10-13"><description><p>AppStream metainfo for app stores.</p></description></release>
```

and change the screenshot `<image>` to `https://raw.githubusercontent.com/xb00tt/debrid-applet/v1.1.1/docs/screenshot.png`.

- [ ] **Step 3: Full check**

Run: `python3 test_debrid_api.py | tail -1 && appstreamcli validate --no-net --pedantic io.github.xb00tt.DebridApplet.metainfo.xml && packaging/build-deb.sh && python3 debrid_applet.py --version && dpkg-deb -c dist/debrid-applet_1.1.1_all.deb | grep -c metainfo`
Expected: `87 passed`, no validator output, `…/dist/debrid-applet_1.1.1_all.deb`, `debrid-applet 1.1.1`, `1`.

- [ ] **Step 4: Commit (no tag)**

```bash
git add debrid_applet.py README.md io.github.xb00tt.DebridApplet.metainfo.xml
git commit -m "Release 1.1.1"
```

Tag and push are the maintainer's: `git push origin main && git tag v1.1.1 && git push origin v1.1.1`. The first push
also runs the new `tests` workflow step; if the runner's appstreamcli reports something 1.0.2 here did not, fix the
file in a follow-up commit before tagging.

- [ ] **Step 5: After installing the new deb (maintainer, needs sudo)**

Run: `appstreamcli get io.github.xb00tt.DebridApplet | head -5`
Expected: the component's `Identifier`, `Name: Debrid Applet` and `Summary` lines from the system pool. If it prints
nothing, run `appstreamcli refresh --force` once as root and retry.
