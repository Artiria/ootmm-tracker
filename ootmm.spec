# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller recipe for the tracker. Build it with:
#
#     python -m PyInstaller ootmm.spec
#
# and the result is `dist/ootmm-tracker.exe`, one single file with no Python
# needed on the machine that runs it.
#
# Two things this has to get right, both of which fail silently otherwise:
#
#   * The **data** goes in by hand (`datas` below). It is not imported by
#     anything, so nothing would pull it in on its own, and its absence only
#     shows up when a seed is loaded.
#   * The generators are imported **by name** from discover.py, so they have to
#     be listed in `hiddenimports` or the analysis will not see them and the
#     tables will not rebuild.
#
# One file (`onefile`) unpacks into a temporary folder on each run, which costs
# a couple of seconds at startup and is why the generated tables go to
# %LOCALAPPDATA% instead (see paths.py).

a = Analysis(
    ["ootmm.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("data", "data"),                  # pool CSVs, scenes/npc/gi, ref/
        ("overlay.html", "."),             # the page the server serves
        ("Scripts/tracker.lua", "Scripts"),  # Project64-EM, installed on first run
        ("Scripts/tracker-bizhawk.lua", "Scripts"),  # BizHawk, loaded by hand in the Lua Console
        ("icons/README.md", "icons"),      # how to replace an icon by hand
        ("README.md", "."),
        # MIT requires the notice to travel with every copy, and `data/` above
        # is OoTMM's. Both files must stay in the exe, not just in the repo.
        ("LICENSE", "."),
        ("THIRD-PARTY.md", "."),
    ],
    hiddenimports=[
        # reached through importlib, not through an import statement
        "mkchecks",
        "mkicons",
        # imported lazily inside the Tracker, so name it here to travel
        "souls",
        # imported lazily when waiting for BizHawk (shared-memory transport)
        "mmflink",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # nothing here uses them and they are the bulk of the size. Careful
        # with this list: `email` and `xml` look just as unused and are not —
        # http.server pulls in `email`, and it only breaks when the page is
        # first served, well after startup says everything is fine.
        "tkinter",
        "unittest",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ootmm-tracker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX makes antivirus false positives worse
    runtime_tmpdir=None,
    console=True,       # the console IS the interface: it reports what it finds
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
