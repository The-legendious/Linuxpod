/*
 * ipod_helper.c
 *
 * A small command-line helper that talks to libgpod so the Python GUI
 * never has to guess libgpod's internal C struct layout itself (which
 * would be fragile to do from Python via ctypes). Instead, this file is
 * compiled locally against whatever libgpod-dev headers are actually
 * installed on your machine, so field offsets are always correct for
 * your system.
 *
 * libgpod itself does NOT read audio tags (title/artist/etc) - it only
 * manages the iPod's on-disk database and copies files into place. Tag
 * reading happens in Python (via mutagen) and is passed in through the
 * manifest file this program reads.
 *
 * Usage:
 *   ipod_helper list            <mountpoint>
 *   ipod_helper sync            <mountpoint> <manifest-file>
 *   ipod_helper remove          <mountpoint> <manifest-file>
 *   ipod_helper remove-playlist <mountpoint> <playlist-name>
 *   ipod_helper fix-podcasts    <mountpoint>
 *
 * Manifest file format (tab-separated, one record per line):
 *   TRACK<TAB>filepath<TAB>title<TAB>artist<TAB>album<TAB>genre<TAB>composer
 *         <TAB>track_nr<TAB>track_total<TAB>year<TAB>duration_ms<TAB>bitrate
 *         <TAB>samplerate<TAB>size_bytes<TAB>filetype<TAB>playlists(csv)
 *         <TAB>artwork_image_path(optional, empty if none)
 *   VIDEO<TAB>filepath<TAB>title<TAB>duration_ms<TAB>size_bytes<TAB>kind
 *         <TAB>show_name(tvshow only)<TAB>season_nr(tvshow only)
 *         <TAB>episode_nr(tvshow only)
 *         (kind is "movie" or "tvshow" for the app's own local library
 *         organization only - on the device, every video is tagged as
 *         a Movie (mediatype 0x02 + movie_flag) regardless of kind,
 *         since classic iPods handle the separate TV Show mediatype
 *         inconsistently; show_name, when given, also becomes an
 *         on-device playlist name so a TV show's episodes stay grouped
 *         and easy to find/play in order - see cmd_sync's VIDEO
 *         handling for the exact values and why)
 *   REMOVE<TAB>track_dbid
 *
 * sync-photos uses its own manifest, passed as a separate invocation
 * (photos live in a completely separate on-device database from
 * music/video):
 *   PHOTO<TAB>filepath<TAB>album_name(optional, empty for none)
 *
 * Progress / results are printed to stdout, one event per line, so the
 * Python side can show a progress bar:
 *   PROGRESS<TAB>current<TAB>total<TAB>title
 *   ERROR<TAB>message
 *   DONE<TAB>added_count
 *   TRACKROW<TAB>dbid<TAB>title<TAB>artist<TAB>album<TAB>duration_ms
 *         <TAB>size_bytes<TAB>mediatype<TAB>tvshow<TAB>season_nr<TAB>episode_nr
 *         <TAB>playcount<TAB>time_played(unix seconds, 0 = never)
 *         (playcount is the iPod's running total - libgpod has already
 *         folded in the device's "Play Counts" file of plays since the
 *         last sync by the time itdb_parse returns; the Python side
 *         keeps its own per-track baseline and only adds the difference
 *         to the local library, so reading the same device twice never
 *         counts a play twice)
 *         (mediatype: see cmd_sync's VIDEO handling above for values -
 *         0/1 is audio, 0x02 is Movie, 0x40 is TV Show; tvshow/season_nr/
 *         episode_nr are only meaningful when mediatype is 0x40)
 *   PLAYLISTROW<TAB>name<TAB>track_count<TAB>is_master(0/1)
 *   PLAYLISTTRACK<TAB>playlist_name<TAB>track_dbid  (one per track per
 *         non-master playlist, right after that playlist's PLAYLISTROW -
 *         lets the Python side actually filter "On iPod" by playlist,
 *         instead of just showing every track regardless of which one
 *         was clicked)
 *   PHOTOALBUMROW<TAB>album_name<TAB>photo_count<TAB>is_master(0/1)
 *         (list only - one row per on-device photo album, including the
 *         required master "Photo Library"; no per-photo thumbnail data,
 *         see cmd_list's photo-listing comment for why)
 *   INFO<TAB>key<TAB>value
 *
 * Cancellation: `sync` catches SIGTERM (what Python's Popen.terminate()
 * sends) and finishes the track currently being copied, then writes the
 * database with everything done so far and exits normally - the same
 * clean state as a completed sync, just shorter. It does NOT stop mid
 * write and never leaves a half-written iTunesDB. `remove` and `list`
 * don't need this - they're fast enough not to warrant cancellation.
 */

#include <gpod/itdb.h>
#include <gdk-pixbuf/gdk-pixbuf.h>
#include <glib.h>
#include <glib/gstdio.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <signal.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/file.h>

#define MAX_LINE 8192
#define MAX_FIELDS 32

static char *fields[MAX_FIELDS];
static int nfields;

/* Set by the SIGTERM handler; only ever written 0->1 from a signal
   context and read from normal code, so a plain sig_atomic_t is safe
   without further locking. */
static volatile sig_atomic_t g_cancel_requested = 0;

static void handle_cancel_signal(int sig) {
    (void) sig;
    g_cancel_requested = 1;
}

static void split_line(char *line) {
    /* NOTE: this must NOT use strtok_r() - strtok_r treats a run of
       delimiters as one, silently dropping empty fields. In a
       tab-separated manifest line, an empty field (no genre, no
       composer, blank track_total - all common) is meaningful and must
       still count as a field, or every column after it shifts left by
       one. That was a real bug: it could put the artwork path (or any
       later field) in the wrong slot whenever an earlier field was
       blank, without ever raising an error - it just silently read the
       wrong data. */
    nfields = 0;
    char *start = line;
    char *p = line;
    for (;;) {
        if (*p == '\t') {
            *p = '\0';
            if (nfields < MAX_FIELDS) fields[nfields++] = start;
            start = p + 1;
            p = start;
        } else if (*p == '\0') {
            if (nfields < MAX_FIELDS) fields[nfields++] = start;
            break;
        } else {
            p++;
        }
    }
}

static gchar *dup_or_null(const char *s) {
    if (!s || s[0] == '\0') return NULL;
    return g_strdup(s);
}

static gint32 parse_int(const char *s, gint32 dflt) {
    if (!s || s[0] == '\0') return dflt;
    return (gint32) atol(s);
}

/* Find (or lazily create + register) a playlist by name. */
static Itdb_Playlist *find_or_create_playlist(Itdb_iTunesDB *itdb, const char *name) {
    for (GList *it = itdb->playlists; it != NULL; it = it->next) {
        Itdb_Playlist *pl = (Itdb_Playlist *) it->data;
        /* Never the master playlist (every song), Podcasts or a smart
           playlist, even if a playlist happens to share its name. */
        if (itdb_playlist_is_mpl(pl) || itdb_playlist_is_podcasts(pl) || pl->is_spl) continue;
        if (pl->name && strcmp(pl->name, name) == 0) {
            return pl;
        }
    }
    Itdb_Playlist *pl = itdb_playlist_new(name, FALSE);
    itdb_playlist_add(itdb, pl, -1);
    return pl;
}

static void add_track_to_playlists(Itdb_iTunesDB *itdb, Itdb_Track *track, const char *csv) {
    if (!csv || csv[0] == '\0') return;
    char *copy = g_strdup(csv);
    char *save = NULL;
    char *tok = strtok_r(copy, ",", &save);
    while (tok) {
        while (*tok == ' ') tok++;
        if (*tok) {
            Itdb_Playlist *pl = find_or_create_playlist(itdb, tok);
            itdb_playlist_add_track(pl, track, -1);
        }
        tok = strtok_r(NULL, ",", &save);
    }
    g_free(copy);
}

/* Podcast episodes (the app stores them with genre "Podcast") go in the
   iPod's own Podcasts menu: mediatype PODCAST, a member of the special
   Podcasts playlist, and NOT of the master playlist - libgpod's docs:
   "member tracks in the Podcasts playlist are not normally members of
   the MPL (so on the iPod they will only show up under the Podcasts
   menu)". Episodes are grouped there by album (= the show's name). */
static gboolean is_podcast_genre(const char *genre) {
    return genre && g_ascii_strcasecmp(genre, "Podcast") == 0;
}

static Itdb_Playlist *podcasts_playlist(Itdb_iTunesDB *itdb) {
    Itdb_Playlist *pl = itdb_playlist_podcasts(itdb);
    if (!pl) {
        pl = itdb_playlist_new("Podcasts", FALSE);
        itdb_playlist_set_podcasts(pl);
        itdb_playlist_add(itdb, pl, -1);
    }
    return pl;
}

/* Takes a track out of every playlist (the master one included) and
   then out of the database, so no playlist is left pointing at it. */
