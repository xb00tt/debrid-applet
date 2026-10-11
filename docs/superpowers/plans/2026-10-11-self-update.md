# Update Check and Self-Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The applet notices a newer GitHub release, installs it on request (one polkit password prompt for the `.deb`, none for a per-user copy) and restarts itself.

**Architecture:** A new GTK-free module `debrid_update.py` does everything testable offline: version parsing, the `releases/latest` lookup, a verified download, the apt argv and the per-user tarball install. `debrid_applet.py` adds the glue: config defaults, a Settings checkbox, tray-menu and About entry points, dialogs and notifications, the install driver and the exec-based restart.

**Tech Stack:** Python 3.12, `requests`, `hashlib`, `tarfile`, GTK3 via PyGObject, `pkexec` + `apt-get` (policykit-1, preinstalled on Mint), GitHub REST API (unauthenticated).

**Spec:** `docs/superpowers/specs/2026-10-11-self-update-design.md`

## Global Constraints

- Python floor is 3.11 (`Depends: python3 (>= 3.11)` in `packaging/build-deb.sh`); `X | None` annotations are fine, no `tarfile` `filter=` reliance.
- `debrid_update.py` imports no GTK and nothing from `debrid_applet.py` (same rule as `debrid_api.py` and `debrid_local_api.py`).
- Release source: `https://api.github.com/repos/xb00tt/debrid-applet/releases/latest`, no credentials ever.
- Install only when the release version is strictly newer than `VERSION`; never downgrade.
- Asset `digest` (`sha256:…`) is mandatory; refuse before any transfer without it; delete the file on mismatch.
- No shell: every subprocess gets an argv list; the `.deb` path is absolute.
- Downloaded files: `<DATA_DIR>/updates/`, directory 0700, files 0600.
- Every test or harness that imports `debrid_applet` must have `DEBRID_APPLET_CONFIG_DIR` and `DEBRID_APPLET_DATA_DIR` set to scratch directories first (the suite does this at the top of `test_debrid_api.py`).
- Tests: `python3 test_debrid_api.py`, plain asserts, no framework; currently 72 pass.
- Deviation from the spec's sketch: `latest_release(current, session=None)` takes the installed version as a parameter
  (the glue passes `VERSION`), because the module must not import `debrid_applet`.
- Commit messages: plain English, end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

1. A tag that is not `vX.Y.Z` (`v1.0.4-rc1`, `latest`): the check must report a readable error, not crash or install. Pinned in Task 1.
2. A release asset without `digest` (GitHub omits it for some old uploads): the download must refuse before sending a request. Pinned in Task 2.
3. The asset URL redirects to another host (`objects.githubusercontent.com`): the redirect is followed and the digest is still enforced. Pinned in Task 2.
4. A tarball member named `prefix/../evil.py` or a symlink member: nothing outside `here` may be written and the install must fail cleanly. Pinned in Task 3.
5. `install_user` into a directory the user cannot write (wrong mode detection, root-owned copy): a readable `ApiError`, no half-replaced files. Pinned in Task 3.

---

### Task 1: `debrid_update.py` core: version parsing, release lookup, install mode, apt argv

**Files:**
- Create: `debrid_update.py`
- Test: `test_debrid_api.py` (append after `test_added_within`, before `test_add_label`)

**Interfaces:**
- Consumes: `ApiError` from `debrid_api`; the test suite's `FakeSession`/`FakeResp` (top of `test_debrid_api.py`: `FakeSession(*responses).request(method, url, **kw)` pops one `FakeResp(status, body)` per call and records `calls`).
- Produces:
  - `parse_version(text) -> tuple[int, int, int] | None`
  - `@dataclass Asset(name: str, url: str, size: int, digest: str)`
  - `@dataclass Release(version: tuple, tag: str, notes_url: str, deb: Asset | None, tar: Asset | None)`
  - `latest_release(current: str, session=None) -> Release | None` (raises `ApiError`)
  - `install_mode(here: str) -> "deb" | "git" | "user"`
  - `deb_command(path: str) -> list[str]`
  - constants `REPO`, `API`, `FILES`

- [ ] **Step 1: Write the failing tests**

Append to `test_debrid_api.py` right before `def test_add_label():`:

