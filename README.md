# Linuxpod
A Itunes like Ipod syncing tool.

A *Vibe Coded* Ipod sync tool for linux, currently only tested with a 5.5 gen video.

---


A small desktop app for **click-wheel iPods** (Classic, Nano, Shuffle —
not the touchscreen iPod touch, which uses a completely different,
locked-down filesystem that open-source tools can't write to). It has:

- a local music library (like iTunes' library) — scan folders, tag
  reading, search, playlists
- an iPod panel — see what's on the device, sync selected tracks or
  playlists to it, remove tracks from it

## How it's built

Writing the iPod's database format (`iTunesDB`) by hand, in Python, from
scratch would be fragile and impossible for me to verify without a real
device to test against. Instead this app uses **libgpod**, the
long-standing, community-maintained C library that gtkpod and (formerly)
Rhythmbox use for exactly this job, via a small helper program
(`helper/ipod_helper.c`) that's compiled *on your machine* against your
system's real libgpod headers.

```
src/main.py  ──▶  src/gui.py  ──▶  src/ipod_backend.py  ──▶  helper/ipod_helper  ──▶  libgpod  ──▶  your iPod
                       │
                       └──▶ src/library.py (SQLite) + src/scanner.py (mutagen tags)
```

Compiling locally, rather than me shipping a prebuilt binary or
hand-rolling bindings, means the helper is always built against the
exact libgpod version on your system, instead of guessing at internal
struct layouts from the outside.

## The log file

A running history of what the app has actually done gets written to a
plain-text log — not just warnings and errors, but the successful
outcome of most actions too, each on its own timestamped line, so you
don't have to catch a dialog box in the moment (or screenshot it) to
piece together what happened:

```
~/.local/share/linuxpod/app.log
```

Covered: connecting, disconnecting, and ejecting an iPod; reading its
library; syncing, removing, and wiping tracks (success and failure
both, plus any rollback that happened — see "Wipe and Remove are
crash-safe" below); scanning a folder and refreshing the library;
creating a playlist and adding tracks to one; clearing the library; and
the appearance toggle. A `=== Linuxpod started ===` line marks
the start of each run, and the background auto-mount/eject watcher logs
anything unexpected it hits rather than failing silently (see "Known
limitations").

Open the file directly, or use Help → Open Log File / Show Log File
Location in the app. Opening it this way (Open Log File specifically —
Show Log File Location doesn't count, since it doesn't show you the
content) is tracked: on the 3rd time, once it's had a few seconds to
actually open in your text viewer, the log is cleared out and the count
resets, so it never grows without bound over a long-running install.
You still see everything logged up to and including that 3rd view; only
the next open after that starts fresh, with a line marking the reset.

## Wipe and Remove are crash-safe

Removing tracks — whether a few via "Remove from iPod" or everything
via *Wipe iPod Library* — used to delete each file from the device's
filesystem immediately, before the final database write that actually
commits the change. If that write then failed (which happens on some
devices — see "Couldn't find the iPod firewire ID" in Known
Limitations below), the files were already gone, but the old, unwritten
database still listed them as present: a genuinely broken, inconsistent
state, not just a cosmetic error message.

Both Remove and Sync now defer any real change (deleting a file when
removing, copying a file when syncing) until *after* the database write
has actually succeeded, rolling back cleanly otherwise. A failed
sync/remove/wipe now leaves the iPod exactly as it was — you'll still
see the error, but nothing is destroyed. The log file (above) records
which of these happened, including the rollback itself when one occurs.

A separate, related gap: neither operation used to explicitly flush
its writes to the physical device before reporting success. iPods are
formatted FAT32, which isn't crash-safe against writes still sitting in
the OS's write-back cache — if the device were disconnected shortly
after the app said "done," genuinely corrupting the filesystem (not
just losing the pending change) was possible, especially for anything
touching a lot of data, like removing many tracks or wiping the whole
library at once. Both Sync and Remove/Wipe now explicitly flush to
disk (`sync()`) before reporting success, closing that window. This
can't undo corruption that already happened on a device before this
fix — if you've already needed an iTunes restore, that step is still
necessary — but it should prevent it from happening again.

## Drag and drop

Click and drag one or more selected tracks (from Library/Songs, a
playlist, or an album's track list) or one or more selected album
tiles (from the Albums grid) onto an entry in the sidebar:

- Drop onto a **playlist** to add those tracks to it.
- Drop onto the **iPod entry or one of its on-device playlists** to
  sync those tracks to the device — same as the toolbar's *Sync
  Selected → iPod* button, just via drag instead (see the note in
  *Known limitations* below about dropping on a specific device
  playlist).

A small label follows the cursor once you've moved far enough to
count as a drag rather than a click, showing what you're carrying.
Dropping anywhere else in the sidebar (a section header, Songs,
Albums) does nothing.

Right-click a playlist in the sidebar (in either the normal PLAYLISTS
section or the device view's Music → Playlists) for **Delete
Playlist…**, with a confirmation. This only removes the playlist
itself — the tracks in it stay in your library untouched.

## The device view (View Connected Device)

Once an iPod is connected, **View Connected Device** — its own item in
the menu bar, sitting directly next to Help, not tucked inside another
menu's dropdown — swaps the sidebar to a device-management view
modeled on iTunes' own, and opens the **Summary** screen. It's greyed
out rather than hidden whenever nothing's connected, since a menu bar
can't cleanly add or remove an entry at runtime the way a toolbar
button can:

- **Contents**: song count, album count (distinct albums among tracks
  that have one — a track with no album tag isn't counted as its own
  "album"), and total playback time.
- **Storage**: used / total / free space on the device's filesystem.
- **Sync options**: *Automatically sync new tracks whenever this iPod
  is connected* — when on, right after the device's library is read
  (once per physical connection, not on every internal refresh), the
  app syncs your whole local library the same way *Sync All* does,
  minus the confirmation prompt. The existing duplicate-skip check
  (title/artist/album/duration) means this only actually copies
  what's new since the last time, not the whole library over again.
  Unsupported-format tracks are skipped silently rather than asking,
  since an unattended sync has no one to ask. Off by default.

  *Always sync all music to this iPod, not just what's selected* —
  when on, the device view's persistent **Sync** button (next to Done)
  syncs your whole local music library instead of your saved selection
  profile, for any music screen (Songs/Albums/Artists/Genres/
  Playlists). The same duplicate-skip applies, so repeat clicks still
  only copy what's new. Your saved selection itself isn't touched or
  cleared by turning this on — it's just not what gets used for that
  click while this is enabled. The individual *Sync Selected* buttons
  on each screen still mean exactly what they say (only the current
  selection) regardless of this setting; it's specifically the
  persistent Sync button's behavior that this changes. Off by default.

  Also here: **Set iPod Model…**, for devices whose on-device info file is
  empty (see Known Limitations) — libgpod then has nothing to identify
  the device from, which silently blocks cover art from writing even
  when a sync reports success. Confirmed working on a real 5.5-gen
  iPod Video using its Apple model number. Some known codes:
  - **5.5-gen "Enhanced"** (has *Search* under the Music menu):
    `MA444` (30GB white), `MA446` (30GB black), `MA450` (80GB black)
  - **Original 5th-gen** (no *Search*): `MA002`/`MA146` (30GB
    white/black), `MA003`/`MA147` (60GB white/black)

  Use the exact code for your capacity and color — confirm via serial
  number lookup (Apple's support site or a site like everymac.com) if
  unsure. The button verifies the result immediately: it reports
  whether libgpod actually recognizes the device as artwork-capable
  afterward, not just that the write succeeded.
- **iPod maintenance**: *Wipe iPod Library*, which removes every track
  currently on the device after a confirmation. This is a
  software-level wipe of the music library only — it does not
  reinstall the iPod's firmware/bootloader, and it can't fix a device
  that's stuck, won't boot, or needs a genuine factory restore (that's
  low-level firmware work outside what this app, or the `libgpod` it's
  built on, does at all — a corrupted or unresponsive iPod needs
  Apple's own restore process, and old firmware images may no longer
  even be served for click-wheel models). This only clears an
  otherwise-working iPod's music back to empty. Non-master on-device
  playlists that only contained wiped tracks are left behind as empty
  shells, since the underlying remove operation unlinks tracks, not
  playlists themselves.
- **Done**, at the bottom-right, returns to the normal library sidebar.
  **Sync**, just to its left, is context-aware: it syncs whatever's
  currently selected in whichever screen is showing — Songs, Albums,
  Movies, TV Shows, or Photos — the same way that screen's own Sync
  Selected button would, just always reachable without needing to
  find that button. Always additive: selecting items and syncing only
  ever adds them to the device, never removes anything already there
  that isn't currently selected.

Beyond Summary, the sidebar's Music section (Songs/Albums/Artists/
Genres/Playlists) is really your local library, just reachable from
within the device view — the same is now true of the Movies, TV
Shows, and Photos entries there too: they show your local library for
that category so you can select specific ones (or use Sync/Sync
Selected for all of them) rather than only being able to sync
everything at once. The separate **On My Device** section below is the
reverse direction — what's already on the iPod, browsable read-only
(with *Remove from iPod* for Movies/TV Shows; Photos there is an
album-name/count summary only, not browsable individually — see
Photo support below for why).

Clicking the **Playlists** folder itself (not one of the individual
playlists listed under it) shows all of them at once as a
multi-select list with track counts — select several and *Sync
Selected → iPod* (or the persistent Sync button) adds every track from
every selected playlist in one go, deduplicated, the same "select
several, then sync" pattern Albums already uses. The individual
playlist entries underneath still work the same as before — click one
to see and select from just its own tracks in the table.

Selections in the device view are a **durable sync profile**, saved to
disk — not just remembered for the current session. Set up which
albums, songs, playlists, artist, and genre you want once, and they
stay selected the next time you open the device view, reconnect the
iPod, or even reopen the app entirely — the point being an iPod
syncer you configure once, not something you re-pick (or re-drag)
every single time. Explicit *Select None* still clears a selection.
This is deliberately scoped to the device view only: entering it loads
this saved profile, leaving it clears the in-memory display (without
touching what's saved), and the normal (non-device-view) library keeps
its original behavior of resetting selection on every visit, exactly
as it always has — the two are intentionally separate. Device-side
selections (for *Remove from iPod* in the On My Device section) are
the one exception, staying session-only, since a device's own track
IDs are tied to whatever's currently connected and wouldn't mean
anything on a later visit or a different iPod.

The Contents/Storage numbers refresh automatically whenever the app
re-reads the device's library (after a sync, a manual iPod → Refresh
iPod Library, etc.), whether or not the Summary screen happens to be
open at the time.

While the device view is open, the sidebar itself changes to:

- **SETTINGS** — *Summary* (above); *Music*, which expands to *Songs*,
  *Albums*, *Genres*, and your *Playlists* — these are just this app's
  existing local-library views, reused here so you can browse what
  you'd sync; a **Sync All** button appears in the toolbar the whole
  time the device view is open and pushes your entire local library to
  the iPod (with a confirmation, since a large library can take a
  while) rather than needing anything selected first, the way the
  regular *Sync Selected → iPod* button does. *Movies*, *TV Shows*, and
  *Photos* are placeholders for later — this app manages music only, so
  they currently just explain that rather than doing anything.
- **ON MY DEVICE** — the same four categories, but for what's actually
  already on the iPod. *Music* reuses the existing "On iPod" view;
  Movies/TV Shows/Photos are the same placeholders as above.

## Browsing by artist, genre, or recently added

Alongside Songs and Albums, the sidebar's LIBRARY section has:

- **Artists** and **Genres** — a plain name + track-count list; double-click
  (or Enter) an entry to see just that artist's or genre's tracks in the
  table, same as opening an album. Tracks with no artist/genre tag show
  as "(Unknown Artist)" / "(Unknown Genre)".
- **Recently Added** — every track in your library, newest-added first.
  "Newest" means when it was first scanned into this app's library, not
  the file's own modified/creation date, and it's stable across
  rescans — re-running Refresh Library on a folder you've already
  imported updates existing tracks in place rather than treating them
  as newly added.

All three behave like any other track list — select, drag, right-click
to add to a playlist or sync to the iPod.

## Video support (Movies and TV Shows)

The "\U0001F3B5 Music \u25be" button above the sidebar expands into **Music** /
**Movies** / **TV Shows**, switching the whole sidebar and content area
between your existing music library and a separate video library,
browsable as a thumbnail grid (like the Albums grid) rather than a
plain list — each video shows a frame grabbed from ~10% into it (via
`ffmpeg`, avoiding black/blank opening frames), with the title (and
show/season/episode for TV) underneath. Click to select, right-click
for Sync/Remove, same interaction as album tiles.

**Adding videos**: in Movies or TV Shows mode, *Add Video Folder…*
scans a folder for video files (`.mp4`, `.m4v`, `.mov`, `.mkv`, `.avi`,
`.wmv`, `.flv`, `.webm`) and adds each one to your library; *Add
Video…* does the same for just one file you pick, when you don't want
to scan a whole folder. TV Shows mode tries to parse `Show Name -
S01E02 - Episode Title.ext` style filenames (dots/underscores as
separators work too, e.g. `The.Office.S03E05.mkv`) to fill in
show/season/episode automatically; anything that doesn't match that
pattern just uses the filename as the title with the show/season/
episode fields left blank.

**Conversion**: classic iPod video (5th/5.5-gen) plays H.264 Baseline
Profile up to Level 3.0, up to 640×480, up to 30fps, with AAC audio —
not just any `.mp4` file. Anything added that isn't already in that
exact format gets automatically converted via `ffmpeg` (checked with
`ffprobe`) before being added to your library, letterboxed rather than
stretched if the source isn't 4:3/16:9-friendly. This needs `ffmpeg`
(and `ffprobe`) **and `AtomicParsley`** installed — `install.sh` now
includes both; the app will tell you plainly if either is missing
rather than failing partway through a scan. Converted files are saved
to a folder you choose once (Edit → Settings → Video → *Choose
Folder…*) — the app asks you to set this before the first video gets
added, since there's nowhere sensible to default to.

Why both tools: codec/profile/resolution conformance alone isn't
enough for these devices. Classic iPods (5th/5.5-gen) require a
special `uuid` atom written into the MP4 container before they'll
actually decode and display H.264 video at all — confirmed directly
from HandBrake's own official documentation ("Older classic iPods
require a special atom to be written to the MP4 file before they will
play back on the device... HandBrake doesn't add this by default")
and from AtomicParsley's own source. A file can be fully spec-compliant
H.264 Baseline/Level 3.0/640×480 and *still* show only a black screen
(audio still plays, duration still reads correctly, since neither
depends on this atom) without it - which is exactly what happened
during development here before this was found. `AtomicParsley` (a
small, purpose-built tool, not a full transcoder) adds it as a
post-processing step after `ffmpeg`'s encode, using the flag confirmed
directly from the tool's own `--longhelp` text
(`--DeepScan --iPod-uuid 1200`). This applies even to files that
*already* look compatible on codec grounds - the app still copies and
atom-checks them, rather than assuming "right codec" implies "has the
atom," since those are genuinely independent facts about a file.

**Syncing**: select videos in the grid and *Sync Selected → iPod*
(toolbar or right-click), the same way music syncs. Getting real video
playback working took two separate, real fixes, in order: the track's
device-side `mediatype` wasn't being set explicitly at first (it
relied on libgpod's own indirect filename-based detection), which is
now set directly to the verified value for video content; and even
after that, video still showed a black screen with correct
duration/audio until the iPod UUID atom (above) was identified as the
actual missing piece. Both are now confirmed against real, independent
sources rather than guessed.

Re-scanning a folder (or re-adding videos after a pipeline fix like
the ones above) used to create a second, duplicate converted file
each time, since the converted filename was only based on collision
avoidance, with no memory of which converted file belonged to which
original. Output filenames are now deterministic per source file (a
short hash of the full source path, not just its name), so
re-converting the same source overwrites its own previous output
instead of accumulating copies, and the library correctly updates the
existing entry rather than adding a duplicate.

**Movies and TV Shows now land in their correct on-device menu.** This
took finding libgpod's own real field documentation, which turned out
to reveal three separate, previously-unverified requirements:
`mediatype` must be `0x40` (not `0x02`) for TV Shows specifically —
"shows up ONLY in TV Shows" — and a wholly separate field,
`movie_flag`, must also be set (`0x01`) for content to register as a
movie; setting `mediatype` alone (what this app did before) wasn't
sufficient. TV Shows also carry their show name, season, and episode
through to the device now, using libgpod's own dedicated
`tvshow`/`season_nr`/`episode_nr` track fields, not just this app's
local library. Even with both of those set correctly, movies were
still appearing under Music instead of Movies, traced to video tracks
also being added to the master playlist (MPL) — libgpod's own docs
describe the MPL as specifically the list behind the Music menu, and
document the same exclusion for Podcasts ("member tracks in the
Podcasts playlist are not normally members of the MPL, so on the iPod
they will only show up under the Podcasts menu"). Video tracks are no
longer added to the MPL; menu placement now follows `mediatype` alone,
matching that same pattern. Video artwork/posters still aren't synced
to the device; the generated thumbnails are local-library-browsing
only.

## Photo support

The "\U0001F3B5 Music \u25be" switcher's **Photos** option works the same way
as Movies/TV Shows: a thumbnail grid, *Add Photo Folder…* to scan a
folder of images (`.jpg`, `.jpeg`, `.png`, `.gif`, `.bmp`, `.tiff`),
and select-and-sync to the iPod.

Unlike video, photos need no conversion step and no extra system
tools — `libgpod` has its own, completely separate on-device database
for photos (`Itdb_PhotoDB`, distinct from the `iTunesDB` music and
video use), and its own `itdb_photodb_add_photo()` function handles
format and thumbnail generation internally once it has a readable
image file. This app's own thumbnails (for the grid view here, not the
device) are just resized copies made with the already-installed
Pillow, the same library used for album art.

Adding a folder optionally asks for an on-device **album** name; every
photo also always goes into the required master "Photo Library" (the
photo equivalent of the MPL for music — every device needs exactly
one for photos to show up at all), in addition to that named album if
one was given. That album name shows as a label under each photo's
thumbnail in the app's own grid too, not just on the device.

**On This iPod**: both the Movies/TV Shows/Photos switcher and the
device view (View Connected Device) sidebars now have an on-device
view alongside the local-library one. For Movies and TV Shows, that's
a real, selectable thumbnail grid — same interaction as the local
grid, just with *Remove from iPod* instead of *Sync*/*Add Folder*, and
sourced from what libgpod's own `mediatype` field reports rather than
this app's own database. Photos is different: the device only hands
back **album names and photo counts**, not individual photo IDs or
thumbnail image data (that would need `gdk-pixbuf` integration this
app doesn't have), so on-device Photos is an album-level summary list,
not a browsable grid like the local one.

## Editing album info

Right-click any album tile in the Albums grid for *Edit Album Info…*:

- **Album / Artist / Genre / Year** fields apply to every track in that
  album. Saving writes these straight to the tracks' own file tags
  (MP3 via ID3, M4A/M4B via MP4 atoms) — not just this app's local
  library — so the edit survives a future *Refresh Library* rescan
  instead of being silently overwritten by whatever the file still
  said. WAV/AIFF and plain AAC aren't supported for tag *writing*
  (only reading) — the library updates for those, but the files
  themselves don't, and you'll see a warning listing which ones.
- **Choose Image File…** picks a local image to use as this album's
  cover art. **Search Internet for Album Info…** looks up the album on
  Apple's public iTunes Search API (no account or API key needed) and
  lets you pick a match to fill in the fields and download its artwork
  in one step. Either way, the art is embedded directly into each
  track's own tags (ID3 `APIC` for MP3, the MP4 `covr` atom for
  M4A/M4B), replacing any existing embedded picture on that file rather
  than piling up duplicates across repeated edits — same
  MP3/M4A/M4B-only support as the text fields above.
- The internet search needs a working connection; I can't test live
  network calls in the environment I built this in, so if a search
  fails outright (as opposed to just finding nothing) the error message
  it shows is worth double-checking against — it's meant to say plainly
  when it can't reach the service versus when there are simply no
  matches.
- Minor known gap: clearing the Genre field clears it on the file too;
  clearing Year only stops a *new* year from being written; it doesn't
  erase an existing one already on the file.

## Appearance toggle

Settings → "Use an iTunes-style look" changes more than color now:

- **Sidebar** becomes a grouped source list — LIBRARY, PLAYLISTS, and
  DEVICES section headers (DEVICES only appears once an iPod is
  connected), instead of one flat list.
- **Toolbar** moves the search box to the far right edge and adds a
  divider between library actions and iPod actions, instead of
  everything sitting in one inline row.
- **Bottom bar** shows a persistent library summary — "N songs, H hr M
  min, size" — the way iTunes' own status bar does, instead of that
  space being empty except during an action.
- Track rows get the striped background and blue selection highlight
  from before.

This is a genuine layout change built with Tkinter's own widgets and
theming, not a pixel-for-pixel recreation of iTunes' actual UI — don't
expect the exact fonts, icons, or window chrome to match, and there
are no (non-functional) playback transport controls, since this app
doesn't play audio. The choice is remembered between launches.

## Setup

```bash
git clone <wherever you put this> linuxpod   # or just unzip it
cd linuxpod
chmod +x install.sh
./install.sh
```

`install.sh` installs (via `apt`, asking for `sudo`): `python3-tk`,
`libgpod-dev`, `pkg-config`, a C compiler, glib headers, `udisks2`/
`util-linux` (for auto-mount/eject), `ffmpeg`/`atomicparsley` (video
conversion), and `zenity` (your desktop's native file/folder picker,
instead of Tkinter's plainer built-in one — `kdialog` is used
automatically instead on KDE Plasma if that's what's installed, and
Tkinter's own dialog is the fallback if neither is present); installs
the `mutagen` and `Pillow` Python packages (tag reading and album art,
respectively); and compiles `helper/ipod_helper`.

On non-Debian distros, install the equivalents yourself — the script
prints the Fedora/Arch package names as a starting point — then run:

```bash
pip3 install --user -r requirements.txt
make -C helper
```

### If compilation fails

`libgpod` hasn't changed its core API in a long time, but if
`make -C helper` reports an error about an unknown field or function
name, open `/usr/include/gpod/itdb.h` on your machine, find the correct
name, and adjust `helper/ipod_helper.c` to match — the compiler error
will point at the exact line. I wrote this against the standard,
long-documented libgpod API, but I don't have a libgpod install or a
physical iPod to test against in the environment I built this in, so
please treat the first sync as a trial run (see "Before your first
real sync" below).

## Running it

```bash
python3 src/main.py
```

1. **Build your library**: File → Add Folder to Library, point it at
   your music folder. It'll walk the folder, read tags with mutagen, and
   list everything on the left/center. The app remembers every folder
   you add.
2. **Keep it up to date**: open Edit → Settings… for
   *Refresh Library* (re-scans every folder you've added — new files get
   added, changed tags get picked up — use it after adding new music to
   a folder you've already imported, rather than re-adding the whole
   folder) and *Remove Duplicate Tracks* (a manual backstop for
   duplicate rows; adding the same file more than once through normal
   use — a different folder pick, a symlink, "Add Files" on something
   already in your library — already updates the existing entry rather
   than creating a duplicate, so this is mainly for edge cases like a
   case-insensitive filesystem). Settings is also where the appearance
   toggle lives (see below); *Wipe iPod Library* lives on the device
   view's Summary screen instead (View Connected Device, also below).
   *Clear Library* (File
   menu / toolbar) wipes your local library (tracks and playlists) so
   you can start over; it only affects this app's local library, never
   anything already on your iPod.
3. **Browse by album**: click "Albums" in the sidebar for an iTunes-style
   grid of cover art (uses embedded art via Pillow; without art, or
   without Pillow installed, albums show as plain colored tiles instead
   so the grid still works, just without pictures). Click a tile to
   select it (blue border) — *Select All*/*Select None* and *Sync
   Selected → iPod* all work on album selections here the same way they
   work on track selections in the list view (and the same way they
   work on video and photo grid selections too — one Select All that
   covers whatever's currently on screen, not just the music list).
   Double-click a tile to open it and see its tracks. Right-click a
   tile for *Edit Album Info…* — see below.
4. **Make playlists** (optional): select tracks, right-click → *Add to
   Playlist…*, or use the *New Playlist* button.
5. **Connect your iPod**: plug it in. Most desktop environments mount it
   automatically; if yours doesn't, the app watches for it in the
   background and mounts it itself the same way a file manager would
   (via `udisksctl`, no root needed), within a few seconds. Detection
   checks every currently-mounted partition reported by `lsblk`
   (wherever it's actually mounted — `/media`, `/run/media`, a
   desktop environment's `gvfs` mount, anywhere) and confirms it's a
   real iPod by its `iPod_Control` folder, not just its label. You can
   also trigger this immediately with iPod → Detect / Auto-Mount iPod,
   or point at it manually with iPod → Choose iPod Folder… if
   auto-mount doesn't find it (e.g. `lsblk`/`udisksctl` aren't
   installed).
6. **Sync**: select tracks (use *Select All* in the toolbar/Edit menu,
   or Ctrl+A, to grab everything in the current view — Library, a
   playlist, or search results), then *Sync Selected → iPod*. Progress
   shows in the status bar. If you need to stop partway through, *Cancel
   Sync* finishes the track currently being copied, saves everything
   done so far to the iPod's database, and stops — it never leaves a
   half-written database or corrupts what's already there; tracks after
   the cancellation point are simply never added. Tracks already on the
   device aren't detected as duplicates by content — syncing the same
   track twice will currently add it twice, so avoid re-selecting tracks
   you've already synced.
7. **Remove tracks from the iPod**: click "On iPod" in the sidebar,
   select tracks, *Remove from iPod*.
8. **Eject when you're done**: use the *Eject iPod* button/menu item.
   This unmounts it and spins the drive down via `udisksctl`, the same
   thing a file manager's Eject does — it's then safe to unplug. The
   background auto-mount watcher deliberately leaves an ejected iPod
   alone (even though it's still plugged in) until you either unplug it
   or ask it to look again, so ejecting doesn't just get immediately
   undone a few seconds later.

Always eject before unplugging, the same as any USB drive.

## Before your first real sync

I built and syntax-checked everything I could without a libgpod
install or a physical iPod (neither is available in the environment I
worked in), including a syntax check of the C helper against a stub
header matching libgpod's documented API. That confirms the code is
well-formed, but it does **not** confirm libgpod's exact field names
matched on the first try, or that a real device behaves as expected.
Recommended first run:

- Sync a **small handful of tracks** first, then check they show up
  correctly on the iPod itself (song list, correct title/artist, plays
  fine) before syncing your whole library.
- Keep in mind the iPod's own library is normally rebuilt entirely by
  iTunes/other tools that own the `iTunesDB` file; this app edits that
  same file, so as with any third-party iPod tool, there's a small
  chance the very first read/write cycle behaves unexpectedly on your
  specific model/firmware. Nothing here does a low-level format of the
  device — worst case is a corrupted database, which is fixed by
  restoring the iPod through Apple's tools or gtkpod, not a bricked
  device.

## Known limitations (v1)

- **Duplicate prevention**: adding music is keyed on each file's real,
  canonical path (symlinks resolved), not the exact string you picked —
  so the same physical file reached two different ways (a symlinked
  folder, a subfolder added both directly and as part of its parent,
  "Add Files" on something already in your library, re-running
  *Refresh Library*) always updates the same entry rather than creating
  a second one. If your library already has old duplicate rows from
  before this fix, the simplest way to clear them out is *Clear
  Library* followed by re-adding your music folder(s) — everything
  will re-import cleanly under this logic. There's no per-track merge
  tool for that one-time cleanup; it wasn't worth the added surface
  area once adding duplicates in the first place was fixed.
- **If tracks you synced before the VBR/CBR update still don't play**, that's
  expected — the VBR/CBR fix below only applies to tracks scanned (or
  rescanned) after it, since it depends on tag data collected at scan
  time. Use *Refresh Library* to pick it up for your existing library
  without re-adding folders from scratch, then re-sync those tracks.
- **iPod touch is not supported.** Only click-wheel iPods (Classic,
  Nano, Shuffle) work — see the top of this file.
- **"Couldn't find the iPod firewire ID" / sync fails outright on
  video Nano (3rd-gen)/later, Classic, or Touch.** These models need
  libgpod to know the device's hardware ID before it can write the
  database at all, normally read from a `SysInfoExtended` file on the
  device. `install.sh` now installs `libgpod-common`, which carries a
  udev rule that populates this automatically the next time you plug
  the iPod in — if you already hit this error before updating, unplug
  and replug the device once after installing it. If that still
  doesn't work: try running `ipod-read-sysinfo-extended /dev/sdX
  /path/to/mountpoint` manually (as root; find the raw device name,
  not the partition, e.g. `sdb` not `sdb1`, via `lsblk` or `dmesg`
  right after plugging in), or as a last resort, get the device's USB
  serial via `lsusb -v | grep -i Serial` and add a line
  `FirewireGuid: 0x<that serial>` to `iPod_Control/Device/SysInfo` on
  the device yourself.
- **A real regression, found and fixed: a fallback artwork call was
  removed by mistake, based on a wrong assumption about what it was
  for.** `itdb_track_set_thumbnails()` (the main libgpod call used per
  track) is a convenience wrapper, and on a device libgpod doesn't
  fully recognize, it can refuse outright — even though the
  lower-level function it wraps, `itdb_artwork_set_thumbnail()`, can
  still succeed and produce real, working artwork on that same device.
  The code originally called this lower-level function as a fallback
  whenever the wrapper failed, but a comment mischaracterized it as
  existing "only to get a better error message," and a later cleanup
  pass removed it entirely on the strength of that wrong assumption —
  removing the thing that was actually making artwork work on at least
  one real device (confirmed: `gtkpod`, using the same underlying
  `libgpod`, could still get artwork onto that device after this app's
  fallback was removed, which is what exposed the mistake). The
  fallback call is back, and this time its result is actually used
  (counted as success) instead of only being used to word an error
  message.

  Separately, the sync log's artwork reporting was made more honest
  regardless of the above: `itdb_track_set_thumbnails()` (or its
  fallback) only confirms the image file itself is readable/settable
  at that point — the real `ArtworkDB` writing happens lazily, inside
  `itdb_write()` later, and libgpod doesn't report per-track
  success/failure for that later step at all. So the sync log checks
  the actual on-device `ArtworkDB` file after a successful write and
  reports what it finds — its real size, or that it wasn't created —
  instead of trusting a per-track return value that was never a fully
  reliable signal on its own.

  Even with the fallback restored, artwork can still fail to write on
  a device where `itdb_device_supports_artwork()` genuinely returns
  false — confirmed via a diagnostic line in the sync log
  (`device_diagnostic`) on a real 5.5-generation iPod Video, a model
  documented (alongside 2nd-gen Nanos and the 2nd-gen Shuffle) as one
  that ships with an empty on-device `SysInfo` file, giving libgpod
  nothing to identify it from. **Set iPod Model…** (Sync options, on
  the device view's Summary screen — model numbers for the iPod
  Video line documented there) writes an Apple model number directly
  to the device's `SysInfo` file — gtkpod's own long-standing fix for
  this — and, unlike before, now immediately verifies the result: it
  re-reads sysinfo fresh afterward and reports whether
  `itdb_device_supports_artwork()` actually flips to true, rather than
  just confirming the write happened. A write can succeed mechanically
  without the given model string being one libgpod's internal table
  actually recognizes, so this closes that gap in what the app tells
  you. Confirmed working on real hardware.
- **iPod Classic 6th/7th-gen and Nano 5th-gen+** use a further, separate
  checksum (`hash58`/`hash72`) baked into the database itself — this is
  a different issue from the firewire ID above, and shows up
  differently: the sync *succeeds* with no error, but the tracks don't
  actually appear in the iPod's own music list. Recent libgpod versions
  include community-reverse-engineered support for it; if your device
  is one of these and this happens, try updating to the newest
  `libgpod-dev` your distro offers, or use `gtkpod` (which uses the
  same library) to cross-check.
- **Syncing skips tracks that already appear to be on the device**,
  matched the same way local-library duplicates are (title, artist,
  album, and duration, case-insensitive) — not a content hash, since
  device dbids are assigned by the device itself and can't be compared
  to local library ids directly. This means a genuine re-recording or
  a different edit of the same song with a different duration would
  still sync as a separate track, and (very rarely) two different
  songs that happen to share all four fields exactly would look like a
  duplicate. Skipped tracks are logged and noted in the sync summary.
- **Dragging tracks/albums onto a specific on-device playlist still
  syncs them to the iPod generally**, following each track's own local
  playlist memberships (same as the toolbar's Sync button), rather
  than filing them under just the on-device playlist you dropped on —
  the sync pipeline doesn't have a way to target one existing device
  playlist directly yet. Actually *viewing* a device playlist (from
  the sidebar's DEVICES section) does correctly show just its own
  tracks — the helper reports each on-device playlist's membership,
  not just its name and count.
- **The device view's Sync All button syncs the whole local library
  any time the device view is open**, not specifically only while
  browsing Music's Songs/Albums/Genres/Playlists sub-screens — a
  simplification of what a strict reading of "sync all" under Music
  would imply, since wiring it to appear/disappear per sub-screen
  would mean injecting it into shared view widgets that also serve
  the normal (non-device) sidebar.
- **Movies, TV Shows, and Photos (under both Settings and On My Device
  in the device view) are placeholders**, not real features — this app
  manages music only, so they just explain that rather than doing
  anything. There's no video/photo scanning, tagging, or sync pipeline
  behind them.
- Only core tags sync (title/artist/album/genre/composer/track/year), plus
  cover art where present — checked in this order: embedded ID3 APIC
  frames (MP3/WAV/AIFF), the MP4 `covr` atom (M4A/M4B), and finally a
  folder-level image (`cover.jpg`, `folder.jpg`, `album.jpg`, `front.jpg`,
  or `albumart.jpg`/`.jpeg`/`.png`, case-insensitive) in the same
  directory as the track — common for collections that store art
  per-album rather than per-file. Plain AAC without an MP4 container has
  no tag mechanism at all, so only a folder image can cover those.
  Ratings and play counts aren't set. Every sync now reports something
  about cover art either way: a line like "3 of 5 tracks got cover art"
  from libgpod itself, or, if nothing was found on the Python side at
  all, an explicit "no cover art found for any of N tracks" note — so a
  sync with no art visible is never just silent about it.
- WAV/AIFF are uncompressed and will eat iPod storage fast — that's an
  iPod limitation, not this app's.

## Project layout

```
linuxpod/
  install.sh            setup script
  requirements.txt
  src/
    main.py              entry point
    gui.py                Tkinter interface
    library.py            local SQLite library (tracks, playlists)
    scanner.py             folder scanning + mutagen tag reading
    ipod_backend.py       finds the iPod, talks to the compiled helper
  helper/
    ipod_helper.c        the libgpod-linked C helper (compiled locally)
    Makefile
```

## Play counts from the iPod

Every time the app reads the iPod (when it connects, and again after
each sync or remove), it pulls the iPod's play counts back into your
library. Songs → **Plays** and **Last Played** columns show them, and
clicking **Plays** sorts most-played first. The iPod view shows the
same columns with the iPod's own numbers.

Plays are *added*, never overwritten: the app remembers each on-iPod
track's play total from the last read and only credits what's been
played since, so reconnecting or re-reading never counts a play twice,
and plays from more than one iPod add up. The first time a song is
seen, all of the iPod's existing plays for it are imported. Songs are
matched to your library the same way sync decides a song is already on
the iPod (title, artist, album and length). Each import is logged as a
`PLAY COUNTS:` line in the log file.

This is one-way (iPod → library); syncing doesn't change play counts
on the iPod. Ratings and skip counts aren't imported, and neither are
video play counts.

## Apple-style look

The app now uses an Apple-style look by default (Settings → Appearance
can switch it off): rounded antialiased buttons, fields, checkboxes,
sliders and progress bar drawn by `src/apple_style.py`, a cleaner font
(Ubuntu / Noto Sans / Open Sans / Roboto / Cantarell / Liberation
Sans / DejaVu Sans, the first one installed as a regular-weight,
non-variable font), white content panes on a light gray sidebar and toolbar,
rounded cover art, eased scrolling in the album/video/photo grids,
cover art that fades in as it loads, an eased progress bar, and a
short fade-in when the window opens (needs a compositing desktop).

### iOpenPod-inspired layout

In the Apple-style look the window follows iOpenPod's layout: a device
card at the top of the sidebar (iPod name, track count and hours,
storage bar, Eject, and a View iPod… / Detect iPod… link), the Sync
buttons right under it (Cancel appears only while something is
running), Settings pinned to the bottom of the sidebar, a page header
over the content with the page title on the left and library actions
plus search on the right, and album cards tinted to match their cover
with an "Artist · Year · N tracks" line.

Its own touches on top of that: the device card is a dark graphite
card with a silver iPod and an iTunes-style color capacity bar (Audio /
Video / Other / Free, with sizes), and album covers get a Cover
Flow-style reflection.

Layout (Apple-style look): a top bar with the app name, Music / Movies /
TV Shows / Photos / Podcasts tabs (replacing the sidebar dropdown),
search and a Settings button; the page title has a count line under it
("96 songs", "12 albums"); and the iPod panel, with its Sync buttons,
sits at the bottom of the sidebar.


## Album view freeze fix and crash logs (2026-10-07)

On Python 3.14 / Tk 9, opening or refreshing a big library could leave
the window unresponsive for 5+ seconds at a time while album covers
loaded, long enough for the desktop to offer to force-quit it. The
cover rounding, reflection and tint now run in the background thread,
album titles are trimmed using cached character widths, the per-cover
card colors no longer create ttk styles (each one re-styled the whole
window), and background results are handled in short slices.

If the app ever closes unexpectedly, look in
`~/.local/share/linuxpod/`: Python errors are in `app.log`,
and anything printed to the terminal (X errors, hard-crash stack dumps)
is in `crash.log`. Run with `LINUXPOD_STDERR=terminal` to keep
those messages in the terminal instead.

## AppImage

`Linuxpod-x86_64.AppImage` is the same app in one file, with its own
Python, Tk, Pillow, mutagen and iPod helper inside, so nothing needs to
be installed with apt/dnf first. Make it executable and run it:

    chmod +x Linuxpod-x86_64.AppImage
    ./Linuxpod-x86_64.AppImage

It uses the same library, settings and logs as the zip install
(`~/.local/share/linuxpod/`). Still taken from your system:
a desktop's usual libraries (X11, fontconfig, gdk-pixbuf, glib), and the
optional tools ffmpeg/AtomicParsley (video), udisks2 (eject) and zenity
(native file pickers). Newer iPods also still need libgpod's udev rule
(see Known Limitations). If double-clicking does nothing, install
`libfuse2` (or run it with `--appimage-extract-and-run`).

Rebuild it with `packaging/build_appimage.sh` on Ubuntu 24.04 or newer.

## Podcasts, album loading and the iPod card (2026-10-07)

- Podcast episodes no longer show up in Songs, Albums, Artists, Genres or
  Recently Added; they live under Podcasts. On the iPod they now go in
  the Podcasts menu (marked as podcasts, unplayed, resume where you left
  off) instead of Music. Episodes an older version synced as music are
  moved to the Podcasts menu the next time the iPod is read.
- Album covers that aren't square are center-cropped, so a cover
  arriving never changes its card's height, and cards load on a neutral
  gray before easing into the cover's color.
- The iPod card is midnight blue, and while no iPod is connected it can
  be minimized to a slim bar with the arrow on its right. The sidebar
  list scrolls when it doesn't fit above the card.
- AppImage: libxml2 and its ICU libraries are bundled for the iPod
  helper (newer distros ship a libxml2 with a different version number).

## "Unsupported checksum type" when syncing (2026-10-08)

Some other iPod tools write their own `iPod_Control/Device/SysInfoExtended`
file with a database version libgpod doesn't know (iOpenPod wrote
`DBVersion 25` on a 5.5-gen iPod Video), and libgpod then refuses to
save the iPod's database. The helper now sets such a file aside while it
runs (identifying the iPod from `SysInfo` instead) and puts it back
untouched afterwards. It also corrects `SysInfo`'s model number when it
holds something libgpod can't look up, using the one in
SysInfoExtended, which also makes cover art work. Set iPod Model now
refuses model numbers libgpod doesn't recognize.

## Get Info (song and episode editor)

Right-click a song or podcast episode and choose **Get Info…** (or press
Ctrl+I, or use Edit → Get Info…) to edit its title, artist, album,
album artist, composer, genre, year, track and disc numbers, and
artwork. Select several to edit them together: fields marked *Mixed*
differ between them and are left alone unless you type into them.
Episodes show Show, Author, Year and Episode instead, and always stay
in Podcasts.

Edits are written into the files' own tags (MP3, M4A/M4B, FLAC, Ogg;
artwork into MP3/M4A), so Refresh Library keeps them. If the iPod is
connected, the copies already on it are updated too, so the next sync
doesn't copy an edited song a second time.

## Library folders, full screen, steadier album grid

- **Settings → General → Library Folders** lists every music, photo and
  video folder Refresh Library scans, marking ones that can't be found.
  Remove them (optionally taking their songs out of the library too),
  use **Remove Missing Folders**, or **Change Folder…** after moving one
  (songs keep their playlists and play counts).
- The window opens maximized, with its normal title bar and borders.
  F11 (or Edit → Full Screen) switches to borderless full screen and
  back; Settings → Appearance turns the maximized default off.
- Album cards are a fixed size and the grid is laid out at its real
  width from the first batch, so tiles no longer jump while loading or
  when selected. Only covers on screen fade in.

## Firmware: import and install

On the iPod's Summary page (View iPod…), the **Firmware** section has
**Import Firmware…**: pick Apple's restore file for a 5th-gen (Video)
iPod, an `.ipsw` containing a `Firmware-25.x.x` file (for example
`iPod_25.1.3.ipsw`), or that firmware file itself. It's checked, kept in
`~/.local/share/linuxpod/firmware/`, and can be installed on
the connected iPod right away. Installing only rewrites the firmware
partition; music on the iPod stays.

Installing writes to the iPod's disk directly, so it asks for your
password through `pkexec`. Before writing anything the app checks that
the target is a USB iPod with the normal two partitions and that the
music partition is the one it has mounted, and refuses otherwise. Only
5th-gen (Video, 5G/5.5G) iPods are supported.

## Video conversion only when asked

Refresh Library no longer scans video folders, so it never starts video
conversions. Videos are converted when you add them (Add Video
Folder/Add Video), and **Settings → Video → Scan Video Folders Now**
picks up and converts new videos in your tracked video folders.

## Renaming the iPod

iPod menu → **Rename iPod…**, or the **Rename…** button next to the
title on the iPod's Summary page. This changes the iPod's own name (the
one the iPod shows on its main menu and iTunes shows in its sidebar,
stored in the iPod's music database). When the system allows it
(udisks), the drive label the file manager shows is renamed too; drive
labels are uppercase and at most 11 characters, so "Max's iPod" becomes
"MAX'S IPOD". If the drive label can't be changed, the iPod's own name
is still changed and a note says so.

## Photos no longer sync twice

Two bugs made photos pile up on the iPod: every photo was put in the
iPod's Photo Library twice, and syncing a photo again added another
copy. Now a photo that's already on the iPod is skipped (matched by the
file's contents, so the same picture in two folders counts once), and
each sync also removes duplicate copies left by older versions. To
clean up without syncing anything, use iPod menu → **Remove Duplicate
Photos** or the **Remove Duplicates on iPod** button in Photos.

## Ctrl-click to deselect

In the album grid, Ctrl-click an album to add it to the selection or
take it out, leaving the other selected albums as they are. (Before,
Ctrl-clicking an album that was already part of a selection dropped
everything else instead.) Song lists already worked this way, and a
plain click on a photo or video toggles it.

## Spoofify

Spoofify keeps two playlists up to date from your play counts (counted
on the iPod and brought into the library each time it connects):

- **Spoofify: Not Played Much**: songs you've never played first, then
  the ones with the fewest plays. Songs tied on plays are shuffled, so
  you get a different mix each time it updates.
- **Spoofify: Most Played**: your highest play counts.

They update when the app starts and whenever new plays come in from the
iPod (before any auto-sync, so the iPod gets the new lists), and they
sync like any other playlist. Settings → **Spoofify** sets how many
songs each one holds (0 turns that one off), turns the feature off, and
has an **Update Now** button. Podcasts are left out.

## Unticking in View Connected Device removes it from the iPod

In View Connected Device, untick albums or songs as much as you like:
nothing comes off the iPod until you press **Done** or **Sync**. Then the
app lists the songs it's about to remove and asks before removing them.
Songs you still have ticked on their own, or in a ticked playlist, stay.
In this view a click on an album only ever flips that album.

## How the app knows which iPod song is which

Every song this app copies to the iPod is recorded with the iPod's own
ID for that copy, so removing a song removes exactly that copy and
nothing else. Songs that were already on the iPod are tied to a library
song only when exactly one library song has the same title, artist and
album (and nearly the same length); anything less certain is never
removed automatically - remove it from **On iPod** instead.

**Remove from iPod** now works from any screen: select songs (Songs,
playlists, artists, genres) or albums, or right-click an album, and it
removes their copies from the iPod after showing the list.

## Remove an album from the library

Right-click an album (or several selected albums) → **Remove from
Library**. The music files stay where they are on your computer, and
Refresh Library skips them from then on. Settings → General shows how
many songs are being skipped and has **Let Refresh Add Them Back** to
undo that. Adding a file by hand with Add Files also brings it back.

## Small fixes (October 19)

- **#** is the track number. On iPod now shows it, along with genre
  and year, read from the iPod itself.
- Last Played shows 12-hour time (e.g. 2024-01-02 3:58 PM) and still
  sorts by actual time.
- The Eject button on the iPod card stays visible with a long iPod name.
- Refresh Library (which also runs when the app starts) now removes
  songs whose files were deleted from your computer. Songs on a drive
  that isn't plugged in are left alone.

### Track numbers in Edit Album Info (November 8)

Right-click an album and choose **Edit Album Info…**. Below the album fields there's now a **Track numbers** list with every song in the album and a number box beside each one. Change the numbers and press Save: they're written into the files' own tags (as "3 of 14"), into the library, and onto the iPod's copies if it's plugged in, so the album plays and sorts in the right order everywhere.

Songs with no track number get one filled in from a number at the start of the title or file name, such as "(01) - Amish Paradise" or "03 Fat.mp3". A note says when that happened, so you can check before saving. A plain number that's part of the name, like "99 Luftballons", isn't treated as a track number. Single songs can still be numbered in Get Info (Ctrl+I) as before.

### Album art is back in the AppImage, and Settings title fix (November 8)

The last two AppImages (November 6) were built without the part of Pillow that shows pictures in the window, so album covers didn't load. The AppImage includes it again, and the build now stops with an error if it's ever missing. Also, leaving Settings by clicking a tab at the top (like Music) no longer leaves "Settings" as the page title and status.

### Ticked playlists only sync your ticked songs (November 6)

A ticked playlist in View Connected Device now only brings the songs of it that you've also ticked, as songs or as part of a ticked album. The playlist on the iPod holds just those songs, in the playlist's order. If none of a ticked playlist's songs are ticked, the whole playlist syncs, like before. The prompt that asked about playlist songs you hadn't ticked is gone, since this rule now decides it. Auto-sync on connect follows the same rule.

### Sync only adds what you ticked (October 20)

Two things could put songs on the iPod that you never ticked:

- **Ticked playlists.** Older versions ticked a playlist just by clicking on it in the Playlists screen, and a playlist's songs don't show as ticked anywhere else (Spoofify playlists also change by themselves). Sync used to list those playlists first and ask; since November 6 a ticked playlist only brings your ticked songs instead (see above).
- **Auto-sync on connect** copied your whole library. It now copies only what's ticked in View Connected Device (or everything if "Always sync all music" is on), and it never removes anything.

### Cleaning up leftover files (October 19)

Removals that didn't finish (before the fixes above) could leave song files on the iPod that aren't in its song list. They can't be played, they don't show in the app's song lists, and their space shows as **Other**. When an iPod connects, the app now checks for these, and for listed songs whose file is gone. If it finds any, it says how many and how much space, and asks before deleting them. You can also run it any time: **iPod > Clean Up Leftover Files…**, or the button on the iPod's Summary page. Your library isn't touched.

### Songs sort by title (October 19)

The Songs list (and On iPod's song list, and search results) now opens sorted by title, A to Z. Before, it was sorted by artist, then album, then track number. Click any column heading to sort by that instead. That now works wherever you've scrolled to: before, if you'd scrolled down, clicking a heading could select the song hidden behind it instead of sorting.

### Album and song ticks always match (October 19)

In View Connected Device, the Albums and Songs screens now stay in step. Ticking an album ticks all its songs on the Songs screen, and unticking an album unticks them, so they come off the iPod. Unticking one of an album's songs on the Songs screen unticks the album too, and the album's other songs stay ticked on their own. A song stays on the iPod only if something still has it ticked (the song itself, its album, or a ticked playlist).

### Picking playlists to sync (October 19)

In View Connected Device, click **Playlists** under Music in the sidebar. Each playlist has a tick box: click a row to tick or untick it (Ctrl+A ticks them all), then press Sync. Every ticked playlist goes onto the iPod as a real playlist with its songs, in the same order. Songs already on the iPod aren't copied again, they're just added to the playlist. If you change a ticked playlist in the app, the next Sync updates it on the iPod to match.

### Syncs that stalled, and "helper exited with code -15" (October 19)

A sync could sit there doing nothing and then show "helper exited with code -15" when you cancelled it. The app wasn't reading the warnings the helper prints, and once enough of them piled up the helper stopped mid-job while still holding the iPod, so the next job (your sync) waited behind it forever. The app now reads them as they come, so that can't happen. If a sync does have to wait for another iPod job, the status bar says so, and cancelling while it waits is a normal cancel instead of an error.

### Picking your iPod model (October 19)

If the app can't tell which iPod model is connected, it now asks, once per connection. You get a dropdown of every iPod model it knows, with a filter box above it: type something like "80 white video" or "MA448" to narrow the list, pick yours, and press Use This Model. Skip leaves it alone. The Set iPod Model button on the iPod's Summary page uses the same dropdown. The model number is on the iPod under Settings > About; it's not the A1xxx number printed on the back.

### Songs you remove can be synced straight back (October 19)

After removing songs from the iPod, syncing them again right away could be skipped with "already on iPod", because the app was still using its list of the iPod's songs from before the removal until it finished re-reading the iPod. It now forgets the removed songs as soon as the removal finishes.

### Removing songs no longer breaks syncing (October 19)

Removing songs could leave the iPod unable to sync until it was restored. The app sometimes read the iPod while it was removing songs, and the two jobs got in each other's way: both moved the iPod's SysInfoExtended file aside and back at the same time, so a write could fail with "Unsupported checksum type", and two writes at once could damage the iPod's song database. Now only one job touches the iPod at a time, and the next one waits for it to finish. Removed songs are also taken out of every playlist on the iPod, not just the song list.

### The Sync button in View Connected Device (October 19)

Sync now works from every screen in View Connected Device, including the Summary page you land on. It adds every song, album, and playlist you've ticked anywhere, not just what's ticked on the screen you're looking at, and removes copies of songs you've unticked, same as before. On the Movies, TV Shows, and Photos screens it still syncs those.