static void detach_track(Itdb_iTunesDB *itdb, Itdb_Track *t) {
    for (GList *l = itdb->playlists; l; l = l->next) {
        Itdb_Playlist *pl = (Itdb_Playlist *) l->data;
        while (itdb_playlist_contains_track(pl, t))
            itdb_playlist_remove_track(pl, t);
    }
    itdb_track_unlink(t);
}

static void make_podcast(Itdb_iTunesDB *itdb, Itdb_Track *track, gboolean new_episode) {
    track->mediatype = ITDB_MEDIATYPE_PODCAST;
    track->remember_playback_position = 0x01;
    track->skip_when_shuffling = 0x01;
    track->flag4 = 0x01;
    if (new_episode) track->mark_unplayed = 0x02;
    Itdb_Playlist *mpl = itdb_playlist_mpl(itdb);
    if (mpl && itdb_playlist_contains_track(mpl, track))
        itdb_playlist_remove_track(mpl, track);
    Itdb_Playlist *pc = podcasts_playlist(itdb);
    if (!itdb_playlist_contains_track(pc, track))
        itdb_playlist_add_track(pc, track, -1);
}

/* Episodes synced by older versions went over as music; move them to
   the Podcasts menu. Returns how many were fixed. */
static int fix_podcasts_synced_as_music(Itdb_iTunesDB *itdb) {
    int fixed = 0;
    for (GList *l = itdb->tracks; l; l = l->next) {
        Itdb_Track *t = l->data;
        if (is_podcast_genre(t->genre) && !(t->mediatype & ITDB_MEDIATYPE_PODCAST)) {
            make_podcast(itdb, t, FALSE);
            fixed++;
        }
    }
    return fixed;
}

/* ---- SysInfoExtended files libgpod can't use --------------------------

   libgpod picks the iTunesDB checksum from SysInfoExtended's DBVersion
   (0-2 none, 3-5 the Classic/nano/touch hashes) and refuses to write the
   database at all for any other value ("Unsupported checksum type").
   Other iPod tools sometimes write their own SysInfoExtended with an
   iTunesDB version number there instead (seen: iOpenPod writing 25 on a
   5.5-gen iPod Video, which needs no checksum). For such a file, this
   helper moves it aside for the duration of the run, so libgpod
   identifies the iPod from SysInfo instead, and puts it back on exit
   (or at the start of the next run, if this one was killed). */

static gchar *g_sie_path = NULL;
static gchar *g_sie_hidden_path = NULL;
static gboolean g_sie_hidden = FALSE;

static void restore_sysinfo_extended(void) {
    if (g_sie_hidden && g_sie_path && g_sie_hidden_path) {
        if (g_rename(g_sie_hidden_path, g_sie_path) == 0) g_sie_hidden = FALSE;
    }
}

/* Value of <key>name</key> followed by <integer>/<string> in a plist. */
static gchar *plist_value(const gchar *xml, const char *name) {
    gchar *key = g_strdup_printf("<key>%s</key>", name);
    const gchar *p = strstr(xml, key);
    g_free(key);
    if (!p) return NULL;
    p = strchr(p + 5, '>');           /* end of </key> */
    if (!p) return NULL;
    p = strchr(p + 1, '>');           /* end of <integer> / <string> */
    if (!p) return NULL;
    const gchar *end = strchr(p + 1, '<');
    if (!end) return NULL;
    return g_strstrip(g_strndup(p + 1, end - p - 1));
}

static gboolean model_is_known(Itdb_Device *dev) {
    const Itdb_IpodInfo *info = itdb_device_get_ipod_info(dev);
    return info && info->ipod_generation != ITDB_IPOD_GENERATION_UNKNOWN;
}

/* Only one helper may touch an iPod at a time. Two at once (say, the
   app reading the iPod while it removes songs) both move
   SysInfoExtended aside and back, so one of them can end up writing
   with the bad file in place ("Unsupported checksum type"), and two
   database writes at once can leave a broken iTunesDB. Every command
   takes this lock first and holds it until it exits. The lock file
   lives in the temp folder, not on the iPod. */
static void lock_device(const char *mountpoint) {
    gchar *real = realpath(mountpoint, NULL);
    gchar *sum = g_compute_checksum_for_string(G_CHECKSUM_SHA1, real ? real : mountpoint, -1);
    free(real);
    gchar *name = g_strdup_printf("linuxpod-%.16s.lock", sum);
    gchar *path = g_build_filename(g_get_tmp_dir(), name, NULL);
    int fd = open(path, O_RDWR | O_CREAT, 0666);
    if (fd >= 0) {
        if (flock(fd, LOCK_EX | LOCK_NB) != 0) {
            printf("INFO	waiting	another iPod job is still running\n");
            fflush(stdout);
            flock(fd, LOCK_EX);   /* released when that one exits */
        }
        /* fd stays open, holding the lock, until this process exits */
    }
    g_free(path);
    g_free(name);
    g_free(sum);
}

static void prepare_device(const char *mountpoint) {
    gchar *dev_dir = itdb_get_device_dir(mountpoint);
    if (!dev_dir) return;
    g_sie_path = g_build_filename(dev_dir, "SysInfoExtended", NULL);
    g_sie_hidden_path = g_build_filename(dev_dir, "SysInfoExtended.musicmanager-hidden", NULL);
    g_free(dev_dir);

    if (!g_file_test(g_sie_path, G_FILE_TEST_EXISTS) &&
        g_file_test(g_sie_hidden_path, G_FILE_TEST_EXISTS)) {
        g_rename(g_sie_hidden_path, g_sie_path);   /* an earlier run was killed */
    }

    gchar *xml = NULL;
    gchar *ext_model = NULL;
    if (g_file_get_contents(g_sie_path, &xml, NULL, NULL)) {
        gchar *dbv = plist_value(xml, "DBVersion");
        ext_model = plist_value(xml, "ModelNumStr");
        int db_version = dbv ? atoi(dbv) : 0;
        if (db_version > 5) {
            if (g_rename(g_sie_path, g_sie_hidden_path) == 0) {
                g_sie_hidden = TRUE;
                atexit(restore_sysinfo_extended);
                printf("INFO\tsysinfo_extended_ignored\tDBVersion %d isn't a real iPod value; "
                       "identifying the iPod from SysInfo instead\n", db_version);
                fflush(stdout);
            }
        }
        g_free(dbv);
        g_free(xml);
    }

    /* SysInfo's ModelNumStr must be Apple's order number with its first
       letter replaced (libgpod skips it): "xA448" for MA448. If it's
       unrecognized (e.g. the "A1136" printed on the back of the case)
       and SysInfoExtended names the real one, correct SysInfo. */
    Itdb_Device *dev = itdb_device_new();
    itdb_device_set_mountpoint(dev, mountpoint);
    if (!model_is_known(dev) && ext_model && strlen(ext_model) >= 2) {
        gchar *old = itdb_device_get_sysinfo(dev, "ModelNumStr");
        gchar *fixed = g_strdup_printf("x%s", ext_model + 1);
        itdb_device_set_sysinfo(dev, "ModelNumStr", fixed);
        if (model_is_known(dev) && itdb_device_write_sysinfo(dev, NULL)) {
            printf("INFO\tmodel_fixed\tModelNumStr '%s' -> '%s'\n", old ? old : "", fixed);
            fflush(stdout);
        }
        g_free(fixed);
        g_free(old);
    }
    itdb_device_free(dev);
    g_free(ext_model);
}

static int cmd_list(const char *mountpoint) {
    GError *error = NULL;
    Itdb_iTunesDB *itdb = itdb_parse(mountpoint, &error);
    if (!itdb) {
        printf("INFO\tempty\t1\n");
        if (error) g_error_free(error);
        fflush(stdout);
        return 0;
    }

    for (GList *it = itdb->tracks; it != NULL; it = it->next) {
        Itdb_Track *t = (Itdb_Track *) it->data;
        printf("TRACKROW\t%" G_GUINT64_FORMAT "\t%s\t%s\t%s\t%d\t%u\t%u\t%s\t%u\t%u\t%u\t%lld\t%s\t%d\t%d\n",
               t->dbid,
               t->title ? t->title : "",
               t->artist ? t->artist : "",
               t->album ? t->album : "",
               t->tracklen,
               (unsigned int) t->size,
               (unsigned int) t->mediatype,
               t->tvshow ? t->tvshow : "",
               (unsigned int) t->season_nr,
               (unsigned int) t->episode_nr,
               (unsigned int) t->playcount,
               (long long) t->time_played,
               t->genre ? t->genre : "",
               t->track_nr,
               t->year);
    }

    for (GList *it = itdb->playlists; it != NULL; it = it->next) {
        Itdb_Playlist *pl = (Itdb_Playlist *) it->data;
        if (pl->is_spl) continue;
        int count = g_list_length(pl->members);
        int is_mpl = itdb_playlist_is_mpl(pl) ? 1 : 0;
        printf("PLAYLISTROW\t%s\t%d\t%d\n", pl->name ? pl->name : "",
               count, is_mpl);

        if (!is_mpl) {
            for (GList *mit = pl->members; mit != NULL; mit = mit->next) {
                Itdb_Track *mt = (Itdb_Track *) mit->data;
                printf("PLAYLISTTRACK\t%s\t%" G_GUINT64_FORMAT "\n",
                       pl->name ? pl->name : "", mt->dbid);
            }
        }
    }

    itdb_free(itdb);

    /* Photos live in a completely separate database (see sync-photos'
       comment above) - read it too here so "list" gives a full picture
       of what's on the device. Deliberately not attempting to extract
       actual thumbnail image data back out (that needs gdk-pixbuf
       integration this helper doesn't have) - just album names and
       counts, which is enough for an on-device summary view. */
    GError *photo_error = NULL;
    Itdb_PhotoDB *photodb = itdb_photodb_parse(mountpoint, &photo_error);
    if (photodb) {
        for (GList *it = photodb->photoalbums; it != NULL; it = it->next) {
            Itdb_PhotoAlbum *album = (Itdb_PhotoAlbum *) it->data;
            int count = g_list_length(album->members);
            printf("PHOTOALBUMROW\t%s\t%d\t%d\n",
                   album->name ? album->name : "", count,
                   album->album_type == 0x01 ? 1 : 0);
        }
        itdb_photodb_free(photodb);
    } else if (photo_error) {
        g_error_free(photo_error);
    }

    printf("DONE\t0\n");
    fflush(stdout);
    return 0;
}

