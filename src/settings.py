"""
settings.py — small persisted app preferences, backing the Settings
panel (currently just the iTunes-style theme toggle).
"""

import json
import os

import appdirs

SETTINGS_DIR = appdirs.DATA_DIR
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")

DEFAULTS = {
    "itunes_theme": True,
    "auto_sync": False,
    "sync_all_music": False,
    "converted_videos_folder": "",
    "thumbnail_scale": 1.0,   # multiplier applied to every grid's base tile/thumbnail size
    "podcasts_folder": "",    # where downloaded podcast episode audio files are saved
    "podcast_download_count": 5,  # how many of a show's most recent episodes to grab by default
}


def load():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(DEFAULTS)
        if isinstance(data, dict):
            merged.update(data)
        return merged
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save(values: dict):
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(values, f, indent=2)
    except OSError:
        pass
