#!/bin/sh
# Install debrid-applet for the current user and enable autostart. Idempotent.
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DIR="$HOME/.local/share/debrid-applet"
APPS="$HOME/.local/share/applications"
AUTO="$HOME/.config/autostart"
mkdir -p "$DIR" "$APPS" "$AUTO"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
META="$HOME/.local/share/metainfo"
mkdir -p "$ICONS" "$META"
cp "$SRC/debrid_api.py" "$SRC/debrid_local_api.py" "$SRC/debrid_update.py" "$SRC/debrid_applet.py" "$SRC/debrid-applet.svg" "$DIR/"
cp "$SRC/debrid-applet.svg" "$ICONS/debrid-applet.svg"
cp "$SRC/io.github.xb00tt.debrid_applet.metainfo.xml" "$META/"
gtk-update-icon-cache -q "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
sed "s|@EXEC@|python3 $DIR/debrid_applet.py|" "$SRC/debrid-applet.desktop" > "$APPS/debrid-applet.desktop"
cp "$APPS/debrid-applet.desktop" "$AUTO/debrid-applet.desktop"
echo "Installed to $DIR. Autostarts on next login. Start now:"
echo "  python3 $DIR/debrid_applet.py &"