/* Playlists ticked in the app, from the sync manifest:
     PLSET<TAB>name             starts a playlist (its old songs are cleared)
     PLITEM<TAB>dbid<TAB>id     a song already on the iPod
     PLITEM<TAB>path<TAB>file   a song copied over in this same sync
   Each playlist ends up with exactly these songs, in this order. */
static void apply_playlist_sets(Itdb_iTunesDB *itdb, const char *manifest_path,
                                GPtrArray *added_tracks, GPtrArray *added_sources) {
    FILE *fp = fopen(manifest_path, "r");
    if (!fp) return;
    char line[MAX_LINE];
    Itdb_Playlist *pl = NULL;
    int sets = 0;
    while (fgets(line, sizeof(line), fp)) {
        char *nl = strchr(line, '\n');
        if (nl) *nl = '\0';
        if (strncmp(line, "PLSET\t", 6) == 0) {
            const char *name = line + 6;
            if (!*name) { pl = NULL; continue; }
            pl = find_or_create_playlist(itdb, name);
            while (pl->members)
                itdb_playlist_remove_track(pl, (Itdb_Track *) pl->members->data);
            sets++;
        } else if (pl && strncmp(line, "PLITEM\t", 7) == 0) {
            char *kind = line + 7;
            char *value = strchr(kind, '\t');
            if (!value) continue;
            *value++ = '\0';
            Itdb_Track *track = NULL;
            if (strcmp(kind, "dbid") == 0) {
                guint64 dbid = g_ascii_strtoull(value, NULL, 10);
                for (GList *it = itdb->tracks; it; it = it->next) {
                    if (((Itdb_Track *) it->data)->dbid == dbid) { track = it->data; break; }
                }
            } else if (strcmp(kind, "path") == 0) {
                for (guint i = 0; i < added_sources->len && i < added_tracks->len; i++) {
                    if (strcmp((const char *) g_ptr_array_index(added_sources, i), value) == 0) {
                        track = g_ptr_array_index(added_tracks, i);
                        break;
                    }
                }
            }
            if (track && !itdb_playlist_contains_track(pl, track))
                itdb_playlist_add_track(pl, track, -1);
        }
    }
    fclose(fp);
    if (sets > 0) {
        printf("INFO\tplaylists_set\t%d\n", sets);
        fflush(stdout);
    }
}

