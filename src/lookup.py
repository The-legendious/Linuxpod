"""
lookup.py — looks up album metadata and cover art from Apple's public
iTunes Search API. No API key or account needed; this is the same
public endpoint iTunes/Music.app's own "get album artwork" style
lookups have historically used, and many open-source tools rely on it
for exactly this purpose.

Used by the "Search the internet for album info" button in the
album-edit dialog. Only ever called on explicit user action — never
automatically in the background.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

SEARCH_URL = "https://itunes.apple.com/search"
USER_AGENT = "linuxpod/1.0"


class LookupError(Exception):
    pass


def search_albums(artist, album, limit=10, timeout=8):
    """
    Returns a list of dicts, best guess first:
      {collection_name, artist_name, genre, year, artwork_url}
    artwork_url is a high-resolution image URL, or None if the result
    had no artwork. Raises LookupError (with a message safe to show the
    user) on any network or parsing failure.
    """
    query = " ".join(p for p in (artist, album) if p).strip()
    if not query:
        raise LookupError("Enter an artist or album name to search for first.")

    params = urllib.parse.urlencode({"term": query, "entity": "album", "limit": str(limit)})
    url = f"{SEARCH_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.URLError as e:
        raise LookupError(f"Couldn't reach the lookup service: {e.reason}") from e
    except Exception as e:
        raise LookupError(f"Lookup failed: {e}") from e

    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise LookupError(f"Got an unreadable response from the lookup service: {e}") from e

    results = []
    for item in data.get("results", []):
        if not isinstance(item, dict):
            continue
        artwork_url = item.get("artworkUrl100")
        if artwork_url:
            # iTunes serves different sizes by swapping this path segment;
            # ask for something print-quality-ish instead of the tiny
            # 100x100 default thumbnail.
            artwork_url = artwork_url.replace("100x100bb", "600x600bb")
        release_date = item.get("releaseDate") or ""
        year = release_date[:4] if len(release_date) >= 4 and release_date[:4].isdigit() else None
        results.append({
            "collection_name": item.get("collectionName") or "",
            "artist_name": item.get("artistName") or "",
            "genre": item.get("primaryGenreName") or "",
            "year": year,
            "artwork_url": artwork_url,
        })
    return results


def fetch_artwork_bytes(url, timeout=8):
    """Downloads the image at url. Raises LookupError on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.URLError as e:
        raise LookupError(f"Couldn't download artwork: {e.reason}") from e
    except Exception as e:
        raise LookupError(f"Couldn't download artwork: {e}") from e
