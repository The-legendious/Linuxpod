"""
applog.py — a small persistent log file for this app.

Sync warnings, errors, and notable status messages get appended here in
addition to showing up in the GUI, so you don't have to catch a dialog
box in the moment (or screenshot it) to report a problem later.
"""

import datetime
import os

import appdirs

LOG_DIR = appdirs.DATA_DIR
LOG_PATH = os.path.join(LOG_DIR, "app.log")
_OPEN_COUNT_PATH = os.path.join(LOG_DIR, "app.log.opens")
OPENS_BEFORE_RESET = 3


def log(message: str):
    """Appends one timestamped line. Best-effort - a logging failure
    should never be the reason the app itself breaks."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")
    except OSError:
        pass


def record_open():
    """Call this each time the log is actually opened for viewing (not
    just when its location is shown, e.g. "Show Log File Location").
    After OPENS_BEFORE_RESET views, the log is cleared and the count
    resets to 0 - most people only open it while chasing a specific
    problem and don't need the history afterward, and this keeps it
    from growing without bound over a long-running install. The clear
    happens after this view's content was already handed to the
    viewer, so the person still sees everything logged up to and
    including this open; only the *next* open starts fresh.
    Returns True if the log was just cleared, False otherwise.
    Best-effort, like log() - a failure here never blocks opening it."""
    count = 0
    try:
        with open(_OPEN_COUNT_PATH, "r", encoding="utf-8") as f:
            count = int(f.read().strip() or "0")
    except (OSError, ValueError):
        count = 0

    count += 1
    was_reset = count >= OPENS_BEFORE_RESET
    if was_reset:
        try:
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.write("")
        except OSError:
            pass
        count = 0

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(_OPEN_COUNT_PATH, "w", encoding="utf-8") as f:
            f.write(str(count))
    except OSError:
        pass

    return was_reset