static int cmd_sync(const char *mountpoint, const char *manifest_path) {
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = handle_cancel_signal;
    sigaction(SIGTERM, &sa, NULL);

    GError *error = NULL;
    Itdb_iTunesDB *itdb = itdb_parse(mountpoint, &error);
    if (!itdb) {
        if (error) g_error_free(error);
        itdb = itdb_new();
        itdb_set_mountpoint(itdb, mountpoint);
    }

    /* Make sure device info (model/generation) is populated - needed
       for correctly-sized artwork thumbnails, and for the model to be
       recognized at all on iPod Classic / Video Nano and later models.
       itdb_parse() usually already does this, but we do it explicitly
       too (harmless if repeated), including for a brand new database. */
    itdb_device_read_sysinfo(itdb->device);

    /* Real diagnostic, not another guess: report what libgpod actually
       thinks this device is, so a failure can be diagnosed from real
       data next time instead of more speculation. gtkpod (confirmed
       working on this exact device) reads the same SysInfo through the
       same libgpod - if artwork_supported reports false here despite
       that, or the model string is empty, that's a concrete, checkable
       fact rather than a hypothesis. */
    {
        gchar *seen_model = itdb_device_get_sysinfo(itdb->device, "ModelNumStr");
        gboolean supports = itdb_device_supports_artwork(itdb->device);
        printf("INFO\tdevice_diagnostic\tModelNumStr='%s' artwork_supported=%s\n",
               seen_model ? seen_model : "(empty)", supports ? "yes" : "no");
        fflush(stdout);
        g_free(seen_model);
    }

    /* Make sure the on-device Artwork folder actually exists. Real
       iTunes creates iPod_Control/Artwork/ the first time it ever
       syncs artwork to a device; a device that's never had artwork
       synced through iTunes (or that's had its music library rebuilt
       by another tool) may not have this folder at all. If itdb_write()
       expects the folder to already be there and doesn't create it
       itself, individual tracks can still get artwork data attached
       successfully in memory while ArtworkDB itself never actually
       gets written to disk - this makes sure that can't be the reason. */
    {
        gchar *artwork_dir = g_build_filename(mountpoint, "iPod_Control", "Artwork", NULL);
        g_mkdir_with_parents(artwork_dir, 0755);
        g_free(artwork_dir);
    }

    Itdb_Playlist *mpl = itdb_playlist_mpl(itdb);
    if (!mpl) {
        mpl = itdb_playlist_new("iPod", FALSE);
        itdb_playlist_set_mpl(mpl);
        itdb_playlist_add(itdb, mpl, 0);
    }

    int podcasts_fixed = fix_podcasts_synced_as_music(itdb);
    if (podcasts_fixed > 0) {
        printf("INFO\tpodcasts_fixed\t%d\n", podcasts_fixed);
        fflush(stdout);
    }

    FILE *fp = fopen(manifest_path, "r");
    if (!fp) {
        printf("ERROR\tcould not open manifest file\n");
        fflush(stdout);
        return 1;
    }

    /* First pass: count TRACK and VIDEO lines for progress reporting. */
    int total = 0;
    char line[MAX_LINE];
    while (fgets(line, sizeof(line), fp)) {
        if (strncmp(line, "TRACK\t", 6) == 0 || strncmp(line, "VIDEO\t", 6) == 0) total++;
    }
    rewind(fp);

    int current = 0;
    int added = 0;
    /* Tracks added in *this* sync (not the whole device library, which
       itdb->tracks also holds) - if the final itdb_write() below fails,
       these are rolled back (files removed, unlinked) so a write
       failure never leaves copied files sitting on the device with no
       working database entry for them. */
    GPtrArray *added_tracks = g_ptr_array_new();
    /* Source file of each added track (same order), reported back with
       the track's dbid after the write so the app knows exactly which
       iPod copy is which library song. */
    GPtrArray *added_sources = g_ptr_array_new_with_free_func(g_free);
    int art_queued = 0;
    while (fgets(line, sizeof(line), fp)) {
        if (g_cancel_requested) {
            printf("INFO\tcancelled\t1\n");
            fflush(stdout);
            break;
        }

        char *nl = strchr(line, '\n');
        if (nl) *nl = '\0';

        if (strncmp(line, "VIDEO\t", 6) == 0) {
            split_line(line);
            /* fields[0]=="VIDEO", [1]=filepath, [2]=title,
               [3]=duration_ms, [4]=size_bytes, [5]=kind (movie|tvshow -
               kept only for the app's own local library organization -
               see below for why the device no longer treats these
               differently), [6]=show_name (tvshow only - also doubles
               as an on-device playlist name, see below),
               [7]=season_nr, [8]=episode_nr (both unused on-device now,
               kept in the manifest for forward-compat/local metadata) */
            if (nfields < 6) continue;

            const char *filepath = fields[1];
            const char *title = fields[2];

            current++;
            printf("PROGRESS\t%d\t%d\t%s\n", current, total, title);
            fflush(stdout);

            Itdb_Track *track = itdb_track_new();
            track->title = dup_or_null(title);
            track->tracklen = parse_int(fields[3], 0);
            track->size = (guint32) parse_int(fields[4], 0);
            track->filetype = g_strdup("MP4 video file");
            /* Both mediatype and movie_flag are confirmed directly from
               libgpod's own itdb.h field documentation for Itdb_Track:
               "mediatype must be set to 0x00000002 for video files...
               0x40 - TV Show (shows up ONLY in TV Shows)", and
               "movie_flag: Whether the track is a movie. True if set
               to 0x01".

               This used to branch on kind - 0x02+movie_flag for
               movies, the separate 0x40 TV Show mediatype for TV
               episodes - matching how real iTunes tags things. In
               practice classic iPods (5th/5.5-gen) are inconsistent
               about the TV Show menu: some units file 0x40 tracks
               under Movies anyway, some show them under TV Shows but
               with broken/missing metadata, which is exactly the
               "movies show up in TV Shows" confusion this was causing.
               Movies (0x02 + movie_flag) is the one combination every
               unit handles correctly and consistently, so *all* video
               - movies and TV episodes alike - is tagged that way on
               the device now. TV episodes keep their own "kind" in
               this app's local library (so Movies/TV Shows stay
               separate tabs here), they just all land in the same
               on-device Movies menu - grouped into a same-named
               playlist below so a whole show is still easy to find
               and play in order on the iPod itself. */
            track->mediatype = 0x00000002;
            track->movie_flag = 0x01;

            itdb_track_add(itdb, track, -1);

            /* fields[6] (show_name) doubles as an on-device playlist
               name for TV episodes - since they no longer get their
               own TV Shows menu entry (see above), a playlist named
               after the show is what makes "play the next episode"
               actually convenient on the device. A single name, not a
               CSV list, so this calls find_or_create_playlist directly
               instead of add_track_to_playlists' comma-splitting. */
            if (nfields > 6 && fields[6][0] != '\0') {
                Itdb_Playlist *show_pl = find_or_create_playlist(itdb, fields[6]);
                itdb_playlist_add_track(show_pl, track, -1);
            }
            /* Deliberately NOT adding to mpl (the master playlist) -
               libgpod's own docs describe the MPL as specifically
               "all tracks accessible on the iPod through the
               Music->Tracks/Albums/Artists... menu", and document the
               exact same exclusion for Podcasts: "member tracks in
               the Podcasts playlist are not normally members of the
               MPL (so on the iPod they will only show up under the
               Podcasts menu)". Adding video tracks to mpl anyway is
               the most likely reason movies were appearing in Music
               instead of Movies even with mediatype/movie_flag
               correctly set - menu placement follows mediatype only
               when the track isn't also pulled into the Music-menu
               list via MPL membership. itdb_track_add() above is
               still required (every track must exist in the itdb's
               own tracklist), just not itdb_playlist_add_track(mpl, ...). */

            GError *cp_error = NULL;
            if (!itdb_cp_track_to_ipod(track, filepath, &cp_error)) {
                printf("ERROR\tfailed to copy %s: %s\n", filepath,
                       cp_error && cp_error->message ? cp_error->message : "unknown error");
                fflush(stdout);
                if (cp_error) g_error_free(cp_error);
                detach_track(itdb, track);
                continue;
            }
            added++;
            g_ptr_array_add(added_tracks, track);
            g_ptr_array_add(added_sources, g_strdup(filepath));
            continue;
        }

        if (strncmp(line, "TRACK\t", 6) != 0) continue;

        split_line(line);
        /* fields[0]=="TRACK", [1]=filepath, [2]=title, [3]=artist,
           [4]=album, [5]=genre, [6]=composer, [7]=track_nr,
           [8]=track_total, [9]=year, [10]=duration_ms, [11]=bitrate,
           [12]=samplerate, [13]=size_bytes, [14]=filetype,
           [15]=playlists csv (optional), [16]=artwork image path (optional),
           [17]=is_vbr ("1"/"0", optional, defaults to CBR) */
        if (nfields < 15) continue;

        const char *filepath = fields[1];
        const char *title = fields[2];

        current++;
        printf("PROGRESS\t%d\t%d\t%s\n", current, total, title);
        fflush(stdout);

        Itdb_Track *track = itdb_track_new();
        track->title = dup_or_null(title);
        track->artist = dup_or_null(fields[3]);
        track->album = dup_or_null(fields[4]);
        track->genre = dup_or_null(fields[5]);
        track->composer = dup_or_null(fields[6]);
        track->track_nr = parse_int(fields[7], 0);
        track->tracks = parse_int(fields[8], 0);
        track->year = parse_int(fields[9], 0);
        track->tracklen = parse_int(fields[10], 0);
        track->bitrate = parse_int(fields[11], 0);
        track->samplerate = (guint16) parse_int(fields[12], 0);
        track->size = (guint32) parse_int(fields[13], 0);
        track->filetype = dup_or_null(fields[14]);
        track->mediatype = ITDB_MEDIATYPE_AUDIO;

        /* type1/type2 tell the iPod's firmware how to compute the seek
           table. Getting this wrong - specifically, marking a VBR MP3 as
           CBR - is a well-known cause of a track showing up in the list
           with its title but refusing to actually play (skips straight
           back to the menu when selected). type2: MP3=1, everything else
           (AAC/M4A etc.) = 0. type1: VBR/ABR=1, CBR=0. */
        gboolean is_mp3 = (fields[14][0] != '\0') && (strcasecmp(fields[14], "mp3") == 0);
        gboolean is_vbr = (nfields >= 18) && (fields[17][0] == '1');
        track->type2 = is_mp3 ? 1 : 0;
        track->type1 = is_vbr ? 1 : 0;

        itdb_track_add(itdb, track, -1);
        if (is_podcast_genre(fields[5]))
            make_podcast(itdb, track, TRUE);
        else
            itdb_playlist_add_track(mpl, track, -1);

        if (nfields >= 16 && fields[15][0] != '\0') {
            add_track_to_playlists(itdb, track, fields[15]);
        }

        if (nfields >= 17 && fields[16][0] != '\0') {
            /* itdb_track_set_thumbnails() is a convenience wrapper, and
               on a device libgpod doesn't fully recognize, it can
               refuse outright even though the lower-level function it
               wraps can still succeed and produce real, working
               artwork on that same device - this app (and gtkpod,
               using the same underlying libgpod call) has synced
               cover art successfully before, so don't give up after
               just the high-level call fails; fall back to the
               lower-level one directly, the same way the iPod's own
               device recognition gaps get worked around elsewhere. */
            gboolean art_ok = itdb_track_set_thumbnails(track, fields[16]);
            if (!art_ok) {
                GError *art_error = NULL;
                art_ok = itdb_artwork_set_thumbnail(track->artwork, fields[16], 0, &art_error);
                if (!art_ok) {
                    printf("ERROR\tcould not set artwork for %s: %s (track synced anyway)\n",
                           title, art_error && art_error->message ? art_error->message : "unknown error");
                    fflush(stdout);
                }
                if (art_error) g_error_free(art_error);
            }
            /* itdb_track_set_thumbnails() only confirms the image file
               itself is readable/settable here - actual ArtworkDB
               writing still happens lazily, inside itdb_write() below,
               so this still isn't proof of a final result; that's
               checked for real after a successful write, below. */
            if (art_ok) {
                art_queued++;
            }
        }

        GError *cp_error = NULL;
        if (!itdb_cp_track_to_ipod(track, filepath, &cp_error)) {
            printf("ERROR\tfailed to copy %s: %s\n", filepath,
                   cp_error && cp_error->message ? cp_error->message : "unknown error");
            fflush(stdout);
            if (cp_error) g_error_free(cp_error);
            detach_track(itdb, track);
            continue;
        }
        added++;
        g_ptr_array_add(added_tracks, track);
        g_ptr_array_add(added_sources, g_strdup(filepath));
    }
    fclose(fp);
    if (!g_cancel_requested)
        apply_playlist_sets(itdb, manifest_path, added_tracks, added_sources);

    if (!itdb_write(itdb, &error)) {
        printf("ERROR\tfailed to write iTunesDB: %s\n",
               error && error->message ? error->message : "unknown error");
        fflush(stdout);
        /* Roll back every file copied in this sync - otherwise a write
           failure leaves them sitting on the device wasting space with
           no working database entry pointing at them. */
        for (guint i = 0; i < added_tracks->len; i++) {
            Itdb_Track *t = (Itdb_Track *) g_ptr_array_index(added_tracks, i);
            gchar *onipod = itdb_filename_on_ipod(t);
            if (onipod) {
                g_remove(onipod);
                g_free(onipod);
            }
            detach_track(itdb, t);
        }
        printf("INFO\tnote\trolled back %d copied file(s) since the database write failed\n",
               added_tracks->len);
        fflush(stdout);
        g_ptr_array_free(added_tracks, TRUE);
        g_ptr_array_free(added_sources, TRUE);
        if (error) g_error_free(error);
        itdb_free(itdb);
        return 1;
    }

    for (guint i = 0; i < added_tracks->len && i < added_sources->len; i++) {
        Itdb_Track *t = (Itdb_Track *) g_ptr_array_index(added_tracks, i);
        printf("ADDED\t%" G_GUINT64_FORMAT "\t%s\n", t->dbid,
               (const char *) g_ptr_array_index(added_sources, i));
    }
    fflush(stdout);
    g_ptr_array_free(added_tracks, TRUE);
    g_ptr_array_free(added_sources, TRUE);

    if (art_queued > 0) {
        /* This is the only point that can honestly say whether cover
           art actually worked: itdb_track_set_thumbnails() above only
           confirmed the image files were readable, not that real
           thumbnail data would end up on the device - that generation
           happens lazily, inside itdb_write() above, and libgpod
           doesn't report per-track success/failure for it. Checking
           the actual on-device ArtworkDB file is the only way to know
           for real, so that's what gets reported, instead of trusting
           a per-track return value that was never a reliable signal. */
        gchar *artwork_db_path = g_build_filename(mountpoint, "iPod_Control", "Artwork", "ArtworkDB", NULL);
        GStatBuf st;
        if (g_stat(artwork_db_path, &st) == 0 && st.st_size > 0) {
            printf("INFO\tartwork_summary\tqueued cover art for %d track(s); "
                   "ArtworkDB is now %ld bytes\n", art_queued, (long) st.st_size);
        } else {
            printf("INFO\tartwork_summary\tqueued cover art for %d track(s), but no ArtworkDB "
                   "was created on the device - this iPod isn't writing artwork through libgpod "
                   "(the library this app is built on); see Known Limitations in the README\n",
                   art_queued);
        }
        fflush(stdout);
        g_free(artwork_db_path);
    }

    /* Push everything actually written above (the database, any copied
       files) out to the physical device before reporting success. FAT32
       (what iPods use) isn't crash-safe against writes still sitting in
       the OS's cache - without this, disconnecting shortly after "DONE"
       reports could leave the device with a genuinely corrupted
       filesystem, not just stale data. This matters most for larger
       operations, where more data sits in the cache for longer. */
    sync();

    printf("DONE\t%d\n", added);
    fflush(stdout);
    itdb_free(itdb);
    return 0;
}


