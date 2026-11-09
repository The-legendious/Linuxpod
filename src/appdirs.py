"""
appdirs.py — where Linuxpod keeps its library, settings and logs.

The app used to be called Music Manager and kept everything in
~/.local/share/ipod-music-manager. Importing this module moves that
folder to ~/.local/share/linuxpod the first time, and leaves a link
at the old name so any saved file paths that point inside it (cover
and video thumbnails, imported firmware) still work.
"""

import os

DATA_DIR = os.path.expanduser("~/.local/share/linuxpod")
OLD_DATA_DIR = os.path.expanduser("~/.local/share/ipod-music-manager")

MIGRATION_NOTE = ""


def migrate_old_data_dir():
    global MIGRATION_NOTE
    try:
        if os.path.islink(OLD_DATA_DIR) or not os.path.isdir(OLD_DATA_DIR):
            return
        if os.path.exists(DATA_DIR):
            MIGRATION_NOTE = (
                f"both {OLD_DATA_DIR} and {DATA_DIR} exist; using {DATA_DIR} "
                "and leaving the old folder alone"
            )
            return
        os.rename(OLD_DATA_DIR, DATA_DIR)
        os.symlink(DATA_DIR, OLD_DATA_DIR)
        MIGRATION_NOTE = f"moved {OLD_DATA_DIR} to {DATA_DIR} (old name kept as a link)"
    except OSError as e:
        MIGRATION_NOTE = f"couldn't move {OLD_DATA_DIR} to {DATA_DIR}: {e}"


migrate_old_data_dir()
