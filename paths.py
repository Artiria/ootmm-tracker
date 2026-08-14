#!/usr/bin/env python3
"""
paths.py - where things are read from and where they get written.

Running from source, both answers are "next to the .py", which is what the
whole project assumed until now. Inside a PyInstaller executable they stop
being the same place and each one breaks differently:

  * The bundled data (`data/`, `overlay.html`, `Scripts/tracker.lua`) is
    unpacked into a temporary folder, `sys._MEIPASS`. `__file__` points inside
    the frozen module, so it does not find them.
  * The generated files (`checks.json`, `icons.*`, `discover-cache.json`) must
    NOT go there: that folder is deleted on exit, so every run would rebuild
    the tables from scratch, which is the slow part.

So: `res()` for what ships with the program, `user()` for what it produces.
From source both return the project folder and nothing changes.
"""

import os
import sys

FROZEN = getattr(sys, "frozen", False)

HERE = os.path.dirname(os.path.abspath(__file__))

# Read-only, ships with the program.
RES_DIR = getattr(sys, "_MEIPASS", HERE)


def res(*parts):
    """A file that ships with the tracker: data/, overlay.html, tracker.lua."""
    return os.path.join(RES_DIR, *parts)


def _user_dir():
    if not FROZEN:
        return HERE
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "OoTMM-Tracker")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        # If %LOCALAPPDATA% is not writable there is nowhere better to go than
        # next to the executable, which at least is not a temporary folder.
        d = os.path.dirname(os.path.abspath(sys.executable))
    return d


USER_DIR = _user_dir()


def user(*parts):
    """A file the tracker generates: checks.json, icons.*, the cache."""
    return os.path.join(USER_DIR, *parts)