/* ---- photo de-duplication ----------------------------------------
   The photo database keeps no file names, so two things stop the same
   picture landing on the iPod twice:
   - a small list on the iPod (Photos/.musicmanager-photos.tsv) of the
     source files already synced, by content hash, each paired with a
     hash of the thumbnail the iPod stored for it;
   - a hash of each stored thumbnail's pixels, which finds copies that
     are already there (e.g. from older versions of this app, which
     synced everything again on every sync). */

static gchar *photo_pixel_hash(Itdb_Device *device, Itdb_Artwork *photo) {
    GdkPixbuf *pb = itdb_artwork_get_pixbuf(device, photo, -1, -1);
    if (!pb) return NULL;
    int w = gdk_pixbuf_get_width(pb), h = gdk_pixbuf_get_height(pb);
    int rs = gdk_pixbuf_get_rowstride(pb), nc = gdk_pixbuf_get_n_channels(pb);
    const guchar *px = gdk_pixbuf_read_pixels(pb);
    GChecksum *ck = g_checksum_new(G_CHECKSUM_SHA1);
    char dims[32];
    snprintf(dims, sizeof(dims), "%dx%dx%d", w, h, nc);
    g_checksum_update(ck, (const guchar *) dims, strlen(dims));
    for (int y = 0; y < h; y++) g_checksum_update(ck, px + (gsize) y * rs, (gsize) w * nc);
    gchar *out = g_strdup(g_checksum_get_string(ck));
    g_checksum_free(ck);
    g_object_unref(pb);
    return out;
}

static gchar *file_sha1(const char *path) {
    gchar *data = NULL;
    gsize len = 0;
    if (!g_file_get_contents(path, &data, &len, NULL)) return NULL;
    gchar *out = g_compute_checksum_for_data(G_CHECKSUM_SHA1, (const guchar *) data, len);
    g_free(data);
    return out;
}

static gchar *photo_index_path(const char *mountpoint) {
    gchar *dir = itdb_get_photos_dir(mountpoint);
    if (!dir) dir = g_build_filename(mountpoint, "Photos", NULL);
    gchar *path = g_build_filename(dir, ".musicmanager-photos.tsv", NULL);
    g_free(dir);
    return path;
}

/* Drops repeated entries inside each album, then merges photos whose
   stored thumbnails are pixel-identical (keeping the first, and putting
   it in every album the copy was in). Fills pixel_to_photo (pixel hash
   -> Itdb_Artwork*, hash strings owned by the table) for the photos
   that remain. Returns how many photos were removed. */
static int dedupe_photodb(Itdb_PhotoDB *db, GHashTable *pixel_to_photo, int *album_repeats) {
    *album_repeats = 0;
    for (GList *it = db->photoalbums; it != NULL; it = it->next) {
        Itdb_PhotoAlbum *album = (Itdb_PhotoAlbum *) it->data;
        GHashTable *seen = g_hash_table_new(g_direct_hash, g_direct_equal);
        GList *m = album->members;
        while (m) {
            GList *next = m->next;
            if (g_hash_table_contains(seen, m->data)) {
                album->members = g_list_delete_link(album->members, m);
                (*album_repeats)++;
            } else {
                g_hash_table_add(seen, m->data);
            }
            m = next;
        }
        g_hash_table_destroy(seen);
    }

    GList *dupes = NULL, *keepers = NULL;
    for (GList *it = db->photos; it != NULL; it = it->next) {
        Itdb_Artwork *photo = (Itdb_Artwork *) it->data;
        gchar *hash = photo_pixel_hash(db->device, photo);
        if (!hash) continue;
        Itdb_Artwork *kept = g_hash_table_lookup(pixel_to_photo, hash);
        if (kept) {
            dupes = g_list_prepend(dupes, photo);
            keepers = g_list_prepend(keepers, kept);
            g_free(hash);
        } else {
            g_hash_table_insert(pixel_to_photo, hash, photo);
        }
    }

    int removed = 0;
    for (GList *d = dupes, *k = keepers; d && k; d = d->next, k = k->next) {
        Itdb_Artwork *dup = d->data, *kept = k->data;
        for (GList *it = db->photoalbums; it != NULL; it = it->next) {
            Itdb_PhotoAlbum *album = (Itdb_PhotoAlbum *) it->data;
            GList *link = g_list_find(album->members, dup);
            if (!link) continue;
            if (g_list_find(album->members, kept)) {
                album->members = g_list_delete_link(album->members, link);
            } else {
                link->data = kept;
            }
        }
        itdb_photodb_remove_photo(db, NULL, dup);
        removed++;
    }
    g_list_free(dupes);
    g_list_free(keepers);
    return removed;
}

/* file hash -> photo, for index entries whose photo is still there. */
static GHashTable *load_photo_index(const char *mountpoint, GHashTable *pixel_to_photo) {
    GHashTable *known = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
    gchar *path = photo_index_path(mountpoint);
    gchar *data = NULL;
    if (g_file_get_contents(path, &data, NULL, NULL)) {
        gchar **lines = g_strsplit(data, "\n", -1);
        for (gchar **l = lines; *l; l++) {
            gchar **f = g_strsplit(*l, "\t", 3);
            if (f[0] && f[1] && f[0][0]) {
                Itdb_Artwork *photo = g_hash_table_lookup(pixel_to_photo, f[1]);
                if (photo) g_hash_table_replace(known, g_strdup(f[0]), photo);
            }
            g_strfreev(f);
        }
        g_strfreev(lines);
        g_free(data);
    }
    g_free(path);
    return known;
}

/* The index lines (file hash, thumbnail pixel hash) for every synced
   file whose photo is in the database. Built right after a write, when
   every photo has its stored thumbnail. */
static GString *photo_index_lines(Itdb_PhotoDB *db, GHashTable *known) {
    GString *out = g_string_new(NULL);
    GHashTableIter iter;
    gpointer key, value;
    g_hash_table_iter_init(&iter, known);
    while (g_hash_table_iter_next(&iter, &key, &value)) {
        if (!g_list_find(db->photos, value)) continue;
        gchar *pixel = photo_pixel_hash(db->device, (Itdb_Artwork *) value);
        if (pixel) g_string_append_printf(out, "%s\t%s\n", (char *) key, pixel);
        g_free(pixel);
    }
    return out;
}

static void save_photo_index(const char *mountpoint, GString *lines) {
    gchar *path = photo_index_path(mountpoint);
    g_file_set_contents(path, lines->str, lines->len, NULL);
    g_free(path);
}

static void photo_album_add_once(Itdb_PhotoDB *db, Itdb_PhotoAlbum *album, Itdb_Artwork *photo) {
    if (album && !g_list_find(album->members, photo)) {
        itdb_photodb_photoalbum_add_photo(db, album, photo, -1);
    }
}

