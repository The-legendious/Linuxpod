"""
podcast.py — resolving an Apple Podcasts (or direct RSS) URL to a feed,
reading that feed's episode list, and downloading episode audio files.

No third-party dependencies: the iTunes lookup call and the RSS feed
itself are both fetched with urllib (stdlib), and the feed is parsed
with xml.etree.ElementTree (stdlib) rather than a feedparser-style
library, since podcast RSS is simple enough not to need one and this
keeps the app installable with nothing beyond what scanner.py/mutagen
already require.

Once an episode is downloaded, it's just an ordinary audio file -
gui.py hands it to scanner.read_tags() and Library.upsert_track() the
same as any file found scanning a folder, so podcasts get everything
that already works for music for free (Albums/Genres browsing,
playlists, syncing, iPod compatibility checks). This module's job
stops at "here is an audio file on disk" - see gui.py for how it
becomes a library entry.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

USER_AGENT = "Mozilla/5.0 (compatible; linuxpod)"
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"

APPLE_PODCASTS_ID_RE = re.compile(r"/id(\d+)")


class PodcastError(Exception):
    pass


def _http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise PodcastError(f"HTTP {e.code} fetching {url}") from e
    except urllib.error.URLError as e:
        raise PodcastError(f"Could not reach {url}: {e.reason}") from e
    except TimeoutError as e:
        raise PodcastError(f"Timed out fetching {url}") from e


def resolve_feed_url(url: str) -> dict:
    """
    Turns whatever the person pasted into an actual RSS feed URL.

    An apple podcasts.apple.com/.../id123456789 link doesn't carry the
    feed itself - Apple's own app resolves it via a private API. The
    public, documented way to get the real feed URL from that same ID
    is the iTunes Search/Lookup API (used by countless podcast tools
    for exactly this), which is what this calls.

    A URL that isn't an apple.com podcasts link is assumed to already
    be a direct RSS feed URL (podcast apps and most podcast websites
    publish these directly) and is returned as-is - fetch_feed() below
    will raise a clear PodcastError if it turns out not to parse as
    one.

    Returns {"feed_url": str, "show_name": str or None, "artwork_url":
    str or None} - show_name/artwork_url are filled in when the source
    (iTunes lookup) provides them; fetch_feed() fills them from the
    feed itself otherwise.
    """
    m = APPLE_PODCASTS_ID_RE.search(url)
    if "podcasts.apple.com" not in url or not m:
        return {"feed_url": url, "show_name": None, "artwork_url": None}

    podcast_id = m.group(1)
    lookup_url = f"https://itunes.apple.com/lookup?id={podcast_id}&entity=podcast"
    raw = _http_get(lookup_url)
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise PodcastError("Apple's lookup service returned something unexpected") from e

    results = data.get("results") or []
    if not results or not results[0].get("feedUrl"):
        raise PodcastError(
            "Couldn't find a podcast feed for that Apple Podcasts link - "
            "it may not be a show page (an episode-only link isn't enough) "
            "or the show may not be available in this lookup."
        )
    result = results[0]
    return {
        "feed_url": result["feedUrl"],
        "show_name": result.get("collectionName"),
        "artwork_url": result.get("artworkUrl600") or result.get("artworkUrl100"),
    }


def _text(el):
    return el.text.strip() if el is not None and el.text else None


def _duration_to_ms(value):
    """itunes:duration is either plain seconds ("1830") or HH:MM:SS /
    MM:SS - handle both."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value) * 1000
    parts = value.split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return None
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds * 1000


