"""
tagwriter.py — writes edited album metadata and cover art back to the
actual audio files.

This matters because scanner.scan_folder always re-reads tags fresh
from disk: if an edit only touched the local SQLite library and not the
file itself, the next Refresh Library would silently overwrite it right
back to whatever the file's own tags still say. Writing through to the
file is what makes an edit actually stick.

Cover art is embedded directly into each track's own tags (ID3 APIC for
MP3, the MP4 'covr' atom for M4A/M4B) — the same place embedded art is
read from — replacing any existing embedded picture rather than
accumulating duplicates across repeated edits.
"""

import os

SUPPORTED_TAG_WRITE_EXTENSIONS = {".mp3", ".m4a", ".m4b"}


# The Get Info editor goes through mutagen's "easy" interface, which
# also covers FLAC and Ogg Vorbis comments.
SUPPORTED_INFO_WRITE_EXTENSIONS = SUPPORTED_TAG_WRITE_EXTENSIONS | {".flac", ".ogg", ".opus"}


def can_write_tags(filepath):
    return os.path.splitext(filepath)[1].lower() in SUPPORTED_TAG_WRITE_EXTENSIONS


def can_write_track_info(filepath):
    return os.path.splitext(filepath)[1].lower() in SUPPORTED_INFO_WRITE_EXTENSIONS


def register_easy_mp4_keys():
    """mutagen's easy MP4 interface has no composer key; map it to the
    standard \xa9wrt atom so M4A composers can be read and written."""
    try:
        from mutagen.easymp4 import EasyMP4Tags
    except ImportError:
        return
    if "composer" not in EasyMP4Tags.Set:
        EasyMP4Tags.RegisterTextKey("composer", "\xa9wrt")


register_easy_mp4_keys()


def _number_tag(number, total):
    if not number:
        return None
    return f"{number}/{total}" if total else str(number)


def write_track_tags(filepath, changes):
    """
    Writes one track's edited fields (the Get Info editor) into its own
    tags. changes maps library column names to new values - only the
    keys present are touched, and a None/empty value removes that tag.
    Track and disc numbers are written as "n/total", so the caller
    passes track_nr together with track_total (and cd_nr with cd_total).
    Returns (success: bool, error_message: str or None).
    """
    if not can_write_track_info(filepath):
        ext = os.path.splitext(filepath)[1].lower().lstrip(".") or "this"
        return False, f"writing tags to .{ext} files isn't supported"
    try:
        import mutagen

        f = mutagen.File(filepath, easy=True)
        if f is None:
            return False, "not a recognized audio file"
        if f.tags is None:
            f.add_tags()

        values = {}
        for col, key in (("title", "title"), ("artist", "artist"), ("album", "album"),
                         ("albumartist", "albumartist"), ("genre", "genre"),
                         ("composer", "composer")):
            if col in changes:
                values[key] = changes[col]
        if "year" in changes:
            values["date"] = str(changes["year"]) if changes["year"] else None
        if "track_nr" in changes or "track_total" in changes:
            values["tracknumber"] = _number_tag(changes.get("track_nr"), changes.get("track_total"))
        if "cd_nr" in changes or "cd_total" in changes:
            values["discnumber"] = _number_tag(changes.get("cd_nr"), changes.get("cd_total"))

        for key, value in values.items():
            if value in (None, ""):
                if key in f.tags:
                    del f.tags[key]
            else:
                f.tags[key] = [str(value)]
        f.save()
        return True, None
    except ImportError:
        return False, "mutagen isn't installed"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def write_album_tags(filepath, album=None, artist=None, genre=None, year=None):
    """
    Writes the given album-level fields into filepath's own tags. Only
    fields passed as not-None are touched — titles, track numbers, etc.
    are left alone, since this is for album-wide edits only.
    Returns (success: bool, error_message: str or None).
    """
    ext = os.path.splitext(filepath)[1].lower()

    try:
        if ext == ".mp3":
            from mutagen.easyid3 import EasyID3
            from mutagen.id3 import ID3NoHeaderError

            try:
                tags = EasyID3(filepath)
            except ID3NoHeaderError:
                tags = EasyID3()
                tags.save(filepath)
                tags = EasyID3(filepath)

            if album is not None:
                tags["album"] = album
            if artist is not None:
                tags["artist"] = artist
            if genre is not None:
                tags["genre"] = genre
            if year:
                tags["date"] = str(year)
            tags.save()
            return True, None

        elif ext in (".m4a", ".m4b"):
            from mutagen.mp4 import MP4

            tags = MP4(filepath)
            if tags.tags is None:
                tags.add_tags()
            if album is not None:
                tags["\xa9alb"] = [album]
            if artist is not None:
                tags["\xa9ART"] = [artist]
            if genre is not None:
                tags["\xa9gen"] = [genre]
            if year:
                tags["\xa9day"] = [str(year)]
            tags.save()
            return True, None

        else:
            return False, f"writing tags to .{ext.lstrip('.')} files isn't supported (only .mp3/.m4a/.m4b are)"

    except ImportError:
        return False, "mutagen isn't installed"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def write_embedded_art(filepath, image_bytes, image_ext=".jpg"):
    """
    Embeds image_bytes as filepath's cover art — ID3 APIC (MP3) or the
    MP4 'covr' atom (M4A/M4B) — replacing any existing embedded picture
    so repeated edits don't pile up duplicate images.
    Returns (success: bool, error_message: str or None).
    """
    ext = os.path.splitext(filepath)[1].lower()
    is_png = image_ext.lower() == ".png"

    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3, ID3NoHeaderError, APIC, PictureType

            try:
                tags = ID3(filepath)
            except ID3NoHeaderError:
                tags = ID3()

            tags.delall("APIC")  # drop any existing pictures first
            tags.add(APIC(
                encoding=3,  # UTF-8
                mime="image/png" if is_png else "image/jpeg",
                type=PictureType.COVER_FRONT,
                desc="Cover",
                data=image_bytes,
            ))
            tags.save(filepath)
            return True, None

        elif ext in (".m4a", ".m4b"):
            from mutagen.mp4 import MP4, MP4Cover

            tags = MP4(filepath)
            if tags.tags is None:
                tags.add_tags()
            fmt = MP4Cover.FORMAT_PNG if is_png else MP4Cover.FORMAT_JPEG
            tags["covr"] = [MP4Cover(image_bytes, imageformat=fmt)]
            tags.save()
            return True, None

        else:
            return False, f"embedding art into .{ext.lstrip('.')} files isn't supported (only .mp3/.m4a/.m4b are)"

    except ImportError:
        return False, "mutagen isn't installed"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
