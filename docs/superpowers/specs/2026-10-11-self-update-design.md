# Debrid Applet — Update check and self-update — Design

Date: 2026-10-11
Applies to: debrid-applet 1.0.3 on Linux Mint 22 Cinnamon (Python 3.12, GTK3, requests, policykit-1). The repository is public,
so the GitHub releases API and release assets are reachable without credentials.

## Goal

The applet tells the user when a newer release exists and installs it on request, with one password prompt for the `.deb`
install and none for a per-user install, then restarts itself. No token, no shell, no silent installs.

## Decisions (from brainstorm)

| Topic | Decision |
|---|---|
| Source of truth | `GET https://api.github.com/repos/xb00tt/debrid-applet/releases/latest` (unauthenticated; excludes drafts and prereleases) |
| Version rule | Tag `vX.Y.Z` only; install only when strictly newer than `VERSION`; never downgrade |
| Integrity | Asset `digest` (`sha256:…`) from the release JSON is mandatory; mismatch or missing digest refuses the update |
| Transport | HTTPS; the asset is fetched through its API URL with `Accept: application/octet-stream` (the browser URL lagged after the repo went public) |
| `.deb` install | `pkexec env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends /abs/path.deb`; Cinnamon's built-in polkit agent prompts |
| Per-user install | Tarball asset; only the four known member names are extracted; files replaced atomically; no prompt |
| Git checkout | Check only; the dialog says to run `git pull` |
| Cadence | 60 s after start, then every 24 h while running, when the "Check for updates daily" setting is on (default on) |
| Restart | `os.execv` of the same file after `gapp.run()` returns; same PID, the bus name is freed by the exec |
| Repeat suppression | "Skip this version" stores `update_skip` in the config |
| Out of scope | Signed releases (can be added later without changing the flow), a custom polkit `.policy` file, delta updates, update of Python dependencies |

## Files

```
debrid_update.py      # NEW, no GTK: version parsing, release lookup, download + verify, install plan, per-user install
debrid_applet.py      # glue: menu entries, About button, Settings checkbox, dialogs, notification, restart
test_debrid_api.py    # offline tests for debrid_update against canned JSON and a local HTTP server
docs/MANUAL.md        # "Updates" section; Settings → Advanced line
README.md             # one line
docs/DEVELOPING.md    # what a release must look like for the updater to see it
install.sh, packaging/build-deb.sh   # ship debrid_update.py (one filename added to each copy list)
```

## Module: `debrid_update.py`

Pure functions and one small data holder. Everything that can fail raises `ApiError` (imported from `debrid_api`) with a
message fit for a dialog; the GTK side never parses exceptions.

```python
REPO = "xb00tt/debrid-applet"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
FILES = ("debrid_api.py", "debrid_local_api.py", "debrid_update.py", "debrid_applet.py", "debrid-applet.svg")  # what install.sh copies

def parse_version(text) -> tuple[int, int, int] | None      # "v1.2.3" / "1.2.3" -> (1, 2, 3); anything else -> None
def latest_release(session=None) -> Release | None          # None when the latest tag is not newer than VERSION
def install_mode(here) -> "deb" | "git" | "user"             # here = directory of debrid_applet.py
def download(asset, dest_dir, session=None) -> path          # streams to <dest_dir>/<asset.name> (0600), verifies digest
def deb_command(path) -> list[str]                           # the pkexec/apt-get argv above
def install_user(tar_path, here)                             # extracts FILES from the tarball into here, atomically
```

`Release` (a `dataclass`): `version` (tuple), `tag`, `notes_url` (release page), `deb` and `tar` (each an `Asset` with
`name`, `url` (API asset URL), `size`, `digest`, or `None` when the release lacks it).

Rules:

- `latest_release` sends `Accept: application/vnd.github+json` and `User-Agent: debrid-applet/<VERSION>`, 15 s timeout.
  HTTP 403/429 → `ApiError("GitHub rate limit, try again later")`; other failures → `ApiError("Update check failed: …")`
  with the host only (same scrubbing rule as `fetch_bytes`). A tag that does not parse → `ApiError`.