```python
RELEASE_JSON = {
    "tag_name": "v9.9.9", "html_url": "https://github.com/xb00tt/debrid-applet/releases/tag/v9.9.9",
    "assets": [
        {"name": "debrid-applet-9.9.9.tar.gz", "url": "https://api.github.com/repos/xb00tt/debrid-applet/releases/assets/1",
         "size": 1000, "digest": "sha256:" + "a" * 64},
        {"name": "debrid-applet_9.9.9_all.deb", "url": "https://api.github.com/repos/xb00tt/debrid-applet/releases/assets/2",
         "size": 2000, "digest": "sha256:" + "b" * 64},
    ],
}


def test_update_parse_version_and_latest_release():
    import debrid_update as du
    assert du.parse_version("v1.2.3") == (1, 2, 3) and du.parse_version("1.2.3") == (1, 2, 3)
    assert du.parse_version("v1.2") is None and du.parse_version("v1.2.3-rc1") is None and du.parse_version("") is None
    assert du.parse_version("latest") is None and du.parse_version(None) is None
    s = FakeSession(FakeResp(200, RELEASE_JSON))
    rel = du.latest_release("1.0.3", s)
    assert rel.version == (9, 9, 9) and rel.tag == "v9.9.9" and rel.notes_url.endswith("/tag/v9.9.9")
    assert rel.deb.name == "debrid-applet_9.9.9_all.deb" and rel.deb.size == 2000 and rel.deb.digest == "sha256:" + "b" * 64
    assert rel.deb.url.endswith("/assets/2") and rel.tar.name == "debrid-applet-9.9.9.tar.gz"
    method, url, kw = s.calls[0]
    assert (method, url) == ("GET", du.API) and kw["headers"]["Accept"] == "application/vnd.github+json"
    assert kw["headers"]["User-Agent"] == "debrid-applet/1.0.3" and kw["timeout"] == 15
    assert du.latest_release("9.9.9", FakeSession(FakeResp(200, RELEASE_JSON))) is None  # equal: nothing to do
    assert du.latest_release("10.0.0", FakeSession(FakeResp(200, RELEASE_JSON))) is None  # newer installed: never downgrade
    rel = du.latest_release("1.0.3", FakeSession(FakeResp(200, {**RELEASE_JSON, "assets": [RELEASE_JSON["assets"][0]]})))
    assert rel.deb is None and rel.tar is not None  # a release without a .deb is still reported
    for body, text in (({**RELEASE_JSON, "tag_name": "v1.0.4-rc1"}, "unexpected tag"), ({**RELEASE_JSON, "tag_name": "latest"}, "unexpected tag")):
        try:
            du.latest_release("1.0.3", FakeSession(FakeResp(200, body)))
            assert False, body["tag_name"]
        except ApiError as e:
            assert text in str(e)
    for status, text in ((403, "rate limit"), (429, "rate limit"), (500, "HTTP 500"), (404, "HTTP 404")):
        try:
            du.latest_release("1.0.3", FakeSession(FakeResp(status, {"message": "x"})))
            assert False, status
        except ApiError as e:
            assert text in str(e)
    try:
        du.latest_release("1.0.3", FakeSession(requests.ConnectionError("boom https://api.github.com/x")))
        assert False
    except ApiError as e:
        assert str(e) == "Update check failed: ConnectionError"  # type only, never the URL
    try:
        du.latest_release("1.0.3", FakeSession(FakeResp(200, None)))  # non-JSON body
        assert False
    except ApiError as e:
        assert "unexpected response" in str(e)


def test_update_install_mode_and_deb_command():
    import os, shutil
    import debrid_update as du
    assert du.install_mode("/usr/share/debrid-applet") == "deb"
    root = "/tmp/debrid-test-mode"
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(os.path.join(root, "git", ".git"))
    os.makedirs(os.path.join(root, "user"))
    assert du.install_mode(os.path.join(root, "git")) == "git" and du.install_mode(os.path.join(root, "user")) == "user"
    assert du.deb_command("/home/u/.local/share/debrid-applet/updates/x.deb") == [
        "pkexec", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "--no-install-recommends",
        "/home/u/.local/share/debrid-applet/updates/x.deb"]
    assert du.FILES == ("debrid_api.py", "debrid_local_api.py", "debrid_update.py", "debrid_applet.py", "debrid-applet.svg")
```

- [ ] **Step 2: Run the suite to see the new tests fail**

Run: `cd /home/borislav/github/debrid-applet && python3 test_debrid_api.py 2>&1 | tail -3`
Expected: `ModuleNotFoundError: No module named 'debrid_update'` (the runner stops at the first failure).

- [ ] **Step 3: Create the module**

Write `debrid_update.py`:

```python
"""Update check and self-update helpers. No GTK here; safe to import anywhere.

The latest GitHub release is the source of truth. Assets are fetched through their API URL and verified against the
digest GitHub publishes in the same JSON. Nothing here needs credentials."""
import hashlib
import os
import tarfile
from dataclasses import dataclass

import requests

from debrid_api import ApiError

REPO = "xb00tt/debrid-applet"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
FILES = ("debrid_api.py", "debrid_local_api.py", "debrid_update.py", "debrid_applet.py", "debrid-applet.svg")  # what install.sh copies
TIMEOUT = 15


@dataclass
class Asset:
    name: str
    url: str  # API asset URL; answered with the file when asked for application/octet-stream
    size: int
    digest: str  # "sha256:<hex>", "" when GitHub gave none


@dataclass
class Release:
    version: tuple
    tag: str
    notes_url: str
    deb: Asset | None
    tar: Asset | None


def parse_version(text):
    """'v1.2.3' or '1.2.3' -> (1, 2, 3); anything else -> None."""
    parts = str(text or "").strip().lstrip("v").split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def _asset(a):
    return Asset(name=str(a.get("name") or ""), url=str(a.get("url") or ""), size=int(a.get("size") or 0),
                 digest=str(a.get("digest") or ""))


def latest_release(current, session=None):
    """The newest release on GitHub when it is newer than current (a version string); None otherwise."""
    s = session or requests.Session()
    try:
        r = s.request("GET", API, headers={"Accept": "application/vnd.github+json", "User-Agent": f"debrid-applet/{current}"},
                      timeout=TIMEOUT)
    except requests.RequestException as e:  # the text would carry the URL; the type is enough
        raise ApiError(f"Update check failed: {type(e).__name__}") from e
    if r.status_code in (403, 429):
        raise ApiError("GitHub rate limit, try again later")
    if r.status_code != 200:
        raise ApiError(f"Update check failed: HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError:
        raise ApiError("Update check failed: unexpected response") from None
    if not isinstance(body, dict):
        raise ApiError("Update check failed: unexpected response")
    version = parse_version(body.get("tag_name"))
    if version is None:
        raise ApiError(f"Update check failed: unexpected tag {body.get('tag_name')!r}")
    mine = parse_version(current)
    if mine is None or version <= mine:
        return None
    assets = [_asset(a) for a in body.get("assets") or [] if isinstance(a, dict)]
    return Release(version=version, tag=str(body.get("tag_name")),
                   notes_url=str(body.get("html_url") or f"https://github.com/{REPO}/releases"),
                   deb=next((a for a in assets if a.name.endswith("_all.deb")), None),
                   tar=next((a for a in assets if a.name.endswith(".tar.gz")), None))


def install_mode(here):
    """'deb' (the package, under /usr), 'git' (a checkout: update with git pull) or 'user' (an install.sh copy)."""
    if here.startswith("/usr/"):
        return "deb"
    if os.path.isdir(os.path.join(here, ".git")):
        return "git"
    return "user"


def deb_command(path):
    """argv that installs the downloaded .deb behind one polkit password prompt; no shell anywhere."""
    return ["pkexec", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "--no-install-recommends", path]
```

