"""
scanner.py — walks a folder tree, reads audio tags with mutagen, and
upserts everything it finds into the local Library.

iPod click-wheel models only understand a handful of audio formats
(MP3, AAC/M4A, and uncompressed WAV/AIFF). We scan more broadly than
that so your library view can hold everything, but ipod_backend flags
anything that isn't iPod-playable when you try to sync it.
"""

import os

try:
    import mutagen
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4
    from mutagen.flac import FLAC
    from mutagen.oggvorbis import OggVorbis
    from mutagen.wave import WAVE
    from mutagen.aiff import AIFF
    HAVE_MUTAGEN = True
except ImportError:
    HAVE_MUTAGEN = False

try:
    from mutagen.mp3 import BitrateMode
except ImportError:
    # Older mutagen versions don't have this — VBR detection just won't
    # run, tag reading otherwise works fine either way.
    BitrateMode = None

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".m4b", ".aac", ".wav", ".aiff", ".aif",
                     ".flac", ".ogg"}

# Formats an unmodified classic iPod can actually play.
IPOD_PLAYABLE_EXTENSIONS = {".mp3", ".m4a", ".m4b", ".aac", ".wav", ".aiff", ".aif"}


try:
    import tagwriter
    tagwriter.register_easy_mp4_keys()  # lets M4A composer tags be read
except Exception:
    pass


def _first(tag_value):
    if isinstance(tag_value, list):
        return tag_value[0] if tag_value else None
    return tag_value


def _to_int(value):
    if value is None:
        return None
    try:
        # mutagen sometimes returns "3/12" style track numbers
        s = str(value).split("/")[0].strip()
        return int(s) if s else None
    except (ValueError, TypeError):
        return None


def _total(value):
    """The "12" of a "3/12" track or disc number, if present."""
    if value is None or "/" not in str(value):
        return None
    return _to_int(str(value).split("/", 1)[1])


def read_tags(filepath: str) -> dict:
    """Best-effort tag extraction. Never raises; falls back to filename."""
    filepath = os.path.realpath(filepath)  # see scan_folder's comment on why
    meta = {
        "filepath": filepath,
        "title": None,
        "artist": None,
        "album": None,
        "albumartist": None,
        "genre": None,
        "composer": None,
        "track_nr": None,
        "track_total": None,
        "cd_nr": None,
        "cd_total": None,
        "year": None,
        "duration_ms": None,
        "bitrate": None,
        "samplerate": None,
        "size_bytes": os.path.getsize(filepath) if os.path.exists(filepath) else 0,
        "filetype": os.path.splitext(filepath)[1].lstrip(".").lower(),
        "is_vbr": False,
    }

    if not HAVE_MUTAGEN:
        meta["title"] = os.path.splitext(os.path.basename(filepath))[0]
        return meta

    try:
        f = mutagen.File(filepath, easy=True)
    except Exception:
        f = None

    if f is not None:
        tags = f.tags or {}
        meta["title"] = _first(tags.get("title"))
        meta["artist"] = _first(tags.get("artist"))
        meta["album"] = _first(tags.get("album"))
        meta["albumartist"] = _first(tags.get("albumartist"))
        meta["genre"] = _first(tags.get("genre"))
        meta["composer"] = _first(tags.get("composer"))
        meta["track_nr"] = _to_int(_first(tags.get("tracknumber")))
        meta["track_total"] = _total(_first(tags.get("tracknumber")))
        meta["cd_nr"] = _to_int(_first(tags.get("discnumber")))
        meta["cd_total"] = _total(_first(tags.get("discnumber")))
        date = _first(tags.get("date")) or _first(tags.get("originaldate"))
        meta["year"] = _to_int(date[:4]) if date else None
        if f.info is not None:
            meta["duration_ms"] = int(getattr(f.info, "length", 0) * 1000)
            meta["bitrate"] = int(getattr(f.info, "bitrate", 0) / 1000) if getattr(f.info, "bitrate", None) else None
            meta["samplerate"] = getattr(f.info, "sample_rate", None)
            bitrate_mode = getattr(f.info, "bitrate_mode", None)
            if BitrateMode is not None and bitrate_mode is not None:
                # ABR is variable-ish enough that it needs the same
                # handling as VBR for the iPod's seek table to be right.
                meta["is_vbr"] = bitrate_mode in (BitrateMode.VBR, BitrateMode.ABR)

    if not meta["title"]:
        meta["title"] = os.path.splitext(os.path.basename(filepath))[0]

    return meta


def scan_folder(folder: str, library, progress_callback=None):
    """
    Walk `folder` recursively, tag every audio file found, and upsert
    into `library`. progress_callback(current, total, filepath) is called
    after each file if provided.

    Returns (count, found_total, errors):
      count       -- tracks successfully added/updated
      found_total -- audio files found by extension, before tagging
      errors      -- list of (filepath, message) for files that failed;
                     scanning continues past individual failures, but
                     these are returned instead of swallowed so the
                     caller can show what actually went wrong.
    """
    if not os.path.isdir(folder):
        return 0, 0, [(folder, "not a folder, or not accessible")]

    found = []
    walk_errors = []
    for root, _dirs, files in os.walk(folder, onerror=lambda e: walk_errors.append(str(e))):
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext in AUDIO_EXTENSIONS:
                # realpath, not just the path we walked: the same file can
                # otherwise get two different path strings (a symlinked
                # folder, a subfolder reachable both directly and via a
                # separately-added parent, etc.), which our dedup — a plain
                # string match on filepath — would then treat as two
                # different tracks and show as a phantom duplicate.
                found.append(os.path.realpath(os.path.join(root, name)))
    found = sorted(set(found))  # os.path.realpath can make two walked paths coincide

    # Songs removed with Remove from Library stay removed.
    if hasattr(library, "removed_track_files"):
        skipped = library.removed_track_files()
        found = [p for p in found if p not in skipped]

    total = len(found)
    count = 0
    errors = [(folder, e) for e in walk_errors]
    for i, path in enumerate(found, start=1):
        try:
            meta = read_tags(path)
            library.upsert_track(meta)
            count += 1
        except Exception as e:
            errors.append((path, f"{type(e).__name__}: {e}"))
        if progress_callback:
            progress_callback(i, total, path)

    return count, total, errors