static int cmd_sync_photos(const char *mountpoint, const char *manifest_path) {
    /* Photos use a completely separate on-device database from music
       and video (Itdb_PhotoDB, not Itdb_iTunesDB), with its own parse/
       write functions - confirmed directly from libgpod's own real
       itdb.h, not guessed. itdb_photodb_add_photo() handles format
       conversion and thumbnail generation internally (unlike video,
       which needed ffmpeg run separately beforehand) - it just needs
       a readable image file. */
    GError *error = NULL;
    Itdb_PhotoDB *photodb = itdb_photodb_parse(mountpoint, &error);
    if (!photodb) {
        if (error) g_error_free(error);
        error = NULL;
        photodb = itdb_photodb_create(mountpoint);
        if (!photodb) {
            printf("ERROR\tcould not create a photo database on this device\n");
            fflush(stdout);
            return 1;
        }
    }

    FILE *fp = fopen(manifest_path, "r");
    if (!fp) {
        printf("ERROR\tcould not open manifest file\n");
        fflush(stdout);
        itdb_photodb_free(photodb);
        return 1;
    }

    int total = 0;
    char line[MAX_LINE];
    while (fgets(line, sizeof(line), fp)) {
        if (strncmp(line, "PHOTO\t", 6) == 0) total++;
    }
    rewind(fp);

    /* The master "Photo Library" album (album_type 1) is required for
       a photo to show up at all - the same role MPL plays for music.
       itdb_photodb_add_photo() already puts each new photo in it, so
       it's only added there here when it's missing (adding it again
       made every photo show up twice). */
    Itdb_PhotoAlbum *master_album = NULL;
    for (GList *it = photodb->photoalbums; it != NULL; it = it->next) {
        Itdb_PhotoAlbum *a = (Itdb_PhotoAlbum *) it->data;
        if (a->album_type == 0x01) { master_album = a; break; }
    }

    GHashTable *pixel_to_photo = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
    int album_repeats = 0;
    int merged = dedupe_photodb(photodb, pixel_to_photo, &album_repeats);
    GHashTable *known = load_photo_index(mountpoint, pixel_to_photo);
    g_hash_table_destroy(pixel_to_photo);

    int current = 0;
    int added = 0;
    int already = 0;
    while (fgets(line, sizeof(line), fp)) {
        char *nl = strchr(line, '\n');
        if (nl) *nl = '\0';
        if (strncmp(line, "PHOTO\t", 6) != 0) continue;
        split_line(line);
        /* fields[0]=="PHOTO", [1]=filepath, [2]=album_name (may be empty) */
        if (nfields < 3) continue;

        const char *filepath = fields[1];
        const char *album_name = fields[2];

        current++;
        printf("PROGRESS\t%d\t%d\t%s\n", current, total, filepath);
        fflush(stdout);

        gchar *sha = file_sha1(filepath);
        Itdb_Artwork *photo = sha ? g_hash_table_lookup(known, sha) : NULL;
        if (photo) {
            already++;
        } else {
            GError *add_error = NULL;
            photo = itdb_photodb_add_photo(photodb, filepath, -1, 0, &add_error);
            if (!photo) {
                printf("ERROR\tfailed to add %s: %s\n", filepath,
                       add_error && add_error->message ? add_error->message : "unknown error");
                fflush(stdout);
                if (add_error) g_error_free(add_error);
                g_free(sha);
                continue;
            }
            if (sha) g_hash_table_replace(known, g_strdup(sha), photo);
            added++;
        }
        g_free(sha);

        photo_album_add_once(photodb, master_album, photo);
        if (album_name[0] != '\0') {
            Itdb_PhotoAlbum *album = itdb_photodb_photoalbum_by_name(photodb, album_name);
            if (!album) {
                album = itdb_photodb_photoalbum_create(photodb, album_name, -1);
            }
            photo_album_add_once(photodb, album, photo);
        }
    }
    fclose(fp);

    GError *write_error = NULL;
    if (!itdb_photodb_write(photodb, &write_error)) {
        printf("ERROR\tfailed to write photo database: %s\n",
               write_error && write_error->message ? write_error->message : "unknown error");
        fflush(stdout);
        if (write_error) g_error_free(write_error);
        g_hash_table_destroy(known);
        itdb_photodb_free(photodb);
        return 1;
    }

    /* A file synced by an older version of this app isn't in the index
       yet, so it was just added again; now that its thumbnail exists it
       can be matched against the copy that was already there. */
    GString *index_lines = photo_index_lines(photodb, known);
    g_hash_table_destroy(known);
    pixel_to_photo = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
    int late_repeats = 0;
    int late_merged = dedupe_photodb(photodb, pixel_to_photo, &late_repeats);
    g_hash_table_destroy(pixel_to_photo);
    if (late_merged + late_repeats > 0) {
        if (!itdb_photodb_write(photodb, &write_error)) {
            printf("ERROR\tfailed to write photo database: %s\n",
                   write_error && write_error->message ? write_error->message : "unknown error");
            fflush(stdout);
            if (write_error) g_error_free(write_error);
            g_string_free(index_lines, TRUE);
            itdb_photodb_free(photodb);
            return 1;
        }
        added -= late_merged;
        already += late_merged;
    }
    save_photo_index(mountpoint, index_lines);
    g_string_free(index_lines, TRUE);
    sync();
    printf("INFO\tphotos_already\t%d\n", already);
    printf("INFO\tphotos_merged\t%d\n", merged + album_repeats);
    printf("DONE\t%d\n", added);
    fflush(stdout);
    itdb_photodb_free(photodb);
    return 0;
}

static int cmd_set_model(const char *mountpoint, const char *model) {
    /* The gtkpod technique for a device libgpod can't identify from
       its own (often empty, on 5.5-gen iPods and others with firmware
       from mid-2006 onward) SysInfo file: manually tell it the Apple
       model number, then persist that to the device's own SysInfo
       file. Unlike the first attempt at this feature, this one
       verifies the result immediately afterward instead of just
       reporting the write succeeded - a write can succeed mechanically
       while still not changing anything that matters, e.g. if the
       model string given isn't one libgpod actually recognizes. */
    GError *error = NULL;
    Itdb_iTunesDB *itdb = itdb_parse(mountpoint, &error);
    if (!itdb) {
        if (error) g_error_free(error);
        itdb = itdb_new();
        itdb_set_mountpoint(itdb, mountpoint);
    }

    itdb_device_read_sysinfo(itdb->device);

    /* libgpod wants Apple's order number with its first letter replaced
       ("xA448" for MA448); accept "MA448", "A448" or "xA448". Refuse a
       number it doesn't know (most often the "A1136"-style hardware
       model printed on the case) instead of writing it. */
    gchar *wanted = (strlen(model) == 4) ? g_strdup_printf("x%s", model)
                                         : g_strdup_printf("x%s", model + 1);
    gchar *previous = itdb_device_get_sysinfo(itdb->device, "ModelNumStr");
    itdb_device_set_sysinfo(itdb->device, "ModelNumStr", wanted);
    if (!model_is_known(itdb->device)) {
        printf("ERROR\t'%s' isn't a model number libgpod knows. Use the order number "
               "(like MA448) from Settings > General > About or Apple's site, not the "
               "A1xxx number on the back of the case.\n", model);
        fflush(stdout);
        if (previous) itdb_device_set_sysinfo(itdb->device, "ModelNumStr", previous);
        g_free(previous);
        g_free(wanted);
        itdb_free(itdb);
        return 1;
    }
    g_free(previous);
    g_free(wanted);

    GError *write_error = NULL;
    if (!itdb_device_write_sysinfo(itdb->device, &write_error)) {
        printf("ERROR\tcould not write model to the device: %s\n",
               write_error && write_error->message ? write_error->message : "unknown error");
        fflush(stdout);
        if (write_error) g_error_free(write_error);
        itdb_free(itdb);
        return 1;
    }
    sync();

    /* Re-read sysinfo fresh from disk, the same way a later sync
       would, and check whether libgpod now actually recognizes this
       as an artwork-capable device - the real test of whether this
       did anything, not just whether the write itself succeeded. */
    itdb_device_read_sysinfo(itdb->device);
    gchar *seen_model = itdb_device_get_sysinfo(itdb->device, "ModelNumStr");
    gboolean supports = itdb_device_supports_artwork(itdb->device);
    printf("INFO\tset_model_result\tModelNumStr='%s' artwork_supported=%s\n",
           seen_model ? seen_model : "(empty)", supports ? "yes" : "no");
    fflush(stdout);
    g_free(seen_model);

    printf("DONE\t0\n");
    fflush(stdout);
    itdb_free(itdb);
    return 0;
}