- [ ] **Step 4: Run the suite**

Run: `python3 test_debrid_api.py 2>&1 | tail -1`
Expected: `74 passed`

- [ ] **Step 5: Commit**

```bash
git add debrid_update.py test_debrid_api.py
git commit -m "debrid_update: version parsing, latest-release lookup, install mode, apt argv

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Verified download

**Files:**
- Modify: `debrid_update.py` (append)
- Test: `test_debrid_api.py` (append after `test_update_install_mode_and_deb_command`)

**Interfaces:**
- Consumes: `Asset`, `ApiError` from Task 1.
- Produces: `download(asset: Asset, dest_dir: str, session=None) -> str` (absolute path of the verified file; raises `ApiError`), private `_unlink(path)`.

- [ ] **Step 1: Write the failing test**

Append to `test_debrid_api.py`:

```python
def test_update_download_verifies_digest():
    import hashlib, http.server, os, shutil, stat, threading
    import debrid_update as du
    root, dest = "/tmp/debrid-test-update-src", "/tmp/debrid-test-update-dest"
    for d in (root, dest):
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(root)
    payload = b"\x1f\x8b" + b"x" * 5000
    open(os.path.join(root, "good.deb"), "wb").write(payload)
    open(os.path.join(root, "huge.deb"), "wb").write(b"y" * 1_500_000)
    good = "sha256:" + hashlib.sha256(payload).hexdigest()

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            if self.path == "/redir":  # GitHub answers the API asset URL with a redirect to its CDN
                self.send_response(302); self.send_header("Location", "/good.deb"); self.end_headers(); return
            assert self.headers.get("Accept") == "application/octet-stream", self.headers.get("Accept")
            super().do_GET()
        def copyfile(self, source, outputfile):
            try:
                super().copyfile(source, outputfile)
            except (BrokenPipeError, ConnectionResetError):
                pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lambda *a, **k: Quiet(*a, directory=root, **k))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    asset = lambda name, path, size, digest: du.Asset(name=name, url=base + path, size=size, digest=digest)
    path = du.download(asset("good.deb", "/good.deb", len(payload), good), dest)
    assert path == os.path.join(dest, "good.deb") and open(path, "rb").read() == payload
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and stat.S_IMODE(os.stat(dest).st_mode) == 0o700
    assert not os.path.exists(path + ".part")
    assert du.download(asset("good.deb", "/redir", len(payload), good), dest) == path  # redirect followed, digest still checked
    try:
        du.download(asset("bad.deb", "/good.deb", len(payload), "sha256:" + "0" * 64), dest)
        assert False
    except ApiError as e:
        assert "checksum" in str(e) and not os.path.exists(os.path.join(dest, "bad.deb")) and not os.path.exists(os.path.join(dest, "bad.deb.part"))
    s = FakeSession()
    try:
        du.download(asset("nodigest.deb", "/good.deb", len(payload), ""), dest, s)  # refused before any request
        assert False
    except ApiError as e:
        assert "no checksum" in str(e) and s.calls == []
    try:
        du.download(asset("huge.deb", "/huge.deb", 100, "sha256:" + "0" * 64), dest)  # body far larger than declared
        assert False
    except ApiError as e:
        assert "larger" in str(e) and not os.path.exists(os.path.join(dest, "huge.deb.part"))
    try:
        du.download(asset("missing.deb", "/missing.deb", 10, good), dest)
        assert False
    except ApiError as e:
        assert str(e) == "Download failed: HTTPError" and not os.path.exists(os.path.join(dest, "missing.deb.part"))
    srv.shutdown()
