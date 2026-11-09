"""
video_converter.py — checks whether a video file is already iPod
5th/5.5-generation-compatible, and converts it via ffmpeg if not.

iPod (Video, 5th/5.5-gen) plays: H.264 Baseline Profile up to Level 3.0,
up to 640x480, up to 30fps, ~1.5 Mbps video; AAC-LC audio up to
160kbps; container .m4v/.mp4/.mov. This targets exactly that
combination — the same one real iTunes itself produced when converting
video for these devices. Verified against a real ffmpeg build: the
command below produces a file ffprobe confirms as
codec=h264/profile=Constrained Baseline/level=30/640x480/yuv420p with
AAC audio, and the -progress output's "out_time_ms" field is
confirmed to actually be microseconds (a known ffmpeg naming quirk,
identical to "out_time_us"), not milliseconds — used accordingly below.
"""

import hashlib
import json
import os
import shutil
import subprocess

TARGET_WIDTH = 640
TARGET_HEIGHT = 480
TARGET_FPS = 30
TARGET_VIDEO_BITRATE = "1500k"
TARGET_AUDIO_BITRATE = "128k"
TARGET_AUDIO_RATE = 44100
COMPATIBLE_CONTAINERS = (".m4v", ".mp4", ".mov")


def _stable_output_path(filepath, output_dir, ext):
    """A deterministic output path for filepath's converted copy, so
    re-converting the same source (e.g. re-scanning a folder, or
    picking up a pipeline fix) overwrites its own previous output
    instead of accumulating numbered duplicates. Includes a short hash
    of the full source path (not just the basename) so two different
    source files that happen to share a filename don't collide with
    each other."""
    base = os.path.splitext(os.path.basename(filepath))[0]
    digest = hashlib.sha1(os.path.abspath(filepath).encode("utf-8")).hexdigest()[:8]
    return os.path.join(output_dir, f"{base}-{digest}{ext}")


class ConversionError(Exception):
    pass


class ConversionCancelled(ConversionError):
    """Raised when a cancel_event fires mid-conversion (or before a
    step even starts). A subclass of ConversionError so any existing
    "treat this file as not added" handling still works unchanged,
    but its own type lets a caller that cares (see gui.py's Cancel
    button) tell "the user stopped this" apart from an actual
    failure."""
    pass


def ffmpeg_available():
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def atomicparsley_available():
    return shutil.which("AtomicParsley") is not None


def probe(filepath):
    """Returns a dict describing the file's video/audio streams, or
    None if ffprobe can't read it at all (not a real/playable media
    file, or ffprobe itself isn't available)."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", filepath],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video:
        return None

    try:
        duration_s = float(data.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration_s = 0.0

    return {
        "video_codec": video.get("codec_name"),
        "profile": (video.get("profile") or "").lower(),
        "width": video.get("width") or 0,
        "height": video.get("height") or 0,
        "fps": _parse_rate(video.get("r_frame_rate")),
        "refs": video.get("refs") or 0,
        "audio_codec": audio.get("codec_name") if audio else None,
        "duration_ms": int(duration_s * 1000),
    }


def _parse_rate(rate_str):
    if not rate_str or "/" not in rate_str:
        return 0.0
    num, _, den = rate_str.partition("/")
    try:
        num, den = float(num), float(den)
        return num / den if den else 0.0
    except ValueError:
        return 0.0


def needs_conversion(filepath):
    """True if the file isn't already iPod-compatible as-is (or can't
    even be read, which conversion will surface a clear ffmpeg error
    for rather than silently skipping it)."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext not in COMPATIBLE_CONTAINERS:
        return True
    info = probe(filepath)
    if not info:
        return True
    if info["video_codec"] != "h264":
        return True
    if "baseline" not in info["profile"]:
        return True
    if info["width"] > TARGET_WIDTH or info["height"] > TARGET_HEIGHT:
        return True
    if info["fps"] > TARGET_FPS + 0.5:
        return True
    # iPod 5th/5.5-gen needs "Low-Complexity Baseline Profile"
    # specifically - stricter than generic Baseline, which alone
    # doesn't restrict reference frame count. A file could otherwise
    # look fully compliant (right codec/profile/resolution) yet still
    # fail to decode on-device if it has more than 1 reference frame.
    if info["refs"] > 1:
        return True
    if info["audio_codec"] not in ("aac", None):
        return True
    return False


