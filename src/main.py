#!/usr/bin/env python3
"""
Linuxpod — a simple iTunes-like library manager that can sync
music onto a click-wheel iPod (Classic / Nano / Shuffle) on Linux.

Run with:  python3 main.py
"""
import faulthandler
import os
import sys
import threading
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import applog  # noqa: E402

CRASH_LOG = os.path.join(applog.LOG_DIR, "crash.log")


def _install_crash_logging():
    """Makes sure a crash leaves something behind to look at: Python
    errors go to app.log, and anything only printed to the terminal
    (an X server error, a hard crash's stack dump) goes to crash.log,
    since the app is often started from a script whose window closes
    with it."""
    try:
        os.makedirs(applog.LOG_DIR, exist_ok=True)
        crash_file = open(CRASH_LOG, "a", buffering=1, encoding="utf-8")
    except OSError:
        return
    crash_file.write(f"\n=== started (pid {os.getpid()}) ===\n")
    faulthandler.enable(file=crash_file, all_threads=True)
    if os.environ.get("MUSIC_MANAGER_STDERR") != "terminal":
        print(f"Linuxpod: errors are logged to {applog.LOG_PATH} and {CRASH_LOG}")
        sys.stderr.flush()
        os.dup2(crash_file.fileno(), 2)

    def excepthook(exc, val, tb):
        applog.log("UNCAUGHT ERROR:\n" + "".join(traceback.format_exception(exc, val, tb)))
        sys.__excepthook__(exc, val, tb)

    def thread_excepthook(args):
        applog.log(f"UNCAUGHT ERROR in thread {args.thread.name if args.thread else '?'}:\n"
                   + "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook


def main():
    _install_crash_logging()
    from gui import MainApp

    app = MainApp()
    app.mainloop()
    applog.log("=== Linuxpod closed ===")


if __name__ == "__main__":
    main()
