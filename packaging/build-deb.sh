#!/bin/sh
# Build debrid-applet_<version>_all.deb into dist/. Needs dpkg-deb (any Debian/Ubuntu/Mint box).
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(python3 -c "import re;print(re.search(r'VERSION = \"([^\"]+)\"', open('$SRC/debrid_applet.py').read()).group(1))")"
PKG="debrid-applet"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
APP="$STAGE/usr/share/$PKG"
mkdir -p "$APP" "$STAGE/usr/bin" "$STAGE/usr/share/applications" "$STAGE/etc/xdg/autostart" \
         "$STAGE/usr/share/icons/hicolor/scalable/apps" "$STAGE/usr/share/doc/$PKG" "$STAGE/DEBIAN"
cp "$SRC/debrid_api.py" "$SRC/debrid_local_api.py" "$SRC/debrid_update.py" "$SRC/debrid_applet.py" "$SRC/debrid-applet.svg" "$APP/"
cp "$SRC/debrid-applet.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/"
cp "$SRC/README.md" "$SRC/LICENSE" "$STAGE/usr/share/doc/$PKG/"
printf '#!/bin/sh\nexec python3 /usr/share/%s/debrid_applet.py "$@"\n' "$PKG" > "$STAGE/usr/bin/$PKG"
chmod 755 "$STAGE/usr/bin/$PKG"
sed "s|@EXEC@|$PKG|" "$SRC/debrid-applet.desktop" > "$STAGE/usr/share/applications/$PKG.desktop"
cp "$STAGE/usr/share/applications/$PKG.desktop" "$STAGE/etc/xdg/autostart/$PKG.desktop"
cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: $PKG
Version: $VERSION
Section: net
Priority: optional
Architecture: all
Depends: python3 (>= 3.11), python3-gi, gir1.2-gtk-3.0, gir1.2-xapp-1.0, gir1.2-notify-0.7, python3-requests
Recommends: mpv | vlc | celluloid, aria2
Maintainer: Borislav <borislav.bg@gmail.com>
Homepage: https://github.com/xb00tt/debrid-applet
Description: Tray control panel for Torbox, Real-Debrid and AllDebrid
 Shows account status and every torrent with live progress, adds magnets,
 .torrent files and hoster links, streams or downloads the results, and
 automates the rest: watch folder, RSS feeds, auto-download, local API.
 Built for Linux Mint Cinnamon (XApp status icon).
CONTROL
find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE/usr" -type f ! -path "*/bin/*" -exec chmod 644 {} +
mkdir -p "$SRC/dist"
dpkg-deb --root-owner-group --build "$STAGE" "$SRC/dist/${PKG}_${VERSION}_all.deb" >/dev/null
echo "$SRC/dist/${PKG}_${VERSION}_all.deb"
