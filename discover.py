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

Project64 also opens a seed straight from its `.zip`, and then `Recent Rom 0`
names the zip. The ROM inside is taken out once, into the tracker's own
folder (`archive-roms/`), and from there on it is a `.z64` like any other;
the spoiler is looked for beside the zip as well as beside the ROM.
"""

import contextlib
import glob
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import zipfile

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
# Archives
# --------------------------------------------------------------------------
#
# Project64 opens a seed straight from its .zip and writes THAT path to
# `Recent Rom 0`. On 22 oct 2026 that was the seed being played: a 54 MB zip
# went down the chain as if it were the ROM, rom_hash() tripped on a length
# that was no multiple of four, auto-detection apologised in one line and the
# overlay came up on the previous seed's tables, confidently. So an archive is
# opened here, once: the ROM inside goes to a folder of the tracker's own and
# from then on it is a .z64 like any other. The folder is keyed by the
# archive's path, size and mtime, so a re-download is a new ROM and an
# unchanged zip costs nothing after the first time.

ROM_EXTENSIONS = (".z64", ".n64", ".v64")
ARCHIVE_EXTENSIONS = (".zip",)
ARCHIVES_DIRNAME = "archive-roms"
ARCHIVES_KEEP = 3                 # extracted seeds kept, newest first
ORIGIN_FILE = "origin.json"       # which archive a folder came out of
_SPOILER_NAME = re.compile(r"OoTMM-Spoiler-[A-Za-z0-9]+\.txt$")


def archives_dir(*parts):
    """Where ROMs taken out of archives live: one folder per archive."""
    return paths.user(ARCHIVES_DIRNAME, *parts)


def is_archive(path):
    return bool(path) and os.path.splitext(path)[1].lower() in ARCHIVE_EXTENSIONS


def _archive_slot(archive):
    st = os.stat(archive)
    key = f"{os.path.normcase(os.path.abspath(archive))}|{st.st_size}|{st.st_mtime_ns}"
    return archives_dir(hashlib.sha1(key.encode("utf-8", "surrogatepass")).hexdigest()[:16])


def _rom_members(names):
    """The ROM entries of an archive, .z64 first, else in the archive's order."""
    roms = [n for n in names if not n.endswith("/") and n.lower().endswith(ROM_EXTENSIONS)]
    return sorted(roms, key=lambda n: (not n.lower().endswith(".z64"), names.index(n)))


def archive_origin(rom):
    """The archive `rom` was taken out of, or None for a loose ROM."""
    try:
        with open(os.path.join(os.path.dirname(rom), ORIGIN_FILE), encoding="utf-8") as fh:
            origin = json.load(fh).get("archive")
    except (OSError, ValueError, AttributeError):
        return None
    return origin if isinstance(origin, str) and origin else None


def _prune_archives(keep):
    """Drop the oldest extracted seeds past ARCHIVES_KEEP. Never `keep`, and
    never a folder without an origin.json: that one is not ours to delete."""
    try:
        slots = [os.path.join(archives_dir(), d) for d in os.listdir(archives_dir())]
    except OSError:
        return
    ours = [s for s in slots if os.path.isfile(os.path.join(s, ORIGIN_FILE))
            and not _same(s, keep)]
    ours.sort(key=os.path.getmtime, reverse=True)
    for stale in ours[ARCHIVES_KEEP - 1:]:
        shutil.rmtree(stale, ignore_errors=True)


