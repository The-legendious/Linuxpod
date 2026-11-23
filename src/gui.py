"""
gui.py — the desktop interface, built with Tkinter (ships with Python,
no extra install beyond the python3-tk system package).

Layout, iTunes-style:
  +----------------+----------------------------------------+
  | Library        |  Title | Artist | Album | Time | ...    |
  | Playlists      |  ------------------------------------   |
  |   My Mix       |  (track rows, multi-selectable)          |
  | ---            |                                          |
  | iPod (name)    |                                          |
  |   On iPod      |                                          |
  |   Device pl.   |                                          |
  +----------------+----------------------------------------+
  | status / device info                    [progress bar]   |
  +------------------------------------------------------------+
"""

import os
import sys
import queue
import threading
import io
import shutil
import re
import tempfile
import time
import traceback
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import tkinter.font as tkfont

import library
import scanner
import ipod_backend
import artwork
import applog
import settings
import tagwriter
import lookup
import video_converter
import native_dialogs
import nfo
import podcast
import firmware
import apple_style

try:
    from PIL import ImageTk
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

PLACEHOLDER_COLORS = (
    "#8B5FBF", "#4F86C6", "#5CB85C", "#D9822B", "#C9506B", "#3AA6A6",
)


def format_duration(ms):
    if not ms:
        return "--:--"
    total_seconds = int(ms / 1000)
    m, s = divmod(total_seconds, 60)
    return f"{m}:{s:02d}"


def format_last_played(ts):
    """Unix time -> 'YYYY-MM-DD 3:58 PM', or '' for never played (see
    _last_played_sort_key for sorting it)."""
    if not ts:
        return ""
    try:
        lt = time.localtime(ts)
    except (ValueError, OverflowError, OSError):
        return ""
    hour = lt.tm_hour % 12 or 12
    return f"{time.strftime('%Y-%m-%d', lt)} {hour}:{lt.tm_min:02d} {'AM' if lt.tm_hour < 12 else 'PM'}"


def _last_played_sort_key(text):
    """Sorts format_last_played() text by actual time, newest first."""
    try:
        return -time.mktime(time.strptime(text, "%Y-%m-%d %I:%M %p"))
    except (ValueError, OverflowError):
        return float("inf")


def format_bytes(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".avi", ".wmv", ".flv", ".webm"}
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif"}

_UNSAFE_FOLDER_CHARS = re.compile(r'[/\\:*?"<>|]')


def _safe_folder_name(name):
    """A show name typed in (or scraped from an .nfo) can contain
    characters that aren't safe as a folder name (a "/" would silently
    create nested folders instead of one folder named that; other
    characters aren't valid on some filesystems at all) - this swaps
    them for "-" and trims stray leading/trailing whitespace/dots so
    the result is always a single, sane folder-name component."""
    cleaned = _UNSAFE_FOLDER_CHARS.sub("-", name).strip(" .")
    return cleaned or "Untitled Show"

_TV_FILENAME_PATTERN = re.compile(r"^(.*?)[\s._-]*[Ss](\d{1,2})[Ee](\d{1,3})[\s._-]*(.*)$")


def _parse_tv_filename(filename):
    """Best-effort parse of a 'Show Name - S01E02 - Episode Title.ext'
    style filename. Returns (show_name, season, episode, episode_title),
    each None if it couldn't be determined - callers should fall back
    to the plain filename as the title in that case."""
    base = os.path.splitext(filename)[0]
    m = _TV_FILENAME_PATTERN.match(base)
    if not m:
        return None, None, None, None
    show_name = _clean_video_name_piece(m.group(1))
    season = int(m.group(2))
    episode = int(m.group(3))
    episode_title = _clean_video_name_piece(m.group(4))
    return show_name, season, episode, episode_title


def _clean_video_name_piece(piece):
    """'The.Office' / 'The_Office' -> 'The Office' - common
    scene-release naming conventions use dots/underscores as spaces."""
    cleaned = piece.replace(".", " ").replace("_", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    return cleaned or None


_ALBUM_KEY_SEP = "\x1f"  # unit separator - won't collide with real album/artist text


def _album_sync_key(album, artist):
    """Encodes an (album, artist) selection tuple as a single string
    key for durable storage in sync_marks (item_type='album')."""
    return f"{album or ''}{_ALBUM_KEY_SEP}{artist or ''}"


def _album_sync_key_decode(key):
    """Reverses _album_sync_key. Returns (album, artist), each None if
    the stored value was empty (matching how the tuple keys elsewhere
    use None for unknown album/artist, not an empty string)."""
    album, _sep, artist = key.partition(_ALBUM_KEY_SEP)
    return (album or None, artist or None)


def _track_dup_key(title, artist, album, duration_ms):
    """Same matching convention as Library.find_duplicate_groups():
    title/artist/album (case-insensitive, whitespace-trimmed) plus
    duration. Used to tell whether a local track already appears to be
    on the device, since device dbids are assigned by the device itself
    and can't be compared to local library ids directly."""
    return (
        (title or "").strip().lower(),
        (artist or "").strip().lower(),
        (album or "").strip().lower(),
        duration_ms or 0,
    )


def _video_dup_key(title, duration_ms):
    """Same idea as _track_dup_key, for videos - matches the heuristic
    _process_pending_video_removals already uses (title + duration)."""
    return ((title or "").strip().lower(), duration_ms or 0)


def _row_get(row, key, default=None):
    """Safe field access that works for both a sqlite3.Row (from the
    local library - doesn't support .get(), raises IndexError for a
    column that doesn't exist) and a plain dict (the on-device-track
    adapter, _device_track_to_video_row) - used for columns like
    show_fanart_path that a dict built before this feature existed
    might not have."""
    if row is None or key not in row.keys():
        return default
    value = row[key]
    return value if value is not None else default


# Apple-style album grid: covers load over a neutral gray swatch and
# card (like Apple Music), so nothing flashes a random color first.
APPLE_PLACEHOLDER_ART = "#dcdce1"
APPLE_PLACEHOLDER_CARD = "#f2f2f5"

_LEADING_NUMBER_RES = (
    re.compile(r"^\s*[\(\[]\s*(\d{1,3})\s*[\)\]]"),   # "(01) Fat", "[3] Fat"
    re.compile(r"^\s*(\d{1,3})\s*[-._:)]\s*\S"),         # "01 - Fat", "1. Fat"
    re.compile(r"^\s*(0\d)\s+\S"),                        # "01 Fat" (not "99 Luftballons")
)


def _guess_track_number(title, filepath):
    """"(01) - Fat", "01 - Fat", "1. Fat" or a file named that way -> "1"."""
    for text in (title or "", os.path.splitext(os.path.basename(filepath or ""))[0]):
        for rx in _LEADING_NUMBER_RES:
            m = rx.match(text)
            if m and int(m.group(1)) > 0:
                return str(int(m.group(1)))
    return ""


class MainApp(tk.Tk):
    VIEW_LIBRARY = "library"
    VIEW_LOCAL_PLAYLIST = "local_playlist"
    VIEW_DEVICE = "device"
    VIEW_DEVICE_PLAYLIST = "device_playlist"
    VIEW_ALBUM_GRID = "album_grid"
    VIEW_PODCASTS = "podcasts"
    VIEW_ALBUM_TRACKS = "album_tracks"
    VIEW_ARTISTS = "artists"
    VIEW_ARTIST_TRACKS = "artist_tracks"
    VIEW_GENRES = "genres"
    VIEW_GENRE_TRACKS = "genre_tracks"
    VIEW_RECENTLY_ADDED = "recently_added"
    VIEW_MOVIES = "movies"
    VIEW_TVSHOWS = "tvshows"
    VIEW_PHOTOS = "photos"
    VIEW_PLAYLIST_LIST = "playlist_list"
    VIEW_IPOD_INFO = "ipod_info"
    VIEW_SETTINGS = "settings"

    # Every view that renders ordinary local-library tracks into
    # self.table (as opposed to device tracks, the album grid, an
    # artist/genre list, or Settings) - these all support select-all,
    # the right-click menu's playlist/sync actions, and dragging tracks
    # out to the sidebar.
    LOCAL_TRACK_VIEWS = (
        VIEW_LIBRARY, VIEW_LOCAL_PLAYLIST, VIEW_ALBUM_TRACKS,
        VIEW_ARTIST_TRACKS, VIEW_GENRE_TRACKS, VIEW_RECENTLY_ADDED,
    )

    def report_callback_exception(self, exc, val, tb):
        """Errors inside button/timer callbacks: Tk only prints these to
        the terminal, so also put them in the app log."""
        applog.log("UI ERROR:\n" + "".join(traceback.format_exception(exc, val, tb)))
        super().report_callback_exception(exc, val, tb)

    def __init__(self):
        super().__init__(className="Linuxpod")
        self.title("Linuxpod")
        self.geometry("1200x720")
        apple_style.init_fonts(self)

        applog.log("=== Linuxpod started ===")
        applog.log(f"Python {sys.version.split()[0]}, Tk {self.tk.call('info', 'patchlevel')}")

        self.lib = library.Library()
        self.work_queue = queue.Queue()
        self.app_settings = settings.load()
        # The iTunes/Apple-style look is the only look now; the plain
        # one (and its Appearance toggle) is gone.
        self.app_settings["itunes_theme"] = True
        apple_style.set_window_options(self, self.app_settings.get("itunes_theme", False))
        self._default_ttk_theme = ttk.Style().theme_use()
        # Opens maximized, as a normal window with its title bar and
        # borders (Settings > Appearance turns it off). F11 still gives
        # true borderless full screen.
        self.bind_all("<F11>", self.toggle_fullscreen)
        if self.app_settings.get("start_maximized", True):
            self.after_idle(lambda: self._set_maximized(True))

        self.current_view = self.VIEW_LIBRARY
        self.current_local_playlist_id = None
        self.current_device_playlist_name = None
        self.current_album_key = None
        self.current_artist = None
        self._album_tile_sizes = {}
        self._photo_dedupe_only = False
        # Local track ids unticked in the device view this visit; Done
        # removes them from the iPod (see _remove_unticked_from_device).
        self._unticked_track_ids = set()
        # {iPod dbid: library track id} for the connected iPod - see
        # Library.link_device_tracks. Only linked tracks are ever
        # removed automatically.
        self.device_track_links = {}
        # Albums unticked in the device view, removed from the iPod a
        # moment later (see _remove_unticked_albums_now).
        self._albums_to_remove = set()
        self._albums_remove_after_id = None
        self.current_genre = None
        self._device_view_active = False  # see show_ipod_info / close_device_view
        self._auto_synced_this_connection = False  # see _set_device / _auto_sync_if_enabled
        self._model_asked_for = set()  # mountpoints asked for a model number this run

        self._album_tile_widgets = {}   # (album, artist) -> image Label widget
        self._album_photo_cache = {}    # (album, artist) -> ImageTk.PhotoImage (must keep a ref)
        self._placeholder_photo_cache = {}  # color -> ImageTk.PhotoImage
        self._album_tile_frames_by_key = {}  # (album, artist) -> tile Frame widget
        self._album_tile_labels = {}    # (album, artist) -> (title Label, subtitle Label)
        self._album_tints = {}          # (album, artist) -> card color picked from its cover art
        self._font_cache = {}           # font tuple -> (tkfont.Font, char widths), for _ellipsize
        self._album_tile_key_by_widget = {}  # any tile sub-widget -> (album, artist), for drag source lookup
        self._selected_albums = set()   # {(album, artist), ...} currently selected in the grid
        self._persistent_table_selection = set()  # {"t:5", "d:123", ...} - survives table repopulation
        # (navigating between Songs/Albums/Genres/Artists/Playlists, e.g.
        # in the device view sidebar) so selections don't vanish just
        # from clicking around; kept in sync via _on_table_selection_changed
        # and re-applied at the end of _populate_table_local/_device.
        self._name_list_selection = {}  # tree widget -> remembered selected name (Artists/Genres lists)
        self._selected_playlist_ids = set()  # {playlist_id, ...} - persistent, for the multi-select playlist view

        self._drag_data = None       # in-progress click-and-drag payload, or None between drags
        self._drag_indicator = None  # small floating Toplevel shown while dragging
        self._last_playlist_id = None  # last playlist chosen via the picker dialog, for quick re-add
        self._pending_click_iid = None  # see _on_table_press
        self._pending_click_key = None  # see _on_tile_press (same idea, for album tiles)

        self.ipod_mountpoint = None
        self.ipod_label = None
        self.device_tracks = []
        self.device_movies = []
        self.device_tvshows = []
        self.device_photo_albums = []
        self.device_playlists = []
        self._suppress_automount = False
        self._active_sync_handle = None
        # A threading.Event, set while Add Video Folder/Add Video is
        # running (converting/copying files in a background thread) -
        # the Cancel button (see cancel_sync) sets it to stop that
        # batch, the same way it stops an active device sync via
        # _active_sync_handle. None whenever no video add is running.
        self._active_video_add_cancel = None
        # Set just before a reconciling-sync removal step starts (see
        # _remove_dbids_then / the "remove_done" queue handler below) -
        # called once, then cleared, so a removal that's just one half
        # of "remove what's stale, then add what's new" can chain into
        # its own add step without the two device-writing operations
        # ever running as overlapping subprocesses.
        self._post_remove_callback = None

        self._build_menu()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()

        self.refresh_sidebar()
        self.show_library()
        self._apply_theme(self.app_settings.get("itunes_theme", False))
        if self.app_settings.get("itunes_theme", False):
            apple_style.fade_in_window(self)
        self._bind_mouse_back_button()
        self.after(200, self._poll_queue)
        self.after(500, self._space_meter_tick)
        # Auto-refresh everything (music/video/photo folders) shortly
        # after startup, quietly skipping the "nothing to refresh yet"
        # popup if no folders have been added at all - see
        # refresh_library(silent=True).
        self.after(1500, lambda: self.refresh_library(silent=True))
        # One-time check, shortly after the window opens, for an iPod
        # that's already mounted (plugged in before the app was
        # started, or auto-mounted by the desktop environment on
        # connect) - so it just appears instead of requiring a manual
        # iPod -> Detect / Auto-Mount iPod first. This is a single
        # one-shot check, NOT the continuous background poll that used
        # to run via _poll_for_ipod - that was tried in an earlier
        # session and explicitly reverted at the user's request, and
        # stays reverted.
        self.after(1000, self._detect_ipod_on_startup)
        self.after(600, lambda: self._refresh_spoofify(quiet=True))
        # Covers a *later* plug-in: a background thread blocks on the
        # kernel telling it the system's mount table changed (see
        # ipod_backend.wait_for_mount_table_change) and wakes up only
        # when something actually gets mounted or unmounted, anywhere
        # - not a timer running a check on a fixed interval regardless
        # of whether anything happened, which is specifically what got
        # reverted before. Debounced on the Tk side (see
        # _mount_change_debounce_id) so several mounts landing in a
        # quick burst (e.g. multiple partitions of the same device)
        # collapse into one check instead of one per event.
        self._mount_change_debounce_id = None
        self._start_mount_watcher()
        # Guards refresh_library() against running twice at once (the
        # silent startup auto-refresh above and a manual click of
        # Settings -> Refresh Library can otherwise overlap) - see
        # refresh_library's own comment on why that's worth avoiding
        # beyond just wasted work: two overlapping
        # self.progress.start(10) calls can leave the progress bar
        # stuck animating forever, since the second start() silently
        # orphans the first one's still-running animation loop, and
        # a single stop() call afterward only ever cancels the most
        # recent one.
        self._refresh_in_progress = False

    def _bind_mouse_back_button(self):
        """A mouse/trackball's "back" thumb button - X11 reports it as
        Button-8 (Button-9 is "forward", not used here) on the vast
        majority of setups, but some desktop environments intercept it
        and deliver it as the XF86Back keysym instead - bound too so it
        works either way. bind_all (not tied to any one widget) since
        this should work no matter what part of the window has focus,
        the same way a browser's back button isn't limited to one pane.
        This app has no general page-history stack, so "back" mirrors
        whichever specific Back/Done control the current screen already
        has, rather than a literal previous-screen history - see
        _handle_mouse_back."""
        for sequence in ("<Button-8>", "<XF86Back>"):
            try:
                self.bind_all(sequence, self._handle_mouse_back)
            except tk.TclError:
                pass

    def _handle_mouse_back(self, event=None):
        if self.library_mode == "tvshows" and self._tvshow_grid_level == "episodes":
            self._show_tvshow_group_grid()
            return "break"
        if self._device_view_active:
            self.close_device_view()
            return "break"
        return None
        self.after(800, self._poll_for_ipod)

    # ---------------------------------------------------------- layout --

    def _build_menu(self):
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Add Folder to Library…", command=self.add_folder)
        file_menu.add_command(label="Add Files to Library…", command=self.add_files)
        file_menu.add_command(label="Add Podcast from URL…", command=self.add_podcast_from_url)
        file_menu.add_separator()
        file_menu.add_command(label="Clear Library…", command=self.clear_library)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self.destroy)
        menubar.add_cascade(label="File", menu=file_menu)

        ipod_menu = tk.Menu(menubar, tearoff=0)
        ipod_menu.add_command(label="Detect / Auto-Mount iPod", command=self.auto_detect_ipod)
        ipod_menu.add_command(label="Choose iPod Folder…", command=self.choose_ipod_manually)
        ipod_menu.add_command(label="Refresh iPod Library", command=self.refresh_device_library)
        ipod_menu.add_command(label="Rename iPod…", command=self.rename_ipod_dialog)
        ipod_menu.add_command(label="Remove Duplicate Photos", command=self.remove_duplicate_photos)
        ipod_menu.add_command(label="Clean Up Leftover Files\u2026", command=self.check_leftover_files)
        ipod_menu.add_separator()
        ipod_menu.add_command(label="Eject iPod", command=self.eject_ipod)
        menubar.add_cascade(label="iPod", menu=ipod_menu)

        edit_menu = tk.Menu(menubar, tearoff=0)
        edit_menu.add_command(label="Settings…", command=self.open_settings)
        edit_menu.add_separator()
        edit_menu.add_command(label="Select All", accelerator="Ctrl+A", command=self.select_all_tracks)
        edit_menu.add_command(label="Select None", command=self.select_none_tracks)
        edit_menu.add_separator()
        edit_menu.add_command(label="Get Info…", accelerator="Ctrl+I", command=self.get_info_for_selection)
        edit_menu.add_separator()
        edit_menu.add_command(label="Full Screen", accelerator="F11", command=self.toggle_fullscreen)
        menubar.add_cascade(label="Edit", menu=edit_menu)

        # A plain command, not a cascade with a dropdown - sits directly
        # in the menu bar, right next to Help. A menu bar can't cleanly
        # add/remove an entry at runtime across platforms the way a
        # toolbar button can be packed/unpacked, so this stays present
        # and is enabled/disabled instead (see _update_ipod_info_button).
        self.device_menu_index = menubar.index(tk.END) + 1
        menubar.add_command(
            label="View Connected Device", command=self.show_ipod_info, state="disabled"
        )

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="Open Log File", command=self.open_log_file)
        help_menu.add_command(label="Show Log File Location", command=self.show_log_path)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.config(menu=menubar)
        self.menubar = menubar

    def _build_toolbar(self):
        bar = ttk.Frame(self, padding=(4, 8, 4, 8))
        bar.pack(side=tk.TOP, fill=tk.X, padx=6)
        self.toolbar_bar = bar

        # Page title shown at the left of this bar in the Apple-style
        # layout, where the bar becomes the content pane's header (see
        # _layout_toolbar); unpacked in the classic layout.
        self.header_title_var = tk.StringVar(value="Songs")
        self.header_sub_var = tk.StringVar(value="")
        self.header_box = ttk.Frame(bar, style="Content.TFrame")
        self.header_title = ttk.Label(self.header_box, textvariable=self.header_title_var, style="Header.TLabel")
        self.header_title.pack(anchor="w")
        ttk.Label(self.header_box, textvariable=self.header_sub_var, style="HeaderSub.TLabel").pack(anchor="w")

        self.add_music_button = ttk.Button(bar, text="Add Music…", command=self.add_folder)
        self.clear_library_button = ttk.Button(bar, text="Clear Library", command=self.clear_library)
        self.new_playlist_button = ttk.Button(bar, text="New Playlist", command=self.new_local_playlist)
        self.select_all_button = ttk.Button(bar, text="Select All", command=self.select_all_tracks)

        # Not packed in the classic layout's default state — only shown
        # in the Apple-style one, to visually separate library actions.
        self.toolbar_separator = ttk.Separator(bar, orient="vertical")

        # The iPod controls are children of the window itself rather
        # than of this bar, so the Apple-style layout can move them into
        # the sidebar's device card (pack(in_=...) only works into the
        # widget's parent or one of its descendants).
        self.sync_button = ttk.Button(self, text="Sync Selected \u2192 iPod", command=self.sync_selected)

        self.cancel_sync_button = ttk.Button(
            self, text="Cancel Sync", command=self.cancel_sync, state="disabled"
        )

        self.remove_button = ttk.Button(bar, text="Remove from iPod", command=self.remove_selected_from_device)

        self.eject_button = ttk.Button(self, text="Eject iPod", command=self.eject_ipod)

        # Not packed here - only shown while the device view (see
        # show_ipod_info) is open; see _update_sync_all_button(), called
        # from refresh_sidebar().
        self.sync_all_button = ttk.Button(
            self, text="Sync All", command=self.sync_all_to_ipod
        )

        self.search_label = ttk.Label(bar, text="Search:")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.on_search())
        self.search_entry = ttk.Entry(bar, textvariable=self.search_var, width=25)
        self._layout_toolbar(False)

    def _layout_toolbar(self, apple):
        """Packs the toolbar's widgets for the classic layout (one row
        of buttons across the top of the window) or the Apple-style one
        modeled on iOpenPod: the bar becomes a header over the content
        pane - page title on the left, library actions and search on the
        right - and the iPod controls move into the sidebar's device
        card (see _layout_device_card)."""
        bar = self.toolbar_bar
        for w in (self.header_box, self.add_music_button, self.clear_library_button,
                  self.new_playlist_button, self.select_all_button, self.toolbar_separator,
                  self.sync_all_button, self.sync_button, self.cancel_sync_button,
                  self.remove_button, self.eject_button, self.search_label, self.search_entry):
            w.pack_forget()
        bar.pack_forget()
        body = getattr(self, "body_frame", None)
        if body is not None:
            body.pack_configure(pady=((10, 0) if apple else 0))
        if apple:
            main = getattr(self, "main_area", None)
            if main is not None:
                slaves = main.pack_slaves()
                if slaves:
                    bar.pack(in_=main, side=tk.TOP, fill=tk.X, before=slaves[0])
                else:
                    bar.pack(in_=main, side=tk.TOP, fill=tk.X)
                bar.lift()
            bar.configure(padding=(16, 12, 10, 10))
            self.header_box.pack(side=tk.LEFT)
            # Search lives in the window's top bar here (see _build_topbar).
            for w in (self.remove_button, self.select_all_button, self.new_playlist_button,
                      self.clear_library_button, self.add_music_button):
                w.pack(side=tk.RIGHT, padx=(6, 0))
        else:
            bar.configure(padding=(4, 8, 4, 8))
            body = getattr(self, "body_frame", None)
            if body is not None:
                bar.pack(side=tk.TOP, fill=tk.X, padx=6, before=body)
            else:
                bar.pack(side=tk.TOP, fill=tk.X, padx=6)
            self.search_label.configure(text="Search:")
            self.search_entry.configure(width=25)
            for w in (self.add_music_button, self.clear_library_button,
                      self.new_playlist_button, self.select_all_button):
                w.pack(in_=bar, side=tk.LEFT, padx=2)
            self.sync_button.pack(in_=bar, side=tk.LEFT, padx=(20, 2))
            self.cancel_sync_button.pack(in_=bar, side=tk.LEFT, padx=2)
            self.remove_button.pack(side=tk.LEFT, padx=2)
            self.eject_button.pack(in_=bar, side=tk.LEFT, padx=2)
            self.search_label.pack(side=tk.LEFT, padx=(20, 4))
            self.search_entry.pack(side=tk.LEFT)
            for w in (self.sync_button, self.cancel_sync_button, self.eject_button, self.sync_all_button):
                w.lift()
        self._update_sync_all_button_if_built()

    def _update_sync_all_button_if_built(self):
        if hasattr(self, "sidebar"):
            self._update_sync_all_button()

    def _build_body(self):
        body = ttk.Frame(self)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6)
        self.body_frame = body

        # --- sidebar ---
        sidebar_frame = ttk.Frame(body, width=220)
        sidebar_frame.pack(side=tk.LEFT, fill=tk.Y)
        sidebar_frame.pack_propagate(False)
        self.sidebar_frame = sidebar_frame

        # Switches which "library" the sidebar/content area show: Music
        # (everything the app has always done), or Movies / TV Shows
        # (the new video library). A button rather than a menu bar
        # entry so it's always visible right above the content it
        # controls, expanding into the three choices on click.
        self.library_mode = "music"
        self.library_mode_button = ttk.Menubutton(sidebar_frame, text="\U0001F3B5 Music")
        self.library_mode_button.pack(fill=tk.X, pady=(0, 4))
        library_mode_menu = tk.Menu(self.library_mode_button, tearoff=0)
        library_mode_menu.add_command(label="\U0001F3B5 Music", command=lambda: self.set_library_mode("music"))
        library_mode_menu.add_command(label="\U0001F3AC Movies", command=lambda: self.set_library_mode("movies"))
        library_mode_menu.add_command(label="\U0001F4FA TV Shows", command=lambda: self.set_library_mode("tvshows"))
        library_mode_menu.add_command(label="\U0001F4F7 Photos", command=lambda: self.set_library_mode("photos"))
        library_mode_menu.add_command(label="\U0001F3A7 Podcasts", command=lambda: self.set_library_mode("podcasts"))
        self.library_mode_button.configure(menu=library_mode_menu)

        # Shown in the same spot instead of library_mode_button while
        # Settings is open (see _update_sidebar_mode_switcher) - the
        # sidebar below it is showing Settings' own category tabs at
        # that point, not a library tree, so a Music/Movies/TV/Photos
        # switcher there would be confusing (and would just leave
        # Settings anyway). Packed with before=self.sidebar so it
        # always lands in the same slot above the tree regardless of
        # pack/pack_forget order.
        self.settings_back_button = ttk.Button(
            sidebar_frame, text="← Back", command=self._exit_settings
        )

        self.sidebar = ttk.Treeview(sidebar_frame, show="tree", selectmode="browse")
        self.sidebar.pack(fill=tk.BOTH, expand=True)
        # A scrollbar that appears only when the list is taller than the
        # space left above the device card, so nothing ends up out of reach.
        self.sidebar_scrollbar = ttk.Scrollbar(sidebar_frame, orient=tk.VERTICAL, command=self.sidebar.yview)

        def sidebar_yscroll(first, last):
            self.sidebar_scrollbar.set(first, last)
            needed = float(first) > 0.0 or float(last) < 1.0
            if needed and not self.sidebar_scrollbar.winfo_ismapped():
                self.sidebar_scrollbar.pack(side=tk.RIGHT, fill=tk.Y, before=self.sidebar)
            elif not needed and self.sidebar_scrollbar.winfo_ismapped():
                self.sidebar_scrollbar.pack_forget()

        self.sidebar.configure(yscrollcommand=sidebar_yscroll)
        self.sidebar.bind("<<TreeviewSelect>>", self.on_sidebar_select)
        self.sidebar.bind("<Button-3>", self.on_sidebar_right_click)

        # --- main area: table and album grid share this space, one visible at a time ---
        self.main_area = ttk.Frame(body)
        self.main_area.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))

        # Shown across the whole device view (Summary, Music/Songs,
        # Albums, Genres, Playlists, the placeholders - all of it),
        # not just while looking at actual on-device tracks, so
        # there's always a visible cue that you're managing the iPod
        # rather than browsing your local library. Toggled in
        # _show_body(), which already runs on every device-view screen
        # change, so this doesn't need separate wiring per screen.
        self.device_view_banner = tk.Label(
            self.main_area, text="\U0001F4F1 Managing iPod",
            anchor="w", padx=10, pady=4,
            font=(apple_style.FAMILY, 9, "bold"),
            background="#f2d9a0", foreground="#4a3a10",
        )

        self.table_frame = ttk.Frame(self.main_area)

        # Shown only while displaying device tracks (see
        # _populate_table_device / _populate_table_local /
        # _set_device_banner) - the one, unmissable visual cue that
        # distinguishes "On iPod" content from the local library, on
        # top of the row-color difference below.
        self.device_banner = tk.Label(
            self.table_frame, text="\U0001F4F1 On iPod",
            anchor="w", padx=10, pady=4,
            font=(apple_style.FAMILY, 9, "bold"),
            background="#f2d9a0", foreground="#4a3a10",
        )

        self.table_row = ttk.Frame(self.table_frame)
        self.table_row.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        columns = ("title", "artist", "album", "genre", "time", "track", "year", "plays", "lastplayed")
        self.table = ttk.Treeview(self.table_row, columns=columns, show="headings", selectmode="extended")
        headings = {
            "title": ("Title", 220), "artist": ("Artist", 160), "album": ("Album", 160),
            "genre": ("Genre", 100), "time": ("Time", 60), "track": ("#", 40), "year": ("Year", 60),
            "plays": ("Plays", 50), "lastplayed": ("Last Played", 120),
        }
        for col, (text, width) in headings.items():
            self.table.heading(col, text=text, anchor=tk.W, command=lambda c=col: self.sort_by(c))
            self.table.column(col, width=width, anchor=tk.W)

        vsb = ttk.Scrollbar(self.table_row, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=vsb.set)
        self.table.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)

        # Device rows get their own warm tan striping, distinct from the
        # library's white/pale-blue (below and in _apply_theme) - kept
        # constant across the iTunes-theme toggle, unlike evenrow/oddrow,
        # so device content always reads differently at a glance no
        # matter which appearance setting is active.
        self.table.tag_configure("device_evenrow", background="#fdf3e3")
        self.table.tag_configure("device_oddrow", background="#f7e8c9")

        self.table.bind("<Button-3>", self.on_right_click)
        self.table.bind("<Control-a>", self.select_all_tracks)
        self.table.bind("<Control-A>", self.select_all_tracks)
        self.table.bind("<Control-i>", self.get_info_for_selection)
        self.table.bind("<Control-I>", self.get_info_for_selection)
        self.table.bind("<ButtonPress-1>", self._on_table_press)
        self.table.bind("<B1-Motion>", self._on_drag_motion)
        self.table.bind("<ButtonRelease-1>", self._on_drag_release)
        self.table.bind("<<TreeviewSelect>>", self._on_table_selection_changed)

        self._build_album_grid()
        self._build_settings_panel()
        self._build_name_list_panels()
        self._build_ipod_info_panel()
        self._build_placeholder_panel()
        self._build_video_list_panel()
        self._build_photo_grid_panel()
        self._build_device_photo_albums_panel()
        self._build_playlist_list_panel()

        self.table_frame.pack(fill=tk.BOTH, expand=True)  # visible by default

    def _build_album_grid(self):
        """A scrollable canvas of album-art tiles, iTunes Grid-View style."""
        self.grid_frame = ttk.Frame(self.main_area)

        canvas = tk.Canvas(self.grid_frame, highlightthickness=0)
        self._default_canvas_bg = canvas.cget("bg")
        vsb = ttk.Scrollbar(self.grid_frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)

        self.grid_canvas = canvas
        self.grid_inner = ttk.Frame(canvas)
        self._grid_window = canvas.create_window((0, 0), window=self.grid_inner, anchor="nw")

        def on_inner_configure(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def on_canvas_configure(event):
            canvas.itemconfig(self._grid_window, width=event.width)
            self._reflow_album_grid()

        self.grid_inner.bind("<Configure>", on_inner_configure)
        canvas.bind("<Configure>", on_canvas_configure)

        def on_mousewheel(event):
            # X11 reports the wheel as Button-4 (up) / Button-5 (down)
            # rather than a MouseWheel delta - event.delta is always 0
            # here, so the previous "event.delta > 0" check never did
            # anything and this ended up scrolling the opposite way of
            # every other app: wheel-up (Button-4) should move the view
            # up (yview_scroll(-1)), wheel-down (Button-5) should move
            # it down (+1).
            delta = -1 if event.num == 4 else 1
            apple_style.smooth_scroll(canvas, delta, self._smooth_motion())

        canvas.bind("<Enter>", lambda e: (
            canvas.bind_all("<MouseWheel>", lambda ev: apple_style.smooth_scroll(
                canvas, -1 if ev.delta > 0 else 1, self._smooth_motion())),
            canvas.bind_all("<Button-4>", on_mousewheel),
            canvas.bind_all("<Button-5>", on_mousewheel),
        ))
        canvas.bind("<Leave>", lambda e: (
            canvas.unbind_all("<MouseWheel>"),
            canvas.unbind_all("<Button-4>"),
            canvas.unbind_all("<Button-5>"),
        ))

        self._album_rows = []          # cached list of library.Row, current album set
        self._album_tile_frames = []   # tile widgets created for the current album set, in order

    def _build_settings_panel(self):
        """Settings is a set of categories, one visible at a time (see
        _show_settings_category), picked from the sidebar's own tabs
        while Settings is open (see _refresh_settings_sidebar) instead
        of one long scrolling page - the sidebar's usual Music/Movies/
        TV/Photos switcher becomes a Back button meanwhile (see
        _update_sidebar_mode_switcher)."""
        self.settings_frame = ttk.Frame(self.main_area)
        self._settings_category_frames = {}

        self._build_settings_general_category()
        self._build_settings_video_category()
        self._build_settings_podcasts_category()
        self._build_settings_spoofify_category()
        self._build_settings_appearance_category()

        self._current_settings_category = "general"

    def _new_settings_category(self, key, title):
        frame = ttk.Frame(self.settings_frame, padding=20)
        self._settings_category_frames[key] = frame
        ttk.Label(frame, text=title, font=(apple_style.FAMILY, 18, "bold")).pack(
            anchor="w", pady=(0, 16)
        )
        return frame

    def _show_settings_category(self, key):
        if key not in self._settings_category_frames:
            key = "general"
        self._current_settings_category = key
        for frame in self._settings_category_frames.values():
            frame.pack_forget()
        self._settings_category_frames[key].pack(side=tk.TOP, fill=tk.BOTH, expand=True, anchor="n")
        if key == "general":
            self._populate_library_folders_tree()
            self._update_removed_songs_count()
        elif key == "video":
            self._populate_video_folders_tree()
            self._update_removed_videos_count()
        elif key == "podcasts":
            self._populate_podcast_shows_tree()
        # Deliberately doesn't also call self.sidebar.selection_set()
        # here: this is called FROM on_sidebar_select() when the user
        # clicks a tab (selection is already set by the click itself),
        # and selection_set() fires <<TreeviewSelect>> unconditionally
        # even when nothing actually changed - calling it here would
        # re-trigger on_sidebar_select(), which calls back into this
        # method, forever. _refresh_settings_sidebar() is the one place
        # that sets the initial selection when Settings first opens.

    def _build_settings_general_category(self):
        frame = self._new_settings_category("general", "General")

        lib_section = ttk.LabelFrame(frame, text="Library maintenance", padding=12)
        lib_section.pack(fill=tk.X, pady=(0, 16))
        ttk.Button(lib_section, text="Refresh Library", command=self.refresh_library).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(lib_section, text="Remove Duplicate Tracks", command=self.remove_duplicate_tracks).pack(
            side=tk.LEFT
        )

        removed_row = ttk.Frame(frame)
        removed_row.pack(fill=tk.X, pady=(0, 16))
        self.removed_songs_count_var = tk.StringVar(value="")
        ttk.Label(removed_row, textvariable=self.removed_songs_count_var, foreground="#666666").pack(
            side=tk.LEFT
        )
        ttk.Button(
            removed_row, text="Let Refresh Add Them Back", command=self._clear_removed_songs
        ).pack(side=tk.RIGHT)
        self._update_removed_songs_count()

        folders_section = ttk.LabelFrame(frame, text="Library Folders", padding=12)
        folders_section.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            folders_section,
            text="Every folder Refresh Library scans. Remove one you've moved or deleted "
                 "to stop the \"could not be found\" warning, or use Change Folder if it "
                 "just moved. Removing a folder never deletes any files.",
            foreground="#666666", wraplength=520, justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 8))

        tree_row = ttk.Frame(folders_section)
        tree_row.pack(fill=tk.BOTH, expand=True)
        tree = ttk.Treeview(tree_row, columns=("kind", "path", "status"), show="headings",
                            selectmode="extended", height=8)
        tree.heading("kind", text="Type")
        tree.heading("path", text="Folder")
        tree.heading("status", text="")
        tree.column("kind", width=80, stretch=False, anchor=tk.W)
        tree.column("path", width=380, anchor=tk.W)
        tree.column("status", width=90, stretch=False, anchor=tk.W)
        tree.tag_configure("missing", foreground="#d70015")
        vsb = ttk.Scrollbar(tree_row, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)
        self.library_folders_tree = tree

        btn_row = ttk.Frame(folders_section)
        btn_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(btn_row, text="Change Folder…", command=self._change_library_folder).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(btn_row, text="Remove", command=self._remove_selected_library_folders).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(btn_row, text="Remove Missing Folders", command=self._remove_missing_library_folders).pack(
            side=tk.LEFT
        )

    # Library Folders (Settings > General): music, photo and video folders
    # in one list; _library_folder_iids maps each row to its (kind, path).
    _FOLDER_KIND_LABELS = {"music": "Music", "photos": "Photos", "movie": "Movies", "tvshow": "TV Shows"}

    def _library_folder_rows(self):
        rows = [("music", p) for p in self.lib.all_source_folders()]
        rows += [("photos", r["path"]) for r in self.lib.all_photo_source_folders()]
        rows += [(kind, path) for path, kind in self.lib.all_video_source_folders()]
        return rows

    def _populate_library_folders_tree(self):
        tree = getattr(self, "library_folders_tree", None)
        if tree is None:
            return
        tree.delete(*tree.get_children())
        self._library_folder_iids = {}
        for kind, path in self._library_folder_rows():
            missing = not os.path.isdir(path)
            iid = tree.insert("", "end",
                        values=(self._FOLDER_KIND_LABELS.get(kind, kind), path,
                                "Not found" if missing else ""),
                        tags=("missing",) if missing else ())
            self._library_folder_iids[iid] = (kind, path)

    def _forget_library_folders(self, folders):
        """folders: [(kind, path)]. Stops scanning them, and offers to drop
        their songs from the library too (handy when the folder is gone)."""
        music = [p for k, p in folders if k == "music"]
        track_ids = [i for p in music for i in self.lib.track_ids_in_folder(p)]
        drop_tracks = False
        if track_ids:
            answer = messagebox.askyesnocancel(
                "Remove Folder" if len(folders) == 1 else "Remove Folders",
                f"Stop scanning {len(folders)} folder(s)?\n\n"
                f"{len(track_ids)} song(s) in your library came from "
                f"{'it' if len(music) == 1 else 'them'}. Remove those songs from the library too?\n\n"
                "Yes removes the songs from the library (the files stay on disk). "
                "No keeps them listed.",
            )
            if answer is None:
                return
            drop_tracks = answer
        elif not messagebox.askyesno(
            "Remove Folder" if len(folders) == 1 else "Remove Folders",
            f"Stop scanning {len(folders)} folder(s)? No files are deleted.",
        ):
            return
        for kind, path in folders:
            if kind == "music":
                self.lib.remove_source_folder(path)
            elif kind == "photos":
                self.lib.remove_photo_source_folder(path)
            else:
                self.lib.remove_video_source_folder(path)
            applog.log(f"SETTINGS: stopped tracking {kind} folder {path}")
        if drop_tracks:
            self.lib.remove_tracks(track_ids)
            applog.log(f"SETTINGS: removed {len(track_ids)} track(s) from removed folder(s)")
        self._populate_library_folders_tree()
        self.status_var.set(
            f"Stopped scanning {len(folders)} folder(s)"
            + (f" and removed {len(track_ids)} song(s) from the library." if drop_tracks else ".")
        )

    def _selected_library_folders(self):
        return [self._library_folder_iids[iid] for iid in self.library_folders_tree.selection()
                if iid in self._library_folder_iids]

    def _remove_selected_library_folders(self):
        folders = self._selected_library_folders()
        if not folders:
            messagebox.showinfo("No Folder Selected", "Select a folder in the list first.")
            return
        self._forget_library_folders(folders)

    def _remove_missing_library_folders(self):
        folders = [(k, p) for k, p in self._library_folder_rows() if not os.path.isdir(p)]
        if not folders:
            messagebox.showinfo("Nothing Missing", "Every library folder is where it should be.")
            return
        self._forget_library_folders(folders)

    def _change_library_folder(self):
        folders = self._selected_library_folders()
        if len(folders) != 1:
            messagebox.showinfo("Select One Folder", "Select the one folder that moved.")
            return
        kind, old_path = folders[0]
        new_path = native_dialogs.choose_directory(title="Choose the New Location for This Folder")
        if not new_path or new_path == old_path:
            return
        try:
            if kind == "music":
                self.lib.move_source_folder(old_path, new_path)
            elif kind == "photos":
                self.lib.update_photo_source_folder_path(old_path, new_path)
            else:
                self.lib.update_video_source_folder_path(old_path, new_path)
        except Exception as e:
            messagebox.showerror(
                "Couldn't Change Folder",
                f"{e}\n\n(Songs from the new location may already be in your library on "
                "their own; remove this entry instead, then Refresh Library.)",
            )
            return
        applog.log(f"SETTINGS: {kind} folder moved from {old_path} to {new_path}")
        self._populate_library_folders_tree()
        self.status_var.set(
            "Folder updated \u2014 "
            + ("use Settings \u2192 Video \u2192 Scan Video Folders Now" if kind in ("movie", "tvshow")
               else "Refresh Library")
            + " to scan the new location."
        )

    def _build_settings_video_category(self):
        frame = self._new_settings_category("video", "Video")

        conv_section = ttk.LabelFrame(frame, text="Video Conversion", padding=12)
        conv_section.pack(fill=tk.X, pady=(0, 16))
        ttk.Label(
            conv_section,
            text="Videos that aren't already iPod-compatible get converted (via ffmpeg)\n"
                 "to H.264/AAC .m4v, then get a special atom added (via AtomicParsley) that\n"
                 "5th/5.5-gen iPods specifically need before they'll actually display video -\n"
                 "without it, a file can look fully compliant and still show a black screen.\n"
                 "Converted copies are saved here, separate from your original files:",
            justify=tk.LEFT,
        ).pack(anchor="w")
        video_folder_row = ttk.Frame(conv_section)
        video_folder_row.pack(fill=tk.X, pady=(8, 0))
        self.converted_videos_folder_var = tk.StringVar(
            value=self.app_settings.get("converted_videos_folder", "") or "(not set)"
        )
        ttk.Label(video_folder_row, textvariable=self.converted_videos_folder_var, foreground="#666666").pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        ttk.Button(video_folder_row, text="Choose Folder\u2026", command=self.choose_converted_videos_folder).pack(
            side=tk.RIGHT
        )

        folders_section = ttk.LabelFrame(frame, text="Tracked Video Folders", padding=12)
        folders_section.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            folders_section,
            text="Folders added via Add Video Folder (Movies/TV Shows). Refresh Library "
                 "leaves them alone so it never starts slow conversions; use Scan Video "
                 "Folders Now to pick up and convert new videos. Removing one here just "
                 "stops tracking it; it doesn't delete anything already added to your "
                 "library or already synced to an iPod. Changing one points future "
                 "refreshes at a new location (e.g. after moving the folder) without "
                 "touching videos already added from the old one.",
            foreground="#666666", wraplength=520, justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 8))

        tree_row = ttk.Frame(folders_section)
        tree_row.pack(fill=tk.BOTH, expand=True)
        columns = ("kind", "path")
        tree = ttk.Treeview(tree_row, columns=columns, show="headings", selectmode="browse", height=8)
        tree.heading("kind", text="Type")
        tree.heading("path", text="Folder")
        tree.column("kind", width=90, anchor=tk.W)
        tree.column("path", width=420, anchor=tk.W)
        vsb = ttk.Scrollbar(tree_row, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)
        self.video_folders_tree = tree

        btn_row = ttk.Frame(folders_section)
        btn_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(btn_row, text="Change Folder\u2026", command=self._change_video_source_folder).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(btn_row, text="Remove", command=self._remove_video_source_folder).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="Scan Video Folders Now",
                   command=lambda: self.refresh_library(videos_only=True)).pack(side=tk.RIGHT)

        removed_section = ttk.LabelFrame(frame, text="Removed Videos", padding=12)
        removed_section.pack(fill=tk.X, pady=(16, 0))
        ttk.Label(
            removed_section,
            text="Using \"Remove from Library\" on a video never deletes its original "
                 "source file, so a refresh remembers which ones you removed and skips "
                 "them instead of silently re-adding them. Clearing that list below lets "
                 "every removed video get picked back up by the next refresh.",
            foreground="#666666", wraplength=520, justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 8))
        removed_row = ttk.Frame(removed_section)
        removed_row.pack(fill=tk.X)
        self.removed_videos_count_var = tk.StringVar(value="")
        ttk.Label(removed_row, textvariable=self.removed_videos_count_var).pack(side=tk.LEFT)
        ttk.Button(
            removed_row, text="Clear Removed-Videos List", command=self._clear_deleted_video_sources
        ).pack(side=tk.RIGHT)

    def _update_removed_songs_count(self):
        n = self.lib.count_removed_track_files()
        self.removed_songs_count_var.set(
            "No songs removed with Remove from Library." if n == 0
            else f"{n} song(s) removed with Remove from Library are skipped by Refresh Library."
        )

    def _clear_removed_songs(self):
        n = self.lib.count_removed_track_files()
        if n == 0:
            return
        if not messagebox.askyesno(
            "Removed Songs",
            f"Let the next Refresh Library add back the {n} song(s) you removed, if their "
            "files are still in a library folder?",
        ):
            return
        self.lib.clear_removed_track_files()
        applog.log(f"SETTINGS: cleared {n} removed-song tombstone(s)")
        self._update_removed_songs_count()
        self.status_var.set("Removed songs will come back on the next Refresh Library.")

    def _update_removed_videos_count(self):
        n = self.lib.count_deleted_video_sources()
        self.removed_videos_count_var.set(
            "No removed videos are being remembered." if n == 0
            else f"{n} removed video(s) currently being skipped on refresh."
        )

    def _clear_deleted_video_sources(self):
        n = self.lib.count_deleted_video_sources()
        if n == 0:
            return
        if not messagebox.askyesno(
            "Clear Removed-Videos List",
            f"Forget all {n} removed video(s)? The next time you use Scan Video Folders "
            "Now, any of them still sitting in a tracked video folder will be re-added "
            "and re-converted.",
        ):
            return
        self.lib.clear_deleted_video_sources()
        applog.log(f"SETTINGS: cleared {n} removed-video tombstone(s)")
        self._update_removed_videos_count()
        self.status_var.set("Removed-videos list cleared.")

    def _populate_video_folders_tree(self):
        tree = self.video_folders_tree
        tree.delete(*tree.get_children())
        for path, kind in self.lib.all_video_source_folders():
            label = "Movie" if kind == "movie" else "TV Show"
            display_path = path if os.path.isdir(path) else f"{path}  (not found)"
            # path is unique per row in video_source_folders (see its
            # schema), so it's safe to use directly as the iid here.
            tree.insert("", "end", iid=path, values=(label, display_path))

    def _change_video_source_folder(self):
        sel = self.video_folders_tree.selection()
        if not sel:
            messagebox.showinfo("No Folder Selected", "Select a tracked video folder first.")
            return
        old_path = sel[0]
        new_path = native_dialogs.choose_directory(title="Choose the New Location for This Folder")
        if not new_path:
            return
        try:
            self.lib.update_video_source_folder_path(old_path, new_path)
        except Exception as e:
            messagebox.showerror(
                "Couldn't Update Folder",
                f"{e}\n\n(If that new location is already tracked separately, remove one "
                "of the two entries first.)",
            )
            return
        applog.log(f"SETTINGS: video folder path changed from {old_path} to {new_path}")
        self._populate_video_folders_tree()
        self.status_var.set("Video folder updated \u2014 use Scan Video Folders Now to scan the new location.")

    def _remove_video_source_folder(self):
        sel = self.video_folders_tree.selection()
        if not sel:
            messagebox.showinfo("No Folder Selected", "Select a tracked video folder first.")
            return
        path = sel[0]
        if not messagebox.askyesno(
            "Stop Tracking This Folder",
            f"Stop tracking this folder?\n\n{path}\n\n"
            "It won't be scanned by Scan Video Folders Now anymore. Videos already "
            "added to your library from it are kept as-is - delete those individually from "
            "the Movies/TV Shows grid if you want them gone too.",
        ):
            return
        self.lib.remove_video_source_folder(path)
        applog.log(f"SETTINGS: stopped tracking video folder {path}")
        self._populate_video_folders_tree()
        self.status_var.set("Stopped tracking that video folder.")

    def _build_settings_podcasts_category(self):
        frame = self._new_settings_category("podcasts", "Podcasts")

        folder_section = ttk.LabelFrame(frame, text="Downloaded Episodes", padding=12)
        folder_section.pack(fill=tk.X, pady=(0, 16))
        ttk.Label(
            folder_section,
            text="Downloaded episode audio files are saved here, then added to your "
                 "library like any other track:",
            justify=tk.LEFT,
        ).pack(anchor="w")
        podcasts_folder_row = ttk.Frame(folder_section)
        podcasts_folder_row.pack(fill=tk.X, pady=(8, 0))
        self.podcasts_folder_var = tk.StringVar(
            value=self.app_settings.get("podcasts_folder", "") or "(not set)"
        )
        ttk.Label(podcasts_folder_row, textvariable=self.podcasts_folder_var, foreground="#666666").pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        ttk.Button(podcasts_folder_row, text="Choose Folder…", command=self.choose_podcasts_folder).pack(
            side=tk.RIGHT
        )

        count_row = ttk.Frame(folder_section)
        count_row.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(count_row, text="Keep this many recent episodes downloaded per show, by default:").pack(
            side=tk.LEFT
        )
        self.podcast_download_count_var = tk.StringVar(
            value=str(self.app_settings.get("podcast_download_count", 5))
        )
        ttk.Spinbox(
            count_row, from_=1, to=999, width=5, textvariable=self.podcast_download_count_var,
            command=self._save_podcast_download_count,
        ).pack(side=tk.LEFT, padx=(8, 0))
        self.podcast_download_count_var.trace_add("write", lambda *a: self._save_podcast_download_count())

        shows_section = ttk.LabelFrame(frame, text="Subscribed Shows", padding=12)
        shows_section.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            shows_section,
            text="Added via File -> Add Podcast from URL… - these get checked for new "
                 "episodes every time you refresh the library. Removing one here just stops "
                 "checking it; episodes already downloaded stay in your library as ordinary "
                 "tracks.",
            foreground="#666666", wraplength=520, justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 8))

        tree_row = ttk.Frame(shows_section)
        tree_row.pack(fill=tk.BOTH, expand=True)
        columns = ("name", "downloading", "feed")
        tree = ttk.Treeview(tree_row, columns=columns, show="headings", selectmode="browse", height=8)
        tree.heading("name", text="Show")
        tree.heading("downloading", text="Downloading")
        tree.heading("feed", text="Feed URL")
        tree.column("name", width=160, anchor=tk.W)
        tree.column("downloading", width=140, anchor=tk.W)
        tree.column("feed", width=280, anchor=tk.W)
        vsb = ttk.Scrollbar(tree_row, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)
        self.podcast_shows_tree = tree

        btn_row = ttk.Frame(shows_section)
        btn_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(btn_row, text="Check for New Episodes Now", command=self._check_one_podcast_show_now).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(btn_row, text="Edit Download Settings…", command=self._edit_podcast_show_settings).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(btn_row, text="Unsubscribe", command=self._remove_podcast_show).pack(side=tk.LEFT)

    def choose_podcasts_folder(self):
        folder = native_dialogs.choose_directory(title="Choose Where Downloaded Episodes Are Saved")
        if not folder:
            return
        self.app_settings["podcasts_folder"] = folder
        settings.save(self.app_settings)
        self.podcasts_folder_var.set(folder)
        applog.log(f"SETTINGS: podcasts folder set to {folder}")

    def _save_podcast_download_count(self):
        text = self.podcast_download_count_var.get().strip()
        if not text.isdigit():
            return
        self.app_settings["podcast_download_count"] = int(text)
        settings.save(self.app_settings)

    def _populate_podcast_shows_tree(self):
        tree = self.podcast_shows_tree
        tree.delete(*tree.get_children())
        for show in self.lib.all_podcast_shows():
            if show["download_all"]:
                downloading = "Every episode"
            else:
                count = show["download_count"]
                downloading = f"{count} most recent" if count is not None else "App default"
            # feed_url is UNIQUE (see podcast_shows' schema), so it's
            # safe to use directly as the iid here, the same way
            # video_source_folders' path already is above.
            tree.insert("", "end", iid=show["feed_url"], values=(show["name"], downloading, show["feed_url"]))

    def _selected_podcast_show(self):
        sel = self.podcast_shows_tree.selection()
        if not sel:
            messagebox.showinfo("No Show Selected", "Select a subscribed show first.")
            return None
        feed_url = sel[0]
        return next((s for s in self.lib.all_podcast_shows() if s["feed_url"] == feed_url), None)

    def _remove_podcast_show(self):
        show = self._selected_podcast_show()
        if not show:
            return
        if not messagebox.askyesno(
            "Unsubscribe",
            f"Stop checking '{show['name']}' for new episodes?\n\n"
            "Episodes already downloaded stay in your library as ordinary tracks - "
            "delete those individually from Songs if you want them gone too.",
        ):
            return
        self.lib.remove_podcast_show(show["id"])
        applog.log(f"SETTINGS: unsubscribed from podcast '{show['name']}'")
        self._populate_podcast_shows_tree()
        self.status_var.set(f"Unsubscribed from '{show['name']}'.")

    def _check_one_podcast_show_now(self):
        show = self._selected_podcast_show()
        if not show:
            return
        self._download_new_podcast_episodes([show])

    def _edit_podcast_show_settings(self):
        """Lets a subscribed show's download settings be changed after
        the fact (switch between 'keep N most recent' and 'download
        every episode automatically', or adjust N) - the same dialog
        shown when first subscribing (_ask_podcast_download_settings),
        just pre-filled with the show's current settings and applied
        via Library.update_podcast_show instead of add_podcast_show.
        Doesn't re-fetch the feed just to show its current episode
        count - not worth the network round-trip for a settings
        tweak, so the dialog is told "at least however many are
        already known" instead."""
        show = self._selected_podcast_show()
        if not show:
            return
        known_episodes = len(self.lib.podcast_episodes_for_show(show["id"]))
        episode_count_hint = max(known_episodes, show["download_count"] or 0, 1)
        current_count = show["download_count"] if show["download_count"] is not None else (
            self.app_settings.get("podcast_download_count", 5)
        )
        choice = self._ask_podcast_download_settings(
            "Edit Podcast Download Settings", "Save", show["name"],
            episode_count_hint, current_count, bool(show["download_all"]),
        )
        if choice is None:
            return
        count, download_all = choice
        self.lib.update_podcast_show(show["id"], download_count=count, download_all=download_all)
        applog.log(
            f"SETTINGS: podcast '{show['name']}' now "
            + ("downloads every episode automatically" if download_all else f"keeps {count} episode(s)")
        )
        self._populate_podcast_shows_tree()
        self.status_var.set(f"Updated download settings for '{show['name']}'.")

    def _set_maximized(self, on):
        try:
            self.state("zoomed" if on else "normal")  # Windows/macOS
        except tk.TclError:
            try:
                self.attributes("-zoomed", bool(on))  # X11
            except tk.TclError:
                pass

    def _set_fullscreen(self, on):
        try:
            self.attributes("-fullscreen", bool(on))
        except tk.TclError:
            # No full-screen support from the window manager: fill the
            # screen as a maximized window instead.
            try:
                self.attributes("-zoomed", bool(on))
            except tk.TclError:
                pass

    def toggle_fullscreen(self, event=None):
        try:
            on = bool(self.attributes("-fullscreen"))
        except tk.TclError:
            on = False
        self._set_fullscreen(not on)
        return "break"

    def _save_start_maximized(self):
        on = bool(self.start_maximized_var.get())
        self.app_settings["start_maximized"] = on
        settings.save(self.app_settings)
        self._set_maximized(on)

    # -------------------------------------------------------- spoofify --
    # Two playlists kept up to date from play counts (which come from
    # the iPod): songs you haven't played much, and your most played.
    # They're ordinary playlists, so they show in the sidebar and sync
    # to the iPod like any other.

    def _spoofify_counts(self):
        def get(key, default):
            try:
                return max(0, min(5000, int(self.app_settings.get(key, default))))
            except (TypeError, ValueError):
                return default
        return get("spoofify_least_count", 50), get("spoofify_most_count", 25)

    def _build_settings_spoofify_category(self):
        frame = self._new_settings_category("spoofify", "Spoofify")
        section = ttk.LabelFrame(frame, text="Automatic playlists", padding=12)
        section.pack(fill=tk.X, pady=(0, 16))
        ttk.Label(
            section,
            text=f"Spoofify keeps two playlists up to date from your play counts (counted on "
                 f"the iPod and brought in each time it connects): \u201c{library.SPOOFIFY_LEAST}\u201d "
                 f"has songs you haven't played, or haven't played much, and "
                 f"\u201c{library.SPOOFIFY_MOST}\u201d has your top songs. They update when the "
                 f"app starts and whenever new plays come in from the iPod, and sync to the "
                 f"iPod like any other playlist. Set a size to 0 to turn that playlist off.",
            foreground="#666666", wraplength=560, justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 10))

        self.spoofify_enabled_var = tk.BooleanVar(value=self.app_settings.get("spoofify_enabled", True))
        ttk.Checkbutton(
            section, text="Turn on Spoofify", variable=self.spoofify_enabled_var,
            command=self._save_spoofify_settings,
        ).pack(anchor="w")

        least, most = self._spoofify_counts()
        self.spoofify_least_var = tk.StringVar(value=str(least))
        self.spoofify_most_var = tk.StringVar(value=str(most))
        for label, var in (
            ("Songs in \u201cNot Played Much\u201d:", self.spoofify_least_var),
            ("Songs in \u201cMost Played\u201d:", self.spoofify_most_var),
        ):
            row = ttk.Frame(section)
            row.pack(fill=tk.X, pady=(8, 0))
            ttk.Label(row, text=label, width=26).pack(side=tk.LEFT)
            ttk.Spinbox(
                row, from_=0, to=5000, increment=5, width=6, textvariable=var,
                command=self._save_spoofify_settings,
            ).pack(side=tk.LEFT)
            var.trace_add("write", lambda *a: self._save_spoofify_settings())

        btn_row = ttk.Frame(section)
        btn_row.pack(fill=tk.X, pady=(12, 0))
        ttk.Button(btn_row, text="Update Now", command=lambda: self._refresh_spoofify(quiet=False)).pack(
            side=tk.LEFT
        )
        self.spoofify_status_var = tk.StringVar(value="")
        ttk.Label(btn_row, textvariable=self.spoofify_status_var, foreground="#666666").pack(
            side=tk.LEFT, padx=(10, 0)
        )

    def _save_spoofify_settings(self):
        changed = False
        enabled = bool(self.spoofify_enabled_var.get())
        if self.app_settings.get("spoofify_enabled", True) != enabled:
            self.app_settings["spoofify_enabled"] = enabled
            changed = True
        for key, var in (("spoofify_least_count", self.spoofify_least_var),
                         ("spoofify_most_count", self.spoofify_most_var)):
            text = var.get().strip()
            if not text.isdigit():
                continue
            value = min(5000, int(text))
            if self.app_settings.get(key) != value:
                self.app_settings[key] = value
                changed = True
        if not changed:
            return
        settings.save(self.app_settings)
        # Typing "120" passes through "1" and "12"; rebuild once typing pauses.
        if getattr(self, "_spoofify_after_id", None):
            self.after_cancel(self._spoofify_after_id)
        self._spoofify_after_id = self.after(700, lambda: self._refresh_spoofify(quiet=True))

    def _refresh_spoofify(self, quiet=False):
        self._spoofify_after_id = None
        try:
            if not self.app_settings.get("spoofify_enabled", True):
                self.lib.remove_spoofify_playlist()
                least_n = most_n = None
            else:
                least_n, most_n = self.lib.refresh_spoofify(*self._spoofify_counts())
        except Exception:
            applog.log("SPOOFIFY update failed:\n" + traceback.format_exc())
            if not quiet:
                messagebox.showerror("Spoofify", "Couldn't update the Spoofify playlists (see the log file).")
            return
        if least_n is None:
            message = "Spoofify is off."
        else:
            message = f"Updated: {least_n} not played much, {most_n} most played."
            if most_n == 0 and self._spoofify_counts()[1] > 0:
                message += " (No plays counted yet - connect your iPod after listening.)"
        applog.log("SPOOFIFY: " + message)
        if hasattr(self, "spoofify_status_var"):
            self.spoofify_status_var.set(message)
        if not quiet:
            self.status_var.set("Spoofify " + message[0].lower() + message[1:])
        if self.current_view == self.VIEW_LOCAL_PLAYLIST:
            ids = {p["id"] for p in self.lib.all_playlists()}
            if self.current_local_playlist_id in ids:
                self.show_local_playlist(self.current_local_playlist_id)
            else:
                self.show_library()
        if self.current_view != self.VIEW_SETTINGS:
            self.refresh_sidebar()

    def _build_settings_appearance_category(self):
        frame = self._new_settings_category("appearance", "Appearance")

        appearance_section = ttk.LabelFrame(frame, text="Appearance", padding=12)
        appearance_section.pack(fill=tk.X, pady=(0, 16))
        self.start_maximized_var = tk.BooleanVar(value=self.app_settings.get("start_maximized", True))
        ttk.Checkbutton(
            appearance_section,
            text="Open maximized (F11 switches to borderless full screen and back)",
            variable=self.start_maximized_var,
            command=self._save_start_maximized,
        ).pack(anchor="w")

        thumb_row = ttk.Frame(appearance_section)
        thumb_row.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(thumb_row, text="Thumbnail size (Albums, Movies, TV Shows, Photos):").pack(anchor="w")
        scale_row = ttk.Frame(thumb_row)
        scale_row.pack(fill=tk.X, pady=(4, 0))
        current_pct = round(self.app_settings.get("thumbnail_scale", 1.0) * 100)
        self.thumbnail_scale_var = tk.IntVar(value=current_pct)
        self.thumbnail_scale_label_var = tk.StringVar(value=f"{current_pct}%")
        scale = ttk.Scale(
            scale_row, from_=50, to=200, orient=tk.HORIZONTAL,
            variable=self.thumbnail_scale_var, command=self._on_thumbnail_scale_dragged,
            length=220,
        )
        scale.pack(side=tk.LEFT)
        ttk.Label(scale_row, textvariable=self.thumbnail_scale_label_var, width=5).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        # Reflowing every grid on each pixel of drag would be sluggish
        # with a large library - the label updates live, but the actual
        # resize/reflow only happens once the slider is released.
        scale.bind("<ButtonRelease-1>", self._apply_thumbnail_scale)

    def _on_thumbnail_scale_dragged(self, value):
        self.thumbnail_scale_label_var.set(f"{round(float(value))}%")

    def _apply_thumbnail_scale(self, event=None):
        pct = self.thumbnail_scale_var.get()
        new_scale = pct / 100.0
        if new_scale == self.app_settings.get("thumbnail_scale", 1.0):
            return
        self.app_settings["thumbnail_scale"] = new_scale
        settings.save(self.app_settings)
        applog.log(f"SETTINGS: thumbnail size set to {pct}%")
        # Old-size cached images would otherwise keep showing at the
        # wrong size inside newly-resized tiles until something else
        # happened to evict them.
        self._video_photo_cache = {}
        self._photo_photo_cache = {}
        self._album_photo_cache = {}
        self._placeholder_photo_cache = {}
        if self.current_view == self.VIEW_ALBUM_GRID:
            self.show_album_grid()
        elif self.current_view == self.VIEW_PODCASTS:
            self.show_podcasts()
        elif self.current_view in (self.VIEW_MOVIES, self.VIEW_TVSHOWS):
            self._refresh_current_video_view()
        elif self.current_view == self.VIEW_PHOTOS and self.photo_list_frame.winfo_ismapped():
            # VIEW_PHOTOS also covers the on-device photo *albums* screen
            # (a plain treeview, not a thumbnail grid - see
            # show_photos_on_device) - only re-populate the actual
            # thumbnail grid when that's what's showing.
            self.show_photos()

    def _build_name_list_panels(self):
        """Artists and Genres are simple name+count lists (no album art
        involved), so they share this one builder rather than each
        getting the album grid's more elaborate tile machinery."""
        self.artist_list_frame, self.artist_tree, self._artist_raw_values = self._make_name_list_frame(
            "Artist", self.show_tracks_for_artist
        )
        self.genre_list_frame, self.genre_tree, self._genre_raw_values = self._make_name_list_frame(
            "Genre", self.show_tracks_for_genre
        )

    def _build_device_photo_albums_panel(self):
        """On-device photos can only be summarized at the album level -
        the device gives back album names/counts, not individual photo
        IDs or thumbnail data (that would need gdk-pixbuf integration
        this app doesn't have), so there's no per-photo browsing or
        selection here, unlike the local Photos grid."""
        self.device_photo_albums_frame = ttk.Frame(self.main_area)
        ttk.Label(
            self.device_photo_albums_frame,
            text="Individual photos already on this iPod can't be browsed here (the "
                 "device doesn't hand back thumbnail data) - this is an album-level summary.",
            foreground="#888888", wraplength=520, justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 8))
        columns = ("name", "count")
        tree = ttk.Treeview(
            self.device_photo_albums_frame, columns=columns, show="headings", selectmode="none"
        )
        tree.heading("name", text="Album")
        tree.heading("count", text="Photos")
        tree.column("name", width=320, anchor=tk.W)
        tree.column("count", width=100, anchor=tk.E)
        vsb = ttk.Scrollbar(self.device_photo_albums_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)
        self.device_photo_albums_tree = tree

    def _populate_device_photo_albums(self):
        self.device_photo_albums_tree.delete(*self.device_photo_albums_tree.get_children())
        for album in self.device_photo_albums:
            name = (album["name"] or "(unnamed)") + (" \u2014 Photo Library" if album["is_master"] else "")
            self.device_photo_albums_tree.insert("", "end", values=(name, album["photo_count"]))

    def show_photos_on_device(self):
        self.current_view = self.VIEW_PHOTOS
        self._show_body("device_photo_albums")
        self._populate_device_photo_albums()
        master = next((a for a in self.device_photo_albums if a["is_master"]), None)
        total = master["photo_count"] if master else 0
        self.status_var.set(
            f"{total} photo(s) on this iPod across {len(self.device_photo_albums)} album(s)"
        )

    def _make_name_list_frame(self, label_text, on_pick):
        frame = ttk.Frame(self.main_area)
        columns = ("name", "count")
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        tree.heading("name", text=label_text)
        tree.heading("count", text="Tracks")
        tree.column("name", width=260, anchor=tk.W)
        tree.column("count", width=70, anchor=tk.E)
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)

        # Rows are looked up by index (the iid) rather than by parsing
        # the displayed name back out, so a real artist/genre that
        # happens to be named e.g. "(Unknown Artist)" can never be
        # misread as the NULL placeholder — see _populate_name_list.
        raw_values = []

        def open_selected(_event=None):
            sel = tree.selection()
            if not sel:
                return
            idx = int(sel[0])
            if 0 <= idx < len(raw_values):
                on_pick(raw_values[idx])

        tree.bind("<Double-Button-1>", open_selected)
        tree.bind("<Return>", open_selected)
        tree.bind("<<TreeviewSelect>>", lambda e: self._on_name_list_selection_changed(tree, raw_values))
        return frame, tree, raw_values

    def _on_name_list_selection_changed(self, tree, raw_values):
        """Remembers which name (not which iid - those are just row
        indices, reassigned every rebuild) is highlighted in this
        Artist/Genre list, so _populate_name_list can restore it after
        the list is rebuilt (e.g. navigating away and back via the
        device view sidebar) instead of the highlight just vanishing."""
        sel = tree.selection()
        if sel:
            idx = int(sel[0])
            if 0 <= idx < len(raw_values):
                self._name_list_selection[tree] = raw_values[idx]
                # Durable sync profile, same reasoning as
                # _persist_album_marks. Only one name can be
                # highlighted at a time here (selectmode="browse"), so
                # the marked set for this type is always just this one
                # value (or empty, if nothing's ever been picked).
                if self._device_view_active:
                    item_type = "artist" if tree is self.artist_tree else "genre"
                    name = raw_values[idx]
                    self.lib.set_marked_for_sync(item_type, [name] if name is not None else [])

    def _populate_name_list(self, tree, raw_values, rows, key_col, unknown_label):
        # Only restores the remembered selection inside the device
        # view (see _load_device_view_selections / _clear_device_view_selections) - the normal
        # library's Artist/Genre lists keep their original behavior of
        # starting unselected on every visit.
        remembered = self._name_list_selection.get(tree) if self._device_view_active else None
        tree.delete(*tree.get_children())
        raw_values.clear()
        reselect_iid = None
        for i, row in enumerate(rows):
            raw = row[key_col]
            name = raw if raw not in (None, "") else f"({unknown_label})"
            tree.insert("", "end", iid=str(i), values=(name, row["track_count"]))
            raw_values.append(raw)
            if remembered is not None and raw == remembered:
                reselect_iid = str(i)
        if reselect_iid is not None:
            tree.selection_set(reselect_iid)
            tree.see(reselect_iid)

    # Base sizes for every tile grid in the app (video/show, album,
    # photo) - all scaled together by the "Thumbnail size" setting (see
    # the Appearance section of Settings / _apply_thumbnail_scale). The
    # actual sizes used anywhere in the code are the *_TILE_WIDTH/
    # *_THUMB_SIZE properties just below, never these _BASE constants
    # directly.
    # Movie/episode and show thumbnails are portrait (poster-style,
    # 2:3) rather than the video's own landscape aspect ratio - see
    # _get_video_thumbnail_photo, which center-crops whatever source
    # image (frame grab or fan art) to fill this shape instead of
    # letterboxing it, so every tile reads as a consistent poster grid
    # regardless of the underlying video's actual aspect ratio.
    VIDEO_TILE_WIDTH_BASE = 130
    # The TV Shows landing grid (one tile per show, like an album grid -
    # see _show_tvshow_group_grid) uses noticeably bigger tiles/thumbnails
    # than the movie/episode grid: a show tile stands in for a whole
    # series and is often showing real fan art now (see nfo.py), so it
    # reads better bigger, the same way a poster does.
    SHOW_TILE_WIDTH_BASE = 180
    VIDEO_THUMB_SIZE_BASE = (110, 165)
    SHOW_THUMB_SIZE_BASE = (160, 240)
    ALBUM_TILE_WIDTH_BASE = 150
    ALBUM_ART_SIZE_BASE = (120, 120)
    PHOTO_TILE_WIDTH_BASE = 130
    PHOTO_THUMB_SIZE_BASE = (110, 110)

    @property
    def _thumb_scale(self):
        return self.app_settings.get("thumbnail_scale", 1.0)

    @property
    def VIDEO_TILE_WIDTH(self):
        return max(60, int(self.VIDEO_TILE_WIDTH_BASE * self._thumb_scale))

    @property
    def SHOW_TILE_WIDTH(self):
        return max(60, int(self.SHOW_TILE_WIDTH_BASE * self._thumb_scale))

    @property
    def VIDEO_THUMB_SIZE(self):
        w, h = self.VIDEO_THUMB_SIZE_BASE
        return (max(40, int(w * self._thumb_scale)), max(30, int(h * self._thumb_scale)))

    @property
    def SHOW_THUMB_SIZE(self):
        w, h = self.SHOW_THUMB_SIZE_BASE
        return (max(40, int(w * self._thumb_scale)), max(30, int(h * self._thumb_scale)))

    @property
    def TILE_WIDTH(self):
        """Album grid tile width - named without an "ALBUM_" prefix
        for historical reasons (see _make_album_tile/_reflow_album_grid)."""
        return max(60, int(self.ALBUM_TILE_WIDTH_BASE * self._thumb_scale))

    @property
    def ALBUM_ART_SIZE(self):
        w, h = self.ALBUM_ART_SIZE_BASE
        return (max(40, int(w * self._thumb_scale)), max(40, int(h * self._thumb_scale)))

    @property
    def PHOTO_TILE_WIDTH(self):
        return max(60, int(self.PHOTO_TILE_WIDTH_BASE * self._thumb_scale))

    @property
    def PHOTO_THUMB_SIZE(self):
        w, h = self.PHOTO_THUMB_SIZE_BASE
        return (max(40, int(w * self._thumb_scale)), max(40, int(h * self._thumb_scale)))

    def _build_video_list_panel(self):
        self.video_list_frame = ttk.Frame(self.main_area)

        toolbar = ttk.Frame(self.video_list_frame)
        toolbar.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))
        self.video_back_to_shows_button = ttk.Button(
            toolbar, text="◀ Back to Shows", command=self._show_tvshow_group_grid
        )
        # Not packed here - shown only while drilled into one show's
        # episode list (TV Shows mode only - see _set_video_toolbar_mode).
        self.video_add_folder_button = ttk.Button(
            toolbar, text="Add Video Folder\u2026", command=self.add_video_folder
        )
        self.video_add_folder_button.pack(side=tk.LEFT)
        self.video_add_file_button = ttk.Button(
            toolbar, text="Add Video\u2026", command=self.add_video_file
        )
        self.video_add_file_button.pack(side=tk.LEFT, padx=(6, 0))
        self.video_sync_button = ttk.Button(
            toolbar, text="Sync Selected \u2192 iPod", command=self.sync_selected_videos
        )
        self.video_sync_button.pack(side=tk.LEFT, padx=(6, 0))
        self.video_remove_device_button = ttk.Button(
            toolbar, text="Remove Selected from iPod", command=self.remove_selected_device_videos
        )
        # Not packed here - shown only in device mode, via _set_video_toolbar_mode().

        grid_row = ttk.Frame(self.video_list_frame)
        grid_row.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(grid_row, highlightthickness=0)
        vsb = ttk.Scrollbar(grid_row, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)

        self.video_grid_canvas = canvas
        self._default_video_canvas_bg = canvas.cget("bg")
        self.video_grid_inner = ttk.Frame(canvas)
        self._video_grid_window = canvas.create_window((0, 0), window=self.video_grid_inner, anchor="nw")

        def on_inner_configure(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def on_canvas_configure(event):
            canvas.itemconfig(self._video_grid_window, width=event.width)
            self._reflow_video_grid()

        self.video_grid_inner.bind("<Configure>", on_inner_configure)
        canvas.bind("<Configure>", on_canvas_configure)

        def on_mousewheel(event):
            # X11 reports the wheel as Button-4 (up) / Button-5 (down)
            # rather than a MouseWheel delta - event.delta is always 0
            # here, so the previous "event.delta > 0" check never did
            # anything and this ended up scrolling the opposite way of
            # every other app: wheel-up (Button-4) should move the view
            # up (yview_scroll(-1)), wheel-down (Button-5) should move
            # it down (+1).
            delta = -1 if event.num == 4 else 1
            apple_style.smooth_scroll(canvas, delta, self._smooth_motion())

        canvas.bind("<Enter>", lambda e: (
            canvas.bind_all("<MouseWheel>", lambda ev: apple_style.smooth_scroll(
                canvas, -1 if ev.delta > 0 else 1, self._smooth_motion())),
            canvas.bind_all("<Button-4>", on_mousewheel),
            canvas.bind_all("<Button-5>", on_mousewheel),
        ))
        canvas.bind("<Leave>", lambda e: (
            canvas.unbind_all("<MouseWheel>"),
            canvas.unbind_all("<Button-4>"),
            canvas.unbind_all("<Button-5>"),
        ))
        canvas.bind("<Control-a>", self.select_all_tracks)
        canvas.bind("<Control-A>", self.select_all_tracks)

        self._video_rows = []
        self._video_tile_frames = []
        self._video_tile_frames_by_id = {}
        self._selected_videos = set()   # {video_id, ...}
        self._video_photo_cache = {}    # video_id -> ImageTk.PhotoImage (must keep a ref)
        self._video_grid_source = "local"   # "local" (this app's library) or "device" (already on iPod)
        # TV Shows only: "shows" (one tile per show, like an album grid)
        # or "episodes" (drilled into one show's episode list - the
        # same grid Movies always uses). Movies has no "shows" level.
        self._tvshow_grid_level = "episodes"
        self._tvshow_groups = {}    # show_key -> list of video rows, while level == "shows"
        self._current_show_key = None   # which show's episodes are showing, while level == "episodes"
        self._video_tile_width = self.VIDEO_TILE_WIDTH   # bumped to SHOW_TILE_WIDTH for the shows grid

    def _populate_video_grid(self, rows):
        for child in self.video_grid_inner.winfo_children():
            child.destroy()
        self._video_rows = rows
        self._video_tile_frames = []
        self._video_tile_frames_by_id = {}
        # Durable, same reasoning as the album grid (_selected_albums) -
        # a sync profile set up once should survive navigating away and
        # back, or reopening the app. Restored straight from disk here
        # (rather than just left alone across repopulation) since
        # _persist_video_marks always writes real video ids, even when
        # the toggle happened on a TV-show *group* tile (see
        # _expand_video_selection) - so re-reading from disk is the one
        # representation that's valid at every grid level. Skipped for
        # the shows-grouping grid itself, since a group tile's "id" is a
        # show-name key, not a real video id, and can't be compared to
        # what's stored. Also skipped for "device" source - that grid
        # shows what's already on the iPod for removal, a one-off pick,
        # never the durable local sync profile.
        showing_show_groups = (self.library_mode == "tvshows" and self._tvshow_grid_level == "shows")
        self._video_tile_width = self.SHOW_TILE_WIDTH if showing_show_groups else self.VIDEO_TILE_WIDTH
        if self._device_view_active and self._video_grid_source == "local" and not showing_show_groups:
            marked = self.lib.all_marked_for_sync("video")
            self._selected_videos = {int(v) for v in marked if v.lstrip("-").isdigit()}
        else:
            self._selected_videos = set()

        for row in rows:
            frame = self._make_video_tile(self.video_grid_inner, row)
            self._video_tile_frames.append(frame)
        self._reflow_video_grid()

    def _reflow_video_grid(self):
        try:
            canvas_width = self.video_grid_canvas.winfo_width() or 900
            columns = max(1, canvas_width // self._video_tile_width)
            # sticky="nw", and no column weight, on purpose - a weighted
            # column stretches to share out any leftover canvas width,
            # which spreads the tiles themselves out across the row
            # instead of packing them from the left with the leftover
            # space at the right, where a grid of fixed-size tiles reads
            # better (the same way a file manager's icon view works).
            for i, frame in enumerate(self._video_tile_frames):
                r, c = divmod(i, columns)
                frame.grid(row=r, column=c, padx=3, pady=3, sticky="nw")
        except Exception:
            self._show_error_with_traceback("Video grid failed to lay out")

    def _get_video_thumbnail_photo(self, row, size=None):
        size = size or self.VIDEO_THUMB_SIZE
        video_id = row["id"]
        cache_key = (video_id, size)
        cached = self._video_photo_cache.get(cache_key)
        if cached is not None:
            return cached
        thumb_path = row["thumbnail_path"] if "thumbnail_path" in row.keys() else None
        if thumb_path and HAVE_PIL and os.path.exists(thumb_path):
            try:
                from PIL import Image, ImageOps
                img = Image.open(thumb_path)
                # Movie/episode/show tiles are posed as portrait poster
                # tiles (see the *_THUMB_SIZE_BASE comment above), but
                # the source image - a frame grab or a wide fan-art
                # banner - is almost always landscape. ImageOps.fit
                # center-crops it to exactly fill the portrait box
                # instead of img.thumbnail()'s letterboxing, which used
                # to leave most of a portrait-shaped tile empty.
                img = ImageOps.fit(img.convert("RGB"), size, method=Image.LANCZOS)
                if self._smooth_motion():
                    img = apple_style.round_corners(img, radius=8)
                photo = ImageTk.PhotoImage(img)
                self._cache_insert(self._video_photo_cache, cache_key, photo)
                return photo
            except Exception:
                pass
        return self._placeholder_photo(row["title"] or "", size=size)

    def _make_video_tile(self, parent, row):
        is_show_group = self.library_mode == "tvshows" and self._tvshow_grid_level == "shows"
        tile_width = self.SHOW_TILE_WIDTH if is_show_group else self.VIDEO_TILE_WIDTH
        thumb_size = self.SHOW_THUMB_SIZE if is_show_group else self.VIDEO_THUMB_SIZE
        video_id = row["id"]
        title = row["title"] or os.path.basename(row["filepath"])
        if is_show_group:
            n = row.get("_episode_count", 0)
            subtitle = f"{n} episode{'s' if n != 1 else ''}"
        else:
            subtitle = ""
            if row["kind"] == "tvshow" and row["show_name"]:
                subtitle = row["show_name"]
                if row["season_nr"] and row["episode_nr"]:
                    subtitle += f" \u2022 S{row['season_nr']:02d}E{row['episode_nr']:02d}"
        selected = video_id in self._selected_videos
        accent = "#0a67e5"

        tile = tk.Frame(
            parent, width=tile_width - 10,
            highlightthickness=3,
        )
        self._mark_tile(tile, selected)
        if self._smooth_motion():
            tile.configure(bg=apple_style.CONTENT)
        self._video_tile_frames_by_id[video_id] = tile

        photo = self._get_video_thumbnail_photo(row, size=thumb_size)
        art_label = tk.Label(tile, image=photo, cursor="hand2", bg=tile.cget("bg"))
        art_label.image = photo
        art_label.pack(padx=4, pady=4)

        name_label = ttk.Label(tile, text=title, wraplength=tile_width - 16,
                                justify=tk.CENTER, font=(apple_style.FAMILY, 9, "bold"))
        name_label.pack()
        subtitle_label = ttk.Label(tile, text=subtitle, wraplength=tile_width - 16,
                                    justify=tk.CENTER, foreground="#666666")
        subtitle_label.pack(pady=(0, 4))

        for widget in (tile, art_label, name_label, subtitle_label):
            widget.bind("<Button-1>", lambda e, vid=video_id: self._toggle_video_selection(vid))
            widget.bind("<Button-3>", lambda e, vid=video_id: self._show_video_context_menu(e, vid))
            if is_show_group:
                # Like an album tile: a click selects, a double-click
                # opens (here, drills into that show's episode list).
                widget.bind("<Double-Button-1>", lambda e, key=video_id: self._show_tvshow_episodes(key))

        return tile

    def _toggle_video_selection(self, video_id):
        if video_id in self._selected_videos:
            self._selected_videos.discard(video_id)
        else:
            self._selected_videos.add(video_id)
        tile = self._video_tile_frames_by_id.get(video_id)
        if tile is not None and tile.winfo_exists():
            self._mark_tile(tile, video_id in self._selected_videos)
        n = len(self._selected_videos)
        if n:
            self.status_var.set(f"{n} video(s) selected.")
        self._persist_video_marks()

    def _persist_video_marks(self):
        """Writes the current video selection to disk as the durable
        sync profile - only meaningful in the device view, browsing the
        local library (not on-device removal picks - see
        _populate_video_grid). Always stores real video ids, expanding
        a TV-show group tile's key into its episode ids first (same
        helper sync/remove already use), so a mark written from any
        grid level is valid when read back at any other level."""
        if self._device_view_active and self._video_grid_source == "local":
            expanded = self._expand_video_selection(self._selected_videos)
            self.lib.set_marked_for_sync("video", [str(v) for v in expanded])

    def _refresh_video_tile_selection_borders(self):
        for vid, tile in self._video_tile_frames_by_id.items():
            if tile.winfo_exists():
                self._mark_tile(tile, vid in self._selected_videos)

    def set_library_mode(self, mode):
        self.library_mode = mode
        label = {
            "music": "\U0001F3B5 Music", "movies": "\U0001F3AC Movies",
            "tvshows": "\U0001F4FA TV Shows", "photos": "\U0001F4F7 Photos",
            "podcasts": "\U0001F3A7 Podcasts",
        }[mode]
        self.library_mode_button.configure(text=label)
        self._update_segments()
        if mode == "music":
            self._show_body("table")
            self.show_library()
        elif mode == "movies":
            self.show_movies()
        elif mode == "tvshows":
            self.show_tvshows()
        elif mode == "podcasts":
            self.show_podcasts()
        else:
            self.show_photos()
        self.refresh_sidebar()
        self._update_sidebar_mode_switcher()
        # Leaving Settings through a top-bar tab: don't keep "Settings"
        # as the page title and status line.
        sel = self.sidebar.selection()
        if sel:
            self._update_header_title(sel[0])
        elif self.header_title_var.get() == "Settings":
            self.header_title_var.set("Songs" if mode == "music" else label.split(" ", 1)[1])
        if self.status_var.get() == "Settings":
            self.status_var.set("")
        self._schedule_header_subtitle()

    def _update_sidebar_mode_switcher(self):
        """Swaps the sidebar's top control between the Music/Movies/TV
        Shows/Photos mode switcher and a plain "Back" button, depending
        on whether Settings is currently showing - see
        settings_back_button's own comment for why."""
        if self.current_view == self.VIEW_SETTINGS:
            self.library_mode_button.pack_forget()
            self.settings_back_button.pack(fill=tk.X, pady=(0, 4), before=self.sidebar)
        else:
            self.settings_back_button.pack_forget()
            if self._smooth_motion():
                # Apple-style layout: the top bar's tabs replace this.
                self.library_mode_button.pack_forget()
            else:
                self.library_mode_button.pack(fill=tk.X, pady=(0, 4), before=self.sidebar)

    def _exit_settings(self):
        self.set_library_mode(getattr(self, "_pre_settings_library_mode", "music") or "music")

    def show_movies(self):
        # Set explicitly (not just relied on from set_library_mode) since
        # VCD's own sidebar (dv_movies/dv_ondevice_movies) calls this
        # directly without going through set_library_mode - _set_video_
        # toolbar_mode's "Back to Shows" visibility depends on this being
        # accurate no matter which sidebar got here.
        self.library_mode = "movies"
        self.current_view = self.VIEW_MOVIES
        self._show_body("videos")
        self._set_video_toolbar_mode("local")
        rows = self.lib.all_videos("movie")
        self._populate_video_grid(rows)
        self.video_grid_canvas.focus_set()
        self.status_var.set(f"{len(rows)} movie(s)")

    def show_tvshows(self):
        self.library_mode = "tvshows"
        self.current_view = self.VIEW_TVSHOWS
        self._tvshow_group_source = "local"
        self._show_tvshow_group_grid()

    @staticmethod
    def _device_track_to_video_row(t):
        """Adapts a device track dict (from read_device_library) into
        the same shape _make_video_tile expects from a local library
        row, so the same grid code can show either. dbid (a string) is
        used as the tile id - fine, since _selected_videos is just an
        opaque set of whatever id type is currently loaded, never mixed
        between local (int) and device (str) in the same grid."""
        return {
            "id": t["dbid"],
            "title": t["title"],
            "kind": "tvshow" if t["mediatype"] == 0x40 else "movie",
            "show_name": t["tvshow"],
            "season_nr": t["season_nr"],
            "episode_nr": t["episode_nr"],
            "duration_ms": t["duration_ms"],
            "size_bytes": t["size_bytes"],
            "filepath": t["title"],
            "thumbnail_path": None,
        }

    def show_movies_on_device(self):
        self.library_mode = "movies"
        self.current_view = self.VIEW_MOVIES
        self._show_body("videos")
        self._set_video_toolbar_mode("device")
        rows = [self._device_track_to_video_row(t) for t in self.device_movies]
        self._populate_video_grid(rows)
        self.video_grid_canvas.focus_set()
        self.status_var.set(f"{len(rows)} movie(s) on this iPod")

    def show_tvshows_on_device(self):
        self.library_mode = "tvshows"
        self.current_view = self.VIEW_TVSHOWS
        self._tvshow_group_source = "device"
        self._show_tvshow_group_grid()

    def _tvshow_source_rows(self):
        """The full, ungrouped list of TV episode rows for whichever
        source (local library or on-device) is currently active."""
        if self._tvshow_group_source == "device":
            return [self._device_track_to_video_row(t) for t in self.device_tvshows]
        return list(self.lib.all_videos("tvshow", order_by="show_name, season_nr, episode_nr"))

    def _show_tvshow_group_grid(self):
        """The TV Shows landing view: one tile per show (grouped by
        show_name), the same way the Albums grid shows one tile per
        album instead of a flat list of every track - double-click a
        show to drill into its episode list (_show_tvshow_episodes).
        Episodes with no show name (parsed from the filename or typed
        in when adding) are grouped under a single catch-all tile
        rather than getting lost."""
        self._tvshow_grid_level = "shows"
        self._current_show_key = None
        self.current_view = self.VIEW_TVSHOWS
        self._show_body("videos")
        self._set_video_toolbar_mode(self._tvshow_group_source)

        rows = self._tvshow_source_rows()
        groups = {}
        for row in rows:
            key = row["show_name"] or ""
            groups.setdefault(key, []).append(row)
        self._tvshow_groups = groups

        group_rows = []
        for key in sorted(groups, key=lambda k: (k == "", k.lower())):
            episodes = groups[key]
            # Prefer a show's fan art for its own group tile/playlist
            # thumbnail over the ordinary per-episode frame grab - it's
            # a proper promotional image rather than a random video
            # frame, when one was found (see nfo.find_show_fanart). Any
            # episode carrying it will do, since it's the same image
            # for the whole show; fall back to the first episode's own
            # extracted thumbnail when no fanart exists for this show.
            fanart = next(
                (p for p in (_row_get(ep, "show_fanart_path") for ep in episodes) if p), None
            )
            group_rows.append({
                "id": key,
                "title": key or "(No Show Name)",
                "kind": "tvshow",
                "show_name": key,
                "season_nr": None,
                "episode_nr": None,
                "duration_ms": None,
                "size_bytes": None,
                "filepath": "",
                "thumbnail_path": fanart or episodes[0]["thumbnail_path"],
                "_episode_count": len(episodes),
            })

        self._populate_video_grid(group_rows)
        self.video_grid_canvas.focus_set()
        suffix = " on this iPod" if self._tvshow_group_source == "device" else ""
        self.status_var.set(f"{len(group_rows)} show(s){suffix} — double-click one to see its episodes.")

    def _show_tvshow_episodes(self, show_key):
        """Drills into one show's episode list - from here on it's the
        exact same grid/selection/sync code Movies always used."""
        self._tvshow_grid_level = "episodes"
        self._current_show_key = show_key
        self._show_body("videos")
        self._set_video_toolbar_mode(self._tvshow_group_source)

        rows = self._tvshow_source_rows()
        episodes = [r for r in rows if (r["show_name"] or "") == show_key]
        episodes.sort(key=lambda r: (r["season_nr"] or 0, r["episode_nr"] or 0, r["title"] or ""))
        self._populate_video_grid(episodes)
        self.video_grid_canvas.focus_set()
        label = show_key or "(No Show Name)"
        suffix = " on this iPod" if self._tvshow_group_source == "device" else ""
        self.status_var.set(f"{len(episodes)} episode(s){suffix} — {label}")

    def _set_video_toolbar_mode(self, mode):
        """mode: 'local' (this app's library - Add Folder / Add Video /
        Sync) or 'device' (already on the iPod - Remove only). Always
        unpacks everything first and repacks only what belongs, in
        order, rather than toggling individual buttons in place - that
        way "Back to Shows" can always be packed first (leftmost)
        without needing an already-packed sibling to insert before."""
        self._video_grid_source = mode
        for btn in (
            self.video_back_to_shows_button, self.video_add_folder_button,
            self.video_add_file_button, self.video_sync_button,
            self.video_remove_device_button,
        ):
            btn.pack_forget()

        # Only makes sense one level down from the TV Shows grid, and
        # only for TV Shows (Movies has no grouping level to go back to).
        if self.library_mode == "tvshows" and self._tvshow_grid_level == "episodes":
            self.video_back_to_shows_button.pack(side=tk.LEFT, padx=(0, 6))

        if mode == "device":
            self.video_remove_device_button.pack(side=tk.LEFT, padx=(6, 0))
        else:
            self.video_add_folder_button.pack(side=tk.LEFT)
            self.video_add_file_button.pack(side=tk.LEFT, padx=(6, 0))
            self.video_sync_button.pack(side=tk.LEFT, padx=(6, 0))

    def _show_video_context_menu(self, event, video_id):
        if video_id not in self._selected_videos:
            self._selected_videos = {video_id}
            self._refresh_video_tile_selection_borders()
        menu = tk.Menu(self, tearoff=0)
        if self._video_grid_source == "device":
            menu.add_command(label="Remove from iPod", command=self.remove_selected_device_videos)
        else:
            menu.add_command(label="Sync Selected \u2192 iPod", command=self.sync_selected_videos)
            menu.add_command(label="Remove from Library", command=self.remove_selected_videos)
            menu.add_separator()
            # Acts on the one tile right-clicked, regardless of how many
            # are selected - editing info for several at once doesn't
            # map to a single form the way sync/remove do.
            menu.add_command(label="Edit Info (.nfo)\u2026", command=lambda: self._open_video_info_editor(video_id))
        menu.tk_popup(event.x_root, event.y_root)

    def _open_video_info_editor(self, video_id):
        """Entry point for "Edit Info (.nfo)..." - video_id is either a
        real video id (a movie or TV episode tile) or a show-group key
        (a string - see _expand_video_selection) when right-clicked from
        the TV Shows landing grid, which need two different editors:
        one describes a single file, the other a whole show."""
        if self.library_mode == "tvshows" and self._tvshow_grid_level == "shows":
            self._open_show_info_editor(video_id)
            return
        rows = self.lib.videos_by_ids([video_id])
        if not rows:
            messagebox.showinfo("Not Found", "This video couldn't be found in your library.")
            return
        self._open_movie_or_episode_info_editor(rows[0])

    def _invalidate_video_thumbnail_cache(self, video_id):
        for key in [k for k in self._video_photo_cache if k[0] == video_id]:
            del self._video_photo_cache[key]

    # Cap for the per-video/photo/album thumbnail caches below - each
    # cached PhotoImage backs a real pixmap kept alive in the X server
    # for as long as this app keeps a Python reference to it, and none
    # of these caches ever evicted anything on their own: browsing a
    # large library over a long session (lots of different videos,
    # photos, and albums, each cached forever, one entry per item ever
    # viewed) could accumulate enough live pixmaps to exceed the X
    # server's own memory limit - seen as a crash with "BadAlloc
    # (insufficient resources for operation)" on X_CreatePixmap,
    # rather than any ordinary Python error. _cache_insert bounds that
    # by dropping the oldest entries once a cache passes this many
    # items; a dropped PhotoImage that isn't referenced by any widget
    # currently on screen (the common case - grids only keep the
    # tiles actually visible) is freed and its pixmap released.
    _THUMB_CACHE_MAX = 400

    def _cache_insert(self, cache, key, value, max_size=None):
        """Inserts key/value into a thumbnail-photo cache dict,
        evicting the oldest entries once it grows past max_size (see
        _THUMB_CACHE_MAX above). Relies on dict preserving insertion
        order (guaranteed since Python 3.7), so "oldest" here means
        "cached longest ago", not true least-recently-used - good
        enough to bound memory without the bookkeeping of real LRU."""
        cache[key] = value
        max_size = max_size or self._THUMB_CACHE_MAX
        while len(cache) > max_size:
            oldest_key = next(iter(cache))
            del cache[oldest_key]

    def _refresh_current_video_view(self):
        """Re-populates whichever video grid is currently showing, so a
        title/thumbnail/fan-art edit is reflected right away without
        needing to navigate away and back."""
        if self.library_mode == "tvshows":
            if self._tvshow_grid_level == "shows":
                self._show_tvshow_group_grid()
            else:
                self._show_tvshow_episodes(self._current_show_key)
        elif self.library_mode == "movies":
            if self._video_grid_source == "device":
                self.show_movies_on_device()
            else:
                self.show_movies()

    def _choose_image_file(self, title):
        return native_dialogs.choose_open_file(
            title=title, filetypes=[("Image files", "*.jpg *.jpeg *.png *.gif")]
        )

    def _build_image_picker_row(self, parent, row_index, label_text, initial_path):
        """One row of the info editor for an image field (thumbnail or
        fan art): a label, a small read-only path preview, and Browse/
        Clear buttons. Returns a tk.StringVar tracking the chosen path
        (empty string means "cleared")."""
        path_var = tk.StringVar(value=initial_path or "")
        ttk.Label(parent, text=label_text + ":").grid(row=row_index, column=0, sticky="ne", padx=(0, 8), pady=3)
        inner = ttk.Frame(parent)
        inner.grid(row=row_index, column=1, sticky="we", pady=3)
        preview = ttk.Label(
            inner, textvariable=path_var, foreground="#666666",
            width=34, anchor="w",
        )
        preview.pack(side=tk.LEFT, fill=tk.X, expand=True)

        def browse():
            chosen = self._choose_image_file(f"Choose {label_text}")
            if chosen:
                path_var.set(chosen)

        def clear():
            path_var.set("")

        ttk.Button(inner, text="Browse\u2026", command=browse).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(inner, text="Clear", command=clear).pack(side=tk.LEFT, padx=(4, 0))
        return path_var

    def _open_movie_or_episode_info_editor(self, row):
        """The Edit Info editor for one movie or TV episode - title,
        show name/season/episode (episodes only), studio, year, genre,
        plot, and thumbnail/fan-art pickers. Saving writes a Kodi-style
        .nfo sidecar next to the original source file (so it's picked
        up by Kodi or re-reading it into this app later) and updates
        the library row immediately, so the change is visible without
        a rescan."""
        is_episode = row["kind"] == "tvshow"
        source_path = row["source_path"]
        if not source_path:
            messagebox.showwarning(
                "Can't Write .nfo",
                "This video's original source file isn't known (it may have been added "
                "before this app tracked that), so there's nowhere to save a matching "
                ".nfo file. You can still edit the fields below, though - they'll be "
                "saved to your library either way.",
            )

        win = tk.Toplevel(self)
        win.title("Edit Episode Info" if is_episode else "Edit Movie Info")
        win.transient(self)
        win.grab_set()
        win.resizable(False, False)

        form = ttk.Frame(win, padding=12)
        form.pack(fill=tk.BOTH, expand=True)
        form.grid_columnconfigure(1, weight=1)

        entries = {}

        def add_entry(r, key, label_text, initial):
            ttk.Label(form, text=label_text + ":").grid(row=r, column=0, sticky="e", padx=(0, 8), pady=3)
            var = tk.StringVar(value="" if initial is None else str(initial))
            ttk.Entry(form, textvariable=var, width=38).grid(row=r, column=1, sticky="we", pady=3)
            entries[key] = var

        r = 0
        add_entry(r, "title", "Title", row["title"]); r += 1
        if is_episode:
            add_entry(r, "show_name", "Show Name", row["show_name"]); r += 1
            add_entry(r, "season_nr", "Season", row["season_nr"]); r += 1
            add_entry(r, "episode_nr", "Episode", row["episode_nr"]); r += 1
        add_entry(r, "studio", "Studio", _row_get(row, "studio")); r += 1
        add_entry(r, "year", "Year", _row_get(row, "year")); r += 1
        add_entry(r, "genre", "Genre", _row_get(row, "genre")); r += 1

        ttk.Label(form, text="Plot:").grid(row=r, column=0, sticky="ne", padx=(0, 8), pady=3)
        plot_text = tk.Text(form, width=38, height=4, wrap="word")
        plot_text.insert("1.0", _row_get(row, "plot", ""))
        plot_text.grid(row=r, column=1, sticky="we", pady=3)
        r += 1

        thumb_var = self._build_image_picker_row(form, r, "Thumbnail", row["thumbnail_path"]); r += 1
        fanart_key = "show_fanart_path" if is_episode else "fanart_path"
        fanart_label = "Fan Art (this show)" if is_episode else "Fan Art"
        fanart_var = self._build_image_picker_row(form, r, fanart_label, _row_get(row, fanart_key)); r += 1
        if not is_episode:
            ttk.Label(
                form, text="Not shown anywhere in this app yet - saved for .nfo/Kodi compatibility.",
                foreground="#888888", font=(apple_style.FAMILY, 8), wraplength=300, justify=tk.LEFT,
            ).grid(row=r, column=1, sticky="w"); r += 1

        button_row = ttk.Frame(form)
        button_row.grid(row=r, column=0, columnspan=2, sticky="e", pady=(10, 0))

        def do_save():
            season_nr = episode_nr = year = None
            try:
                if is_episode:
                    season_text = entries["season_nr"].get().strip()
                    episode_text = entries["episode_nr"].get().strip()
                    season_nr = int(season_text) if season_text else None
                    episode_nr = int(episode_text) if episode_text else None
                year_text = entries["year"].get().strip()
                year = int(year_text) if year_text else None
            except ValueError:
                messagebox.showerror("Invalid Number", "Season, Episode, and Year must be whole numbers (or left blank).")
                return

            title = entries["title"].get().strip() or None
            studio = entries["studio"].get().strip() or None
            genre = entries["genre"].get().strip() or None
            plot = plot_text.get("1.0", "end").strip() or None
            thumbnail_path = thumb_var.get().strip() or None
            fanart_path = fanart_var.get().strip() or None

            fields = {
                "title": title, "studio": studio, "year": year, "genre": genre,
                "plot": plot, "thumbnail_path": thumbnail_path, fanart_key: fanart_path,
            }
            if is_episode:
                show_name = entries["show_name"].get().strip() or None
                fields["show_name"] = show_name
                fields["season_nr"] = season_nr
                fields["episode_nr"] = episode_nr

            try:
                if source_path:
                    if is_episode:
                        nfo.write_episode_nfo(
                            source_path, title=title, show_name=fields.get("show_name"),
                            season_nr=season_nr, episode_nr=episode_nr, studio=studio,
                            year=year, genre=genre, plot=plot,
                        )
                    else:
                        nfo.write_movie_nfo(
                            source_path, title=title, studio=studio, year=year,
                            genre=genre, plot=plot,
                        )
            except OSError as e:
                messagebox.showwarning(
                    "Saved, But Couldn't Write .nfo",
                    f"Your library was updated, but the .nfo file itself couldn't be "
                    f"written ({e}). This app's own copy of the info is still saved.",
                )

            self.lib.update_video_fields(row["id"], fields)
            self._invalidate_video_thumbnail_cache(row["id"])
            applog.log(f"VIDEO INFO: updated '{title or row['title']}' (id {row['id']})")
            win.destroy()
            self._refresh_current_video_view()

        ttk.Button(button_row, text="Cancel", command=win.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(button_row, text="Save", command=do_save).pack(side=tk.RIGHT)

    def _open_show_info_editor(self, show_key):
        """The Edit Info editor for a whole TV show (right-clicked from
        the TV Shows landing grid) - show name, studio, year, genre,
        plot, and fan art, applied to every episode of that show at
        once (see Library.update_video_fields_by_show), plus a
        show-level tvshow.nfo written alongside the show's fan art (or
        its first episode, if no fan art is set) for Kodi compatibility."""
        episodes = self._tvshow_groups.get(show_key, [])
        if not episodes:
            return
        first = episodes[0]

        win = tk.Toplevel(self)
        win.title("Edit Show Info")
        win.transient(self)
        win.grab_set()
        win.resizable(False, False)

        form = ttk.Frame(win, padding=12)
        form.pack(fill=tk.BOTH, expand=True)
        form.grid_columnconfigure(1, weight=1)

        entries = {}

        def add_entry(r, key, label_text, initial):
            ttk.Label(form, text=label_text + ":").grid(row=r, column=0, sticky="e", padx=(0, 8), pady=3)
            var = tk.StringVar(value="" if initial is None else str(initial))
            ttk.Entry(form, textvariable=var, width=38).grid(row=r, column=1, sticky="we", pady=3)
            entries[key] = var

        r = 0
        add_entry(r, "show_name", "Show Name", show_key); r += 1
        add_entry(r, "studio", "Studio", _row_get(first, "studio")); r += 1
        add_entry(r, "year", "Year", _row_get(first, "year")); r += 1
        add_entry(r, "genre", "Genre", _row_get(first, "genre")); r += 1

        ttk.Label(form, text="Plot:").grid(row=r, column=0, sticky="ne", padx=(0, 8), pady=3)
        plot_text = tk.Text(form, width=38, height=4, wrap="word")
        plot_text.insert("1.0", _row_get(first, "plot", ""))
        plot_text.grid(row=r, column=1, sticky="we", pady=3)
        r += 1

        existing_fanart = next(
            (p for p in (_row_get(ep, "show_fanart_path") for ep in episodes) if p), None
        )
        fanart_var = self._build_image_picker_row(form, r, "Fan Art", existing_fanart); r += 1
        ttk.Label(
            form, text=f"Applies to all {len(episodes)} episode(s) of this show.",
            foreground="#888888", font=(apple_style.FAMILY, 8),
        ).grid(row=r, column=1, sticky="w"); r += 1

        button_row = ttk.Frame(form)
        button_row.grid(row=r, column=0, columnspan=2, sticky="e", pady=(10, 0))

        def do_save():
            try:
                year_text = entries["year"].get().strip()
                year = int(year_text) if year_text else None
            except ValueError:
                messagebox.showerror("Invalid Number", "Year must be a whole number (or left blank).")
                return

            new_show_name = entries["show_name"].get().strip() or None
            studio = entries["studio"].get().strip() or None
            genre = entries["genre"].get().strip() or None
            plot = plot_text.get("1.0", "end").strip() or None
            fanart_path = fanart_var.get().strip() or None

            fields = {
                "show_name": new_show_name, "studio": studio, "year": year,
                "genre": genre, "plot": plot, "show_fanart_path": fanart_path,
            }

            try:
                # tvshow.nfo is a folder-level file, not a sidecar of
                # one video - written next to the chosen fan art when
                # there is one (clearly "the show's own folder"),
                # falling back to the first episode's own folder.
                target_dir = os.path.dirname(fanart_path) if fanart_path else os.path.dirname(first["source_path"] or "")
                if target_dir and os.path.isdir(target_dir):
                    nfo.write_tvshow_nfo(
                        target_dir, show_name=new_show_name or show_key, studio=studio,
                        year=year, genre=genre, plot=plot,
                    )
            except OSError as e:
                messagebox.showwarning(
                    "Saved, But Couldn't Write tvshow.nfo",
                    f"Your library was updated, but tvshow.nfo itself couldn't be "
                    f"written ({e}). This app's own copy of the info is still saved.",
                )

            self.lib.update_video_fields_by_show(show_key, fields)
            for ep in episodes:
                self._invalidate_video_thumbnail_cache(ep["id"])
            applog.log(f"VIDEO INFO: updated show '{new_show_name or show_key}' ({len(episodes)} episode(s))")
            win.destroy()
            self._refresh_current_video_view()

        ttk.Button(button_row, text="Cancel", command=win.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(button_row, text="Save", command=do_save).pack(side=tk.RIGHT)

    def _expand_video_selection(self, ids):
        """While browsing the TV Shows grid at the "shows" level,
        _selected_videos holds show-group keys, not real episode ids -
        this expands each selected show into every episode it groups
        (same convenience as selecting a whole album to sync/remove
        all its tracks at once). A no-op once drilled into a show's
        own episode list, where ids are already real."""
        if self.library_mode != "tvshows" or self._tvshow_grid_level != "shows":
            return list(ids)
        expanded = []
        for key in ids:
            expanded.extend(row["id"] for row in self._tvshow_groups.get(key, []))
        return expanded

    def remove_selected_videos(self):
        if not self._selected_videos:
            return
        video_ids = self._expand_video_selection(self._selected_videos)
        count = len(video_ids)
        if not messagebox.askyesno(
            "Remove from Library",
            f"Remove {count} video(s) from your library and delete their converted "
            "copy on disk? Your original source file(s) are never touched.\n\n"
            "If any of these are on an iPod, they'll be removed from it too the "
            "next time it connects.",
        ):
            return
        rows = self.lib.videos_by_ids(video_ids)
        file_errors = []
        for row in rows:
            # filepath is always this app's own converted/atom-tagged
            # copy, never the original the user pointed it at
            # (source_path) - convert()/ensure_ipod_atom() always write
            # into the chosen converted-videos folder, so this can only
            # equal source_path if something upstream changed that
            # invariant, and the check below is the safety net for
            # exactly that case.
            filepath = row["filepath"]
            if filepath and filepath != row["source_path"] and os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except OSError as e:
                    file_errors.append(f"{row['title'] or filepath}: {e}")
            # The iPod might not be connected right now (or might not
            # even be the one this video was ever synced to) - record
            # it so the next successful device read removes any
            # matching track instead of trying right away.
            self.lib.add_pending_video_removal(row["title"], row["duration_ms"])
            # The original source file is never touched (see above), so
            # it's still sitting in a tracked video folder - without
            # this, the next refresh would just rediscover and
            # re-add/re-convert it, undoing the removal.
            self.lib.mark_video_source_deleted(row["source_path"])
        for video_id in video_ids:
            self.lib.delete_video(video_id)
        applog.log(f"VIDEO LIBRARY: removed {count} video(s)")
        if file_errors:
            applog.log(f"VIDEO LIBRARY delete errors ({len(file_errors)}): " + " | ".join(file_errors))
            messagebox.showwarning(
                "Removed, but some files couldn't be deleted",
                "\n".join(file_errors[:10]) + (f"\n…and {len(file_errors) - 10} more" if len(file_errors) > 10 else ""),
            )
        if self.library_mode == "movies":
            self.show_movies()
        else:
            self._show_tvshow_group_grid()

    def remove_selected_device_videos(self):
        if not self.ipod_mountpoint:
            return
        if not self._selected_videos:
            messagebox.showinfo("Nothing selected", "Select one or more videos first.")
            return
        dbids = self._expand_video_selection(self._selected_videos)
        count = len(dbids)
        if not messagebox.askyesno(
            "Remove from iPod",
            f"Remove {count} video(s) from this iPod? This can't be undone.",
            icon="warning",
        ):
            return
        applog.log(f"DEVICE VIDEO REMOVE: requested {count} video(s) from {self.ipod_mountpoint}")
        self.status_var.set(f"Removing {count} video(s) from iPod\u2026")
        self.video_remove_device_button.configure(state="disabled")

        def work():
            try:
                removed, errors, notes = ipod_backend.remove_tracks(self.ipod_mountpoint, dbids)
                self.work_queue.put(("device_video_remove_done", (removed, errors, notes)))
            except ipod_backend.IpodError as e:
                self.work_queue.put(("error", str(e)))
            except Exception:
                # Anything other than IpodError used to just kill this
                # background thread silently - no dialog, no log line,
                # the progress bar and buttons left stuck as they were
                # mid-sync. That's indistinguishable from "syncing just
                # stopped working" with nothing to go on. Catch broadly
                # here, log the real traceback so a real cause (e.g. a
                # non-ASCII filename/title tripping something not
                # expecting it) is actually visible, and still let the
                # UI recover via the normal "error" handler.
                applog.log("SYNC WORKER CRASHED:\n" + traceback.format_exc())
                self.work_queue.put((
                    "error",
                    "Something went wrong during this operation (see the log file "
                    "for details). It's been logged so it can be tracked down.",
                ))

        threading.Thread(target=work, daemon=True).start()

    def _build_photo_grid_panel(self):
        self.photo_list_frame = ttk.Frame(self.main_area)

        toolbar = ttk.Frame(self.photo_list_frame)
        toolbar.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))
        self.photo_add_folder_button = ttk.Button(
            toolbar, text="Add Photo Folder\u2026", command=self.add_photo_folder
        )
        self.photo_add_folder_button.pack(side=tk.LEFT)
        self.photo_sync_button = ttk.Button(
            toolbar, text="Sync Selected \u2192 iPod", command=self.sync_selected_photos
        )
        self.photo_sync_button.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(
            toolbar, text="Remove Duplicates on iPod", command=self.remove_duplicate_photos
        ).pack(side=tk.LEFT, padx=(6, 0))

        grid_row = ttk.Frame(self.photo_list_frame)
        grid_row.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(grid_row, highlightthickness=0)
        vsb = ttk.Scrollbar(grid_row, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)

        self.photo_grid_canvas = canvas
        self._default_photo_canvas_bg = canvas.cget("bg")
        self.photo_grid_inner = ttk.Frame(canvas)
        self._photo_grid_window = canvas.create_window((0, 0), window=self.photo_grid_inner, anchor="nw")

        def on_inner_configure(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def on_canvas_configure(event):
            canvas.itemconfig(self._photo_grid_window, width=event.width)
            self._reflow_photo_grid()

        self.photo_grid_inner.bind("<Configure>", on_inner_configure)
        canvas.bind("<Configure>", on_canvas_configure)

        def on_mousewheel(event):
            # X11 reports the wheel as Button-4 (up) / Button-5 (down)
            # rather than a MouseWheel delta - event.delta is always 0
            # here, so the previous "event.delta > 0" check never did
            # anything and this ended up scrolling the opposite way of
            # every other app: wheel-up (Button-4) should move the view
            # up (yview_scroll(-1)), wheel-down (Button-5) should move
            # it down (+1).
            delta = -1 if event.num == 4 else 1
            apple_style.smooth_scroll(canvas, delta, self._smooth_motion())

        canvas.bind("<Enter>", lambda e: (
            canvas.bind_all("<MouseWheel>", lambda ev: apple_style.smooth_scroll(
                canvas, -1 if ev.delta > 0 else 1, self._smooth_motion())),
            canvas.bind_all("<Button-4>", on_mousewheel),
            canvas.bind_all("<Button-5>", on_mousewheel),
        ))
        canvas.bind("<Leave>", lambda e: (
            canvas.unbind_all("<MouseWheel>"),
            canvas.unbind_all("<Button-4>"),
            canvas.unbind_all("<Button-5>"),
        ))
        canvas.bind("<Control-a>", self.select_all_tracks)
        canvas.bind("<Control-A>", self.select_all_tracks)

        self._photo_rows = []
        self._photo_tile_frames = []
        self._photo_tile_frames_by_id = {}
        self._selected_photos = set()   # {photo_id, ...}
        self._photo_photo_cache = {}    # photo_id -> ImageTk.PhotoImage (must keep a ref)

    def _populate_photo_grid(self, rows):
        for child in self.photo_grid_inner.winfo_children():
            child.destroy()
        self._photo_rows = rows
        self._photo_tile_frames = []
        self._photo_tile_frames_by_id = {}
        # Durable sync profile, same reasoning as the album/video grids.
        if self._device_view_active:
            marked = self.lib.all_marked_for_sync("photo")
            self._selected_photos = {int(p) for p in marked if p.isdigit()}
        else:
            self._selected_photos = set()

        for row in rows:
            frame = self._make_photo_tile(self.photo_grid_inner, row)
            self._photo_tile_frames.append(frame)
        self._reflow_photo_grid()

    def _reflow_photo_grid(self):
        try:
            canvas_width = self.photo_grid_canvas.winfo_width() or 900
            columns = max(1, canvas_width // self.PHOTO_TILE_WIDTH)
            # No column weight - see _reflow_video_grid for why: it
            # keeps tiles packed from the left instead of stretched out
            # across any leftover canvas width.
            for i, frame in enumerate(self._photo_tile_frames):
                r, c = divmod(i, columns)
                frame.grid(row=r, column=c, padx=3, pady=3, sticky="nw")
        except Exception:
            self._show_error_with_traceback("Photo grid failed to lay out")

    def _get_photo_thumbnail_photo(self, row):
        photo_id = row["id"]
        size = self.PHOTO_THUMB_SIZE
        cache_key = (photo_id, size)
        cached = self._photo_photo_cache.get(cache_key)
        if cached is not None:
            return cached
        thumb_path = row["thumbnail_path"] if "thumbnail_path" in row.keys() else None
        if thumb_path and HAVE_PIL and os.path.exists(thumb_path):
            try:
                from PIL import Image
                img = Image.open(thumb_path)
                img.thumbnail(size)
                photo = ImageTk.PhotoImage(img)
                self._cache_insert(self._photo_photo_cache, cache_key, photo)
                return photo
            except Exception:
                pass
        return self._placeholder_photo(os.path.basename(row["filepath"]), size=max(size))

    def _make_photo_tile(self, parent, row):
        photo_id = row["id"]
        selected = photo_id in self._selected_photos
        accent = "#0a67e5"

        tile = tk.Frame(
            parent, width=self.PHOTO_TILE_WIDTH - 10,
            highlightthickness=3,
        )
        self._mark_tile(tile, selected)
        if self._smooth_motion():
            tile.configure(bg=apple_style.CONTENT)
        self._photo_tile_frames_by_id[photo_id] = tile

        photo_img = self._get_photo_thumbnail_photo(row)
        art_label = tk.Label(tile, image=photo_img, cursor="hand2", bg=tile.cget("bg"))
        art_label.image = photo_img
        art_label.pack(padx=3, pady=3)

        widgets_to_bind = [tile, art_label]
        album_name = row["album_name"] if "album_name" in row.keys() else None
        if album_name:
            album_label = ttk.Label(
                tile, text=album_name, wraplength=self.PHOTO_TILE_WIDTH - 16,
                justify=tk.CENTER, foreground="#666666", font=(apple_style.FAMILY, 8),
            )
            album_label.pack(pady=(0, 3))
            widgets_to_bind.append(album_label)

        for widget in widgets_to_bind:
            widget.bind("<Button-1>", lambda e, pid=photo_id: self._toggle_photo_selection(pid))
            widget.bind("<Button-3>", lambda e, pid=photo_id: self._show_photo_context_menu(e, pid))

        return tile

    def _toggle_photo_selection(self, photo_id):
        if photo_id in self._selected_photos:
            self._selected_photos.discard(photo_id)
        else:
            self._selected_photos.add(photo_id)
        tile = self._photo_tile_frames_by_id.get(photo_id)
        if tile is not None and tile.winfo_exists():
            self._mark_tile(tile, photo_id in self._selected_photos)
        n = len(self._selected_photos)
        if n:
            self.status_var.set(f"{n} photo(s) selected.")
        self._persist_photo_marks()

    def _persist_photo_marks(self):
        """Writes the current photo selection to disk as the durable
        sync profile - same reasoning as _persist_video_marks/
        _persist_album_marks."""
        if self._device_view_active:
            self.lib.set_marked_for_sync("photo", [str(p) for p in self._selected_photos])

    def _refresh_photo_tile_selection_borders(self):
        for pid, tile in self._photo_tile_frames_by_id.items():
            if tile.winfo_exists():
                self._mark_tile(tile, pid in self._selected_photos)

    def show_photos(self):
        self.current_view = self.VIEW_PHOTOS
        self._show_body("photos")
        rows = self.lib.all_photos()
        self._populate_photo_grid(rows)
        self.photo_grid_canvas.focus_set()
        self.status_var.set(f"{len(rows)} photo(s)")

    def _show_photo_context_menu(self, event, photo_id):
        if photo_id not in self._selected_photos:
            self._selected_photos = {photo_id}
            self._refresh_photo_tile_selection_borders()
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Sync Selected \u2192 iPod", command=self.sync_selected_photos)
        menu.add_command(label="Remove from Library", command=self.remove_selected_photos)
        menu.tk_popup(event.x_root, event.y_root)

    def remove_selected_photos(self):
        if not self._selected_photos:
            return
        count = len(self._selected_photos)
        if not messagebox.askyesno(
            "Remove from Library",
            f"Remove {count} photo(s) from your library? The files on disk aren't touched.",
        ):
            return
        for photo_id in list(self._selected_photos):
            self.lib.delete_photo(photo_id)
        applog.log(f"PHOTO LIBRARY: removed {count} photo(s)")
        self.show_photos()

    def _build_playlist_list_panel(self):
        """A multi-select playlist browser - distinct from the
        individual playlist entries already in the sidebar (which each
        navigate into one playlist's own track table): this lets you
        select several whole playlists at once and sync all of their
        tracks together, the same "select tiles/rows, then Sync" pattern
        Albums/Movies/Photos already use. Reached by clicking the
        "Playlists" folder itself in the device view sidebar."""
        self.playlist_list_frame = ttk.Frame(self.main_area)

        toolbar = ttk.Frame(self.playlist_list_frame)
        toolbar.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))
        self.playlist_sync_button = ttk.Button(
            toolbar, text="Sync \u2192 iPod", command=self._device_view_sync_selected
        )
        self.playlist_sync_button.pack(side=tk.LEFT)
        ttk.Label(
            toolbar,
            text="Tick the playlists you want on the iPod, then press Sync. "
                 "Each one is copied over as a playlist with its songs.",
        ).pack(side=tk.LEFT, padx=(12, 0))

        table_row = ttk.Frame(self.playlist_list_frame)
        table_row.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        columns = ("sync", "name", "count")
        tree = ttk.Treeview(table_row, columns=columns, show="headings", selectmode="none")
        tree.heading("sync", text="Sync")
        tree.heading("name", text="Playlist")
        tree.heading("count", text="Tracks")
        tree.column("sync", width=50, anchor=tk.CENTER, stretch=False)
        tree.column("name", width=280, anchor=tk.W)
        tree.column("count", width=80, anchor=tk.E)
        vsb = ttk.Scrollbar(table_row, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)
        tree.bind("<Button-1>", self._on_playlist_list_click)
        tree.bind("<space>", self._on_playlist_list_click)
        tree.bind("<Control-a>", self.select_all_tracks)
        tree.bind("<Control-A>", self.select_all_tracks)
        self.playlist_list_tree = tree
        self._playlist_list_rows = []

    def _populate_playlist_list(self):
        rows = self.lib.all_playlists_with_counts()
        self._playlist_list_rows = rows
        self.playlist_list_tree.delete(*self.playlist_list_tree.get_children())
        for row in rows:
            self.playlist_list_tree.insert(
                "", "end", iid=f"pl:{row['id']}", values=("", row["name"], row["track_count"])
            )
        self._refresh_playlist_ticks()

    def _refresh_playlist_ticks(self):
        tree = self.playlist_list_tree
        for iid in tree.get_children():
            ticked = int(iid.split(":", 1)[1]) in self._selected_playlist_ids
            tree.set(iid, "sync", "\u2611" if ticked else "\u2610")

    def _on_playlist_list_click(self, event=None):
        """A click anywhere on a playlist's row ticks or unticks it."""
        tree = self.playlist_list_tree
        if event is not None and str(event.type) in ("ButtonPress", "4"):
            if tree.identify_region(event.x, event.y) == "heading":
                return None
            iid = tree.identify_row(event.y)
        else:
            iid = tree.focus()
        if not iid:
            return None
        tree.focus(iid)
        pid = int(iid.split(":", 1)[1])
        if pid in self._selected_playlist_ids:
            self._selected_playlist_ids.discard(pid)
        else:
            self._selected_playlist_ids.add(pid)
        self._on_playlist_marks_changed()
        return "break"

    def _on_playlist_marks_changed(self):
        self._refresh_playlist_ticks()
        # Durable sync profile, same reasoning as _persist_album_marks.
        if self._device_view_active:
            self.lib.set_marked_for_sync("playlist", [str(pid) for pid in self._selected_playlist_ids])
        n = len(self._selected_playlist_ids)
        self.status_var.set(f"{n} playlist(s) ticked to sync." if n else "No playlists ticked.")

    def show_playlist_list(self):
        self.current_view = self.VIEW_PLAYLIST_LIST
        self._show_body("playlist_list")
        self._populate_playlist_list()
        self.playlist_list_tree.focus_set()
        self.status_var.set(f"{len(self._playlist_list_rows)} playlist(s)")

    def sync_selected_playlists(self):
        if not self.ipod_mountpoint:
            messagebox.showwarning("No iPod", "Detect or choose an iPod folder first (iPod menu).")
            return
        if not self._selected_playlist_ids:
            messagebox.showinfo("Nothing selected", "Select one or more playlists first.")
            return
        seen_ids = set()
        rows = []
        for playlist_id in self._selected_playlist_ids:
            for t in self.lib.playlist_tracks(playlist_id):
                if t["id"] not in seen_ids:
                    seen_ids.add(t["id"])
                    rows.append(t)
        self._sync_rows_to_ipod(rows)

    def _build_ipod_info_panel(self):
        self.ipod_info_frame = ttk.Frame(self.main_area)
        # Scrollable: the page is taller than most windows, and without
        # this everything below Sync options (Wipe, Firmware) was cut off.
        canvas = tk.Canvas(self.ipod_info_frame, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(self.ipod_info_frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)
        self.ipod_info_canvas = canvas
        self.ipod_info_inner = ttk.Frame(canvas, padding=20)
        inner = self.ipod_info_inner
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(window, width=e.width))

        def on_wheel(event):
            if event.num in (4, 5):
                direction = -1 if event.num == 4 else 1
            else:
                direction = -1 if event.delta > 0 else 1
            apple_style.smooth_scroll(canvas, direction, self._smooth_motion())

        def bind_wheel(_e):
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                canvas.bind_all(seq, on_wheel)

        def unbind_wheel(_e):
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                canvas.unbind_all(seq)

        self.ipod_info_frame.bind("<Enter>", bind_wheel)
        self.ipod_info_frame.bind("<Leave>", unbind_wheel)

        self.ipod_info_title_var = tk.StringVar(value="About This iPod")
        title_row = ttk.Frame(inner)
        title_row.pack(fill=tk.X, pady=(0, 16))
        ttk.Label(title_row, textvariable=self.ipod_info_title_var, font=(apple_style.FAMILY, 18, "bold")).pack(
            side=tk.LEFT
        )
        ttk.Button(title_row, text="Rename\u2026", command=self.rename_ipod_dialog).pack(
            side=tk.LEFT, padx=(12, 0)
        )

        contents_section = ttk.LabelFrame(inner, text="Contents", padding=12)
        contents_section.pack(fill=tk.X, pady=(0, 16))
        self.ipod_songs_var = tk.StringVar(value="\u2014")
        self.ipod_albums_var = tk.StringVar(value="\u2014")
        self.ipod_hours_var = tk.StringVar(value="\u2014")
        for label_text, var in (
            ("Songs:", self.ipod_songs_var),
            ("Albums:", self.ipod_albums_var),
            ("Total time:", self.ipod_hours_var),
        ):
            row = ttk.Frame(contents_section)
            row.pack(fill=tk.X, pady=2)
            ttk.Label(row, text=label_text, width=12, anchor="w").pack(side=tk.LEFT)
            ttk.Label(row, textvariable=var).pack(side=tk.LEFT)

        storage_section = ttk.LabelFrame(inner, text="Storage", padding=12)
        storage_section.pack(fill=tk.X, pady=(0, 16))
        self.ipod_storage_var = tk.StringVar(value="\u2014")
        ttk.Label(storage_section, textvariable=self.ipod_storage_var).pack(anchor="w")

        sync_options_section = ttk.LabelFrame(inner, text="Sync options", padding=12)
        sync_options_section.pack(fill=tk.X, pady=(0, 16))
        self.auto_sync_var = tk.BooleanVar(value=self.app_settings.get("auto_sync", False))
        ttk.Checkbutton(
            sync_options_section,
            text="Automatically sync ticked music whenever this iPod is connected",
            variable=self.auto_sync_var,
            command=self.toggle_auto_sync,
        ).pack(anchor="w")
        ttk.Label(
            sync_options_section,
            text="Runs once per connection, right after the iPod's library is read.\n"
                 "Copies the songs, albums and playlists ticked in View Connected\n"
                 "Device that aren't on the iPod yet (everything, with the option\n"
                 "below on). It never removes anything.",
            foreground="#888888", font=(apple_style.FAMILY, 8), justify=tk.LEFT,
        ).pack(anchor="w", pady=(4, 0))

        self.sync_all_music_var = tk.BooleanVar(value=self.app_settings.get("sync_all_music", False))
        ttk.Checkbutton(
            sync_options_section,
            text="Always sync all music to this iPod, not just what's selected",
            variable=self.sync_all_music_var,
            command=self.toggle_sync_all_music,
        ).pack(anchor="w", pady=(10, 0))
        ttk.Label(
            sync_options_section,
            text="With this on, the Sync button (Songs/Albums/Artists/Genres/\n"
                 "Playlists) copies your whole local music library instead of only\n"
                 "your saved selection - the same duplicate-skip as always applies,\n"
                 "so repeat syncs still only copy what's actually new. Your saved\n"
                 "selection itself is kept either way, just not used while this is on.",
            foreground="#888888", font=(apple_style.FAMILY, 8), justify=tk.LEFT,
        ).pack(anchor="w", pady=(4, 0))

        ttk.Button(
            sync_options_section, text="Set iPod Model\u2026", command=self.set_ipod_model_dialog
        ).pack(anchor="w", pady=(10, 0))
        ttk.Button(
            sync_options_section, text="Clean Up Leftover Files\u2026", command=self.check_leftover_files
        ).pack(anchor="w", pady=(10, 0))
        ttk.Label(
            sync_options_section,
            text="Finds song files left on the iPod that aren't in its song list\n"
                 "(they show up as Other space) and songs whose file is gone.",
            foreground="#888888", font=(apple_style.FAMILY, 8), justify=tk.LEFT,
        ).pack(anchor="w", pady=(4, 0))
        ttk.Label(
            sync_options_section,
            text="Some iPods (5.5-gen and later) ship with an empty on-device\n"
                 "info file, so libgpod can't tell what model it is — without\n"
                 "that, cover art can fail to write at all, even when a sync\n"
                 "reports success. This writes the model number directly onto\n"
                 "the iPod (the same fix gtkpod uses) and immediately checks\n"
                 "whether it actually worked, rather than just confirming the\n"
                 "write happened. Use the exact model number for your device\n"
                 "(e.g. MA446 for a 30GB black 5.5G) — find it via its serial\n"
                 "number on Apple's support site or a site like everymac.com.",
            foreground="#888888", font=(apple_style.FAMILY, 8), justify=tk.LEFT,
        ).pack(anchor="w", pady=(4, 0))

        maint_section = ttk.LabelFrame(inner, text="iPod maintenance", padding=12)
        maint_section.pack(fill=tk.X)
        ttk.Button(maint_section, text="Wipe iPod Library", command=self.wipe_ipod_library).pack(
            side=tk.LEFT
        )
        ttk.Label(
            maint_section,
            text="Removes every track from this iPod. This doesn't touch its\n"
                 "firmware — it can't fix a device that won't turn on or boot, only\n"
                 "clear its music library back to empty.",
            foreground="#888888", font=(apple_style.FAMILY, 8), justify=tk.LEFT,
        ).pack(side=tk.LEFT, padx=(12, 0))

        fw_section = ttk.LabelFrame(inner, text="Firmware", padding=12)
        fw_section.pack(fill=tk.X, pady=(16, 0))
        fw_row = ttk.Frame(fw_section)
        fw_row.pack(fill=tk.X)
        ttk.Button(fw_row, text="Import Firmware…", command=self.import_firmware_dialog).pack(side=tk.LEFT)
        self.firmware_status_var = tk.StringVar(value=self._firmware_status_text())
        ttk.Label(fw_section, textvariable=self.firmware_status_var).pack(anchor="w", pady=(8, 0))
        ttk.Label(
            fw_section,
            text="For 5th-gen (Video) iPods. Import Apple's restore file for your iPod\n"
                 "(an .ipsw with a Firmware-25 file inside) to update or repair its\n"
                 "firmware. Your music stays. Installing asks for your password, since\n"
                 "it writes to the iPod's disk directly.",
            foreground="#888888", font=(apple_style.FAMILY, 8), justify=tk.LEFT,
        ).pack(anchor="w", pady=(4, 0))

    def _populate_ipod_info(self):
        label = self.ipod_label or "iPod"
        self.ipod_info_title_var.set(f"About {label}")

        tracks = self._device_music_tracks()
        self.ipod_songs_var.set(str(len(tracks)))

        albums = {t.get("album") for t in tracks if t.get("album")}
        self.ipod_albums_var.set(str(len(albums)))

        total_seconds = sum(t.get("duration_ms") or 0 for t in tracks) / 1000
        hours = total_seconds / 3600
        if hours >= 1:
            self.ipod_hours_var.set(f"{hours:.1f} hours")
        else:
            self.ipod_hours_var.set(f"{total_seconds / 60:.0f} minutes")

        if self.ipod_mountpoint and os.path.isdir(self.ipod_mountpoint):
            try:
                usage = shutil.disk_usage(self.ipod_mountpoint)
                self.ipod_storage_var.set(
                    f"{format_bytes(usage.used)} used of {format_bytes(usage.total)} total "
                    f"({format_bytes(usage.free)} free)"
                )
            except OSError:
                self.ipod_storage_var.set("Unavailable")
        else:
            self.ipod_storage_var.set("Unavailable")

    def show_ipod_info(self):
        if not self.ipod_mountpoint:
            messagebox.showwarning("No iPod", "Detect or choose an iPod folder first (iPod menu).")
            return
        entering = not self._device_view_active
        self._device_view_active = True
        if entering:
            self._load_device_view_selections()
            self._unticked_track_ids = set()
        self.current_view = self.VIEW_IPOD_INFO
        self._show_body("ipod_info")
        self._populate_ipod_info()
        self.status_var.set(f"About {self.ipod_label or 'iPod'}")
        if entering:
            # refresh_sidebar() rebuilds the tree and, since
            # _device_view_active is now true, builds the device
            # sidebar instead of the normal one. This is guarded by
            # `entering` so it only happens on the transition into
            # device view, not on every call this method makes (e.g.
            # the re-entrant one selection_set() below triggers) -
            # otherwise every click back onto Summary would rebuild
            # the whole sidebar for no reason.
            self.refresh_sidebar()
            if self.sidebar.exists("dv_summary"):
                self.sidebar.selection_set("dv_summary")

    def close_device_view(self):
        """The Summary screen's Done button - leaves the device view
        and returns to the normal library sidebar. Songs and albums
        unticked during the visit are removed from the iPod first (the
        Sync button does the same, as part of its full reconcile)."""
        # Albums still waiting for their removal are covered here.
        if self._albums_remove_after_id:
            self.after_cancel(self._albums_remove_after_id)
            self._albums_remove_after_id = None
        self._albums_to_remove = set()
        unticked = self._unticked_tracks_on_device()
        if unticked and (self._active_sync_handle is not None or self._post_remove_callback is not None):
            # Used to skip the removal silently here; keep the list and stay.
            messagebox.showinfo(
                "iPod Busy",
                "The iPod is still finishing another job. Press Done again when it's "
                f"finished, and the {len(unticked)} unticked song(s) will be removed.",
            )
            return
        self._unticked_track_ids = set()
        if unticked:
            if self._confirm_reconcile_removal(unticked, "track"):
                self._remove_dbids_then(
                    [t["dbid"] for t in unticked], lambda: None, label="REMOVE UNTICKED"
                )
        self._device_view_active = False
        self._clear_device_view_selections()
        self.current_view = self.VIEW_LIBRARY
        self._show_body("table")
        self.refresh_sidebar()
        self.show_library()

    def _load_device_view_selections(self):
        """Loads the durable sync profile from disk into the in-memory
        selection state, on entering the device view - this is what
        makes it function as a real sync profile (set up once, reused
        every time you reconnect or reopen the app) rather than
        something you re-pick every visit. Device-side ("d:") track
        selections are deliberately never part of this profile (see
        _on_table_selection_changed) since they're tied to whatever
        happens to be connected right now, not a durable choice."""
        self._selected_albums = {
            _album_sync_key_decode(k) for k in self.lib.all_marked_for_sync("album")
        }
        self._persistent_table_selection = {
            f"t:{tid}" for tid in self.lib.all_marked_for_sync("track")
        }
        for album, artist in self._selected_albums:
            self._persistent_table_selection.update(
                f"t:{r['id']}" for r in self.lib.tracks_by_album(album, artist)
            )
        self._selected_playlist_ids = {int(pid) for pid in self.lib.all_marked_for_sync("playlist")}
        self._name_list_selection = {}
        marked_artists = self.lib.all_marked_for_sync("artist")
        if marked_artists:
            self._name_list_selection[self.artist_tree] = next(iter(marked_artists))
        marked_genres = self.lib.all_marked_for_sync("genre")
        if marked_genres:
            self._name_list_selection[self.genre_tree] = next(iter(marked_genres))
        self._selected_videos = {
            int(v) for v in self.lib.all_marked_for_sync("video") if v.lstrip("-").isdigit()
        }
        self._selected_photos = {
            int(p) for p in self.lib.all_marked_for_sync("photo") if p.isdigit()
        }

    def _clear_device_view_selections(self):
        """Clears in-memory selection state on leaving the device view.
        The durable profile itself lives on disk (see
        _load_device_view_selections) and isn't touched here - this
        just empties the in-memory display state, which the normal
        library never reads anyway (each view is gated on
        self._device_view_active), but keeping it genuinely empty when
        unused is cleaner than leaving stale state sitting around."""
        self._selected_albums = set()
        self._persistent_table_selection = set()
        self._name_list_selection = {}
        self._selected_playlist_ids = set()
        self._selected_videos = set()
        self._selected_photos = set()

    def _build_placeholder_panel(self):
        """A generic 'not supported here' screen, shared by the Movies/
        TV Shows/Photos entries under both Settings and On My Device in
        the device view - this app manages music only, so there's
        nothing real to show for any of them yet."""
        self.placeholder_frame = ttk.Frame(self.main_area)
        self.placeholder_inner = ttk.Frame(self.placeholder_frame, padding=20)
        inner = self.placeholder_inner
        inner.pack(side=tk.TOP, fill=tk.BOTH, anchor="n")

        self.placeholder_title_var = tk.StringVar(value="")
        ttk.Label(inner, textvariable=self.placeholder_title_var, font=(apple_style.FAMILY, 18, "bold")).pack(
            anchor="w", pady=(0, 12)
        )
        self.placeholder_body_var = tk.StringVar(value="")
        ttk.Label(
            inner, textvariable=self.placeholder_body_var,
            foreground="#888888", justify=tk.LEFT, wraplength=420,
        ).pack(anchor="w")

    def show_placeholder(self, title, body):
        self._show_body("placeholder")
        self.placeholder_title_var.set(title)
        self.placeholder_body_var.set(body)
        self.status_var.set(title)

    def _update_ipod_info_button(self):
        """Enables/disables the menu bar's View Connected Device entry
        to match whether an iPod is actually connected - called from
        refresh_sidebar() since that already runs at every
        connect/disconnect/eject."""
        if self.ipod_mountpoint:
            self.menubar.entryconfig(self.device_menu_index, state="normal")
        else:
            self.menubar.entryconfig(self.device_menu_index, state="disabled")
            if self._device_view_active:
                # The device went away while the device view was open -
                # drop out of it. The caller (refresh_sidebar) rebuilds
                # the sidebar right after this returns, so clearing the
                # flag here is enough to bring the normal sidebar back;
                # don't call refresh_sidebar() from here or it would
                # recurse into its own caller.
                self._device_view_active = False
                self._show_body("table")
                self.show_library()

    def _build_device_card(self):
        """The device card at the top of the sidebar, styled after the
        iPod itself: a dark graphite card with a silver iPod picture,
        name, track count/hours, an iTunes-style color capacity bar
        (audio / video / other / free) with a legend, Eject, and a link
        to the device view (or to detect an iPod when none is
        connected). Built once; shown only in the Apple-style layout."""
        card = ttk.Frame(self, style="DarkCard.TFrame", padding=(12, 12, 10, 12))
        self.device_card = card

        top = ttk.Frame(card, style="DarkCardInner.TFrame")
        top.pack(fill=tk.X)
        self._device_icon_photo = apple_style.ipod_icon_photo(silver=True)
        if self._device_icon_photo is not None:
            ttk.Label(top, image=self._device_icon_photo, style="DarkCard.TLabel").pack(side=tk.LEFT, padx=(0, 10))
        names = ttk.Frame(top, style="DarkCardInner.TFrame")
        names.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.device_card_name_var = tk.StringVar(value="No iPod")
        self.device_card_sub_var = tk.StringVar(value="Plug one in to get started")
        ttk.Label(names, textvariable=self.device_card_name_var, style="DarkCardTitle.TLabel").pack(anchor="w")
        sub_label = ttk.Label(names, textvariable=self.device_card_sub_var, style="DarkCardSecondary.TLabel")
        sub_label.pack(anchor="w")
        self._device_card_sub_labels = [sub_label]
        self.device_card_eject = ttk.Label(top, text="\u23CF", style="DarkCardEject.TLabel", cursor="hand2")
        self.device_card_eject.bind("<Button-1>", lambda e: self.eject_ipod())
        # Minimize / expand, offered only while no iPod is connected.
        self.device_card_toggle = ttk.Label(top, text="\u25BE", style="DarkCardEject.TLabel", cursor="hand2")
        self.device_card_toggle.bind("<Button-1>", lambda e: self._toggle_device_card())
        self.device_card_top = top
        self.device_card_names = names
        self._device_icon_small = apple_style.ipod_icon_photo(height=26, silver=True)
        self.device_card_icon = top.pack_slaves()[0] if self._device_icon_photo is not None else None

        self.device_card_details = ttk.Frame(card, style="DarkCardInner.TFrame")
        storage_row = ttk.Frame(self.device_card_details, style="DarkCardInner.TFrame")
        storage_row.pack(fill=tk.X, pady=(12, 5))
        ttk.Label(storage_row, text="Capacity", style="DarkCardSecondary.TLabel").pack(side=tk.LEFT)
        self.device_card_storage_var = tk.StringVar(value="")
        ttk.Label(storage_row, textvariable=self.device_card_storage_var,
                  style="DarkCardSmall.TLabel").pack(side=tk.RIGHT)
        self.device_card_bar = ttk.Label(self.device_card_details, style="DarkCard.TLabel")
        self.device_card_bar.pack(anchor="w")
        legend = ttk.Frame(self.device_card_details, style="DarkCardInner.TFrame")
        legend.pack(fill=tk.X, pady=(6, 0))
        self.device_card_legend_vars = {}
        for i, (key, label) in enumerate(apple_style.CAPACITY_KINDS):
            cell = ttk.Frame(legend, style="DarkCardInner.TFrame")
            cell.grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 10))
            ttk.Label(cell, text="\u25CF", style=f"Legend{key}.TLabel").pack(side=tk.LEFT)
            var = tk.StringVar(value=label)
            ttk.Label(cell, textvariable=var, style="DarkCardSmall.TLabel").pack(side=tk.LEFT, padx=(3, 0))
            self.device_card_legend_vars[key] = (label, var)

        self.device_card_link = ttk.Label(card, text="Detect iPod\u2026", style="DarkCardLink.TLabel",
                                          cursor="hand2")
        self.device_card_link.pack(anchor="w", pady=(12, 0))
        self.device_card_link.bind("<Button-1>", lambda e: self._device_card_link_clicked())

        # Settings lives at the bottom of the sidebar.
        self.sidebar_settings_button = ttk.Button(
            self, text="\u2699  Settings", style="SidebarLink.TButton", command=self.open_settings
        )

    def _toggle_device_card(self):
        self.app_settings["device_card_collapsed"] = not self.app_settings.get("device_card_collapsed", False)
        settings.save(self.app_settings)
        self._update_device_card()

    def _set_device_card_collapsed(self, collapsed):
        """Collapsed: a slim bar with the iPod name and an expand arrow.
        Only while no iPod is connected - a connected one always shows
        its full card."""
        if getattr(self, "_device_card_is_collapsed", None) == collapsed:
            return
        self._device_card_is_collapsed = collapsed
        if collapsed:
            for w in self._device_card_sub_labels:
                w.pack_forget()
            self.device_card_link.pack_forget()
            self.sync_button.pack_forget()
            self.device_card.configure(padding=(12, 8, 10, 8))
            if self.device_card_icon is not None and self._device_icon_small is not None:
                self.device_card_icon.configure(image=self._device_icon_small)
        else:
            for w in self._device_card_sub_labels:
                w.pack(anchor="w")
            self.device_card_link.pack(anchor="w", pady=(12, 0))
            self.sync_button.pack(in_=self.device_card, side=tk.BOTTOM, fill=tk.X, pady=(12, 0), ipady=3)
            self.device_card.configure(padding=(12, 12, 10, 12))
            if self.device_card_icon is not None:
                self.device_card_icon.configure(image=self._device_icon_photo)

    def _device_card_link_clicked(self):
        if self.ipod_mountpoint:
            self.show_ipod_info()
        else:
            self.auto_detect_ipod()

    def _build_topbar(self):
        """Apple-style layout only: a bar across the top of the window
        with tabs for the libraries (Music / Movies / TV Shows / Photos /
        Podcasts, replacing the sidebar's dropdown), search, and a
        Settings button."""
        bar = ttk.Frame(self, style="Chrome.TFrame", padding=(12, 10, 12, 6))
        self.topbar = bar
        ttk.Label(bar, text="\u266B  Linuxpod", style="Brand.TLabel").pack(side=tk.LEFT)

        right = ttk.Frame(bar, style="Chrome.TFrame")
        right.pack(side=tk.RIGHT)
        ttk.Button(right, text="\u2699", style="Icon.TButton", width=3,
                   command=self.open_settings).pack(side=tk.RIGHT, padx=(8, 0))
        self.topbar_search = ttk.Entry(right, textvariable=self.search_var, width=24)
        self.topbar_search.pack(side=tk.RIGHT)
        ttk.Label(right, text="\U0001F50D", style="Chrome.TLabel").pack(side=tk.RIGHT, padx=(0, 6))

        # Segmented control, centered in the space between brand and search.
        track = ttk.Frame(bar, style="SegTrack.TFrame", padding=(3, 3, 3, 3))
        track.pack(side=tk.TOP)
        self._segment_buttons = {}
        for mode, text in (("music", "Music"), ("movies", "Movies"), ("tvshows", "TV Shows"),
                           ("photos", "Photos"), ("podcasts", "Podcasts")):
            btn = ttk.Button(track, text=text, style="Segment.TButton",
                             command=lambda m=mode: self._segment_clicked(m))
            btn.pack(side=tk.LEFT)
            self._segment_buttons[mode] = btn

    def _segment_clicked(self, mode):
        if self.current_view == self.VIEW_SETTINGS or mode != self.library_mode:
            self.set_library_mode(mode)

    def _update_segments(self):
        for mode, btn in getattr(self, "_segment_buttons", {}).items():
            on = mode == self.library_mode and self.current_view != self.VIEW_SETTINGS
            btn.configure(style="SegmentOn.TButton" if on else "Segment.TButton")

    def _layout_device_card(self, apple):
        if not hasattr(self, "device_card"):
            self._build_device_card()
            self._build_topbar()
        for w in (self.device_card, self.sync_button, self.sidebar_settings_button, self.topbar):
            if w is not self.sync_button or apple:
                w.pack_forget()
        self._update_sidebar_mode_switcher()
        if not apple:
            return
        self.topbar.pack(side=tk.TOP, fill=tk.X, before=self.body_frame)
        self.topbar.lift()
        self._update_segments()
        # The iPod panel sits at the bottom of the sidebar, with the
        # Sync button inside it.
        self.device_card.pack(in_=self.sidebar_frame, side=tk.BOTTOM, fill=tk.X, pady=(8, 0),
                              before=self.sidebar)
        self.sync_button.pack(in_=self.device_card, side=tk.BOTTOM, fill=tk.X, pady=(12, 0), ipady=3)
        self.sync_button.configure(style="CardAccent.TButton")
        self.sync_all_button.configure(style="CardAccent.TButton")
        self.cancel_sync_button.configure(style="CardPlain.TButton")
        for w in (self.device_card, self.sync_button):
            w.lift()
        self._device_card_is_collapsed = None  # sync button was just re-packed
        self._update_device_card()

    def _update_device_card(self):
        if not hasattr(self, "device_card") or not self._smooth_motion():
            return
        if not self.ipod_mountpoint:
            self.device_card_name_var.set("No iPod connected")
            self.device_card_sub_var.set("Plug one in to get started")
            self.device_card_details.pack_forget()
            self.device_card_eject.pack_forget()
            self.device_card_link.configure(text="Detect iPod\u2026")
            collapsed = bool(self.app_settings.get("device_card_collapsed", False))
            self._set_device_card_collapsed(collapsed)
            self.device_card_toggle.configure(text="\u25B4" if collapsed else "\u25BE")
            if not self.device_card_toggle.winfo_ismapped():
                self.device_card_toggle.pack(side=tk.RIGHT, anchor="center" if collapsed else "n", padx=(4, 0))
            else:
                self.device_card_toggle.pack_configure(anchor="center" if collapsed else "n")
            return
        self.device_card_toggle.pack_forget()
        self._set_device_card_collapsed(False)
        self.device_card_name_var.set(self.ipod_label or "iPod")
        tracks = self._device_music_tracks()
        hours = sum(t.get("duration_ms") or 0 for t in tracks) / 3_600_000
        n = len(tracks)
        self.device_card_sub_var.set(
            f"{n} track{'s' if n != 1 else ''} \u00B7 {hours:.1f} hours" if hours >= 1
            else f"{n} track{'s' if n != 1 else ''}"
        )
        if not self.device_card_eject.winfo_ismapped():
            # before=names: packed ahead of the name block, so a long
            # iPod name shrinks instead of pushing the button off the card.
            self.device_card_eject.pack(side=tk.RIGHT, anchor="n", padx=(4, 0), before=self.device_card_names)
        try:
            usage = shutil.disk_usage(self.ipod_mountpoint)
        except OSError:
            self.device_card_details.pack_forget()
        else:
            # iTunes-style capacity breakdown. Sizes come from the iPod's
            # own database; whatever's used beyond that (photos, notes,
            # the database itself, other files) counts as Other.
            audio = sum(t.get("size_bytes") or 0 for t in (self.device_tracks or []))
            video = sum((v.get("size_bytes") or 0) for v in
                        list(self.device_movies or []) + list(self.device_tvshows or [])
                        if isinstance(v, dict))
            other = max(usage.used - audio - video, 0)
            sizes = {"audio": audio, "video": video, "other": other, "free": usage.free}
            self.device_card_storage_var.set(f"{format_bytes(usage.used)} of {format_bytes(usage.total)}")
            width = max(self.sidebar_frame.winfo_width() - 36, 150)
            # used + free rather than total, so filesystem-reserved space
            # doesn't leave an unexplained gap at the end of the bar.
            photo = apple_style.capacity_bar_photo(width, sizes, usage.used + usage.free)
            if photo is not None:
                self._device_card_bar_photo = photo
                self.device_card_bar.configure(image=photo)
            for key, (label, var) in self.device_card_legend_vars.items():
                var.set(f"{label} {format_bytes(sizes[key])}")
            if not self.device_card_details.winfo_ismapped():
                self.device_card_details.pack(fill=tk.X, before=self.device_card_link)
        self.device_card_link.configure(text="View iPod\u2026")

    def _update_sync_all_button(self):
        """Shows the toolbar's Sync All button only while the device
        view is open and an iPod is connected — it syncs the whole
        local library, which only makes sense in that context."""
        if self._device_view_active and self.ipod_mountpoint:
            if not self.sync_all_button.winfo_ismapped():
                if self._smooth_motion():
                    # Apple-style layout: stacked above Sync Selected in
                    # the sidebar's device card area.
                    self.sync_all_button.pack(in_=self.device_card, side=tk.BOTTOM, fill=tk.X,
                                              pady=(12, 0), after=self.sync_button)
                else:
                    self.sync_all_button.pack(in_=self.toolbar_bar, side=tk.LEFT, padx=(20, 2),
                                              before=self.sync_button)
                self.sync_all_button.lift()
        else:
            self.sync_all_button.pack_forget()

    def _update_device_view_done_button(self):
        """Shows the persistent, bottom-right-corner Done button (and
        the Sync button just to its left) only while the device view
        is open - pinned rightmost in the status bar (before=self.progress,
        and before=self.device_view_done_button for Sync) so they stay
        in the same corner regardless of window size or which
        device-view screen is showing, rather than being part of just
        one screen's content."""
        if self._device_view_active:
            if not self.device_view_done_button.winfo_ismapped():
                self.device_view_done_button.pack(side=tk.RIGHT, before=self.progress, padx=(0, 8))
            if not self.device_view_sync_button.winfo_ismapped():
                self.device_view_sync_button.pack(
                    side=tk.RIGHT, before=self.device_view_done_button, padx=(0, 8)
                )
            if not self.space_meter_label.winfo_ismapped():
                self.space_meter_label.pack(
                    side=tk.RIGHT, after=self.device_view_done_button, padx=(0, 12)
                )
        else:
            self.device_view_done_button.pack_forget()
            self.device_view_sync_button.pack_forget()
            self.space_meter_label.pack_forget()

    def sync_all_to_ipod(self):
        if not self.ipod_mountpoint:
            messagebox.showwarning("No iPod", "Detect or choose an iPod folder first (iPod menu).")
            return
        rows = self.lib.all_tracks(include_podcasts=True)
        if not rows:
            messagebox.showinfo("Nothing to sync", "Your local library is empty.")
            return
        if not messagebox.askyesno(
            "Sync All",
            f"Sync your entire local library ({len(rows)} track(s)) to "
            f"{self.ipod_label or 'this iPod'}? This can take a while for a large library.",
        ):
            return
        if not self._music_fits_or_warn(sync_all=True):
            return
        self._sync_rows_to_ipod(rows)

    def _auto_sync_if_enabled(self):
        """Called once per connection, right after the device's library
        has been read successfully (see the device_read handler) - the
        duplicate-skip check in _sync_rows_to_ipod needs that library
        loaded first to know what's already there, and this doubles as
        what makes "auto-sync" actually mean "auto-sync anything new"
        rather than blindly re-copying the whole library every time."""
        if not self.app_settings.get("auto_sync", False):
            return
        if not self.ipod_mountpoint:
            return
        if self.app_settings.get("sync_all_music", False):
            rows = self.lib.all_tracks(include_podcasts=True)
        else:
            # Only what's ticked in View Connected Device, same as Sync.
            ids = {int(t) for t in self.lib.all_marked_for_sync("track") if t.isdigit()}
            for key in self.lib.all_marked_for_sync("album"):
                ids.update(r["id"] for r in self.lib.tracks_by_album(*_album_sync_key_decode(key)))
            pids = {int(p) for p in self.lib.all_marked_for_sync("playlist") if p.isdigit()}
            for _name, pl_ids in self._playlist_sync_songs(set(ids), pids):
                ids.update(pl_ids)
            rows = self.lib.tracks_by_ids(list(ids))
        if not rows:
            return
        applog.log(f"AUTO-SYNC: triggered by connecting to {self.ipod_label or self.ipod_mountpoint}")
        self._sync_rows_to_ipod(rows, silent=True)

    def _process_pending_video_removals(self):
        """Called once per connection, right after the device's library
        has been read (same timing as _auto_sync_if_enabled, and for
        the same reason - this needs device_movies/device_tvshows
        loaded first). Actually removes from the iPod any video that
        was deleted from the local library since the last time it
        connected (see remove_selected_videos - the converted file is
        deleted right away, but the iPod copy can't be until it's
        actually connected, so that's deferred to here). Matched by
        title+duration since the on-device dbid isn't known unless the
        iPod happened to be connected at the moment of removal."""
        pending = self.lib.all_pending_video_removals()
        if not pending or not self.ipod_mountpoint:
            return
        on_device = list(self.device_movies) + list(self.device_tvshows)
        dbids = []
        matched_ids = []
        for p in pending:
            for t in on_device:
                if t["title"] == p["title"] and t["duration_ms"] == p["duration_ms"]:
                    dbids.append(t["dbid"])
                    matched_ids.append(p["id"])
                    break
        if not dbids:
            return
        applog.log(
            f"VIDEO LIBRARY: removing {len(dbids)} previously-deleted video(s) "
            f"from {self.ipod_mountpoint} on connect"
        )

        def work():
            try:
                removed, errors, notes = ipod_backend.remove_tracks(self.ipod_mountpoint, dbids)
                self.work_queue.put(("pending_video_removal_done", (removed, errors, matched_ids)))
            except ipod_backend.IpodError as e:
                self.work_queue.put(("error", str(e)))
            except Exception:
                applog.log("SYNC WORKER CRASHED:\n" + traceback.format_exc())
                self.work_queue.put((
                    "error",
                    "Something went wrong removing previously-deleted videos from the "
                    "iPod (see the log file for details).",
                ))

        threading.Thread(target=work, daemon=True).start()

    def toggle_auto_sync(self):
        enabled = self.auto_sync_var.get()
        self.app_settings["auto_sync"] = enabled
        settings.save(self.app_settings)
        applog.log(f"SETTINGS: auto-sync {'enabled' if enabled else 'disabled'}")

    def toggle_sync_all_music(self):
        enabled = self.sync_all_music_var.get()
        self.app_settings["sync_all_music"] = enabled
        settings.save(self.app_settings)
        applog.log(f"SETTINGS: sync-all-music {'enabled' if enabled else 'disabled'}")

    def choose_converted_videos_folder(self):
        folder = native_dialogs.choose_directory(title="Choose a folder for converted videos")
        if not folder:
            return
        self.app_settings["converted_videos_folder"] = folder
        settings.save(self.app_settings)
        self.converted_videos_folder_var.set(folder)
        applog.log(f"SETTINGS: converted videos folder set to '{folder}'")

    def _smooth_motion(self):
        """Eased scrolling/animations are part of the Apple-style look."""
        return bool(self.app_settings.get("itunes_theme", False))

    def _content_frames(self):
        return (self.main_area, self.table_frame, self.grid_frame, self.grid_inner,
                self.settings_frame, *self._settings_category_frames.values(),
                self.artist_list_frame, self.genre_list_frame,
                self.ipod_info_frame, self.ipod_info_inner,
                self.placeholder_frame, self.placeholder_inner,
                self.video_list_frame, self.video_grid_inner,
                self.photo_list_frame, self.photo_grid_inner, self.device_photo_albums_frame,
                self.playlist_list_frame)

    def _apply_theme(self, enabled):
        style = ttk.Style()
        apple_style.set_window_options(self, enabled)
        apple_style.SmoothProgressbar.animate = enabled
        chrome_frames = (self.toolbar_bar, self.body_frame, self.sidebar_frame, self.statusbar_bar)
        chrome_labels = (self.search_label, self.status_label, self.summary_label, self.progress_label,
                         self.space_meter_label)
        accent_buttons = (self.sync_button, self.sync_all_button, self.device_view_sync_button,
                          self.video_sync_button, self.photo_sync_button, self.playlist_sync_button)
        if enabled:
            # ttk's OS-native themes (the default on Linux/Windows/macOS)
            # mostly ignore custom colors, so an actually different look
            # needs a theme that honors them - clam, restyled with
            # rounded image elements by apple_style.
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
            apple_style.apply(self, style)
            content_bg = apple_style.CONTENT
            self.sidebar.configure(style="Sidebar.Treeview")
            self.table.tag_configure("evenrow", background=content_bg)
            self.table.tag_configure("oddrow", background=apple_style.STRIPE)

            # Sidebar, toolbar and status bar sit on the pale gray window
            # chrome; the track table / grids / settings are white panes.
            for f in chrome_frames:
                f.configure(style="Chrome.TFrame")
            for lbl in chrome_labels:
                lbl.configure(style="Chrome.TLabel")
            # The toolbar is the content pane's white header here.
            self.toolbar_bar.configure(style="Content.TFrame")
            self.search_label.configure(style="Secondary.TLabel")
            for btn in accent_buttons:
                btn.configure(style="Accent.TButton")
            self.toolbar_separator.configure(style="Chrome.TSeparator")
            for f in self._content_frames():
                f.configure(style="Content.TFrame")
            self.grid_canvas.configure(bg=content_bg)
            self.ipod_info_canvas.configure(bg=content_bg)
            self.video_grid_canvas.configure(bg=content_bg)
            self.photo_grid_canvas.configure(bg=content_bg)
        else:
            try:
                style.theme_use(self._default_ttk_theme)
            except tk.TclError:
                pass
            self.configure(background=self._default_canvas_bg)
            self.table.tag_configure("evenrow", background="")
            self.table.tag_configure("oddrow", background="")
            self.sidebar.configure(style="Treeview")
            for f in chrome_frames:
                f.configure(style="TFrame")
            for lbl in chrome_labels:
                lbl.configure(style="TLabel")
            for btn in accent_buttons:
                btn.configure(style="TButton")
            self.cancel_sync_button.configure(style="TButton")
            self.toolbar_separator.configure(style="TSeparator")
            for f in self._content_frames():
                f.configure(style="TFrame")
            self.grid_canvas.configure(bg=self._default_canvas_bg)
            self.ipod_info_canvas.configure(bg=self._default_canvas_bg)
            self.video_grid_canvas.configure(bg=self._default_video_canvas_bg)
            self.photo_grid_canvas.configure(bg=self._default_photo_canvas_bg)

        # --- structural changes, not just color ---

        # Toolbar / sidebar: the Apple-style layout follows iOpenPod -
        # a device card with storage bar and a big Sync button at the
        # top of the sidebar, Settings pinned to its bottom, and the
        # toolbar turned into a header over the content pane.
        self._layout_toolbar(enabled)
        self._layout_device_card(enabled)

        # Sidebar: grouped LIBRARY / PLAYLISTS / DEVICES source-list
        # sections instead of a flat list — see refresh_sidebar.
        self.refresh_sidebar()

        # Bottom bar: a persistent "N songs, H:MM, size" library summary
        # (iTunes' classic bottom-of-window readout) instead of that
        # space just sitting empty between the status text and progress.
        self.summary_label.pack_forget()
        if enabled:
            self.summary_label.pack(side=tk.LEFT, expand=True)
            self._update_library_summary()
        # else: leave it unpacked/hidden

    def _update_library_summary(self):
        if not self.app_settings.get("itunes_theme", False):
            return
        try:
            tracks = self.lib.all_tracks()
        except Exception:
            return
        count = len(tracks)
        total_ms = sum((t["duration_ms"] or 0) for t in tracks)
        total_bytes = sum((t["size_bytes"] or 0) for t in tracks)
        total_minutes = total_ms // 60000
        hours, minutes = divmod(int(total_minutes), 60)
        time_str = f"{hours} hr {minutes} min" if hours else f"{minutes} min"
        self.summary_var.set(
            f"{count} song{'s' if count != 1 else ''}, {time_str}, {format_bytes(total_bytes)}"
        )

    def _show_body(self, mode):
        """mode: 'table', 'grid', 'settings', 'artists', 'genres', 'ipod_info', 'placeholder', 'videos', 'photos', or 'device_photo_albums'"""
        self._body_mode = mode
        self._schedule_header_subtitle()
        self.table_frame.pack_forget()
        self.grid_frame.pack_forget()
        self.settings_frame.pack_forget()
        self.artist_list_frame.pack_forget()
        self.genre_list_frame.pack_forget()
        self.ipod_info_frame.pack_forget()
        self.placeholder_frame.pack_forget()
        self.video_list_frame.pack_forget()
        self.photo_list_frame.pack_forget()
        self.device_photo_albums_frame.pack_forget()
        self.playlist_list_frame.pack_forget()

        if self._device_view_active:
            self.device_view_banner.configure(text=f"\U0001F4F1 Managing {self.ipod_label or 'iPod'}")
            if not self.device_view_banner.winfo_ismapped():
                self.device_view_banner.pack(side=tk.TOP, fill=tk.X)
        else:
            self.device_view_banner.pack_forget()

        if mode == "grid":
            self.grid_frame.pack(fill=tk.BOTH, expand=True)
        elif mode == "settings":
            self.settings_frame.pack(fill=tk.BOTH, expand=True)
        elif mode == "artists":
            self.artist_list_frame.pack(fill=tk.BOTH, expand=True)
        elif mode == "genres":
            self.genre_list_frame.pack(fill=tk.BOTH, expand=True)
        elif mode == "ipod_info":
            self.ipod_info_frame.pack(fill=tk.BOTH, expand=True)
        elif mode == "placeholder":
            self.placeholder_frame.pack(fill=tk.BOTH, expand=True)
        elif mode == "videos":
            self.video_list_frame.pack(fill=tk.BOTH, expand=True)
        elif mode == "photos":
            self.photo_list_frame.pack(fill=tk.BOTH, expand=True)
        elif mode == "device_photo_albums":
            self.device_photo_albums_frame.pack(fill=tk.BOTH, expand=True)
        elif mode == "playlist_list":
            self.playlist_list_frame.pack(fill=tk.BOTH, expand=True)
        else:
            self.table_frame.pack(fill=tk.BOTH, expand=True)

    def _build_statusbar(self):
        bar = ttk.Frame(self)
        # Packed ahead of the body so a tall page (e.g. the device view)
        # can never squeeze the status bar, and its Done button, off-screen.
        bar.pack(side=tk.BOTTOM, fill=tk.X, padx=6, pady=4, before=self.body_frame)
        self.statusbar_bar = bar

        self.status_var = tk.StringVar(value="No iPod detected.")
        self.status_label = ttk.Label(bar, textvariable=self.status_var)
        self.status_label.pack(side=tk.LEFT)

        # iTunes' bottom bar shows a running "N songs, H:MM, size" summary
        # of the current library rather than a transient status message.
        # Only shown in the iTunes-style layout (see _apply_theme /
        # _update_library_summary); built here so it exists to pack later.
        self.summary_var = tk.StringVar(value="")
        self.summary_label = ttk.Label(bar, textvariable=self.summary_var, anchor="center")

        self.progress = apple_style.SmoothProgressbar(bar, orient="horizontal", mode="determinate", length=220)
        self.progress.pack(side=tk.RIGHT)
        self.progress_label_var = tk.StringVar(value="")
        self.progress_label = ttk.Label(bar, textvariable=self.progress_label_var)
        self.progress_label.pack(side=tk.RIGHT, padx=8)

        # Not packed here - only shown while the device view is open;
        # see _update_device_view_done_button(), called from
        # refresh_sidebar() alongside the other device-view toggles.
        # Packed with before=self.progress so it's always the
        # rightmost thing in the bottom bar when shown - pinned to the
        # bottom-right corner regardless of window size or which
        # device-view screen (Summary, Songs, Albums, ...) is current,
        # rather than living inside just one screen's own content.
        self.device_view_done_button = ttk.Button(bar, text="Done", command=self.close_device_view)
        # Same idea, packed with before=self.device_view_done_button so
        # it sits directly to Done's left - one consistent place to
        # sync whatever's currently selected (Songs/Albums, Movies, TV
        # Shows, or Photos - see _device_view_sync_selected), instead
        # of having to find that screen's own Sync Selected button.
        self.device_view_sync_button = ttk.Button(
            bar, text="Sync", command=self._device_view_sync_selected
        )
        # Live "how much will fit" readout for the ticked songs, shown
        # left of Sync while the device view is open (see _space_meter_tick).
        self.space_meter_var = tk.StringVar(value="")
        self.space_meter_label = ttk.Label(bar, textvariable=self.space_meter_var)
        self._space_meter_sig = None
        self._space_meter_checked = 0.0
        self._space_plan = None

    # Room left on the iPod for the songs it will get. Kept for the
    # iPod's database and artwork, which grow with each sync.
    SPACE_HEADROOM = 50 * 1024 * 1024

    def _music_space_plan(self, sync_all=None, removals=True):
        """What a Sync would do to the iPod's free space, worked out the
        same way the sync itself decides: songs already on the iPod are
        skipped, songs no longer ticked come off (only exact, linked
        copies; see _compute_extra_music_on_device). Returns a dict, or
        None when no iPod is connected."""
        if not self.ipod_mountpoint:
            return None
        try:
            usage = shutil.disk_usage(self.ipod_mountpoint)
        except OSError:
            return None
        if sync_all is None:
            sync_all = self.app_settings.get("sync_all_music", False)
        if sync_all:
            rows = self.lib.all_tracks(include_podcasts=True)
            extra = []
        else:
            wanted = self._compute_wanted_music_track_ids()
            rows = self.lib.tracks_by_ids(list(wanted)) if wanted else []
            extra = self._compute_extra_music_on_device() if removals else []
        linked_ids = set(self.device_track_links.values())
        unlinked_keys = {
            _track_dup_key(t.get("title"), t.get("artist"), t.get("album"), t.get("duration_ms"))
            for t in self.device_tracks if str(t["dbid"]) not in self.device_track_links
        }
        add_bytes = add_count = 0
        for r in rows:
            if f".{(r['filetype'] or '').lower()}" not in scanner.IPOD_PLAYABLE_EXTENSIONS:
                continue
            if r["id"] in linked_ids or _track_dup_key(
                r["title"], r["artist"], r["album"], r["duration_ms"]
            ) in unlinked_keys:
                continue
            size = r["size_bytes"]
            if not size:
                try:
                    size = os.path.getsize(r["filepath"])
                except (OSError, TypeError):
                    size = 0
            add_bytes += size
            add_count += 1
        remove_bytes = sum(t.get("size_bytes") or 0 for t in extra)
        free_after = usage.free + remove_bytes - add_bytes
        return {
            "add_bytes": add_bytes, "add_count": add_count,
            "remove_bytes": remove_bytes, "remove_count": len(extra),
            "free": usage.free, "total": usage.total, "free_after": free_after,
            "fits": free_after >= self.SPACE_HEADROOM,
        }

    def _space_meter_tick(self):
        """Runs twice a second. Recomputes the space readout whenever the
        ticked songs, albums or playlists (or the iPod's contents)
        change, and every few seconds anyway to catch the free space
        changing, so the number follows each tick as it happens."""
        try:
            if self._device_view_active and self.ipod_mountpoint:
                sig = (
                    frozenset(self._persistent_table_selection),
                    frozenset(self._selected_albums),
                    frozenset(self._selected_playlist_ids),
                    len(self.device_tracks or []),
                    len(self.device_track_links or {}),
                    bool(self.app_settings.get("sync_all_music", False)),
                    self.ipod_mountpoint,
                )
                now = time.monotonic()
                if sig != self._space_meter_sig or now - self._space_meter_checked > 5:
                    self._space_meter_sig = sig
                    self._space_meter_checked = now
                    self._show_space_plan(self._music_space_plan())
            elif self._space_meter_sig is not None:
                self._space_meter_sig = None
                self._space_plan = None
                self.space_meter_var.set("")
        except Exception:
            applog.log("SPACE METER FAILED:\n" + traceback.format_exc())
        self.after(500, self._space_meter_tick)

    def _show_space_plan(self, plan):
        self._space_plan = plan
        if plan is None:
            self.space_meter_var.set("")
            return
        parts = []
        if plan["add_count"]:
            parts.append(f"Adding {plan['add_count']} song{'s' if plan['add_count'] != 1 else ''} "
                         f"({format_bytes(plan['add_bytes'])})")
        if plan["remove_count"]:
            parts.append(f"removing {plan['remove_count']} ({format_bytes(plan['remove_bytes'])})")
        if not parts:
            parts.append("Nothing new to add")
        if plan["fits"]:
            tail = f"{format_bytes(plan['free_after'])} free after sync"
            color = ""
        else:
            short = self.SPACE_HEADROOM - plan["free_after"]
            tail = f"Not enough space: {format_bytes(short)} over"
            color = "#d0342c"
        text = " \u00B7 ".join(parts) + " \u00B7 " + tail
        self.space_meter_var.set(text[0].upper() + text[1:])
        self.space_meter_label.configure(foreground=color)

    def _device_view_sync_selected(self):
        """The persistent Sync button next to Done in the device view.
        Context-aware: syncs whatever's currently selected in whichever
        local-content screen is showing.

        For music and video, this reconciles rather than just adding:
        anything already on the device that no longer matches your
        durable sync profile (deleted from your library, or simply
        unmarked/deselected) is removed first - after you confirm the
        exact list - then whatever's currently marked is added, same as
        before. "Always sync all music" (Sync options) skips
        reconciliation for music, since "everything" has nothing to
        remove by definition.

        Photos stay purely additive for now: this app has never tracked
        individual photos already on a device (only album counts), so
        there's no reliable way yet to tell which specific on-device
        photo corresponds to which local one - deleting the wrong photo
        by mistake is worse than not deleting stale ones automatically."""
        if self.current_view in (self.VIEW_MOVIES, self.VIEW_TVSHOWS):
            if self._video_grid_source == "device":
                messagebox.showinfo(
                    "Already On This iPod",
                    "This is the on-device list. Browse Movies/TV Shows under Music "
                    "in the sidebar above instead, to select new ones to sync.",
                )
                return
            self._reconcile_and_sync_videos()
        elif self.current_view == self.VIEW_PHOTOS:
            self.sync_selected_photos()
        elif self.app_settings.get("sync_all_music", False):
            self.sync_all_to_ipod()
        else:
            # Any other screen (Summary, Songs, Albums, Playlists, ...):
            # sync every song ticked anywhere, not just this screen's.
            self._reconcile_and_sync_music()

    def _remove_dbids_then(self, dbids, callback, label="RECONCILE"):
        """Removes the given on-device track/video dbids, then - only
        once that removal has actually finished (see the "remove_done"
        handler) - calls `callback` to run the add step. Chaining it
        this way, instead of firing both right away, keeps the two
        device-writing helper subprocesses from ever running at the
        same time against the same iTunesDB."""
        applog.log(f"{label}: requested for {len(dbids)} track(s) on {self.ipod_mountpoint}")
        self._post_remove_callback = callback

        def work():
            try:
                removed, errors, notes = ipod_backend.remove_tracks(self.ipod_mountpoint, dbids)
                self.work_queue.put(("remove_done", (removed, errors, notes, label, dbids)))
            except ipod_backend.IpodError as e:
                self.work_queue.put(("error", str(e)))
            except Exception:
                applog.log("SYNC WORKER CRASHED:\n" + traceback.format_exc())
                self.work_queue.put((
                    "error",
                    "Something went wrong during this operation (see the log file "
                    "for details). It's been logged so it can be tracked down.",
                ))

        threading.Thread(target=work, daemon=True).start()

    def _own_ticked_track_ids(self):
        """Songs ticked directly (Songs tab) or through a ticked album."""
        own = {int(i.split(":", 1)[1]) for i in self._persistent_table_selection if i.startswith("t:")}
        for album, artist in self._selected_albums:
            own.update(r["id"] for r in self.lib.tracks_by_album(album, artist))
        return own

    def _playlist_sync_songs(self, own, playlist_ids):
        """[(playlist name, [track ids])] for each ticked playlist.
        A ticked playlist only brings the songs of it that are also
        ticked (as songs or albums); if none of its songs are ticked,
        it brings all of them."""
        names = {p["id"]: p["name"] for p in self.lib.all_playlists()}
        result = []
        for pid in sorted(playlist_ids):
            if pid not in names:
                continue
            ids = [r["id"] for r in self.lib.playlist_tracks(pid)]
            chosen = [i for i in ids if i in own]
            result.append((names[pid], chosen or ids))
        return result

    def _compute_wanted_music_track_ids(self):
        """Every local track id currently marked to be on this iPod:
        ticked songs, songs of ticked albums, and what each ticked
        playlist brings (see _playlist_sync_songs)."""
        own = self._own_ticked_track_ids()
        ids = set(own)
        for _name, pl_ids in self._playlist_sync_songs(own, self._selected_playlist_ids):
            ids.update(pl_ids)
        return ids

    def _device_tracks_for_local_ids(self, track_ids):
        """iPod tracks known to be copies of these library songs."""
        track_ids = set(track_ids)
        links = self.device_track_links
        return [t for t in self.device_tracks if links.get(str(t["dbid"])) in track_ids]

    def _compute_extra_music_on_device(self):
        """iPod tracks that are copies of library songs no longer ticked.
        Tracks that can't be tied to one library song for certain (see
        Library.link_device_tracks) are left alone."""
        wanted_ids = self._compute_wanted_music_track_ids()
        links = self.device_track_links
        return [
            t for t in self.device_tracks
            if links.get(str(t["dbid"])) is not None and links[str(t["dbid"])] not in wanted_ids
        ]

    def _music_fits_or_warn(self, sync_all=None, removals=True):
        """False (after telling the user) when the ticked songs won't fit."""
        plan = self._music_space_plan(sync_all=sync_all, removals=removals)
        if plan is None or plan["fits"]:
            return True
        short = self.SPACE_HEADROOM - plan["free_after"]
        messagebox.showwarning(
            "Not Enough Space",
            f"The ticked songs need {format_bytes(plan['add_bytes'])}, but "
            f"{self.ipod_label or 'the iPod'} only has room for "
            f"{format_bytes(max(plan['free'] + plan['remove_bytes'] - self.SPACE_HEADROOM, 0))}. "
            f"Untick about {format_bytes(short)} of music and press Sync again.\n\n"
            "Nothing was changed on the iPod.",
        )
        return False

    def _reconcile_and_sync_music(self):
        if not self._music_fits_or_warn():
            return
        self._unticked_track_ids = set()
        extra = self._compute_extra_music_on_device()
        if extra and not self._confirm_reconcile_removal(extra, "track"):
            extra = []
            # Those songs stay, so check again without their space back.
            if not self._music_fits_or_warn(removals=False):
                return
        wanted = self._compute_wanted_music_track_ids()
        if not wanted and not extra:
            messagebox.showinfo(
                "Nothing to Sync",
                "Nothing is ticked yet. Tick songs, albums, or playlists under Music in "
                "the sidebar, then press Sync.",
            )
            return

        def continue_sync():
            rows = self.lib.tracks_by_ids(list(wanted)) if wanted else []
            playlists = self._playlist_sync_songs(self._own_ticked_track_ids(), self._selected_playlist_ids)
            self._sync_rows_to_ipod(rows, playlists=playlists)

        if extra:
            self._remove_dbids_then([t["dbid"] for t in extra], continue_sync, label="RECONCILE MUSIC")
        else:
            continue_sync()

    def _compute_wanted_video_ids(self):
        """Real local video ids (movies and TV episodes alike) durably
        marked to be on this iPod - see _persist_video_marks."""
        return {int(v) for v in self.lib.all_marked_for_sync("video") if v.lstrip("-").isdigit()}

    def _compute_extra_video_on_device(self):
        wanted_ids = self._compute_wanted_video_ids()
        wanted_rows = self.lib.videos_by_ids(list(wanted_ids)) if wanted_ids else []
        wanted_keys = {_video_dup_key(r["title"], r["duration_ms"]) for r in wanted_rows}
        on_device = list(self.device_movies) + list(self.device_tvshows)
        return [t for t in on_device if _video_dup_key(t["title"], t["duration_ms"]) not in wanted_keys]

    def _reconcile_and_sync_videos(self):
        extra = self._compute_extra_video_on_device()
        if extra and not self._confirm_reconcile_removal(extra, "video"):
            extra = []
        if extra:
            self._remove_dbids_then(
                [t["dbid"] for t in extra], self.sync_selected_videos, label="RECONCILE VIDEO"
            )
        else:
            self.sync_selected_videos()

    def _confirm_reconcile_removal(self, extra_rows, noun):
        names = "\n".join(f"• {t['title'] or '(untitled)'}" for t in extra_rows[:15])
        if len(extra_rows) > 15:
            names += f"\n…and {len(extra_rows) - 15} more"
        return messagebox.askyesno(
            "Remove From iPod?",
            f"{len(extra_rows)} {noun}(s) on the iPod are no longer part of your synced "
            f"selection (deleted from your library, or just unmarked) and will be removed "
            f"to keep the iPod in sync:\n\n{names}\n\nRemove them now?",
        )

    # -------------------------------------------------------- sidebar --

    def refresh_sidebar(self):
        self.sidebar.delete(*self.sidebar.get_children())
        itunes = self.app_settings.get("itunes_theme", False)
        self._update_ipod_info_button()
        self._update_sync_all_button()
        self._update_device_card()
        self._update_device_view_done_button()

        if self.current_view == self.VIEW_SETTINGS:
            self._refresh_settings_sidebar()
            return

        if self._device_view_active:
            self._refresh_device_sidebar()
            return

        if self.library_mode in ("movies", "tvshows"):
            self._refresh_video_sidebar()
            return

        if self.library_mode == "photos":
            self._refresh_photo_sidebar()
            return

        if self.library_mode == "podcasts":
            self._refresh_podcasts_sidebar()
            return

        if itunes:
            self.sidebar.tag_configure(
                "header", foreground="#8a8a8a", font=(apple_style.FAMILY, 8, "bold")
            )

            lib_hdr = self.sidebar.insert(
                "", "end", iid="hdr_library", text="LIBRARY", tags=("header",), open=True
            )
            self.sidebar.insert(lib_hdr, "end", iid="recently_added", text="\u23F1 Recently Added")
            self.sidebar.insert(lib_hdr, "end", iid="library", text="\u266A Songs")
            self.sidebar.insert(lib_hdr, "end", iid="albums", text="\u25A6 Albums")
            self.sidebar.insert(lib_hdr, "end", iid="artists", text="\U0001F3A4 Artists")
            self.sidebar.insert(lib_hdr, "end", iid="genres", text="\U0001F3B8 Genres")

            pl_hdr = self.sidebar.insert(
                "", "end", iid="hdr_playlists", text="PLAYLISTS", tags=("header",), open=True
            )
            for pl in self.lib.all_playlists():
                self.sidebar.insert(pl_hdr, "end", iid=f"lp:{pl['id']}", text=f"\u266B {pl['name']}")

            if self.ipod_mountpoint:
                dev_hdr = self.sidebar.insert(
                    "", "end", iid="hdr_devices", text="DEVICES", tags=("header",), open=True
                )
                dev_label = f"\U0001F4F1 {self.ipod_label or 'iPod'}"
                dev_root = self.sidebar.insert(dev_hdr, "end", iid="device", text=dev_label, open=True)
                self.sidebar.insert(dev_root, "end", iid="device_all", text="On iPod")
                for pl in self.device_playlists:
                    if pl.get("is_master"):
                        continue
                    self.sidebar.insert(dev_root, "end", iid=f"dp:{pl['name']}", text=pl["name"])

            return

        self.sidebar.insert("", "end", iid="recently_added", text="\u23F1 Recently Added")
        self.sidebar.insert("", "end", iid="albums", text="\U0001F5BC Albums")
        self.sidebar.insert("", "end", iid="library", text="\U0001F3B5 Library")
        self.sidebar.insert("", "end", iid="artists", text="\U0001F3A4 Artists")
        self.sidebar.insert("", "end", iid="genres", text="\U0001F3B8 Genres")

        pl_root = self.sidebar.insert("", "end", iid="playlists", text="Playlists", open=True)
        for pl in self.lib.all_playlists():
            self.sidebar.insert(pl_root, "end", iid=f"lp:{pl['id']}", text=pl["name"])

        if self.ipod_mountpoint:
            dev_label = f"\U0001F4F1 {self.ipod_label or 'iPod'}"
            dev_root = self.sidebar.insert("", "end", iid="device", text=dev_label, open=True)
            self.sidebar.insert(dev_root, "end", iid="device_all", text="On iPod")
            for pl in self.device_playlists:
                if pl.get("is_master"):
                    continue
                self.sidebar.insert(dev_root, "end", iid=f"dp:{pl['name']}", text=pl["name"])

    def _refresh_settings_sidebar(self):
        """Settings gets its own simple list of category tabs here in
        place of the normal library tree - see _show_settings_category
        (dispatched from on_sidebar_select) and _build_settings_panel
        for where each category's content actually lives."""
        for key, label in (
            ("general", "⚙ General"),
            ("video", "\U0001F3AC Video Folders"),
            ("podcasts", "\U0001F3A7 Podcasts"),
            ("spoofify", "\U0001F525 Spoofify"),
            ("appearance", "\U0001F3A8 Appearance"),
        ):
            self.sidebar.insert("", "end", iid=f"settings_{key}", text=label)
        current = getattr(self, "_current_settings_category", "general")
        if self.sidebar.exists(f"settings_{current}"):
            self.sidebar.selection_set(f"settings_{current}")

    def _refresh_video_sidebar(self):
        """The sidebar for Movies/TV Shows mode - deliberately simple
        for now (a single "All X" entry, plus "On This iPod" when a
        device is connected), since there's no album/genre/playlist
        equivalent browsing built for video yet. Add Video Folder and
        the video list itself (with Show/Season/Episode columns for TV)
        do the real work; this just gives the sidebar something to
        show and a way back to the content if it's ever navigated away
        from within this mode."""
        kind_label = "Movies" if self.library_mode == "movies" else "TV Shows"
        self.sidebar.insert("", "end", iid="all_videos", text=f"\U0001F4C1 All {kind_label}")
        if self.ipod_mountpoint:
            self.sidebar.insert("", "end", iid="ondevice_videos", text=f"\U0001F4F1 On This iPod")
        self.sidebar.selection_set("all_videos")

    def _refresh_photo_sidebar(self):
        """Same rationale as _refresh_video_sidebar - deliberately
        simple for now."""
        self.sidebar.insert("", "end", iid="all_photos", text="\U0001F4C1 All Photos")
        if self.ipod_mountpoint:
            self.sidebar.insert("", "end", iid="ondevice_photos", text="\U0001F4F1 On This iPod")
        self.sidebar.selection_set("all_photos")

    def _refresh_podcasts_sidebar(self):
        """Same rationale as _refresh_video_sidebar/_refresh_photo_sidebar
        - deliberately simple: a grid of subscribed shows is the whole
        point of this mode, so there's no album/genre-equivalent
        browsing to build here."""
        self.sidebar.insert("", "end", iid="all_podcasts", text="\U0001F4C1 All Podcasts")
        if self.ipod_mountpoint:
            self.sidebar.insert("", "end", iid="ondevice_podcasts", text="\U0001F4F1 On This iPod")
        self.sidebar.selection_set("all_podcasts")

    def _refresh_device_sidebar(self):
        """The sidebar shown while the device view (View Connected
        Device) is open — modeled on real iTunes' device management
        source list: a SETTINGS section (Summary, then Music with its
        own Songs/Albums/Genres/Playlists underneath, then placeholder
        Movies/TV Shows/Photos entries), and an ON MY DEVICE section
        mirroring what's actually on the iPod. Selection isn't forced
        here on every rebuild (e.g. after a sync re-reads the device
        library) — only show_ipod_info() does that, on first entering
        this mode - so browsing around doesn't keep getting yanked back
        to Summary."""
        self.sidebar.tag_configure(
            "header", foreground="#8a8a8a", font=(apple_style.FAMILY, 8, "bold")
        )

        settings_hdr = self.sidebar.insert(
            "", "end", iid="hdr_dv_settings", text="SETTINGS", tags=("header",), open=True
        )
        self.sidebar.insert(settings_hdr, "end", iid="dv_summary", text="Summary")
        music_node = self.sidebar.insert(settings_hdr, "end", iid="dv_music", text="Music", open=True)
        self.sidebar.insert(music_node, "end", iid="dv_music_songs", text="Songs")
        self.sidebar.insert(music_node, "end", iid="dv_music_albums", text="Albums")
        self.sidebar.insert(music_node, "end", iid="dv_music_artists", text="Artists")
        self.sidebar.insert(music_node, "end", iid="dv_music_genres", text="Genres")
        pl_node = self.sidebar.insert(music_node, "end", iid="dv_music_playlists", text="Playlists", open=True)
        for pl in self.lib.all_playlists():
            self.sidebar.insert(pl_node, "end", iid=f"dv_pl:{pl['id']}", text=pl["name"])
        self.sidebar.insert(settings_hdr, "end", iid="dv_movies", text="Movies")
        self.sidebar.insert(settings_hdr, "end", iid="dv_tv", text="TV Shows")
        self.sidebar.insert(settings_hdr, "end", iid="dv_photos", text="Photos")
        self.sidebar.insert(settings_hdr, "end", iid="dv_podcasts", text="Podcasts")

        ondevice_hdr = self.sidebar.insert(
            "", "end", iid="hdr_dv_ondevice", text="ON MY DEVICE", tags=("header",), open=True
        )
        self.sidebar.insert(ondevice_hdr, "end", iid="dv_ondevice_music", text="Music")
        self.sidebar.insert(ondevice_hdr, "end", iid="dv_ondevice_movies", text="Movies")
        self.sidebar.insert(ondevice_hdr, "end", iid="dv_ondevice_tv", text="TV Shows")
        self.sidebar.insert(ondevice_hdr, "end", iid="dv_ondevice_photos", text="Photos")
        self.sidebar.insert(ondevice_hdr, "end", iid="dv_ondevice_podcasts", text="Podcasts")

    def _update_header_title(self, iid=None):
        """Title over the content pane in the Apple-style layout - the
        selected sidebar entry's name, without its leading icon."""
        if self.current_view == self.VIEW_SETTINGS or (iid or "").startswith("settings_"):
            self.header_title_var.set("Settings")
            self._update_segments()
            return
        if iid and self.sidebar.exists(iid):
            text = self.sidebar.item(iid, "text") or ""
            text = text.lstrip()
            if text and not text[0].isalnum():
                text = text[1:].lstrip()
            if text:
                self.header_title_var.set(text)

    def _schedule_header_subtitle(self):
        """The line under the page title ("96 songs", "12 albums"...) -
        computed a moment after a view switch, once it's populated."""
        if not hasattr(self, "header_sub_var"):
            return
        job = getattr(self, "_header_sub_job", None)
        if job is not None:
            self.after_cancel(job)
        self._header_sub_job = self.after(120, self._update_header_subtitle)

    def _update_header_subtitle(self):
        self._header_sub_job = None
        mode = getattr(self, "_body_mode", "table")

        def plural(n, word):
            return f"{n} {word}{'' if n == 1 else 's'}"

        text = ""
        if mode == "table":
            n = len(self.table.get_children())
            word = "song" if self.current_view in self.LOCAL_TRACK_VIEWS else "item"
            text = plural(n, word)
        elif mode == "grid":
            text = plural(len(self._album_rows), "album")
        elif mode == "videos":
            n = len(self._video_tile_frames_by_id)
            text = plural(n, "show" if self.library_mode == "tvshows" and
                          self._tvshow_grid_level == "shows" else "video")
        elif mode == "photos":
            text = plural(len(self._photo_tile_frames_by_id), "photo")
        elif mode == "artists":
            text = plural(len(self.artist_tree.get_children()), "artist")
        elif mode == "genres":
            text = plural(len(self.genre_tree.get_children()), "genre")
        elif mode == "ipod_info" and self.ipod_label:
            text = "Connected"
        self.header_sub_var.set(text)

    def on_sidebar_select(self, _event=None):
        sel = self.sidebar.selection()
        if not sel:
            return
        iid = sel[0]
        self._update_header_title(iid)
        if iid.startswith("settings_"):
            self._show_settings_category(iid[len("settings_"):])
        elif iid == "all_videos":
            if self.library_mode == "movies":
                self.show_movies()
            else:
                self.show_tvshows()
        elif iid == "ondevice_videos":
            if self.library_mode == "movies":
                self.show_movies_on_device()
            else:
                self.show_tvshows_on_device()
        elif iid == "all_photos":
            self.show_photos()
        elif iid == "ondevice_photos":
            self.show_photos_on_device()
        elif iid == "all_podcasts":
            self.show_podcasts()
        elif iid == "ondevice_podcasts":
            self._show_body("table")
            self.show_podcasts_on_device()
        elif iid == "albums":
            self.show_album_grid()
        elif iid == "library":
            self._show_body("table")
            self.show_library()
        elif iid == "artists":
            self.show_artist_list()
        elif iid == "genres":
            self.show_genre_list()
        elif iid == "recently_added":
            self.show_recently_added()
        elif iid.startswith("lp:"):
            self._show_body("table")
            self.show_local_playlist(int(iid.split(":", 1)[1]))
        elif iid == "dv_summary":
            self.show_ipod_info()
        elif iid in ("dv_music", "dv_music_songs"):
            self._show_body("table")
            self.show_library()
        elif iid == "dv_music_albums":
            self.show_album_grid()
        elif iid == "dv_music_artists":
            self.show_artist_list()
        elif iid == "dv_music_genres":
            self.show_genre_list()
        elif iid == "dv_music_playlists":
            self.show_playlist_list()
        elif iid.startswith("dv_pl:"):
            self._show_body("table")
            self.show_local_playlist(int(iid.split(":", 1)[1]))
        elif iid == "dv_movies":
            self.show_movies()
        elif iid == "dv_tv":
            self.show_tvshows()
        elif iid == "dv_photos":
            self.show_photos()
        elif iid == "dv_podcasts":
            self.show_podcasts()
        elif iid == "dv_ondevice_movies":
            self.show_movies_on_device()
        elif iid == "dv_ondevice_tv":
            self.show_tvshows_on_device()
        elif iid == "dv_ondevice_photos":
            self.show_photos_on_device()
        elif iid == "dv_ondevice_podcasts":
            self._show_body("table")
            self.show_podcasts_on_device()
        elif iid == "dv_ondevice_music":
            self._show_body("table")
            self.show_device_all()
        elif iid == "device_all":
            self._show_body("table")
            self.show_device_all()
        elif iid.startswith("dp:"):
            self._show_body("table")
            self.show_device_playlist(iid.split(":", 1)[1])

    def on_sidebar_right_click(self, event):
        iid = self.sidebar.identify_row(event.y)
        if not iid:
            return
        # Both the normal sidebar's "lp:<id>" entries and the device
        # view's "dv_pl:<id>" entries point at the same local playlists
        # - right-clicking either offers the same actions.
        if iid.startswith("lp:") or iid.startswith("dv_pl:"):
            pl_id = int(iid.split(":", 1)[1])
            self.sidebar.selection_set(iid)
            menu = tk.Menu(self, tearoff=0)
            menu.add_command(
                label="Sync Playlist to iPod", command=lambda: self.sync_playlist_to_ipod(pl_id)
            )
            menu.add_separator()
            menu.add_command(label="Delete Playlist…", command=lambda: self._delete_playlist_confirm(pl_id))
            menu.tk_popup(event.x_root, event.y_root)
        # "dp:<name>" entries are playlists that already exist ON the
        # device (see _refresh_device_sidebar/_refresh_settings_sidebar
        # building them from self.device_playlists) - a completely
        # different thing from the local "lp:"/"dv_pl:" ones above, so
        # this removes the on-device playlist itself, not anything in
        # this app's own library.
        elif iid.startswith("dp:"):
            pl_name = iid.split(":", 1)[1]
            self.sidebar.selection_set(iid)
            menu = tk.Menu(self, tearoff=0)
            menu.add_command(
                label="Remove Playlist from iPod…",
                command=lambda: self._remove_device_playlist_confirm(pl_name),
            )
            menu.tk_popup(event.x_root, event.y_root)

    def _remove_device_playlist_confirm(self, pl_name):
        if not self.ipod_mountpoint:
            messagebox.showinfo("No iPod", "No iPod is currently connected.")
            return
        if not messagebox.askyesno(
            "Remove Playlist from iPod",
            f"Remove the playlist '{pl_name}' from the iPod?\n\n"
            "The songs themselves stay on the iPod — only the playlist is removed.",
        ):
            return
        mountpoint = self.ipod_mountpoint
        self.status_var.set(f"Removing playlist '{pl_name}' from iPod…")

        def work():
            success, message = ipod_backend.remove_playlist(mountpoint, pl_name)
            self.work_queue.put(("device_playlist_removed", (success, message, pl_name)))

        threading.Thread(target=work, daemon=True).start()

    def _delete_playlist_confirm(self, pl_id):
        pl = next((p for p in self.lib.all_playlists() if p["id"] == pl_id), None)
        name = pl["name"] if pl else "this playlist"
        if not messagebox.askyesno(
            "Delete Playlist",
            f"Delete the playlist '{name}'?\n\n"
            "The tracks themselves stay in your library — only the playlist is removed.",
        ):
            return
        self.lib.delete_playlist(pl_id)
        applog.log(f"PLAYLIST: deleted '{name}'")
        if self._last_playlist_id == pl_id:
            self._last_playlist_id = None
        if self.current_local_playlist_id == pl_id:
            self.current_local_playlist_id = None
            if self.current_view == self.VIEW_LOCAL_PLAYLIST:
                self._show_body("table")
                self.show_library()
        self.refresh_sidebar()

    def show_settings(self):
        if self.current_view != self.VIEW_SETTINGS:
            # Remembered so the sidebar's Back button (see
            # _update_sidebar_mode_switcher/_exit_settings) returns to
            # whichever library you were actually looking at, rather
            # than always landing back on Music.
            self._pre_settings_library_mode = self.library_mode
        self.current_view = self.VIEW_SETTINGS
        self.header_title_var.set("Settings")
        self._show_body("settings")
        self._update_segments()
        self._show_settings_category(getattr(self, "_current_settings_category", "general"))
        self.refresh_sidebar()
        self._update_sidebar_mode_switcher()
        self.status_var.set("Settings")

    def open_settings(self):
        """Edit menu -> Settings…. Settings now lives only in the Edit
        menu, not the sidebar, so this just switches the view directly."""
        self.show_settings()

    # ----------------------------------------------------------- views --

    def show_library(self):
        self.current_view = self.VIEW_LIBRARY
        self._populate_table_local(self.lib.all_tracks(order_by=library.SONG_ORDER))
        self._update_library_summary()

    def show_local_playlist(self, playlist_id):
        self.current_view = self.VIEW_LOCAL_PLAYLIST
        self.current_local_playlist_id = playlist_id
        self._populate_table_local(self.lib.playlist_tracks(playlist_id))

    def show_device_all(self):
        self.current_view = self.VIEW_DEVICE
        self._populate_table_device(self._device_music_tracks())

    def _device_podcast_test(self):
        """A function telling on-device podcast episodes apart from music:
        synced as podcasts (mediatype 0x04, see ipod_helper.c), or older
        syncs that went over as music but belong to a subscribed show."""
        try:
            show_names = {s["name"] for s in self.lib.all_podcast_shows()}
        except Exception:
            show_names = set()
        return lambda t: bool((t.get("mediatype") or 0) & 0x04) or t.get("album") in show_names

    def _device_music_tracks(self):
        is_podcast = self._device_podcast_test()
        rows = [t for t in self.device_tracks if not is_podcast(t)]
        return sorted(rows, key=lambda t: ((t.get("title") or "").casefold(), (t.get("artist") or "").casefold()))

    def show_device_playlist(self, name):
        self.current_view = self.VIEW_DEVICE_PLAYLIST
        self.current_device_playlist_name = name
        pl = next((p for p in self.device_playlists if p["name"] == name), None)
        dbids = pl["track_dbids"] if pl else set()
        rows = [t for t in self.device_tracks if t["dbid"] in dbids]
        self._populate_table_device(rows)
        self.status_var.set(f"{name}: {len(rows)} track(s)")

    def show_podcasts_on_device(self):
        """The on-device Podcasts tab (VCD's ON MY DEVICE section, and
        the plain sidebar's own 'On This iPod' entry under Podcasts
        mode). The C helper's on-device track listing has no genre
        field (only title/artist/album/duration/size/mediatype - see
        ipod_backend._parse_stream), so a synced episode can't be
        picked out by genre the way a local one can - instead this
        matches by album name against every currently-subscribed
        show's name, which needs no helper changes at all. A show
        that's since been unsubscribed from won't be recognized here
        even if its episodes are still on the iPod - they're still
        right there under Music, just not this filtered view."""
        self.current_view = self.VIEW_DEVICE
        is_podcast = self._device_podcast_test()
        rows = [t for t in self.device_tracks if is_podcast(t)]
        self._populate_table_device(rows)
        self.status_var.set(f"{len(rows)} podcast episode(s) on this iPod")

    def show_podcasts(self):
        """The Podcasts library view - a grid of subscribed shows,
        deliberately reusing the exact same tile-grid machinery as
        show_album_grid (_build_album_tiles_batch/_reflow_album_grid/
        _start_loading_album_art all key off self._album_rows and a
        (album, artist) tuple, not anything album-specific), just
        sourced from all_podcast_albums() instead of all_albums(). A
        show's artwork is picked up the same way any album's cover art
        is - artwork.get_cover_image() reading it straight off disk
        next to the episode files (see podcast.download_show_artwork) -
        so no separate art-loading code is needed here either.
        Double-clicking a show tile opens its episode list exactly like
        double-clicking an album does (show_album), since a podcast
        show IS an album as far as the tracks table is concerned."""
        self.current_view = self.VIEW_PODCASTS
        self._show_body("grid")
        try:
            self._album_rows = self.lib.all_podcast_albums()
        except Exception:
            self._show_error_with_traceback("Could not load podcasts")
            self._album_rows = []

        for child in self.grid_inner.winfo_children():
            child.destroy()
        self._album_tile_widgets = {}
        self._album_tile_frames = []
        self._album_tile_frames_by_key = {}
        self._album_tile_labels = {}
        self._album_tile_key_by_widget = {}
        if not self._device_view_active:
            self._selected_albums = set()

        if self._album_rows:
            self._build_album_tiles_batch(0, len(self._album_rows))
        else:
            self.status_var.set(
                "No podcast episodes downloaded yet — use File → Add Podcast from URL… to subscribe to a show."
            )

    def show_album_grid(self):
        self.current_view = self.VIEW_ALBUM_GRID
        self._show_body("grid")
        try:
            self._album_rows = self.lib.all_albums()
        except Exception:
            self._show_error_with_traceback("Could not load albums")
            self._album_rows = []

        # Clear anything from a previous visit (e.g. after a rescan) before
        # rebuilding — this is the only place tiles are destroyed; resize
        # only repositions them (see _reflow_album_grid).
        for child in self.grid_inner.winfo_children():
            child.destroy()
        self._album_tile_widgets = {}
        self._album_tile_frames = []
        self._album_tile_frames_by_key = {}
        self._album_tile_labels = {}
        self._album_tile_key_by_widget = {}
        # Persisting selection across navigation only makes sense
        # within the device view (see _load_device_view_selections / _clear_device_view_selections,
        # which clears this on entering/leaving it) - the normal
        # library grid keeps its original behavior of starting fresh
        # every visit, same as before this feature existed.
        if not self._device_view_active:
            self._selected_albums = set()

        # This grid isn't virtualized (every visible tile is a real widget +
        # image), so an unbounded library could still exhaust X server
        # resources in steady state even with batched creation. Cap it
        # rather than pretend to support unlimited galleries.
        ALBUM_DISPLAY_CAP = 500
        total_albums = len(self._album_rows)
        if total_albums > ALBUM_DISPLAY_CAP:
            self._album_rows = self._album_rows[:ALBUM_DISPLAY_CAP]

        if self._album_rows:
            self._build_album_tiles_batch(0, total_albums)
        else:
            self.status_var.set("No albums yet — add some music to your library first.")

    ALBUM_BATCH_SIZE = 24

    def _build_album_tiles_batch(self, start_index, total_albums):
        """
        Creates tile widgets a batch at a time (each `after()` call hands
        control back to the event loop in between) instead of creating
        hundreds of image-bearing widgets in one burst — a big burst of
        X_CreatePixmap requests is what caused the X server BadAlloc crash
        this replaced.
        """
        end_index = min(start_index + self.ALBUM_BATCH_SIZE, len(self._album_rows))
        for row in self._album_rows[start_index:end_index]:
            frame = self._make_album_tile(self.grid_inner, row)
            self._album_tile_frames.append(frame)

        # Only place the new tiles; re-gridding the ones already on
        # screen every batch made the grid visibly twitch while loading.
        self._reflow_album_grid(start=start_index)

        if end_index < len(self._album_rows):
            self.status_var.set(f"Loading albums… {end_index}/{len(self._album_rows)}")
            self.after(15, lambda: self._build_album_tiles_batch(end_index, total_albums))
        else:
            if total_albums > len(self._album_rows):
                self.status_var.set(
                    f"Showing the first {len(self._album_rows)} of {total_albums} albums "
                    "(this view doesn't scale to huge libraries yet — try search/playlists to narrow it down)."
                )
            else:
                self._update_album_selection_status()
            self._start_loading_album_art()

    def _start_loading_album_art(self):
        if not HAVE_PIL:
            self.status_var.set(
                "Showing albums without cover art — install the 'Pillow' Python package for real artwork."
            )
            return

        rows = self._album_rows
        art_size = self.ALBUM_ART_SIZE  # read on the main thread; captured for the worker below
        smooth = self._smooth_motion()

        def work():
            for row in rows:
                try:
                    key = (row["album"], row["artist"])
                    img = artwork.get_cover_image(row["sample_filepath"], max_size=art_size)
                    if img is not None:
                        # Always exactly the placeholder's size (non-square
                        # covers - YouTube thumbnails, banners - are center
                        # cropped like Apple Music does), so a cover arriving
                        # never changes its tile's height and shoves every
                        # row below it.
                        if img.size != tuple(art_size):
                            from PIL import Image, ImageOps
                            img = ImageOps.fit(img, tuple(art_size), method=Image.LANCZOS)
                        # The Apple-style rounding, reflection and tint are
                        # done here, off the UI thread - doing them per
                        # cover in _poll_queue stalled the window.
                        tint = None
                        if smooth:
                            tint = apple_style.tint_for(img)
                            img = apple_style.add_reflection(apple_style.round_corners(img, radius=8))
                        self.work_queue.put(("album_thumb_ready", (key, img, tint, smooth)))
                except Exception as e:
                    self.work_queue.put(("error", f"Couldn't load art for {row['album']}: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _reflow_album_grid(self, start=0):
        """Repositions already-created tiles into the current column count.
        Cheap — never destroys or recreates a widget, so it's safe to call
        on every resize event without risking a repeat of the X crash.
        start > 0 places only tiles from that index on (new ones), unless
        the column count changed since the last full layout."""
        try:
            canvas_width = self.grid_canvas.winfo_width()
            if canvas_width <= 1:
                # Not laid out yet (first batch right after switching
                # views): settle the geometry now, or the first tiles go
                # into one column and then all jump into place.
                self.grid_canvas.update_idletasks()
                canvas_width = self.grid_canvas.winfo_width()
                if canvas_width <= 1:
                    canvas_width = max(self.main_area.winfo_width(), 900)
            columns = max(1, canvas_width // self.TILE_WIDTH)
            if columns != getattr(self, "_album_grid_columns", None):
                start = 0
                # Same width for every column (and height for every row),
                # so one odd tile can't widen its column and shift the rest.
                for c in range(columns):
                    self.grid_inner.grid_columnconfigure(c, uniform="album_tile")
            self._album_grid_columns = columns

            # No column weight - see _reflow_video_grid for why: it
            # keeps tiles packed from the left instead of stretched out
            # across any leftover canvas width.
            for i in range(start, len(self._album_tile_frames)):
                frame = self._album_tile_frames[i]
                r, c = divmod(i, columns)
                if c == 0:
                    self.grid_inner.grid_rowconfigure(r, uniform="album_tile")
                frame.grid(row=r, column=c, padx=8, pady=8, sticky="nw")
        except Exception:
            self._show_error_with_traceback("Album grid failed to lay out")

    def _show_error_with_traceback(self, title):
        import traceback
        err = traceback.format_exc()
        print(err)  # also to the terminal, in case the dialog gets dismissed too fast to read
        messagebox.showerror(title, f"{err}\n\nPlease copy this and report it.")

    def open_log_file(self):
        if not os.path.exists(applog.LOG_PATH):
            messagebox.showinfo(
                "No log yet",
                f"Nothing has been logged yet. It'll be created at:\n{applog.LOG_PATH}",
            )
            return
        import subprocess
        try:
            subprocess.Popen(["xdg-open", applog.LOG_PATH])
        except OSError:
            # No xdg-open (not every minimal desktop has it) - fall back
            # to just telling the person where the file is.
            self.show_log_path()
            return
        # xdg-open returns almost immediately, well before the program
        # it launches has necessarily read the file - clearing it right
        # away risks the viewer seeing an empty file instead of what was
        # actually there. A few seconds' delay gives it time to open and
        # buffer the content first.
        self.after(3000, self._finish_log_open)

    def _finish_log_open(self):
        if applog.record_open():
            applog.log("=== log cleared after 3 views ===")
            self.status_var.set(
                f"That was view #{applog.OPENS_BEFORE_RESET} of the log — "
                "it's now been cleared to keep it from growing indefinitely."
            )

    def show_log_path(self):
        exists = os.path.exists(applog.LOG_PATH)
        messagebox.showinfo(
            "Log File Location",
            f"{applog.LOG_PATH}\n\n" + ("" if exists else "(nothing logged yet)"),
        )

    def _mark_tile(self, tile, selected):
        """Grid tiles always keep their 3px selection ring and only change
        its color - toggling the ring's thickness resized the tile and
        nudged every tile after it."""
        if selected:
            ring = "#0a67e5"
        else:
            parent = tile.master
            try:
                ring = ttk.Style(self).lookup(parent.cget("style") or parent.winfo_class(), "background")
            except tk.TclError:
                ring = ""
            ring = ring or self._default_canvas_bg
        tile.configure(highlightbackground=ring, highlightcolor=ring)

    def _make_album_tile(self, parent, row):
        album = row["album"] or "Unknown Album"
        artist = row["artist"] or "Unknown Artist"
        key = (row["album"], row["artist"])
        selected = key in self._selected_albums
        itunes = self.app_settings.get("itunes_theme", False)
        accent = "#0a67e5"

        # A plain tk.Frame (not ttk) so highlightthickness/highlightbackground
        # can draw a colored selection border directly, regardless of theme.
        tile_kwargs = dict(
            width=self.TILE_WIDTH - 10,
            highlightthickness=3,
        )
        if itunes:
            # Real iTunes' grid sits on a plain white pane; give the tile
            # itself that background so it blends in rather than showing
            # the default gray Tk frame color behind the art/text.
            tile_kwargs["bg"] = "#ffffff"
        tile = tk.Frame(parent, **tile_kwargs)
        self._mark_tile(tile, selected)
        self._album_tile_frames_by_key[key] = tile

        photo = self._album_photo_cache.get(key) or (
            self._reflected_placeholder(album) if itunes
            else self._placeholder_photo(album, size=max(self.ALBUM_ART_SIZE))
        )
        art_kwargs = dict(image=photo, cursor="hand2")
        if itunes:
            # The reference screenshot frames each cover with a thin gray
            # border, so cards read distinctly against the white content
            # pane even when the art itself is pale.
            # Rounded covers carry their own hairline edge (see
            # apple_style.round_corners), so no square border here.
            art_kwargs.update(bg="#ffffff", highlightthickness=0)
        art_label = tk.Label(tile, **art_kwargs)
        art_label.image = photo
        art_label.pack(padx=4, pady=4)
        self._album_tile_widgets[key] = art_label

        if itunes:
            # iOpenPod-style card: tinted to match the cover (see
            # _apply_album_tint), one-line title and an
            # "Artist · Year · N tracks" line underneath, left-aligned.
            tint = self._album_tints.get(key) or APPLE_PLACEHOLDER_CARD
            tile.configure(bg=tint)
            art_label.configure(bg=tint)
            art_label.pack_configure(padx=8, pady=(8, 2))
            text_width = max(self.ALBUM_ART_SIZE)
            bits = [artist]
            year = _row_get(row, "year")
            if year:
                bits.append(str(year))
            count = _row_get(row, "track_count")
            if count:
                bits.append(f"{count} track{'s' if count != 1 else ''}")
            # Plain tk.Labels with their own bg, not ttk styles: every new
            # ttk style re-styles every ttk widget in the window, and one
            # per cover color froze big libraries for seconds at a time.
            title_font = (apple_style.FAMILY, 11)
            sub_font = (apple_style.FAMILY, 9)
            name_label = tk.Label(tile, text=self._ellipsize(album, title_font, text_width),
                                  font=title_font, bg=tint, fg=apple_style.TEXT, bd=0, padx=0, pady=0)
            name_label.pack(anchor="w", padx=8)
            artist_label = tk.Label(tile, text=self._ellipsize(" \u00B7 ".join(bits), sub_font, text_width),
                                    font=sub_font, bg=tint, fg=apple_style.SECONDARY, bd=0, padx=0, pady=0)
            artist_label.pack(anchor="w", padx=8, pady=(0, 8))
        else:
            name_label = ttk.Label(tile, text=album, wraplength=self.TILE_WIDTH - 16,
                                    justify=tk.CENTER, font=(apple_style.FAMILY, 9, "bold"), style="TLabel")
            name_label.pack()
            artist_label = ttk.Label(tile, text=artist, wraplength=self.TILE_WIDTH - 16,
                                      justify=tk.CENTER, foreground="#666666", style="TLabel")
            artist_label.pack(pady=(0, 4))
        self._album_tile_labels[key] = (name_label, artist_label)

        if itunes:
            # Every card gets exactly the first card's size and keeps it,
            # so nothing that happens inside one (art arriving, a font
            # settling) can resize it and shove its neighbours.
            size_key = (tuple(self.ALBUM_ART_SIZE), self.TILE_WIDTH)
            size = self._album_tile_sizes.get(size_key)
            if size is None:
                tile.update_idletasks()
                size = (tile.winfo_reqwidth(), tile.winfo_reqheight())
                self._album_tile_sizes[size_key] = size
            tile.configure(width=size[0], height=size[1])
            tile.pack_propagate(False)

        for widget in (tile, art_label, name_label, artist_label):
            widget.bind("<Button-1>", lambda e, k=key: self._on_tile_press(e, k))
            widget.bind("<Double-Button-1>", lambda e, a=row["album"], ar=row["artist"]: self.show_album(a, ar))
            widget.bind("<Button-3>", lambda e, k=key: self._show_album_context_menu(e, k))
            widget.bind("<B1-Motion>", self._on_drag_motion)
            widget.bind("<ButtonRelease-1>", self._on_drag_release)
            self._album_tile_key_by_widget[widget] = key

        return tile

    def _reflected_placeholder(self, album_text):
        """The album grid's no-art-yet swatch, with the same Cover Flow
        reflection real covers get - so a tile doesn't change height when
        its art arrives."""
        size = max(self.ALBUM_ART_SIZE)
        color = APPLE_PLACEHOLDER_ART
        cache_key = ("reflected", color, size)
        cached = self._placeholder_photo_cache.get(cache_key)
        if cached is None:
            cached = ImageTk.PhotoImage(apple_style.add_reflection(apple_style.rounded_swatch(color, size, size)))
            self._placeholder_photo_cache[cache_key] = cached
        return cached

    def _ellipsize(self, text, font, width):
        """Trims text to fit width pixels in font, ending in an ellipsis.
        Character widths are measured once per font and summed here -
        asking Tk to measure the whole string after every trimmed
        character took seconds per batch of album tiles on Tk 9, enough
        for the desktop to report the app as not responding."""
        entry = self._font_cache.get(font)
        if entry is None:
            entry = (tkfont.Font(self, font=font), {})
            self._font_cache[font] = entry
        f, widths = entry

        def w(ch):
            v = widths.get(ch)
            if v is None:
                v = widths[ch] = f.measure(ch)
            return v

        if len(text) * w("W") <= width:
            return text
        total = sum(w(ch) for ch in text)
        if total <= width:
            return text
        limit = width - w("\u2026")
        used = 0
        for i, ch in enumerate(text):
            used += w(ch)
            if used > limit:
                return text[:i].rstrip() + "\u2026"
        return text

    def _album_tile_on_screen(self, key):
        tile = self._album_tile_frames_by_key.get(key)
        if tile is None or not tile.winfo_exists() or not self.grid_canvas.winfo_ismapped():
            return False
        top = self.grid_canvas.canvasy(0)
        y = tile.winfo_y()
        return top - tile.winfo_height() < y < top + self.grid_canvas.winfo_height()

    def _apply_album_tint(self, key, tint, animate=True):
        """Eases the card from its neutral loading color to the cover's
        tint, in step with the cover fading in."""
        tile = self._album_tile_frames_by_key.get(key)
        if tint is None or tile is None or not tile.winfo_exists():
            return
        widgets = [tile]
        art = self._album_tile_widgets.get(key)
        if art is not None:
            widgets.append(art)
        widgets += [w for w in (self._album_tile_labels.get(key) or ()) if isinstance(w, tk.Label)]
        try:
            start = tuple(c // 257 for c in tile.winfo_rgb(tile.cget("bg")))
            end = tuple(c // 257 for c in tile.winfo_rgb(tint))
        except tk.TclError:
            return
        steps = 6 if animate and self._smooth_motion() else 1

        def tick(i=1):
            f = i / steps
            color = "#%02x%02x%02x" % tuple(round(a + (b - a) * f) for a, b in zip(start, end))
            for w in widgets:
                try:
                    w.configure(bg=color)
                except tk.TclError:
                    return
            if i < steps:
                tile.after(22, tick, i + 1)

        tick()

    def _on_tile_press(self, event, key):
        """Mirrors _on_table_press: a plain click on a tile that's
        already part of a multi-selection would normally toggle just
        that tile (which, mid-drag, both breaks dragging the whole
        selection and makes a quick second click land as a double-click
        that opens the album). So that case is deferred to release
        instead of applied immediately."""
        self._pending_click_key = None
        if event.state & 0x0004:
            # Ctrl-click adds or removes just this album, keeping the
            # rest of the selection (a drag that starts here still
            # carries whatever is selected afterwards).
            self.toggle_album_selection(key)
            return
        if key in self._selected_albums and len(self._selected_albums) > 1:
            self._pending_click_key = key
            return
        self.toggle_album_selection(key)

    def _set_album_selection(self, new_keys):
        old_keys = self._selected_albums
        self._selected_albums = set(new_keys)
        self._note_unticked_albums(old_keys - self._selected_albums)
        self._note_reticked_albums(self._selected_albums - old_keys)
        for k in old_keys | self._selected_albums:
            tile = self._album_tile_frames_by_key.get(k)
            if tile is not None and tile.winfo_exists():
                self._mark_tile(tile, k in self._selected_albums)
        self._update_album_selection_status()

    # ------------------------------------------------------ drag & drop --
    # Click-and-drag songs (from the track table) or albums (from the
    # grid) onto a playlist or a mounted iPod in the sidebar. Both
    # widgets already handle plain clicks for selection on ButtonPress,
    # so rather than a separate ButtonPress handler (which would race
    # the Treeview's/tile's own click handling), the drag payload is
    # captured lazily on the first motion tick after the button goes
    # down — by then the click's own selection change has already
    # applied, so "whatever's selected" is exactly what should drag.

    def _on_table_press(self, event):
        """Runs before the Treeview's own class binding (instance
        bindings fire first). A plain click on a row that's already
        part of a multi-selection would normally collapse the
        selection down to just that row immediately - which is right
        for a click, but wrong for the start of a drag, where the
        whole selection should move together. So when that's about to
        happen, this defers the collapse (returning "break" stops the
        class binding from running) until release, by which point
        _on_drag_release knows whether a drag actually happened."""
        self._pending_click_iid = None
        # A click on a column heading sorts. identify_row() still names
        # the song scrolled up behind the heading, so check this first.
        if self.table.identify_region(event.x, event.y) in ("heading", "separator"):
            self._drag_data = {"track_ids": None}  # and isn't the start of a drag
            return
        iid = self.table.identify_row(event.y)
        if not iid:
            return
        ctrl_or_shift = bool(event.state & 0x0005)  # Control (0x4) or Shift (0x1)
        if ctrl_or_shift:
            return  # let normal multi-select toggle/range-select run as usual
        sel = self.table.selection()
        if iid in sel and len(sel) > 1:
            self._pending_click_iid = iid
            return "break"

    def _resolve_drag_payload(self, widget):
        """Returns {"track_ids": [...], "label": "..."} for whatever's
        selected/clicked on the given source widget, or None if that
        widget isn't a drag source (or nothing's draggable right now)."""
        if widget is self.table:
            if self.current_view not in self.LOCAL_TRACK_VIEWS:
                return None  # device-track views aren't a drag source here
            sel = [iid for iid in self.table.selection() if iid.startswith("t:")]
            if not sel:
                return None
            track_ids = [int(iid.split(":", 1)[1]) for iid in sel]
            label = f"\U0001F3B5 {len(track_ids)} track{'s' if len(track_ids) != 1 else ''}"
            return {"track_ids": track_ids, "label": label}

        key = self._album_tile_key_by_widget.get(widget)
        if key is None:
            return None
        keys = self._selected_albums if key in self._selected_albums else {key}
        track_ids = []
        for album, artist in keys:
            track_ids.extend(r["id"] for r in self.lib.tracks_by_album(album, artist))
        if not track_ids:
            return None
        if len(keys) == 1:
            label = f"\U0001F5B4 {next(iter(keys))[0] or 'Unknown Album'}"
        else:
            label = f"\U0001F5B4 {len(keys)} albums"
        return {"track_ids": track_ids, "label": label}

    def _on_drag_motion(self, event):
        data = self._drag_data
        if data is None:
            payload = self._resolve_drag_payload(event.widget)
            if payload is None:
                self._drag_data = {"track_ids": None}  # nothing draggable; ignore rest of this gesture
                return
            self._drag_data = {
                "track_ids": payload["track_ids"], "label": payload["label"],
                "start_x": event.x_root, "start_y": event.y_root, "active": False,
            }
            return

        if data.get("track_ids") is None:
            return

        dx = abs(event.x_root - data["start_x"])
        dy = abs(event.y_root - data["start_y"])
        if not data["active"] and (dx > 6 or dy > 6):
            data["active"] = True
            self._show_drag_indicator(data["label"])
        if data["active"]:
            self._move_drag_indicator(event)

    def _on_drag_release(self, event):
        data = self._drag_data
        self._drag_data = None
        self._hide_drag_indicator()

        pending_iid = self._pending_click_iid
        self._pending_click_iid = None
        pending_key = self._pending_click_key
        self._pending_click_key = None

        if not data or not data.get("active"):
            # No drag happened - if we deferred a selection-collapse
            # (see _on_table_press / _on_tile_press), this was just a
            # plain click on one of several selected items, so apply
            # the collapse now.
            if pending_iid is not None:
                self.table.selection_set(pending_iid)
            if pending_key is not None:
                if self._device_view_active:
                    # In the device view ticks are a sync list, so a click
                    # only ever flips the album clicked.
                    self.toggle_album_selection(pending_key)
                else:
                    self._set_album_selection({pending_key})
            return

        target_widget = self.winfo_containing(event.x_root, event.y_root)
        if target_widget is not self.sidebar:
            return
        y_local = event.y_root - self.sidebar.winfo_rooty()
        target_iid = self.sidebar.identify_row(y_local)
        if target_iid:
            self._handle_drop(data["track_ids"], target_iid)

    def _show_drag_indicator(self, label):
        win = tk.Toplevel(self)
        win.overrideredirect(True)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        tk.Label(
            win, text=label, background="#333333", foreground="#ffffff",
            padx=8, pady=4, font=(apple_style.FAMILY, 9),
        ).pack()
        self._drag_indicator = win

    def _move_drag_indicator(self, event):
        if self._drag_indicator is not None and self._drag_indicator.winfo_exists():
            self._drag_indicator.geometry(f"+{event.x_root + 14}+{event.y_root + 10}")

    def _hide_drag_indicator(self):
        if self._drag_indicator is not None:
            try:
                self._drag_indicator.destroy()
            except tk.TclError:
                pass
            self._drag_indicator = None

    def _handle_drop(self, track_ids, target_iid):
        if not track_ids:
            return

        if target_iid.startswith("lp:"):
            pl_id = int(target_iid.split(":", 1)[1])
            self._last_playlist_id = pl_id
            self._add_track_ids_to_playlist(track_ids, pl_id)

        elif target_iid == "device" or target_iid == "device_all" or target_iid.startswith("dp:"):
            # Dropping on any device entry — the general iPod row or a
            # specific on-device playlist — syncs the tracks to the
            # iPod; on-device playlist membership still follows each
            # track's own local playlists (see sync_tracks), the same
            # as the toolbar's Sync button, rather than filing them
            # under just the playlist that was dropped on.
            if not self.ipod_mountpoint:
                messagebox.showwarning("No iPod", "No iPod is currently mounted.")
                return
            rows = self.lib.tracks_by_ids(track_ids)
            self._sync_rows_to_ipod(rows)
        # dropped on a header, Settings, Songs, or Albums — no-op

    # ------------------------------------------------- album context menu --

    def _album_context_targets(self, key):
        """Which (album, artist) keys a context-menu action on this tile
        should apply to: the whole multi-selection if the right-clicked
        tile is part of it, otherwise just this one album — so
        right-clicking outside a selection acts on just that tile, and
        right-clicking within one acts on everything selected."""
        if key in self._selected_albums and len(self._selected_albums) > 1:
            return set(self._selected_albums)
        return {key}

    def _track_ids_for_album_keys(self, keys):
        track_ids = []
        for album, artist in keys:
            track_ids.extend(r["id"] for r in self.lib.tracks_by_album(album, artist))
        return track_ids

    def _show_album_context_menu(self, event, key):
        targets = self._album_context_targets(key)
        multi = len(targets) > 1

        menu = tk.Menu(self, tearoff=0)
        if not multi:
            album, artist = key
            menu.add_command(label="Edit Album Info…", command=lambda: self.edit_album_dialog(album, artist))
            menu.add_separator()
        menu.add_command(
            label="Add to Playlist…",
            command=lambda: self._add_track_ids_to_playlist_picker(self._track_ids_for_album_keys(targets)),
        )
        menu.add_command(
            label="Add to Last Selected Playlist",
            command=lambda: self._add_track_ids_to_last_playlist(self._track_ids_for_album_keys(targets)),
            state="normal" if self._last_playlist_id is not None else "disabled",
        )
        menu.add_separator()
        menu.add_command(
            label="Sync to iPod",
            command=lambda: self._sync_rows_to_ipod(self.lib.tracks_by_ids(self._track_ids_for_album_keys(targets))),
        )
        menu.add_command(
            label="Remove from iPod",
            command=lambda: self._remove_track_ids_from_device(self._track_ids_for_album_keys(targets)),
            state="normal" if self.ipod_mountpoint else "disabled",
        )
        menu.add_separator()
        menu.add_command(
            label="Remove from Library" + (f" ({len(targets)} albums)" if multi else ""),
            command=lambda: self.remove_albums_from_library(targets),
        )
        menu.tk_popup(event.x_root, event.y_root)

    def remove_albums_from_library(self, keys):
        keys = list(keys)
        track_ids = self._track_ids_for_album_keys(keys)
        if not track_ids:
            return
        if len(keys) == 1:
            what = f"\u201c{keys[0][0] or 'Unknown Album'}\u201d ({len(track_ids)} song(s))"
        else:
            what = f"{len(keys)} albums ({len(track_ids)} songs)"
        if not messagebox.askyesno(
            "Remove from Library",
            f"Remove {what} from your library?\n\nThe music files stay where they are on "
            "your computer, and Refresh Library won't add them back (Settings \u2192 General "
            "can undo that). Copies already on an iPod stay there until you remove them.",
        ):
            return
        removed = self.lib.remove_tracks_from_library(track_ids)
        applog.log(f"LIBRARY: removed {removed} song(s) in {len(keys)} album(s) from the library")
        for key in keys:
            self._selected_albums.discard(key)
        self._persist_album_marks()
        self.status_var.set(f"Removed {removed} song(s) from your library.")
        if self.current_view == self.VIEW_ALBUM_GRID:
            self.show_album_grid()
        else:
            self.on_sidebar_select()
        self._update_library_summary()
        if hasattr(self, "removed_songs_count_var"):
            self._update_removed_songs_count()

    def _note_unticked_albums(self, keys):
        if not self._device_view_active:
            return
        keys = set(keys)
        album_iids = set()
        for album, artist in keys:
            ids = [r["id"] for r in self.lib.tracks_by_album(album, artist)]
            self._unticked_track_ids.update(ids)
            album_iids.update(f"t:{tid}" for tid in ids)
        # Unticking an album unticks its songs on the Songs screen too,
        # so a song ticked there earlier doesn't keep it on the iPod.
        if album_iids & self._persistent_table_selection:
            self._persistent_table_selection -= album_iids
            self._persist_track_marks()
        # Nothing comes off the iPod yet: Done or Sync removes unticked
        # songs, after asking (see close_device_view / _reconcile_and_sync_music).

    def _note_reticked_albums(self, keys):
        if not self._device_view_active:
            return
        added = set()
        for album, artist in keys:
            ids = [r["id"] for r in self.lib.tracks_by_album(album, artist)]
            self._unticked_track_ids.difference_update(ids)
            added.update(f"t:{tid}" for tid in ids)
        self._albums_to_remove -= set(keys)
        # A ticked album shows its songs ticked on the Songs screen too.
        if added - self._persistent_table_selection:
            self._persistent_table_selection |= added
            self._persist_track_marks()

    def _schedule_album_removal(self, delay=1200):
        if self._albums_remove_after_id:
            self.after_cancel(self._albums_remove_after_id)
        self._albums_remove_after_id = self.after(delay, self._remove_unticked_albums_now)

    def _remove_unticked_albums_now(self):
        """An album unticked in the device view comes off the iPod right
        away (after a short pause, so a quick untick-and-tick-back does
        nothing). Songs still wanted another way (ticked on their own,
        or in a ticked playlist) stay."""
        self._albums_remove_after_id = None
        if not self._albums_to_remove or not self.ipod_mountpoint:
            self._albums_to_remove = set()
            return
        if self._active_sync_handle is not None or self._post_remove_callback is not None:
            self._schedule_album_removal(1500)  # wait for the current iPod write
            return
        keys = self._albums_to_remove
        self._albums_to_remove = set()
        rows = []
        for album, artist in keys:
            rows.extend(self.lib.tracks_by_album(album, artist))
        wanted = self._compute_wanted_music_track_ids()
        rows = [r for r in rows if r["id"] not in wanted]
        on_device = self._device_tracks_for_local_ids(r["id"] for r in rows)
        names = ", ".join(sorted((a or "Unknown Album") for a, _ar in keys))
        if not on_device:
            applog.log(f"UNTICK: nothing on the iPod matched {len(rows)} song(s) of {names}")
            self.status_var.set(f"None of the songs from {names} were found on the iPod.")
            return
        if len(keys) > 3 and not self._confirm_reconcile_removal(on_device, "track"):
            return
        self._unticked_track_ids.difference_update(r["id"] for r in rows)
        self.status_var.set(f"Removing {len(on_device)} song(s) from the iPod\u2026")
        self._remove_dbids_then([t["dbid"] for t in on_device], lambda: None, label="UNTICK ALBUM")

    def _unticked_tracks_on_device(self):
        """iPod tracks matching songs unticked in the device view this
        visit (directly, or as part of an album) that nothing still
        ticked wants."""
        if not self._unticked_track_ids or not self.ipod_mountpoint:
            return []
        gone = self._unticked_track_ids - self._compute_wanted_music_track_ids()
        if not gone:
            return []
        return self._device_tracks_for_local_ids(gone)

    def toggle_album_selection(self, key):
        if key in self._selected_albums:
            self._selected_albums.discard(key)
            self._note_unticked_albums([key])
        else:
            self._selected_albums.add(key)
            self._note_reticked_albums([key])
        tile = self._album_tile_frames_by_key.get(key)
        if tile is not None and tile.winfo_exists():
            self._mark_tile(tile, key in self._selected_albums)
        self._update_album_selection_status()
        # Durable, so this is remembered across app restarts/reconnects
        # - the whole point being a sync profile you set up once, not
        # something you re-pick every time. Only in the device view:
        # the normal library's own album selection (for the ordinary
        # Sync Selected button there) stays a one-off, in-memory pick.
        self._persist_album_marks()

    def _update_album_selection_status(self):
        n = len(self._selected_albums)
        if n:
            self.status_var.set(
                f"{n} album{'s' if n != 1 else ''} selected \u2014 Sync Selected \u2192 iPod to sync "
                "them, or click again to deselect."
            )
        else:
            self.status_var.set(
                f"{len(self._album_rows)} album(s). Click to select, double-click to open, "
                "right-click to edit."
            )

    def _placeholder_photo(self, album_text, size=120):
        """size can be a single int (square placeholder, used for
        albums/photos) or an (width, height) tuple (used for video/show
        poster tiles, so a missing thumbnail still shows a portrait-
        shaped block rather than a square one that looks out of place
        next to real posters)."""
        w, h = size if isinstance(size, tuple) else (size, size)
        color = PLACEHOLDER_COLORS[hash(album_text or "") % len(PLACEHOLDER_COLORS)]
        cache_key = (color, w, h)
        cached = self._placeholder_photo_cache.get(cache_key)
        if cached is not None:
            return cached
        swatch = apple_style.rounded_swatch(color, w, h) if self._smooth_motion() else None
        if swatch is not None:
            img = ImageTk.PhotoImage(swatch)
        else:
            img = tk.PhotoImage(width=w, height=h)
            img.put(color, to=(0, 0, w, h))
        self._placeholder_photo_cache[cache_key] = img
        return img

    def show_album(self, album, artist):
        self._show_body("table")
        self.current_view = self.VIEW_ALBUM_TRACKS
        self.current_local_playlist_id = None
        self.current_album_key = (album, artist)
        self._populate_table_local(self.lib.tracks_by_album(album, artist))
        self.status_var.set(f"{album or 'Unknown Album'} — {artist or 'Unknown Artist'}")

    def show_artist_list(self):
        self.current_view = self.VIEW_ARTISTS
        self._show_body("artists")
        self._populate_name_list(
            self.artist_tree, self._artist_raw_values,
            self.lib.artist_summary(), "artist", "Unknown Artist",
        )
        self.status_var.set(f"{len(self._artist_raw_values)} artist(s)")

    def show_genre_list(self):
        self.current_view = self.VIEW_GENRES
        self._show_body("genres")
        self._populate_name_list(
            self.genre_tree, self._genre_raw_values,
            self.lib.genre_summary(), "genre", "Unknown Genre",
        )
        self.status_var.set(f"{len(self._genre_raw_values)} genre(s)")

    def show_tracks_for_artist(self, artist):
        self._show_body("table")
        self.current_view = self.VIEW_ARTIST_TRACKS
        self.current_artist = artist
        rows = self.lib.tracks_by_artist(artist)
        self._populate_table_local(rows)
        self.status_var.set(f"{artist or 'Unknown Artist'} — {len(rows)} track(s)")

    def show_tracks_for_genre(self, genre):
        self._show_body("table")
        self.current_view = self.VIEW_GENRE_TRACKS
        self.current_genre = genre
        rows = self.lib.tracks_by_genre(genre)
        self._populate_table_local(rows)
        self.status_var.set(f"{genre or 'Unknown Genre'} — {len(rows)} track(s)")

    def show_recently_added(self):
        self._show_body("table")
        self.current_view = self.VIEW_RECENTLY_ADDED
        rows = self.lib.all_tracks(order_by="date_added DESC")
        self._populate_table_local(rows)
        self.status_var.set(f"Recently Added — {len(rows)} track(s), newest first")

    def _get_album_edit_preview_photo(self, sample_filepath, album_text):
        if HAVE_PIL and sample_filepath:
            img = artwork.get_cover_image(sample_filepath, max_size=(120, 120))
            if img is not None:
                return ImageTk.PhotoImage(img)
        return self._placeholder_photo(album_text)

    def edit_album_dialog(self, album, artist):
        tracks = self.lib.tracks_by_album(album, artist)
        sample_filepath = tracks[0]["filepath"] if tracks else None

        dialog = tk.Toplevel(self)
        dialog.title("Edit Album Info")
        dialog.transient(self)
        dialog.resizable(False, False)

        state = {"pending_art_bytes": None, "pending_art_ext": ".jpg"}

        frm = ttk.Frame(dialog, padding=16)
        frm.pack(fill=tk.BOTH, expand=True)

        art_frame = ttk.Frame(frm)
        art_frame.grid(row=0, column=0, rowspan=6, padx=(0, 16), sticky="n")
        preview_photo = self._get_album_edit_preview_photo(sample_filepath, album or "Unknown Album")
        art_preview_label = tk.Label(art_frame, image=preview_photo)
        art_preview_label.image = preview_photo
        art_preview_label.pack()

        def choose_image_file():
            path = native_dialogs.choose_open_file(
                title="Choose album art image",
                filetypes=[("Image files", "*.jpg *.jpeg *.png"), ("All files", "*.*")],
            )
            if not path:
                return
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError as e:
                messagebox.showerror("Couldn't read image", str(e))
                return
            state["pending_art_bytes"] = data
            state["pending_art_ext"] = os.path.splitext(path)[1].lower() or ".jpg"
            self._set_preview_image_from_bytes(art_preview_label, data)

        ttk.Button(art_frame, text="Choose Image File…", command=choose_image_file).pack(
            pady=(8, 2), fill=tk.X
        )

        search_button = ttk.Button(art_frame, text="Search Internet for Album Info…")
        search_button.pack(pady=2, fill=tk.X)

        album_var = tk.StringVar(value=album or "")
        artist_var = tk.StringVar(value=artist or "")
        genre_var = tk.StringVar(value=(tracks[0]["genre"] if tracks else "") or "")
        year_var = tk.StringVar(value=str(tracks[0]["year"]) if tracks and tracks[0]["year"] else "")

        def apply_search_result(result):
            if result.get("collection_name"):
                album_var.set(result["collection_name"])
            if result.get("artist_name"):
                artist_var.set(result["artist_name"])
            if result.get("genre"):
                genre_var.set(result["genre"])
            if result.get("year"):
                year_var.set(result["year"])
            artwork_url = result.get("artwork_url")
            if artwork_url:
                search_button.configure(state="disabled", text="Downloading art…")

                def work():
                    try:
                        data = lookup.fetch_artwork_bytes(artwork_url)
                        state["pending_art_bytes"] = data
                        state["pending_art_ext"] = ".jpg"
                        self.work_queue.put(("album_art_downloaded", (data, art_preview_label)))
                    except lookup.LookupError as e:
                        self.work_queue.put(("error", f"Couldn't download artwork: {e}"))
                    finally:
                        self.work_queue.put(("album_search_button_reset", search_button))

                threading.Thread(target=work, daemon=True).start()

        search_button.configure(
            command=lambda: self._open_album_search_dialog(
                dialog, artist_var.get(), album_var.get(), apply_search_result
            )
        )

        ttk.Label(frm, text="Album:").grid(row=0, column=1, sticky="e", pady=4)
        ttk.Entry(frm, textvariable=album_var, width=32).grid(row=0, column=2, sticky="w", pady=4)
        ttk.Label(frm, text="Artist:").grid(row=1, column=1, sticky="e", pady=4)
        ttk.Entry(frm, textvariable=artist_var, width=32).grid(row=1, column=2, sticky="w", pady=4)
        ttk.Label(frm, text="Genre:").grid(row=2, column=1, sticky="e", pady=4)
        try:
            existing_genres = self.lib.all_genres()
        except Exception:
            existing_genres = []
        genre_combo = ttk.Combobox(frm, textvariable=genre_var, width=29, values=existing_genres)
        genre_combo.grid(row=2, column=2, sticky="w", pady=4)
        ttk.Label(frm, text="Year:").grid(row=3, column=1, sticky="e", pady=4)
        ttk.Entry(frm, textvariable=year_var, width=32).grid(row=3, column=2, sticky="w", pady=4)

        ttk.Label(
            frm, text=f"{len(tracks)} track(s) in this album will be updated.",
            foreground="#666666",
        ).grid(row=4, column=1, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(
            frm, text="Text edits and artwork are both written directly into each\n"
                      "track's own tags (MP3/M4A only; other formats show a warning).",
            foreground="#888888", font=(apple_style.FAMILY, 8),
        ).grid(row=5, column=1, columnspan=2, sticky="w")

        # Track numbers, one per song, so the album (and the iPod) play
        # and sort in the right order. Blank ones are guessed from a
        # leading number in the title or file name, like "(01) - Fat".
        ttk.Label(frm, text="Track numbers", font=(apple_style.FAMILY, 10, "bold")).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(14, 2))
        number_rows = []  # (track row, StringVar, original text)
        guessed_any = False
        list_outer = ttk.Frame(frm)
        list_outer.grid(row=7, column=0, columnspan=3, sticky="we")
        list_canvas = tk.Canvas(list_outer, highlightthickness=0, borderwidth=0,
                                background=ttk.Style(dialog).lookup("TFrame", "background") or "#ffffff")
        list_inner = ttk.Frame(list_canvas)
        list_canvas.create_window((0, 0), window=list_inner, anchor="nw")
        def start_value(t):
            return (str(t["track_nr"]) if t["track_nr"] else "") or _guess_track_number(t["title"], t["filepath"])

        listed = sorted(tracks, key=lambda t: (t["cd_nr"] or 0, int(start_value(t) or 9999),
                                               (t["title"] or "").lower()))
        for t in listed:
            original = str(t["track_nr"]) if t["track_nr"] else ""
            value = start_value(t)
            if value and not original:
                guessed_any = True
            var = tk.StringVar(value=value)
            line = ttk.Frame(list_inner)
            line.pack(fill=tk.X, pady=1)
            ttk.Entry(line, textvariable=var, width=4, justify=tk.RIGHT).pack(side=tk.LEFT)
            ttk.Label(line, text=(t["title"] or os.path.basename(t["filepath"])),
                      width=46).pack(side=tk.LEFT, padx=(8, 0))
            number_rows.append((t, var, original))
        list_inner.update_idletasks()
        list_canvas.configure(width=list_inner.winfo_reqwidth(),
                              height=min(list_inner.winfo_reqheight(), 260),
                              scrollregion=(0, 0, list_inner.winfo_reqwidth(), list_inner.winfo_reqheight()))
        list_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        if list_inner.winfo_reqheight() > 260:
            list_scroll = ttk.Scrollbar(list_outer, orient=tk.VERTICAL, command=list_canvas.yview)
            list_scroll.pack(side=tk.RIGHT, fill=tk.Y)
            list_canvas.configure(yscrollcommand=list_scroll.set)

            def on_wheel(event):
                step = -1 if (getattr(event, "num", 0) == 4 or getattr(event, "delta", 0) > 0) else 1
                list_canvas.yview_scroll(step, "units")
                return "break"

            for w in [list_canvas, list_inner] + list(list_inner.winfo_children()):
                for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                    w.bind(seq, on_wheel, add="+")
                    for child in w.winfo_children():
                        child.bind(seq, on_wheel, add="+")
        if guessed_any:
            ttk.Label(frm, text="Numbers that were blank were filled in from the song titles; "
                                "check them before saving.",
                      foreground="#888888", font=(apple_style.FAMILY, 8)).grid(
                row=8, column=0, columnspan=3, sticky="w", pady=(2, 0))

        button_row = ttk.Frame(frm)
        button_row.grid(row=9, column=0, columnspan=3, pady=(16, 0), sticky="e")

        def do_save():
            number_edits = {}
            for t, var, original in number_rows:
                text = var.get().strip()
                if text == original:
                    continue
                if text and not text.isdigit():
                    messagebox.showerror("Invalid Number",
                                         "Track numbers must be whole numbers (or blank).", parent=dialog)
                    return
                number_edits[t["id"]] = {
                    "track_nr": int(text) if text else None,
                    "track_total": t["track_total"] or len(tracks),
                }
            mountpoint = self.ipod_mountpoint
            device_tracks = list(self.device_tracks or []) if mountpoint else []
            device_links = dict(self.device_track_links)
            device_busy = self._active_sync_handle is not None
            new_album = album_var.get().strip() or None
            new_artist = artist_var.get().strip() or None
            new_genre = genre_var.get().strip()
            year_str = year_var.get().strip()
            new_year = int(year_str) if year_str.isdigit() else None

            save_button.configure(state="disabled")
            cancel_button.configure(state="disabled")

            def work():
                try:
                    affected = self.lib.update_album_info(
                        album, artist, new_album, new_artist, new_genre, new_year
                    )
                except Exception as e:
                    self.work_queue.put(("error", f"Could not update library: {e}"))
                    self.work_queue.put(("album_edit_saved", (dialog, 0, [], [])))
                    return

                tag_errors = []
                art_errors = []
                for t in affected:
                    if not tagwriter.can_write_tags(t["filepath"]):
                        continue
                    ok, err = tagwriter.write_album_tags(
                        t["filepath"], album=new_album, artist=new_artist,
                        genre=new_genre, year=new_year,
                    )
                    if not ok:
                        tag_errors.append(f"{os.path.basename(t['filepath'])}: {err}")

                    if state["pending_art_bytes"]:
                        ok, err = tagwriter.write_embedded_art(
                            t["filepath"], state["pending_art_bytes"], state["pending_art_ext"]
                        )
                        if not ok:
                            art_errors.append(f"{os.path.basename(t['filepath'])}: {err}")

                if number_edits:
                    try:
                        fresh = self.lib.tracks_by_ids(list(number_edits))
                        _saved, warnings, _dev, device_note = self._save_track_info(
                            fresh, {}, None, None, mountpoint, device_tracks, device_busy,
                            device_links, per_row=number_edits,
                        )
                        tag_errors.extend(warnings)
                        if device_note:
                            tag_errors.append(device_note)
                        applog.log(f"ALBUM EDIT: set track numbers on {len(number_edits)} track(s)")
                    except Exception as e:
                        tag_errors.append(f"Track numbers: {e}")

                self.work_queue.put(("album_edit_saved", (dialog, len(affected), tag_errors, art_errors)))

            threading.Thread(target=work, daemon=True).start()

        cancel_button = ttk.Button(button_row, text="Cancel", command=dialog.destroy)
        cancel_button.pack(side=tk.RIGHT, padx=(6, 0))
        save_button = ttk.Button(button_row, text="Save", command=do_save)
        save_button.pack(side=tk.RIGHT)

        # grab_set() requires the window to actually be mapped on screen
        # first, or it raises "window not viewable" - update_idletasks()
        # alone isn't always enough (timing varies by Tk build/platform),
        # so wait_visibility() blocks until it genuinely is.
        dialog.update_idletasks()
        dialog.wait_visibility()
        dialog.grab_set()

    # ------------------------------------------------- song / episode info --

    # (column, label) per editor field. Episodes keep their "Podcast"
    # genre (the Podcasts library keys off it), so they don't get one.
    _SONG_INFO_FIELDS = (
        ("title", "Title"), ("artist", "Artist"), ("album", "Album"),
        ("albumartist", "Album Artist"), ("composer", "Composer"),
        ("genre", "Genre"), ("year", "Year"),
    )
    _EPISODE_INFO_FIELDS = (
        ("title", "Episode Title"), ("album", "Show"), ("artist", "Author"),
        ("year", "Year"),
    )
    _INFO_NUMBER_COLUMNS = ("year", "track_nr", "track_total", "cd_nr", "cd_total")

    def get_info_for_selection(self, event=None):
        if self.current_view not in self.LOCAL_TRACK_VIEWS:
            return "break" if event else None
        sel = [iid for iid in self.table.selection() if iid.startswith("t:")]
        if sel:
            self.track_info_dialog([int(iid.split(":", 1)[1]) for iid in sel])
        return "break" if event else None

    def track_info_dialog(self, track_ids):
        """Get Info: edits title/artist/album/etc. for one or more songs
        or podcast episodes. Like the album editor, every change is
        written into the files' own tags (so Refresh Library keeps it)
        and, when the iPod is plugged in, onto the copies already there
        (otherwise the next sync would see an edited song as new and
        copy it a second time)."""
        rows = self.lib.tracks_by_ids(track_ids)
        if not rows:
            return
        multi = len(rows) > 1
        episodes = all((r["genre"] or "") == "Podcast" for r in rows)
        kind = "Episode" if episodes else "Song"

        dialog = tk.Toplevel(self)
        dialog.title(f"{kind} Info" if not multi else f"{len(rows)} {kind}s")
        dialog.transient(self)
        dialog.resizable(False, False)

        state = {"pending_art_bytes": None, "pending_art_ext": ".jpg"}

        frm = ttk.Frame(dialog, padding=16)
        frm.pack(fill=tk.BOTH, expand=True)
        frm.grid_columnconfigure(1, weight=1)

        # Header: artwork beside the current title, iTunes Get Info style.
        header = ttk.Frame(frm)
        header.grid(row=0, column=0, columnspan=2, sticky="we", pady=(0, 12))
        first = rows[0]
        preview_photo = self._get_album_edit_preview_photo(
            first["filepath"], first["album"] or first["title"] or "?"
        )
        art_label = tk.Label(header, image=preview_photo, cursor="hand2", borderwidth=0)
        art_label.image = preview_photo
        art_label.grid(row=0, column=0, rowspan=4, sticky="nw", padx=(0, 14))

        def common(col):
            values = {r[col] for r in rows}
            return (values.pop(), False) if len(values) == 1 else (None, True)

        if multi:
            heading = f"{len(rows)} {'episodes' if episodes else 'songs'} selected"
            sub = "Fields marked Mixed differ between them; leave those alone to keep each one's own value."
        else:
            heading = first["title"] or os.path.basename(first["filepath"])
            sub = " — ".join(x for x in (first["artist"], first["album"]) if x)
        ttk.Label(header, text=heading, font=(apple_style.FAMILY, 13, "bold"),
                  wraplength=250, justify=tk.LEFT).grid(row=0, column=1, sticky="w")
        if sub:
            ttk.Label(header, text=sub, foreground=apple_style.SECONDARY,
                      wraplength=250, justify=tk.LEFT).grid(row=1, column=1, sticky="w")
        if not multi:
            facts = [(first["filetype"] or "").upper()]
            if first["duration_ms"]:
                secs = int(first["duration_ms"] / 1000)
                facts.append(f"{secs // 60}:{secs % 60:02d}")
            if first["bitrate"]:
                facts.append(f"{first['bitrate']} kbps")
            if first["size_bytes"]:
                facts.append(f"{first['size_bytes'] / 1048576:.1f} MB")
            ttk.Label(header, text="  ·  ".join(f for f in facts if f),
                      foreground=apple_style.TERTIARY,
                      font=(apple_style.FAMILY, 9)).grid(row=2, column=1, sticky="w", pady=(4, 0))

        def choose_image_file(event=None):
            path = native_dialogs.choose_open_file(
                title="Choose artwork image",
                filetypes=[("Image files", "*.jpg *.jpeg *.png"), ("All files", "*.*")],
            )
            if not path:
                return
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError as e:
                messagebox.showerror("Couldn't read image", str(e), parent=dialog)
                return
            state["pending_art_bytes"] = data
            state["pending_art_ext"] = os.path.splitext(path)[1].lower() or ".jpg"
            self._set_preview_image_from_bytes(art_label, data)

        art_label.bind("<Button-1>", choose_image_file)
        ttk.Button(header, text="Change Artwork…", command=choose_image_file).grid(
            row=3, column=1, sticky="w", pady=(8, 0)
        )

        style = ttk.Style(dialog)
        style.configure("Mixed.TEntry", foreground=apple_style.TERTIARY)
        style.configure("Mixed.TCombobox", foreground=apple_style.TERTIARY)

        # col -> (var, initial text, widget, mixed?)
        entries = {}

        def add_field(parent, col, width, combo_values=None):
            value, mixed = common(col)
            initial = "" if value is None else str(value)
            var = tk.StringVar(value="Mixed" if mixed else initial)
            if combo_values is not None:
                widget = ttk.Combobox(parent, textvariable=var, width=width - 3, values=combo_values)
            else:
                widget = ttk.Entry(parent, textvariable=var, width=width)
            if mixed:
                widget.configure(style="Mixed.TCombobox" if combo_values is not None else "Mixed.TEntry")

                def clear_placeholder(event, var=var, widget=widget, col=col):
                    if entries[col][3]:
                        entries[col] = (var, "", widget, False)
                        var.set("")
                        widget.configure(style="TCombobox" if combo_values is not None else "TEntry")

                widget.bind("<FocusIn>", clear_placeholder, add="+")
                if combo_values is not None:
                    widget.bind("<<ComboboxSelected>>", clear_placeholder, add="+")
            entries[col] = (var, initial, widget, mixed)
            return widget

        fields = self._EPISODE_INFO_FIELDS if episodes else self._SONG_INFO_FIELDS
        try:
            genres = [g for g in self.lib.all_genres() if g and g != "Podcast"]
        except Exception:
            genres = []
        r = 1
        for col, label in fields:
            if multi and col == "title":
                continue
            ttk.Label(frm, text=label + ":").grid(row=r, column=0, sticky="e", padx=(0, 8), pady=3)
            if col == "year":
                add_field(frm, col, 8).grid(row=r, column=1, sticky="w", pady=3)
            else:
                add_field(frm, col, 36, genres if col == "genre" else None).grid(
                    row=r, column=1, sticky="we", pady=3
                )
            r += 1

        if episodes:
            # One episode number; "of N" means nothing for a feed.
            if not multi:
                ttk.Label(frm, text="Episode:").grid(row=r, column=0, sticky="e", padx=(0, 8), pady=3)
                add_field(frm, "track_nr", 5).grid(row=r, column=1, sticky="w", pady=3)
                r += 1
        else:
            for nr_col, total_col, label in (("track_nr", "track_total", "Track"),
                                             ("cd_nr", "cd_total", "Disc")):
                box = ttk.Frame(frm)
                box.grid(row=r, column=1, sticky="w", pady=3)
                if multi:
                    # Each song keeps its own number; only the total is shared.
                    ttk.Label(frm, text=f"{label}s in Total:").grid(
                        row=r, column=0, sticky="e", padx=(0, 8), pady=3)
                else:
                    ttk.Label(frm, text=label + ":").grid(row=r, column=0, sticky="e", padx=(0, 8), pady=3)
                    add_field(box, nr_col, 5).pack(side=tk.LEFT)
                    ttk.Label(box, text=" of ").pack(side=tk.LEFT)
                add_field(box, total_col, 5).pack(side=tk.LEFT)
                r += 1

        unwritable = [x for x in rows if not tagwriter.can_write_track_info(x["filepath"])]
        if self.ipod_mountpoint:
            note = "Changes are saved into the files themselves and onto the connected iPod."
        else:
            note = "Changes are saved into the files themselves."
        if unwritable:
            note += (f"\n{len(unwritable)} file(s) are in a format whose tags can't be "
                     "written; those change in this library only.")
        ttk.Label(frm, text=note, foreground=apple_style.TERTIARY,
                  font=(apple_style.FAMILY, 9), wraplength=380, justify=tk.LEFT).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )
        r += 1

        button_row = ttk.Frame(frm)
        button_row.grid(row=r, column=0, columnspan=2, pady=(14, 0), sticky="e")

        def collect_edits():
            """{col: new value} for the fields the user actually changed."""
            edits = {}
            for col, (var, initial, _widget, mixed) in entries.items():
                if mixed:
                    continue
                text = var.get().strip()
                if text == initial.strip():
                    continue
                if col in self._INFO_NUMBER_COLUMNS:
                    if text and not text.isdigit():
                        raise ValueError(col)
                    edits[col] = int(text) if text else None
                else:
                    edits[col] = text or None
            if not episodes and (edits.get("genre") or "").lower() == "podcast":
                raise ValueError("genre")
            return edits

        def do_save(event=None):
            try:
                edits = collect_edits()
            except ValueError as e:
                if str(e) == "genre":
                    messagebox.showerror(
                        "Genre", "“Podcast” is reserved for podcast episodes. "
                                 "Subscribe to the show under File → Add Podcast instead.",
                        parent=dialog,
                    )
                else:
                    messagebox.showerror("Invalid Number",
                                         "Year, track and disc numbers must be whole numbers (or blank).",
                                         parent=dialog)
                return
            if not edits and not state["pending_art_bytes"]:
                dialog.destroy()
                return
            save_button.configure(state="disabled")
            cancel_button.configure(state="disabled")
            mountpoint = self.ipod_mountpoint
            device_tracks = list(self.device_tracks or []) if mountpoint else []
            device_links = dict(self.device_track_links)
            device_busy = self._active_sync_handle is not None

            def work():
                result = self._save_track_info(
                    rows, edits, state["pending_art_bytes"], state["pending_art_ext"],
                    mountpoint, device_tracks, device_busy, device_links,
                )
                self.work_queue.put(("track_info_saved", (dialog,) + result))

            threading.Thread(target=work, daemon=True).start()

        cancel_button = ttk.Button(button_row, text="Cancel", command=dialog.destroy)
        cancel_button.pack(side=tk.RIGHT, padx=(6, 0))
        save_button = ttk.Button(
            button_row, text="Save", command=do_save,
            style="Accent.TButton" if self.app_settings.get("itunes_theme", False) else "TButton",
        )
        save_button.pack(side=tk.RIGHT)
        dialog.bind("<Return>", do_save)
        dialog.bind("<Escape>", lambda e: dialog.destroy())

        dialog.update_idletasks()
        dialog.wait_visibility()
        dialog.grab_set()
        first_entry = next((e[2] for e in entries.values() if not e[3]), None)
        if first_entry is not None:
            first_entry.focus_set()
            if isinstance(first_entry, ttk.Entry):
                first_entry.select_range(0, tk.END)

    def _save_track_info(self, rows, edits, art_bytes, art_ext, mountpoint, device_tracks, device_busy,
                         device_links=None, per_row=None):
        """Worker-thread half of track_info_dialog. Returns (saved_count,
        warnings, device_updated, device_note)."""
        warnings = []
        device_updates = []
        # iPod copies of each song, by the exact links (see
        # Library.link_device_tracks).
        on_device = {}
        for t in device_tracks:
            tid = (device_links or {}).get(str(t["dbid"]))
            if tid is not None:
                on_device.setdefault(tid, []).append(t["dbid"])

        for row in rows:
            changes = dict(edits)
            changes.update((per_row or {}).get(row["id"], {}))
            if not changes and not art_bytes:
                continue
            if (row["genre"] or "") == "Podcast":
                changes.pop("genre", None)  # episodes stay in Podcasts
            # Track/disc numbers are one tag ("3/12"), so a changed half
            # goes out together with the other half's current value.
            for nr_col, total_col in (("track_nr", "track_total"), ("cd_nr", "cd_total")):
                if nr_col in changes or total_col in changes:
                    changes.setdefault(nr_col, row[nr_col])
                    changes.setdefault(total_col, row[total_col])
            name = os.path.basename(row["filepath"])
            if changes and tagwriter.can_write_track_info(row["filepath"]):
                ok, err = tagwriter.write_track_tags(row["filepath"], changes)
                if not ok:
                    warnings.append(f"{name}: {err}")
            if art_bytes:
                if tagwriter.can_write_tags(row["filepath"]):
                    ok, err = tagwriter.write_embedded_art(row["filepath"], art_bytes, art_ext)
                    if not ok:
                        warnings.append(f"{name} (artwork): {err}")
                else:
                    warnings.append(f"{name}: artwork can only be embedded in MP3/M4A files")
            self.lib.update_track_fields(row["id"], changes)

            if on_device.get(row["id"]):
                new = {c: row[c] for c in row.keys()}
                new.update(changes)
                for dbid in on_device[row["id"]]:
                    device_updates.append((dbid, new))

        device_updated = 0
        device_note = None
        if device_updates and device_busy:
            device_note = ("The iPod is busy syncing, so its copies weren't changed. "
                           "Edit them again once the sync finishes.")
        elif device_updates:
            art_path = None
            if art_bytes:
                fd, art_path = tempfile.mkstemp(prefix="ipod_art_", suffix=art_ext or ".jpg")
                with os.fdopen(fd, "wb") as f:
                    f.write(art_bytes)
            try:
                device_updated, errors = ipod_backend.update_tracks(mountpoint, device_updates, art_path)
                if errors:
                    device_note = "Couldn't update the iPod: " + "; ".join(errors[:3])
            except Exception as e:
                device_note = f"Couldn't update the iPod: {e}"
            finally:
                if art_path:
                    try:
                        os.remove(art_path)
                    except OSError:
                        pass
        return len(rows), warnings, device_updated, device_note

    def _refresh_after_track_edit(self):
        view = self.current_view
        if view == self.VIEW_ALBUM_TRACKS and self.current_album_key:
            if self.lib.tracks_by_album(*self.current_album_key):
                self.show_album(*self.current_album_key)
            else:
                self.show_podcasts() if self.library_mode == "podcasts" else self.show_album_grid()
        elif view == self.VIEW_LOCAL_PLAYLIST and self.current_local_playlist_id is not None:
            self.show_local_playlist(self.current_local_playlist_id)
        elif view == self.VIEW_ARTIST_TRACKS:
            self.show_tracks_for_artist(self.current_artist)
        elif view == self.VIEW_GENRE_TRACKS:
            self.show_tracks_for_genre(self.current_genre)
        elif view == self.VIEW_RECENTLY_ADDED:
            self.show_recently_added()
        elif view == self.VIEW_LIBRARY:
            self.show_library()

    def _set_preview_image_from_bytes(self, label, data):
        if not HAVE_PIL or data is None:
            return
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(data)).convert("RGB")
            img.thumbnail((120, 120))
            photo = ImageTk.PhotoImage(img)
        except Exception:
            return
        if label.winfo_exists():
            label.configure(image=photo)
            label.image = photo

    def _open_album_search_dialog(self, parent_dialog, artist, album, on_pick):
        search_dialog = tk.Toplevel(parent_dialog)
        search_dialog.title("Search Internet for Album Info")
        search_dialog.transient(parent_dialog)
        search_dialog.geometry("420x300")

        query_text = " ".join(p for p in (artist, album) if p).strip() or "(nothing entered)"
        ttk.Label(search_dialog, text=f"Searching for: {query_text}", wraplength=380).pack(
            padx=12, pady=(12, 4), anchor="w"
        )
        status_var = tk.StringVar(value="Searching\u2026")
        ttk.Label(search_dialog, textvariable=status_var, foreground="#666666").pack(
            padx=12, anchor="w"
        )

        results_frame = ttk.Frame(search_dialog, padding=(12, 8))
        results_frame.pack(fill=tk.BOTH, expand=True)
        listbox = tk.Listbox(results_frame)
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb = ttk.Scrollbar(results_frame, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.LEFT, fill=tk.Y)

        results_holder = {"results": []}

        def pick_current(event=None):
            sel = listbox.curselection()
            if not sel or not results_holder["results"]:
                return
            result = results_holder["results"][sel[0]]
            on_pick(result)
            search_dialog.destroy()

        listbox.bind("<Double-Button-1>", pick_current)

        button_row = ttk.Frame(search_dialog)
        button_row.pack(fill=tk.X, padx=12, pady=(0, 12))
        ttk.Button(button_row, text="Cancel", command=search_dialog.destroy).pack(
            side=tk.RIGHT, padx=(6, 0)
        )
        ttk.Button(button_row, text="Use Selected", command=pick_current).pack(side=tk.RIGHT)

        def work():
            try:
                results = lookup.search_albums(artist, album)
                self.work_queue.put(("album_search_results", (search_dialog, listbox, status_var, results_holder, results)))
            except lookup.LookupError as e:
                self.work_queue.put(("album_search_error", (search_dialog, status_var, str(e))))

        threading.Thread(target=work, daemon=True).start()

        search_dialog.update_idletasks()
        search_dialog.wait_visibility()
        search_dialog.grab_set()

    def _set_device_banner(self, show):
        # If the persistent device-view banner (see _show_body) is
        # already up, it already says "you're managing this iPod" -
        # showing this table-specific one too would just be a second,
        # redundant tan bar stacked directly underneath it.
        if show and not self._device_view_active:
            if not self.device_banner.winfo_ismapped():
                self.device_banner.pack(side=tk.TOP, fill=tk.X, before=self.table_row)
        else:
            self.device_banner.pack_forget()

    def _populate_table_local(self, rows):
        self._set_device_banner(False)
        self.table.delete(*self.table.get_children())
        for i, row in enumerate(rows):
            tag = "evenrow" if i % 2 == 0 else "oddrow"
            self.table.insert("", "end", iid=f"t:{row['id']}", tags=(tag,), values=(
                row["title"] or "", row["artist"] or "", row["album"] or "",
                row["genre"] or "", format_duration(row["duration_ms"]),
                row["track_nr"] or "", row["year"] or "",
                _row_get(row, "play_count") or "", format_last_played(_row_get(row, "last_played")),
            ))
        self._reapply_persistent_table_selection()

    def _populate_table_device(self, rows):
        self._set_device_banner(True)
        self.table.delete(*self.table.get_children())
        for i, row in enumerate(rows):
            tag = "device_evenrow" if i % 2 == 0 else "device_oddrow"
            self.table.insert("", "end", iid=f"d:{row['dbid']}", tags=(tag,), values=(
                row["title"] or "", row["artist"] or "", row["album"] or "",
                row.get("genre") or "", format_duration(row["duration_ms"]),
                row.get("track_nr") or "", row.get("year") or "",
                row.get("play_count") or "", format_last_played(row.get("last_played")),
            ))
        self._reapply_persistent_table_selection()

    def _refresh_play_count_cells(self, track_ids):
        """Updates just the Plays / Last Played cells of any of these
        local tracks currently shown, in place, so a play-count import
        doesn't reset scrolling or the selection."""
        for row in self.lib.tracks_by_ids(list(set(track_ids))):
            iid = f"t:{row['id']}"
            if self.table.exists(iid):
                self.table.set(iid, "plays", row["play_count"] or "")
                self.table.set(iid, "lastplayed", format_last_played(row["last_played"]))

    def _reapply_persistent_table_selection(self):
        """Restores whichever rows in self._persistent_table_selection
        still exist in the just-repopulated table - lets a selection
        made in one device-view screen (e.g. Songs) survive navigating
        elsewhere and back within it, without trying to carry over rows
        that plain aren't part of the newly shown set (they're
        silently skipped, not an error). Only applies inside the
        device view - see _load_device_view_selections / _clear_device_view_selections; the normal
        library table keeps its original behavior of starting
        unselected on every visit."""
        if not self._device_view_active:
            return
        to_reselect = [iid for iid in self._persistent_table_selection if self.table.exists(iid)]
        if to_reselect:
            self.table.selection_set(to_reselect)

    def _on_table_selection_changed(self, event=None):
        # Only update the part of the persistent set that's actually
        # relevant to what's shown right now - leave alone any selected
        # iid that belongs to a different subset (e.g. a different
        # local playlist) not currently visible. Replacing the whole
        # set with just self.table.selection() would silently drop
        # those the moment you navigate somewhere that doesn't include
        # them, since a row that doesn't exist here can't appear
        # "selected" no matter what was chosen elsewhere earlier.
        current_rows = set(self.table.get_children())
        currently_selected = set(self.table.selection())
        previous = self._persistent_table_selection
        self._persistent_table_selection = (self._persistent_table_selection - current_rows) | currently_selected
        if self._device_view_active:
            def local_ids(iids):
                return {int(i.split(":", 1)[1]) for i in iids if i.startswith("t:")}
            newly_unticked = local_ids(previous - self._persistent_table_selection)
            self._unticked_track_ids |= newly_unticked
            self._unticked_track_ids -= local_ids(self._persistent_table_selection - previous)
            self._untick_albums_of_songs(newly_unticked)
        # Durable sync profile, same reasoning as _persist_album_marks -
        # only local tracks ("t:" prefix) are worth remembering across
        # restarts; device dbids ("d:" prefix, used for "Remove from
        # iPod" selections) are tied to whatever's currently connected
        # and wouldn't mean anything on a later visit or a different
        # iPod, so those are deliberately left as session-only.
        if self._device_view_active:
            local_ids = [
                iid.split(":", 1)[1] for iid in self._persistent_table_selection if iid.startswith("t:")
            ]
            self.lib.set_marked_for_sync("track", local_ids)

    def sort_by(self, col):
        items = [(self.table.set(k, col), k) for k in self.table.get_children("")]
        if col == "plays":
            # Numeric, most-played first - as text "10" would sort before "9".
            items.sort(key=lambda it: -int(it[0]) if str(it[0]).isdigit() else 0)
        elif col == "lastplayed":
            items.sort(key=lambda it: _last_played_sort_key(it[0]))
        elif col in ("track", "year"):
            items.sort(key=lambda it: int(it[0]) if str(it[0]).isdigit() else 10**9)
        else:
            items.sort()
        for index, (_val, k) in enumerate(items):
            self.table.move(k, "", index)

    def _persist_track_marks(self):
        if self._device_view_active:
            self.lib.set_marked_for_sync("track", [
                iid.split(":", 1)[1] for iid in self._persistent_table_selection if iid.startswith("t:")
            ])

    def _untick_albums_of_songs(self, track_ids):
        """Songs unticked on the Songs screen untick their album too (the
        album's other songs stay ticked on their own)."""
        if not track_ids or not self._selected_albums:
            return
        gone = set()
        for key in self._selected_albums:
            if any(r["id"] in track_ids for r in self.lib.tracks_by_album(*key)):
                gone.add(key)
        if not gone:
            return
        self._selected_albums -= gone
        for k in gone:
            tile = self._album_tile_frames_by_key.get(k)
            if tile is not None and tile.winfo_exists():
                self._mark_tile(tile, False)
        self._persist_album_marks()

    def _persist_album_marks(self):
        """Writes the current album selection to disk as the durable
        sync profile - only meaningful while in the device view (see
        toggle_album_selection); called from every place that can
        change the selection there (individual clicks, Select All,
        Select None), not just the single-click toggle."""
        if self._device_view_active:
            self.lib.set_marked_for_sync("album", [_album_sync_key(a, ar) for a, ar in self._selected_albums])

    def select_all_tracks(self, event=None):
        if self.current_view in (self.VIEW_ALBUM_GRID, self.VIEW_PODCASTS):
            self._set_album_selection(self._selected_albums | set(self._album_tile_frames_by_key))
            self._persist_album_marks()
            return "break"
        if self.current_view in (self.VIEW_MOVIES, self.VIEW_TVSHOWS):
            self._selected_videos = set(self._video_tile_frames_by_id.keys())
            self._refresh_video_tile_selection_borders()
            n = len(self._selected_videos)
            if n:
                self.status_var.set(f"{n} video(s) selected.")
            self._persist_video_marks()
            return "break"
        if self.current_view == self.VIEW_PHOTOS:
            self._selected_photos = set(self._photo_tile_frames_by_id.keys())
            self._refresh_photo_tile_selection_borders()
            n = len(self._selected_photos)
            if n:
                self.status_var.set(f"{n} photo(s) selected.")
            self._persist_photo_marks()
            return "break"
        if self.current_view == self.VIEW_PLAYLIST_LIST:
            self._selected_playlist_ids = {r["id"] for r in self._playlist_list_rows}
            self._on_playlist_marks_changed()
            return "break"
        self.table.selection_set(self.table.get_children())
        return "break"

    def select_none_tracks(self, event=None):
        if self.current_view in (self.VIEW_ALBUM_GRID, self.VIEW_PODCASTS):
            self._set_album_selection(set())
            self._persist_album_marks()
            return "break"
        if self.current_view in (self.VIEW_MOVIES, self.VIEW_TVSHOWS):
            self._selected_videos = set()
            self._refresh_video_tile_selection_borders()
            self._persist_video_marks()
            return "break"
        if self.current_view == self.VIEW_PHOTOS:
            self._selected_photos = set()
            self._refresh_photo_tile_selection_borders()
            self._persist_photo_marks()
            return "break"
        if self.current_view == self.VIEW_PLAYLIST_LIST:
            self._selected_playlist_ids = set()
            self._on_playlist_marks_changed()
            return "break"
        self.table.selection_remove(self.table.selection())
        return "break"

    def on_search(self):
        self._schedule_header_subtitle()
        query = self.search_var.get().strip()
        if self.current_view in (self.VIEW_LIBRARY, self.VIEW_LOCAL_PLAYLIST):
            rows = self.lib.search_tracks(query) if query else self.lib.all_tracks(order_by=library.SONG_ORDER)
            self._populate_table_local(rows)

    # --------------------------------------------------------- library --

    def add_folder(self):
        folder = native_dialogs.choose_directory(title="Choose a music folder")
        if not folder:
            return
        self.lib.add_source_folder(folder)
        self._run_scan(folder)

    def refresh_library(self, silent=False, videos_only=False):
        """Refreshes everything this app tracks from disk in one pass -
        every added music folder, video (movie/TV show) folder, and
        photo folder alike - picking up new/changed files in each.
        Also flags any source folder that's gone missing (moved,
        renamed, or its drive/share isn't mounted right now) via a
        popup, so a folder silently going stale doesn't happen
        unnoticed.
        silent=True (used for the automatic refresh on startup) skips
        the "nothing to refresh yet" popup when no folders have been
        added at all, and is otherwise identical - a folder that IS
        missing, or a file that fails, still gets a popup either way,
        since that's exactly the kind of thing this feature exists to
        surface."""
        if self._refresh_in_progress:
            # Without this, the silent startup auto-refresh and a
            # manual Refresh Library click landing a moment later (or
            # two manual clicks) start two overlapping background
            # scans - each calls self.progress.start(10) on the same
            # shared progress bar, and the second start() orphans the
            # first one's still-running animation loop (a plain
            # .stop() afterward only ever cancels the most recently
            # started one) - the progress bar then bounces forever
            # with no way to stop it, even after both scans finish.
            if not silent:
                self.status_var.set("Already refreshing…")
            return

        # Video folders are only scanned (and converted) when asked for
        # explicitly - Settings > Video > Scan Video Folders Now - since
        # conversions are slow and a normal refresh shouldn't start them.
        if videos_only:
            music_folders, photo_folders, podcast_shows = [], [], []
            video_folders = self.lib.all_video_source_folders()  # [(path, kind), ...]
        else:
            music_folders = self.lib.all_source_folders()
            video_folders = []
            photo_folders = self.lib.all_photo_source_folders()  # [(path, album_name), ...]
            podcast_shows = [s for s in self.lib.all_podcast_shows() if s["auto_download"]]

        if not (music_folders or video_folders or photo_folders or podcast_shows):
            if videos_only and not silent:
                messagebox.showinfo("No Video Folders", "No video folders have been added yet.")
                return
            if not silent:
                messagebox.showinfo(
                    "Nothing to refresh",
                    "No folders have been added yet — use Add Music/Add Video/Add Photos "
                    "first. (Files added individually aren't tracked for refresh.)",
                )
            return

        # Video refresh needs the same prerequisites as adding video
        # does (ffmpeg/AtomicParsley, a converted-videos folder). Rather
        # than blocking the whole refresh (music/photos) on that, or
        # popping up the same setup dialogs add_video_folder() already
        # shows, a missing video prereq just skips the video part of
        # this refresh - and is called out by name in the results popup
        # below so it's never a silent skip.
        converted_folder = None
        video_prereqs_missing = None
        if video_folders:
            if not (video_converter.ffmpeg_available() and video_converter.atomicparsley_available()):
                video_prereqs_missing = "ffmpeg/AtomicParsley isn't installed"
            else:
                converted_folder = self.app_settings.get("converted_videos_folder", "")
                if not converted_folder:
                    video_prereqs_missing = "no converted-videos folder is set (Settings → Video)"

        total_folders = len(music_folders) + len(video_folders) + len(photo_folders) + len(podcast_shows)
        self.status_var.set(f"Refreshing library from {total_folders} folder(s)…")
        self._refresh_in_progress = True
        self._last_refresh_pruned = 0
        # Defensive: cancel any animation loop still running from
        # before (there shouldn't be one - the guard above is what
        # actually prevents this - but stopping before starting is
        # cheap insurance against the same "orphaned indeterminate
        # loop" problem from any other stray start() call).
        self.progress.stop()
        self.progress.configure(mode="indeterminate")
        self.progress.start(10)

        # Video folders can mean a real ffmpeg conversion running here
        # too, same as Add Video Folder/Add Video - wire up the Cancel
        # button the same way so it actually does something during a
        # refresh's conversions instead of sitting there disabled/inert.
        cancel_event = None
        if (video_folders and not video_prereqs_missing) or podcast_shows:
            cancel_event = threading.Event()
            self._active_video_add_cancel = cancel_event
            self._update_cancel_button_state()

        def work():
            total_added = 0
            total_found = 0
            all_errors = []       # [(path_or_folder, message), ...]
            missing_folders = []  # folders that no longer exist on disk
            cancelled = False
            try:
                # Every per-folder operation below already has its own
                # try/except, so this outer one is only ever a truly
                # unexpected crash - but it still has to reach
                # refresh_done no matter what (see the finally below),
                # or _refresh_in_progress never clears and every
                # future refresh is silently ignored (see the guard at
                # the top of refresh_library), on top of leaving the
                # progress bar animating with nothing left to stop it.
                for folder in music_folders:
                    if not os.path.isdir(folder):
                        missing_folders.append(folder)
                        continue
                    try:
                        count, found_total, errors = scanner.scan_folder(folder, self.lib)
                        total_added += count
                        total_found += found_total
                        all_errors.extend(errors)
                    except Exception as e:
                        all_errors.append((folder, f"{type(e).__name__}: {e}"))

                for folder, kind in video_folders:
                    if cancelled:
                        break
                    if not os.path.isdir(folder):
                        missing_folders.append(folder)
                        continue
                    if video_prereqs_missing:
                        continue
                    try:
                        added, skipped, errors, folder_cancelled = self._scan_and_convert_video_folder(
                            folder, kind, converted_folder, cancel_event=cancel_event,
                        )
                        total_added += added
                        total_found += added + skipped
                        all_errors.extend((folder, e) for e in errors)
                        cancelled = cancelled or folder_cancelled
                    except Exception as e:
                        all_errors.append((folder, f"{type(e).__name__}: {e}"))

                if not cancelled:
                    for folder, album_name in photo_folders:
                        if not os.path.isdir(folder):
                            missing_folders.append(folder)
                            continue
                        try:
                            added, errors = self._scan_photo_folder(folder, album_name)
                            total_added += added
                            total_found += added
                            all_errors.extend((folder, e) for e in errors)
                        except Exception as e:
                            all_errors.append((folder, f"{type(e).__name__}: {e}"))

                if not cancelled:
                    # Re-fetch each show's own row rather than reusing the
                    # snapshot taken before this thread started, so a show
                    # unsubscribed from (Settings -> Podcasts) while this
                    # refresh was already running is skipped instead of
                    # downloading into a subscription that no longer exists -
                    # the same "did this still exist by the time we got to
                    # it" check _scan_and_convert_video_folder does for
                    # tracked video folders via is_video_source_folder_tracked.
                    for show in podcast_shows:
                        if cancelled or cancel_event.is_set():
                            cancelled = True
                            break
                        current = self.lib.podcast_show(show["id"])
                        if not current or not current["auto_download"]:
                            continue
                        try:
                            added, skipped, ep_errors = self._download_podcast_show_episodes(
                                current, cancel_event=cancel_event,
                            )
                            total_added += added
                            total_found += added + skipped
                            all_errors.extend((current["name"], e) for e in ep_errors)
                        except Exception as e:
                            all_errors.append((show["name"], f"{type(e).__name__}: {e}"))
                # Songs whose files were deleted leave the library too.
                try:
                    gone = self.lib.prune_missing_tracks(music_folders)
                    self._last_refresh_pruned = len(gone)
                    if gone:
                        applog.log(f"REFRESH: removed {len(gone)} song(s) whose files were deleted")
                except Exception:
                    applog.log("REFRESH prune failed:\n" + traceback.format_exc())
            except Exception as e:
                applog.log("REFRESH WORKER CRASHED:\n" + traceback.format_exc())
                all_errors.append(("refresh", f"{type(e).__name__}: {e}"))
            finally:
                self.work_queue.put((
                    "refresh_done",
                    (total_added, total_found, all_errors, missing_folders, video_prereqs_missing, cancelled),
                ))

        threading.Thread(target=work, daemon=True).start()

    def clear_library(self):
        if not messagebox.askyesno(
            "Clear Library",
            "Remove everything from your local music library — all tracks and playlists?\n\n"
            "This only affects this app's library on your computer. It does NOT touch "
            "anything already synced to your iPod.",
        ):
            return
        try:
            self.lib.clear_library()
        except Exception as e:
            applog.log(f"CLEAR LIBRARY failed: {type(e).__name__}: {e}")
            messagebox.showerror("Clear Library failed", str(e))
            return
        applog.log("CLEAR LIBRARY: local library cleared")
        self.current_local_playlist_id = None
        self.current_album_key = None
        self.search_var.set("")
        self.refresh_sidebar()
        self.show_library()
        self.status_var.set("Library cleared.")

    def remove_duplicate_tracks(self):
        try:
            groups = self.lib.find_duplicate_groups()
        except Exception as e:
            messagebox.showerror("Remove Duplicates failed", str(e))
            return
        if not groups:
            messagebox.showinfo("No duplicates found", "No duplicate tracks were found in your library.")
            return

        total_extra = sum(len(g) - 1 for g in groups)
        sample_lines = [
            f"\u2022 {g[0]['title'] or '(untitled)'} \u2014 {g[0]['artist'] or 'Unknown Artist'} ({len(g)} copies)"
            for g in groups[:10]
        ]
        if len(groups) > 10:
            sample_lines.append(f"\u2026 and {len(groups) - 10} more")

        if not messagebox.askyesno(
            "Remove Duplicate Tracks",
            f"Found {len(groups)} song(s) that look duplicated (same title/artist/"
            f"album/length) \u2014 {total_extra} extra row(s) total:\n\n" + "\n".join(sample_lines) +
            "\n\nKeep the oldest copy of each and remove the rest? This only affects "
            "your local library, not your iPod.",
        ):
            return

        try:
            removed = self.lib.remove_duplicate_tracks()
        except Exception as e:
            messagebox.showerror("Remove Duplicates failed", str(e))
            return
        self.refresh_sidebar()
        if self.current_view != self.VIEW_SETTINGS:
            self.show_library()
        self.status_var.set(f"Removed {removed} duplicate track(s).")

    def add_files(self):
        paths = native_dialogs.choose_open_files(title="Choose audio files")
        if not paths:
            return
        errors = []
        added = 0
        for p in paths:
            try:
                meta = scanner.read_tags(p)
                # Picking a file by hand brings it back even if it was
                # removed from the library before.
                self.lib.forget_removed_track_file(meta.get("filepath") or p)
                self.lib.upsert_track(meta)
                added += 1
            except Exception as e:
                errors.append((p, f"{type(e).__name__}: {e}"))
        self.status_var.set(f"Added {added}/{len(paths)} file(s).")
        if errors:
            detail = "\n".join(f"{os.path.basename(p)}: {msg}" for p, msg in errors[:15])
            messagebox.showwarning("Some files failed to import", detail)
        self.show_library()

    def _run_scan(self, folder):
        self.status_var.set(f"Scanning {folder} …")
        self.progress.configure(mode="indeterminate")
        self.progress.start(10)

        def work():
            try:
                count, found_total, errors = scanner.scan_folder(folder, self.lib)
                self.work_queue.put(("scan_done", (count, found_total, errors)))
            except Exception as e:
                # Belt-and-suspenders: even an unexpected crash gets shown
                # in the GUI instead of dying silently in a background thread.
                self.work_queue.put(("scan_done", (0, 0, [(folder, f"{type(e).__name__}: {e}")])))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ podcasts --

    def _ask_podcast_download_settings(self, title, button_label, show_name, episode_count, initial_count, initial_all):
        """Small modal dialog offering a choice between 'keep the N
        most recent episodes downloaded' and 'download every episode
        automatically' - used both to subscribe to a new show
        (_confirm_and_subscribe_podcast) and to edit an already-
        subscribed one's settings later (Settings -> Podcasts' Edit
        button). Returns (count, download_all) on confirm - count is
        whatever whole number the spinbox holds, meaningless when
        download_all is True - or None if cancelled."""
        result = {"value": None}
        win = tk.Toplevel(self)
        win.title(title)
        win.transient(self)
        win.grab_set()
        win.resizable(False, False)

        form = ttk.Frame(win, padding=12)
        form.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            form, text=f"'{show_name}' has {episode_count} episode(s) in its feed.",
            wraplength=360, justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 10))

        mode_var = tk.StringVar(value="all" if initial_all else "recent")

        recent_row = ttk.Frame(form)
        recent_row.pack(fill=tk.X, anchor="w")
        ttk.Radiobutton(recent_row, text="Keep the", variable=mode_var, value="recent").pack(side=tk.LEFT)
        start_count = initial_count if initial_count is not None else (min(5, episode_count) or episode_count)
        count_var = tk.StringVar(value=str(start_count))
        ttk.Spinbox(
            recent_row, from_=0, to=max(episode_count, 1), width=5, textvariable=count_var,
        ).pack(side=tk.LEFT, padx=6)
        ttk.Label(recent_row, text="most recent episode(s) downloaded").pack(side=tk.LEFT)

        ttk.Radiobutton(
            form, text="Download every episode automatically (existing and new)",
            variable=mode_var, value="all",
        ).pack(anchor="w", pady=(8, 0))

        ttk.Label(
            form,
            text="Either way, new episodes are grabbed automatically every time "
                 "you Refresh Library.",
            foreground="#888888", wraplength=360, justify=tk.LEFT,
        ).pack(anchor="w", pady=(10, 0))

        button_row = ttk.Frame(form)
        button_row.pack(fill=tk.X, pady=(14, 0))

        def do_ok():
            if mode_var.get() == "all":
                result["value"] = (None, True)
                win.destroy()
                return
            text = count_var.get().strip()
            if not text.isdigit():
                messagebox.showerror("Invalid Number", "Enter a whole number of episodes to keep (0 or more).")
                return
            result["value"] = (int(text), False)
            win.destroy()

        ttk.Button(button_row, text="Cancel", command=win.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(button_row, text=button_label, command=do_ok).pack(side=tk.RIGHT)

        win.wait_window()
        return result["value"]

    def add_podcast_from_url(self):
        url = simpledialog.askstring(
            "Add Podcast from URL",
            "Paste an Apple Podcasts show link (podcasts.apple.com/…) or a "
            "direct podcast RSS feed URL:",
        )
        url = (url or "").strip()
        if not url:
            return
        if not self.app_settings.get("podcasts_folder"):
            messagebox.showinfo(
                "Choose a Podcasts Folder First",
                "Pick a folder for downloaded episodes in Settings → Podcasts first.",
            )
            self.open_settings()
            self._show_settings_category("podcasts")
            return

        self.status_var.set("Looking up that podcast…")
        applog.log(f"PODCAST: looking up {url}")

        def work():
            try:
                resolved = podcast.resolve_feed_url(url)
                feed = podcast.fetch_feed(resolved["feed_url"])
            except podcast.PodcastError as e:
                self.work_queue.put(("podcast_lookup_done", (None, str(e))))
                return
            show_name = resolved.get("show_name") or feed["show_name"]
            artwork_url = feed.get("artwork_url") or resolved.get("artwork_url")
            self.work_queue.put((
                "podcast_lookup_done",
                ({"feed_url": resolved["feed_url"], "show_name": show_name,
                  "episodes": feed["episodes"], "artwork_url": artwork_url}, None),
            ))

        threading.Thread(target=work, daemon=True).start()

    def _confirm_and_subscribe_podcast(self, result):
        """Runs on the main thread (from the podcast_lookup_done queue
        handler) - the feed's already been fetched in the background;
        this just asks how the show's episodes should be kept
        downloaded, subscribes, and kicks off the actual downloads
        (which also grabs the show's artwork on this first pass - see
        _download_podcast_show_episodes)."""
        feed_url = result["feed_url"]
        show_name = result["show_name"]
        episodes = result["episodes"]  # newest first, per podcast.fetch_feed
        default_count = min(self.app_settings.get("podcast_download_count", 5), len(episodes)) or len(episodes)
        choice = self._ask_podcast_download_settings(
            "Subscribe to Podcast", "Subscribe", show_name, len(episodes), default_count, False,
        )
        if choice is None:
            self.status_var.set("Not added.")
            return
        count, download_all = choice
        show_id = self.lib.add_podcast_show(
            show_name, feed_url, download_count=count, download_all=download_all,
        )
        applog.log(
            f"PODCAST: subscribed to '{show_name}' ({feed_url}), "
            + ("downloading every episode automatically" if download_all else f"keeping {count} episode(s)")
        )
        show_row = self.lib.podcast_show(show_id)
        self._download_new_podcast_episodes([show_row])

    def _download_podcast_show_episodes(self, show_row, cancel_event=None):
        """Runs in a background thread (called both from
        _download_new_podcast_episodes below and from refresh_library's
        own worker). Re-fetches show_row's feed fresh, figures out
        which episodes haven't been downloaded yet (matched by the
        feed's own <guid> - see podcast_episodes' schema, the same
        "stable identity across refreshes" role videos.source_path
        plays for video folders) - either the newest `keep_count` of
        them, or every one of them when the show's download_all flag
        is set - downloads those, and adds each one to the library as
        an ordinary track via scanner.read_tags/Library.upsert_track -
        from that point on a podcast episode IS a track, with
        everything that already works for music (Albums/Genres
        browsing, playlists, syncing, iPod-format checks) working for
        it too, for free. Also grabs the show's own artwork the first
        time it's called for a show that doesn't have it yet, into the
        show's episode folder, so it's picked up as folder-level cover
        art for every episode in it (see artwork.py).
        Returns (added_count, skipped_count, error_messages)."""
        show_id = show_row["id"]
        show_name = show_row["name"]
        download_all = bool(show_row["download_all"])
        keep_count = show_row["download_count"] or self.app_settings.get("podcast_download_count", 5)

        try:
            feed = podcast.fetch_feed(show_row["feed_url"])
        except podcast.PodcastError as e:
            return 0, 0, [f"{show_name}: {e}"]

        self.lib.set_podcast_show_last_checked(show_id, time.time())

        added = 0
        skipped = 0
        errors = []
        podcasts_folder = self.app_settings.get("podcasts_folder", "")
        show_folder = os.path.join(podcasts_folder, podcast.safe_filename(show_name))

        # Grab the show's artwork once, the first time this runs for it
        # (fresh subscribe, or an existing one that predates this
        # feature) - saved as cover.<ext> right in the show's own
        # episode folder, which artwork.py's folder-art fallback
        # already knows to look for next to every track in that
        # directory, so every episode (already-downloaded and future)
        # picks up real cover art with no per-file tag writes needed.
        if not show_row["artwork_path"] and feed.get("artwork_url"):
            art_path = podcast.download_show_artwork(feed["artwork_url"], show_folder)
            if art_path:
                self.lib.set_podcast_show_artwork(show_id, art_path)
                # _find_folder_art caches per-directory (see artwork.py) -
                # clear any stale "no art here" result from before this
                # download so the grid/sync pick the new file up right away.
                artwork._folder_art_cache.pop(show_folder, None)

        episodes_to_check = feed["episodes"] if download_all else feed["episodes"][:keep_count]

        for ep in episodes_to_check:
            if cancel_event is not None and cancel_event.is_set():
                break
            if self.lib.podcast_episode_by_guid(show_id, ep["guid"]):
                continue  # already downloaded in a previous check
            ext = podcast.guess_extension(ep["audio_url"], ep.get("mime_type"))
            dest_path = os.path.join(show_folder, f"{podcast.safe_filename(ep['title'])}{ext}")
            self.work_queue.put(("podcast_download_progress", f"Downloading {show_name}: {ep['title']}…"))

            def on_progress(done, total, _title=ep["title"]):
                pct = f" ({done * 100 // total}%)" if total else ""
                self.work_queue.put((
                    "podcast_download_progress", f"Downloading {show_name}: {_title}{pct}",
                ))

            try:
                podcast.download_episode(
                    ep["audio_url"], dest_path, on_progress=on_progress, cancel_event=cancel_event,
                )
            except podcast.DownloadCancelled:
                break
            except podcast.PodcastError as e:
                errors.append(f"{ep['title']}: {e}")
                skipped += 1
                continue

            try:
                meta = scanner.read_tags(dest_path)
                meta["title"] = meta["title"] or ep["title"]
                meta["artist"] = meta["artist"] or show_name
                meta["album"] = meta["album"] or show_name
                meta["albumartist"] = meta["albumartist"] or show_name
                # Always forced to exactly "Podcast" (not just a fallback
                # for a blank tag) - the Podcasts library view
                # (Library.all_podcast_albums) keys off this exact
                # value, and a lot of podcast MP3s carry their own
                # arbitrary genre tag (or none) that would otherwise
                # leave an episode misclassified there.
                meta["genre"] = "Podcast"
                if ep.get("episode_nr"):
                    meta["track_nr"] = meta["track_nr"] or ep["episode_nr"]
                track_id = self.lib.upsert_track(meta)
                self.lib.record_podcast_episode(show_id, ep["guid"], ep["title"], ep["pub_date"], track_id)
                added += 1
            except Exception as e:
                errors.append(f"{ep['title']}: {type(e).__name__}: {e}")
                skipped += 1

        return added, skipped, errors

    def _download_new_podcast_episodes(self, shows, silent=False):
        """The standalone entry point (after subscribing, or Settings
        -> Podcasts -> Check for New Episodes Now) - wires up the
        progress bar and Cancel button the same way Add Video Folder
        does, then runs _download_podcast_show_episodes for each show
        in a background thread. Not used by refresh_library, which
        does the equivalent inline in its own already-background
        work() so podcasts show up in that single combined summary
        instead of a separate popup."""
        if not shows:
            return
        self.status_var.set(f"Checking {len(shows)} podcast(s) for new episodes…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(10)
        cancel_event = threading.Event()
        self._active_video_add_cancel = cancel_event
        self._update_cancel_button_state()

        def work():
            total_added = 0
            total_skipped = 0
            all_errors = []
            for show in shows:
                if cancel_event.is_set():
                    break
                added, skipped, errors = self._download_podcast_show_episodes(show, cancel_event=cancel_event)
                total_added += added
                total_skipped += skipped
                all_errors.extend(errors)
            self.work_queue.put(("podcast_download_done", (total_added, total_skipped, all_errors)))

        threading.Thread(target=work, daemon=True).start()

    def _check_video_prereqs(self):
        """Shared by Add Video Folder and Add Video (single file) -
        both need the same tools/settings before doing anything.
        Returns the converted-videos folder path on success, or None
        (having already shown the relevant error) on failure."""
        if not video_converter.ffmpeg_available():
            messagebox.showerror(
                "ffmpeg Not Found",
                "This app needs ffmpeg and ffprobe installed to scan and convert video.\n\n"
                "Install it via your package manager (e.g. sudo apt install ffmpeg) and try again.",
            )
            return None

        if not video_converter.atomicparsley_available():
            messagebox.showerror(
                "AtomicParsley Not Found",
                "This app also needs AtomicParsley installed - classic iPods (5th/5.5-gen) "
                "won't actually display video without a special atom it adds to the file; "
                "without it, videos would sync but show a black screen.\n\n"
                "Install it via your package manager (e.g. sudo apt install atomicparsley) "
                "and try again.",
            )
            return None

        converted_folder = self.app_settings.get("converted_videos_folder", "")
        if not converted_folder:
            messagebox.showwarning(
                "Choose a Converted-Videos Folder First",
                "Before adding videos, choose a folder to save converted copies in:\n"
                "Edit \u2192 Settings \u2192 Video.",
            )
            return None
        return converted_folder

    def add_video_folder(self):
        if self.library_mode not in ("movies", "tvshows"):
            return
        kind = "movie" if self.library_mode == "movies" else "tvshow"
        folder = native_dialogs.choose_directory(
            title=f"Choose a folder of {'movies' if kind == 'movie' else 'TV show episodes'}"
        )
        if not folder:
            return

        show_name = None
        if kind == "tvshow":
            # Asked once for the whole folder, not guessed per-episode
            # from filenames (which often don't follow a parseable
            # "S01E02" pattern anyway) - this becomes every episode's
            # show_name and, on sync, the name of an on-device playlist
            # that groups the whole show together for easy browsing/
            # in-order playback on the iPod itself.
            show_name = simpledialog.askstring(
                "TV Show Name",
                "Name of this show (used to group its episodes into a "
                "playlist on the iPod):",
            )
            show_name = (show_name or "").strip() or None

        season_override = None
        if kind == "tvshow":
            # Optional - for a show with more than one season, this
            # forces every episode found in this folder onto the same
            # season number (handy for a folder that's all one
            # season, e.g. ".../Show/Season 02", where filenames don't
            # reliably encode "S02" themselves). Leaving it blank
            # falls back to whatever an .nfo or the filename itself
            # says for each episode, same as before this existed.
            season_text = simpledialog.askstring(
                "Season Number (optional)",
                "Season number for all episodes in this folder, if they're "
                "all from the same season (leave blank to detect per "
                "episode from filename/.nfo):",
            )
            season_text = (season_text or "").strip()
            if season_text:
                try:
                    season_override = int(season_text)
                except ValueError:
                    messagebox.showwarning(
                        "Not a Number",
                        f"\"{season_text}\" isn't a whole number - ignoring it and "
                        "detecting season per episode instead.",
                    )

        converted_folder = self._check_video_prereqs()
        if not converted_folder:
            return

        self.lib.add_video_source_folder(folder, kind)
        applog.log(
            f"VIDEO SCAN: requested for {folder} ({kind})"
            + (f" [show: {show_name}]" if show_name else "")
            + (f" [season: {season_override}]" if season_override is not None else "")
        )
        self.status_var.set(f"Scanning {folder} for video files\u2026")
        self.progress.configure(mode="indeterminate")
        self.progress.start(10)
        self.video_add_folder_button.configure(state="disabled")
        self.video_add_file_button.configure(state="disabled")
        cancel_event = threading.Event()
        self._active_video_add_cancel = cancel_event
        self._update_cancel_button_state()

        def work():
            try:
                added, skipped, errors, cancelled = self._scan_and_convert_video_folder(
                    folder, kind, converted_folder, show_name_override=show_name,
                    season_override=season_override, cancel_event=cancel_event,
                )
                self.work_queue.put(("video_scan_done", (added, skipped, errors, kind, cancelled)))
            except Exception as e:
                self.work_queue.put(("video_scan_done", (0, 0, [f"{type(e).__name__}: {e}"], kind, False)))

        threading.Thread(target=work, daemon=True).start()

    def add_video_file(self):
        if self.library_mode not in ("movies", "tvshows"):
            return
        kind = "movie" if self.library_mode == "movies" else "tvshow"
        filetypes = [
            ("Video files", " ".join(f"*{ext}" for ext in sorted(VIDEO_EXTENSIONS))),
            ("All files", "*.*"),
        ]
        filepath = native_dialogs.choose_open_file(
            title=f"Choose a {'movie' if kind == 'movie' else 'TV show episode'} file",
            filetypes=filetypes,
        )
        if not filepath:
            return

        show_name = None
        if kind == "tvshow":
            show_name = simpledialog.askstring(
                "TV Show Name",
                "Name of this show (used to group its episodes into a "
                "playlist on the iPod):",
            )
            show_name = (show_name or "").strip() or None

        season_override = None
        if kind == "tvshow":
            season_text = simpledialog.askstring(
                "Season Number (optional)",
                "Season number for this episode, if known (leave blank to "
                "detect from the filename/.nfo):",
            )
            season_text = (season_text or "").strip()
            if season_text:
                try:
                    season_override = int(season_text)
                except ValueError:
                    messagebox.showwarning(
                        "Not a Number",
                        f"\"{season_text}\" isn't a whole number - ignoring it and "
                        "detecting the season from the filename/.nfo instead.",
                    )

        converted_folder = self._check_video_prereqs()
        if not converted_folder:
            return

        name = os.path.basename(filepath)
        applog.log(
            f"VIDEO ADD: requested for {filepath} ({kind})"
            + (f" [show: {show_name}]" if show_name else "")
            + (f" [season: {season_override}]" if season_override is not None else "")
        )
        self.status_var.set(f"Adding {name}\u2026")
        self.progress.configure(mode="indeterminate")
        self.progress.start(10)
        self.video_add_folder_button.configure(state="disabled")
        self.video_add_file_button.configure(state="disabled")
        cancel_event = threading.Event()
        self._active_video_add_cancel = cancel_event
        self._update_cancel_button_state()

        def work():
            try:
                was_new = self._process_one_video_file(
                    filepath, kind, converted_folder,
                    show_name_override=show_name, season_override=season_override,
                    cancel_event=cancel_event,
                )
                # was_new is False when this exact file was already added in
                # a previous pass and its converted copy is still on disk -
                # nothing was (re)converted this time, so don't claim 1 was.
                self.work_queue.put(("video_scan_done", (1 if was_new else 0, 0, [], kind, False)))
            except video_converter.ConversionCancelled:
                self.work_queue.put(("video_scan_done", (0, 0, [], kind, True)))
            except video_converter.ConversionError as e:
                self.work_queue.put(("video_scan_done", (0, 1, [f"{name}: {e}"], kind, False)))
            except Exception as e:
                self.work_queue.put(("video_scan_done", (0, 0, [f"{type(e).__name__}: {e}"], kind, False)))

        threading.Thread(target=work, daemon=True).start()

    def _process_one_video_file(self, filepath, kind, converted_folder,
                                 show_name_override=None, season_override=None,
                                 cancel_event=None):
        """Converts (if needed) and adds a single video file to the
        library - the shared per-file logic behind both Add Video
        Folder (scanning many) and Add Video (picking just one).
        show_name_override, when given (TV shows only), is used as-is
        instead of trying to guess the show name from the filename -
        this also becomes the on-device playlist name these episodes
        get grouped into on sync (see ipod_backend.sync_videos), so
        every episode added under the same show name ends up in the
        same easy-to-find playlist on the iPod.
        season_override, when given (TV shows only), likewise wins
        over whatever an .nfo or the filename itself says for this
        episode's season number - for a show with multiple seasons
        added folder-by-folder (one season per folder/add), asking
        once up front is more reliable than trusting every filename to
        encode "S02" correctly.
        cancel_event, if given, is passed straight through to
        video_converter.convert()/ensure_ipod_atom() - see those for
        exactly when it takes effect (the Cancel button, see
        cancel_sync, sets it).
        Raises video_converter.ConversionError on failure (including
        video_converter.ConversionCancelled, a subclass, if cancelled);
        the caller decides how that gets reported.
        Returns True if it actually converted/copied and added this
        file, or False if it was already added in a previous pass and
        its converted copy is still sitting right there on disk - in
        which case none of that work happens again. This is what a
        rescan/refresh is for: picking up genuinely new files, not
        re-running ffmpeg and AtomicParsley on every already-processed
        video every single time (a real, slow waste of time, and the
        reason Refresh Library used to look like it was "converting
        videos that are already converted")."""
        name = os.path.basename(filepath)

        existing = self.lib.video_by_source_path(filepath)
        if existing and existing["filepath"] and os.path.exists(existing["filepath"]):
            self.work_queue.put(("video_scan_progress", f"Already added: {name}"))
            return False

        self.work_queue.put(("video_scan_progress", f"Checking {name}…"))

        episode_nfo = {}
        effective_show_name = None
        parsed_show_name = season_nr = episode_nr = episode_title = None
        if kind == "tvshow":
            # Worked out up front (same precedence as meta["show_name"]
            # below: explicit override > .nfo > filename guess) so the
            # destination subfolder name and the library's show_name
            # always agree, even when no override was typed in.
            parsed_show_name, season_nr, episode_nr, episode_title = _parse_tv_filename(name)
            episode_nfo = nfo.parse_episode_nfo(filepath) or {}
            effective_show_name = (
                show_name_override or episode_nfo.get("show_name") or parsed_show_name
            )

        # Episodes of the same show land together in their own
        # "<Show Name> Converted" subfolder of the converted-videos
        # folder, instead of every show dumping its converted files
        # into one flat folder - movies (no show name) still go
        # straight into the converted-videos folder as before.
        dest_folder = converted_folder
        if kind == "tvshow" and effective_show_name:
            # convert()/ensure_ipod_atom() below both create output_dir
            # themselves if it doesn't exist yet.
            safe_name = _safe_folder_name(effective_show_name)
            dest_folder = os.path.join(converted_folder, f"{safe_name} Converted")

        if video_converter.needs_conversion(filepath):
            self.work_queue.put(("video_scan_progress", f"Converting {name}…"))
            final_path = video_converter.convert(
                filepath, dest_folder,
                on_progress=lambda frac, n=name: self.work_queue.put(
                    ("video_scan_progress", f"Converting {n} ({frac * 100:.0f}%)")
                ),
                cancel_event=cancel_event,
            )
            was_converted = 1
        else:
            # Codec/profile/resolution already look fine, but that
            # alone doesn't guarantee the iPod atom is present - copy
            # (never modify the original) and make sure it is.
            self.work_queue.put(("video_scan_progress", f"Copying {name}…"))
            final_path = video_converter.ensure_ipod_atom(filepath, dest_folder, cancel_event=cancel_event)
            was_converted = 0

        info = video_converter.probe(final_path) or {}
        try:
            size_bytes = os.path.getsize(final_path)
        except OSError:
            size_bytes = 0

        meta = {
            "filepath": final_path,
            "source_path": filepath,
            "title": os.path.splitext(name)[0],
            "kind": kind,
            "show_name": None,
            "season_nr": None,
            "episode_nr": None,
            "duration_ms": info.get("duration_ms", 0),
            "size_bytes": size_bytes,
            "was_converted": was_converted,
            "show_fanart_path": None,
        }
        if kind == "movie":
            # A Kodi-style <basename>.nfo next to the source file, if
            # present, overrides the filename-derived title - .nfo
            # metadata was deliberately scraped/entered by the user (or
            # whatever tool produced it), so it's more trustworthy than
            # guessing from a filename. Best-effort: no .nfo, or one
            # that can't be read as expected, just leaves the title as
            # it already was.
            movie_nfo = nfo.parse_movie_nfo(filepath)
            if movie_nfo and movie_nfo.get("title"):
                meta["title"] = movie_nfo["title"]
        else:
            # The user-supplied show name (asked for once per folder/
            # file add) always wins, since it's what becomes the
            # on-device playlist name and every episode of the same
            # show needs to agree on it exactly to land in the same
            # playlist. Failing that, an .nfo's <showtitle> is more
            # reliable than guessing from the filename. (Computed
            # above, before conversion, so the destination subfolder
            # name matches this exactly.)
            meta["show_name"] = effective_show_name
            meta["season_nr"] = (
                season_override if season_override is not None
                else episode_nfo.get("season_nr", season_nr)
            )
            meta["episode_nr"] = episode_nfo.get("episode_nr", episode_nr)
            meta["title"] = episode_nfo.get("title") or episode_title or meta["title"]
            # Fan art for the *show* (not this one episode) - used as
            # the show's group-tile/playlist thumbnail in the TV Shows
            # grid (see _show_tvshow_group_grid), falling back to the
            # ordinary extracted-frame thumbnail below when none is
            # found.
            meta["show_fanart_path"] = nfo.find_show_fanart(filepath)

        thumb_dir = os.path.join(applog.LOG_DIR, "video_thumbnails")
        # A stable, collision-resistant filename derived from the
        # final file's own path, so re-adding the same video reuses
        # (and overwrites) the same thumbnail rather than accumulating
        # a new one every time.
        thumb_name = str(abs(hash(final_path))) + ".jpg"
        thumb_path = os.path.join(thumb_dir, thumb_name)
        meta["thumbnail_path"] = thumb_path if video_converter.extract_thumbnail(final_path, thumb_path) else None

        self.lib.upsert_video(meta)
        # Only reachable here if a folder scan chose to process this
        # file anyway (it won't, if is_video_source_deleted said yes -
        # see _scan_and_convert_video_folder) or, more to the point,
        # this was an explicit single-file Add Video pointed at exactly
        # this file - either way, it's back in the library on purpose
        # now, so any stale "don't re-add this" tombstone no longer
        # applies.
        self.lib.unmark_video_source_deleted(filepath)
        return True

    def _scan_and_convert_video_folder(self, folder, kind, converted_folder,
                                        show_name_override=None, season_override=None,
                                        cancel_event=None):
        """Runs in a background thread. Walks folder for video files;
        anything not already iPod-compatible gets converted into
        converted_folder first. Returns (added_count, skipped_count,
        error_messages, cancelled). added_count only counts files that
        were actually newly converted/copied this pass - a file this
        folder was already scanned into the library before, whose
        converted copy is still on disk, is left alone entirely (see
        _process_one_video_file) rather than being reprocessed on
        every single refresh. A per-file conversion failure is skipped
        (logged as an error) rather than aborting the whole scan;
        cancel_event being set (see cancel_sync) stops the scan after
        whatever file is currently converting/copying finishes its own
        cancellation, without treating the remaining not-yet-processed
        files as failures.
        Also stops (cleanly, not as a cancellation) if `folder` itself
        gets removed from the tracked folders list (Settings -> Video
        -> Tracked Video Folders -> Remove) partway through - the
        folder list a refresh/scan started with is only ever captured
        once, up front, so without this check, removing a folder
        while its own conversion is still slowly working through a
        long list of files wouldn't actually stop anything until it
        finished on its own. This is deliberately NOT reported back as
        `cancelled` - that flag means the Cancel button was used and
        makes refresh_library stop every remaining folder too, which
        would defeat the point: removing one folder you've changed
        your mind about shouldn't interrupt every other folder still
        being refreshed normally in the same pass."""
        added = 0
        skipped = 0
        errors = []
        cancelled = False
        for root, _dirs, files in os.walk(folder):
            if cancelled:
                break
            if not self.lib.is_video_source_folder_tracked(folder):
                break
            for name in sorted(files):
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                if not self.lib.is_video_source_folder_tracked(folder):
                    break
                ext = os.path.splitext(name)[1].lower()
                if ext not in VIDEO_EXTENSIONS:
                    continue
                filepath = os.path.join(root, name)
                if self.lib.is_video_source_deleted(filepath):
                    # Previously removed via "Remove from Library" -
                    # the original file was deliberately left in place
                    # on disk (see remove_selected_videos), but that
                    # means it's still sitting right here in a tracked
                    # folder; skip it instead of silently re-adding/
                    # re-converting something the person just deleted.
                    # An explicit single-file Add Video for this exact
                    # file still works and clears this (see
                    # _process_one_video_file).
                    continue
                try:
                    was_new = self._process_one_video_file(
                        filepath, kind, converted_folder,
                        show_name_override=show_name_override,
                        season_override=season_override,
                        cancel_event=cancel_event,
                    )
                    if was_new:
                        added += 1
                except video_converter.ConversionCancelled:
                    cancelled = True
                    break
                except video_converter.ConversionError as e:
                    errors.append(f"{name}: {e}")
                    skipped += 1
        return added, skipped, errors, cancelled

    def sync_selected_videos(self):
        if not self.ipod_mountpoint:
            messagebox.showwarning("No iPod", "Detect or choose an iPod folder first (iPod menu).")
            return
        if not self._selected_videos:
            messagebox.showinfo("Nothing selected", "Select one or more videos first.")
            return
        video_ids = self._expand_video_selection(self._selected_videos)
        rows = self.lib.videos_by_ids(video_ids)
        if not rows:
            return

        self.progress.configure(mode="determinate", maximum=max(len(rows), 1), value=0)
        self.status_var.set(f"Syncing {len(rows)} video(s) to iPod\u2026")
        self._active_sync_handle = ipod_backend.SyncHandle()
        self.video_sync_button.configure(state="disabled")
        self._update_cancel_button_state()

        def on_progress(current, total, title):
            self.work_queue.put(("sync_progress", (current, total, title)))

        def work():
            try:
                added, errors, cancelled = ipod_backend.sync_videos(
                    self.ipod_mountpoint, rows, on_progress=on_progress, handle=self._active_sync_handle,
                )
                self.work_queue.put(("video_sync_done", (added, errors, cancelled)))
            except ipod_backend.IpodError as e:
                self.work_queue.put(("error", str(e)))
            except Exception:
                # Anything other than IpodError used to just kill this
                # background thread silently - no dialog, no log line,
                # the progress bar and buttons left stuck as they were
                # mid-sync. That's indistinguishable from "syncing just
                # stopped working" with nothing to go on. Catch broadly
                # here, log the real traceback so a real cause (e.g. a
                # non-ASCII filename/title tripping something not
                # expecting it) is actually visible, and still let the
                # UI recover via the normal "error" handler.
                applog.log("SYNC WORKER CRASHED:\n" + traceback.format_exc())
                self.work_queue.put((
                    "error",
                    "Something went wrong during this operation (see the log file "
                    "for details). It's been logged so it can be tracked down.",
                ))

        threading.Thread(target=work, daemon=True).start()

    def add_photo_folder(self):
        folder = native_dialogs.choose_directory(title="Choose a folder of photos")
        if not folder:
            return

        album_name = simpledialog.askstring(
            "Album Name (optional)",
            "Put these photos in an on-device album, in addition to the main "
            "Photo Library (leave blank for just the main library):",
        )
        album_name = (album_name or "").strip()

        self.lib.add_photo_source_folder(folder, album_name)
        applog.log(f"PHOTO SCAN: requested for {folder}" + (f" (album: {album_name})" if album_name else ""))
        self.status_var.set(f"Scanning {folder} for photos\u2026")
        self.progress.configure(mode="indeterminate")
        self.progress.start(10)
        self.photo_add_folder_button.configure(state="disabled")

        def work():
            try:
                added, errors = self._scan_photo_folder(folder, album_name)
                self.work_queue.put(("photo_scan_done", (added, errors)))
            except Exception as e:
                self.work_queue.put(("photo_scan_done", (0, [f"{type(e).__name__}: {e}"])))

        threading.Thread(target=work, daemon=True).start()

    def _scan_photo_folder(self, folder, album_name):
        """Runs in a background thread. No conversion step needed for
        photos (unlike video) - libgpod's own itdb_photodb_add_photo()
        handles format/thumbnail generation internally on-device.
        Generates a local thumbnail (a resized copy) purely for this
        app's own grid view. Returns (added_count, error_messages)."""
        added = 0
        errors = []
        thumb_dir = os.path.join(applog.LOG_DIR, "photo_thumbnails")
        for root, _dirs, files in os.walk(folder):
            for name in sorted(files):
                ext = os.path.splitext(name)[1].lower()
                if ext not in PHOTO_EXTENSIONS:
                    continue
                filepath = os.path.join(root, name)
                self.work_queue.put(("photo_scan_progress", name))

                try:
                    size_bytes = os.path.getsize(filepath)
                except OSError:
                    size_bytes = 0

                thumb_name = str(abs(hash(filepath))) + ".jpg"
                thumb_path = os.path.join(thumb_dir, thumb_name)
                thumb_ok = False
                if HAVE_PIL:
                    try:
                        from PIL import Image
                        os.makedirs(thumb_dir, exist_ok=True)
                        img = Image.open(filepath)
                        img.thumbnail((220, 220))
                        if img.mode != "RGB":
                            img = img.convert("RGB")
                        img.save(thumb_path, "JPEG", quality=85)
                        thumb_ok = True
                    except Exception as e:
                        errors.append(f"{name}: couldn't generate a thumbnail ({e})")

                self.lib.upsert_photo({
                    "filepath": filepath,
                    "source_path": filepath,
                    "album_name": album_name or None,
                    "thumbnail_path": thumb_path if thumb_ok else None,
                    "size_bytes": size_bytes,
                })
                added += 1
        return added, errors

    def sync_selected_photos(self):
        if not self.ipod_mountpoint:
            messagebox.showwarning("No iPod", "Detect or choose an iPod folder first (iPod menu).")
            return
        if not self._selected_photos:
            messagebox.showinfo("Nothing selected", "Select one or more photos first.")
            return
        rows = self.lib.photos_by_ids(list(self._selected_photos))
        if not rows:
            return

        self._photo_dedupe_only = False
        self.progress.configure(mode="determinate", maximum=max(len(rows), 1), value=0)
        self.status_var.set(f"Syncing {len(rows)} photo(s) to iPod\u2026")
        self._active_sync_handle = ipod_backend.SyncHandle()
        self.photo_sync_button.configure(state="disabled")
        self._update_cancel_button_state()

        def on_progress(current, total, title):
            self.work_queue.put(("sync_progress", (current, total, title)))

        def work():
            try:
                added, errors, cancelled, counts = ipod_backend.sync_photos(
                    self.ipod_mountpoint, rows, on_progress=on_progress, handle=self._active_sync_handle,
                )
                self.work_queue.put(("photo_sync_done", (added, errors, cancelled, counts)))
            except ipod_backend.IpodError as e:
                self.work_queue.put(("error", str(e)))
            except Exception:
                # Anything other than IpodError used to just kill this
                # background thread silently - no dialog, no log line,
                # the progress bar and buttons left stuck as they were
                # mid-sync. That's indistinguishable from "syncing just
                # stopped working" with nothing to go on. Catch broadly
                # here, log the real traceback so a real cause (e.g. a
                # non-ASCII filename/title tripping something not
                # expecting it) is actually visible, and still let the
                # UI recover via the normal "error" handler.
                applog.log("SYNC WORKER CRASHED:\n" + traceback.format_exc())
                self.work_queue.put((
                    "error",
                    "Something went wrong during this operation (see the log file "
                    "for details). It's been logged so it can be tracked down.",
                ))

        threading.Thread(target=work, daemon=True).start()

    def remove_duplicate_photos(self):
        if not self.ipod_mountpoint:
            messagebox.showwarning("No iPod", "Detect or choose an iPod folder first (iPod menu).")
            return
        if self._active_sync_handle is not None:
            messagebox.showinfo("Busy", "Wait for the current sync to finish first.")
            return
        self.status_var.set("Looking for duplicate photos on the iPod\u2026")
        self._photo_dedupe_only = True
        self._active_sync_handle = ipod_backend.SyncHandle()
        self.photo_sync_button.configure(state="disabled")
        self._update_cancel_button_state()

        def work():
            try:
                result = ipod_backend.sync_photos(self.ipod_mountpoint, [], handle=self._active_sync_handle)
                self.work_queue.put(("photo_sync_done", result))
            except Exception as e:
                applog.log("PHOTO DEDUPE failed:\n" + traceback.format_exc())
                self.work_queue.put(("error", f"Couldn't check the iPod's photos: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def new_local_playlist(self):
        name = simpledialog.askstring("New Playlist", "Playlist name:")
        if not name:
            return
        pl_id = self.lib.create_playlist(name)
        applog.log(f"PLAYLIST: created '{name}'")
        sel = self.table.selection()
        if sel and messagebox.askyesno("Add tracks", "Add the currently selected tracks to this playlist?"):
            track_ids = [int(iid.split(":", 1)[1]) for iid in sel if iid.startswith("t:")]
            self.lib.add_to_playlist(pl_id, track_ids)
            applog.log(f"PLAYLIST: added {len(track_ids)} track(s) to '{name}'")
        self.refresh_sidebar()

    def on_right_click(self, event):
        iid = self.table.identify_row(event.y)
        if iid and iid not in self.table.selection():
            self.table.selection_set(iid)

        menu = tk.Menu(self, tearoff=0)
        if self.current_view in self.LOCAL_TRACK_VIEWS:
            menu.add_command(label="Get Info…", accelerator="Ctrl+I", command=self.get_info_for_selection)
            menu.add_separator()
            menu.add_command(label="Add to Playlist…", command=self.add_selected_to_playlist)
            menu.add_command(
                label="Add to Last Selected Playlist",
                command=self.add_selected_to_last_playlist,
                state="normal" if self._last_playlist_id is not None else "disabled",
            )
            menu.add_command(label="Sync Selected \u2192 iPod", command=self.sync_selected)
            menu.add_command(
                label="Remove from iPod", command=self.remove_selected_from_device,
                state="normal" if self.ipod_mountpoint else "disabled",
            )
            if self.current_view == self.VIEW_LOCAL_PLAYLIST:
                menu.add_command(label="Remove from This Playlist", command=self.remove_from_local_playlist)
        else:
            menu.add_command(label="Remove from iPod", command=self.remove_selected_from_device)
        menu.tk_popup(event.x_root, event.y_root)

    def add_selected_to_playlist(self):
        sel = [iid for iid in self.table.selection() if iid.startswith("t:")]
        if not sel:
            return
        track_ids = [int(iid.split(":", 1)[1]) for iid in sel]
        self._add_track_ids_to_playlist_picker(track_ids)

    def add_selected_to_last_playlist(self):
        sel = [iid for iid in self.table.selection() if iid.startswith("t:")]
        if not sel:
            return
        track_ids = [int(iid.split(":", 1)[1]) for iid in sel]
        self._add_track_ids_to_last_playlist(track_ids)

    def _choose_playlist_dialog(self):
        """A small listbox picker for an existing playlist. Returns the
        chosen playlist id, or None if there are no playlists or the
        user cancelled."""
        playlists = self.lib.all_playlists()
        if not playlists:
            messagebox.showinfo("No playlists", "Create a playlist first (New Playlist).")
            return None

        result = {"id": None}
        dialog = tk.Toplevel(self)
        dialog.title("Add to Playlist")
        dialog.transient(self)
        dialog.resizable(False, False)

        ttk.Label(dialog, text="Choose a playlist:").pack(padx=12, pady=(12, 4), anchor="w")
        listbox = tk.Listbox(dialog, height=min(8, len(playlists)), exportselection=False, width=32)
        for p in playlists:
            listbox.insert(tk.END, p["name"])
        listbox.selection_set(0)
        listbox.pack(padx=12, fill=tk.BOTH, expand=True)

        def confirm(_event=None):
            sel = listbox.curselection()
            if sel:
                result["id"] = playlists[sel[0]]["id"]
            dialog.destroy()

        listbox.bind("<Double-Button-1>", confirm)

        button_row = ttk.Frame(dialog)
        button_row.pack(fill=tk.X, padx=12, pady=(8, 12))
        ttk.Button(button_row, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(button_row, text="Add", command=confirm).pack(side=tk.RIGHT)

        # Same grab_set() timing fix as the album-edit dialog: the window
        # has to actually be visible before it can take the grab.
        dialog.update_idletasks()
        dialog.wait_visibility()
        dialog.grab_set()
        dialog.wait_window()
        return result["id"]

    def _add_track_ids_to_playlist_picker(self, track_ids):
        if not track_ids:
            return
        pl_id = self._choose_playlist_dialog()
        if pl_id is None:
            return
        self._last_playlist_id = pl_id
        self._add_track_ids_to_playlist(track_ids, pl_id)

    def _add_track_ids_to_last_playlist(self, track_ids):
        if not track_ids:
            return
        if self._last_playlist_id is None:
            messagebox.showinfo(
                "No recent playlist",
                "Use \u201cAdd to Playlist\u2026\u201d once first, then this will remember it.",
            )
            return
        pl = next((p for p in self.lib.all_playlists() if p["id"] == self._last_playlist_id), None)
        if pl is None:
            messagebox.showinfo("Playlist not found", "The last-used playlist no longer exists.")
            self._last_playlist_id = None
            return
        self._add_track_ids_to_playlist(track_ids, self._last_playlist_id)

    def _add_track_ids_to_playlist(self, track_ids, pl_id):
        self.lib.add_to_playlist(pl_id, track_ids)
        pl = next((p for p in self.lib.all_playlists() if p["id"] == pl_id), None)
        pl_name = pl["name"] if pl else "the playlist"
        applog.log(f"PLAYLIST: added {len(track_ids)} track(s) to '{pl_name}'")
        self.status_var.set(f"Added {len(track_ids)} track(s) to '{pl_name}'.")
        if self.current_view == self.VIEW_LOCAL_PLAYLIST and self.current_local_playlist_id == pl_id:
            self.show_local_playlist(pl_id)

    def remove_from_local_playlist(self):
        sel = [iid for iid in self.table.selection() if iid.startswith("t:")]
        if not sel or self.current_local_playlist_id is None:
            return
        track_ids = [int(iid.split(":", 1)[1]) for iid in sel]
        self.lib.remove_from_playlist(self.current_local_playlist_id, track_ids)
        self.show_local_playlist(self.current_local_playlist_id)

    # ------------------------------------------------------------ iPod --

    def auto_detect_ipod(self):
        """Manual 'Detect / Auto-Mount iPod' trigger — searches, and if
        nothing's mounted yet, tries to auto-mount a connected one, right
        away (rather than waiting for the next background poll)."""
        self._suppress_automount = False
        self.status_var.set("Looking for an iPod…")

        def work():
            mounts, message = ipod_backend.find_or_mount_ipod()
            self.work_queue.put(("ipod_scan_result", (mounts, message, True)))

        threading.Thread(target=work, daemon=True).start()

    def eject_ipod(self):
        if not self.ipod_mountpoint:
            messagebox.showinfo("No iPod", "No iPod is currently connected.")
            return
        if not messagebox.askyesno(
            "Eject iPod",
            f"Eject {self.ipod_label or 'the iPod'}? Make sure any sync has finished first.",
        ):
            return
        mountpoint = self.ipod_mountpoint
        self.status_var.set("Ejecting iPod…")

        def work():
            success, message = ipod_backend.eject_ipod(mountpoint)
            self.work_queue.put(("eject_done", (success, message)))

        threading.Thread(target=work, daemon=True).start()

    def _detect_ipod_on_startup(self):
        """Runs once, shortly after the window opens (see __init__) -
        reuses _poll_for_ipod_once()'s "look for/auto-mount an iPod"
        logic for a single check, quietly, with no popup if nothing's
        connected (was_manual=False in the ipod_scan_result handler).
        Deliberately does not reschedule itself - this is not the
        continuous background poll (_poll_for_ipod) that was reverted
        earlier; it only catches the case where an iPod is already
        mounted by the time the app starts."""
        try:
            self._poll_for_ipod_once()
        except Exception as e:
            applog.log(f"IPOD STARTUP DETECT error: {type(e).__name__}: {e}")

    def _start_mount_watcher(self):
        """Starts the background thread that notices when the system
        mounts or unmounts something (see __init__ and
        ipod_backend.wait_for_mount_table_change). It never touches
        Tk state itself - a background thread doing that is a recipe
        for the kind of hard-to-reproduce crash this app has hit
        before - it only ever puts a message on work_queue, the same
        thread-safe hand-off every other background operation in this
        app already uses; _poll_queue (on the main thread) picks it up
        from there."""
        def watch():
            while True:
                try:
                    changed = ipod_backend.wait_for_mount_table_change(timeout=None)
                except Exception as e:
                    applog.log(f"MOUNT WATCH error (stopping): {type(e).__name__}: {e}")
                    return
                if changed:
                    self.work_queue.put(("mount_table_changed", None))
                else:
                    # wait_for_mount_table_change only returns False
                    # when it can't watch at all (no /proc/mounts) -
                    # looping on that immediately would just spin, so
                    # stop instead. A real, supported wait either
                    # blocks until a change or blocks forever; it
                    # doesn't time out on its own here (timeout=None).
                    return

        threading.Thread(target=watch, daemon=True).start()

    def _on_mount_table_changed(self):
        """Queue handler for a "mount_table_changed" message from the
        watcher thread above - debounced by _mount_change_debounce_id
        so several mounts landing in a quick burst (e.g. every
        partition of a multi-partition device showing up within
        milliseconds of each other) collapse into a single check
        instead of running one per event."""
        if self._mount_change_debounce_id is not None:
            self.after_cancel(self._mount_change_debounce_id)
        self._mount_change_debounce_id = self.after(500, self._run_debounced_mount_check)

    def _run_debounced_mount_check(self):
        self._mount_change_debounce_id = None
        self._poll_for_ipod_once()

    def _poll_for_ipod(self):
        """Runs continuously in the background via self.after(); see
        _poll_for_ipod_once() for what it actually does each cycle.
        This wrapper's only job is making sure that cycle's work can
        never stop the polling loop itself - without the try/finally,
        any unexpected exception in a single cycle (a surprise USB
        removal can trigger some genuinely odd, hard-to-predict I/O
        errors, and refresh_sidebar() below touches a lot of state)
        would mean self.after(4000, self._poll_for_ipod) never gets
        called again, silently ending auto-detection for the rest of
        the session with no visible sign why - exactly the "only shows
        up once, a reconnect after that does nothing" symptom."""
        try:
            self._poll_for_ipod_once()
        except Exception as e:
            applog.log(f"IPOD POLL error (continuing): {type(e).__name__}: {e}")
        finally:
            self.after(4000, self._poll_for_ipod)

    def _poll_for_ipod_once(self):
        """Notices when a connected iPod disappears, and tries to
        auto-mount a newly plugged-in one without the user having to
        ask. Stays quiet after an explicit Eject until the device is
        actually unplugged, so it doesn't just remount the same iPod a
        few seconds later."""
        if self.ipod_mountpoint:
            if not os.path.isdir(self.ipod_mountpoint):
                applog.log(f"DISCONNECT: {self.ipod_label} at {self.ipod_mountpoint} went away")
                self.ipod_mountpoint = None
                self.ipod_label = None
                self.device_tracks = []
                self.device_movies = []
                self.device_tvshows = []
                self.device_photo_albums = []
                self.device_playlists = []
                self.refresh_sidebar()
                self.status_var.set("iPod disconnected.")
        elif self._suppress_automount:
            def work():
                try:
                    present = ipod_backend.is_ipod_physically_present()
                except Exception as e:
                    applog.log(f"IPOD PRESENCE CHECK error: {type(e).__name__}: {e}")
                    return
                self.work_queue.put(("ipod_presence_check", present))

            threading.Thread(target=work, daemon=True).start()
        else:
            def work():
                try:
                    mounts, message = ipod_backend.find_or_mount_ipod()
                except Exception as e:
                    applog.log(f"IPOD AUTO-MOUNT error: {type(e).__name__}: {e}")
                    return
                self.work_queue.put(("ipod_scan_result", (mounts, message, False)))

            threading.Thread(target=work, daemon=True).start()

    def choose_ipod_manually(self):
        folder = native_dialogs.choose_directory(title="Select the iPod's mounted volume")
        if not folder:
            return
        self._set_device({"mountpoint": folder, "label": os.path.basename(folder), "free_bytes": None})

    def _set_device(self, mount_info):
        self.ipod_mountpoint = mount_info["mountpoint"]
        self.ipod_label = mount_info["label"]
        self._auto_synced_this_connection = False
        free = mount_info.get("free_bytes")
        applog.log(
            f"CONNECT: {self.ipod_label} at {self.ipod_mountpoint}"
            + (f" ({format_bytes(free)} free)" if free is not None else "")
        )
        self.status_var.set(
            f"iPod connected: {self.ipod_label} at {self.ipod_mountpoint}"
            + (f" ({format_bytes(free)} free)" if free is not None else "")
        )
        self.refresh_device_library()

    def refresh_device_library(self):
        if not self.ipod_mountpoint:
            messagebox.showinfo("No iPod", "Detect or choose an iPod folder first.")
            return
        self.status_var.set("Reading iPod library…")

        def work():
            try:
                tracks, playlists, errors, photo_albums = ipod_backend.read_device_library(self.ipod_mountpoint)
                # Pull the iPod's play counts back into the library on
                # every read (connect, and after every sync/remove) -
                # only plays since the last read are added, so this is
                # safe to repeat. See Library.import_ipod_play_counts.
                try:
                    play_import = self.lib.import_ipod_play_counts(tracks)
                except Exception:
                    applog.log("PLAY COUNTS import failed:\n" + traceback.format_exc())
                    play_import = (0, [])
                try:
                    links = self.lib.link_device_tracks(
                        [t for t in tracks if t["mediatype"] not in (0x02, 0x40)]
                    )
                except Exception:
                    applog.log("IPOD LINKS failed:\n" + traceback.format_exc())
                    links = {}
                self.work_queue.put(("device_read", (tracks, playlists, errors, photo_albums, play_import, links)))
            except ipod_backend.IpodError as e:
                self.work_queue.put(("error", str(e)))
            except Exception:
                # Anything other than IpodError used to just kill this
                # background thread silently - no dialog, no log line,
                # the progress bar and buttons left stuck as they were
                # mid-sync. That's indistinguishable from "syncing just
                # stopped working" with nothing to go on. Catch broadly
                # here, log the real traceback so a real cause (e.g. a
                # non-ASCII filename/title tripping something not
                # expecting it) is actually visible, and still let the
                # UI recover via the normal "error" handler.
                applog.log("SYNC WORKER CRASHED:\n" + traceback.format_exc())
                self.work_queue.put((
                    "error",
                    "Something went wrong during this operation (see the log file "
                    "for details). It's been logged so it can be tracked down.",
                ))

        threading.Thread(target=work, daemon=True).start()

    def sync_selected(self):
        if not self.ipod_mountpoint:
            messagebox.showwarning("No iPod", "Detect or choose an iPod folder first (iPod menu).")
            return

        if self.current_view in (self.VIEW_ALBUM_GRID, self.VIEW_PODCASTS):
            if not self._selected_albums:
                messagebox.showinfo(
                    "Nothing selected", "Click one or more album tiles to select them first."
                )
                return
            rows = []
            for album, artist in self._selected_albums:
                rows.extend(self.lib.tracks_by_album(album, artist))
        else:
            # In the device view, the durable sync profile can span
            # multiple screens (e.g. some tracks picked while viewing
            # Songs, others via a specific local playlist) - use the
            # full cross-view set so Sync actually syncs everything
            # marked, not just whatever happens to be visible in this
            # particular screen right now. Outside the device view,
            # there's no such profile concept - just sync whatever's
            # currently selected in the table, as before.
            if self._device_view_active:
                sel = [iid for iid in self._persistent_table_selection if iid.startswith("t:")]
            else:
                sel = [iid for iid in self.table.selection() if iid.startswith("t:")]
            if not sel:
                messagebox.showinfo("Nothing selected", "Select one or more tracks first.")
                return
            track_ids = [int(iid.split(":", 1)[1]) for iid in sel]
            rows = self.lib.tracks_by_ids(track_ids)

        self._sync_rows_to_ipod(rows)

    def sync_playlist_to_ipod(self, pl_id):
        """Right-click a playlist -> Sync Playlist to iPod: the one
        deliberate path that creates/updates a same-named playlist on
        the device (see _sync_rows_to_ipod's docstring for why every
        other sync path doesn't). Only this playlist's own membership
        is sent - not every other playlist any of these tracks happen
        to also be in."""
        if not self.ipod_mountpoint:
            messagebox.showwarning("No iPod", "Detect or choose an iPod folder first (iPod menu).")
            return
        pl = next((p for p in self.lib.all_playlists() if p["id"] == pl_id), None)
        if not pl:
            return
        rows = self.lib.playlist_tracks(pl_id)
        if not rows:
            messagebox.showinfo("Empty Playlist", f"'{pl['name']}' has no tracks to sync.")
            return
        playlist_names_by_track_id = {r["id"]: [pl["name"]] for r in rows}
        self._sync_rows_to_ipod(rows, playlist_names_by_track_id=playlist_names_by_track_id)

    def _sync_rows_to_ipod(self, rows, silent=False, playlist_names_by_track_id=None, playlists=None):
        """The actual sync-to-iPod work, shared by sync_selected() (the
        toolbar/menu path), dropping tracks or albums onto the iPod
        entry in the sidebar, auto-sync, and sync_playlist_to_ipod().
        silent=True (auto-sync only) skips confirmation dialogs - an
        unattended sync can't stop to ask a question, so it defaults
        to the safe choice (skip unsupported-format tracks) instead of
        assuming yes.
        playlist_names_by_track_id, when given, tags each of those
        track ids with the on-device playlist name(s) to add them to.
        Left as None (the default) for every path except
        sync_playlist_to_ipod() - a plain track/album sync, drag onto
        the iPod sidebar entry, or auto-sync never sends playlist
        membership at all, so a local playlist never gets created or
        updated on the device just because some of its songs happened
        to get synced some other way. Only deliberately syncing that
        playlist (right-click it -> Sync Playlist to iPod) does."""
        if not rows and not playlists:
            return
        if not self.ipod_mountpoint:
            if not silent:
                messagebox.showwarning("No iPod", "Detect or choose an iPod folder first (iPod menu).")
            return

        bad = [r for r in rows if f".{(r['filetype'] or '').lower()}" not in scanner.IPOD_PLAYABLE_EXTENSIONS]
        if bad:
            if silent:
                rows = [r for r in rows if r not in bad]
            else:
                names = ", ".join((r["title"] or r["filepath"]) for r in bad[:5])
                if not messagebox.askyesno(
                    "Unsupported format",
                    f"{len(bad)} selected track(s) are not a format classic iPods play "
                    f"(e.g. {names}). Sync the rest anyway?",
                ):
                    rows = [r for r in rows if r not in bad]

        # Skip tracks already on the device: ones linked to these exact
        # library songs (see Library.link_device_tracks), plus an exact
        # title/artist/album/length match among the unlinked ones, so
        # songs put there some other way aren't copied a second time.
        linked_ids = set(self.device_track_links.values())
        unlinked_keys = {
            _track_dup_key(t.get("title"), t.get("artist"), t.get("album"), t.get("duration_ms"))
            for t in self.device_tracks if str(t["dbid"]) not in self.device_track_links
        }
        already_there = [
            r for r in rows
            if r["id"] in linked_ids
            or _track_dup_key(r["title"], r["artist"], r["album"], r["duration_ms"]) in unlinked_keys
        ]
        if already_there:
            rows = [r for r in rows if r not in already_there]
            applog.log(f"SYNC: skipped {len(already_there)} track(s) already on the device")

        # Ticked playlists (playlists = [(name, [track_id, ...])]): each
        # song is either already on the iPod (by its known copy) or one
        # being copied now; the helper sets each playlist to exactly these.
        playlist_sets = []
        if playlists:
            dbid_by_track = {tid: dbid for dbid, tid in self.device_track_links.items()}
            path_by_track = {r["id"]: r["filepath"] for r in rows}
            for name, track_ids in playlists:
                items = []
                for tid in track_ids:
                    if tid in path_by_track:
                        items.append(("path", path_by_track[tid]))
                    elif tid in dbid_by_track:
                        items.append(("dbid", dbid_by_track[tid]))
                playlist_sets.append((name, items))

        if already_there:
            if not rows and not playlist_sets:
                if not silent:
                    messagebox.showinfo(
                        "Already on iPod",
                        f"All {len(already_there)} selected track(s) already appear to be on the iPod.",
                    )
                return

        playlist_names_by_track_id = playlist_names_by_track_id or {}

        self.progress.configure(mode="determinate", maximum=max(len(rows), 1), value=0)
        skip_note = f" ({len(already_there)} already on the device, skipped)" if already_there else ""
        auto_note = "Auto-syncing" if silent else "Syncing"
        self.status_var.set(f"{auto_note} {len(rows)} track(s) to iPod\u2026{skip_note}")

        self._active_sync_handle = ipod_backend.SyncHandle()
        self.sync_button.configure(state="disabled")
        self._update_cancel_button_state()

        def on_progress(current, total, title):
            self.work_queue.put(("sync_progress", (current, total, title)))

        def work():
            try:
                added, errors, art_notes, cancelled, links = ipod_backend.sync_tracks(
                    self.ipod_mountpoint, rows, playlist_names_by_track_id,
                    on_progress=on_progress, handle=self._active_sync_handle,
                    playlist_sets=playlist_sets,
                )
                self.lib.add_ipod_links(links, how="sync")
                self.work_queue.put(("sync_done", (added, errors, art_notes, cancelled, len(already_there))))
            except ipod_backend.IpodError as e:
                self.work_queue.put(("error", str(e)))
            except Exception:
                # Anything other than IpodError used to just kill this
                # background thread silently - no dialog, no log line,
                # the progress bar and buttons left stuck as they were
                # mid-sync. That's indistinguishable from "syncing just
                # stopped working" with nothing to go on. Catch broadly
                # here, log the real traceback so a real cause (e.g. a
                # non-ASCII filename/title tripping something not
                # expecting it) is actually visible, and still let the
                # UI recover via the normal "error" handler.
                applog.log("SYNC WORKER CRASHED:\n" + traceback.format_exc())
                self.work_queue.put((
                    "error",
                    "Something went wrong during this operation (see the log file "
                    "for details). It's been logged so it can be tracked down.",
                ))

        threading.Thread(target=work, daemon=True).start()

    def _update_cancel_button_state(self):
        """The Cancel button reflects whatever's actually cancellable
        right now, not just whichever single operation most recently
        started or finished. _active_sync_handle covers a music/video/
        photo sync in progress; _active_video_add_cancel covers a
        video folder scan/conversion, Add Video (single file), a
        podcast download, or a Refresh Library pass (which can itself
        be converting videos and checking podcasts). More than one of
        these can be running at once (e.g. Refresh Library's video
        conversion alongside a Sync Selected the person started from a
        different menu), so every place that used to set this button's
        state directly - on either side, enabling or disabling - now
        goes through this instead: without it, whichever operation's
        "done" handler happened to run first would switch Cancel off
        even though another one was still going, leaving the person
        with no way to stop it."""
        active = self._active_sync_handle is not None or self._active_video_add_cancel is not None
        self.cancel_sync_button.configure(state="normal" if active else "disabled")
        if self._smooth_motion():
            # The sidebar only shows Cancel while there's something to cancel.
            if active and not self.cancel_sync_button.winfo_ismapped():
                self.cancel_sync_button.pack(in_=self.device_card, side=tk.BOTTOM, fill=tk.X,
                                             pady=(6, 0), before=self.sync_button)
                self.cancel_sync_button.lift()
            elif not active:
                self.cancel_sync_button.pack_forget()

    def cancel_sync(self):
        """Cancels whichever cancellable operation(s) are currently
        running, regardless of which menu/button started them - both
        kinds are checked and cancelled unconditionally (not
        exclusively), since a sync and a video/podcast operation can
        genuinely be in flight at the same time."""
        cancelling_something = False
        if self._active_sync_handle is not None:
            self._active_sync_handle.cancel()
            self.status_var.set("Cancelling… finishing the current track, then stopping.")
            cancelling_something = True
        if self._active_video_add_cancel is not None:
            self._active_video_add_cancel.set()
            self.status_var.set("Cancelling… stopping after the current file.")
            cancelling_something = True
        if cancelling_something:
            # Not a hard disable - _update_cancel_button_state below
            # will correctly leave it enabled if the OTHER kind of
            # operation is still running and still cancellable; this
            # just means the just-cancelled one(s) no longer count.
            self._update_cancel_button_state()

    def _remove_local_selection_from_device(self):
        """Remove from iPod outside the On iPod screens: removes the
        iPod copies of the selected library songs or albums."""
        if self.current_view in (self.VIEW_ALBUM_GRID, self.VIEW_PODCASTS):
            if not self._selected_albums:
                messagebox.showinfo("Nothing selected", "Select one or more albums first.")
                return
            track_ids = self._track_ids_for_album_keys(self._selected_albums)
        else:
            track_ids = [int(i.split(":", 1)[1]) for i in self.table.selection() if i.startswith("t:")]
            if not track_ids:
                messagebox.showinfo("Nothing selected", "Select one or more songs first.")
                return
        self._remove_track_ids_from_device(track_ids)

    def _remove_track_ids_from_device(self, track_ids):
        if not self.ipod_mountpoint:
            messagebox.showwarning("No iPod", "Detect or choose an iPod folder first (iPod menu).")
            return
        copies = self._device_tracks_for_local_ids(track_ids)
        if not copies:
            messagebox.showinfo(
                "Remove from iPod",
                "None of the selected songs are on the iPod (or they were put there by another "
                "program and can't be matched for certain - remove those from On iPod instead).",
            )
            return
        names = "\n".join(f"\u2022 {t['title'] or '(untitled)'} \u2014 {t['artist'] or ''}" for t in copies[:15])
        if len(copies) > 15:
            names += f"\n\u2026and {len(copies) - 15} more"
        if not messagebox.askyesno(
            "Remove from iPod",
            f"Delete {len(copies)} song(s) from the iPod?\n\n{names}\n\nThey stay in your library.",
        ):
            return
        self._remove_dbids_then([t["dbid"] for t in copies], lambda: None, label="REMOVE")

    def remove_selected_from_device(self):
        if not self.ipod_mountpoint:
            messagebox.showwarning("No iPod", "Detect or choose an iPod folder first (iPod menu).")
            return
        if self.current_view not in (self.VIEW_DEVICE, self.VIEW_DEVICE_PLAYLIST):
            self._remove_local_selection_from_device()
            return
        sel = [iid for iid in self.table.selection() if iid.startswith("d:")]
        if not sel:
            messagebox.showinfo("Nothing selected", "Select one or more tracks first.")
            return
        if not messagebox.askyesno("Remove from iPod", f"Delete {len(sel)} track(s) from the iPod? This cannot be undone."):
            return
        dbids = [iid.split(":", 1)[1] for iid in sel]
        applog.log(f"REMOVE: requested for {len(dbids)} track(s) on {self.ipod_mountpoint}")

        def work():
            try:
                removed, errors, notes = ipod_backend.remove_tracks(self.ipod_mountpoint, dbids)
                self.work_queue.put(("remove_done", (removed, errors, notes, "REMOVE", dbids)))
            except ipod_backend.IpodError as e:
                self.work_queue.put(("error", str(e)))
            except Exception:
                # Anything other than IpodError used to just kill this
                # background thread silently - no dialog, no log line,
                # the progress bar and buttons left stuck as they were
                # mid-sync. That's indistinguishable from "syncing just
                # stopped working" with nothing to go on. Catch broadly
                # here, log the real traceback so a real cause (e.g. a
                # non-ASCII filename/title tripping something not
                # expecting it) is actually visible, and still let the
                # UI recover via the normal "error" handler.
                applog.log("SYNC WORKER CRASHED:\n" + traceback.format_exc())
                self.work_queue.put((
                    "error",
                    "Something went wrong during this operation (see the log file "
                    "for details). It's been logged so it can be tracked down.",
                ))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ firmware --

    def _firmware_status_text(self):
        fw = firmware.current_firmware()
        if not fw:
            return "No firmware imported yet."
        return f"Imported: {fw['name']} (from {fw['source']}, {format_bytes(fw['size'])})"

    def import_firmware_dialog(self):
        path = native_dialogs.choose_open_file(
            title="Choose an iPod restore file (.ipsw) or firmware image",
            filetypes=[("iPod firmware", "*.ipsw Firmware-*"), ("All files", "*.*")],
        )
        if not path:
            return
        self.status_var.set("Checking firmware…")

        def work():
            try:
                self.work_queue.put(("firmware_imported", (firmware.import_firmware(path), None)))
            except (firmware.FirmwareError, OSError, ValueError) as e:
                self.work_queue.put(("firmware_imported", (None, str(e))))

        threading.Thread(target=work, daemon=True).start()

    def _on_firmware_imported(self, info, error):
        if error:
            self.status_var.set("Firmware not imported.")
            messagebox.showerror("Import Firmware", error)
            return
        applog.log(f"FIRMWARE: imported {info['name']} from {info['source']}")
        if hasattr(self, "firmware_status_var"):
            self.firmware_status_var.set(self._firmware_status_text())
        self.status_var.set(f"Imported {info['name']}.")
        if self.ipod_mountpoint and messagebox.askyesno(
            "Install Firmware",
            f"{info['name']} is ready.\n\nInstall it on {self.ipod_label or 'the iPod'} now? "
            "Your music stays. You'll be asked for your password.",
        ):
            self._run_firmware_job()

    def _run_firmware_job(self):
        mountpoint = self.ipod_mountpoint
        name = self.ipod_label or "iPod"
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.status_var.set(f"Installing firmware on {name}\u2026 "
                            "(enter your password when asked; don't unplug the iPod)")
        applog.log(f"FIRMWARE: install started on {name} at {mountpoint}")

        def work():
            try:
                firmware.install(mountpoint)
                self.work_queue.put(("firmware_done", None))
            except firmware.FirmwareError as e:
                self.work_queue.put(("firmware_done", str(e)))
            except Exception as e:
                applog.log("FIRMWARE crashed:\n" + traceback.format_exc())
                self.work_queue.put(("firmware_done", f"{type(e).__name__}: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _on_firmware_done(self, error):
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        if error:
            applog.log(f"FIRMWARE: failed: {error}")
            self.status_var.set("Firmware install failed.")
            messagebox.showerror("Install Firmware", error)
            return
        applog.log("FIRMWARE: install finished")
        self.status_var.set("Firmware written. Eject the iPod and unplug it to finish.")
        messagebox.showinfo(
            "Done",
            "The firmware was installed. Eject the iPod and unplug it: it restarts "
            "into the new firmware. Your music is still on it.",
        )

    def _ipod_display_name(self):
        master = next((p for p in self.device_playlists if p.get("is_master")), None)
        return (master and master.get("name")) or self.ipod_label or "iPod"

    def rename_ipod_dialog(self):
        if not self.ipod_mountpoint:
            messagebox.showwarning("No iPod", "Connect the iPod first (iPod menu \u2192 Detect).")
            return
        current = self._ipod_display_name()
        name = simpledialog.askstring("Rename iPod", "New name for this iPod:", initialvalue=current, parent=self)
        if name is None:
            return
        name = " ".join(name.split())
        if not name or name == current:
            return
        if len(name) > 63:
            messagebox.showerror("Rename iPod", "That name is too long (63 characters at most).")
            return
        mountpoint = self.ipod_mountpoint
        self.status_var.set(f"Renaming the iPod to {name}\u2026")

        def work():
            try:
                errors, label_note = ipod_backend.rename_ipod(mountpoint, name)
            except Exception as e:
                errors, label_note = [f"{type(e).__name__}: {e}"], None
            self.work_queue.put(("ipod_renamed", (name, errors, label_note)))

        threading.Thread(target=work, daemon=True).start()

    def _on_ipod_renamed(self, name, errors, label_note):
        if errors:
            applog.log("RENAME failed: " + "; ".join(errors))
            self.status_var.set("Couldn't rename the iPod.")
            messagebox.showerror("Rename iPod", "\n".join(errors[:5]))
            return
        applog.log(f"RENAME: iPod renamed to {name}" + (f" ({label_note})" if label_note else ""))
        self.ipod_label = name
        for pl in self.device_playlists:
            if pl.get("is_master"):
                pl["name"] = name
        self.refresh_sidebar()
        self._update_device_card()
        if hasattr(self, "ipod_info_title_var"):
            self.ipod_info_title_var.set(f"About {name}")
        if getattr(self, "device_view_banner", None) is not None:
            self.device_view_banner.configure(text=f"\U0001F4F1 Managing {name}")
        self.status_var.set(f"Renamed the iPod to {name}.")
        if label_note:
            short_note = label_note.split(" [", 1)[0]
            messagebox.showinfo(
                "Rename iPod",
                f"The iPod is now called {name}. Note: {short_note}.",
            )

    def wipe_ipod_library(self):
        if not self.ipod_mountpoint:
            messagebox.showwarning("No iPod", "Detect or choose an iPod folder first (iPod menu).")
            return
        device_name = self.ipod_label or "this iPod"
        if not messagebox.askyesno(
            "Wipe iPod Library",
            f"Remove every track from {device_name}?\n\n"
            "This deletes all music currently on the device. It does NOT "
            "reinstall the iPod's firmware or fix a device that won't turn "
            "on — it only empties the music library. This cannot be undone.",
            icon="warning",
        ):
            return

        applog.log(f"WIPE: requested for {device_name} at {self.ipod_mountpoint}")
        self.status_var.set(f"Wiping {device_name}\u2026")

        def work():
            try:
                removed, errors, notes = ipod_backend.wipe_library(self.ipod_mountpoint)
                self.work_queue.put(("remove_done", (removed, errors, notes, "WIPE")))
            except ipod_backend.IpodError as e:
                self.work_queue.put(("error", str(e)))
            except Exception:
                # Anything other than IpodError used to just kill this
                # background thread silently - no dialog, no log line,
                # the progress bar and buttons left stuck as they were
                # mid-sync. That's indistinguishable from "syncing just
                # stopped working" with nothing to go on. Catch broadly
                # here, log the real traceback so a real cause (e.g. a
                # non-ASCII filename/title tripping something not
                # expecting it) is actually visible, and still let the
                # UI recover via the normal "error" handler.
                applog.log("SYNC WORKER CRASHED:\n" + traceback.format_exc())
                self.work_queue.put((
                    "error",
                    "Something went wrong during this operation (see the log file "
                    "for details). It's been logged so it can be tracked down.",
                ))

        threading.Thread(target=work, daemon=True).start()

    def _check_ipod_model(self):
        """Once per connection: if libgpod can't tell which iPod this is,
        ask for the model number (see _ask_for_ipod_model)."""
        mountpoint = self.ipod_mountpoint
        if not mountpoint or mountpoint in self._model_asked_for:
            return

        def work():
            try:
                known = ipod_backend.ipod_model_known(mountpoint)
            except Exception:
                applog.log("MODEL CHECK failed:\n" + traceback.format_exc())
                return
            self.work_queue.put(("model_check", (mountpoint, known)))

        threading.Thread(target=work, daemon=True).start()

    def check_leftover_files(self, auto=False):
        """Looks for song files on the iPod that its song list doesn't
        include, and listed songs whose file is gone, and offers to clean
        both up. Run once per connection (auto=True stays quiet when
        there's nothing to clean) and from the iPod menu / Summary page."""
        mountpoint = self.ipod_mountpoint
        if not mountpoint:
            if not auto:
                messagebox.showwarning("No iPod", "Detect or choose an iPod folder first (iPod menu).")
            return
        if not auto:
            self.status_var.set("Looking for leftover files on the iPod\u2026")

        def work():
            try:
                found = ipod_backend.find_leftovers(mountpoint)
            except Exception:
                applog.log("LEFTOVERS check failed:\n" + traceback.format_exc())
                return
            self.work_queue.put(("leftovers_found", (mountpoint, auto, found)))

        threading.Thread(target=work, daemon=True).start()

    def _offer_leftover_cleanup(self, mountpoint, auto, found):
        orphans, missing, errors = found
        if mountpoint != self.ipod_mountpoint:
            return
        if errors:
            applog.log("LEFTOVERS errors: " + " | ".join(errors))
            if not auto:
                messagebox.showwarning("Clean Up Leftover Files", "\n".join(errors))
            return
        applog.log(f"LEFTOVERS: {len(orphans)} orphan file(s), {len(missing)} song(s) with missing files")
        if not orphans and not missing:
            if not auto:
                self.status_var.set("No leftover files on the iPod.")
                messagebox.showinfo("Clean Up Leftover Files", "Nothing to clean up: every song file on the iPod is in its song list.")
            return
        lines = []
        if orphans:
            size = sum(b for _p, b in orphans)
            lines.append(
                f"\u2022 {len(orphans)} song file(s) ({format_bytes(size)}) that aren't in the iPod's "
                "song list, so they can't be played and show up as Other space."
            )
        if missing:
            names = ", ".join((t or "(untitled)") for _d, t in missing[:5])
            more = f" and {len(missing) - 5} more" if len(missing) > 5 else ""
            lines.append(f"\u2022 {len(missing)} song(s) listed whose file is gone: {names}{more}.")
        if not messagebox.askyesno(
            "Clean Up Leftover Files",
            "Found on the iPod:\n\n" + "\n".join(lines) + "\n\nThese are probably left over from "
            "removals that didn't finish. Delete them? Your library isn't touched.",
        ):
            return
        self.status_var.set("Cleaning up leftover files on the iPod\u2026")

        def work():
            try:
                done = ipod_backend.find_leftovers(mountpoint, delete=True)
            except Exception:
                applog.log("LEFTOVERS cleanup failed:\n" + traceback.format_exc())
                done = ([], [], ["The cleanup failed (see the log file for details)."])
            self.work_queue.put(("leftovers_cleaned", done))

        threading.Thread(target=work, daemon=True).start()

    def _ask_for_ipod_model(self):
        mountpoint = self.ipod_mountpoint
        if mountpoint in self._model_asked_for:
            return
        self._model_asked_for.add(mountpoint)
        applog.log(f"MODEL CHECK: model unknown for {mountpoint}, asking")
        model = self._pick_ipod_model(
            "Which iPod Is This?",
            "The app couldn't tell which iPod model this is, so album art and some "
            "features may not work. Pick yours below. The model number (like MA448) "
            "is on the iPod under Settings > About. It's not the A1xxx number printed "
            "on the back.\n\nPress Skip to leave it. You can set it later on the "
            "iPod's Summary page (Set iPod Model).",
        )
        if model and model.strip():
            self._write_ipod_model(model.strip())

    def _pick_ipod_model(self, title, intro):
        """A dialog with a dropdown of every iPod model libgpod knows (and
        a filter box above it). Returns the chosen model number, like
        "MA448", or None if skipped."""
        try:
            models = ipod_backend.list_ipod_models()
        except Exception:
            applog.log("MODEL LIST failed:\n" + traceback.format_exc())
            models = []
        if not models:
            messagebox.showerror(title, "The list of iPod models couldn't be loaded.")
            return None
        labels = [m["label"] for m in models]
        by_label = {m["label"]: m["number"] for m in models}

        win = tk.Toplevel(self)
        win.title(title)
        win.transient(self)
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text=intro, wraplength=460, justify=tk.LEFT).pack(anchor="w", pady=(0, 12))

        ttk.Label(frame, text="Filter (e.g. 80, video, classic, MA448):").pack(anchor="w")
        filter_var = tk.StringVar()
        filter_entry = ttk.Entry(frame, textvariable=filter_var, width=60)
        filter_entry.pack(fill=tk.X, pady=(2, 10))

        ttk.Label(frame, text="Model:").pack(anchor="w")
        choice_var = tk.StringVar()
        combo = ttk.Combobox(frame, textvariable=choice_var, values=labels, state="readonly", width=60, height=20)
        combo.pack(fill=tk.X, pady=(2, 14))

        def on_filter(*_a):
            words = filter_var.get().lower().split()
            shown = [l for l in labels if all(w in l.lower() for w in words)]
            combo.configure(values=shown)
            if choice_var.get() not in shown:
                choice_var.set(shown[0] if len(shown) == 1 else "")
            ok_button.configure(state="normal" if choice_var.get() else "disabled")

        result = {"model": None}

        def ok():
            label = choice_var.get()
            if label:
                result["model"] = by_label[label]
                win.destroy()

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X)
        ok_button = ttk.Button(buttons, text="Use This Model", command=ok, state="disabled")
        ok_button.pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Skip", command=win.destroy).pack(side=tk.RIGHT, padx=(0, 8))
        filter_var.trace_add("write", on_filter)
        choice_var.trace_add("write", lambda *_a: ok_button.configure(
            state="normal" if choice_var.get() else "disabled"))
        win.bind("<Return>", lambda _e: ok())
        win.bind("<Escape>", lambda _e: win.destroy())

        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.grab_set()
        filter_entry.focus_set()
        self.wait_window(win)
        return result["model"]

    def set_ipod_model_dialog(self):
        if not self.ipod_mountpoint:
            messagebox.showwarning("No iPod", "Detect or choose an iPod folder first (iPod menu).")
            return
        model = self._pick_ipod_model(
            "Set iPod Model",
            "Pick this iPod's model. The model number (like MA448) is on the iPod "
            "under Settings > About, or on Apple's site from the serial number. "
            "It's not the A1xxx number printed on the back.\n\nThis is written to "
            "the iPod's own info file, so it only needs doing once.",
        )
        if not model:
            return
        device_name = self.ipod_label or "this iPod"
        if not messagebox.askyesno(
            "Set iPod Model",
            f"Write model number '{model}' to {device_name}'s own info file?\n\n"
            "This changes information stored on the device itself, not just in "
            "this app. Setting the wrong model can make artwork or other "
            "device-specific behavior worse, not better.",
            icon="warning",
        ):
            return
        self._write_ipod_model(model)

    def _write_ipod_model(self, model):
        device_name = self.ipod_label or "this iPod"
        applog.log(f"SET MODEL: requested '{model}' for {device_name} at {self.ipod_mountpoint}")
        self.status_var.set(f"Setting iPod model to '{model}'\u2026")

        def work():
            try:
                success, errors, verified_supported = ipod_backend.set_ipod_model(self.ipod_mountpoint, model)
                self.work_queue.put(("set_model_done", (success, errors, model, verified_supported)))
            except ipod_backend.IpodError as e:
                self.work_queue.put(("error", str(e)))
            except Exception:
                # Anything other than IpodError used to just kill this
                # background thread silently - no dialog, no log line,
                # the progress bar and buttons left stuck as they were
                # mid-sync. That's indistinguishable from "syncing just
                # stopped working" with nothing to go on. Catch broadly
                # here, log the real traceback so a real cause (e.g. a
                # non-ASCII filename/title tripping something not
                # expecting it) is actually visible, and still let the
                # UI recover via the normal "error" handler.
                applog.log("SYNC WORKER CRASHED:\n" + traceback.format_exc())
                self.work_queue.put((
                    "error",
                    "Something went wrong during this operation (see the log file "
                    "for details). It's been logged so it can be tracked down.",
                ))

        threading.Thread(target=work, daemon=True).start()

    # -------------------------------------------------------- queue loop --

    def _poll_queue(self):
        # Handle results in short slices so a burst of them (a big
        # library's covers all arriving at once) can't hold the window
        # unresponsive; the rest are picked up on the next quick pass.
        deadline = time.monotonic() + 0.04
        try:
            while True:
                if time.monotonic() > deadline:
                    self.after(10, self._poll_queue)
                    return
                kind, payload = self.work_queue.get_nowait()

                if kind == "scan_done":
                    count, found_total, errors = payload
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    if found_total == 0 and not errors:
                        self.status_var.set(
                            "No audio files found in that folder (looked for "
                            + ", ".join(sorted(scanner.AUDIO_EXTENSIONS)) + ")."
                        )
                    elif errors:
                        self.status_var.set(f"Added {count}/{found_total} track(s) — {len(errors)} failed.")
                        applog.log(f"SCAN errors ({len(errors)}): " + " | ".join(f"{p}: {msg}" for p, msg in errors))
                        detail = "\n".join(f"{os.path.basename(p)}: {msg}" for p, msg in errors[:15])
                        if len(errors) > 15:
                            detail += f"\n… and {len(errors) - 15} more."
                        messagebox.showwarning("Some files failed to import", detail)
                    else:
                        applog.log(f"SCAN: added/updated {count} track(s) ({found_total} found)")
                        self.status_var.set(f"Added/updated {count} track(s) in your library.")
                    self.show_library()
                    self.refresh_sidebar()

                elif kind == "refresh_done":
                    total_added, total_found, errors, missing_folders, video_prereqs_missing, cancelled = payload
                    self._refresh_in_progress = False
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    self._active_video_add_cancel = None
                    self._update_cancel_button_state()

                    # Anything the person needs to know about - a folder
                    # that's gone missing, video refresh skipped for a
                    # setup reason, or individual files that failed -
                    # gets surfaced in one popup rather than silently
                    # logged, since that's the whole point of refreshing.
                    problems = []
                    if missing_folders:
                        problems.append(
                            f"{len(missing_folders)} folder(s) could not be found (moved, "
                            "renamed, or not currently mounted):"
                        )
                        problems.extend(f"• {f}" for f in missing_folders[:10])
                        if len(missing_folders) > 10:
                            problems.append(f"… and {len(missing_folders) - 10} more.")
                        problems.append(
                            "If they're gone for good, remove them under Settings → General "
                            "→ Library Folders."
                        )
                    if video_prereqs_missing:
                        problems.append(f"Video folders were skipped: {video_prereqs_missing}.")
                    if errors:
                        problems.append(f"{len(errors)} file(s) failed:")
                        problems.extend(f"• {os.path.basename(p)}: {msg}" for p, msg in errors[:15])
                        if len(errors) > 15:
                            problems.append(f"… and {len(errors) - 15} more.")

                    if cancelled:
                        applog.log(f"REFRESH: cancelled by user — {total_added} item(s) updated before stopping")
                        self.status_var.set(f"Refresh cancelled — {total_added} item(s) updated before stopping.")
                        if problems:
                            messagebox.showwarning("Refresh finished with issues", "\n".join(problems))
                    elif problems:
                        applog.log("REFRESH problems:\n" + "\n".join(problems))
                        self.status_var.set(
                            f"Refreshed: {total_added}/{total_found} item(s) updated — see popup for issues."
                        )
                        messagebox.showwarning("Refresh finished with issues", "\n".join(problems))
                    else:
                        applog.log(f"REFRESH: {total_added} item(s) up to date")
                        pruned = getattr(self, "_last_refresh_pruned", 0)
                        self.status_var.set(
                            f"Library refreshed — {total_added} item(s) up to date"
                            + (f", {pruned} deleted song(s) removed." if pruned else ".")
                        )
                    self.show_library()
                    self.refresh_sidebar()

                elif kind == "eject_done":
                    success, message = payload
                    if success:
                        applog.log(f"EJECT: {message}")
                        self.ipod_mountpoint = None
                        self.ipod_label = None
                        self.device_tracks = []
                        self.device_movies = []
                        self.device_tvshows = []
                        self.device_photo_albums = []
                        self.device_playlists = []
                        self._suppress_automount = True
                        self.refresh_sidebar()
                        if self.current_view in (self.VIEW_DEVICE, self.VIEW_DEVICE_PLAYLIST):
                            self.show_library()
                        self.status_var.set(message)
                    else:
                        applog.log(f"EJECT failed: {message}")
                        messagebox.showerror("Eject failed", message)
                        self.status_var.set(message)

                elif kind == "mount_table_changed":
                    self._on_mount_table_changed()

                elif kind == "device_playlist_removed":
                    success, message, pl_name = payload
                    if success:
                        applog.log(f"DEVICE PLAYLIST: removed '{pl_name}' from {self.ipod_mountpoint}")
                        self.status_var.set(message)
                        if (
                            self.current_view == self.VIEW_DEVICE_PLAYLIST
                            and getattr(self, "current_device_playlist_name", None) == pl_name
                        ):
                            self._show_body("table")
                            self.show_device_all()
                        self.refresh_device_library()
                    else:
                        applog.log(f"DEVICE PLAYLIST remove failed: {message}")
                        messagebox.showerror("Couldn't Remove Playlist", message)
                        self.status_var.set(message)

                elif kind == "ipod_presence_check":
                    still_present = payload
                    if not still_present:
                        self._suppress_automount = False

                elif kind == "ipod_scan_result":
                    mounts, message, was_manual = payload
                    if mounts and not self.ipod_mountpoint:
                        self._set_device(mounts[0])
                        if message:
                            self.status_var.set(message)
                    elif message:
                        self.status_var.set(message)
                    elif was_manual and not self.ipod_mountpoint:
                        self.status_var.set(
                            "No iPod detected. Plug it in, or use iPod \u2192 Choose iPod Folder…"
                        )

                elif kind == "album_thumb_ready":
                    key, pil_img, tint, smooth = payload
                    if HAVE_PIL and smooth == self._smooth_motion():
                        # Only covers on screen animate; off-screen ones
                        # just swap, so scrolling stays smooth while a big
                        # library's art streams in.
                        on_screen = smooth and self._album_tile_on_screen(key)
                        if tint:
                            self._album_tints[key] = tint
                            self._apply_album_tint(key, tint, animate=on_screen)
                        photo = ImageTk.PhotoImage(pil_img)
                        self._cache_insert(self._album_photo_cache, key, photo)
                        label = self._album_tile_widgets.get(key)
                        if label is not None and label.winfo_exists():
                            if on_screen:
                                # Fade the cover in over its placeholder
                                # swatch (same color _make_album_tile used).
                                apple_style.fade_in_image(label, APPLE_PLACEHOLDER_ART, pil_img, photo)
                            else:
                                label.configure(image=photo)
                                label.image = photo

                elif kind == "album_search_results":
                    dialog, listbox, status_var, results_holder, results = payload
                    if dialog.winfo_exists():
                        results_holder["results"] = results
                        listbox.delete(0, tk.END)
                        if not results:
                            status_var.set("No matches found.")
                        else:
                            status_var.set(
                                f"{len(results)} result(s) \u2014 double-click one, or select and Use Selected."
                            )
                            for r in results:
                                label_text = f"{r['collection_name']} \u2014 {r['artist_name']}"
                                if r.get("year"):
                                    label_text += f" ({r['year']})"
                                listbox.insert(tk.END, label_text)

                elif kind == "album_search_error":
                    dialog, status_var, message = payload
                    if dialog.winfo_exists():
                        status_var.set(f"Search failed: {message}")

                elif kind == "album_art_downloaded":
                    data, art_preview_label = payload
                    self._set_preview_image_from_bytes(art_preview_label, data)

                elif kind == "album_search_button_reset":
                    button = payload
                    if button.winfo_exists():
                        button.configure(state="normal", text="Search Internet for Album Info…")

                elif kind == "firmware_imported":
                    self._on_firmware_imported(*payload)

                elif kind == "ipod_renamed":
                    self._on_ipod_renamed(*payload)

                elif kind == "firmware_done":
                    self._on_firmware_done(payload)

                elif kind == "track_info_saved":
                    dialog, saved, warnings, device_updated, device_note = payload
                    messages = []
                    if warnings:
                        messages.append(
                            f"{len(warnings)} file(s) couldn't be fully updated:\n" + "\n".join(warnings[:10])
                        )
                    if device_note:
                        messages.append(device_note)
                    if messages:
                        applog.log("TRACK INFO warnings: " + " | ".join(messages))
                        messagebox.showwarning("Saved with warnings", "\n\n".join(messages))
                    if dialog.winfo_exists():
                        dialog.destroy()
                    applog.log(f"TRACK INFO: updated {saved} track(s), {device_updated} on the iPod")
                    self._refresh_after_track_edit()
                    on_ipod = f", including {device_updated} on the iPod" if device_updated else ""
                    self.status_var.set(f"Updated {saved} item(s){on_ipod}.")
                    if device_updated:
                        self.refresh_device_library()

                elif kind == "album_edit_saved":
                    dialog, affected_count, tag_errors, art_errors = payload
                    messages = []
                    if tag_errors:
                        messages.append(
                            f"{len(tag_errors)} file(s) couldn't be updated:\n" + "\n".join(tag_errors[:10])
                        )
                    if art_errors:
                        messages.append(
                            f"Cover art couldn't be embedded in {len(art_errors)} file(s):\n"
                            + "\n".join(art_errors[:10])
                        )
                    if messages:
                        applog.log("ALBUM EDIT warnings: " + " | ".join(messages))
                        messagebox.showwarning("Saved with warnings", "\n\n".join(messages))
                    if dialog.winfo_exists():
                        dialog.destroy()
                    if affected_count:
                        self.status_var.set(f"Updated {affected_count} track(s).")
                        applog.log(f"ALBUM EDIT: updated {affected_count} track(s)")
                    if self.current_view == self.VIEW_ALBUM_GRID:
                        self.show_album_grid()
                    elif self.current_view == self.VIEW_PODCASTS:
                        self.show_podcasts()
                    else:
                        self.show_library()

                elif kind == "device_read":
                    tracks, playlists, errors, photo_albums, play_import, links = payload
                    self.device_track_links = links
                    # mediatype: 0/1 = audio, 0x02 = Movie, 0x40 = TV
                    # Show - confirmed against real libgpod source (see
                    # cmd_sync's VIDEO handling). device_tracks stays
                    # music-only so the existing music-focused device
                    # view/counts/dedup-check aren't polluted with
                    # video; movies/TV shows get their own lists.
                    #
                    # Every video synced from here on is tagged 0x02
                    # (Movie) on-device regardless of local kind - see
                    # ipod_backend.sync_videos for why - so mediatype
                    # alone can no longer tell "On My Device" movies
                    # from TV episodes the way it used to. What still
                    # can: a TV show's episodes are also added to an
                    # on-device playlist named after the show (same
                    # sync step), and an ordinary movie never is. A
                    # non-master playlist made up entirely of video
                    # tracks is therefore one of these show playlists -
                    # its members go in device_tvshows (grouped by that
                    # playlist's name), everything else video stays in
                    # device_movies. Tracks still carrying the legacy
                    # 0x40 TV Show mediatype (synced before this
                    # change) are kept in device_tvshows too, by their
                    # own tvshow field, for backward compatibility.
                    video_dbids = {t["dbid"] for t in tracks if t["mediatype"] in (0x02, 0x40)}
                    show_name_by_dbid = {}
                    for pl in playlists:
                        if pl["is_master"]:
                            continue
                        members = pl["track_dbids"]
                        if members and members <= video_dbids:
                            for dbid in members:
                                show_name_by_dbid[dbid] = pl["name"]

                    self.device_tracks = [t for t in tracks if t["mediatype"] not in (0x02, 0x40)]
                    self.device_movies = [
                        t for t in tracks
                        if t["mediatype"] == 0x02 and t["dbid"] not in show_name_by_dbid
                    ]
                    self.device_tvshows = [
                        (dict(t, mediatype=0x40, tvshow=show_name_by_dbid[t["dbid"]])
                         if t["mediatype"] == 0x02 else t)
                        for t in tracks
                        if t["mediatype"] == 0x40 or t["dbid"] in show_name_by_dbid
                    ]
                    self.device_photo_albums = photo_albums
                    self.device_playlists = playlists
                    master_pl = next((p for p in playlists if p.get("is_master") and p.get("name")), None)
                    if master_pl:
                        self.ipod_label = master_pl["name"]
                        self._update_device_card()
                    self.refresh_sidebar()
                    self._populate_ipod_info()
                    if errors:
                        applog.log("DEVICE READ errors: " + "; ".join(errors))
                        self.status_var.set("iPod library read with warnings: " + "; ".join(errors))
                    else:
                        applog.log(
                            f"DEVICE READ: {len(self.device_tracks)} track(s), "
                            f"{len(self.device_movies)} movie(s), {len(self.device_tvshows)} TV episode(s), "
                            f"{len(playlists)} playlist(s), {len(photo_albums)} photo album(s)"
                        )
                        self.status_var.set(f"iPod: {len(self.device_tracks)} track(s), {len(playlists)} playlist(s).")
                    new_plays, played_ids = play_import
                    if played_ids:
                        applog.log(
                            f"PLAY COUNTS: imported {new_plays} new play(s) from the iPod "
                            f"across {len(set(played_ids))} track(s)"
                        )
                        if new_plays and not errors:
                            self.status_var.set(
                                self.status_var.get()
                                + f" Imported {new_plays} new play(s) from the iPod."
                            )
                        self._refresh_play_count_cells(played_ids)
                        # Before any auto-sync below, so the iPod gets
                        # Spoofify playlists that include these plays.
                        self._refresh_spoofify(quiet=True)
                    if not self._auto_synced_this_connection:
                        self._auto_synced_this_connection = True
                        self._check_ipod_model()
                        self.check_leftover_files(auto=True)
                        if not errors:
                            self._auto_sync_if_enabled()
                            self._process_pending_video_removals()

                elif kind == "pending_video_removal_done":
                    removed, errors, pending_ids = payload
                    self.lib.clear_pending_video_removals(pending_ids)
                    applog.log(f"VIDEO LIBRARY: removed {removed} previously-deleted video(s) from iPod")
                    if errors:
                        applog.log("VIDEO LIBRARY REMOVE errors: " + "; ".join(errors))
                    self.refresh_device_library()

                elif kind == "sync_progress":
                    current, total, title = payload
                    if total < 0:
                        self.status_var.set("Waiting for the iPod to finish another job\u2026")
                    else:
                        self.progress.configure(maximum=max(total, 1), value=current)
                        self.progress_label_var.set(f"{current}/{total}")
                        self.status_var.set(f"Copying: {title}")

                elif kind == "sync_done":
                    added, errors, art_notes, cancelled, skipped = payload
                    self.progress.configure(value=0)
                    self.progress_label_var.set("")
                    self._active_sync_handle = None
                    self.sync_button.configure(state="normal")
                    self._update_cancel_button_state()
                    applog.log(
                        f"SYNC: added {added} track(s) to {self.ipod_mountpoint}"
                        + (f", skipped {skipped} already on device" if skipped else "")
                        + (" (cancelled by user)" if cancelled else "")
                    )
                    if art_notes:
                        applog.log("SYNC artwork: " + "; ".join(art_notes))
                    if errors:
                        applog.log(f"SYNC errors ({len(errors)}): " + " | ".join(errors))
                        messagebox.showwarning("Sync finished with warnings", "\n".join(errors))
                    if cancelled:
                        status = f"Sync cancelled — {added} track(s) were saved before stopping."
                    else:
                        status = f"Synced {added} track(s) to iPod."
                    if skipped:
                        status += f" ({skipped} already on device, skipped.)"
                    if art_notes:
                        status += " " + "; ".join(art_notes)
                    self.status_var.set(status)
                    self.refresh_device_library()

                elif kind == "video_scan_progress":
                    # payload is already a full, specific status message
                    # ("Checking x.mp4…", "Converting x.mp4 (42%)",
                    # "Copying x.mp4…") - showing it as-is instead of
                    # burying it behind a generic "Scanning:" prefix.
                    self.status_var.set(str(payload))

                elif kind == "video_scan_done":
                    added, skipped, errors, kind_scanned, cancelled = payload
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    self.video_add_folder_button.configure(state="normal")
                    self.video_add_file_button.configure(state="normal")
                    self._active_video_add_cancel = None
                    self._update_cancel_button_state()
                    applog.log(
                        f"VIDEO SCAN: added {added} video(s)"
                        + (f", {skipped} skipped due to conversion errors" if skipped else "")
                        + (" (cancelled by user)" if cancelled else "")
                    )
                    if errors:
                        applog.log(f"VIDEO SCAN errors ({len(errors)}): " + " | ".join(errors))
                        messagebox.showwarning(
                            "Some Videos Couldn't Be Converted",
                            "\n".join(errors[:10]) + (f"\n\u2026and {len(errors) - 10} more" if len(errors) > 10 else ""),
                        )
                    if cancelled:
                        self.status_var.set(f"Cancelled \u2014 {added} video(s) were added before stopping.")
                    elif added == 0 and not errors:
                        self.status_var.set("Already in your library \u2014 nothing new to convert.")
                    else:
                        self.status_var.set(f"Added {added} video(s) to your library.")
                    if kind_scanned == "movie":
                        self.show_movies()
                    else:
                        self.show_tvshows()

                elif kind == "podcast_lookup_done":
                    result, error = payload
                    if error:
                        applog.log(f"PODCAST: lookup failed - {error}")
                        self.status_var.set("Couldn't add that podcast.")
                        messagebox.showerror("Couldn't Add Podcast", error)
                    else:
                        self._confirm_and_subscribe_podcast(result)

                elif kind == "podcast_download_progress":
                    # payload is already a full, specific status message
                    # ("Downloading <show>: <episode> (42%)…") - same
                    # reasoning as video_scan_progress above.
                    self.status_var.set(str(payload))

                elif kind == "podcast_download_done":
                    added, skipped, errors = payload
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    self._active_video_add_cancel = None
                    self._update_cancel_button_state()
                    applog.log(
                        f"PODCAST: downloaded {added} episode(s)"
                        + (f", {skipped} skipped due to errors" if skipped else "")
                    )
                    if errors:
                        applog.log(f"PODCAST errors ({len(errors)}): " + " | ".join(errors))
                        messagebox.showwarning(
                            "Some Episodes Couldn't Be Downloaded",
                            "\n".join(errors[:10]) + (f"\n…and {len(errors) - 10} more" if len(errors) > 10 else ""),
                        )
                    if added == 0 and not errors:
                        self.status_var.set("Already up to date — no new episodes.")
                    else:
                        self.status_var.set(f"Downloaded {added} new episode(s).")
                    if added:
                        self.show_library()

                elif kind == "video_sync_done":
                    added, errors, cancelled = payload
                    self.progress.configure(value=0)
                    self.progress_label_var.set("")
                    self._active_sync_handle = None
                    self.video_sync_button.configure(state="normal")
                    self._update_cancel_button_state()
                    applog.log(
                        f"VIDEO SYNC: added {added} video(s) to {self.ipod_mountpoint}"
                        + (" (cancelled by user)" if cancelled else "")
                    )
                    if errors:
                        applog.log(f"VIDEO SYNC errors ({len(errors)}): " + " | ".join(errors))
                        messagebox.showwarning("Sync finished with warnings", "\n".join(errors))
                    if cancelled:
                        self.status_var.set(f"Sync cancelled — {added} video(s) were saved before stopping.")
                    else:
                        self.status_var.set(f"Synced {added} video(s) to iPod.")
                    self.refresh_device_library()

                elif kind == "photo_scan_progress":
                    self.status_var.set(f"Scanning: {payload}")

                elif kind == "photo_scan_done":
                    added, errors = payload
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    self.photo_add_folder_button.configure(state="normal")
                    applog.log(f"PHOTO SCAN: added {added} photo(s)" + (f", {len(errors)} warning(s)" if errors else ""))
                    if errors:
                        applog.log(f"PHOTO SCAN warnings ({len(errors)}): " + " | ".join(errors))
                    self.status_var.set(f"Added {added} photo(s) to your library.")
                    self.show_photos()

                elif kind == "photo_sync_done":
                    added, errors, cancelled, counts = payload
                    self.progress.configure(value=0)
                    self.progress_label_var.set("")
                    self._active_sync_handle = None
                    self.photo_sync_button.configure(state="normal")
                    self._update_cancel_button_state()
                    applog.log(
                        f"PHOTO SYNC: added {added} photo(s) to {self.ipod_mountpoint}"
                        + (" (cancelled by user)" if cancelled else "")
                    )
                    if errors:
                        applog.log(f"PHOTO SYNC errors ({len(errors)}): " + " | ".join(errors))
                        messagebox.showwarning("Sync finished with warnings", "\n".join(errors))
                    if counts["already"] or counts["merged"]:
                        applog.log(
                            f"PHOTO SYNC: {counts['already']} already on the iPod, "
                            f"{counts['merged']} duplicate copy(ies) removed"
                        )
                    extra = []
                    if counts["already"]:
                        extra.append(f"{counts['already']} already on the iPod")
                    if counts["merged"]:
                        extra.append(f"removed {counts['merged']} duplicate cop{'y' if counts['merged'] == 1 else 'ies'}")
                    extra_text = f" ({', '.join(extra)})" if extra else ""
                    if cancelled:
                        self.status_var.set(f"Sync cancelled — {added} photo(s) were saved before stopping.")
                    elif self._photo_dedupe_only:
                        message = (
                            f"Removed {counts['merged']} duplicate photo(s) from the iPod."
                            if counts["merged"] else "No duplicate photos on the iPod."
                        )
                        self.status_var.set(message)
                        messagebox.showinfo("Remove Duplicate Photos", message)
                    else:
                        self.status_var.set(f"Synced {added} photo(s) to iPod{extra_text}.")
                    self._photo_dedupe_only = False
                    self.refresh_device_library()

                elif kind == "device_video_remove_done":
                    removed, errors, notes = payload
                    self.video_remove_device_button.configure(state="normal")
                    applog.log(f"DEVICE VIDEO REMOVE: removed {removed} video(s) from {self.ipod_mountpoint}")
                    if errors:
                        applog.log(f"DEVICE VIDEO REMOVE errors ({len(errors)}): " + " | ".join(errors))
                        messagebox.showwarning("Remove finished with warnings", "\n".join(errors))
                    if notes:
                        applog.log("DEVICE VIDEO REMOVE notes: " + " | ".join(v for _k, v in notes))
                    self.status_var.set(f"Removed {removed} video(s) from iPod.")
                    self.refresh_device_library()

                elif kind == "remove_done":
                    removed, errors, notes, label = payload[:4]
                    if label == "WIPE" and not errors:
                        payload = payload[:4] + ([t["dbid"] for t in self.device_tracks],)
                    if removed and not errors and len(payload) > 4:
                        # Forget the removed copies now rather than when
                        # the iPod is next read, so syncing them again
                        # straight away isn't skipped as "already on iPod".
                        gone = {str(d) for d in payload[4]}
                        self.device_tracks = [t for t in self.device_tracks if str(t["dbid"]) not in gone]
                        self.device_track_links = {
                            d: tid for d, tid in self.device_track_links.items() if d not in gone
                        }
                        self.lib.forget_ipod_links(list(gone))
                    if errors:
                        applog.log(f"{label} errors ({len(errors)}): " + " | ".join(errors))
                        message = "\n".join(errors)
                        if notes:
                            applog.log(f"{label} notes: " + " | ".join(notes))
                            message += "\n\n" + "\n".join(notes)
                        messagebox.showwarning(
                            f"{'Wipe' if label == 'WIPE' else 'Remove'} finished with warnings", message
                        )
                    else:
                        applog.log(f"{label}: removed {removed} track(s) from {self.ipod_mountpoint}")
                    self.status_var.set(f"Removed {removed} track(s) from iPod.")
                    # A reconciling sync (see _remove_dbids_then) chains
                    # its add step from here, once removal has actually
                    # finished - rather than firing both right away and
                    # risking two device-writing subprocesses running at
                    # once. Skip the refresh in that case: the add
                    # step's own "*_sync_done" handler refreshes the
                    # device view again once it's done, so this one
                    # would just be a redundant read squeezed in between
                    # a write that just finished and one about to start.
                    callback = self._post_remove_callback
                    self._post_remove_callback = None
                    if callback is not None:
                        callback()
                        # Nothing left to add (e.g. only unticked items
                        # were removed): no sync handler will refresh,
                        # so do it here or the device view stays stale.
                        if self._active_sync_handle is None:
                            self.refresh_device_library()
                    else:
                        self.refresh_device_library()

                elif kind == "leftovers_found":
                    self._offer_leftover_cleanup(*payload)

                elif kind == "leftovers_cleaned":
                    orphans, missing, errors = payload
                    if errors:
                        applog.log("LEFTOVERS cleanup errors: " + " | ".join(errors))
                        messagebox.showwarning("Clean Up Leftover Files", "\n".join(errors))
                    size = sum(b for _p, b in orphans)
                    applog.log(f"LEFTOVERS: deleted {len(orphans)} file(s) ({size} bytes), {len(missing)} missing song(s)")
                    self.status_var.set(
                        f"Cleaned up the iPod: {len(orphans)} leftover file(s) ({format_bytes(size)}) "
                        f"and {len(missing)} missing song(s) removed."
                    )
                    self.refresh_device_library()

                elif kind == "model_check":
                    mountpoint, known = payload
                    if not known and mountpoint == self.ipod_mountpoint:
                        self._ask_for_ipod_model()

                elif kind == "set_model_done":
                    success, errors, model, verified_supported = payload
                    if not success:
                        applog.log("SET MODEL errors: " + " | ".join(errors))
                        messagebox.showerror("Could Not Set Model", "\n".join(errors))
                    elif verified_supported is True:
                        applog.log(f"SET MODEL: '{model}' written to {self.ipod_mountpoint}; artwork now supported")
                        self.status_var.set(f"iPod model set to '{model}' — artwork is now supported.")
                        messagebox.showinfo(
                            "Model Set",
                            f"'{model}' was written and verified: libgpod now recognizes this "
                            "device as artwork-capable. Try syncing again.",
                        )
                    else:
                        applog.log(
                            f"SET MODEL: '{model}' written to {self.ipod_mountpoint}, "
                            "but artwork still not recognized as supported"
                        )
                        self.status_var.set(f"iPod model set to '{model}', but artwork still isn't recognized.")
                        messagebox.showwarning(
                            "Model Written, But Not Confirmed",
                            f"'{model}' was written to the device, but libgpod still doesn't recognize "
                            "it as artwork-capable afterward. Either this isn't the right model number "
                            "for your exact device, or libgpod's own model table doesn't include it.",
                        )
                    self.refresh_device_library()

                elif kind == "error":
                    applog.log(f"ERROR: {payload}")
                    if self._post_remove_callback is not None:
                        # A reconciling sync's removal half failed - don't
                        # chain into the add half on top of an error the
                        # user hasn't even seen yet.
                        self._post_remove_callback = None
                    if self._active_sync_handle is not None:
                        self._active_sync_handle = None
                        self.sync_button.configure(state="normal")
                        self._update_cancel_button_state()
                    # Whatever kind of sync was in flight when this came
                    # in, its "start" button was disabled for the
                    # duration (see sync_selected_videos/_photos/etc.) -
                    # re-enable all of them here rather than only the
                    # music one, so a video/photo sync that hit an
                    # error doesn't leave its own button stuck disabled
                    # forever (which looks exactly like "syncing just
                    # stopped working" on the next attempt).
                    for btn_name in ("video_sync_button", "photo_sync_button"):
                        btn = getattr(self, btn_name, None)
                        if btn is not None:
                            try:
                                btn.configure(state="normal")
                            except tk.TclError:
                                pass
                    messagebox.showerror("Error", payload)
                    self.status_var.set(str(payload))

        except queue.Empty:
            pass
        except Exception:
            # Without this, one bad result stopped this loop for good and
            # every later background job (refresh, sync, art) looked hung.
            applog.log("UI ERROR handling a background result:\n" + traceback.format_exc())
            self.after(10, self._poll_queue)
            return
        self.after(200, self._poll_queue)