```

- [ ] **Step 2: Run the suite to see it fail**

Run: `python3 test_debrid_api.py 2>&1 | tail -3`
Expected: `AttributeError: module 'debrid_update' has no attribute 'download'`

- [ ] **Step 3: Implement `download`**

Append to `debrid_update.py`:

```python
def download(asset, dest_dir, session=None):
    """Fetch asset into dest_dir (file 0600) and verify its sha256 against the release digest; the path on success.
    Refuses without a digest, stops at twice the declared size, leaves nothing behind on failure."""
    if not asset.digest.startswith("sha256:"):
        raise ApiError(f"Release has no checksum for {asset.name}; not installing")
    os.makedirs(dest_dir, mode=0o700, exist_ok=True)
    path = os.path.join(dest_dir, asset.name)
    part = path + ".part"
    limit = max(2 * asset.size, 1_000_000)
    digest = hashlib.sha256()
    s = session or requests.Session()
    try:
        with s.get(asset.url, headers={"Accept": "application/octet-stream", "User-Agent": "debrid-applet"},
                   timeout=30, stream=True, allow_redirects=True) as r:
            r.raise_for_status()
            size = 0
            with open(os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
                for chunk in r.iter_content(65536):
                    size += len(chunk)
                    if size > limit:
                        raise ApiError(f"{asset.name} is larger than the release says; not installing")
                    digest.update(chunk)
                    f.write(chunk)
    except requests.RequestException as e:
        _unlink(part)
        raise ApiError(f"Download failed: {type(e).__name__}") from e
    except ApiError:
        _unlink(part)
        raise
    if "sha256:" + digest.hexdigest() != asset.digest:
        _unlink(part)
        raise ApiError("Downloaded file does not match the release checksum")
    os.replace(part, path)
    return path


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass
```

Note: `FakeSession` has no `get`, which is why the no-digest case must refuse before touching the session; the test asserts exactly that.

- [ ] **Step 4: Run the suite**

Run: `python3 test_debrid_api.py 2>&1 | tail -1`
Expected: `75 passed`

- [ ] **Step 5: Commit**

```bash
git add debrid_update.py test_debrid_api.py
git commit -m "debrid_update: verified download (digest mandatory, size cap, 0600 files)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Per-user install from the tarball

**Files:**
- Modify: `debrid_update.py` (append)
- Test: `test_debrid_api.py` (append after `test_update_download_verifies_digest`)

**Interfaces:**
- Consumes: `FILES`, `ApiError`, `_unlink` from Tasks 1–2.
- Produces: `install_user(tar_path: str, here: str) -> None` (raises `ApiError`).

- [ ] **Step 1: Write the failing test**

Append to `test_debrid_api.py`:

```python
def test_update_install_user_from_tarball():
    import io, os, shutil, stat, tarfile
    import debrid_update as du
    root = "/tmp/debrid-test-update-user"
    shutil.rmtree(root, ignore_errors=True)
    here = os.path.join(root, "here")
    os.makedirs(here)
    for name in du.FILES:
        open(os.path.join(here, name), "w").write("old")

    def build(tar_path, names, extra=()):
        with tarfile.open(tar_path, "w:gz") as tar:
            for name in names:
                data = f"new {name}".encode()
                info = tarfile.TarInfo(f"debrid-applet-9.9.9/{name}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            for info in extra:
                tar.addfile(info, io.BytesIO(b"evil") if info.isfile() else None)
    good = os.path.join(root, "good.tar.gz")
    build(good, du.FILES)
    du.install_user(good, here)
    for name in du.FILES:
        assert open(os.path.join(here, name)).read() == f"new {name}" and not os.path.exists(os.path.join(here, name + ".tmp"))
    # a member that escapes the prefix and a symlink member are ignored: only the five names are ever read
    evil = tarfile.TarInfo("debrid-applet-9.9.9/../evil.py"); evil.size = 4
    link = tarfile.TarInfo("debrid-applet-9.9.9/debrid_api.py"); link.type = tarfile.SYMTYPE; link.linkname = "/etc/passwd"
    bad = os.path.join(root, "bad.tar.gz")
    build(bad, [n for n in du.FILES if n != "debrid_api.py"], extra=(evil, link))
    try:
        du.install_user(bad, here)
        assert False
    except ApiError as e:
        assert "debrid_api.py" in str(e)
    assert not os.path.exists(os.path.join(root, "evil.py")) and open(os.path.join(here, "debrid_api.py")).read() == "new debrid_api.py"
    ro = os.path.join(root, "ro")
    os.makedirs(ro)
    os.chmod(ro, 0o500)
    try:
        du.install_user(good, ro)
        assert False
    except ApiError as e:
        assert "Cannot write" in str(e)
    finally:
        os.chmod(ro, 0o700)
    assert os.listdir(ro) == []
```

- [ ] **Step 2: Run the suite to see it fail**

Run: `python3 test_debrid_api.py 2>&1 | tail -3`
Expected: `AttributeError: module 'debrid_update' has no attribute 'install_user'`

- [ ] **Step 3: Implement `install_user`**

Append to `debrid_update.py`:

```python
def install_user(tar_path, here):
    """Replace FILES in here with the copies inside the release tarball (members <prefix>/<name>), one atomic
    rename per file. Members are read by exact name; nothing else in the archive is looked at."""
    with tarfile.open(tar_path, "r:gz") as tar:
        members = {m.name.split("/", 1)[1]: m for m in tar.getmembers() if m.isfile() and "/" in m.name}
        missing = [n for n in FILES if n not in members]
        if missing:
            raise ApiError(f"Release tarball lacks {', '.join(missing)}")
        for name in FILES:
            data = tar.extractfile(members[name]).read()
            tmp = os.path.join(here, name + ".tmp")
            try:
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, os.path.join(here, name))
            except OSError as e:
                _unlink(tmp)
                raise ApiError(f"Cannot write {name}: {e.strerror}") from e
```

- [ ] **Step 4: Run the suite**

Run: `python3 test_debrid_api.py 2>&1 | tail -1`
Expected: `76 passed`

- [ ] **Step 5: Commit**

```bash
git add debrid_update.py test_debrid_api.py
git commit -m "debrid_update: per-user install from the release tarball

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Ship the module; config defaults; Settings checkbox

**Files:**
- Modify: `install.sh:11`, `packaging/build-deb.sh:12` (copy lists)
- Modify: `debrid_applet.py` (`EMPTY_CONFIG` near line 79, `SettingsForm._advanced` near line 2313, import block near line 42)
- Test: `test_debrid_api.py` (`defaults` dict in `test_config_roundtrip_and_mode`, line ~578)

**Interfaces:**
- Produces: `cfg["check_updates"]` (bool, default `True`), `cfg["update_skip"]` (str, default `""`); `debrid_applet` imports `deb_command, download, install_mode, install_user, latest_release` from `debrid_update`.

- [ ] **Step 1: Write the failing test**

In `test_debrid_api.py`, change the end of the `defaults` dict in `test_config_roundtrip_and_mode`:

```python
                "time_filter": "week"}
