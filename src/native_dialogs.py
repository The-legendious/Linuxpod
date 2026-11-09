"""
native_dialogs.py — uses the desktop's own native file picker (zenity
on GTK desktops like GNOME/Cinnamon/XFCE, kdialog on KDE Plasma)
instead of Tkinter's built-in file dialogs, which look and behave
noticeably differently from the rest of the desktop. Falls back to
Tkinter's own filedialog if neither is installed, so the app still
works either way - it just won't match the desktop's native look in
that fallback case.

Detection happens once at import time (which tool is installed doesn't
change while the app is running), and zenity is preferred when both
are present since it's the more common baseline on most distros. Only
one tool is ever tried per call - cancelling that tool's dialog means
"nothing chosen", not a cue to try a different tool next.
"""

import shlex
import shutil
import subprocess
from tkinter import filedialog

_ZENITY = shutil.which("zenity")
_KDIALOG = shutil.which("kdialog")
_TOOL = "zenity" if _ZENITY else ("kdialog" if _KDIALOG else None)


def _run_native(cmd):
    """Runs a native dialog command and returns its stdout, stripped,
    or None on any failure (crashed, or the person hit Cancel - both
    indistinguishable from a non-zero exit here, which is fine: the
    caller treats None as "nothing chosen" either way)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _zenity_filters(filetypes):
    """Tkinter's [("Image files", "*.jpg *.jpeg *.png"), ...] becomes
    zenity's repeated --file-filter='Image files | *.jpg *.jpeg *.png'
    arguments (confirmed against zenity's own documented syntax:
    --file-filter=NAME | PATTERN1 PATTERN2 ...)."""
    args = []
    for label, patterns in filetypes or []:
        args.append(f"--file-filter={label} | {patterns}")
    return args


def choose_directory(title="Choose a folder"):
    """Returns the chosen directory path, or None if cancelled/closed."""
    if _TOOL == "zenity":
        return _run_native(["zenity", "--file-selection", "--directory", "--title", title])
    if _TOOL == "kdialog":
        return _run_native(["kdialog", "--getexistingdirectory", ".", "--title", title])
    return filedialog.askdirectory(title=title) or None


def choose_open_file(title="Choose a file", filetypes=None):
    """Returns the chosen file path, or None if cancelled/closed."""
    if _TOOL == "zenity":
        cmd = ["zenity", "--file-selection", "--title", title] + _zenity_filters(filetypes)
        return _run_native(cmd)
    if _TOOL == "kdialog":
        return _run_native(["kdialog", "--getopenfilename", ".", "--title", title])
    return filedialog.askopenfilename(title=title, filetypes=filetypes or []) or None


def choose_open_files(title="Choose files", filetypes=None):
    """Returns a list of chosen file paths (empty if cancelled/closed)."""
    if _TOOL == "zenity":
        cmd = ["zenity", "--file-selection", "--multiple", "--separator=\n",
               "--title", title] + _zenity_filters(filetypes)
        result = _run_native(cmd)
        return [p for p in result.split("\n") if p] if result else []
    if _TOOL == "kdialog":
        # kdialog's multi-select (--multiple --getopenfilename) prints
        # paths space-separated rather than newline-separated, and
        # quotes any that contain spaces - shlex handles that quoting
        # so a path with a literal space still parses correctly.
        result = _run_native(["kdialog", "--multiple", "--getopenfilename", ".", "--title", title])
        return shlex.split(result) if result else []
    files = filedialog.askopenfilenames(title=title, filetypes=filetypes or [])
    return list(files) if files else []