static int cmd_remove(const char *mountpoint, const char *manifest_path) {
    GError *error = NULL;
    Itdb_iTunesDB *itdb = itdb_parse(mountpoint, &error);
    if (!itdb) {
        printf("ERROR\tno iTunesDB found on this iPod\n");
        fflush(stdout);
        if (error) g_error_free(error);
        return 1;
    }

    FILE *fp = fopen(manifest_path, "r");
    if (!fp) {
        printf("ERROR\tcould not open manifest file\n");
        fflush(stdout);
        itdb_free(itdb);
        return 1;
    }

    char line[MAX_LINE];
    int removed = 0;
    /* Track which on-disk files to delete, but don't delete anything
       yet - only unlink the tracks from the in-memory database here.
       Deletion is deferred until after itdb_write() has actually
       succeeded (below), so a write failure (e.g. this device needing
       a firewire ID libgpod couldn't find) leaves the iPod completely
       untouched instead of deleting real files while the still-old,
       unwritten database keeps listing them as present - that
       mismatch is what "wipe seems to corrupt the iPod" was. */
    GPtrArray *paths_to_delete = g_ptr_array_new_with_free_func(g_free);
    while (fgets(line, sizeof(line), fp)) {
        char *nl = strchr(line, '\n');
        if (nl) *nl = '\0';
        if (strncmp(line, "REMOVE\t", 7) != 0) continue;
        split_line(line);
        if (nfields < 2) continue;
        guint64 dbid = g_ascii_strtoull(fields[1], NULL, 10);

        for (GList *it = itdb->tracks; it != NULL; it = it->next) {
            Itdb_Track *t = (Itdb_Track *) it->data;
            if (t->dbid == dbid) {
                gchar *onipod = itdb_filename_on_ipod(t);
                if (onipod) {
                    g_ptr_array_add(paths_to_delete, onipod);  /* array owns it now */
                }
                detach_track(itdb, t);
                removed++;
                break;
            }
        }
    }
    fclose(fp);

    if (!itdb_write(itdb, &error)) {
        printf("ERROR\tfailed to write iTunesDB: %s\n",
               error && error->message ? error->message : "unknown error");
        printf("INFO\tnote\tno files were deleted - the database write failed, "
               "so nothing on the iPod was actually changed\n");
        fflush(stdout);
        if (error) g_error_free(error);
        g_ptr_array_free(paths_to_delete, TRUE);
        itdb_free(itdb);
        return 1;
    }

    /* Only now that the database write has actually succeeded is it
       safe to remove the underlying files. */
    for (guint i = 0; i < paths_to_delete->len; i++) {
        g_remove((const char *) g_ptr_array_index(paths_to_delete, i));
    }
    g_ptr_array_free(paths_to_delete, TRUE);

    /* Same reasoning as cmd_sync: push the database rewrite and every
       file deletion above out to the physical device before reporting
       success, since FAT32 isn't crash-safe against writes still
       sitting in the OS's cache. This matters most here specifically -
       removing many tracks at once (a large "Wipe iPod Library" in
       particular) touches a lot of data, leaving a longer window where
       an early disconnect could corrupt the filesystem rather than
       just lose the pending change. */
    sync();

    printf("DONE\t%d\n", removed);
    fflush(stdout);
    itdb_free(itdb);
    return 0;
}

static int cmd_rename(const char *mountpoint, const char *name) {
    /* The iPod's own name (shown in its About screen and by iTunes) is
       the name of its master playlist. */
    GError *error = NULL;
    Itdb_iTunesDB *itdb = itdb_parse(mountpoint, &error);
    if (!itdb) {
        printf("ERROR\tno iTunesDB found on this iPod\n");
        fflush(stdout);
        if (error) g_error_free(error);
        return 1;
    }
    Itdb_Playlist *mpl = itdb_playlist_mpl(itdb);
    if (!mpl) {
        printf("ERROR\tthis iPod's database has no main playlist to rename\n");
        fflush(stdout);
        itdb_free(itdb);
        return 1;
    }
    g_free(mpl->name);
    mpl->name = g_strdup(name);
    if (!itdb_write(itdb, &error)) {
        printf("ERROR\tfailed to write iTunesDB: %s\n",
               error && error->message ? error->message : "unknown error");
        fflush(stdout);
        if (error) g_error_free(error);
        itdb_free(itdb);
        return 1;
    }
    sync();
    printf("DONE\t1\n");
    fflush(stdout);
    itdb_free(itdb);
    return 0;
}

static int cmd_info(const char *mountpoint) {
    /* What kind of iPod this is - the firmware restore only supports the
       5th-gen (Video) family, whose firmware is a plain partition image. */
    Itdb_Device *dev = itdb_device_new();
    itdb_device_set_mountpoint(dev, mountpoint);
    const Itdb_IpodInfo *info = itdb_device_get_ipod_info(dev);
    if (info && info->ipod_generation != ITDB_IPOD_GENERATION_UNKNOWN) {
        gboolean video = info->ipod_generation == ITDB_IPOD_GENERATION_VIDEO_1 ||
                         info->ipod_generation == ITDB_IPOD_GENERATION_VIDEO_2;
        printf("INFO\tgeneration\t%s\n", itdb_info_get_ipod_generation_string(info->ipod_generation));
        printf("INFO\tmodel\t%s\n", info->model_number ? info->model_number : "");
        printf("INFO\tfirmware_family\t%s\n", video ? "25" : "");
    }
    printf("DONE\t0\n");
    fflush(stdout);
    itdb_device_free(dev);
    return 0;
}

static int cmd_update(const char *mountpoint, const char *manifest_path) {
    /* Get Info edits for tracks already on the iPod. Manifest lines:
       UPDATE dbid title artist album albumartist genre composer track_nr
       track_total cd_nr cd_total year [artwork path] - every text field
       is the track's full new value (empty clears it). */
    GError *error = NULL;
    Itdb_iTunesDB *itdb = itdb_parse(mountpoint, &error);
    if (!itdb) {
        printf("ERROR\tno iTunesDB found on this iPod\n");
        fflush(stdout);
        if (error) g_error_free(error);
        return 1;
    }

    FILE *fp = fopen(manifest_path, "r");
    if (!fp) {
        printf("ERROR\tcould not open manifest file\n");
        fflush(stdout);
        itdb_free(itdb);
        return 1;
    }

    char line[MAX_LINE];
    int updated = 0;
    while (fgets(line, sizeof(line), fp)) {
        char *nl = strchr(line, '\n');
        if (nl) *nl = '\0';
        if (strncmp(line, "UPDATE\t", 7) != 0) continue;
        split_line(line);
        if (nfields < 13) continue;
        guint64 dbid = g_ascii_strtoull(fields[1], NULL, 10);

        Itdb_Track *t = NULL;
        for (GList *it = itdb->tracks; it != NULL; it = it->next) {
            if (((Itdb_Track *) it->data)->dbid == dbid) {
                t = (Itdb_Track *) it->data;
                break;
            }
        }
        if (!t) continue;

        g_free(t->title);       t->title = dup_or_null(fields[2]);
        g_free(t->artist);      t->artist = dup_or_null(fields[3]);
        g_free(t->album);       t->album = dup_or_null(fields[4]);
        g_free(t->albumartist); t->albumartist = dup_or_null(fields[5]);
        g_free(t->genre);       t->genre = dup_or_null(fields[6]);
        g_free(t->composer);    t->composer = dup_or_null(fields[7]);
        t->track_nr = parse_int(fields[8], 0);
        t->tracks = parse_int(fields[9], 0);
        t->cd_nr = parse_int(fields[10], 0);
        t->cds = parse_int(fields[11], 0);
        t->year = parse_int(fields[12], 0);

        if (nfields >= 14 && fields[13][0] != '\0') {
            gboolean art_ok = itdb_track_set_thumbnails(t, fields[13]);
            if (!art_ok) {
                GError *art_error = NULL;
                art_ok = itdb_artwork_set_thumbnail(t->artwork, fields[13], 0, &art_error);
                if (!art_ok) {
                    printf("ERROR\tcould not set artwork for %s: %s\n",
                           t->title ? t->title : "a track",
                           art_error && art_error->message ? art_error->message : "unknown error");
                    fflush(stdout);
                }
                if (art_error) g_error_free(art_error);
            }
        }
        updated++;
    }
    fclose(fp);

    if (updated > 0 && !itdb_write(itdb, &error)) {
        printf("ERROR\tfailed to write iTunesDB: %s\n",
               error && error->message ? error->message : "unknown error");
        fflush(stdout);
        if (error) g_error_free(error);
        itdb_free(itdb);
        return 1;
    }
    sync();

    printf("DONE\t%d\n", updated);
    fflush(stdout);
    itdb_free(itdb);
    return 0;
}

static int cmd_fix_podcasts(const char *mountpoint) {
    /* Run before every device read: moves episodes that older versions
       synced as music into the Podcasts menu. Only writes the database
       when something actually needed fixing. */
    GError *error = NULL;
    Itdb_iTunesDB *itdb = itdb_parse(mountpoint, &error);
    if (!itdb) {
        if (error) g_error_free(error);
        printf("DONE\t0\n");
        return 0;
    }
    int fixed = fix_podcasts_synced_as_music(itdb);
    if (fixed > 0 && !itdb_write(itdb, &error)) {
        printf("ERROR\tfailed to write iTunesDB: %s\n",
               error && error->message ? error->message : "unknown error");
        fflush(stdout);
        if (error) g_error_free(error);
        itdb_free(itdb);
        return 1;
    }
    printf("DONE\t%d\n", fixed);
    fflush(stdout);
    itdb_free(itdb);
    return 0;
}

