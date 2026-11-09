"""
library.py — the local music library, stored in a small SQLite database.

This is the "iTunes-like library" the app keeps on your computer, separate
from whatever is actually on the iPod. You scan folders into it, organize
playlists, and then choose what to push to the device.
"""

import os

import appdirs
import sqlite3
import threading
import time

DB_DIR = appdirs.DATA_DIR
DB_PATH = os.path.join(DB_DIR, "library.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath TEXT UNIQUE NOT NULL,
    title TEXT,
    artist TEXT,
    album TEXT,
    albumartist TEXT,
    genre TEXT,
    composer TEXT,
    track_nr INTEGER,
    track_total INTEGER,
    cd_nr INTEGER,
    cd_total INTEGER,
    year INTEGER,
    duration_ms INTEGER,
    bitrate INTEGER,
    samplerate INTEGER,
    size_bytes INTEGER,
    filetype TEXT,
    is_vbr INTEGER,
    date_added REAL,
    play_count INTEGER DEFAULT 0,   -- plays imported from the iPod (see import_ipod_play_counts)
    last_played REAL                -- unix time of the most recent play seen on the iPod
);

CREATE TABLE IF NOT EXISTS ipod_play_counts (
    dbid TEXT PRIMARY KEY,          -- the on-device track's dbid (unique per iPod track)
    track_id INTEGER NOT NULL,      -- which local track its plays were credited to
    playcount INTEGER NOT NULL,     -- the iPod's own running total as of the last import -
                                     -- only plays above this get added to tracks.play_count,
                                     -- so reading the device again never double-counts
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id INTEGER NOT NULL,
    track_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (playlist_id, track_id),
    FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS source_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath TEXT NOT NULL,         -- the actual playable file (possibly a converted copy)
    source_path TEXT,               -- the original file the user added - stable identity across
                                     -- re-scans/re-conversions, unlike filepath, which can change.
                                     -- Uniqueness enforced in Library.upsert_video(), not here -
                                     -- SQLite can't add a UNIQUE column via ALTER TABLE for
                                     -- users upgrading from before this column existed.
    title TEXT,
    kind TEXT NOT NULL,             -- 'movie' or 'tvshow'
    show_name TEXT,                 -- TV shows only
    season_nr INTEGER,              -- TV shows only
    episode_nr INTEGER,             -- TV shows only
    duration_ms INTEGER,
    size_bytes INTEGER,
    was_converted INTEGER,          -- 1 if this file is a converted copy, not the original
    thumbnail_path TEXT,            -- a generated frame grab, for the video grid view
    show_fanart_path TEXT,          -- TV shows only: a fan-art image found alongside the
                                     -- episode (see nfo.find_show_fanart) - preferred over
                                     -- thumbnail_path for the show's own group-tile/playlist
                                     -- thumbnail in the TV Shows grid, falling back to
                                     -- thumbnail_path when no fanart was found
    fanart_path TEXT,               -- movies only: a fan-art image chosen via the Edit Info
                                     -- editor (see gui._open_movie_or_episode_info_editor) -
                                     -- saved for Kodi-compatibility/round-tripping through
                                     -- .nfo, not currently shown anywhere in this app's own UI
    studio TEXT,                    -- from the Edit Info editor or a parsed .nfo
    year INTEGER,
    genre TEXT,
    plot TEXT,
    date_added REAL
);

CREATE TABLE IF NOT EXISTS video_source_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL              -- 'movie' or 'tvshow' - which library this folder feeds
);

CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath TEXT NOT NULL,         -- the actual synced file (always the original - no
                                     -- conversion step for photos, unlike video)
    source_path TEXT,               -- stable identity across re-scans, same reasoning as
                                     -- videos.source_path; uniqueness enforced in
                                     -- Library.upsert_photo(), not at the SQL level
    album_name TEXT,                -- optional - which on-device album this belongs to,
                                     -- in addition to the required master Photo Library
    thumbnail_path TEXT,            -- a resized copy, for the photo grid view
    size_bytes INTEGER,
    date_added REAL
);

CREATE TABLE IF NOT EXISTS photo_source_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    album_name TEXT                 -- empty/NULL means no album, just the master library
);

CREATE TABLE IF NOT EXISTS sync_marks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_type TEXT NOT NULL,        -- 'album', 'track', 'playlist', 'artist', 'genre'
    item_key TEXT NOT NULL,         -- see Library.mark_for_sync for the per-type key format
    UNIQUE(item_type, item_key)
);

CREATE TABLE IF NOT EXISTS pending_video_removals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,                     -- matched against on-device tracks by title+duration
    duration_ms INTEGER,            -- (the on-device dbid isn't known unless an iPod happens
                                     -- to be connected right when the video is removed from
                                     -- the library - see Library.add_pending_video_removal)
    requested_at REAL
);

CREATE TABLE IF NOT EXISTS deleted_video_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT UNIQUE NOT NULL,   -- the ORIGINAL file's path (videos.source_path), not
                                         -- the converted copy - the original is never touched
                                         -- by Remove from Library, so it's still sitting right
                                         -- there in a tracked folder for the next scan to find
                                         -- unless it's recorded here as "don't bring this back"
    deleted_at REAL
);

CREATE TABLE IF NOT EXISTS ipod_links (
    dbid TEXT PRIMARY KEY,          -- a song on an iPod (libgpod's 64-bit dbid)
    track_id INTEGER NOT NULL,      -- the library song it is a copy of
    how TEXT,                       -- 'sync' (this app copied it) or 'match' (found already there)
    linked_at REAL
);

CREATE TABLE IF NOT EXISTS removed_track_files (
    filepath TEXT PRIMARY KEY,      -- a song removed with Remove from Library; the file
                                     -- itself is left alone, so Refresh Library skips it
                                     -- instead of adding it straight back
    removed_at REAL
);

CREATE TABLE IF NOT EXISTS podcast_shows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    feed_url TEXT UNIQUE NOT NULL,
    artwork_path TEXT,              -- downloaded show artwork, used as track artwork on sync
    auto_download INTEGER DEFAULT 1,-- whether Refresh Library checks this feed for new episodes
    download_count INTEGER,         -- how many of the newest episodes to keep downloaded;
                                     -- NULL means "use the app-wide default" (Settings) -
                                     -- ignored entirely when download_all is set
    download_all INTEGER DEFAULT 0, -- 1 = grab every episode in the feed (not just the
                                     -- newest download_count) and keep grabbing every new
                                     -- one as it's published, forever
    last_checked REAL,
    date_added REAL
);