- `download` refuses before any transfer when the asset has no `digest`; streams with `allow_redirects=True` (the hop goes
  to `objects.githubusercontent.com`; the digest check makes the hop's identity irrelevant); stops at
  `2 * asset.size` bytes; writes to `<name>.part` then renames; on mismatch deletes the file and raises
  `ApiError("Downloaded file does not match the release checksum")`.
- `install_mode`: `here.startswith("/usr/")` → `deb`; `os.path.isdir(os.path.join(here, ".git"))` → `git`; else `user`.
- `install_user`: opens the tarball with `tarfile`, reads each `<prefix>/<name>` member for `name in FILES` via
  `extractfile` (missing member → `ApiError`), writes `<here>/<name>.tmp` then `os.replace`. No `extractall`.
- `deb_command` returns the argv verbatim; the caller runs it with `subprocess.Popen` (no shell).

## Glue: `debrid_applet.py`

- Config: `check_updates: True`, `update_skip: ""` in `EMPTY_CONFIG`; Settings → Advanced gets
  `self._check(g, "check_updates", "Check for updates daily")` under a new "Updates" heading, above "Activity log".
- `App.__init__`: `GLib.timeout_add_seconds(60, self.check_updates_auto)` once, and the same every 86 400 s, both only
  when `cfg["check_updates"]`.
- `App.check_updates(manual)`: runs `latest_release` in `run_async`. Manual: a `Gtk.MessageDialog` with either
  "You have the latest version (1.0.3)" / Close, or "Debrid Applet 1.0.4 is available" with buttons Update, Release
  notes, Close. Automatic: nothing when up to date or when `version == cfg["update_skip"]`; otherwise a notification
  "Debrid Applet 1.0.4 available" with buttons Update, Release notes, Skip this version. A manual check ignores
  `update_skip`. Errors: manual → the same dialog
  with the message; automatic → `log_event("", …, "verbose")` and nothing else.
- `App.update_to(release)`: by `install_mode(HERE)`:
  - `git`: dialog "This is a git checkout: run `git pull`" (manual) / log line (automatic). No download.
  - `user`: status "Downloading update 1.0.4…" → `download(release.tar, DATA_DIR/updates)` → `install_user` → restart dialog.
  - `deb`: status "Downloading update 1.0.4…" → `download(release.deb, DATA_DIR/updates)` → status
    "Installing update 1.0.4… (password prompt)" → `Popen(deb_command(path))` waited on a thread like `auto_download`.
    Exit 0 → restart dialog; 126/127 → "Update cancelled: authentication did not complete"; other → dialog "Update failed
    (apt-get exited N)" with a "Copy sudo command" button that copies `sudo apt install /abs/path.deb`.
  - The downloaded file is removed after a successful install; left in place after a failure for the sudo fallback.
- Restart dialog: "Debrid Applet 1.0.4 is installed. Restart now?" Yes → `self.hide_window()` (saves the window size),
  `state["restart"] = True`, `self.gapp.quit()`. `main()` after `gapp.run(sys.argv)`:
  `if state.get("restart"): os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])`.
- Entry points: tray menu "Check for updates…" (before About…), a "Check for updates" button added to the About dialog
  (`dlg.add_button`), both call `check_updates(manual=True)`.
- Status lines use the current tab's `say()`; the "Updates" directory is created 0700 like the rest of `DATA_DIR`.

## Error handling

| Case | Behaviour |
|---|---|
| Offline / API down (automatic) | verbose log line, nothing shown |
| Offline / API down (manual) | dialog with the host-only message |
| Rate limited | "GitHub rate limit, try again later" |
| Release without `.deb` / tarball for the current mode | dialog "Release 1.0.4 has no package for this install", Release notes button still works |
| Missing or wrong digest | refused; file deleted; dialog |
| Oversize download | stopped at 2× declared size; dialog |
| pkexec cancelled (126/127) | status line, no dialog |
| apt-get failure | dialog with exit code and "Copy sudo command" |
| Per-user directory not writable | `ApiError` from `install_user`, dialog |
| Restart | exec replaces the process; should exec fail, the process exits with the error on stderr and the autostart entry starts the new version at the next login |

## Security

- HTTPS only; hosts are GitHub's; no credentials sent or stored.
- The digest comes from the same JSON as the asset URL, so it protects integrity in transit and on disk, not against a
  compromised GitHub account; that risk equals today's manual install. Signed releases are the upgrade path.
- Fixed argv, absolute paths, no shell anywhere; the install runs only after a click; the version compare refuses downgrades.
- Downloaded files live in `~/.local/share/debrid-applet/updates/` (dir 0700, file 0600); apt-get as root reads them fine.
- Tarball members are read by exact name; no extraction of arbitrary paths.

## Testing

Offline, in `test_debrid_api.py`, with a `FakeSession` for the API call and a local `http.server` for the download:

- `parse_version`: `v1.2.3`, `1.2.3`, `v1.2`, `v1.2.3-rc1`, `""` → tuples or `None`.
- `latest_release`: canned JSON newer / equal / older than `VERSION`; missing `.deb` asset → `deb is None`; bad tag → `ApiError`;
  403 → rate-limit message.
- `download`: good digest → file written 0600 and returned; wrong digest → `ApiError` and no file left; no digest → refused
  before any request (session records zero calls); body larger than 2× size → `ApiError`.
- `install_mode`: `/usr/share/debrid-applet`, a temp dir with `.git`, a plain temp dir.
- `install_user`: a tarball built in the test with prefix `debrid-applet-9.9.9/` → the five files replaced in a temp
  `here`; a tarball missing one member → `ApiError` and `here` untouched.
- `deb_command`: exact argv.
- Config default `check_updates` in the roundtrip test.

GTK glue is covered by an offscreen construction of the dialogs and a run of `check_updates(manual=True)` against a stubbed
`latest_release`, plus a manual test of the real flow on this machine (install 1.0.3 → release 1.0.4 → Update → restart).

## Release requirements (for DEVELOPING.md)

Tag `vX.Y.Z`; assets named `debrid-applet_X.Y.Z_all.deb` and `debrid-applet-X.Y.Z.tar.gz`, both produced by the existing
workflow; GitHub computes the digests. The updater ignores drafts and prereleases.
