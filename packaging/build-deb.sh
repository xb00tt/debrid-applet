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
cat > "$STAGE/usr/bin/$PKG" <<LAUNCHER
#!/bin/sh
# Imports the module instead of running the file: a script named on the command line is never bytecode-cached, an
# imported module is, and postinst compiles the cache once (the directory is root-owned, so Python could not write it
# at run time). -P keeps the working directory off the import path, as running the file did.
exec python3 -P -c 'import sys; sys.path.insert(0, "/usr/share/$PKG"); import debrid_applet; debrid_applet.main()' "\$@"
LAUNCHER
chmod 755 "$STAGE/usr/bin/$PKG"
cat > "$STAGE/DEBIAN/postinst" <<POSTINST
#!/bin/sh
# Bytecode next to the sources: one compile per install instead of one per start. After a Python upgrade the cache is
# stale until the package is reinstalled; Python then compiles at start, as before, and nothing breaks.
set -e
python3 -m compileall -q /usr/share/$PKG
POSTINST
cat > "$STAGE/DEBIAN/prerm" <<PRERM
#!/bin/sh
rm -rf /usr/share/$PKG/__pycache__
PRERM
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm"
sed "s|@EXEC@|$PKG|" "$SRC/debrid-applet.desktop" > "$STAGE/usr/share/applications/$PKG.desktop"
cp "$STAGE/usr/share/applications/$PKG.desktop" "$STAGE/etc/xdg/autostart/$PKG.desktop"
cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: $PKG
Version: $VERSION
Section: net
Priority: optional
Architecture: all
Depends: python3 (>= 3.11), python3-gi, gir1.2-gtk-3.0, gir1.2-notify-0.7, python3-requests, python3-defusedxml
Recommends: gir1.2-ayatanaappindicator3-0.1, gir1.2-xapp-1.0, mpv | vlc | celluloid, aria2
Maintainer: xb00tt <borislav.bg@gmail.com>
Homepage: https://github.com/xb00tt/debrid-applet
Description: Tray control panel for Torbox, Real-Debrid and AllDebrid
 Shows account status and every torrent with live progress, adds magnets,
 .torrent files and hoster links, streams or downloads the results, and
 automates the rest: watch folder, RSS feeds, auto-download, local API.
 Tray icon on Cinnamon, KDE, XFCE, MATE and GNOME (with the AppIndicator
 extension); works as a plain window elsewhere.
CONTROL
find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE/usr" -type f ! -path "*/bin/*" -exec chmod 644 {} +
mkdir -p "$SRC/dist"
dpkg-deb --root-owner-group --build "$STAGE" "$SRC/dist/${PKG}_${VERSION}_all.deb" >/dev/null
echo "$SRC/dist/${PKG}_${VERSION}_all.deb"
