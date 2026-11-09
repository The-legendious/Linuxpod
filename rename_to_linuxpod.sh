#!/usr/bin/env bash
# Renames Linuxpod's old "Music Manager" folders to their new names:
#   ~/Downloads/ipod-music-manager       -> ~/Downloads/linuxpod
#   ~/Downloads/ipod-music-manager.zip   -> ~/Downloads/linuxpod.zip
#   ~/.local/share/ipod-music-manager    -> ~/.local/share/linuxpod
# The library folder keeps a link at its old name, so saved paths to
# thumbnails and firmware inside it still work. Nothing is deleted.
# If linuxpod.zip is in Downloads, it's then unzipped into the new
# install folder, so its new setup_and_run.sh is there.
# Safe to run more than once.
set -u

DOWNLOADS="$HOME/Downloads"
DATA="$HOME/.local/share"

if pgrep -f "(music-manager|linuxpod)/src/main\.py" >/dev/null 2>&1; then
    echo "Linuxpod (or Music Manager) is running. Quit it first, then run this again."
    exit 1
fi

status=0

move() {  # move OLD NEW LABEL
    local old="$1" new="$2" label="$3"
    if [ -L "$old" ] || [ ! -e "$old" ]; then
        if [ -e "$new" ]; then
            echo "  $label: already $new"
        else
            echo "  $label: nothing to rename (no $old)"
        fi
        return 0
    fi
    if [ -e "$new" ]; then
        echo "  $label: both $old and $new exist - leaving both alone. Move what you need by hand."
        status=1
        return 0
    fi
    if mv "$old" "$new"; then
        echo "  $label: $old -> $new"
    else
        echo "  $label: couldn't rename $old"
        status=1
    fi
}

echo "== Renaming Music Manager folders to Linuxpod =="
move "$DOWNLOADS/ipod-music-manager" "$DOWNLOADS/linuxpod" "Install folder"
if [ -f "$DOWNLOADS/linuxpod.zip" ] && [ -f "$DOWNLOADS/ipod-music-manager.zip" ]; then
    echo "  Zip: using the new linuxpod.zip (the old ipod-music-manager.zip can be deleted)"
else
    move "$DOWNLOADS/ipod-music-manager.zip" "$DOWNLOADS/linuxpod.zip" "Zip"
fi
move "$DATA/ipod-music-manager" "$DATA/linuxpod" "Library and settings"
if [ -d "$DATA/linuxpod" ] && [ ! -e "$DATA/ipod-music-manager" ]; then
    ln -s "$DATA/linuxpod" "$DATA/ipod-music-manager" && \
        echo "  Left a link at $DATA/ipod-music-manager for saved paths"
fi

if [ -f "$DOWNLOADS/linuxpod.zip" ] && command -v unzip >/dev/null; then
    echo ""
    echo "== Unzipping linuxpod.zip into $DOWNLOADS/linuxpod =="
    unzip -oq "$DOWNLOADS/linuxpod.zip" -d "$DOWNLOADS/linuxpod" && echo "  Done"
fi

echo ""
if [ "$status" = 0 ]; then
    echo "All done. From now on, run: $DOWNLOADS/linuxpod/setup_and_run.sh"
else
    echo "Finished with problems (see above)."
fi
exit $status