CREATE TABLE IF NOT EXISTS podcast_episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id INTEGER NOT NULL,
    guid TEXT NOT NULL,             -- the feed's own <guid> (or enclosure URL, if a feed omits
                                     -- guid) - identifies an episode across refreshes so it's
                                     -- only ever downloaded once, the same role
                                     -- videos.source_path plays for video folder scans
    track_id INTEGER,               -- tracks.id once downloaded and added to the library -
                                     -- NULL for an episode this app knows about (from the feed)
                                     -- but hasn't downloaded (e.g. older than the keep-count)
    title TEXT,
    pub_date TEXT,
    UNIQUE(show_id, guid),
    FOREIGN KEY (show_id) REFERENCES podcast_shows(id) ON DELETE CASCADE,
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE SET NULL
);
"""



# Podcast episodes are stored as tracks with genre 'Podcast' (see gui.py's
# podcast download code); the music views leave them out.
NOT_PODCAST = "(genre IS NULL OR genre != 'Podcast')"

# Spoofify's automatic playlists (see Library.refresh_spoofify).
SPOOFIFY_LEAST = "Spoofify: Not Played Much"
SPOOFIFY_MOST = "Spoofify: Most Played"
# The Songs list's default order: by title, A to Z, ignoring case.
SONG_ORDER = "title COLLATE NOCASE, artist COLLATE NOCASE, album COLLATE NOCASE"

class Library:
    """
    Wraps a sqlite3 connection so it's safe to call from the GUI's
    background worker threads (scanning, syncing) as well as the main
    thread. sqlite3 connections are tied to the thread that created them
    by default (check_same_thread=True); we relax that and instead
    serialize all access ourselves with a lock, since SQLite doesn't
    support true concurrent writers on one connection anyway.
    """

    def __init__(self, db_path=DB_PATH):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.commit()

    def _migrate(self):
        """Adds columns introduced after a user's database already
        existed, so upgrading the app doesn't break on an old library.db."""
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(tracks)").fetchall()}
        if "is_vbr" not in existing:
            self.conn.execute("ALTER TABLE tracks ADD COLUMN is_vbr INTEGER")
        if "play_count" not in existing:
            self.conn.execute("ALTER TABLE tracks ADD COLUMN play_count INTEGER DEFAULT 0")
        if "last_played" not in existing:
            self.conn.execute("ALTER TABLE tracks ADD COLUMN last_played REAL")

        video_cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(videos)").fetchall()}
        if video_cols and "thumbnail_path" not in video_cols:
            self.conn.execute("ALTER TABLE videos ADD COLUMN thumbnail_path TEXT")
        if video_cols and "source_path" not in video_cols:
            self.conn.execute("ALTER TABLE videos ADD COLUMN source_path TEXT")
        if video_cols and "show_fanart_path" not in video_cols:
            self.conn.execute("ALTER TABLE videos ADD COLUMN show_fanart_path TEXT")
        for col, coltype in (
            ("fanart_path", "TEXT"), ("studio", "TEXT"), ("year", "INTEGER"),
            ("genre", "TEXT"), ("plot", "TEXT"),
        ):
            if video_cols and col not in video_cols:
                self.conn.execute(f"ALTER TABLE videos ADD COLUMN {col} {coltype}")

        podcast_show_cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(podcast_shows)").fetchall()}
        if podcast_show_cols and "download_all" not in podcast_show_cols:
            self.conn.execute("ALTER TABLE podcast_shows ADD COLUMN download_all INTEGER DEFAULT 0")

    # ---- tracks -----------------------------------------------------

    def upsert_track(self, meta: dict) -> int:
        """meta must contain 'filepath'; other keys are optional tag fields."""
        cols = [
            "filepath", "title", "artist", "album", "albumartist", "genre",
            "composer", "track_nr", "track_total", "cd_nr", "cd_total",
            "year", "duration_ms", "bitrate", "samplerate", "size_bytes",
            "filetype", "is_vbr",
        ]
        values = [meta.get(c) for c in cols]
        with self._lock:
            cur = self.conn.execute(
                "SELECT id FROM tracks WHERE filepath = ?", (meta["filepath"],)
            )
            row = cur.fetchone()
            if row:
                track_id = row["id"]
                set_clause = ", ".join(f"{c} = ?" for c in cols)
                self.conn.execute(
                    f"UPDATE tracks SET {set_clause} WHERE id = ?",
                    values + [track_id],
                )
            else:
                placeholders = ", ".join("?" for _ in cols)
                cur = self.conn.execute(
                    f"INSERT INTO tracks ({', '.join(cols)}, date_added) "
                    f"VALUES ({placeholders}, ?)",
                    values + [time.time()],
                )
                track_id = cur.lastrowid
            self.conn.commit()
        return track_id

    def all_tracks(self, order_by="artist, album, track_nr", include_podcasts=False):
        """Music tracks; podcast episodes (genre 'Podcast') live in the
        Podcasts library instead and are left out unless asked for."""
        where = "" if include_podcasts else f"WHERE {NOT_PODCAST} "
        with self._lock:
            return self.conn.execute(
                f"SELECT * FROM tracks {where}ORDER BY {order_by}"
            ).fetchall()

    def search_tracks(self, query: str):
        like = f"%{query}%"
        with self._lock:
            return self.conn.execute(
                f"SELECT * FROM tracks WHERE (title LIKE ? OR artist LIKE ? "
                f"OR album LIKE ?) AND {NOT_PODCAST} ORDER BY {SONG_ORDER}",
                (like, like, like),
            ).fetchall()

    def tracks_by_ids(self, ids):
        if not ids:
            return []
        ids = list(ids)
        rows = []
        with self._lock:
            # Chunked so a huge selection stays under SQLite's variable limit.
            for i in range(0, len(ids), 900):
                chunk = ids[i:i + 900]
                placeholders = ", ".join("?" for _ in chunk)
                rows.extend(self.conn.execute(
                    f"SELECT * FROM tracks WHERE id IN ({placeholders})", chunk
                ).fetchall())
        return rows

    # ---- source folders (for Refresh Library) --------------------------

    def add_source_folder(self, path: str):
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO source_folders (path) VALUES (?)", (path,)
            )
            self.conn.commit()

    def all_source_folders(self):
        with self._lock:
            return [
                row["path"] for row in
                self.conn.execute("SELECT path FROM source_folders ORDER BY path").fetchall()
            ]

    def remove_source_folder(self, path: str):
        with self._lock:
            self.conn.execute("DELETE FROM source_folders WHERE path = ?", (path,))
            self.conn.commit()

    def track_ids_in_folder(self, path: str):
        prefix = path.rstrip("/") + "/"
        with self._lock:
            return [
                row["id"] for row in self.conn.execute(
                    "SELECT id FROM tracks WHERE substr(filepath, 1, ?) = ?",
                    (len(prefix), prefix),
                ).fetchall()
            ]

    def prune_missing_tracks(self, folders):
        """Drops library songs whose files have been deleted. Only songs
        inside a library folder that exists right now are checked (a
        folder on an unplugged drive is left alone), plus songs added
        one by one whose own folder still exists. Returns the ids
        removed."""
        folders = [f.rstrip("/") + "/" for f in folders if os.path.isdir(f)]
        with self._lock:
            rows = self.conn.execute("SELECT id, filepath FROM tracks").fetchall()
        gone = []
        for r in rows:
            path = r["filepath"] or ""
            if not path or os.path.exists(path):
                continue
            in_folder = any(path.startswith(f) for f in folders)
            if in_folder or os.path.isdir(os.path.dirname(path)):
                gone.append(r["id"])
        self.remove_tracks(gone)
        return gone

    def move_source_folder(self, old_path: str, new_path: str):
        """Points a tracked music folder at its new location, and moves its
        songs' paths along with it so playlists and play counts survive.
        Raises sqlite3.IntegrityError if songs from new_path are already
        in the library separately."""
        old_prefix = old_path.rstrip("/") + "/"
        new_prefix = new_path.rstrip("/") + "/"
        with self._lock:
            try:
                self.conn.execute(
                    "UPDATE tracks SET filepath = ? || substr(filepath, ?) WHERE substr(filepath, 1, ?) = ?",
                    (new_prefix, len(old_prefix) + 1, len(old_prefix), old_prefix),
                )
                self.conn.execute("DELETE FROM source_folders WHERE path = ?", (old_path,))
                self.conn.execute("INSERT OR IGNORE INTO source_folders (path) VALUES (?)", (new_path,))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    # ---- videos -------------------------------------------------------

    def upsert_video(self, meta: dict) -> int:
        """meta must contain 'filepath', 'source_path', and 'kind'
        ('movie' or 'tvshow'); other keys are optional. Keyed on
        source_path (the original file, stable across re-scans), not
        filepath (the actual playable file, which can legitimately
        change if a file gets re-converted) - re-adding the same
        source_path (e.g. re-scanning a folder) updates the existing
        row in place rather than creating a duplicate."""
        cols = [
            "filepath", "source_path", "title", "kind", "show_name", "season_nr",
            "episode_nr", "duration_ms", "size_bytes", "was_converted",
            "thumbnail_path", "show_fanart_path",
        ]
        values = [meta.get(c) for c in cols]
        with self._lock:
            cur = self.conn.execute(
                "SELECT id FROM videos WHERE source_path = ?", (meta.get("source_path"),)
            )
            row = cur.fetchone()
            if row:
                video_id = row["id"]
                set_clause = ", ".join(f"{c} = ?" for c in cols)
                self.conn.execute(
                    f"UPDATE videos SET {set_clause} WHERE id = ?",
                    values + [video_id],
                )
            else:
                placeholders = ", ".join("?" for _ in cols)
                cur = self.conn.execute(
                    f"INSERT INTO videos ({', '.join(cols)}, date_added) "
                    f"VALUES ({placeholders}, ?)",
                    values + [time.time()],
                )
                video_id = cur.lastrowid
            self.conn.commit()
        return video_id

    def all_videos(self, kind=None, order_by="title"):
        with self._lock:
            if kind:
                return self.conn.execute(
                    f"SELECT * FROM videos WHERE kind = ? ORDER BY {order_by}", (kind,)
                ).fetchall()
            return self.conn.execute(f"SELECT * FROM videos ORDER BY {order_by}").fetchall()

    def video_by_source_path(self, source_path):
        """Looks up a video by its original source file's path - used
        to skip re-converting/re-copying a file a folder scan already
        processed in a previous pass (see gui.py's
        _process_one_video_file), the same way source_path already
        keys upsert_video's own insert-vs-update decision above."""
        if not source_path:
            return None
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM videos WHERE source_path = ?", (source_path,)
            ).fetchone()

    def videos_by_ids(self, ids):
        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        with self._lock:
            return self.conn.execute(
                f"SELECT * FROM videos WHERE id IN ({placeholders})", list(ids)
            ).fetchall()

    def delete_video(self, video_id):
        with self._lock:
            self.conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))
            self.conn.commit()

    # ---- deleted-video tombstones ---------------------------------
    #
    # Deleting a video only ever removes the library row and its
    # converted copy - the original source file is deliberately never
    # touched, so it's still sitting right where it was in a tracked
    # video folder. Without this, the very next Refresh Library (or
    # the automatic one on startup) would just rediscover that file
    # and silently re-add/re-convert it, making "delete" not stick.
    # gui.py's remove_selected_videos records one of these per deleted
    # video; _process_one_video_file skips any source_path found here
    # during a folder scan (not for an explicit single-file Add Video,
    # which un-tombstones it instead - see gui.py).

    def mark_video_source_deleted(self, source_path: str):
        if not source_path:
            return
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO deleted_video_sources (source_path, deleted_at) VALUES (?, ?)",
                (source_path, time.time()),
            )
            self.conn.commit()

    def unmark_video_source_deleted(self, source_path: str):
        with self._lock:
            self.conn.execute("DELETE FROM deleted_video_sources WHERE source_path = ?", (source_path,))
            self.conn.commit()

    def is_video_source_deleted(self, source_path: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM deleted_video_sources WHERE source_path = ?", (source_path,)
            ).fetchone()
            return row is not None

    def count_deleted_video_sources(self) -> int:
        with self._lock:
            row = self.conn.execute("SELECT COUNT(*) AS c FROM deleted_video_sources").fetchone()
            return row["c"] if row else 0

    def clear_deleted_video_sources(self):
        """Forgets every tombstone at once - lets previously-removed
        videos get picked back up by the next refresh, for anyone who
        wants that (see Settings → Video Folders)."""
        with self._lock:
            self.conn.execute("DELETE FROM deleted_video_sources")
            self.conn.commit()

    # Fields the Edit Info (.nfo) editor is allowed to touch - kept out
    # of upsert_video's own column list on purpose, so re-scanning a
    # folder (which calls upsert_video again) never wipes out metadata
    # entered here; these two methods are the only way these columns
    # are ever written.
    EDITABLE_VIDEO_FIELDS = (
        "title", "show_name", "season_nr", "episode_nr",
        "thumbnail_path", "show_fanart_path", "fanart_path",
        "studio", "year", "genre", "plot",
    )

    def update_video_fields(self, video_id, fields: dict):
        """Updates only the given columns (must be in
        EDITABLE_VIDEO_FIELDS) for one video row - used by the Edit
        Info editor for a single movie or episode."""
        fields = {k: v for k, v in fields.items() if k in self.EDITABLE_VIDEO_FIELDS}
        if not fields:
            return
        cols = list(fields.keys())
        set_clause = ", ".join(f"{c} = ?" for c in cols)
        with self._lock:
            self.conn.execute(
                f"UPDATE videos SET {set_clause} WHERE id = ?",
                [fields[c] for c in cols] + [video_id],
            )
            self.conn.commit()

    def update_video_fields_by_show(self, show_name, fields: dict):
        """Same as update_video_fields, but applies to every episode of
        one TV show at once - used by the show-level Edit Info editor
        for fields like studio/genre/plot/fan art that describe the
        show as a whole, not one specific episode. show_name="" (the
        catch-all "(No Show Name)" group) is a valid target."""
        fields = {k: v for k, v in fields.items() if k in self.EDITABLE_VIDEO_FIELDS}
        if not fields:
            return
        cols = list(fields.keys())
        set_clause = ", ".join(f"{c} = ?" for c in cols)
        # The "(No Show Name)" catch-all group (key "") covers episodes
        # with either an empty or a NULL show_name (see
        # _show_tvshow_group_grid's own `row["show_name"] or ""`) - match
        # both here too, or a NULL one would silently be skipped.
        where = "kind = 'tvshow' AND (show_name = ? OR show_name IS NULL)" if not show_name \
            else "kind = 'tvshow' AND show_name = ?"
        with self._lock:
            self.conn.execute(
                f"UPDATE videos SET {set_clause} WHERE {where}",
                [fields[c] for c in cols] + [show_name],
            )
            self.conn.commit()

    def add_pending_video_removal(self, title, duration_ms):
        """Records that a video removed from the library should also be
        removed from an iPod the next time one connects - the device
        might not be connected right now (or its dbid for this video
        isn't known unless it's currently connected and read), so this
        is what makes the removal happen later instead of right away.
        Matched back to an actual on-device track by title+duration
        (see gui.py's _process_pending_video_removals)."""
        with self._lock:
            self.conn.execute(
                "INSERT INTO pending_video_removals (title, duration_ms, requested_at) VALUES (?, ?, ?)",
                (title, duration_ms, time.time()),
            )
            self.conn.commit()

    def all_pending_video_removals(self):
        with self._lock:
            return self.conn.execute("SELECT * FROM pending_video_removals").fetchall()

    def clear_pending_video_removals(self, ids):
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        with self._lock:
            self.conn.execute(f"DELETE FROM pending_video_removals WHERE id IN ({placeholders})", list(ids))
            self.conn.commit()

    def add_video_source_folder(self, path: str, kind: str):
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO video_source_folders (path, kind) VALUES (?, ?)",
                (path, kind),
            )
            self.conn.commit()

    def all_video_source_folders(self, kind=None):
        with self._lock:
            if kind:
                rows = self.conn.execute(
                    "SELECT path FROM video_source_folders WHERE kind = ? ORDER BY path", (kind,)
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT path, kind FROM video_source_folders ORDER BY path"
                ).fetchall()
            return [row["path"] for row in rows] if kind else rows

    def is_video_source_folder_tracked(self, path: str) -> bool:
        """True if `path` is still in the tracked video folders list.
        Checked mid-scan (see gui.py's _scan_and_convert_video_folder)
        so that removing a folder (Settings -> Video -> Tracked Video
        Folders -> Remove) while a long conversion from it is still
        running actually stops that scan from touching its remaining
        files, rather than finishing anyway because the folder list a
        scan started with was only ever captured once, up front."""
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM video_source_folders WHERE path = ?", (path,)
            ).fetchone()
            return row is not None

    def remove_video_source_folder(self, path: str):
        """Stops tracking this folder for refresh - doesn't touch any
        videos already added to the library from it (see gui.py's
        Settings → Video → Remove, which explains that to the
        person before calling this)."""
        with self._lock:
            self.conn.execute("DELETE FROM video_source_folders WHERE path = ?", (path,))
            self.conn.commit()

    def update_video_source_folder_path(self, old_path: str, new_path: str):
        """Repoints an already-tracked folder at a new location (e.g.
        after the person moved it on disk) - future refreshes scan
        new_path instead, but videos already added from old_path keep
        their existing source_path/filepath untouched. Can raise
        sqlite3.IntegrityError if new_path is already tracked
        separately (path is UNIQUE) - left for the caller to handle."""
        with self._lock:
            self.conn.execute(
                "UPDATE video_source_folders SET path = ? WHERE path = ?", (new_path, old_path)
            )
            self.conn.commit()

    # ---- photos --------------------------------------------------------

    def upsert_photo(self, meta: dict) -> int:
        """meta must contain 'filepath' and 'source_path'; other keys
        are optional. Keyed on source_path, same reasoning as
        upsert_video - re-adding the same source_path (e.g. re-scanning
        a folder) updates the existing row instead of duplicating it."""
        cols = ["filepath", "source_path", "album_name", "thumbnail_path", "size_bytes"]
        values = [meta.get(c) for c in cols]
        with self._lock:
            cur = self.conn.execute(
                "SELECT id FROM photos WHERE source_path = ?", (meta.get("source_path"),)
            )
            row = cur.fetchone()
            if row:
                photo_id = row["id"]
                set_clause = ", ".join(f"{c} = ?" for c in cols)
                self.conn.execute(
                    f"UPDATE photos SET {set_clause} WHERE id = ?",
                    values + [photo_id],
                )
            else:
                placeholders = ", ".join("?" for _ in cols)
                cur = self.conn.execute(
                    f"INSERT INTO photos ({', '.join(cols)}, date_added) "
                    f"VALUES ({placeholders}, ?)",
                    values + [time.time()],
                )
                photo_id = cur.lastrowid
            self.conn.commit()
        return photo_id

    def all_photos(self, order_by="date_added DESC"):
        with self._lock:
            return self.conn.execute(f"SELECT * FROM photos ORDER BY {order_by}").fetchall()

    def photos_by_ids(self, ids):
        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        with self._lock:
            return self.conn.execute(
                f"SELECT * FROM photos WHERE id IN ({placeholders})", list(ids)
            ).fetchall()

    def delete_photo(self, photo_id):
        with self._lock:
            self.conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
            self.conn.commit()

    def add_photo_source_folder(self, path: str, album_name: str = ""):
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO photo_source_folders (path, album_name) VALUES (?, ?)",
                (path, album_name),
            )
            self.conn.commit()

    def all_photo_source_folders(self):
        with self._lock:
            return self.conn.execute(
                "SELECT path, album_name FROM photo_source_folders ORDER BY path"
            ).fetchall()

    def remove_photo_source_folder(self, path: str):
        with self._lock:
            self.conn.execute("DELETE FROM photo_source_folders WHERE path = ?", (path,))
            self.conn.commit()

    def update_photo_source_folder_path(self, old_path: str, new_path: str):
        with self._lock:
            self.conn.execute(
                "UPDATE photo_source_folders SET path = ? WHERE path = ?", (new_path, old_path)
            )
            self.conn.commit()

    # ---- sync marks (durable device-view sync selections) -------------

    def mark_for_sync(self, item_type: str, item_key: str):
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO sync_marks (item_type, item_key) VALUES (?, ?)",
                (item_type, item_key),
            )
            self.conn.commit()

    def unmark_for_sync(self, item_type: str, item_key: str):
        with self._lock:
            self.conn.execute(
                "DELETE FROM sync_marks WHERE item_type = ? AND item_key = ?",
                (item_type, item_key),
            )
            self.conn.commit()

    def all_marked_for_sync(self, item_type: str):
        """Returns the set of item_keys currently marked for this type."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT item_key FROM sync_marks WHERE item_type = ?", (item_type,)
            ).fetchall()
            return {row["item_key"] for row in rows}

    def set_marked_for_sync(self, item_type: str, keys):
        """Replaces the entire marked set for item_type in one go - the
        simplest way for the GUI to keep the on-disk profile in sync
        with an in-memory selection set/dict without tracking
        individual add/remove deltas itself."""
        keys = list(keys)
        with self._lock:
            self.conn.execute("DELETE FROM sync_marks WHERE item_type = ?", (item_type,))
            if keys:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO sync_marks (item_type, item_key) VALUES (?, ?)",
                    [(item_type, k) for k in keys],
                )
            self.conn.commit()

    def clear_library(self):
        """Wipes tracks, playlists, and remembered source folders. Local
        library only — never touches anything already on the iPod."""
        with self._lock:
            self.conn.execute("DELETE FROM playlist_tracks")
            self.conn.execute("DELETE FROM playlists")
            self.conn.execute("DELETE FROM tracks")
            self.conn.execute("DELETE FROM source_folders")
            self.conn.commit()

    # ---- albums ---------------------------------------------------------

    def all_albums(self):
        """One row per (album, artist) with a track count and a sample
        filepath (for pulling cover art), for the album grid view."""
        with self._lock:
            return self.conn.execute(
                "SELECT album, artist, COUNT(*) AS track_count, "
                "MIN(filepath) AS sample_filepath, MAX(year) AS year "
                f"FROM tracks WHERE {NOT_PODCAST} GROUP BY album, artist "
                "ORDER BY album COLLATE NOCASE"
            ).fetchall()

    def all_podcast_albums(self):
        """Same row shape as all_albums() (album, artist, track_count,
        sample_filepath), restricted to downloaded podcast episodes
        (genre = 'Podcast') - powers the Podcasts library view by
        reusing the exact same album-grid tile code path, so each show
        gets real cover art (pulled from its episodes' own folder -
        see gui.py's podcast download code, which saves the show's
        artwork there as cover.jpg/png), drag-to-playlist, sync
        selection, and the rest of what already works for Albums, for
        free."""
        with self._lock:
            return self.conn.execute(
                "SELECT album, artist, COUNT(*) AS track_count, "
                "MIN(filepath) AS sample_filepath "
                "FROM tracks WHERE genre = 'Podcast' GROUP BY album, artist "
                "ORDER BY album COLLATE NOCASE"
            ).fetchall()

    def tracks_by_album(self, album, artist):
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM tracks WHERE album IS ? AND artist IS ? "
                "ORDER BY cd_nr, track_nr",
                (album, artist),
            ).fetchall()

    def all_genres(self):
        """Distinct, non-empty genres already present in the library,
        alphabetically — used to populate the genre dropdown in the
        album-edit dialog so the user can pick an existing one instead
        of retyping/misspelling it."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT genre FROM tracks "
                "WHERE genre IS NOT NULL AND TRIM(genre) != '' "
                "ORDER BY genre COLLATE NOCASE"
            ).fetchall()
        return [r["genre"] for r in rows]

    def artist_summary(self):
        """(artist, track_count) for every distinct artist in the
        library, alphabetically — feeds the Artists sidebar view."""
        with self._lock:
            return self.conn.execute(
                "SELECT artist, COUNT(*) AS track_count FROM tracks "
                f"WHERE {NOT_PODCAST} GROUP BY artist ORDER BY artist COLLATE NOCASE"
            ).fetchall()

    def genre_summary(self):
        """(genre, track_count) for every distinct genre in the
        library, alphabetically — feeds the Genres sidebar view."""
        with self._lock:
            return self.conn.execute(
                "SELECT genre, COUNT(*) AS track_count FROM tracks "
                f"WHERE {NOT_PODCAST} GROUP BY genre ORDER BY genre COLLATE NOCASE"
            ).fetchall()

    def tracks_by_artist(self, artist):
        with self._lock:
            return self.conn.execute(
                f"SELECT * FROM tracks WHERE artist IS ? AND {NOT_PODCAST} ORDER BY album, cd_nr, track_nr",
                (artist,),
            ).fetchall()

    def tracks_by_genre(self, genre):
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM tracks WHERE genre IS ? ORDER BY artist, album, cd_nr, track_nr",
                (genre,),
            ).fetchall()

    def update_album_info(self, old_album, old_artist, new_album, new_artist, new_genre, new_year):
        """
        Bulk-updates album/artist/genre/year for every track currently
        grouped under (old_album, old_artist) — used by the album-edit
        dialog. Returns the affected tracks (fetched *before* the
        update, so filepath etc. are available) so the caller can also
        write the change back to the actual audio files; without that,
        a later Refresh Library would just overwrite this edit with
        whatever the files' own tags still say.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM tracks WHERE album IS ? AND artist IS ?",
                (old_album, old_artist),
            ).fetchall()
            self.conn.execute(
                "UPDATE tracks SET album = ?, artist = ?, genre = ?, year = ? "
                "WHERE album IS ? AND artist IS ?",
                (new_album, new_artist, new_genre, new_year, old_album, old_artist),
            )
            self.conn.commit()
        return rows

    EDITABLE_TRACK_COLUMNS = (
        "title", "artist", "album", "albumartist", "genre", "composer",
        "track_nr", "track_total", "cd_nr", "cd_total", "year",
    )

    def update_track_fields(self, track_id, fields: dict):
        """Saves one track's Get Info edits. Unknown keys are ignored."""
        cols = [c for c in fields if c in self.EDITABLE_TRACK_COLUMNS]
        if not cols:
            return
        with self._lock:
            self.conn.execute(
                f"UPDATE tracks SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?",
                [fields[c] for c in cols] + [track_id],
            )
            self.conn.commit()

    def remove_tracks(self, ids):
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        with self._lock:
            self.conn.execute(f"DELETE FROM tracks WHERE id IN ({placeholders})", list(ids))
            self.conn.commit()

    def add_ipod_links(self, pairs, how="sync"):
        if not pairs:
            return
        now = time.time()
        with self._lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO ipod_links (dbid, track_id, how, linked_at) VALUES (?, ?, ?, ?)",
                [(str(dbid), int(tid), how, now) for dbid, tid in pairs],
            )
            self.conn.commit()

    def forget_ipod_links(self, dbids):
        if not dbids:
            return
        with self._lock:
            self.conn.executemany("DELETE FROM ipod_links WHERE dbid = ?", [(str(d),) for d in dbids])
            self.conn.commit()

    def link_device_tracks(self, device_tracks):
        """Works out which library song each iPod track is a copy of.
        Returns {dbid: track_id}. A track this app copied to the iPod
        is known exactly (recorded at sync time). One that was already
        there is linked only when exactly one library song has the same
        title, artist and album (and a length within 2 seconds), and
        that song isn't already linked to another iPod copy - anything
        less certain stays unlinked, and unlinked tracks are never
        removed automatically."""
        with self._lock:
            known = {r["dbid"]: r["track_id"] for r in self.conn.execute(
                "SELECT l.dbid, l.track_id FROM ipod_links l JOIN tracks t ON t.id = l.track_id"
            )}
            rows = self.conn.execute("SELECT id, title, artist, album, duration_ms FROM tracks").fetchall()
        present = {str(t.get("dbid")) for t in device_tracks}
        links = {d: tid for d, tid in known.items() if d in present}
        taken = set(links.values())

        def norm(v):
            return " ".join((v or "").lower().split())

        local_by_key = {}
        for r in rows:
            if not norm(r["title"]):
                continue
            local_by_key.setdefault((norm(r["title"]), norm(r["artist"]), norm(r["album"])), []).append(r)
        new_links = []
        for t in device_tracks:
            dbid = str(t.get("dbid") or "")
            if not dbid or dbid in links:
                continue
            key = (norm(t.get("title")), norm(t.get("artist")), norm(t.get("album")))
            candidates = [
                r for r in local_by_key.get(key, [])
                if abs((r["duration_ms"] or 0) - (t.get("duration_ms") or 0)) <= 2000
            ]
            if len(candidates) == 1 and candidates[0]["id"] not in taken:
                links[dbid] = candidates[0]["id"]
                taken.add(candidates[0]["id"])
                new_links.append((dbid, candidates[0]["id"]))
        self.add_ipod_links(new_links, how="match")
        return links

    def remove_tracks_from_library(self, ids):
        """Remove from Library: drops the tracks and remembers their
        files so a refresh doesn't bring them back. Files aren't touched."""
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        now = time.time()
        with self._lock:
            paths = [r["filepath"] for r in self.conn.execute(
                f"SELECT filepath FROM tracks WHERE id IN ({placeholders})", list(ids)
            )]
            self.conn.executemany(
                "INSERT OR REPLACE INTO removed_track_files (filepath, removed_at) VALUES (?, ?)",
                [(p, now) for p in paths if p],
            )
            self.conn.execute(f"DELETE FROM tracks WHERE id IN ({placeholders})", list(ids))
            self.conn.commit()
        return len(paths)

    def removed_track_files(self):
        with self._lock:
            return {r["filepath"] for r in self.conn.execute("SELECT filepath FROM removed_track_files")}

    def forget_removed_track_file(self, filepath):
        with self._lock:
            self.conn.execute("DELETE FROM removed_track_files WHERE filepath = ?", (filepath,))
            self.conn.commit()

    def count_removed_track_files(self):
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) AS c FROM removed_track_files").fetchone()["c"]

    def clear_removed_track_files(self):
        with self._lock:
            self.conn.execute("DELETE FROM removed_track_files")
            self.conn.commit()

    def find_duplicate_groups(self):
        """Groups of tracks that look like the same actual song (matching
        title/artist/album/duration, case-insensitive) — a manual backstop
        for cases path-based prevention (see scanner.scan_folder) doesn't
        catch, e.g. a case-insensitive filesystem, or rows left over from
        before that fix existed. Returns a list of lists of Row,
        oldest-added first in each group; only groups with more than one
        track are included."""
        with self._lock:
            rows = self.conn.execute("SELECT * FROM tracks ORDER BY date_added").fetchall()
        groups = {}
        for row in rows:
            key = (
                (row["title"] or "").strip().lower(),
                (row["artist"] or "").strip().lower(),
                (row["album"] or "").strip().lower(),
                row["duration_ms"] or 0,
            )
            groups.setdefault(key, []).append(row)
        return [g for g in groups.values() if len(g) > 1]

    def remove_duplicate_tracks(self):
        """Keeps the oldest-added track in each duplicate group (see
        find_duplicate_groups) and removes the rest. Returns how many
        tracks were removed."""
        remove_ids = [row["id"] for group in self.find_duplicate_groups() for row in group[1:]]
        self.remove_tracks(remove_ids)
        return len(remove_ids)

    # ---- play counts from the iPod -------------------------------------

    def import_ipod_play_counts(self, device_tracks):
        """Merges play counts from the iPod into the local library.
        device_tracks are the dicts from ipod_backend.read_device_library;
        each is matched to a local track the same way the rest of the app
        decides a song is already on the device (title/artist/album/
        duration, case-insensitive - see find_duplicate_groups).

        Plays are ADDED, never overwritten: for each on-device track we
        remember the iPod's own running total from the last import
        (ipod_play_counts) and credit only what's been played since. The
        first time a track is seen, all of its iPod plays count as new.
        If the iPod's total ever goes down (restored or reset device,
        same dbid), its whole current total is treated as new plays.
        last_played becomes whichever is more recent.

        Safe to call on every device read - a second read with no new
        plays changes nothing. Returns (new_plays, updated_track_ids)."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, title, artist, album, duration_ms FROM tracks ORDER BY date_added"
            ).fetchall()
            local_by_key = {}
            for row in rows:
                key = (
                    (row["title"] or "").strip().lower(),
                    (row["artist"] or "").strip().lower(),
                    (row["album"] or "").strip().lower(),
                    row["duration_ms"] or 0,
                )
                local_by_key.setdefault(key, row["id"])  # oldest copy wins, like remove_duplicate_tracks
            baselines = {
                r["dbid"]: (r["track_id"], r["playcount"])
                for r in self.conn.execute("SELECT dbid, track_id, playcount FROM ipod_play_counts")
            }

            new_plays = 0
            updated = []
            now = time.time()
            for t in device_tracks:
                dbid = str(t.get("dbid") or "")
                if not dbid:
                    continue
                key = (
                    (t.get("title") or "").strip().lower(),
                    (t.get("artist") or "").strip().lower(),
                    (t.get("album") or "").strip().lower(),
                    t.get("duration_ms") or 0,
                )
                track_id = local_by_key.get(key)
                if track_id is None:
                    continue
                ipod_count = int(t.get("play_count") or 0)
                last_played = t.get("last_played")
                prev = baselines.get(dbid)
                if prev is None or prev[0] != track_id or ipod_count < prev[1]:
                    delta = ipod_count
                else:
                    delta = ipod_count - prev[1]
                if prev is None or prev != (track_id, ipod_count):
                    self.conn.execute(
                        "INSERT OR REPLACE INTO ipod_play_counts (dbid, track_id, playcount, updated_at) "
                        "VALUES (?, ?, ?, ?)",
                        (dbid, track_id, ipod_count, now),
                    )
                if delta > 0 or last_played:
                    cur = self.conn.execute(
                        "UPDATE tracks SET play_count = COALESCE(play_count, 0) + ?, "
                        "last_played = MAX(COALESCE(last_played, 0), COALESCE(?, 0)) "
                        "WHERE id = ? AND (? > 0 OR COALESCE(last_played, 0) < COALESCE(?, 0))",
                        (delta, last_played, track_id, delta, last_played),
                    )
                    if cur.rowcount:
                        updated.append(track_id)
                new_plays += max(delta, 0)
            self.conn.commit()
        return new_plays, updated

    # ---- playlists ----------------------------------------------------

    def create_playlist(self, name: str) -> int:
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO playlists (name) VALUES (?)", (name,)
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT id FROM playlists WHERE name = ?", (name,)
            ).fetchone()
            return row["id"]

    def delete_playlist(self, playlist_id: int):
        with self._lock:
            self.conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
            self.conn.commit()

    def all_playlists(self):
        with self._lock:
            return self.conn.execute("SELECT * FROM playlists ORDER BY name").fetchall()

    def all_playlists_with_counts(self):
        with self._lock:
            return self.conn.execute(
                "SELECT p.id, p.name, COUNT(pt.track_id) AS track_count "
                "FROM playlists p LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id "
                "GROUP BY p.id ORDER BY p.name"
            ).fetchall()

    def add_to_playlist(self, playlist_id: int, track_ids):
        with self._lock:
            cur = self.conn.execute(
                "SELECT COALESCE(MAX(position), -1) AS m FROM playlist_tracks "
                "WHERE playlist_id = ?",
                (playlist_id,),
            )
            pos = cur.fetchone()["m"] + 1
            for tid in track_ids:
                self.conn.execute(
                    "INSERT OR IGNORE INTO playlist_tracks "
                    "(playlist_id, track_id, position) VALUES (?, ?, ?)",
                    (playlist_id, tid, pos),
                )
                pos += 1
            self.conn.commit()

    def remove_from_playlist(self, playlist_id: int, track_ids):
        placeholders = ", ".join("?" for _ in track_ids)
        with self._lock:
            self.conn.execute(
                f"DELETE FROM playlist_tracks WHERE playlist_id = ? "
                f"AND track_id IN ({placeholders})",
                [playlist_id] + list(track_ids),
            )
            self.conn.commit()

    def playlist_tracks(self, playlist_id: int):
        with self._lock:
            return self.conn.execute(
                "SELECT t.* FROM tracks t "
                "JOIN playlist_tracks pt ON pt.track_id = t.id "
                "WHERE pt.playlist_id = ? ORDER BY pt.position",
                (playlist_id,),
            ).fetchall()

    def set_playlist_tracks(self, playlist_id: int, track_ids):
        """Replaces a playlist's contents with track_ids, in that order."""
        with self._lock:
            self.conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
            self.conn.executemany(
                "INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
                [(playlist_id, tid, pos) for pos, tid in enumerate(track_ids)],
            )
            self.conn.commit()

    def refresh_spoofify(self, least_count: int, most_count: int):
        """Spoofify: rebuilds the two automatic playlists from play
        counts (imported from the iPod). "Not Played Much" is songs
        never played first, then the fewest plays, oldest last play
        first, with ties shuffled so it isn't alphabetical; "Most
        Played" is the highest play counts. A count of 0 removes that
        playlist. Podcasts are left out. Returns (least_n, most_n)."""
        with self._lock:
            least = [r["id"] for r in self.conn.execute(
                f"SELECT id FROM tracks WHERE {NOT_PODCAST} "
                "ORDER BY COALESCE(play_count, 0), COALESCE(last_played, 0), RANDOM() LIMIT ?",
                (max(0, least_count),),
            )]
            most = [r["id"] for r in self.conn.execute(
                f"SELECT id FROM tracks WHERE {NOT_PODCAST} AND COALESCE(play_count, 0) > 0 "
                "ORDER BY play_count DESC, COALESCE(last_played, 0) DESC LIMIT ?",
                (max(0, most_count),),
            )]
        for name, ids, count in ((SPOOFIFY_LEAST, least, least_count), (SPOOFIFY_MOST, most, most_count)):
            if count > 0:
                self.set_playlist_tracks(self.create_playlist(name), ids)
            else:
                self.remove_spoofify_playlist(name)
        return len(least) if least_count > 0 else 0, len(most) if most_count > 0 else 0

    def remove_spoofify_playlist(self, name=None):
        names = [name] if name else [SPOOFIFY_LEAST, SPOOFIFY_MOST]
        with self._lock:
            for n in names:
                self.conn.execute("DELETE FROM playlists WHERE name = ?", (n,))
            self.conn.commit()

    def playlists_for_track(self, track_id: int):
        with self._lock:
            return self.conn.execute(
                "SELECT p.* FROM playlists p "
                "JOIN playlist_tracks pt ON pt.playlist_id = p.id "
                "WHERE pt.track_id = ?",
                (track_id,),
            ).fetchall()

    # ------------------------------------------------------------ podcasts --

    def add_podcast_show(self, name: str, feed_url: str, artwork_path=None,
                          download_count=None, download_all=False) -> int:
        """Subscribes to a feed - INSERT OR IGNORE so re-adding the same
        feed_url (the UNIQUE key) is a harmless no-op rather than an
        error, the same pattern as add_video_source_folder.
        download_count, when given, becomes this show's own "keep this
        many recent episodes downloaded" count (see gui.py's
        add_podcast_from_url) - NULL falls back to the app-wide
        default (Settings -> Podcasts) every time it's used.
        download_all=True instead grabs every episode in the feed, and
        every new one as it's published, ignoring download_count entirely."""
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO podcast_shows "
                "(name, feed_url, artwork_path, download_count, download_all, date_added) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, feed_url, artwork_path, download_count, 1 if download_all else 0, time.time()),
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT id FROM podcast_shows WHERE feed_url = ?", (feed_url,)
            ).fetchone()
            return row["id"]

    def update_podcast_show(self, show_id: int, download_count=None, download_all=None, auto_download=None):
        """Changes a subscribed show's own settings after the fact
        (Settings -> Podcasts' per-show editor) - only the fields
        actually passed (not None) are touched. download_count=0 is a
        legitimate value (distinct from None, "use the app default"),
        so this checks "is not None", never truthiness."""
        with self._lock:
            if download_count is not None:
                self.conn.execute(
                    "UPDATE podcast_shows SET download_count = ? WHERE id = ?", (download_count, show_id)
                )
            if download_all is not None:
                self.conn.execute(
                    "UPDATE podcast_shows SET download_all = ? WHERE id = ?",
                    (1 if download_all else 0, show_id),
                )
            if auto_download is not None:
                self.conn.execute(
                    "UPDATE podcast_shows SET auto_download = ? WHERE id = ?",
                    (1 if auto_download else 0, show_id),
                )
            self.conn.commit()

    def set_podcast_show_artwork(self, show_id: int, artwork_path):
        with self._lock:
            self.conn.execute(
                "UPDATE podcast_shows SET artwork_path = ? WHERE id = ?", (artwork_path, show_id)
            )
            self.conn.commit()

    def all_podcast_shows(self):
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM podcast_shows ORDER BY name"
            ).fetchall()

    def podcast_show(self, show_id: int):
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM podcast_shows WHERE id = ?", (show_id,)
            ).fetchone()

    def remove_podcast_show(self, show_id: int):
        """Unsubscribes - stops Refresh Library from checking this feed
        for new episodes. Doesn't touch anything already downloaded:
        those stay exactly where they are, as ordinary tracks in your
        library (see gui.py's Settings -> Podcasts, which explains
        this to the person before calling it) - the same "untracking
        isn't deleting" rule add_video_source_folder/
        remove_video_source_folder already follow."""
        with self._lock:
            self.conn.execute("DELETE FROM podcast_shows WHERE id = ?", (show_id,))
            self.conn.commit()

    def set_podcast_show_last_checked(self, show_id: int, when: float):
        with self._lock:
            self.conn.execute(
                "UPDATE podcast_shows SET last_checked = ? WHERE id = ?", (when, show_id)
            )
            self.conn.commit()

    def podcast_episode_by_guid(self, show_id: int, guid: str):
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM podcast_episodes WHERE show_id = ? AND guid = ?",
                (show_id, guid),
            ).fetchone()

    def record_podcast_episode(self, show_id: int, guid: str, title: str, pub_date: str, track_id: int):
        """Called once an episode has actually been downloaded and
        added to the tracks table (track_id) - this is what lets a
        later refresh recognize "already got this one" purely from the
        feed's guid, without needing to guess from a filename or
        re-download anything just to check."""
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO podcast_episodes "
                "(show_id, guid, track_id, title, pub_date) VALUES (?, ?, ?, ?, ?)",
                (show_id, guid, track_id, title, pub_date),
            )
            self.conn.commit()

    def podcast_episodes_for_show(self, show_id: int):
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM podcast_episodes WHERE show_id = ? ORDER BY pub_date DESC",
                (show_id,),
            ).fetchall()