```
to
```python
                "time_filter": "week", "check_updates": True, "update_skip": ""}
```

- [ ] **Step 2: Run the suite to see it fail**

Run: `python3 test_debrid_api.py 2>&1 | grep -E 'AssertionError|passed' | head -2`
Expected: `AssertionError` in `test_config_roundtrip_and_mode` (the dict comparison).

- [ ] **Step 3: Config defaults, import, Settings checkbox**

In `debrid_applet.py`, change:

```python
                "config_version": 2,  # bumped when the layout changes; see migrate_config()
```
to
```python
                "check_updates": True, "update_skip": "",  # daily GitHub release check; version the user chose to skip
                "config_version": 2,  # bumped when the layout changes; see migrate_config()
```

Change the import line:

```python
from debrid_local_api import LocalApi  # noqa: E402
```
to
```python
from debrid_local_api import LocalApi  # noqa: E402
from debrid_update import deb_command, download, install_mode, install_user, latest_release  # noqa: E402
```

In `SettingsForm._advanced`, change:

```python
        self._heading(g, "Activity log")
```
to
```python
        self._heading(g, "Updates")
        self._check(g, "check_updates", "Check for updates daily (GitHub releases)")
        self._heading(g, "Activity log")
```

Change the copy lists so the package and the per-user install ship the module:

`install.sh` line 11:
```sh
cp "$SRC/debrid_api.py" "$SRC/debrid_local_api.py" "$SRC/debrid_update.py" "$SRC/debrid_applet.py" "$SRC/debrid-applet.svg" "$DIR/"
```
`packaging/build-deb.sh` line 12:
```sh
cp "$SRC/debrid_api.py" "$SRC/debrid_local_api.py" "$SRC/debrid_update.py" "$SRC/debrid_applet.py" "$SRC/debrid-applet.svg" "$APP/"
```

- [ ] **Step 4: Run the suite and a package build**

Run: `python3 test_debrid_api.py 2>&1 | tail -1 && packaging/build-deb.sh >/dev/null && dpkg-deb --contents dist/debrid-applet_1.0.3_all.deb | grep -c debrid_update.py`
Expected: `76 passed` then `1`

- [ ] **Step 5: Commit**

```bash
git add install.sh packaging/build-deb.sh debrid_applet.py test_debrid_api.py
git commit -m "Ship debrid_update.py; check_updates / update_skip config; Settings checkbox

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Check for updates: dialogs, notification, timers, menu and About entry points

**Files:**
- Modify: `debrid_applet.py` (`App.__init__` after `self.start_api()` near line 1891; tray menu near line 1863; `open_about` near line 2117; new methods next to `open_about`)

**Interfaces:**
- Consumes: `latest_release(VERSION)`, `Release` fields `version`, `notes_url`; `run_async`, `notify`, `open_url`, `log_event`, `save_config`, `VERSION`.
- Produces: `App.check_updates(manual=True)`, `App._dialog(title, text)`, `App._ask(text, yes_label) -> bool`, `App._skip(version_text)`, `App._update_dialog(release, v)`; `App.update_to(release)` is only *called* here and implemented in Task 6, so add the stub shown in Step 1 now.

- [ ] **Step 1: Add the methods**

In `debrid_applet.py`, insert before `def open_about(self, *_):`:

```python
    # ----- updates -----
    def _dialog(self, title, text):
        dlg = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.INFO,
                                buttons=Gtk.ButtonsType.CLOSE, text=title)
        dlg.format_secondary_text(text)
        dlg.run()
        dlg.destroy()

    def _ask(self, text, yes_label):
        dlg = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.QUESTION, text=text)
        dlg.add_buttons("Not now", Gtk.ResponseType.NO, yes_label, Gtk.ResponseType.YES)
        dlg.set_default_response(Gtk.ResponseType.YES)
        answer = dlg.run()
        dlg.destroy()
        return answer == Gtk.ResponseType.YES

    def _skip(self, v):
        self.cfg["update_skip"] = v
        save_config(self.cfg)

    def _update_dialog(self, release, v):
        dlg = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.INFO,
                                text=f"Debrid Applet {v} is available")
        dlg.format_secondary_text(f"You have {VERSION}. Update downloads the release and installs it"
                                  + (" (one password prompt)." if install_mode(HERE) == "deb" else "."))
        dlg.add_buttons("Release notes", 1, "Close", Gtk.ResponseType.CLOSE, "Update", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        answer = dlg.run()
        dlg.destroy()
        if answer == 1:
            open_url(release.notes_url)
        elif answer == Gtk.ResponseType.OK:
            self.update_to(release)

    def check_updates(self, manual=True):
        """Ask GitHub for the latest release. Manual checks always report in a dialog; automatic ones only notify
        about a newer version the user has not skipped, and log everything else at verbose level."""
        def done(release, err):
            if err:
                if manual:
                    self._dialog("Update check failed", err)
                else:
                    log_event("", f"Update check: {err}", "verbose")
                return
            if release is None:
                if manual:
                    self._dialog("Up to date", f"Debrid Applet {VERSION} is the latest version.")
                else:
                    log_event("", "Update check: up to date", "verbose")
                return
            v = ".".join(map(str, release.version))
            log_event("", f"Update check: {v} available")
            if manual:
                self._update_dialog(release, v)
            elif v != self.cfg.get("update_skip"):
                notify(f"Debrid Applet {v} available", f"You have {VERSION}.",
                       [("Update", lambda: self.update_to(release)), ("Release notes", lambda: open_url(release.notes_url)),
                        ("Skip this version", lambda: self._skip(v))])
        run_async(lambda: latest_release(VERSION), done)

    def _check_updates_auto(self):
        if self.cfg.get("check_updates"):
            self.check_updates(manual=False)
        return True

    def update_to(self, release):
        """Replaced in the next task."""
        self._dialog("Not yet", "Self-update arrives in the next commit.")

```

