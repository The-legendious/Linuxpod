# Linuxpod
A Itunes like Ipod syncing tool.

A *Vibe Coded* Ipod sync tool for linux, currently only tested with a 5.5 gen video.

Linuxpod is a music library and iPod sync app for Linux, in the spirit of iTunes. It keeps your music, movies, TV shows, photos and podcasts in one library and puts them on a click-wheel iPod.

It works with click-wheel iPods (Classic, Video, Nano, Shuffle). The iPod touch isn't supported. Back up your iPod's music before your first sync.

## Contents

- [Installing](#installing)
- [Building your library](#building-your-library)
- [Finding your way around](#finding-your-way-around)
- [Connecting your iPod](#connecting-your-ipod)
- [Choosing what goes on the iPod](#choosing-what-goes-on-the-ipod)
- [Removing things from the iPod](#removing-things-from-the-ipod)
- [Editing song and album info](#editing-song-and-album-info)
- [Playlists and play counts](#playlists-and-play-counts)
- [Movies and TV shows](#movies-and-tv-shows)
- [Photos](#photos)
- [Podcasts](#podcasts)
- [iPod tools](#ipod-tools)
- [Settings](#settings)
- [Shortcuts](#shortcuts)
- [Troubleshooting](#troubleshooting)
- [Building from source](#building-from-source)

## Installing

### AppImage (easiest)

Download `Linuxpod-x86_64.AppImage`, make it executable and run it:

    chmod +x Linuxpod-x86_64.AppImage
    ./Linuxpod-x86_64.AppImage

Everything the app needs is inside the file. A few optional tools come from your system if you want their features:

- **ffmpeg** and **AtomicParsley** to add videos
- **udisks2** to eject the iPod from the app
- **zenity** (or **kdialog** on KDE) for your desktop's own file pickers

If double-clicking the AppImage does nothing, install `libfuse2`, or run it with `--appimage-extract-and-run`.

### From the zip

On Debian or Ubuntu, save `linuxpod.zip` to your Downloads folder and run:

    unzip ~/Downloads/linuxpod.zip -d ~/Downloads/linuxpod
    bash ~/Downloads/linuxpod/setup_and_run.sh

This installs what the app needs (asking for your password), builds the small iPod helper program and starts the app. Run `setup_and_run.sh` again whenever you get a new zip.

On other distributions, install the equivalents of `python3-tk`, `libgpod-dev`, `pkg-config`, a C compiler, `ffmpeg`, `atomicparsley` and `udisks2`, then run these from the unzipped folder:

    pip3 install --user -r requirements.txt
    make -C helper
    python3 src/main.py

## Building your library

- **File → Add Folder to Library…** scans a folder of music and adds every song. **File → Add Files to Library…** adds individual files.
- The app remembers every folder you add. **Settings → General → Refresh Library** scans them again for new songs and changed tags. This also runs each time the app starts, and it drops songs whose files you've deleted.
- **Settings → General → Library Folders** lists your folders. Use it to remove a folder, or to point the app at a folder you've moved without losing playlists or play counts.
- To take an album out of the library without deleting its files, right-click it and choose **Remove from Library**.
- **Clear Library** removes everything from the library. It never touches your files or your iPod.

Supported music formats are MP3, M4A/AAC, M4B, FLAC, Ogg, WAV and AIFF. Cover art comes from the songs' embedded artwork, or from a `cover.jpg`, `folder.jpg` or similar image in the same folder.

## Finding your way around

- **Tabs at the top:** Music, Movies, TV Shows, Photos and Podcasts each have their own library.
- **Sidebar:** under Music you get Recently Added, Songs, Albums, Artists and Genres, followed by your playlists.
- **Search box:** at the top right. It searches whatever you're looking at.
- **Gear button:** opens Settings.
- **iPod panel:** at the bottom of the sidebar. It shows the connected iPod and its storage, with the Sync and Eject buttons.

Click any column heading to sort by it. Songs start sorted by title. The **#** column is the track number, and **Plays** and **Last Played** come from your iPod (see [play counts](#playlists-and-play-counts)).

The Albums view shows a grid of covers. Double-click an album to see its songs.

## Connecting your iPod

Plug in the iPod. The app finds it within a few seconds and mounts it if your desktop hasn't already. If it doesn't appear, use **iPod → Detect / Auto-Mount iPod**, or **iPod → Choose iPod Folder…** to point at where it's mounted.

If the app can't tell which iPod model you have, it asks. Pick yours from the list; typing something like "80 white video" or the model number narrows it down. The model number is on the iPod under **Settings → About** and starts with M (for example MA448). It isn't the A1xxx number on the back.

When you're done, press **Eject** on the iPod panel (or **iPod → Eject iPod**) before unplugging.

## Choosing what goes on the iPod

Open **View Connected Device** from the menu bar. It's greyed out until an iPod is connected. This is where you decide what the iPod holds.

- **Summary** shows the iPod's name, model, what's on it and how much space is free, plus the sync options and iPod tools.
- **Music** (Songs, Albums, Artists, Genres, Playlists) shows your library with a tick box on everything. Tick what you want on the iPod.
- **Movies, TV Shows, Photos and Podcasts** work the same way.
- **On My Device** shows what's actually on the iPod right now.

Then press **Sync** at the bottom. The iPod is made to match what you ticked. Ticked items are copied over, and anything already on the iPod isn't copied again. Before anything is removed, the app lists it and asks you.

How ticks work:

- Ticking an album ticks all its songs, and unticking it unticks them. Unticking one song from an album unticks the album, and its other songs stay ticked.
- A ticked playlist goes on the iPod as a playlist. If you've also ticked some of its songs, only those are synced. If none of its songs are ticked, the whole playlist is synced.
- Your ticks are saved, so they're still there the next time you open the app or connect the iPod.
- Nothing is removed while you tick and untick. Removals only happen when you press **Done** or **Sync**, after you confirm them.

Sync options, on the Summary page:

- **Automatically sync ticked music whenever this iPod is connected** copies anything newly ticked each time you plug in. It never removes anything.
- **Always sync all music to this iPod, not just what's selected** makes the Sync button copy your whole music library.

Outside View Connected Device, you can also select songs anywhere and press **Sync Selected → iPod**, or drag songs or albums onto the iPod in the sidebar. **Cancel Sync** stops after the current song and keeps everything copied so far.

## Removing things from the iPod

- Untick it in View Connected Device and press **Sync** or **Done**.
- Or select songs or albums anywhere and choose **Remove from iPod**. You see the list before anything is removed.

The app only ever removes the exact copies it knows belong to those songs. Songs that were on the iPod before you used Linuxpod are only removed when the app can match them to one library song for certain. Otherwise, remove them from **On My Device → Music** yourself.

## Editing song and album info

- **Get Info:** right-click a song and choose **Get Info…**, or press Ctrl+I. You can edit the title, artist, album, album artist, composer, genre, year, track and disc numbers, and artwork. Select several songs to change them together. Fields marked *Mixed* are left alone unless you type in them.
- **Edit Album Info:** right-click an album and choose **Edit Album Info…** to change the album name, artist, genre, year and cover for all its songs at once. **Search Internet for Album Info…** looks the album up on the iTunes Store and fills in the details and cover. The **Track numbers** list sets the order of the album's songs. Blank numbers are filled in from titles like "(01) - Song", so check them before saving.

Changes are saved into the music files themselves, so a Refresh Library keeps them. If the iPod is connected, the copies on it are updated too.

## Playlists and play counts

- Make a playlist with **New Playlist**, then add songs by right-clicking them and choosing **Add to Playlist…**, or by dragging them onto the playlist in the sidebar.
- Right-click a playlist in the sidebar to delete it. Its songs stay in your library.
- **Play counts:** every time the iPod connects, the plays you've done on it are added to the Plays and Last Played columns. Plays are only ever added, never counted twice.
- **Spoofify** keeps two automatic playlists up to date from your play counts. **Spoofify: Not Played Much** has songs you rarely play, shuffled, and **Spoofify: Most Played** has your favourites. Set their size, or turn them off, in **Settings → Spoofify**.

## Movies and TV shows

Switch to the **Movies** or **TV Shows** tab and use **Add Video Folder…** or **Add Video…**. Videos are converted to a format the iPod can play when you add them, which needs ffmpeg and AtomicParsley. The first time, the app asks where to keep the converted copies.

TV episodes named like `Show Name - S01E02 - Title.mp4` get their show, season and episode filled in automatically. On the iPod, movies go under Movies and episodes under TV Shows.

**Settings → Video Folders → Scan Video Folders Now** picks up new videos in folders you've already added.

## Photos

Switch to the **Photos** tab and use **Add Photo Folder…**. You can give the folder an album name for the iPod. Photos that are already on the iPod are skipped when you sync, and **iPod → Remove Duplicate Photos** cleans up any doubles.

## Podcasts

Switch to the **Podcasts** tab and use **File → Add Podcast from URL…** with the show's feed address. New episodes download automatically. On the iPod, episodes go in the Podcasts menu and remember where you stopped.

## iPod tools

These are on the iPod's Summary page and in the **iPod** menu:

- **Rename iPod…** changes the name the iPod shows.
- **Set iPod Model…** tells the app your iPod's model if it was detected wrong. On some iPods this is also what makes cover art show up.
- **Refresh iPod Library** re-reads what's on the iPod.
- **Clean Up Leftover Files…** finds song files on the iPod that aren't in its song list (they waste space and show as "Other") and offers to delete them.
- **Wipe iPod Library** removes all music from the iPod. It doesn't touch the iPod's firmware.
- **Import Firmware…** (5th-generation iPod Video only) installs Apple's firmware from an `.ipsw` file without erasing your music.

## Settings

Open Settings with the gear button or **Edit → Settings…**.

- **General:** Refresh Library, Remove Duplicate Tracks, your library folders, and albums you've removed from the library.
- **Video Folders:** where converted videos go and which folders are scanned.
- **Podcasts:** download options.
- **Spoofify:** the automatic playlists.
- **Appearance:** whether the window opens maximized, and the size of album, video and photo thumbnails.

## Shortcuts

| Action | Shortcut |
|---|---|
| Select everything in the list | Ctrl+A |
| Get Info | Ctrl+I |
| Full screen | F11 |
| Add or remove one item from a selection | Ctrl+click |
| Open an album | Double-click |
| More options | Right-click |

## Troubleshooting

- **The log:** `~/.local/share/linuxpod/app.log` records what the app does and any errors. Open it with **Help → Open Log File**. If the app crashes, also look at `crash.log` in the same folder. Your library and settings are kept in that folder too.
- **The iPod isn't found:** check that it shows up in your file manager, then try **iPod → Detect / Auto-Mount iPod**.
- **Sync fails with "Couldn't find the iPod firewire ID":** iPods that sign their song database (Nano 3rd generation and later, Classic) need their FireWire ID. Linuxpod adds it to the iPod's `iPod_Control/Device/SysInfo` file by itself, from the iPod's USB serial number. If it still fails, check that `lsblk -o NAME,SERIAL` shows a 16-character serial for the iPod, or add the line `FirewireGuid: 0x` followed by that serial to SysInfo yourself.
- **Songs don't appear on an iPod Classic (6th/7th generation) or Nano (5th generation and later):** these use an extra database checksum. Try the newest libgpod your distribution offers.
- **No cover art on the iPod:** use **Set iPod Model…** on the Summary page so the app knows exactly which iPod you have.
- **Something went wrong on the iPod:** the app never formats the iPod. At worst its music database needs rebuilding, which a restore with Apple's tools (or gtkpod) fixes.

## Building from source

Linuxpod is written in Python with Tkinter (`src/`). A small C helper (`helper/ipod_helper.c`) talks to the iPod through [libgpod](https://sourceforge.net/projects/gtkpod/). `install.sh` installs the dependencies and builds the helper.

To build the AppImage, run `packaging/build_appimage.sh` on Ubuntu 24.04 or newer. It needs `python3.12 python3-tk python3-pil python3-pil.imagetk python3-mutagen libgpod-dev pkg-config build-essential rsync` and [appimagetool](https://github.com/AppImage/appimagetool).

    src/               the app
      main.py            starts it
      gui.py             the window
      library.py         the library database
      scanner.py         reads tags from music files
      ipod_backend.py    talks to the helper
    helper/            the libgpod helper (C)
    packaging/         AppImage build script
    install.sh         installs dependencies and builds the helper
    setup_and_run.sh   unzips, installs and starts the app