def fetch_feed(feed_url: str, timeout=20) -> dict:
    """
    Downloads and parses an RSS feed. Returns:
      {
        "show_name": str, "artwork_url": str or None, "description": str or None,
        "episodes": [
          {"guid": str, "title": str, "pub_date": str or None,
           "audio_url": str, "mime_type": str or None,
           "duration_ms": int or None, "episode_nr": int or None},
          ...
        ],  # newest first, as published in the feed
      }
    Raises PodcastError if the URL doesn't parse as an RSS feed at all,
    or has no episodes with a usable audio enclosure.
    """
    raw = _http_get(feed_url, timeout=timeout)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise PodcastError(
            "That URL doesn't look like a podcast RSS feed (couldn't parse it as XML)."
        ) from e

    channel = root.find("channel")
    if channel is None:
        raise PodcastError("That feed has no <channel> - not a podcast RSS feed.")

    show_name = _text(channel.find("title")) or "Untitled Podcast"
    description = _text(channel.find("description"))
    artwork_url = None
    itunes_image = channel.find(f"{{{ITUNES_NS}}}image")
    if itunes_image is not None:
        artwork_url = itunes_image.get("href")
    if not artwork_url:
        image = channel.find("image/url")
        artwork_url = _text(image)

    episodes = []
    for item in channel.findall("item"):
        enclosure = item.find("enclosure")
        audio_url = enclosure.get("url") if enclosure is not None else None
        if not audio_url:
            continue  # not every <item> is necessarily an audio episode
        guid_el = item.find("guid")
        guid = _text(guid_el) or audio_url
        episode_nr = None
        ep_el = item.find(f"{{{ITUNES_NS}}}episode")
        if ep_el is not None and ep_el.text and ep_el.text.strip().isdigit():
            episode_nr = int(ep_el.text.strip())
        episodes.append({
            "guid": guid,
            "title": _text(item.find("title")) or "Untitled Episode",
            "pub_date": _text(item.find("pubDate")),
            "audio_url": audio_url,
            "mime_type": enclosure.get("type"),
            "duration_ms": _duration_to_ms(_text(item.find(f"{{{ITUNES_NS}}}duration"))),
            "episode_nr": episode_nr,
        })

    if not episodes:
        raise PodcastError(f"'{show_name}' has no episodes with a playable audio file in this feed.")

    return {
        "show_name": show_name,
        "artwork_url": artwork_url,
        "description": description,
        "episodes": episodes,
    }


_EXT_BY_MIME = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".m4a",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}


def guess_extension(audio_url: str, mime_type: str = None) -> str:
    path_ext = os.path.splitext(audio_url.split("?")[0])[1].lower()
    if path_ext in (".mp3", ".m4a", ".m4b", ".aac", ".wav", ".aiff", ".aif"):
        return path_ext
    if mime_type and mime_type.lower() in _EXT_BY_MIME:
        return _EXT_BY_MIME[mime_type.lower()]
    return ".mp3"  # the overwhelming majority of feeds; a safe default


def safe_filename(text: str, max_len: int = 120) -> str:
    """Same rationale as gui._safe_folder_name - podcast titles are
    free text from someone else's RSS feed and can contain anything,
    including characters that break as a filename."""
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", text or "").strip()
    cleaned = cleaned.strip(". ")
    return (cleaned or "untitled")[:max_len]


def download_show_artwork(url, dest_dir, timeout=20):
    """
    Downloads a show's artwork into dest_dir as cover.<ext> - the exact
    filename artwork.py's folder-art lookup already recognizes (see
    artwork._find_folder_art), so every episode downloaded into that
    same folder picks up real cover art automatically the same way a
    manually-ripped album folder with its own cover.jpg already does -
    nothing needs to be embedded into each episode's own tags, and the
    art shows up both in this app's grid and (via
    artwork.extract_artwork_to_tempfile) when synced to the iPod.

    Best-effort: returns the saved path, or None on any failure -
    missing/broken artwork is never worth failing a subscribe or
    refresh over.
    """
    if not url:
        return None
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            content_type = resp.headers.get("Content-Type", "")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return None
    if not data:
        return None
    ext = ".png" if ("png" in content_type.lower() or url.split("?")[0].lower().endswith(".png")) else ".jpg"
    dest_path = os.path.join(dest_dir, f"cover{ext}")
    try:
        os.makedirs(dest_dir, exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(data)
    except OSError:
        return None
    return dest_path


class DownloadCancelled(PodcastError):
    pass


def download_episode(audio_url, dest_path, on_progress=None, cancel_event=None, timeout=30):
    """
    Streams audio_url to dest_path (a full file path, not just a
    folder), writing to a temporary ".part" sibling first and renaming
    into place only once the download completes fully - so a
    cancelled or failed download never leaves a corrupt, partial file
    sitting at dest_path looking like a finished one (same reasoning
    as video_converter.convert's cancellation handling).

    on_progress(bytes_done, total_bytes_or_None) is called periodically.
    cancel_event, if given, is checked between chunks; raises
    DownloadCancelled (leaving no partial file behind) if set.
    """
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    part_path = dest_path + ".part"
    req = urllib.request.Request(audio_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total = resp.headers.get("Content-Length")
            total = int(total) if total and total.isdigit() else None
            done = 0
            with open(part_path, "wb") as f:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise DownloadCancelled("cancelled")
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
    except DownloadCancelled:
        try:
            os.remove(part_path)
        except OSError:
            pass
        raise
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
        try:
            os.remove(part_path)
        except OSError:
            pass
        raise PodcastError(f"Download failed: {e}") from e

    os.replace(part_path, dest_path)
    return dest_path
