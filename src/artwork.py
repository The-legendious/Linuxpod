"""
artwork.py — pulls cover art for a track so it can be handed to libgpod
(which wants a path to an image file, via itdb_track_set_thumbnails).

Two sources, tried in order:
1. Embedded tags: ID3 APIC frames (MP3, and WAV/AIFF when they carry
   ID3 tags) and the MP4 'covr' atom (M4A/M4B/AAC). Plain AAC (ADTS, no
   MP4 container) has no tag mechanism for art at all.
2. A folder-level image file (cover.jpg, folder.jpg, album.jpg, front.jpg,
   albumart.jpg — any of those, .jpg/.jpeg/.png, case-insensitive) in the
   same directory as the track. This is how a lot of ripped/downloaded
   music collections store art instead of embedding it per-file, so it's
   checked whenever a track has no embedded art of its own.
"""

import io
import os
import tempfile

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

FOLDER_ART_NAMES = (
    "cover", "folder", "album", "front", "albumart", "albumartsmall", "art",
)
FOLDER_ART_EXTS = (".jpg", ".jpeg", ".png")

_folder_art_cache = {}  # directory path -> resolved art filepath or None


# ID3 APIC picture type 3 = "Cover (front)" per the ID3v2 spec — this is
# what iTunes itself displays/syncs when a file embeds more than one
# picture (front cover, back cover, artist photo, etc.).
try:
    from mutagen.id3 import PictureType
    _COVER_FRONT = PictureType.COVER_FRONT
except ImportError:
    _COVER_FRONT = 3


def _from_id3(filepath):
    try:
        from mutagen.id3 import ID3
    except ImportError:
        return None
    try:
        id3 = ID3(filepath)
    except Exception:
        return None
    apics = id3.getall("APIC")
    if not apics:
        return None
    for apic in apics:
        if getattr(apic, "type", None) == _COVER_FRONT:
            return apic.data
    return apics[0].data  # no explicit front cover tagged - best guess


def _from_mp4(filepath):
    try:
        from mutagen.mp4 import MP4
    except ImportError:
        return None
    try:
        mp4 = MP4(filepath)
    except Exception:
        return None
    if not mp4.tags:
        return None
    covr = mp4.tags.get("covr")
    if not covr:
        return None
    return bytes(covr[0])


def _find_folder_art(filepath):
    """Looks for a cover/folder/album/front image in the track's own
    directory. Cached per-directory since every track in an album folder
    asks the same question."""
    directory = os.path.dirname(filepath) or "."
    if directory in _folder_art_cache:
        return _folder_art_cache[directory]

    found = None
    try:
        entries = os.listdir(directory)
    except OSError:
        entries = []

    lowered = {name.lower(): name for name in entries}
    for base in FOLDER_ART_NAMES:
        for ext in FOLDER_ART_EXTS:
            candidate = lowered.get(base + ext)
            if candidate:
                found = os.path.join(directory, candidate)
                break
        if found:
            break

    _folder_art_cache[directory] = found
    return found


def _from_folder(filepath):
    art_path = _find_folder_art(filepath)
    if not art_path:
        return None
    try:
        with open(art_path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _extract_art_bytes(filepath):
    """Raw cover-art bytes for a track (embedded first, then a
    folder-level image), or None if neither is found. Shared by both the
    sync path (writes a temp file for libgpod) and the grid view (decodes
    straight to a thumbnail via Pillow)."""
    ext = os.path.splitext(filepath)[1].lower()
    data = None
    if ext == ".mp3" or ext in (".wav", ".aiff", ".aif"):
        data = _from_id3(filepath)
    elif ext in (".m4a", ".m4b", ".aac"):
        data = _from_mp4(filepath)
    return data if data else _from_folder(filepath)


def has_any_art(filepath):
    """Cheap check (no decoding) for whether extraction would find
    something — used to report 'found art for X of Y tracks' up front."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".mp3" or ext in (".wav", ".aiff", ".aif"):
        if _from_id3(filepath):
            return True
    elif ext in (".m4a", ".m4b", ".aac"):
        if _from_mp4(filepath):
            return True
    return _find_folder_art(filepath) is not None


def extract_artwork_to_tempfile(filepath):
    """
    Returns a path to a temp image file containing the track's cover art
    (embedded or folder-level), or None if there isn't any (or it can't
    be read). Caller is responsible for deleting the returned file when
    done with it.
    """
    data = _extract_art_bytes(filepath)
    if not data:
        return None

    suffix = ".png" if data[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"
    fd, path = tempfile.mkstemp(prefix="ipod_art_", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except OSError:
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    return path


def get_cover_image(filepath, max_size=(120, 120)):
    """
    Returns a PIL Image (RGB, downsized to fit max_size) of a track's
    cover art (embedded or folder-level), for the album grid view — or
    None if there's no art, the file can't be read, or Pillow isn't
    installed. Pillow reads JPEG (the usual format) where Tkinter's own
    PhotoImage can't.
    """
    if not HAVE_PIL:
        return None
    data = _extract_art_bytes(filepath)
    if not data:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
        img.thumbnail(max_size)
        return img
    except Exception:
        return None
