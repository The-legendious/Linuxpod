#!/usr/bin/env bash
# Unzips the linuxpod.zip you downloaded, installs its dependencies,
# (re)builds the iPod helper, and launches the app - all in one go.
# Meant to be run again each time you get a fresh zip: it overwrites
# the previous install in place.
set -e

DOWNLOADS="$HOME/Downloads"
ZIP_PATH="$DOWNLOADS/linuxpod.zip"
INSTALL_DIR="$DOWNLOADS/linuxpod"

# Older versions were called Music Manager: accept the old zip name,
# and move an old install folder to the new name.
if [ ! -f "$ZIP_PATH" ] && [ -f "$DOWNLOADS/ipod-music-manager.zip" ]; then
    ZIP_PATH="$DOWNLOADS/ipod-music-manager.zip"
fi
if [ ! -e "$INSTALL_DIR" ] && [ -d "$DOWNLOADS/ipod-music-manager" ]; then
    mv "$DOWNLOADS/ipod-music-manager" "$INSTALL_DIR"
    echo "Moved $DOWNLOADS/ipod-music-manager to $INSTALL_DIR"
fi

if [ ! -f "$ZIP_PATH" ]; then
    echo "Couldn't find $ZIP_PATH"
    echo "Save the zip to your Downloads folder as linuxpod.zip and run this again."
    exit 1
fi

echo "== Unzipping =="
unzip -o "$ZIP_PATH" -d "$INSTALL_DIR"

echo ""
echo "== Installing dependencies =="
chmod +x "$INSTALL_DIR/install.sh"
"$INSTALL_DIR/install.sh"

echo ""
echo "== Building the iPod helper =="
make -C "$INSTALL_DIR/helper"

echo ""
echo "== Launching Linuxpod =="
python3 "$INSTALL_DIR/src/main.py"
