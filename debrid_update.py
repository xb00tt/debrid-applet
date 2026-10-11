"""Update check and self-update helpers. No GTK here; safe to import anywhere.

The latest GitHub release is the source of truth. Assets are fetched through their API URL and verified against the
digest GitHub publishes in the same JSON. Nothing here needs credentials."""
import hashlib
import os
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass

import requests

from debrid_api import ApiError

REPO = "xb00tt/debrid-applet"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
FILES = ("debrid_api.py", "debrid_local_api.py", "debrid_update.py", "debrid_applet.py", "debrid-applet.svg")  # what install.sh copies
TIMEOUT = 15
# Releases are signed with OpenSSH signatures (ssh-keygen -Y sign); any of these public keys may have signed one.
# Rotation: ship the new key here in a release signed by the old one, then sign with the new one.
SIGNING_KEYS = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBqWO2K0QoJHnjZ3kxzI+XVafzggQGw/WEnYWh64xjzZ",)
SIGN_PRINCIPAL = "debrid-applet"  # the identity in the allowed-signers list


def sign_namespace(version):
    """Namespace a release's signatures are made in: it names the version, so a signed asset of an older release
    cannot be served as a newer one (the signature would be for the wrong namespace)."""
    return f"debrid-applet-{version}"


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
    deb_sig: Asset | None = None  # the .sig files published next to the assets
    tar_sig: Asset | None = None


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
    pick = lambda suffix: next((a for a in assets if a.name.endswith(suffix)), None)
    return Release(version=version, tag=str(body.get("tag_name")),
                   notes_url=str(body.get("html_url") or f"https://github.com/{REPO}/releases"),
                   deb=pick("_all.deb"), tar=pick(".tar.gz"), deb_sig=pick("_all.deb.sig"), tar_sig=pick(".tar.gz.sig"))


def verify_signature(path, sig_path, version, keys=SIGNING_KEYS, tool="ssh-keygen"):
    """ssh-keygen -Y verify of path against sig_path with the project keys, in the namespace of version; ApiError when
    it does not verify. Runs before anything is installed, on the very file the install then uses."""
    with tempfile.NamedTemporaryFile("w", suffix=".allowed_signers", delete=False) as allowed:
        allowed.write("".join(f"{SIGN_PRINCIPAL} {k}\n" for k in keys))
    try:
        with open(path, "rb") as data:
            result = subprocess.run([tool, "-Y", "verify", "-f", allowed.name, "-I", SIGN_PRINCIPAL, "-n", sign_namespace(version),
                                     "-s", sig_path], stdin=data, capture_output=True, text=True)
    except OSError as e:
        raise ApiError(f"Cannot verify the signature: {e.strerror}") from e
    finally:
        _unlink(allowed.name)
    if result.returncode != 0:
        raise ApiError("Release is not signed by the project key; not installing")


def install_mode(here):
    """'deb' (the package, under /usr), 'git' (a checkout: update with git pull) or 'user' (an install.sh copy)."""
    if here.startswith("/usr/"):
        return "deb"
    if os.path.exists(os.path.join(here, ".git")):  # a directory, or a file in a git worktree
        return "git"
    return "user"


DEB_SCRIPT = """set -e
t=$(mktemp --suffix=.deb)
trap 'rm -f "$t"' EXIT
cp "$1" "$t"
echo "$2  $t" | sha256sum -c --quiet
apt-get install -y --no-install-recommends "$t"
"""


def deb_command(path, digest):
    """argv that, behind one polkit password prompt, copies the .deb to a root-owned temp file, checks that copy's
    sha256 and installs it. Verifying as root closes the window in which the user-owned file could be swapped
    after download() checked it. The script is a constant; path and digest are positional parameters."""
    return ["pkexec", "env", "DEBIAN_FRONTEND=noninteractive", "sh", "-c", DEB_SCRIPT, "sh", path, digest.removeprefix("sha256:")]


def download(asset, dest_dir, session=None):
    """Fetch asset into dest_dir (file 0600) and verify its sha256 against the release digest; the path on success.
    Refuses without a digest, stops at twice the declared size, leaves nothing behind on failure."""
    if not asset.digest.startswith("sha256:"):
        raise ApiError(f"Release has no checksum for {asset.name}; not installing")
    name = os.path.basename(asset.name)  # the name comes from the release JSON: never a path
    if not name:
        raise ApiError("Release asset has no usable name")
    os.makedirs(dest_dir, mode=0o700, exist_ok=True)
    path = os.path.join(dest_dir, name)
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


def install_user(tar_path, here):
    """Replace the program files in here with the copies inside the release tarball: every regular member directly
    under the archive prefix whose name ends in .py or .svg (tests excluded), so a module the new version adds
    travels with it; FILES is the required minimum. All files are staged as <name>.tmp first, then renamed, so a
    failure leaves the installed version untouched. Members are read by name; nothing else is extracted."""
    with tarfile.open(tar_path, "r:gz") as tar:
        members = {m.name.split("/", 1)[1]: m for m in tar.getmembers()
                   if m.isfile() and m.name.count("/") == 1 and m.name.endswith((".py", ".svg"))
                   and not m.name.split("/", 1)[1].startswith("test_")}
        missing = [n for n in FILES if n not in members]
        if missing:
            raise ApiError(f"Release tarball lacks {', '.join(missing)}")
        staged = []
        try:
            for name, member in members.items():
                tmp = os.path.join(here, name + ".tmp")
                with open(tmp, "wb") as f:
                    f.write(tar.extractfile(member).read())
                staged.append((tmp, os.path.join(here, name)))
        except OSError as e:
            for tmp, _dst in staged:
                _unlink(tmp)
            raise ApiError(f"Cannot write {name}: {e.strerror}") from e
    for tmp, dst in staged:  # renames inside one directory that just accepted every write
        os.replace(tmp, dst)
