"""
ipod_backend.py — everything related to talking to an actual iPod.

The heavy lifting (reading/writing the iTunesDB) is done by the compiled
`ipod_helper` C program (see ../helper/), which links against your
system's real libgpod. This module finds the iPod, writes the small
manifest file the helper reads, runs it, and turns its line-oriented
output into Python data the GUI can use.
"""

import json
import re
import signal
import threading
import os
import select
import shutil
import subprocess
import tempfile

import applog
import artwork

HELPER_NAMES = ("ipod_helper",)


class IpodError(Exception):
    pass


class SyncHandle:
    """
    A handle to a running sync, so the caller (the GUI, on the main
    thread) can ask it to stop while it's running in a background
    thread. sync_tracks() fills in .proc as soon as the helper process
    starts; calling .cancel() before that just marks the request so it
    takes effect the moment the process exists.
    """

    def __init__(self):
        self.proc = None
        self._cancel_requested = False

    def cancel(self):
        self._cancel_requested = True
        if self.proc is not None:
            self.proc.cancel_requested = True
            try:
                self.proc.terminate()  # sends SIGTERM; the helper catches
                                         # it and finishes cleanly (see
                                         # ipod_helper.c's cancellation note)
            except OSError:
                pass


def locate_helper():
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", "helper", name)
        for name in HELPER_NAMES
    ] + [
        os.path.join(here, "helper", name) for name in HELPER_NAMES
    ]
    for path in candidates:
        path = os.path.abspath(path)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise IpodError(
        "ipod_helper is not built yet. Run install.sh (or 'make' inside "
        "the helper/ folder) first."
    )


def find_ipod_mounts():
    """
    Looks for currently-mounted volumes that really are an iPod.

    Primarily uses lsblk's live MOUNTPOINT data, since that reflects
    wherever the volume actually got mounted - udisks under /media or
    /run/media, a desktop environment's gvfs mount under
    /run/user/<uid>/gvfs, a manual mount, anywhere - rather than only
    checking the couple of conventional directories a file manager
    happens to use. Every mounted partition is confirmed by checking
    for its iPod_Control folder, which is what actually makes a volume
    an iPod - a label or USB vendor string can be wrong or missing,
    but a real iPod's filesystem layout can't be faked by accident.

    Falls back to walking the conventional mount directories if lsblk
    isn't available at all. Returns a list of dicts with mountpoint,
    volume label, and free bytes.
    """
    found = []
    seen = set()

    devices = _list_block_devices()
    for dev in devices:
        mountpoint = dev.get("mountpoint")
        if not mountpoint or mountpoint in seen or not os.path.isdir(mountpoint):
            continue
        seen.add(mountpoint)
        if not _find_ipod_control_dir(mountpoint):
            continue
        try:
            free = shutil.disk_usage(mountpoint).free
        except OSError:
            free = None
        found.append({
            "mountpoint": mountpoint,
            "label": dev.get("label") or os.path.basename(mountpoint),
            "free_bytes": free,
        })

    if not devices:
        # lsblk missing or failed entirely - fall back to scanning the
        # handful of conventional mount roots directly.
        for mountpoint, label in _scan_conventional_mount_roots():
            if mountpoint in seen:
                continue
            seen.add(mountpoint)
            if not _find_ipod_control_dir(mountpoint):
                continue
            try:
                free = shutil.disk_usage(mountpoint).free
            except OSError:
                free = None
            found.append({"mountpoint": mountpoint, "label": label, "free_bytes": free})

    return found


def _scan_conventional_mount_roots():
    """Yields (mountpoint, label) for every entry under the usual
    removable-media mount directories. Used only as a fallback when
    lsblk isn't available to report live mountpoints directly."""
    search_roots = []
    user = os.environ.get("USER", "")
    for base in (f"/media/{user}", "/run/media/" + user, "/media", "/mnt"):
        if os.path.isdir(base):
            search_roots.append(base)

    for root in search_roots:
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for entry in entries:
            mountpoint = os.path.join(root, entry)
            if os.path.isdir(mountpoint):
                yield mountpoint, entry


def _find_ipod_control_dir(mountpoint):
    """iPod_Control's casing varies (iPod_Control vs iPod_control)."""
    try:
        for name in os.listdir(mountpoint):
            if name.lower() == "ipod_control":
                return os.path.join(mountpoint, name)
    except OSError:
        pass
    return None


