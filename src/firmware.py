"""
firmware.py - Import Firmware for 5th-generation (Video, 5G/5.5G)
iPods, installed the way Rockbox's manual restore guide does it on
Linux: the firmware partition (the iPod's first partition) gets Apple's
firmware image written to it raw - that image is the "Firmware-25.x.x"
file inside Apple's .ipsw restore bundle. The music partition is never
touched.

The write goes straight to the iPod's block device, so it needs root:
it runs through pkexec, which shows the desktop's own password prompt.
Before anything is written, plan_device() checks that the target really
is a USB iPod whose second partition is the one mounted where this app
found iPod_Control - so a wrong pick can't overwrite some other disk.

Other iPod families (Nano, Classic 6G+, Shuffle) use encrypted or
differently packaged firmware and aren't supported here.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import zipfile

import appdirs
import ipod_backend

FIRMWARE_DIR = os.path.join(appdirs.DATA_DIR, "firmware")
_META_PATH = os.path.join(FIRMWARE_DIR, "imported.json")

# The 5G firmware partition starts with Apple's ASCII-art stop sign
# (ipodpatcher refuses a firmware partition that doesn't).
_STOP_SIGN_START = b"{{~~"
_MIN_FW_BYTES = 1 * 1024 * 1024
_MAX_FW_BYTES = 128 * 1024 * 1024
_FW_MEMBER_RE = re.compile(r"(?:^|/)Firmware-(\d+)[\d.\-]*$")


class FirmwareError(Exception):
    pass


def _check_image_header(head, where):
    if not head.startswith(_STOP_SIGN_START) or b"S T O P" not in head[:512]:
        raise FirmwareError(
            f"{where} isn't a 5th-gen iPod firmware image (it doesn't start with "
            "Apple's firmware partition header)."
        )


def import_firmware(path):
    """Validates an .ipsw (or a bare Firmware-25.x.x file) and keeps a
    copy in FIRMWARE_DIR for later restores. Returns the stored info
    dict (name, path, size)."""
    os.makedirs(FIRMWARE_DIR, exist_ok=True)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            members = [(m, _FW_MEMBER_RE.search(m.filename)) for m in z.infolist()]
            members = [(m, match) for m, match in members if match]
            ours = [m for m, match in members if match.group(1) == "25"]
            if not ours:
                if members:
                    other = ", ".join(sorted({os.path.basename(m.filename) for m, _ in members}))
                    raise FirmwareError(
                        f"This restore file is for a different iPod ({other}). 5th-gen "
                        "(Video) iPods need one containing a Firmware-25 file."
                    )
                raise FirmwareError("No iPod firmware was found inside this file.")
            member = max(ours, key=lambda m: m.file_size)
            if not _MIN_FW_BYTES <= member.file_size <= _MAX_FW_BYTES:
                raise FirmwareError("The firmware inside this file is an unexpected size.")
            name = os.path.basename(member.filename)
            with z.open(member) as src:
                _check_image_header(src.read(4096), name)
            dest = os.path.join(FIRMWARE_DIR, name)
            with z.open(member) as src, open(dest + ".part", "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
    else:
        size = os.path.getsize(path)
        if not _MIN_FW_BYTES <= size <= _MAX_FW_BYTES:
            raise FirmwareError("That file is the wrong size to be iPod firmware.")
        name = os.path.basename(path)
        with open(path, "rb") as f:
            _check_image_header(f.read(4096), name)
        dest = os.path.join(FIRMWARE_DIR, name)
        shutil.copyfile(path, dest + ".part")
    os.replace(dest + ".part", dest)

    # Only the newest import is kept around.
    previous = current_firmware()
    if previous and previous["path"] != dest:
        try:
            os.remove(previous["path"])
        except OSError:
            pass
    info = {"name": name, "path": dest, "size": os.path.getsize(dest), "source": os.path.basename(path)}
    with open(_META_PATH, "w") as f:
        json.dump(info, f)
    return info


def current_firmware():
    """The imported firmware (dict) or None."""
    try:
        with open(_META_PATH) as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    return info if os.path.isfile(info.get("path", "")) else None


def device_family(mountpoint):
    """("25", "Video (2nd Gen.)") for a supported iPod, else ("", generation)."""
    proc = ipod_backend._run_helper(["info", mountpoint])
    _t, _p, _d, _errors, notes, _a = ipod_backend._parse_stream(proc)
    values = dict(notes)
    return values.get("firmware_family", ""), values.get("generation", "unknown model")


def _lsblk(path):
    result = subprocess.run(
        ["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,TYPE,PKNAME,TRAN,VENDOR,MODEL,MOUNTPOINT,LABEL", path],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise FirmwareError(f"Couldn't read the iPod's disk layout: {result.stderr.strip()}")
    return json.loads(result.stdout)["blockdevices"]


def plan_device(mountpoint):
    """Works out (and sanity-checks) which block devices a restore would
    write. Returns {"disk", "fw_part", "data_part", "label", "fw_size"}."""
    if not shutil.which("lsblk"):
        raise FirmwareError("lsblk isn't installed, so the iPod's disk can't be identified safely.")
    data_part = ipod_backend._find_block_device_for_mount(mountpoint)
    if not data_part or not data_part.startswith("/dev/"):
        raise FirmwareError("Couldn't tell which disk the iPod is.")
    part = _lsblk(data_part)[0]
    if part.get("type") != "part" or not part.get("pkname"):
        raise FirmwareError("The iPod's music partition doesn't look like a normal disk partition.")
    disk = _lsblk("/dev/" + part["pkname"])[0]
    vendor = (disk.get("vendor") or "").strip().lower()
    model = (disk.get("model") or "").strip().lower()
    if disk.get("tran") != "usb" or not ("ipod" in model or "apple" in vendor):
        raise FirmwareError(
            f"The disk behind {mountpoint} doesn't identify itself as a USB iPod "
            f"({disk.get('vendor') or '?'} {disk.get('model') or '?'}), so nothing was written."
        )
    children = [c for c in disk.get("children") or [] if c.get("type") == "part"]
    if len(children) != 2 or children[1].get("path") != part.get("path"):
        raise FirmwareError(
            "This iPod's partitions aren't laid out the usual way (a firmware partition, "
            "then the music partition), so restoring it from here isn't safe. Apple's "
            "own restore (iTunes/Finder) can repair it."
        )
    fw = children[0]
    fw_size = int(fw.get("size") or 0)
    if not 16 * 1024 * 1024 <= fw_size <= 512 * 1024 * 1024:
        raise FirmwareError("The iPod's firmware partition is an unexpected size, so nothing was written.")
    label = (part.get("label") or "IPOD").upper()[:11]
    return {"disk": disk["path"], "fw_part": fw["path"], "data_part": part["path"],
            "label": label, "fw_size": fw_size}


def build_script(plan, firmware_path):
    """The root shell commands for one install (kept separate so it can
    be checked without touching a real iPod)."""
    q = shlex.quote
    return "\n".join([
        "set -e",
        f"dd if={q(firmware_path)} of={q(plan['fw_part'])} bs=1M conv=fsync status=none",
        "sync",
    ])


def install(mountpoint):
    """Writes the imported firmware to the iPod's firmware partition.
    Music, playlists and settings on the iPod are left as they are."""
    fw = current_firmware()
    if not fw:
        raise FirmwareError("Import a firmware file first.")
    family, generation = device_family(mountpoint)
    if family != "25":
        raise FirmwareError(
            f"Installing firmware only supports 5th-gen (Video) iPods; this one is {generation}."
        )
    plan = plan_device(mountpoint)
    if fw["size"] > plan["fw_size"]:
        raise FirmwareError("That firmware is larger than this iPod's firmware partition.")
    if not shutil.which("pkexec"):
        raise FirmwareError("pkexec isn't installed (needed to write to the iPod).")
    try:
        result = subprocess.run(
            ["pkexec", "/bin/sh", "-c", build_script(plan, fw["path"])],
            capture_output=True, text=True,
        )
    except OSError as e:
        raise FirmwareError(f"Couldn't start pkexec: {e}")
    if result.returncode in (126, 127) and not result.stderr.strip():
        raise FirmwareError("The password prompt was cancelled, so nothing was changed.")
    if result.returncode != 0:
        raise FirmwareError("Writing to the iPod failed: " + (result.stderr.strip() or f"exit {result.returncode}"))