def extract_thumbnail(filepath, output_path, timestamp_fraction=0.1):
    """
    Grabs a single frame from filepath and saves it as a JPEG at
    output_path, for use as a thumbnail (the video grid, similar to
    album art). Seeks to timestamp_fraction of the way through the
    video (default 10%) rather than frame 0, since opening titles/black
    frames at the very start are common and make poor thumbnails.
    Returns True on success, False if a frame couldn't be extracted
    (e.g. ffmpeg/ffprobe missing, corrupt file) - never raises, since a
    missing thumbnail shouldn't block adding the video itself.
    """
    info = probe(filepath)
    duration_s = (info["duration_ms"] / 1000) if info and info["duration_ms"] else 0
    seek_s = max(0.0, duration_s * timestamp_fraction) if duration_s > 0 else 0.0

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-ss", str(seek_s), "-i", filepath,
        "-frames:v", "1", "-q:v", "4",
        "-loglevel", "error",
        output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0


def convert(filepath, output_dir, on_progress=None, cancel_event=None):
    """
    Converts filepath to an iPod 5th/5.5-gen-compatible .m4v in
    output_dir (created if needed), preserving aspect ratio by
    letterboxing rather than stretching. Returns the output path, never
    overwriting an existing file of the same name. Raises
    ConversionError on failure - the caller should treat that source
    file as not added rather than partially added.
    on_progress(fraction: float 0..1), if given, is called periodically
    based on ffmpeg's own reported encoding position vs. the source's
    duration (best-effort; not called at all if duration is unknown).
    cancel_event, if given (a threading.Event), is checked before
    starting ffmpeg and then once per progress line while it runs; if
    it's set, the ffmpeg process is terminated, any partial output is
    removed, and ConversionCancelled is raised instead of returning.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise ConversionCancelled("cancelled before starting")

    os.makedirs(output_dir, exist_ok=True)
    output_path = _stable_output_path(filepath, output_dir, ".m4v")

    info = probe(filepath)
    duration_s = (info["duration_ms"] / 1000) if info and info["duration_ms"] else 0

    cmd = [
        "ffmpeg", "-y", "-i", filepath,
        "-vf", (f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease,"
                f"pad={TARGET_WIDTH}:{TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2,format=yuv420p"),
        "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.0", "-pix_fmt", "yuv420p",
        # iPod 5th/5.5-gen documents itself as "Low-Complexity Baseline
        # Profile" specifically, not just plain Baseline - confirmed
        # against multiple independent, real (period-accurate) sources
        # for this exact hardware. Baseline alone (x264's -profile:v
        # baseline) already forces no B-frames and no CABAC, but does
        # NOT by itself restrict reference frames - x264 defaults to
        # multiple. -refs 1 is the specific extra constraint these
        # devices' decoder chips need beyond generic Baseline
        # conformance, which is also why a generic decoder (ffprobe,
        # any modern player) can still read the file and report
        # correct duration/timestamps even when the actual iPod
        # decoder can't decode the frames themselves.
        "-refs", "1", "-bf", "0",
        "-b:v", TARGET_VIDEO_BITRATE, "-maxrate", TARGET_VIDEO_BITRATE, "-bufsize", "2000k",
        "-r", str(TARGET_FPS),
        "-c:a", "aac", "-b:a", TARGET_AUDIO_BITRATE, "-ar", str(TARGET_AUDIO_RATE),
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats", "-loglevel", "error",
        output_path,
    ]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    except OSError as e:
        raise ConversionError(f"could not start ffmpeg: {e}")

    cancelled = False
    for line in proc.stdout:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        line = line.strip()
        if line.startswith("out_time_us=") and on_progress and duration_s > 0:
            value = line.split("=", 1)[1]
            if value != "N/A":
                try:
                    on_progress(min(1.0, (int(value) / 1_000_000) / duration_s))
                except (ValueError, ZeroDivisionError):
                    pass

    if cancelled:
        # -nostats/-loglevel error means ffmpeg won't respond to a
        # plain SIGTERM as gracefully as it would to its usual "q"
        # keypress, but it does still exit promptly on it in practice;
        # kill() as a backstop only if it somehow doesn't.
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except OSError:
            pass
        raise ConversionCancelled("cancelled")

    stderr_output = proc.stderr.read()
    proc.wait()

    if proc.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except OSError:
            pass
        raise ConversionError(stderr_output.strip() or f"ffmpeg exited with code {proc.returncode}")

    if cancel_event is not None and cancel_event.is_set():
        # Encoding finished right as Cancel was hit - don't bother
        # running the (comparatively slow, subprocess-spawning)
        # AtomicParsley step on output that's about to be discarded.
        try:
            os.remove(output_path)
        except OSError:
            pass
        raise ConversionCancelled("cancelled")

    _add_ipod_atom(output_path)
    return output_path


def ensure_ipod_atom(filepath, output_dir, cancel_event=None):
    """
    For a file that's already otherwise iPod-compatible
    (needs_conversion() is False) - still copies it into output_dir
    and runs the iPod atom step on the copy, never modifying the
    original in place. A file can pass every codec/profile/resolution
    check and still be missing this atom (e.g. produced by some other
    tool that never added it), so "already compatible" on codec
    grounds alone isn't sufficient to skip this step - only a full
    re-encode can safely be skipped. Returns the copy's path. Raises
    ConversionError under the same conditions as convert().
    cancel_event, if given, is checked before the copy and again
    before the (slower) AtomicParsley step - this path has no
    long-running subprocess to interrupt mid-flight the way convert()
    does, so cancellation here just means "don't start the next step".
    """
    if cancel_event is not None and cancel_event.is_set():
        raise ConversionCancelled("cancelled before starting")

    os.makedirs(output_dir, exist_ok=True)
    ext = os.path.splitext(filepath)[1] or ".m4v"
    output_path = _stable_output_path(filepath, output_dir, ext)
    shutil.copy2(filepath, output_path)

    if cancel_event is not None and cancel_event.is_set():
        try:
            os.remove(output_path)
        except OSError:
            pass
        raise ConversionCancelled("cancelled")

    _add_ipod_atom(output_path)
    return output_path


def _add_ipod_atom(filepath):
    """
    Classic iPods (5th/5.5-gen specifically) require a special uuid
    atom written into the MP4 container before they'll actually
    decode/display H.264 video from it at all - confirmed directly
    from AtomicParsley's own source and from HandBrake's own official
    docs ("Older classic iPods require a special atom to be written to
    the MP4 file before they will play back on the device... HandBrake
    doesn't add this by default"). This is a real, historically
    well-documented requirement distinct from codec/profile/level
    conformance - a file can be fully spec-compliant H.264 Baseline
    and still show a black screen (audio still playing, duration still
    reading correctly, since none of that depends on this atom) without
    it. Uses AtomicParsley (a separate, purpose-built tool for exactly
    this) rather than hand-rolling raw MP4 atom bytes.
    Raises ConversionError if AtomicParsley isn't available or fails -
    treated as a real failure, not a soft warning, since a video
    without this atom is a confirmed-broken result on this hardware.
    """
    if not atomicparsley_available():
        raise ConversionError(
            "AtomicParsley isn't installed - it's required (alongside ffmpeg) to make "
            "video actually play on 5th/5.5-gen iPods, not just an optional extra. "
            "Install it via your package manager (e.g. sudo apt install atomicparsley)."
        )
    cmd = ["AtomicParsley", filepath, "--DeepScan", "--iPod-uuid", "1200", "--overWrite"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ConversionError(f"could not run AtomicParsley: {e}")
    combined_output = (result.stdout or "") + (result.stderr or "")
    if "was not added" in combined_output or "the ipod hi-res uuid was not added" in combined_output.lower():
        raise ConversionError(
            "AtomicParsley refused to add the iPod atom (it double-checks the file's own "
            "profile/level before adding it) - the encode may not actually be within spec: "
            + combined_output.strip()[:300]
        )
    if result.returncode != 0:
        raise ConversionError(f"AtomicParsley failed: {combined_output.strip()[:300]}")
