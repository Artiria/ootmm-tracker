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
import re
import sys

FROZEN = getattr(sys, "frozen", False)

HERE = os.path.dirname(os.path.abspath(__file__))

# Read-only, ships with the program.
RES_DIR = getattr(sys, "_MEIPASS", HERE)


def res(*parts):
    """A file that ships with the tracker: data/, overlay.html, tracker.lua."""
    return os.path.join(RES_DIR, *parts)


# Sends everything the tracker generates somewhere else: checks.json, the
# caches, options.json, the notes. For guards and audits, which must be able
# to run the real code without writing over the files of a real run.
#
# It exists because on 1 sep 2026 an audit round rebuilt a checks.json that
# was not its own and there was no way to stop it: OUT is USER_DIR, and from
# source USER_DIR is the checkout. The evidence of a live seed was lost to a
# guard doing its job.
HOME_ENV = "OOTMM_TRACKER_HOME"


def _blank(name):
    """A variable that is SET but empty is a caller asking for something and
    naming nowhere. Treating it as unset is the silent fallback these two
    exist to prevent, so it is an error like any other unusable value."""
    raise SystemExit(
        f"{name} is set to an empty string, which is not a folder.\n"
        "Refusing to fall back: something asked to be redirected and did not\n"
        "say where, and guessing means writing where it meant not to.")


def _user_dir():
    forced = os.environ.get(HOME_ENV)
    if forced is not None and not forced.strip():
        _blank(HOME_ENV)
    if forced:
        try:
            os.makedirs(forced, exist_ok=True)
        except OSError as ex:
            # Never fall back to the real folder. A sandbox that quietly is
            # not one writes over exactly the files it was asked to protect,
            # and the caller has no way of noticing.
            raise SystemExit(
                f"{HOME_ENV} is set to {forced!r}, which cannot be used ({ex}).\n"
                "Refusing to fall back to the real folder: whatever asked for a\n"
                "sandbox would have written over the files it meant to protect.")
        return os.path.abspath(forced)
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


# --------------------------------------------------------------------------
# data/: the one thing here that is pinned to an OoTMM version
# --------------------------------------------------------------------------
#
# The addresses, the placement and the bits come from the ROM, so they follow
# whatever version the seed is. The check NAMES do not: they come from
# OoTMM's own data/, and the copy that ships is v32.0's. When upstream renames
# a check the tracker keeps calling it by the old name, and on 1 sep 2026 that
# turned out to be worse than stale -- v32.3 moved `Great Bay Coast Pot
# Ledge 1` onto a different pot, so the old name is now a name that exists and
# points somewhere else.
#
# So a newer data/ can be downloaded and adopted (dataupdate.py). It never
# touches the bundled copy: a downloaded tree lives on its own under
# `data-updates/<tag>/`, and one line of `active.json` says which one is in
# force. Nothing is adopted until it has been measured against the open ROM.
DATA_ENV = "OOTMM_TRACKER_DATA"
UPDATES_DIRNAME = "data-updates"
ACTIVE_FILE = "active.json"

# What a data/ has to hold before it counts as one, in either layout: the CSV
# pool of v32 and earlier, or the per-scene XML of gen 943 and later.
_DATA_MARKERS = (("pool_oot.csv",), ("pool", "pool_oot.csv"), ("checks",))


def _looks_like_data(d):
    return any(os.path.exists(os.path.join(d, *m)) for m in _DATA_MARKERS)


def updates_dir(*parts):
    """Where downloaded data trees live, one folder per tag."""
    return os.path.join(USER_DIR, UPDATES_DIRNAME, *parts)


def active_data_tag():
    """The tag of the adopted data/, or None when the bundled one is in force."""
    import json

    try:
        with open(updates_dir(ACTIVE_FILE), encoding="utf-8") as fh:
            tag = json.load(fh).get("tag")
    except (OSError, ValueError):
        return None
    if not tag or not _looks_like_data(updates_dir(tag)):
        # A stamp naming a tree that is not there means an update that did not
        # finish, or a folder somebody deleted by hand. The bundled data is
        # still good, so this is not an error: it is just not adopted.
        return None
    return tag


def data_dir():
    """The data/ in force: an adopted download if there is one, else bundled.

    `OOTMM_TRACKER_DATA` forces a folder outright, which is how a guard tests
    one version's names against another version's ROM without adopting
    anything.
    """
    forced = os.environ.get(DATA_ENV)
    if forced is not None and not forced.strip():
        _blank(DATA_ENV)
    if forced:
        if not _looks_like_data(forced):
            raise SystemExit(
                f"{DATA_ENV} is set to {forced!r}, which holds no pool_oot.csv and no\n"
                "checks/ folder, so it is not an OoTMM data/. Refusing to fall back to\n"
                "the bundled one: the caller asked to be judged against that tree.")
        return os.path.abspath(forced)
    tag = active_data_tag()
    return updates_dir(tag) if tag else res("data")


# What the copy that ships is: the version its names were taken from. Every
# checks.json says this unless a download is in force, and it is the answer to
# "which OoTMM's labels am I looking at".
BUNDLED_VERSION = "v32.0"


def data_version():
    """A label for the data/ in force, for whoever writes it down.

    Never a guess about the SEED -- the ROM does not carry its version and
    this does not pretend to know it. It says which labels are being used, so
    that a checks.json built with a forced or downloaded data/ does not go on
    claiming to be the bundled one.
    """
    forced = os.environ.get(DATA_ENV)
    if forced is not None and not forced.strip():
        _blank(DATA_ENV)
    if forced:
        # Pointing the variable AT the bundled folder is the bundled version,
        # not some third thing. It reads like a third thing otherwise, and
        # the comparison table says it out loud (dataupdate names the
        # baseline explicitly, so this is the common case, not a corner).
        if os.path.normcase(os.path.abspath(forced)) == os.path.normcase(res("data")):
            return BUNDLED_VERSION
        name = os.path.basename(os.path.normpath(forced))
        return name if re.fullmatch(r"v\d+(\.\d+)*", name) else f"{DATA_ENV}={forced}"
    return active_data_tag() or BUNDLED_VERSION


def data_file(name, base=None):
    """One file of data/, in either layout.

    The copy that ships is flat (`data/gi.yml`); OoTMM's own repository keeps
    it in folders (`data/defs/gi.yml`, `data/pool/pool_oot.csv`), and a
    downloaded tree is that repository verbatim. Both are answered here so
    that no caller has to know which one it got. Missing files come back as
    the flat path, so whoever opens it reports the name a reader expects.

    It used to live in mkchecks alone; placement.py had its own flat
    `DATA / "gi.yml"`, and the day a downloaded data/ was first measured that
    was the file it could not find -- quietly, because the caller catches the
    error and builds anyway (1 sep 2026).

    `base` is the folder to look in, and callers pass their OWN module-level
    DATA rather than letting this work it out. That is not ceremony: the
    guards rebind `mkchecks.DATA` to a fixture to test one generation's data
    against another's ROM, and a lookup that quietly consulted data_dir()
    instead would read the shipped copy while the caller believed it was
    reading the fixture.
    """
    d = data_dir() if base is None else str(base)
    for cand in (os.path.join(d, name),
                 os.path.join(d, "defs", name),
                 os.path.join(d, "pool", name)):
        if os.path.exists(cand):
            return cand
    return os.path.join(d, name)