In `App.__init__`, change:

```python
        self.start_api()
```
(the one at the end of `__init__`, line ~1891, not the one in `open_settings`) to
```python
        self.start_api()
        GLib.timeout_add_seconds(60, lambda: self._check_updates_auto() and False)  # once, shortly after start
        GLib.timeout_add_seconds(86400, self._check_updates_auto)  # then daily while running
```

In the tray menu, change:

```python
        for label, cb in (("Refresh", self.refresh), ("Settings…", self.open_settings), ("About…", self.open_about),
                          ("Quit", lambda *_: self.gapp.quit())):
```
to
```python
        for label, cb in (("Refresh", self.refresh), ("Settings…", self.open_settings),
                          ("Check for updates…", lambda *_: self.check_updates(True)), ("About…", self.open_about),
                          ("Quit", lambda *_: self.gapp.quit())):
```

In `open_about`, change:

```python
        dlg.connect("response", lambda d, _r: d.destroy())
        dlg.show()
```
to
```python
        dlg.add_button("Check for updates", 1)
        dlg.connect("response", lambda d, r: (d.destroy(), self.check_updates(True)) if r == 1 else d.destroy())
        dlg.show()
```

- [ ] **Step 2: Run the suite (unchanged count) and an offscreen check of the glue**

Run: `python3 test_debrid_api.py 2>&1 | tail -1`
Expected: `76 passed`

Then run this scratch script (it never touches the real config: env vars first, `save_config`, `notify`, `run_async` and the dialogs are stubbed):

```bash
DEBRID_APPLET_CONFIG_DIR=/tmp/debrid-smoke-cfg DEBRID_APPLET_DATA_DIR=/tmp/debrid-smoke-data python3 - <<'EOF'
import sys; sys.path.insert(0, "/home/borislav/github/debrid-applet")
import debrid_applet as da
import debrid_update as du
shown, notes = [], []
def sync_run_async(fn, on_done):  # run the worker inline; deliver ApiError like the real one does
    try:
        on_done(fn(), None)
    except da.ApiError as e:
        on_done(None, str(e))
da.run_async = sync_run_async
da.save_config = lambda cfg: None
da.notify = lambda title, body, actions=(): notes.append((title, [a for a, _ in actions]))
da.App._dialog = lambda self, title, text: shown.append(("dialog", title))
da.App._update_dialog = lambda self, release, v: shown.append(("update", v))
app = da.App.__new__(da.App); app.cfg = {**da.EMPTY_CONFIG}; app.window = None
rel = du.Release(version=(9, 9, 9), tag="v9.9.9", notes_url="https://x", deb=None, tar=None)
da.latest_release = lambda current: rel
app.check_updates(True);  assert shown[-1] == ("update", "9.9.9")
app.check_updates(False); assert notes[-1] == ("Debrid Applet 9.9.9 available", ["Update", "Release notes", "Skip this version"])
app.cfg["update_skip"] = "9.9.9"; n = len(notes)
app.check_updates(False); assert len(notes) == n            # skipped version: silent
app.check_updates(True);  assert shown[-1] == ("update", "9.9.9")  # manual ignores the skip
da.latest_release = lambda current: None
app.check_updates(True);  assert shown[-1] == ("dialog", "Up to date")
def boom(current): raise da.ApiError("GitHub rate limit, try again later")
da.latest_release = boom
app.check_updates(True);  assert shown[-1] == ("dialog", "Update check failed")
app.cfg["check_updates"] = False; s = len(shown); assert app._check_updates_auto() is True and len(shown) == s
print("GLUE OK")
EOF
```
Expected: `GLUE OK`

- [ ] **Step 3: Commit**

```bash
git add debrid_applet.py
git commit -m "Check for updates: daily check, tray and About entry points, dialogs and notification

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Install and restart

**Files:**
- Modify: `debrid_applet.py` (`App.update_to` stub from Task 5; `App.__init__` near `self._polling = False`; `main()` near line 2524)

**Interfaces:**
- Consumes: `download(asset, dest_dir)`, `install_user(tar_path, here)`, `deb_command(path)`, `install_mode(here)`; `App._dialog`, `App._ask`; `copy_text`, `run_async`, `threading`, `subprocess`, `shlex`, `GLib.idle_add`, `DATA_DIR`, `HERE`.
- Produces: `App.update_to(release)`, `App.restart()`, `App.restart_requested` (bool), re-exec in `main()`.

- [ ] **Step 1: Replace the stub**

In `debrid_applet.py`, replace:

```python
    def update_to(self, release):
        """Replaced in the next task."""
        self._dialog("Not yet", "Self-update arrives in the next commit.")