def _list_block_devices():
    """Returns a flat list of block-device dicts from `lsblk`, or [] if
    lsblk isn't available/fails."""
    if not shutil.which("lsblk"):
        return []
    try:
        result = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,LABEL,FSTYPE,MOUNTPOINT,TRAN,VENDOR,MODEL,TYPE"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    devices = []

    def walk(nodes):
        for node in nodes:
            devices.append(node)
            if node.get("children"):
                walk(node["children"])

    walk(data.get("blockdevices", []))
    return devices


def _looks_like_ipod_partition(dev):
    if dev.get("type") != "part":
        return False
    label = (dev.get("label") or "").upper()
    vendor = (dev.get("vendor") or "").upper()
    model = (dev.get("model") or "").upper()
    if "IPOD" in label:
        return True
    if "APPLE" in vendor and "IPOD" in model:
        return True
    return False


def _looks_like_unmounted_ipod(dev):
    return _looks_like_ipod_partition(dev) and not dev.get("mountpoint")


def is_ipod_physically_present():
    """True if an iPod-like partition shows up in lsblk at all, mounted
    or not — used to tell 'ejected but still plugged in' apart from
    'actually unplugged'."""
    return any(_looks_like_ipod_partition(d) for d in _list_block_devices())


def wait_for_mount_table_change(timeout=None):
    """
    Blocks until the system mounts or unmounts something, anywhere —
    not just an iPod — or until `timeout` seconds pass (None waits
    forever). Returns True if the mount table actually changed, False
    on timeout or if this isn't supported here.

    Linux lets you select()/poll() an open /proc/mounts file descriptor
    for POLLERR/POLLPRI, which the kernel signals exactly when the
    mount table changes (see 'man 5 proc', the /proc/[pid]/mounts
    entry) — no busy-waiting, no periodic re-checking, just a blocking
    wait that wakes up on the actual event. This is what lets the app
    notice a newly-mounted iPod immediately, the moment udisks (or
    whatever mounted it) finishes, instead of running a check on a
    fixed timer whether or not anything happened.
    """
    try:
        with open("/proc/mounts", "r") as f:
            poller = select.poll()
            poller.register(f, select.POLLERR | select.POLLPRI)
            events = poller.poll(timeout * 1000 if timeout is not None else None)
            return bool(events)
    except OSError:
        # /proc/mounts isn't available (not Linux, or some locked-down
        # environment) - caller falls back to whatever it does without
        # live mount-change notifications.
        return False


def _mount_device(devname):
    """
    Mounts /dev/<devname> the same way a desktop file manager would —
    via udisksctl, which mounts removable media for the logged-in user
    without needing root. Returns (mountpoint, error_message).
    """
    if not shutil.which("udisksctl"):
        return None, "udisksctl is not installed (usually part of the udisks2 package)"
    devpath = f"/dev/{devname}"
    try:
        result = subprocess.run(
            ["udisksctl", "mount", "-b", devpath],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)

    stdout = result.stdout.strip()
    if result.returncode == 0 and " at " in stdout:
        return stdout.split(" at ", 1)[1].rstrip("."), None

    combined = (stdout + " " + result.stderr.strip()).strip()
    if "already mounted" in combined.lower() and " at " in combined:
        # udisksctl reports the existing mountpoint in this case too.
        return combined.split(" at ", 1)[1].split(".")[0].strip(), None
    return None, combined or f"udisksctl exited with code {result.returncode}"


def _find_block_device_for_mount(mountpoint):
    """Returns the /dev/... source device backing a mountpoint, or None."""
    if shutil.which("findmnt"):
        try:
            result = subprocess.run(
                ["findmnt", "-n", "-o", "SOURCE", "--target", mountpoint],
                capture_output=True, text=True, timeout=5,
            )
            src = result.stdout.strip()
            if result.returncode == 0 and src:
                return src
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == mountpoint:
                    return parts[0]
    except OSError:
        pass
    return None


def eject_ipod(mountpoint):
    """
    Safely unmounts the iPod (and, where possible, spins the USB device
    down) the same way a file manager's 'Eject' button does — via
    udisksctl, so it's safe to physically unplug afterward.
    Returns (success, message).
    """
    if not shutil.which("udisksctl"):
        return False, (
            "udisksctl isn't installed, so this app can't eject it directly — "
            "use your file manager's Eject/Safely Remove instead."
        )

    device = _find_block_device_for_mount(mountpoint)
    if not device:
        return False, f"Couldn't find the underlying device for {mountpoint}."

    try:
        result = subprocess.run(
            ["udisksctl", "unmount", "-b", device],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)

    combined = (result.stdout + " " + result.stderr).strip()
    if result.returncode != 0 and "not mounted" not in combined.lower():
        return False, combined or f"udisksctl exited with code {result.returncode}"

    # Best-effort: power off the whole disk so it's truly safe to unplug.
    # Not fatal if this fails - the unmount above is the part that matters.
    parent = device.rstrip("0123456789") or device
    for candidate in (parent, device):
        try:
            po = subprocess.run(
                ["udisksctl", "power-off", "-b", candidate],
                capture_output=True, text=True, timeout=10,
            )
            if po.returncode == 0:
                break
        except (OSError, subprocess.SubprocessError):
            continue

    return True, f"Ejected {os.path.basename(mountpoint)}. It's safe to unplug now."


