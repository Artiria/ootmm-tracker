#!/usr/bin/env python3
"""
discover.py - work out on its own which ROM and spoiler you are playing.

You used to have to pass `--rom` and `--spoiler` by hand and remember to
regenerate checks.json and icons.json when changing seed. This solves it by
looking at what the emulator already knows.

How the ROM is identified
-------------------------
Project64 stores saves in `Save/OOT+MM COMBO-<hash>/`, and that hash is the
**MD5 of the ROM with its 4-byte words byte-swapped**, which is the order the
emulator keeps it in internally. Verified: the MD5 of the file as-is does not
match, the one of the swap4 version does, exactly.

That gives a chain with nothing guessed in it:

  1. the most recently written save folder says which seed you are playing
  2. its hash identifies the ROM with no ambiguity
  3. that ROM is looked up among `Project64.cfg`'s recent ones (`Recent Rom N`)

If nothing matches, it falls back to `Recent Rom 0`, the last one opened.

The spoiler is looked for next to the ROM: for `OoTMM-<id>.z64` it prefers
`OoTMM-Spoiler-<id>.txt`, and failing that, any spoiler in that folder.
"""

import contextlib
import glob
import hashlib
import io
import json
import os
import re

import paths

CACHE = paths.user("discover-cache.json")


# --------------------------------------------------------------------------
# Emulator
# --------------------------------------------------------------------------


# Exe names (lower-case prefixes) of the emulators whose folder is worth
# knowing: Project64-EM ships as `Project64-EM.exe`, plain builds as
# `Project64.exe`.
EMULATOR_EXES = ("project64",)


def running_emulators():
    """Folders of the Project64 builds running right now, newest first.

    Where the emulator is installed is not the tracker's to guess: the second
    person to try it had it outside every folder `find_emulator` looks in, and
    sat on "no ROM found" with the seed open. A running `Project64-EM.exe`
    says where it lives, and next to it are the `Config/Project64.cfg` naming
    the open ROM and the `Scripts` folder the Lua goes in.

    Windows only, no dependencies: a Toolhelp snapshot lists every process's
    exe name without opening it, and the full path comes from
    QueryFullProcessImageNameW with the *limited* query right, which is
    granted across elevation for the same user. Anything unreadable is
    skipped -- this is a hint, never a reason to fail. Newest process first:
    with two builds open, the one started last is the one being played.
    """
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return []

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    try:
        k32 = ctypes.WinDLL("kernel32")
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
    except (AttributeError, OSError):
        return []

    TH32CS_SNAPPROCESS = 0x2
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return []
    pids = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        more = k32.Process32FirstW(snap, ctypes.byref(entry))
        while more:
            name = entry.szExeFile.lower()
            if name.endswith(".exe") and name.startswith(EMULATOR_EXES):
                pids.append(entry.th32ProcessID)
            more = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)

    found = []
    for pid in pids:
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                continue
            times = [wintypes.FILETIME() for _ in range(4)]
            started = 0
            if k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
                started = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        finally:
            k32.CloseHandle(handle)
        folder = os.path.dirname(buf.value)
        if os.path.isfile(os.path.join(folder, "Config", "Project64.cfg")):
            found.append((started, folder))
    found.sort(reverse=True)
    out = []
    for _, folder in found:
        if folder not in out:
            out.append(folder)
    return out


def emulator_candidates(hint=None):
    """`(how, folder)` pairs, the most trustworthy first: what the user said,
    what is running, what worked last time, then the usual places."""
    if hint:
        yield "--emu", hint
    if os.environ.get("PJ64_DIR"):
        yield "PJ64_DIR", os.environ["PJ64_DIR"]
    for d in running_emulators():
        yield "running", d
    cache = load_cache()
    if cache.get("emu"):
        yield "cache", cache["emu"]
    # last resort: look nearby, without sweeping the whole disk
    home = os.path.expanduser("~")
    for pat in (
        os.path.join(home, "Downloads", "*", "Project64*", "Config", "Project64.cfg"),
        os.path.join(home, "Downloads", "Project64*", "Config", "Project64.cfg"),
        os.path.join(home, "*", "Project64*", "Config", "Project64.cfg"),
    ):
        hits = glob.glob(pat)
        if hits:
            yield "nearby", os.path.dirname(os.path.dirname(sorted(hits)[0]))


HOW_FOUND = {
    "--emu": "from --emu",
    "PJ64_DIR": "from PJ64_DIR",
    "running": "running now",
    "cache": "remembered from last time",
    "nearby": "found nearby",
}