```
with
```python
    def update_to(self, release):
        """Download the asset for this kind of install and install it; the user confirms the restart.
        .deb: pkexec + apt-get (one password prompt). Per-user copy: files replaced in place. Git checkout: told to pull."""
        v = ".".join(map(str, release.version))
        mode = install_mode(HERE)
        tab = self.current_tab()
        if mode == "git":
            self._dialog(f"Debrid Applet {v} is available", f"This is a git checkout: run git pull in {HERE}.")
            return
        asset = release.deb if mode == "deb" else release.tar
        if asset is None:
            self._dialog("No package in this release", f"Release {v} has no {'.deb' if mode == 'deb' else 'tarball'} for this install.")
            return
        updates = os.path.join(DATA_DIR, "updates")
        tab.say(f"Downloading update {v}…")

        def installed(err=None, path=None):
            if err:
                tab.say(f"Update failed: {err}", error=True)
                self._dialog("Update failed", err)
                return
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            tab.say(f"Debrid Applet {v} installed")
            notify(f"Debrid Applet {v} installed", "Restart to use it.")
            if self._ask(f"Debrid Applet {v} is installed. Restart now?", "Restart"):
                self.restart()

        def after_download(path, err):
            if err:
                installed(err)
                return
            if mode == "user":
                run_async(lambda: install_user(path, HERE), lambda _r, e: installed(e, path))
                return
            tab.say(f"Installing update {v}… (password prompt)")
            log_path = os.path.join(updates, "apt.log")
            try:
                with open(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab") as log:
                    proc = subprocess.Popen(deb_command(path), stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            except OSError as e:
                installed(f"cannot run pkexec: {e.strerror}")
                return

            def finished(code):
                if code == 0:
                    installed(None, path)
                elif code in (126, 127):  # pkexec: dismissed or not authorised
                    tab.say("Update cancelled: authentication did not complete", error=True)
                else:
                    tab.say(f"Update failed: apt-get exited {code}, see {log_path}", error=True)
                    if self._ask(f"apt-get exited with code {code} (details in {log_path}).\n"
                                 "Copy the command to install it yourself?", "Copy sudo command"):
                        copy_text(f"sudo apt install {shlex.quote(path)}")
            threading.Thread(target=lambda: GLib.idle_add(finished, proc.wait()), daemon=True).start()
        run_async(lambda: download(asset, updates), after_download)

    def restart(self):
        """Quit and let main() exec this file again: same PID, the bus name is freed by the exec, so the new
        process becomes the primary instance instead of being forwarded to a dying one."""
        self.hide_window()
        self.restart_requested = True
        self.gapp.quit()
```

In `App.__init__`, change:

```python
        self._polling = False  # a poll is in flight; refresh() then only asks for another one afterwards
```
to
```python
        self._polling = False  # a poll is in flight; refresh() then only asks for another one afterwards
        self.restart_requested = False  # set by restart(); main() re-execs this file after the main loop ends
```

In `main()`, change:

```python
    gapp.connect("command-line", on_cmdline)
    gapp.run(sys.argv)
```
to
```python
    gapp.connect("command-line", on_cmdline)
    gapp.run(sys.argv)
    if "app" in state and state["app"].restart_requested:  # an update was installed: become the new version
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])
```

- [ ] **Step 2: Run the suite and an offscreen check of `update_to`**

Run: `python3 test_debrid_api.py 2>&1 | tail -1`
Expected: `76 passed`

Scratch script (the real `pkexec` is replaced by `/bin/true`, so no prompt appears; per-user mode is exercised against a temp copy):

```bash
DEBRID_APPLET_CONFIG_DIR=/tmp/debrid-smoke-cfg DEBRID_APPLET_DATA_DIR=/tmp/debrid-smoke-data python3 - <<'EOF'
import os, shutil, sys, time
sys.path.insert(0, "/home/borislav/github/debrid-applet")
import debrid_applet as da
import debrid_update as du
from gi.repository import GLib
said, dialogs, asked = [], [], []
class Tab:
    def say(self, m, error=False): said.append((m, error))
app = da.App.__new__(da.App); app.cfg = {**da.EMPTY_CONFIG}; app.window = None; app.restart_requested = False
app.current_tab = lambda: Tab()
da.App._dialog = lambda self, title, text: dialogs.append((title, text))
da.App._ask = lambda self, text, yes: asked.append(text) or False   # answer "Not now"
da.notify = lambda *a, **k: None
def sync_run_async(fn, on_done):  # run the worker inline; deliver ApiError like the real one does
    try:
        on_done(fn(), None)
    except da.ApiError as e:
        on_done(None, str(e))
da.run_async = sync_run_async
da.download = lambda asset, dest: "/tmp/debrid-smoke-data/updates/fake.deb"
da.deb_command = lambda path: ["/bin/true", path]
os.makedirs("/tmp/debrid-smoke-data/updates", exist_ok=True); open("/tmp/debrid-smoke-data/updates/fake.deb", "w").close()
rel = du.Release(version=(9, 9, 9), tag="v9.9.9", notes_url="https://x",
                 deb=du.Asset("fake.deb", "https://x", 1, "sha256:" + "0" * 64), tar=None)