def find_or_mount_ipod():
    """
    Like find_ipod_mounts(), but if nothing is currently mounted, also
    looks for a plugged-in-but-unmounted iPod (via lsblk) and mounts it
    (via udisksctl) the same way a desktop file manager would.

    Returns (mounts, message) — mounts is the same shape as
    find_ipod_mounts(); message is a human-readable note about what
    auto-mounting did (or tried to do), or None if nothing needed it.
    """
    mounts = find_ipod_mounts()
    if mounts:
        return mounts, None

    candidates = [d for d in _list_block_devices() if _looks_like_unmounted_ipod(d)]
    if not candidates:
        return [], None

    found = []
    notes = []
    for dev in candidates:
        mp, err = _mount_device(dev["name"])
        if mp and _find_ipod_control_dir(mp):
            try:
                free = shutil.disk_usage(mp).free
            except OSError:
                free = None
            found.append({
                "mountpoint": mp,
                "label": dev.get("label") or os.path.basename(mp),
                "free_bytes": free,
            })
            notes.append(f"Auto-mounted iPod at {mp}")
        elif mp:
            notes.append(f"Mounted {mp}, but it doesn't look like an iPod (no iPod_Control folder)")
        else:
            notes.append(f"Found a likely iPod ({dev.get('name')}) but couldn't mount it: {err}")

    return found, "; ".join(notes) if notes else None