def find_emulator_how(hint=None):
    """`(folder, how)` for the emulator folder -- the one holding
    Config/Project64.cfg -- or `(None, None)`. `how` is a HOW_FOUND key."""
    for how, d in emulator_candidates(hint):
        if d and os.path.isfile(os.path.join(d, "Config", "Project64.cfg")):
            return d, how
    return None, None


def find_emulator(hint=None):
    """The emulator folder, the one holding Config/Project64.cfg."""
    return find_emulator_how(hint)[0]


def recent_roms(emu):
    """The `Recent Rom N` paths from Project64.cfg, in order."""
    cfg = os.path.join(emu, "Config", "Project64.cfg")
    out = []
    try:
        with open(cfg, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.match(r"^Recent Rom (\d+)\s*=\s*(.+?)\s*$", line)
                if m:
                    out.append((int(m.group(1)), m.group(2)))
    except OSError:
        return []
    return [p for _, p in sorted(out)]


def active_save_hash(emu):
    """Hash of the most recently written save folder."""
    saves = glob.glob(os.path.join(emu, "Save", "*-" + "[0-9A-Fa-f]" * 32))
    if not saves:
        return None
    newest = max(saves, key=lambda p: _newest_mtime(p))
    return os.path.basename(newest).rsplit("-", 1)[1].upper()


def _newest_mtime(d):
    ts = [os.path.getmtime(d)]
    try:
        ts += [os.path.getmtime(os.path.join(d, f)) for f in os.listdir(d)]
    except OSError:
        pass
    return max(ts)


# --------------------------------------------------------------------------
# ROM
# --------------------------------------------------------------------------


def rom_hash(path):
    """MD5 of the ROM in the emulator's internal order (words byte-swapped)."""
    raw = open(path, "rb").read()
    swapped = bytearray(raw)
    swapped[0::4], swapped[1::4], swapped[2::4], swapped[3::4] = (
        raw[3::4], raw[2::4], raw[1::4], raw[0::4],
    )
    return hashlib.md5(bytes(swapped)).hexdigest().upper()


def rom_hash_cached(path):
    """Same, but without re-reading 64 MB on every startup."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    cache = load_cache()
    key = os.path.normcase(os.path.abspath(path))
    hit = (cache.get("hashes") or {}).get(key)
    if hit and hit.get("mtime") == st.st_mtime and hit.get("size") == st.st_size:
        return hit["md5"]
    md5 = rom_hash(path)
    cache.setdefault("hashes", {})[key] = {
        "md5": md5, "mtime": st.st_mtime, "size": st.st_size,
    }
    save_cache(cache)
    return md5


def find_rom(emu, verbose=True):
    """The ROM the emulator has open.

    There are two independent signals here and they answer slightly different
    questions, which is the whole point:

      * **`Recent Rom 0`** is the last ROM *opened*. That is what is on screen.
      * **the newest save folder** is the last ROM *saved*. It lags: until you
        save, it still names the seed you were playing before.

    So the open one wins and the save is the cross-check. It used to be the
    other way round, and that is what bit on 14 ago 2026: a whole session read
    against dockiNAq's tables while dHN9YY2c was being played, because
    dockiNAq's save was newer. Nothing broke — the overlay just showed another
    seed's items, confidently.

    When the two disagree it says so. A wrong answer here cannot be caught
    later: another seed of the same version has the very same locations with
    different items, so no barrier downstream can tell (see check_spoiler in
    overlay.py).
    """
    roms = [p for p in recent_roms(emu) if os.path.isfile(p)]
    if not roms:
        return None
    abierta = roms[0]

    want = active_save_hash(emu)
    guardada = None
    if want:
        for p in roms:
            if rom_hash_cached(p) == want:
                guardada = p
                break

    if guardada and not _same(guardada, abierta):
        if verbose:
            print()
            print("[auto] CAREFUL: the emulator has one ROM open and a different")
            print("[auto] one was saved last. Going with the open one.")
            print(f"[auto]    open (used) : {abierta}")
            print(f"[auto]    saved last  : {guardada}")
            print("[auto] That is normal just after changing seed. If the items")
            print("[auto] the tracker shows are not the ones in your game, this")
            print("[auto] is the line that says why.")
            print()
    elif verbose:
        como = "open, and its save agrees" if guardada else "open"
        print(f"[auto] ROM ({como}): {abierta}")
    return abierta


def find_spoiler(rom, verbose=True):
    """The spoiler that goes with that ROM, if it sits near it.

    A seed's files are `OoTMM-<id>.z64` and `OoTMM-Spoiler-<id>.txt`, and that
    id is the only thing tying them together: a spoiler with another id is
    another seed's, however close it lies. The ROM often ends up loose in
    Downloads with its zip unpacked into an `OoTMM-<id>` folder beside it, so
    that folder is looked in too.

    Any `*Spoiler*.txt` in the folder used to do as a fallback, and on 24 ago
    2026 that picked another multiworld's spoiler out of Downloads and put its
    items on every check of the seed being played. The fallback now applies
    only to a ROM whose name carries no seed id, and only when there is exactly
    one spoiler to pick from.
    """
    if not rom:
        return None
    d = os.path.dirname(rom)
    stem = os.path.splitext(os.path.basename(rom))[0]
    m = re.match(r"OoTMM-([A-Za-z0-9]+)", stem)
    if m:
        sid = m.group(1)
        name = f"OoTMM-Spoiler-{sid}.txt"
        cands = [os.path.join(d, name), os.path.join(d, f"OoTMM-{sid}", name)]
        cands += sorted(glob.glob(os.path.join(glob.escape(d), "*", name)))
        for cand in cands:
            if os.path.isfile(cand):
                if verbose:
                    print(f"[auto] spoiler: {cand}")
                return cand
        if verbose:
            print(f"[auto] no spoiler for seed {sid} near the ROM (not needed: items come from it)")
        return None
    hits = sorted(glob.glob(os.path.join(glob.escape(d), "*Spoiler*.txt")))
    if len(hits) == 1:
        if verbose:
            print(f"[auto] spoiler: {hits[0]} (the only one next to a ROM with no seed id in its name)")
        return hits[0]
    if verbose:
        if hits:
            print(f"[auto] {len(hits)} spoilers next to the ROM and no seed id in its name to "
                  "tell which is its own; none used (items come from the ROM)")
        else:
            print("[auto] no spoiler found next to the ROM (not needed: items come from it)")
    return None


# --------------------------------------------------------------------------
# Regeneration
# --------------------------------------------------------------------------


def _built_from(path, key="rom"):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get(key)
    except (OSError, ValueError):
        return None


def _same(a, b):
    return bool(a) and bool(b) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _has_key(path, *keys):
    """Whether the JSON at `path` carries that key at all (null counts as yes).

    Tells a table built by an older generator -- which lacks the key -- from
    one where the generator looked and found nothing, which stores null. Only
    the first should trigger a rebuild, or a ROM that cannot be read would be
    regenerated on every start.

    Several keys walk into nested objects: _has_key(p, "payload", "oot",
    "triforce") asks for the last one down that path.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            node = json.load(fh)
    except (OSError, ValueError):
        return False
    for k in keys[:-1]:
        node = node.get(k) if isinstance(node, dict) else None
        if node is None:
            # a null on the way is an older table, not a considered "nothing"
            return False
    return isinstance(node, dict) and keys[-1] in node


def _generate(module, argv, verbose):
    """Run one of the generators, in-process, and return its exit code.

    They used to be subprocesses (`sys.executable mkchecks.py --rom ...`).
    That cannot work inside the .exe: there is no interpreter to hand a script
    to, and `sys.executable` is the tracker itself, so it would relaunch the
    tracker instead of generating anything. Calling `main()` does the same job
    in both worlds.
    """
    import importlib

    try:
        mod = importlib.import_module(module)
        quiet = contextlib.redirect_stdout(io.StringIO())
        with contextlib.nullcontext() if verbose else quiet:
            return mod.main(argv) or 0
    except SystemExit as ex:
        # argparse and the "these tables are not this version's" abort
        return ex.code if isinstance(ex.code, int) else 1
    except Exception as ex:
        print(f"[auto] {module} failed: {type(ex).__name__}: {ex}")
        return 1


def ensure_tables(rom, spoiler, verbose=True):
    """Regenerate checks.json and icons.json if they are not from this ROM.

    Returns the list of what got rebuilt.
    """
    hecho = []
    if not rom:
        return hecho

    checks = paths.user("checks.json")
    # `payload` is what mkchecks reads out of the ROM's code (payload.py):
    # a checks.json without the key was built before that existed, and the
    # overlay would fall back to sweeping for gSharedCustomSave without a word.
    # A table built before the `souls` key existed is stale the same way.
    # `scene_layers` (the scenes' alternate headers) and the Triforce record
    # inside `payload` are the same story, one generator later: without them the
    # panel guesses the loaded setup and the Triforce figure never shows. And
    # `last_scene` (with it, the grotto byte) is what tells one grotto or
    # fairy fountain from the others sharing its scene.
    if (not os.path.exists(checks) or not _same(_built_from(checks), rom)
            or not _has_key(checks, "payload")
            or not _has_key(checks, "souls")
            or not _has_key(checks, "scene_layers")
            or not _has_key(checks, "payload", "oot", "triforce")
            or not _has_key(checks, "payload", "oot", "last_scene")
            or not _has_key(checks, "mq")):
        argv = ["--rom", rom]
        if spoiler:
            argv += ["--spoiler", spoiler]
        if verbose:
            print("[auto] regenerating checks.json for this ROM...")
        if _generate("mkchecks", argv, verbose):
            # mkchecks refuses when the tables do not add up, usually
            # because the seed is from another OoTMM version. That does not
            # stop the overlay, but it has to say loudly that the checks are
            # not this ROM's.
            viejo = _built_from(checks)
            print()
            print("[auto] WARNING: could not generate the tables for this ROM.")
            if viejo:
                print(f"[auto] the checks about to be used are from another ROM: {viejo}")
                print("[auto] items and inventory are still fine; the CHECKS may be wrong.")
            else:
                print("[auto] no checks.json: the overlay will only show items.")
            print()
        else:
            hecho.append("checks.json")

    icons = paths.user("icons.json")
    if not os.path.exists(icons) or not _same(_built_from(icons), rom):
        if verbose:
            print("[auto] extracting icons from the ROM...")
        if _generate("mkicons", ["--rom", rom], verbose):
            print("[auto] WARNING: could not extract the icons from this ROM.")
        else:
            hecho.append("icons.png")

    return hecho


# --------------------------------------------------------------------------
# tracker.lua
# --------------------------------------------------------------------------


def ensure_lua(emu=None, force=False, verbose=True):
    """Put `tracker.lua` in the emulator's Scripts folder.

    Returns `(path, status)`, status being "written", "same", "kept" (there is
    another version there and it was left alone) or None with no path when the
    emulator could not be found. The caller has to be able to tell those apart:
    saying "installed" over a script that was not replaced is the kind of lie
    that costs an evening.

    Running from source this is the user's own business: the project's copy and
    the emulator's are the *same file* (a hard link), and overwriting it would
    quietly turn it into two files that then drift apart. So it only writes
    when asked (`force`) or from the .exe, where the script is inside the
    bundle and there is nothing to link to.

    An existing script is never replaced silently: if it differs, it says so
    and leaves it alone.
    """
    found = find_emulator(emu)
    if not found:
        return None, None
    dst = os.path.join(found, "Scripts", "tracker.lua")
    try:
        src_bytes = open(paths.res("Scripts", "tracker.lua"), "rb").read()
    except OSError:
        return None, None

    if os.path.exists(dst):
        try:
            same = open(dst, "rb").read() == src_bytes
        except OSError:
            same = False
        if same or not force:
            if not same and verbose:
                print(f"[auto] {dst} is another version of the script;")
                print("[auto] not touching it. To replace it: install-lua --force")
            return dst, ("same" if same else "kept")
    elif not (force or paths.FROZEN):
        # nothing there and nobody asked: say where it goes, do not decide
        if verbose:
            print(f"[auto] tracker.lua is not in {os.path.dirname(dst)}")
        return dst, None

    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        # written as bytes on purpose: a BOM here breaks the Lua parser
        with open(dst, "wb") as fh:
            fh.write(src_bytes)
    except OSError as ex:
        if verbose:
            print(f"[auto] could not write {dst}: {ex}")
        return dst, None
    if verbose:
        print(f"[auto] tracker.lua installed in {dst}")
    return dst, "written"


# --------------------------------------------------------------------------


def load_cache():
    try:
        with open(CACHE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_cache(cache):
    try:
        with open(CACHE, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=1)
    except OSError:
        pass


def resolve(rom=None, spoiler=None, emu=None, verbose=True):
    """Everything together: (rom, spoiler, rebuilt)."""
    if not rom:
        found, how = find_emulator_how(emu)
        if not found:
            if verbose:
                print("[auto] cannot find Project64.cfg; pass --rom or --emu")
            return None, spoiler, []
        if verbose:
            print(f"[auto] emulator: {found} ({HOW_FOUND.get(how, how)})")
        cache = load_cache()
        cache["emu"] = found
        save_cache(cache)
        rom = find_rom(found, verbose)
    if rom and not spoiler:
        spoiler = find_spoiler(rom, verbose)
    return rom, spoiler, ensure_tables(rom, spoiler, verbose)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--emu", help="emulator folder")
    ap.add_argument("--rom")
    ap.add_argument("--spoiler")
    a = ap.parse_args()
    r, s, hecho = resolve(a.rom, a.spoiler, a.emu)
    print()
    print("ROM     :", r)
    print("spoiler :", s)
    print("rebuilt :", ", ".join(hecho) or "nothing, already up to date")