da.install_mode = lambda here: "deb"
app.update_to(rel)
loop = GLib.MainLoop(); GLib.timeout_add(500, loop.quit); loop.run()   # let the pkexec wait-thread report back
assert asked and "installed. Restart now?" in asked[-1] and not os.path.exists("/tmp/debrid-smoke-data/updates/fake.deb")
assert any("Installing update 9.9.9" in m for m, _ in said)
da.deb_command = lambda path: ["/bin/sh", "-c", "exit 126"]
open("/tmp/debrid-smoke-data/updates/fake.deb", "w").close(); app.update_to(rel)
loop = GLib.MainLoop(); GLib.timeout_add(500, loop.quit); loop.run()
assert said[-1] == ("Update cancelled: authentication did not complete", True)
da.deb_command = lambda path: ["/bin/sh", "-c", "exit 100"]
app.update_to(rel); loop = GLib.MainLoop(); GLib.timeout_add(500, loop.quit); loop.run()
assert "apt-get exited 100" in said[-1][0] and "Copy the command" in asked[-1]
da.install_mode = lambda here: "git"; app.update_to(rel); assert "git pull" in dialogs[-1][1]
da.install_mode = lambda here: "user"; app.update_to(rel); assert dialogs[-1][0] == "No package in this release"
print("UPDATE_TO OK")
EOF
```
Expected: `UPDATE_TO OK`

- [ ] **Step 3: Commit**

```bash
git add debrid_applet.py
git commit -m "Self-update: download, pkexec apt-get or per-user copy, restart by re-exec

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Docs, and the end-to-end check on this machine

**Files:**
- Modify: `docs/MANUAL.md` (Integration list near line 74; Settings list line ~131), `README.md` (Everyday use list, before the "Everything else" paragraph near line 43), `docs/DEVELOPING.md` (Repository layout line ~17; Releases section line ~35)

- [ ] **Step 1: Manual**

In `docs/MANUAL.md`, after the `- **Command line.**` bullet's last line (`… for a global hotkey.`), add:

```markdown
- **Updates.** Once a day (and 60 s after start) the applet asks GitHub for the latest release and, when it is newer,
  shows a notification with Update / Release notes / Skip this version. Right-click the tray icon → **Check for
  updates…** (or the button in About) checks at once. Update downloads the release, verifies it against the checksum
  GitHub publishes, and installs it: the `.deb` through `apt-get` behind one password prompt, a `./install.sh` copy by
  replacing its files, a git checkout only tells you to `git pull`. Then it offers to restart. Turn the daily check
  off under Settings → Advanced. Nothing is ever installed without your click; failed installs leave the file in
  `~/.local/share/debrid-applet/updates/` and offer the `sudo apt install` line to copy.
```

Change:
```markdown
- **Advanced**: local API port and token; activity log level and size cap.
```
to
```markdown
- **Advanced**: local API port and token; daily update check; activity log level and size cap.
```

- [ ] **Step 2: README**

In `README.md`, before the paragraph starting `Everything else, from watch folders`, add a bullet to the Everyday use list:

```markdown
- **Updates** announce themselves: a notification offers to download, verify and install a new release (one password
  prompt), then restart. Or right-click the tray icon → Check for updates…
```

- [ ] **Step 3: DEVELOPING**

In `docs/DEVELOPING.md` Repository layout, after the `debrid_local_api.py` line add:

```markdown
- `debrid_update.py`: update check and self-update (GitHub releases API, verified download, apt / per-user install).
```

At the end of the Releases section add:

```markdown
The applet's update check reads `releases/latest`: keep the tag `vX.Y.Z` and the asset names
`debrid-applet_X.Y.Z_all.deb` and `debrid-applet-X.Y.Z.tar.gz` (both produced by the workflow); GitHub computes the
`digest` the applet verifies. Drafts and prereleases are invisible to it. A per-user install updates from the tarball,
so `debrid_update.FILES` and the copy list in `install.sh` must stay the same five files.
```

- [ ] **Step 4: Commit the docs**

```bash
git add docs/MANUAL.md README.md docs/DEVELOPING.md
git commit -m "Docs: update check and self-update

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

- [ ] **Step 5: End-to-end on this machine (manual, after the release)**

1. Bump `VERSION` to `1.0.4`, update the README install line, run `packaging/build-deb.sh`, commit "Release 1.0.4", tag `v1.0.4`, push branch and tag, wait for the release workflow (`gh run watch`).
2. Confirm anonymously: `curl -s https://api.github.com/repos/xb00tt/debrid-applet/releases/latest | grep -E '"tag_name"|"digest"'` shows `v1.0.4` and two digests.
3. The installed applet is still 1.0.3 but has no updater; install the locally built 1.0.4 `.deb` once by hand (`sudo apt install ./dist/debrid-applet_1.0.4_all.deb`), restart it. From then on: tray → Check for updates… must say "Up to date".
4. To exercise the whole flow, temporarily set `VERSION = "1.0.0"` in `/usr/share/debrid-applet/debrid_applet.py` (as root), restart the applet, Check for updates… → dialog "1.0.4 available" → Update → polkit prompt → "installed, restart now?" → Restart. `debrid-applet --version` prints 1.0.4 and `~/.local/share/debrid-applet/activity.log` shows "Update check: 1.0.4 available" and "Debrid Applet 1.0.4 started".
5. Cancel path: repeat step 4 but press Cancel on the polkit prompt: status line "Update cancelled: authentication did not complete", file left in `~/.local/share/debrid-applet/updates/`.