def rom_file(path, extract=True, verbose=True):
    """The ROM file behind a path from the emulator's list, or None.

    A .z64 (.n64, .v64) is its own answer. For a .zip it is the ROM inside,
    extracted into archives_dir() -- with any spoiler that travels in the same
    zip -- the first time, and found there afterwards. With extract=False the
    path it would have is returned without touching the archive, for a caller
    that only wants to compare (check_rom_open asks every few seconds).

    None, and a line saying why, for a file no reader here can open: another
    archive format, a zip with no ROM in it, a zip that is not one.
    """
    if not path:
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext in ROM_EXTENSIONS:
        return path
    if ext not in ARCHIVE_EXTENSIONS:
        if verbose:
            print(f"[auto] the emulator has {path} open, and the tracker cannot read a ROM")
            print("[auto] out of that: it wants a .z64, or a .zip with one inside.")
        return None
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            members = _rom_members(names)
            if not members:
                if verbose:
                    print(f"[auto] {path} holds no ROM (.z64/.n64/.v64); nothing to track in it")
                return None
            member = members[0]
            slot = _archive_slot(path)
            target = os.path.join(slot, os.path.basename(member))
            info = z.getinfo(member)
            if os.path.isfile(target) and os.path.getsize(target) == info.file_size:
                return target
            if not extract:
                return target
            if verbose:
                print(f"[auto] the emulator opened an archive: {path}")
                if len(members) > 1:
                    print(f"[auto] it holds {len(members)} ROMs; using the first, {member}")
                print(f"[auto] taking {member} out of it ({info.file_size:,} bytes)...")
            os.makedirs(slot, exist_ok=True)
            wanted = [member] + [n for n in names if _SPOILER_NAME.search(n)]
            for name in wanted:
                dst = os.path.join(slot, os.path.basename(name))
                tmp = dst + ".tmp"
                with z.open(name) as src, open(tmp, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                os.replace(tmp, dst)
            with open(os.path.join(slot, ORIGIN_FILE), "w", encoding="utf-8") as fh:
                json.dump({"archive": os.path.abspath(path), "member": member}, fh, indent=1)
    except (OSError, zipfile.BadZipFile) as ex:
        if verbose:
            print(f"[auto] {path} could not be opened as a zip ({type(ex).__name__}: {ex})")
        return None
    _prune_archives(slot)
    return target


def _rom_bytes(path):
    """The ROM's bytes, out of the file or out of the archive it is in; None
    when there is no ROM there. Reads, never writes: hashing the recent list
    must not extract every zip in it."""
    try:
        if is_archive(path):
            with zipfile.ZipFile(path) as z:
                members = _rom_members(z.namelist())
                return z.read(members[0]) if members else None
        with open(path, "rb") as fh:
            return fh.read()
    except (OSError, zipfile.BadZipFile):
        return None


# --------------------------------------------------------------------------
# ROM
# --------------------------------------------------------------------------


def rom_hash(path):
    """MD5 of the ROM in the emulator's internal order (words byte-swapped).

    None when there is no ROM to hash: a file no reader can open, or one whose
    length is not a multiple of four -- which is not a ROM, whatever its name,
    and used to come back as a ValueError that took auto-detection down with it.
    """
    raw = _rom_bytes(path)
    if raw is None or len(raw) % 4:
        return None
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
    recent = [p for p in recent_roms(emu) if os.path.isfile(p)]
    if not recent:
        return None
    abierta = recent[0]

    want = active_save_hash(emu)
    guardada = None
    if want:
        for p in recent:
            # an archive is hashed by the ROM inside it, read in memory:
            # nothing in this loop writes
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
    # What the readers get: the ROM itself, out of its archive when the
    # emulator opened one. None, said out loud, when there is no reading it.
    usable = rom_file(abierta, verbose=verbose)
    if verbose and usable and not _same(usable, abierta):
        print(f"[auto] tracking the ROM inside it: {usable}")
    return usable


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
    # A ROM taken out of an archive has two homes: the folder it was put in
    # (where a spoiler packed in the same zip went too) and the archive's own,
    # which is where the unpacked `OoTMM-<id>` folder sits. The seed id comes
    # from whichever name carries one; the zip's "(2)" copy suffix does not
    # get in the way of that.
    origin = archive_origin(rom)
    places = [rom] + ([origin] if origin else [])
    dirs = []
    for p in places:
        d = os.path.dirname(p)
        if not any(_same(d, x) or d == x for x in dirs):
            dirs.append(d)
    sid = None
    for p in places:
        m = re.match(r"OoTMM-([A-Za-z0-9]+)", os.path.splitext(os.path.basename(p))[0])
        if m:
            sid = m.group(1)
            break
    if sid:
        name = f"OoTMM-Spoiler-{sid}.txt"
        cands = []
        for d in dirs:
            cands += [os.path.join(d, name), os.path.join(d, f"OoTMM-{sid}", name)]
            cands += sorted(glob.glob(os.path.join(glob.escape(d), "*", name)))
        for cand in cands:
            if os.path.isfile(cand):
                if verbose:
                    print(f"[auto] spoiler: {cand}")
                return cand
        if verbose:
            print(f"[auto] no spoiler for seed {sid} near the ROM (not needed: items come from it)")
        return None
    hits = []
    for d in dirs:
        hits += sorted(glob.glob(os.path.join(glob.escape(d), "*Spoiler*.txt")))
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
        if module == "mkchecks":
            # This module may already be loaded when an adopted data tree changes.
            for reader in (mod, importlib.import_module("placement"), importlib.import_module("entrances")):
                reader.DATA = pathlib.Path(paths.data_dir())
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

    import provenance
    with open(rom, "rb") as source:
        rom_bytes = source.read()
    paths.select_rom_data(rom_bytes)
    digest = provenance.rom_digest(rom_bytes)
    data_digest = provenance.data_digest(paths.data_dir())

    checks = paths.user("checks.json")
    # `payload` is what mkchecks reads out of the ROM's code (payload.py):
    # a checks.json without the key was built before that existed, and the
    # overlay would fall back to sweeping for gSharedCustomSave without a word.
    # A table built before the `souls` key existed is stale the same way.
    # `scene_layers` (the scenes' alternate headers) and the Triforce record
    # inside `payload` are the same story, one generator later: without them the
    # panel guesses the loaded setup and the Triforce figure never shows. And
    # `last_scene` (with it, the grotto byte) is what tells one grotto or
    # fairy fountain from the others sharing its scene, and
    # `shared_scene_rooms` is the pass that then puts that scene's roomless
    # rows -- its chests, scrubs and npcs -- in the one they belong to: an
    # older table has them in none, so every grotto's chest would go on being
    # listed in every grotto with nothing said about why.
    if (not os.path.exists(checks) or not _same(_built_from(checks), rom)
            or _built_from(checks, "rom_sha256") != digest
            or _built_from(checks, "generator_revision") != provenance.CHECKS_REVISION
            or _built_from(checks, "data_sha256") != data_digest
            or _built_from(checks, "version") != paths.data_version()
            or not _has_key(checks, "payload")
            or not _has_key(checks, "souls")
            or not _has_key(checks, "scene_layers")
            or not _has_key(checks, "payload", "oot", "triforce")
            or not _has_key(checks, "payload", "oot", "last_scene")
            or not _has_key(checks, "shared_scene_rooms")
            or not _has_key(checks, "mm_merged_scenes")
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
    if (not os.path.exists(icons) or not _same(_built_from(icons), rom)
            or _built_from(icons, "rom_sha256") != digest
            or not os.path.isfile(paths.user("icons.png"))):
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
    # written beside and swapped in, so a crash or a second tracker writing at
    # the same moment leaves the old cache rather than half a file
    try:
        tmp = CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=1)
        os.replace(tmp, CACHE)
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