static int cmd_remove_playlist(const char *mountpoint, const char *playlist_name) {
    /* Removes just the playlist container itself - the tracks that
       were in it are untouched (they stay wherever else they already
       are on the device, e.g. the master playlist), the same
       "unlink, don't delete the underlying thing" idea as cmd_remove
       only ever unlinking tracks it's explicitly told to, never
       anything implied by playlist membership. */
    GError *error = NULL;
    Itdb_iTunesDB *itdb = itdb_parse(mountpoint, &error);
    if (!itdb) {
        printf("ERROR\tno iTunesDB found on this iPod\n");
        fflush(stdout);
        if (error) g_error_free(error);
        return 1;
    }

    Itdb_Playlist *target = NULL;
    for (GList *it = itdb->playlists; it != NULL; it = it->next) {
        Itdb_Playlist *pl = (Itdb_Playlist *) it->data;
        if (!itdb_playlist_is_mpl(pl) && pl->name && strcmp(pl->name, playlist_name) == 0) {
            target = pl;
            break;
        }
    }

    if (!target) {
        printf("ERROR\tno playlist named '%s' found on this iPod\n", playlist_name);
        fflush(stdout);
        itdb_free(itdb);
        return 1;
    }

    /* Takes it out of itdb->playlists and frees it. Does not touch
       itdb->tracks or the master playlist - the tracks stay exactly
       where they were. */
    itdb_playlist_remove(target);

    if (!itdb_write(itdb, &error)) {
        printf("ERROR\tfailed to write iTunesDB: %s\n",
               error && error->message ? error->message : "unknown error");
        fflush(stdout);
        if (error) g_error_free(error);
        itdb_free(itdb);
        return 1;
    }
    sync();

    printf("DONE\t1\n");
    fflush(stdout);
    itdb_free(itdb);
    return 0;
}

/* Leftover files: song files in iPod_Control/Music that no song in the
   iPod's database points to (left behind when a database write was
   lost), and database songs whose file is gone. Lists them:
     ORPHAN<TAB>path<TAB>bytes
     MISSING<TAB>dbid<TAB>title
   With "delete", removes both (orphan files from disk, missing songs
   from the database). */
static int cmd_orphans(const char *mountpoint, gboolean do_delete) {
    GError *error = NULL;
    Itdb_iTunesDB *itdb = itdb_parse(mountpoint, &error);
    if (!itdb) {
        printf("ERROR\tno iTunesDB found on this iPod\n");
        fflush(stdout);
        if (error) g_error_free(error);
        return 1;
    }
    GHashTable *known = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
    GPtrArray *missing = g_ptr_array_new();
    for (GList *it = itdb->tracks; it; it = it->next) {
        Itdb_Track *t = (Itdb_Track *) it->data;
        gchar *path = itdb_filename_on_ipod(t);
        if (path) {
            g_hash_table_add(known, g_utf8_strdown(path, -1));
            g_free(path);
        } else if (t->ipod_path && *t->ipod_path) {
            g_ptr_array_add(missing, t);
        }
    }
    gchar *music_dir = itdb_get_music_dir(mountpoint);
    int orphans = 0;
    guint64 orphan_bytes = 0;
    GDir *top = music_dir ? g_dir_open(music_dir, 0, NULL) : NULL;
    if (top) {
        const gchar *sub;
        while ((sub = g_dir_read_name(top))) {
            gchar *subdir = g_build_filename(music_dir, sub, NULL);
            GDir *d = g_dir_open(subdir, 0, NULL);
            if (d) {
                const gchar *name;
                while ((name = g_dir_read_name(d))) {
                    if (name[0] == '.') continue;
                    gchar *file = g_build_filename(subdir, name, NULL);
                    gchar *lower = g_utf8_strdown(file, -1);
                    GStatBuf st;
                    if (!g_hash_table_contains(known, lower) && g_stat(file, &st) == 0 && S_ISREG(st.st_mode)) {
                        orphans++;
                        orphan_bytes += st.st_size;
                        printf("ORPHAN\t%s\t%lld\n", file, (long long) st.st_size);
                        if (do_delete) g_remove(file);
                    }
                    g_free(lower);
                    g_free(file);
                }
                g_dir_close(d);
            }
            g_free(subdir);
        }
        g_dir_close(top);
    }
    for (guint i = 0; i < missing->len; i++) {
        Itdb_Track *t = g_ptr_array_index(missing, i);
        printf("MISSING\t%" G_GUINT64_FORMAT "\t%s\n", t->dbid, t->title ? t->title : "");
    }
    int rc = 0;
    if (do_delete && missing->len > 0) {
        for (guint i = 0; i < missing->len; i++)
            detach_track(itdb, g_ptr_array_index(missing, i));
        if (!itdb_write(itdb, &error)) {
            printf("ERROR\tfailed to write iTunesDB: %s\n",
                   error && error->message ? error->message : "unknown error");
            if (error) g_error_free(error);
            rc = 1;
        }
    }
    if (do_delete) sync();
    printf("INFO\torphans\t%d\t%" G_GUINT64_FORMAT "\n", orphans, orphan_bytes);
    printf("DONE\t%d\n", orphans + (int) missing->len);
    fflush(stdout);
    g_ptr_array_free(missing, TRUE);
    g_hash_table_destroy(known);
    g_free(music_dir);
    itdb_free(itdb);
    return rc;
}

/* Every iPod model libgpod knows, for the app's model picker:
   MODEL<TAB>number<TAB>capacity GB<TAB>model name<TAB>generation */
static int cmd_models(void) {
    for (const Itdb_IpodInfo *i = itdb_info_get_ipod_info_table(); i && i->model_number; i++) {
        if (i->ipod_generation == ITDB_IPOD_GENERATION_UNKNOWN) continue;
        printf("MODEL\t%s\t%g\t%s\t%s\n", i->model_number, i->capacity,
               itdb_info_get_ipod_model_name_string(i->ipod_model),
               itdb_info_get_ipod_generation_string(i->ipod_generation));
    }
    printf("DONE\t0\n");
    fflush(stdout);
    return 0;
}

int main(int argc, char **argv) {
    if (argc >= 2 && strcmp(argv[1], "models") == 0) return cmd_models();
    if (argc < 3) {
        fprintf(stderr,
                "usage: %s list <mountpoint>\n"
                "       %s sync <mountpoint> <manifest-file>\n"
                "       %s remove <mountpoint> <manifest-file>\n"
                "       %s remove-playlist <mountpoint> <playlist-name>\n"
                "       %s set-model <mountpoint> <model-number>\n"
                "       %s sync-photos <mountpoint> <manifest-file>\n"
                "       %s update <mountpoint> <manifest-file>\n"
                "       %s info <mountpoint>\n"
                "       %s rename <mountpoint> <name>\n",
                argv[0], argv[0], argv[0], argv[0], argv[0], argv[0], argv[0], argv[0], argv[0]);
        return 2;
    }

    const char *cmd = argv[1];
    const char *mountpoint = argv[2];
    lock_device(mountpoint);
    prepare_device(mountpoint);

    if (strcmp(cmd, "list") == 0) {
        return cmd_list(mountpoint);
    } else if (strcmp(cmd, "sync") == 0 && argc >= 4) {
        return cmd_sync(mountpoint, argv[3]);
    } else if (strcmp(cmd, "remove") == 0 && argc >= 4) {
        return cmd_remove(mountpoint, argv[3]);
    } else if (strcmp(cmd, "remove-playlist") == 0 && argc >= 4) {
        return cmd_remove_playlist(mountpoint, argv[3]);
    } else if (strcmp(cmd, "rename") == 0 && argc >= 4) {
        return cmd_rename(mountpoint, argv[3]);
    } else if (strcmp(cmd, "info") == 0) {
        return cmd_info(mountpoint);
    } else if (strcmp(cmd, "update") == 0 && argc >= 4) {
        return cmd_update(mountpoint, argv[3]);
    } else if (strcmp(cmd, "orphans") == 0) {
        return cmd_orphans(mountpoint, argc >= 4 && strcmp(argv[3], "delete") == 0);
    } else if (strcmp(cmd, "fix-podcasts") == 0) {
        return cmd_fix_podcasts(mountpoint);
    } else if (strcmp(cmd, "set-model") == 0 && argc >= 4) {
        return cmd_set_model(mountpoint, argv[3]);
    } else if (strcmp(cmd, "sync-photos") == 0 && argc >= 4) {
        return cmd_sync_photos(mountpoint, argv[3]);
    }

    fprintf(stderr, "unknown or incomplete command\n");
    return 2;
}
