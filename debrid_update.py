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
