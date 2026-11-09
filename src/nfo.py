"""
nfo.py — reads and writes Kodi/XBMC-style .nfo sidecar files for
movies and TV episodes, plus locating (or setting) a TV show's "fan
art" image.

.nfo files are a long-standing convention from media-center software
(Kodi/XBMC, and the various scrapers that feed it) - an XML file next
to a video file carrying metadata (title, show name, season/episode
number, etc.) that would otherwise have to be guessed from the
filename. Real-world .nfo files aren't always well-formed XML, though -
Kodi also supports a "URL nfo" that's just a bare scraper URL, and
some tools produce malformed or truncated files - so every parse here
is best-effort: anything that doesn't look like the expected document
just means nothing gets overridden, never an error that blocks adding
the video.

The write side (write_movie_nfo/write_episode_nfo, used by the app's
own Edit Info editor) always produces a plain, well-formed document in
the same shape Kodi itself would read - so a show or movie edited here
stays fully compatible with Kodi/other scrapers too, not just this app.
"""

import os
import xml.etree.ElementTree as ET

NFO_EXT = ".nfo"
# Kodi's own naming convention for a TV show's fan-art image, kept in
# the show's root folder (one level above any "Season 01" subfolders).
# "banner" is included too since it's a common alternate name for the
# same kind of wide promotional image when no dedicated fanart file
# was scraped.
FANART_BASENAMES = ("fanart", "show_fanart", "banner")
FANART_EXTENSIONS = (".jpg", ".jpeg", ".png")


def _find_sidecar(filepath):
    """<basename>.nfo right next to filepath, if it exists."""
    candidate = os.path.splitext(filepath)[0] + NFO_EXT
    return candidate if os.path.isfile(candidate) else None


def _parse_xml(path):
    try:
        return ET.parse(path).getroot()
    except (ET.ParseError, OSError, UnicodeDecodeError):
        return None


def _text(root, tag):
    if root is None:
        return None
    el = root.find(tag)
    if el is None or el.text is None:
        return None
    value = el.text.strip()
    return value or None


def _int(root, tag):
    value = _text(root, tag)
    if value is None:
        return None
    try:
        # Some scrapers write "3.0" instead of "3" - float() handles both.
        return int(float(value))
    except ValueError:
        return None


def parse_movie_nfo(filepath):
    """Looks for <basename>.nfo next to filepath. Returns {"title": ...}
    if it's a readable <movie> document with a title, else None."""
    path = _find_sidecar(filepath)
    if not path:
        return None
    root = _parse_xml(path)
    if root is None or root.tag != "movie":
        return None
    title = _text(root, "title")
    return {"title": title} if title else None


def parse_episode_nfo(filepath):
    """Looks for <basename>.nfo next to filepath. Returns a dict with
    whatever it found among {"title", "show_name", "season_nr",
    "episode_nr"} (keys it couldn't find are simply left out), or None
    if there's no usable .nfo at all."""
    path = _find_sidecar(filepath)
    if not path:
        return None
    root = _parse_xml(path)
    if root is None or root.tag != "episodedetails":
        return None

    result = {}
    title = _text(root, "title")
    if title:
        result["title"] = title
    show_name = _text(root, "showtitle") or _text(root, "showname")
    if show_name:
        result["show_name"] = show_name
    season_nr = _int(root, "season")
    if season_nr is not None:
        result["season_nr"] = season_nr
    episode_nr = _int(root, "episode")
    if episode_nr is not None:
        result["episode_nr"] = episode_nr
    return result or None


def _sidecar_path(filepath):
    """Same naming rule as _find_sidecar, but returns the path
    regardless of whether it exists yet - for writing a new one."""
    return os.path.splitext(filepath)[0] + NFO_EXT


def _write_document(root_tag, fields, path):
    """fields: an iterable of (tag, value) pairs. A value that's None
    or an empty/whitespace-only string is left out of the document
    entirely, rather than written as an empty tag - Kodi and other
    readers treat a missing tag and an empty one the same way, and
    leaving it out keeps the file from filling up with clutter for
    whatever the person didn't fill in."""
    root = ET.Element(root_tag)
    for tag, value in fields:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        ET.SubElement(root, tag).text = text
    tree = ET.ElementTree(root)
    try:
        ET.indent(tree, space="  ")  # Python 3.9+; a plain-but-valid file without it is fine too
    except AttributeError:
        pass
    tree.write(path, encoding="utf-8", xml_declaration=True)


def write_movie_nfo(filepath, title=None, studio=None, year=None, genre=None, plot=None):
    """Writes (or overwrites) <basename>.nfo next to filepath as a
    Kodi-style <movie> document. Returns the path written to."""
    path = _sidecar_path(filepath)
    _write_document("movie", [
        ("title", title), ("studio", studio), ("year", year),
        ("genre", genre), ("plot", plot),
    ], path)
    return path


def write_episode_nfo(filepath, title=None, show_name=None, season_nr=None,
                       episode_nr=None, studio=None, year=None, genre=None, plot=None):
    """Writes (or overwrites) <basename>.nfo next to filepath as a
    Kodi-style <episodedetails> document. Returns the path written to."""
    path = _sidecar_path(filepath)
    _write_document("episodedetails", [
        ("title", title), ("showtitle", show_name), ("season", season_nr),
        ("episode", episode_nr), ("studio", studio), ("year", year),
        ("genre", genre), ("plot", plot),
    ], path)
    return path


def write_tvshow_nfo(folder, show_name=None, studio=None, year=None, genre=None, plot=None):
    """Writes (or overwrites) tvshow.nfo in `folder` - Kodi's
    show-level (not per-episode) metadata file, for the show as a
    whole rather than one episode. Returns the path written to."""
    path = os.path.join(folder, "tvshow.nfo")
    _write_document("tvshow", [
        ("title", show_name), ("studio", studio), ("year", year),
        ("genre", genre), ("plot", plot),
    ], path)
    return path


def find_show_fanart(filepath):
    """Looks for a TV show's fan-art image, for use as its playlist/
    group-tile thumbnail. Checked in the episode's own folder first,
    then one folder up - the common Kodi layout is
    <Show>/fanart.jpg alongside <Show>/Season 01/episode.mkv, so an
    episode sitting in a season subfolder still finds its show's
    fanart one level up. Returns the image path, or None if nothing
    matched."""
    folder = os.path.dirname(filepath)
    parent = os.path.dirname(folder) if folder else ""
    for candidate_dir in (folder, parent):
        if not candidate_dir:
            continue
        for base in FANART_BASENAMES:
            for ext in FANART_EXTENSIONS:
                candidate = os.path.join(candidate_dir, base + ext)
                if os.path.isfile(candidate):
                    return candidate
    return None