def _usb_serial_for_mount(mountpoint):
    """The USB serial number of the disk mounted at mountpoint, from
    lsblk (no root needed), or None."""
    if not shutil.which("lsblk"):
        return None
    try:
        result = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,MOUNTPOINT,SERIAL,TYPE"],
            capture_output=True, text=True, timeout=5,
        )
        data = json.loads(result.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    target = os.path.realpath(mountpoint)

    def mounted_here(node):
        mp = node.get("mountpoint")
        if mp and os.path.realpath(mp) == target:
            return True
        return any(mounted_here(c) for c in node.get("children") or [])

    for disk in data.get("blockdevices", []):
        if mounted_here(disk):
            return (disk.get("serial") or "").strip() or None
    return None


def ensure_firewire_guid(mountpoint):
    """iPods that sign their database (Nano 3rd generation and later,
    Classic) need their FireWire ID, which libgpod looks for in
    SysInfo or SysInfoExtended. When neither has it, write it into
    SysInfo from the iPod's USB serial number, which is that same ID.
    Returns a note for the log when it writes one, else None."""
    device_dir = os.path.join(mountpoint, "iPod_Control", "Device")
    if not os.path.isdir(device_dir):
        return None
    sysinfo = os.path.join(device_dir, "SysInfo")
    for path, needle in ((sysinfo, "firewireguid"),
                         (os.path.join(device_dir, "SysInfoExtended"), "firewireguid")):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                if needle in f.read().lower():
                    return None
        except OSError:
            pass
    serial = (_usb_serial_for_mount(mountpoint) or "").upper()
    if serial.startswith("0X"):
        serial = serial[2:]
    if not re.fullmatch(r"[0-9A-F]{16}", serial):
        return None
    try:
        existing = ""
        if os.path.exists(sysinfo):
            with open(sysinfo, encoding="utf-8", errors="replace") as f:
                existing = f.read()
        with open(sysinfo, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(f"FirewireGuid: 0x{serial}\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        return f"couldn't add the iPod's FireWire ID to SysInfo: {e}"
    return f"added FirewireGuid 0x{serial} (from its USB serial number) to SysInfo"


def _run_helper(args, timeout=None):
    if len(args) >= 2 and os.path.isdir(os.path.join(args[1], "iPod_Control")):
        try:
            note = ensure_firewire_guid(args[1])
            if note:
                applog.log("IPOD ID: " + note)
        except Exception as e:  # never block a job over this
            applog.log(f"IPOD ID: check failed: {type(e).__name__}: {e}")
    helper = locate_helper()
    proc = subprocess.Popen(
        [helper] + list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    # Read stderr as it comes. Left unread, a helper that prints a lot of
    # warnings (libgpod does, on some iPods) fills the pipe and stops
    # mid-job, still holding the iPod's lock, so every later job waits.
    proc.stderr_chunks = []

    def drain():
        for line in proc.stderr:
            proc.stderr_chunks.append(line)

    proc.stderr_thread = threading.Thread(target=drain, daemon=True)
    proc.stderr_thread.start()
    return proc


def _parse_stream(proc, on_progress=None):
    """
    Reads the helper's stdout line by line, dispatching to callbacks.
    Returns (tracks, playlists, done_count, errors, notes,
    photo_albums). Each playlist dict's "track_dbids" is the set of
    TRACKROW dbids that are members of it (built from PLAYLISTTRACK
    lines) - empty for the master playlist, since the helper doesn't
    emit membership for it (every track is trivially a member).
    photo_albums is only populated by "list" (on-device photo album
    names/counts, including the master "Photo Library") - empty for
    every other command.
    """
    tracks = []
    playlists = []
    photo_albums = []
    playlist_track_dbids = {}  # playlist name -> set of member dbids
    errors = []
    notes = []
    done_count = 0

    for raw in proc.stdout:
        line = raw.rstrip("\n")
        if not line:
            continue
        parts = line.split("\t")
        tag = parts[0]

        if tag == "PROGRESS" and len(parts) >= 4:
            current, total, title = parts[1], parts[2], parts[3]
            if on_progress:
                try:
                    on_progress(int(current), int(total), title)
                except ValueError:
                    pass
        elif tag == "TRACKROW" and len(parts) >= 6:
            tracks.append({
                "dbid": parts[1],
                "title": parts[2],
                "artist": parts[3],
                "album": parts[4],
                "duration_ms": int(parts[5]) if parts[5].isdigit() else 0,
                "size_bytes": int(parts[6]) if len(parts) > 6 and parts[6].isdigit() else 0,
                "mediatype": int(parts[7]) if len(parts) > 7 and parts[7].isdigit() else 0,
                "tvshow": parts[8] if len(parts) > 8 else "",
                "season_nr": int(parts[9]) if len(parts) > 9 and parts[9].isdigit() else 0,
                "episode_nr": int(parts[10]) if len(parts) > 10 and parts[10].isdigit() else 0,
                # Missing on a helper built before play-count sync
                # existed - 0/None then, so nothing gets imported
                # rather than anything breaking.
                "play_count": int(parts[11]) if len(parts) > 11 and parts[11].isdigit() else 0,
                "last_played": (
                    int(parts[12]) if len(parts) > 12 and parts[12].isdigit() and int(parts[12]) > 0
                    else None
                ),
                # Missing on an older helper - blank then.
                "genre": parts[13] if len(parts) > 13 else "",
                "track_nr": int(parts[14]) if len(parts) > 14 and parts[14].isdigit() else 0,
                "year": int(parts[15]) if len(parts) > 15 and parts[15].isdigit() else 0,
            })
        elif tag == "PHOTOALBUMROW" and len(parts) >= 4:
            photo_albums.append({
                "name": parts[1],
                "photo_count": int(parts[2]) if parts[2].isdigit() else 0,
                "is_master": parts[3] == "1",
            })
        elif tag == "PLAYLISTROW" and len(parts) >= 3:
            playlists.append({
                "name": parts[1],
                "track_count": int(parts[2]) if parts[2].isdigit() else 0,
                "is_master": len(parts) > 3 and parts[3] == "1",
            })
        elif tag == "PLAYLISTTRACK" and len(parts) >= 3:
            playlist_track_dbids.setdefault(parts[1], set()).add(parts[2])
        elif tag == "ERROR":
            errors.append(parts[1] if len(parts) > 1 else "unknown error")
        elif tag == "DONE":
            done_count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        elif tag == "INFO" and len(parts) >= 3:
            notes.append((parts[1], parts[2]))
            if parts[1] == "waiting" and on_progress:
                on_progress(0, -1, parts[2])
        elif tag == "ADDED" and len(parts) >= 3:
            # dbid of a track the helper just put on the iPod, and the
            # file it came from - how the app knows which copy is which.
            notes.append(("added", (parts[1], parts[2])))

    for pl in playlists:
        pl["track_dbids"] = playlist_track_dbids.get(pl["name"], set())

    proc.wait()
    proc.stderr_thread.join(timeout=5)
    stderr_out = "".join(proc.stderr_chunks[-200:])
    if proc.returncode == -signal.SIGTERM and getattr(proc, "cancel_requested", False):
        # Cancelled before the helper got going (for example while it
        # waited for another iPod job): not an error.
        notes.append(("cancelled", "1"))
    elif proc.returncode not in (0,) and not errors:
        errors.append(stderr_out.strip() or f"helper exited with code {proc.returncode}")

    return tracks, playlists, done_count, errors, notes, photo_albums


def read_device_library(mountpoint):
    """Returns (tracks, playlists, errors, photo_albums) currently on
    the iPod. tracks includes music, movies, and TV shows together -
    distinguish by each track's "mediatype" (0/1=audio, 0x02=movie,
    0x40=tvshow). photo_albums is on-device photo album names/counts
    only (no thumbnail data - see cmd_list's comment on why)."""
    fix_errors = []
    try:
        # Moves podcast episodes older versions synced as music into the
        # iPod's Podcasts menu (a no-op once they're all there).
        fix = _run_helper(["fix-podcasts", mountpoint])
        _t, _p, _done, fix_errors, _n, _a = _parse_stream(fix)
    except IpodError:
        raise
    except Exception as e:
        fix_errors = [f"podcast fix-up failed: {e}"]
    proc = _run_helper(["list", mountpoint])
    tracks, playlists, _done, errors, _notes, photo_albums = _parse_stream(proc)
    return tracks, playlists, list(fix_errors) + list(errors), photo_albums


def _escape_field(value) -> str:
    if value is None:
        return ""
    return str(value).replace("\t", " ").replace("\n", " ")


def sync_tracks(mountpoint, tracks, playlist_names_by_track_id=None, on_progress=None, handle=None,
                playlist_sets=None):
    """
    playlist_sets: optional [(name, [("dbid", dbid) | ("path", filepath)])]
            - on-device playlists to set to exactly these songs, in order
            ("path" = one of `tracks`, copied in this same sync).
    tracks: iterable of sqlite3.Row (from Library.all_tracks / tracks_by_ids)
    playlist_names_by_track_id: optional {track_id: [playlist name, ...]}
    handle: optional SyncHandle - if given, its .cancel() can be called
            from another thread to stop the sync early (finishing the
            track in progress, keeping everything done so far).
    Returns (added_count, errors, artwork_notes, cancelled, links) where
    links is [(dbid, track_id)] for every track now on the iPod.
    """
    playlist_names_by_track_id = playlist_names_by_track_id or {}
    id_by_path = {}

    fd, manifest_path = tempfile.mkstemp(prefix="ipod_sync_", suffix=".manifest")
    art_tempfiles = []
    art_found_count = 0
    total_count = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for t in tracks:
                total_count += 1
                id_by_path[t["filepath"]] = t["id"]
                playlists_csv = ",".join(playlist_names_by_track_id.get(t["id"], []))
                art_path = artwork.extract_artwork_to_tempfile(t["filepath"])
                if art_path:
                    art_tempfiles.append(art_path)
                    art_found_count += 1
                fields = [
                    "TRACK",
                    t["filepath"],
                    _escape_field(t["title"]),
                    _escape_field(t["artist"]),
                    _escape_field(t["album"]),
                    _escape_field(t["genre"]),
                    _escape_field(t["composer"]),
                    _escape_field(t["track_nr"] or 0),
                    _escape_field(t["track_total"] or 0),
                    _escape_field(t["year"] or 0),
                    _escape_field(t["duration_ms"] or 0),
                    _escape_field(t["bitrate"] or 0),
                    _escape_field(t["samplerate"] or 0),
                    _escape_field(t["size_bytes"] or 0),
                    _escape_field(t["filetype"]),
                    _escape_field(playlists_csv),
                    _escape_field(art_path or ""),
                    "1" if t["is_vbr"] else "0",
                ]
                f.write("\t".join(fields) + "\n")
            for name, items in playlist_sets or []:
                f.write(f"PLSET\t{_escape_field(name)}\n")
                for kind, value in items:
                    f.write(f"PLITEM\t{kind}\t{value if kind == 'path' else _escape_field(value)}\n")

        proc = _run_helper(["sync", mountpoint, manifest_path])
        if handle is not None:
            handle.proc = proc
            if handle._cancel_requested:
                # cancel() was called before the process even existed -
                # apply it now that it does.
                handle.cancel()
        _tracks, _playlists, done_count, errors, notes, _photo_albums = _parse_stream(proc, on_progress=on_progress)
        cancelled = any(key == "cancelled" for key, _value in notes)
        # The rollback reassurance (if the final database write failed)
        # only ever appears alongside an ERROR line, so it belongs with
        # errors rather than as its own separate channel here.
        errors = errors + [value for key, value in notes if key == "note"]
        art_notes = [value for key, value in notes if key == "artwork_summary"]
        art_notes += [f"device: {value}" for key, value in notes if key == "device_diagnostic"]
        if not art_notes:
            # The helper only reports a summary when it was actually handed
            # at least one art file. If we found none at all on the Python
            # side (no embedded tags, no folder cover image), say so
            # explicitly instead of leaving the sync silent about art.
            if art_found_count == 0 and total_count > 0:
                art_notes = [
                    f"no cover art found for any of {total_count} track(s) "
                    "(checked embedded tags and folder images like cover.jpg)"
                ]
            elif art_found_count > 0:
                art_notes = [f"found art for {art_found_count} of {total_count} track(s), but none could be attached"]
        links = [
            (dbid, id_by_path[path]) for key, value in notes if key == "added"
            for dbid, path in [value] if path in id_by_path
        ]
        return done_count, errors, art_notes, cancelled, links
    finally:
        try:
            os.remove(manifest_path)
        except OSError:
            pass
        for path in art_tempfiles:
            try:
                os.remove(path)
            except OSError:
                pass


def sync_videos(mountpoint, videos, on_progress=None, handle=None):
    """
    videos: iterable of sqlite3.Row (from Library.all_videos / videos_by_ids)
    handle: optional SyncHandle - same cancellation support as sync_tracks.
    Returns (added_count, errors, cancelled, counts): counts["already"]
    is how many were skipped because they're already on the iPod, and
    counts["merged"] how many duplicate copies already there were
    removed. An empty photos list just removes duplicates. "kind" (movie vs tvshow)
    only controls this app's own local library organization now - on
    the device, every video is tagged as a Movie (mediatype/movie_flag)
    regardless of kind, since classic iPods handle the separate TV Show
    mediatype inconsistently (this used to cause synced movies to show
    up filed under TV Shows and vice versa). A TV show's episodes still
    get grouped together on the device, via a same-named playlist
    (built from each video's show_name) instead of the TV Show menu -
    see the VIDEO line's handling in the C helper for the exact values.
    """
    fd, manifest_path = tempfile.mkstemp(prefix="ipod_syncvideo_", suffix=".manifest")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for v in videos:
                fields = [
                    "VIDEO",
                    v["filepath"],
                    _escape_field(v["title"]),
                    _escape_field(v["duration_ms"] or 0),
                    _escape_field(v["size_bytes"] or 0),
                    v["kind"],
                    _escape_field(v["show_name"] or "") if v["kind"] == "tvshow" else "",
                    _escape_field(v["season_nr"] or 0) if v["kind"] == "tvshow" else "",
                    _escape_field(v["episode_nr"] or 0) if v["kind"] == "tvshow" else "",
                ]
                f.write("\t".join(fields) + "\n")
            for name, items in playlist_sets or []:
                f.write(f"PLSET\t{_escape_field(name)}\n")
                for kind, value in items:
                    f.write(f"PLITEM\t{kind}\t{value if kind == 'path' else _escape_field(value)}\n")

        proc = _run_helper(["sync", mountpoint, manifest_path])
        if handle is not None:
            handle.proc = proc
            if handle._cancel_requested:
                handle.cancel()
        _tracks, _playlists, done_count, errors, notes, _photo_albums = _parse_stream(proc, on_progress=on_progress)
        cancelled = any(key == "cancelled" for key, _value in notes)
        errors = errors + [value for key, value in notes if key == "note"]
        values = dict(notes)
        counts = {
            "already": int(values.get("photos_already", "0") or 0),
            "merged": int(values.get("photos_merged", "0") or 0),
        }
        return done_count, errors, cancelled, counts
    finally:
        try:
            os.remove(manifest_path)
        except OSError:
            pass


def sync_photos(mountpoint, photos, on_progress=None, handle=None):
    """
    photos: iterable of sqlite3.Row (from Library.all_photos / photos_by_ids)
    handle: optional SyncHandle - same cancellation support as sync_tracks.
    Returns (added_count, errors, cancelled, counts): counts["already"]
    is how many were skipped because they're already on the iPod, and
    counts["merged"] how many duplicate copies already there were
    removed. An empty photos list just removes duplicates. Uses a separate helper
    command (sync-photos) since photos live in their own on-device
    database, entirely separate from music/video.
    """
    fd, manifest_path = tempfile.mkstemp(prefix="ipod_syncphoto_", suffix=".manifest")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for p in photos:
                fields = [
                    "PHOTO",
                    p["filepath"],
                    _escape_field(p["album_name"] or ""),
                ]
                f.write("\t".join(fields) + "\n")

        proc = _run_helper(["sync-photos", mountpoint, manifest_path])
        if handle is not None:
            handle.proc = proc
            if handle._cancel_requested:
                handle.cancel()
        _tracks, _playlists, done_count, errors, notes, _photo_albums = _parse_stream(proc, on_progress=on_progress)
        cancelled = any(key == "cancelled" for key, _value in notes)
        errors = errors + [value for key, value in notes if key == "note"]
        values = dict(notes)
        counts = {
            "already": int(values.get("photos_already", "0") or 0),
            "merged": int(values.get("photos_merged", "0") or 0),
        }
        return done_count, errors, cancelled, counts
    finally:
        try:
            os.remove(manifest_path)
        except OSError:
            pass


def remove_tracks(mountpoint, dbids):
    """Returns (removed_count, errors, notes) - notes carries
    informational context like the C helper's rollback reassurance
    when a database write fails ("no files were deleted...")."""
    fd, manifest_path = tempfile.mkstemp(prefix="ipod_remove_", suffix=".manifest")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for dbid in dbids:
                f.write(f"REMOVE\t{dbid}\n")

        proc = _run_helper(["remove", mountpoint, manifest_path])
        _tracks, _playlists, done_count, errors, notes, _photo_albums = _parse_stream(proc)
        note_lines = [value for key, value in notes if key == "note"]
        return done_count, errors, note_lines
    finally:
        try:
            os.remove(manifest_path)
        except OSError:
            pass


def update_tracks(mountpoint, updates, art_path=None):
    """Pushes Get Info edits to tracks already on the iPod. updates is a
    list of (dbid, track) pairs, where track holds the full new values
    (a dict or sqlite3.Row with the library's column names). art_path,
    if given, becomes the cover of every listed track.
    Returns (updated_count, errors)."""
    fd, manifest_path = tempfile.mkstemp(prefix="ipod_update_", suffix=".manifest")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for dbid, t in updates:
                fields = ["UPDATE", str(dbid)] + [
                    _escape_field(t[c]) for c in (
                        "title", "artist", "album", "albumartist", "genre", "composer",
                    )
                ] + [
                    _escape_field(t[c] or 0) for c in (
                        "track_nr", "track_total", "cd_nr", "cd_total", "year",
                    )
                ] + [_escape_field(art_path or "")]
                f.write("\t".join(fields) + "\n")

        proc = _run_helper(["update", mountpoint, manifest_path])
        _tracks, _playlists, done_count, errors, _notes, _photo_albums = _parse_stream(proc)
        return done_count, errors
    finally:
        try:
            os.remove(manifest_path)
        except OSError:
            pass


def _fat_label(name):
    """A FAT volume label: at most 11 characters, upper case, none of
    the characters FAT forbids."""
    cleaned = "".join(c for c in name.upper() if c not in '*?.,;:/\\|+=<>[]"').strip()
    return cleaned[:11].rstrip()


def rename_ipod(mountpoint, name):
    """Renames the iPod: the name in its music database (what the iPod
    and iTunes show) and, when udisks allows it, the drive label (what
    the file manager shows). Returns (errors, label_note) - label_note
    says what happened to the drive label, or None if it was changed."""
    proc = _run_helper(["rename", mountpoint, name])
    _t, _p, _done, errors, _n, _a = _parse_stream(proc)
    if errors:
        return errors, None

    label = _fat_label(name)
    device = _find_block_device_for_mount(mountpoint)
    if not label:
        return [], "the drive name was left as it was (no usable characters for a drive label)"
    if not device or not device.startswith("/dev/") or not shutil.which("gdbus"):
        return [], "the drive name couldn't be changed here, so the file manager keeps the old one"
    object_path = "/org/freedesktop/UDisks2/block_devices/" + os.path.basename(device).replace("-", "_2d")
    try:
        result = subprocess.run(
            ["gdbus", "call", "--system", "--dest", "org.freedesktop.UDisks2",
             "--object-path", object_path, "--method", "org.freedesktop.UDisks2.Filesystem.SetLabel",
             label, "{}"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return [], f"the drive name couldn't be changed, so the file manager keeps the old one [{e}]"
    if result.returncode != 0:
        detail = result.stderr.strip() or "udisks refused"
        return [], f"the drive name couldn't be changed, so the file manager keeps the old one [{detail}]"
    return [], None


def wipe_library(mountpoint):
    """
    Removes every track currently on the iPod - a full library wipe -
    by reading the current device library and removing each track the
    same way remove_tracks() already does one at a time. This does NOT
    touch the device's firmware/bootloader; it's the equivalent of
    selecting everything in "On iPod" and choosing Remove, just in one
    step. Non-master on-device playlists that only contained now-removed
    tracks are left behind as empty shells - the C helper's "remove"
    mode only unlinks tracks, not playlists themselves.
    Returns (removed_count, errors, notes).
    """
    tracks, _playlists, list_errors, _photo_albums = read_device_library(mountpoint)
    if list_errors:
        return 0, list_errors, []
    if not tracks:
        return 0, [], []
    dbids = [t["dbid"] for t in tracks]
    return remove_tracks(mountpoint, dbids)


def find_leftovers(mountpoint, delete=False):
    """Song files on the iPod that its database doesn't list (they show
    up as "Other" space), and database songs whose file is gone. With
    delete=True, removes both. Returns (orphans [(path, bytes)],
    missing [(dbid, title)], errors)."""
    proc = _run_helper(["orphans", mountpoint] + (["delete"] if delete else []))
    orphans, missing, errors = [], [], []
    for raw in proc.stdout:
        parts = raw.rstrip("\n").split("\t")
        if parts[0] == "ORPHAN" and len(parts) >= 3:
            orphans.append((parts[1], int(parts[2]) if parts[2].isdigit() else 0))
        elif parts[0] == "MISSING" and len(parts) >= 3:
            missing.append((parts[1], parts[2]))
        elif parts[0] == "ERROR" and len(parts) >= 2:
            errors.append(parts[1])
    proc.wait()
    proc.stderr_thread.join(timeout=5)
    if proc.returncode != 0 and not errors:
        errors.append("".join(proc.stderr_chunks[-20:]).strip() or f"helper exited with code {proc.returncode}")
    return orphans, missing, errors


def list_ipod_models():
    """Every iPod model libgpod knows, as dicts with "number" (Apple's
    order number, like "MA448") and "label" for a picker, newest
    families first."""
    proc = subprocess.Popen(
        [locate_helper(), "models"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    out, _err = proc.communicate(timeout=20)
    models = []
    for line in out.splitlines():
        parts = line.split("\t")
        if parts[0] != "MODEL" or len(parts) < 5:
            continue
        number = "M" + parts[1]
        name, generation = parts[3], parts[4]
        try:
            capacity = float(parts[2])
        except ValueError:
            continue
        # Phones, touches and iPads can't be synced this way at all.
        if capacity <= 0 or any(w in generation for w in ("Mobile", "Touch", "iPhone", "iPad")):
            continue
        family = generation.split(" (")[0]
        kind = name if name.startswith(family.split()[0]) else f"{family} {name}"
        models.append({
            "number": number,
            "label": f"{number}  \u2013  iPod {kind}, {parts[2]} GB  ({generation})",
        })
    models.reverse()
    return models


def ipod_model_known(mountpoint):
    """True if libgpod can tell which iPod model this is (the helper's
    `info` command only reports a generation for a known model)."""
    proc = _run_helper(["info", mountpoint])
    _t, _p, _d, _errors, notes, _a = _parse_stream(proc)
    return any(key == "generation" for key, _value in notes)


def set_ipod_model(mountpoint, model):
    """
    Writes an Apple model number (e.g. "MA446") to the device's own
    SysInfo file, persisting it there - the technique gtkpod has long
    used when libgpod can't auto-detect a device's model from an
    empty/incomplete on-device SysInfo (documented as common on 5.5-gen
    iPods and other models with firmware from mid-2006 onward). This is
    a one-time fix, not something reapplied per sync: because it's
    written to the device itself, every later read benefits from it
    automatically. Unlike a plain "it was written" confirmation, this
    also verifies the result: it re-reads sysinfo fresh afterward and
    checks whether libgpod now actually recognizes the device as
    artwork-capable, since a write can succeed mechanically without
    the given model string being one libgpod actually recognizes.
    Returns (success, errors, verified_supported: bool or None).
    """
    proc = _run_helper(["set-model", mountpoint, model])
    _tracks, _playlists, _done, errors, notes, _photo_albums = _parse_stream(proc)
    result_lines = [value for key, value in notes if key == "set_model_result"]
    verified_supported = None
    if result_lines:
        verified_supported = "artwork_supported=yes" in result_lines[0]
    return not errors, errors, verified_supported


def remove_playlist(mountpoint, playlist_name):
    """
    Removes just the named on-device playlist - the tracks that were
    in it are left exactly where they are on the iPod (still playable,
    still members of the on-device master playlist); only the playlist
    itself goes away. Returns (success, message).
    """
    proc = _run_helper(["remove-playlist", mountpoint, playlist_name])
    _tracks, _playlists, _done, errors, _notes, _photo_albums = _parse_stream(proc)
    if errors:
        return False, "; ".join(errors)
    return True, f"Removed the playlist '{playlist_name}' from the iPod (the songs themselves are still there)."
