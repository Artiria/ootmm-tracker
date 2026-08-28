#!/usr/bin/env python3
"""
mkchecks.py - build checks.json out of OoTMM's own data.

Inputs (in data/, exactly as they come from the OoTMM/OoTMM repo):
  pool_oot.csv, pool_mm.csv   location, type, hint, scene, id, item
  scenes.yml                  SCENE_NAME: index
  gi.yml                      the GI table, for item names

And, for the xflags, the seed's ROM: the three lookup tables live inside it
(see resolve_xflags below). Without --rom the xflags come out with no
address, as they used to.

Output: checks.json, with every location resolved to the concrete address and
bit whenever we know where its flag lives, plus the item that sits in it —
read from the ROM's placement table, see placement.py.

State of the mapping
--------------------
Chests are verified against the game's RAM: the CSV `id` field is the bit
number inside the scene's `chest` u32. Checked with Mido's House (scene 0x28,
4 chests -> chest = 0x0F) and with Kokiri Forest (scene 0x55, chest 0 ->
chest = 0x01).

The xflags are verified against the save: of the 12 bits set in oot.xflags in
the .fla, all 12 map to a named check and all of them land in Kokiri Forest /
Saria's house, which is exactly where the run starts.

The rest (gs, cow, stray fairies) has no field assigned yet. They come out in
the JSON with "field": null, so they read as pending instead of vanishing.
"""

import argparse
import csv
import json
import pathlib
import re
import struct
import sys

import paths
import payload
import rom

# `data/` ships with the program; `checks.json` is generated, and inside the
# .exe those two are no longer the same folder. See paths.py.
DATA = pathlib.Path(paths.res("data"))
OUT = pathlib.Path(paths.USER_DIR)

# Verified against RAM on OoTMM v32.0. MM's flags have not been located yet,
# so we only resolve addresses for OoT.
LAYOUT = {
    "oot": {
        "base": 0x8011A5D0,
        "scene_flags": 0xD4,
        "scene_size": 0x1C,
        "scene_count": 124,
        "fields": {
            "chest": 0x00,
            "swch": 0x04,
            "clear": 0x08,
            "collect": 0x0C,
            "unk": 0x10,
            "rooms": 0x14,
            "floors": 0x18,
        },
    },
    # Careful with MM's base: it is the one the signature pins down (newf at
    # base+0x1C, same as OoT), and it turns out to be MmSave+0x08, not MmSave.
    # Every offset in inventory.py is relative to it, so it is kept as is.
    #
    # From there, info = base+0x1C, and the struct closes at info+0xD4, which
    # is where permanentSceneFlags[120] begins: base+0xF0 = 0x8044BF08.
    # DERIVED, not measured: the table is all zeros in both dumps because no
    # MM chest has been opened yet. What *is* measured in game are the five
    # offsets the same struct arithmetic produces (items 0x68, ammo 0x98,
    # upgrades 0xB0, quest 0xB4, strayFairies 0xCC) plus skullCountSwamp at
    # 0xEB8, which falls behind the table; with those six anchored there is no
    # slack left to put `perm` anywhere else.
    #
    # The field order is NOT OoT's: MM has two switch fields, so collectible
    # lands at +0x10 and not at +0x0C.
    "mm": {
        "base": 0x8044BE18,
        "scene_flags": 0xF0,
        "scene_size": 0x1C,
        "scene_count": 120,
        "fields": {
            "chest": 0x00,
            "switch0": 0x04,
            "switch1": 0x08,
            "clear": 0x0C,
            "collect": 0x10,
            "floors": 0x14,
            "rooms": 0x18,
        },
    },
}

# --- xflags -----------------------------------------------------------------
#
# An xflag's bit is not in the CSV: it is computed from three chained tables
# that live in the ROM (packages/generator/src/common/xflags.c):
#
#   setupIndex = sceneTable[sceneId] + setupId
#   roomIndex  = setupTable[setupIndex] + roomId*12 + sliceId
#   bitPos     = roomTable[roomIndex] + actorId          (roomTable is s16)
#
# The tables live in the "extra DMA": a u32 at COMBO_META_ROM gives the
# physical address of a DmaEntry table and the next u32 its entry count. Each
# of the six is **its own entry, uncompressed**, so pstart is the direct offset
# inside the .z64 file.
#
# These VROMs are custom.h's, from v32.0. They are no longer how the tables are
# found —see locate_xflag_tables()— but they stay as the cross-check: on a ROM
# of this version the hunt has to come back with exactly these.
COMBO_META_ROM = 0x03FFF000
XFLAG_TABLES = {
    "oot": (0x080B0F00, 0x080B0FD0, 0x080B10F0),  # scenes, setups, rooms
    "mm": (0x080B41D0, 0x080B42C0, 0x080B43B0),
}
XFLAGS_COUNT = {"oot": 0x2FA, "mm": 0x350}  # xflags_data.h
SLICES = 12  # xsanity.ts

# The CSV `id` of an xflag row is a packed key; the packing comes from
# xsanity.ts, the script that generates those rows:
#   key = (sliceId << 16) | ((setupId & 3) << 14) | (roomId << 8) | actorId
def unpack_xflag_key(key):
    return {
        "slice": key >> 16,
        "setup": (key >> 14) & 3,
        "room": (key >> 8) & 0x3F,
        "actor": key & 0xFF,
    }

# Where each type ends up, taken from Mark_SetOot/Mark_SetMm in
# packages/generator/src/common/mark.c (copy in data/ref/).
#
#   OV_CHEST        perm[scene].chests      |= 1 << id
#   OV_COLLECTIBLE  perm[scene].collectibles|= 1 << id
#   OV_GS           BITMAP32_SET(gsFlags, id - 8)
#   OV_COW          gCowFlags |= 1 << id
#   OV_NPC/SHOP/SCRUB/SR/FISH   BITMAP8_SET(gSharedCustomSave...., id)
#   everything else xflags: BITMAP8_SET(gSharedCustomSave.oot.xflags, bitPos)
#                   with bitPos = three lookup tables living in the ROM
#
# "scene" resolves to an address right away. "custom" and "gs" need
# gSharedCustomSave and gsFlags located. "xflags" needs the tables on top.
TYPE_TARGET = {
    "chest": ("scene", "chest"),
    "collectible": ("scene", "collect"),
    "gs": ("gs_flags", None),
    "cow": ("cow_flags", None),
    "npc": ("custom", "npc"),
    "shop": ("custom", "shops"),
    "scrub": ("custom", "scrubs"),
    "sr": ("custom", "sr"),
    "fish": ("custom", "caughtFishFlags"),
    "sf": ("mm_stray_fairy", None),
}

# gSharedCustomSave, worked out from a single measured fact: buying
# "Kokiri Shop Item 2" (id 1) set bit 1 of byte 0x8044B88A, which is shops[0].
# Subtracting the OotCustomSave layout (save.h) and XFLAGS_COUNT_OOT = 0x2FA
# (xflags_data.h) gives the base.
CUSTOM_BASE = 0x8044B570
CUSTOM_OOT = {
    "xflags": 0x000,   # 0x2FA bytes; the bit comes from the ROM tables
    "npc": 0x2FA,
    "shops": 0x31A,    # verified
    "scrubs": 0x322,
    "sr": 0x32A,
}

# MmCustomSave sits right behind OotCustomSave inside SharedCustomSave.
# OotCustomSave ends at 0x377 (sr ends at 0x33A, padding to 0x33C, two
# OotRespawnData of 0x1C, powderKegTimer s16 at 0x374, bitfield at 0x376) and
# carries ALIGNED(16), so MmCustomSave starts at 0x380.
#
# Confirmed twice over against the .fla: OotCustomSave's trailing bitfield
# shows up at +0x376, and mm.halfDays reads 0x3F exactly at +0x6F4, which is
# where this layout puts it.
# gsFlags: OoT's 100 gold skulltulas (144 rows counting the Master Quest
# ones). Not custom save, just another field of OotSaveInfo:
#
#   OV_GS   BITMAP32_SET(gOotSave.info.gsFlags, id - 8)
#
# BITMAP32_SET(m,b) is m[b >> 5] |= 1 << (b & 0x1f): LSB first within the u32.
# The ids go in blocks of 8 per scene group (block 0 is reserved, hence the -8)
# and run up to 179, i.e. bits 0..171 of the 192 available.
#
# DERIVED, not measured: the area is all zeros because no skulltula has been
# killed yet. But the walk through the struct closes from both ends: forwards,
# perm ends at 0xE64, fw takes 0x28 -> 0xE8C (which is literally the name of
# the next field, unk_e8c), +0x10 -> 0xE9C; backwards, unk_EB4 pins the end of
# gsFlags[6] at 0xEB4 - 0x18 = 0xE9C. Carrying on from there lands exactly on
# eventsMisc = 0xEF8, which does have an ASSERT_OFFSET. No slack left.
OOT_GS_OFF = 0xE9C

# OoTMM's "extra records": twenty-odd u32 of its own, tucked INSIDE OoT's
# per-scene flag table (combo/save.h):
#
#   #define SAVE_EXTRA_RECORD(type, index) (gOotSave + 0xd4 + 0x1c*(index) + 0x10)
#
# 0xD4 + scene*0x1C is the table, and +0x10 is the `unk` field of each scene --
# the one OoT vanilla does not use. So record N squats in scene N's `unk`, and
# that is the answer to the loose end the POC had open for days: items setting
# bits in `unk` of two scenes at once were writing two of these records (Cojiro
# -> gOotExtraTrade at index 0 and gOotExtraTradeSave at index 10, which is
# exactly the pair that was measured).
#
# gCowFlags is index 9, so both games' cows land on OoT's save at +0x1E0.
EXTRA_RECORD_OFF = 0xD4 + 0x10

CUSTOM_MM_OFF = 0x380
CUSTOM_MM = {
    "xflags": CUSTOM_MM_OFF + 0x000,   # 0x350 bytes
    "npc": CUSTOM_MM_OFF + 0x350,      # 32 bytes
    "shops": CUSTOM_MM_OFF + 0x370,    # 4 bytes
    "halfDays": CUSTOM_MM_OFF + 0x374,  # verified (0x3F)
}

# gSharedCustomSave is also stored verbatim in flash, so the same offsets work
# on a de-word-swapped .fla without touching the emulator at all:
#   Flash_ReadWrite(0x18000 + 0x4000 * fileIndex, &gSharedCustomSave, ...)
FLASH_CUSTOM_SAVE = 0x18000
FLASH_FILE_STRIDE = 0x4000


# Which anchor each target hangs off. "game" means the save context of that
# row's game. All three anchors are relocated at startup: the save ones by
# signature, and the custom save one by its fixed offset from MM's buffer (both
# are globals of the same build, so their distance is a per-version constant
# even when the addresses move).
ANCHOR_OF = {
    "scene": "game",
    "mm_stray_fairy": "game",
    "gs_flags": "oot",
    "cow_flags": "oot",
    "custom": "custom",
    "xflags": "custom",
}
ANCHOR_BASE = {
    "oot": LAYOUT["oot"]["base"],
    "mm": LAYOUT["mm"]["base"],
    "custom": CUSTOM_BASE,
}


# --------------------------------------------------------------------------
# Finding the tables without an address
# --------------------------------------------------------------------------
#
# The VROMs above are a version constant, and it is the one that has already
# broken once. It does not have to be: the three tables are a chain, and a
# chain can be recognised by its shape.
#
#   scenes[]  u16, non-decreasing, starts at 0, indexes setups[]
#   setups[]  u16, non-decreasing, starts at 0, indexes rooms[]
#   rooms[]   s16, the bit itself; no order to it
#
# So a candidate is three consecutive uncompressed entries where the first two
# have that shape and **each one indexes inside the next**: max(scenes) <
# len(setups) and max(setups) < len(rooms). Measured over the 29 ROMs in
# Downloads, spanning two version families: exactly two chains in each, no
# false positives, and on the current ones the answer is the constants above.
#
# Which chain is which game comes from scenes.yml: OoT's scene ids reach 100
# and MM's 113, so a table has to be long enough to hold them. The generator
# emits OoT's first and that has held in all 29, but the fit is checked rather
# than assumed, and if neither assignment fits, the hunt is declared failed.
_SCENES_MIN, _SCENES_MAX = 32, 512
_SETUPS_MAX = 1024


def _index_table(data, maxlen):
    """The u16 of a non-decreasing table that starts at 0, or None."""
    if len(data) % 2 or not (_SCENES_MIN <= len(data) <= maxlen):
        return None
    v = struct.unpack_from(f">{len(data) // 2}H", data, 0)
    if v[0] != 0 or any(v[i] > v[i + 1] for i in range(len(v) - 1)):
        return None
    return v


def find_xflag_chains(rom_bytes):
    """[(vroms, scene count)] for every chain in the ROM, in ROM order."""
    ents = rom.extra_entries(rom_bytes)
    out = []
    for i in range(len(ents) - 2):
        (va, _, da), (vb, _, db), (vc, _, dc) = ents[i], ents[i + 1], ents[i + 2]
        escenas = _index_table(da, _SCENES_MAX)
        setups = _index_table(db, _SETUPS_MAX)
        if not escenas or not setups or len(dc) % 2:
            continue
        if max(escenas) >= len(setups) or max(setups) >= len(dc) // 2:
            continue                      # the chain does not close
        out.append(((va, vb, vc), len(escenas)))
    return out


def locate_xflag_tables(rom_bytes, scenes, verbose=True):
    """{game: (scenes, setups, rooms)} found by shape, or None.

    Never silently replaces the constants: when what it finds is not what
    custom.h says, it prints both, because that means this seed is from
    another OoTMM version and everything else built on constants is suspect.
    """
    necesita = {}
    for game in ("oot", "mm"):
        ids = [v for k, v in scenes.items() if k.startswith(game.upper() + "_")]
        necesita[game] = max(ids) + 1 if ids else 0

    cadenas = find_xflag_chains(rom_bytes)
    if len(cadenas) != 2:
        if verbose:
            print(f"xflags: {len(cadenas)} table chains in the ROM, expected 2;"
                  " falling back to the v32.0 addresses")
        return None

    for orden in ((0, 1), (1, 0)):
        cabe = all(cadenas[orden[i]][1] >= necesita[g]
                   for i, g in enumerate(("oot", "mm")))
        if cabe:
            found = {"oot": cadenas[orden[0]][0], "mm": cadenas[orden[1]][0]}
            break
    else:
        if verbose:
            print("xflags: the chains found do not fit the scene counts"
                  f" ({[c[1] for c in cadenas]} vs {list(necesita.values())});"
                  " falling back to the v32.0 addresses")
        return None

    if verbose:
        iguales = all(found[g] == XFLAG_TABLES[g] for g in found)
        if iguales:
            print("xflags: tables located by shape, and they are where"
                  " custom.h v32.0 says")
        else:
            print("xflags: tables located by shape, and they are NOT where"
                  " custom.h v32.0 says:")
            for g in ("oot", "mm"):
                print(f"   {g}: {found[g][0]:#010x} (v32.0: {XFLAG_TABLES[g][0]:#010x})")
            print("   this seed is from another OoTMM version; the pool CSVs are")
            print("   still v32.0's, so watch for collisions below")
    return found


# --------------------------------------------------------------------------
# gSharedCustomSave and the MM buffer, from the payload's code
# --------------------------------------------------------------------------
#
# CUSTOM_BASE, LAYOUT["mm"]["base"], CUSTOM_OOT, CUSTOM_MM_OFF, CUSTOM_MM and
# XFLAGS_COUNT above are all v32.0's, and every one of them is a global of the
# payload or a field offset inside one -- which is to say, they move with the
# build. Measured over the 42-seed corpus: three generations, three different
# gSharedCustomSave addresses AND three different layouts inside it (the xflags
# array is 0x25D, 0x2E8 or 0x2FA bytes long, so npc/shops/scrubs/sr and the
# whole MM half sit at different offsets). On an older seed the constants would
# put every npc/shop/scrub/silver-rupee check on the wrong byte without a word.
#
# payload.locate() reads all of them out of the code that uses them (see
# payload.py). What it returns replaces the constants; the constants stay as
# the cross-check and get printed when they disagree, the way
# locate_xflag_tables() treats custom.h's VROMs.

def apply_payload_layout(rom_bytes, verbose=True):
    """Override the custom-save constants with what the ROM's code says.

    Returns the located block for checks.json (so the overlay can use the
    same addresses at run time), or None when the ROM did not give a complete
    answer -- in which case nothing is touched and it says so.
    """
    global CUSTOM_BASE, CUSTOM_OOT, CUSTOM_MM_OFF, CUSTOM_MM, XFLAGS_COUNT
    try:
        pl = payload.locate(rom_bytes)
    except Exception as ex:  # a scan bug must not take mkchecks down
        if verbose:
            print(f"payload: could not scan the payload ({type(ex).__name__}: {ex});"
                  " keeping the v32.0 constants")
        return None
    oot = pl.get("oot", {})
    lay = pl.get("layout", {})
    if "custom" not in oot or "foreign_base" not in oot or not payload.layout_complete(lay):
        if verbose:
            print("payload: gSharedCustomSave not pinned from the ROM's code;"
                  " keeping the v32.0 constants")
            if lay.get("oot", {}).get("_ambiguous"):
                print(f"   the OoT half fits at more than one offset: "
                      f"{[hex(x) for x in lay['oot']['_ambiguous']]}")
        return None

    old = {
        "gSharedCustomSave (running OoT)": (CUSTOM_BASE, oot["custom"][0]),
        "MM buffer (running OoT)": (LAYOUT["mm"]["base"], oot["foreign_base"]),
        "oot.npc": (CUSTOM_OOT["npc"], lay["oot"]["npc"]),
        "oot.shops": (CUSTOM_OOT["shops"], lay["oot"]["shops"]),
        "oot.scrubs": (CUSTOM_OOT["scrubs"], lay["oot"]["scrubs"]),
        "oot.sr": (CUSTOM_OOT["sr"], lay["oot"]["sr"]),
        "mm (MmCustomSave)": (CUSTOM_MM_OFF, lay["mm"]["base"]),
        "mm.npc": (CUSTOM_MM["npc"], lay["mm"]["npc"]),
        "mm.shops": (CUSTOM_MM["shops"], lay["mm"]["shops"]),
        "mm.halfDays": (CUSTOM_MM["halfDays"], lay["mm"]["halfDays"]),
        "XFLAGS_COUNT_OOT": (XFLAGS_COUNT["oot"], lay["oot"]["xflags_count"]),
        "XFLAGS_COUNT_MM": (XFLAGS_COUNT["mm"], lay["mm"]["xflags_count"]),
    }
    CUSTOM_BASE = oot["custom"][0]
    LAYOUT["mm"]["base"] = oot["foreign_base"]
    ANCHOR_BASE["mm"] = LAYOUT["mm"]["base"]
    ANCHOR_BASE["custom"] = CUSTOM_BASE
    CUSTOM_OOT = {"xflags": 0, "npc": lay["oot"]["npc"], "shops": lay["oot"]["shops"],
                  "scrubs": lay["oot"]["scrubs"], "sr": lay["oot"]["sr"]}
    # caughtFishFlags is a field of the shared struct, not of the OoT half,
    # but the fish checks are OoT's and TYPE_TARGET files them under `custom`
    # with the OoT map: it goes in here, when the ROM gives it.
    if lay["oot"].get("caughtFishFlags") is not None:
        CUSTOM_OOT["caughtFishFlags"] = lay["oot"]["caughtFishFlags"]
    CUSTOM_MM_OFF = lay["mm"]["base"]
    CUSTOM_MM = {"xflags": lay["mm"]["xflags"], "npc": lay["mm"]["npc"],
                 "shops": lay["mm"]["shops"], "halfDays": lay["mm"]["halfDays"]}
    XFLAGS_COUNT = {"oot": lay["oot"]["xflags_count"], "mm": lay["mm"]["xflags_count"]}

    moved = {k: v for k, v in old.items() if v[0] != v[1]}
    if verbose:
        print(f"payload: gSharedCustomSave {CUSTOM_BASE:#x} ({oot['custom'][1]:#x} bytes),"
              f" MM buffer {LAYOUT['mm']['base']:#x}, xflags {XFLAGS_COUNT['oot']:#x}/{XFLAGS_COUNT['mm']:#x}"
              f" bytes, MmCustomSave at +{CUSTOM_MM_OFF:#x} -- read from the ROM's code")
        if "caughtFishFlags" in CUSTOM_OOT:
            print(f"payload: caughtFishFlags at +{CUSTOM_OOT['caughtFishFlags']:#x} (the pond fish)")
        else:
            print("payload: caughtFishFlags not found by shape; the 33 pond-fish checks stay unresolved")
        if moved:
            print("   NOTE: these differ from the v32.0 constants; this seed is from")
            print("   another OoTMM version and the ROM's values are the ones used:")
            for k, (a, b) in moved.items():
                print(f"      {k}: {b:#x} (v32.0: {a:#x})")
        if len(lay["oot"].get("_candidates", [])) > 1 or len(lay["mm"].get("_candidates", [])) > 1:
            print("   (the layout had more than one fit; the first, structurally"
                  " the right one, was taken -- see payload.layout)")
    return pl


class XflagTables:
    """A game's three lookup tables, read from the seed's ROM."""

    def __init__(self, rom_bytes, game, vroms=None):
        # rom.py handles Yaz0 and several tables sharing one entry
        self.scenes, self.setups, self.rooms = (
            rom.read_extra_vrom(rom_bytes, v) for v in (vroms or XFLAG_TABLES[game])
        )
        self.limit = XFLAGS_COUNT[game] * 8

    def bitpos(self, scene_id, key):
        x = unpack_xflag_key(key)
        setup_index = struct.unpack_from(">H", self.scenes, scene_id * 2)[0] + x["setup"]
        room_index = (
            struct.unpack_from(">H", self.setups, setup_index * 2)[0]
            + x["room"] * SLICES
            + x["slice"]
        )
        bit = struct.unpack_from(">h", self.rooms, room_index * 2)[0] + x["actor"]
        if not 0 <= bit < self.limit:
            raise ValueError(f"bitPos {bit} outside [0, {self.limit})")
        return bit

# Only these resolve to a concrete address today.
TYPE_FIELD = {"chest": "chest", "collectible": "collect"}


def load_npcs():
    """npc.yml: NAME -> index, game-prefixed like scenes.yml."""
    out = {}
    f = DATA / "npc.yml"
    if not f.exists():
        return out
    for line in f.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z0-9_]+):\s*(0x[0-9a-fA-F]+|\d+)\s*$", line.strip())
        if m:
            out[m.group(1)] = int(m.group(2), 0)
    return out


def load_scenes():
    scenes = {}
    for line in (DATA / "scenes.yml").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z0-9_]+):\s*(0x[0-9a-fA-F]+|\d+)\s*$", line.strip())
        if m:
            scenes[m.group(1)] = int(m.group(2), 0)
    return scenes


# Rows of OoTMM's pool CSVs filed under a scene the check is not in. The scene
# only decides which panel lists the check and which region counts it; the
# flag that marks it is untouched, and no npc row keys on its scene
# (placement.CON_ESCENA), so the override table matches exactly as before.
SCENE_FIXES = {
    # pool_mm.csv puts it in FAIRY_FOUNTAIN; the statue stands at Snowhead,
    # before the temple, the way Woodfall's stands in WOODFALL. His report,
    # 28 Aug 2026: "Snowhead Owl Statue" listed inside Clock Town's fountain.
    ("mm", "Snowhead Owl Statue"): "SNOWHEAD",
}


def load_pool(path):
    # every reader of the CSV goes through here, so the fixes apply to the
    # placement index, the scene recovery and the rows alike
    game = "mm" if pathlib.Path(path).name.startswith("pool_mm") else "oot"
    with open(path, newline="", encoding="utf-8") as fh:
        r = csv.reader(fh)
        header = [h.strip() for h in next(r)]
        for row in r:
            if row and any(c.strip() for c in row):
                d = dict(zip(header, [c.strip() for c in row]))
                fix = SCENE_FIXES.get((game, d.get("location")))
                if fix:
                    d["scene"] = fix
                yield d


def load_mq(spoiler):
    """The seed's Master Quest dungeons, as the spoiler names them.

    Only to check against: which scenes are Master Quest comes out of the ROM
    now (placement.master_quest_scenes), and the tracker no longer needs a
    spoiler to know.

    OoTMM writes them as a list under the header, one `- Name` per line, and
    only "none" goes on the header line itself. Reading just the header, which
    is what this did, made **every** MQ seed come back as "none" -- so the
    warning about not being able to map them never even fired, and the seed
    quietly kept every vanilla twin.
    """
    if spoiler is None:
        return None
    lineas = pathlib.Path(spoiler).read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lineas):
        if not line.strip().startswith("Master Quest Dungeons:"):
            continue
        val = line.split(":", 1)[1].strip()
        if val and val != "none":
            return {d.strip() for d in val.split(",")}     # older one-line form
        fuera = set()
        for seguida in lineas[i + 1:]:
            m = re.match(r"^\s+-\s+(\S.*?)\s*$", seguida)
            if not m:
                break
            fuera.add(m.group(1))
        return fuera
    return None


# --------------------------------------------------------------------------
# The pool CSVs, split into what the ROM knows and what only they know
# --------------------------------------------------------------------------
#
# The CSVs are the last thing here that is pinned to a version: they are the
# list of which checks exist, and they are v32.0's. The ROM knows that list
# too —it placed the items— and it knows where each bit lives; what it does
# not carry are the randomizer's own labels: the location name, the hint
# region and the vanilla item.
#
# So these two functions cut the row in half. row_parts() is the half the ROM
# also has, and it is what forms the override key; name_index() keeps the
# other half filed under that same key, which is what turns the CSV from a
# census into a dictionary.


def row_parts(game, row, scenes, npcs, unresolved=None):
    """(scene_id, csv_id, xflag) of a pool row: the numbers under its labels."""
    clave = f"{game.upper()}_{row['scene']}"
    scene_id = scenes.get(clave)
    if scene_id is None and unresolved is not None:
        unresolved.add(clave)

    try:
        csv_id = int(row["id"], 0)
    except ValueError:
        # npcs carry the id as a symbol (WEIRD_EGG); npc.yml resolves it
        csv_id = npcs.get(f"{game.upper()}_{row['id']}")

    target = TYPE_TARGET.get(row["type"], ("xflags", None))[0]
    xflag = None
    if target == "xflags" and scene_id is not None and csv_id is not None:
        # the CSV `id` of an xflag row was never a bit number, it is the key
        xflag = unpack_xflag_key(csv_id)
    return scene_id, csv_id, xflag


def name_index(scenes, npcs):
    """{(game, ovkey): [row, ...]} keyed the way the ROM's table is keyed.

    The value is a **list**, not a row, because two rows can share a key: a
    vanilla dungeon and its Master Quest twin are the same scene and the same
    chest id, so they pack identically. Which of the two a seed actually has is
    not something the CSV or the placement table can say (see load_mq), so
    nothing is chosen here. Non-MQ rows come first.

    Rows whose key cannot be formed —an unknown scene, an npc symbol npc.yml
    does not have— are skipped: without a key they cannot be matched to
    anything, and they are counted by the caller rather than hidden.
    """
    import placement

    index = {}
    saltadas = 0
    for game, pool in (("oot", "pool_oot.csv"), ("mm", "pool_mm.csv")):
        for row in load_pool(DATA / pool):
            scene_id, csv_id, xflag = row_parts(game, row, scenes, npcs)
            key = placement.override_key(row["type"], scene_id, csv_id, xflag)
            if key is None:
                saltadas += 1
                continue
            index.setdefault((game, key), []).append({
                "name": row["location"],
                "hint": row["hint"],
                "vanilla": row["item"],
                "type": row["type"],
                "scene": row["scene"],
                "scene_id": scene_id,
                "csv_id": csv_id,
                "xflag": xflag,
                "mq": row["location"].startswith("MQ "),
            })
    for filas in index.values():
        filas.sort(key=lambda r: r["mq"])
    return index, saltadas


def index_coverage(rom_bytes, index):
    """(both, only in the ROM, only in the CSV), each a list of (game, ovkey).

    "Only in the ROM" is the one that has to stay empty: it means this seed has
    a location no CSV row names, which is what a nameless check would come
    from. "Only in the CSV" is expected and fine —Master Quest rows above all—
    and it is the set step 3 has to keep.
    """
    import placement

    de_rom = set(placement.read_tables(rom_bytes))
    de_csv = set(index)
    return (sorted(de_rom & de_csv), sorted(de_rom - de_csv), sorted(de_csv - de_rom))


# --------------------------------------------------------------------------
# Building a check out of the ROM's key
# --------------------------------------------------------------------------
#
# The key of COMBO_VROM_CHECKS carries everything that decides where a flag
# lives: the override type, the scene, the room, the setup and the actor. So
# the check gets built from it, and the CSV is consulted only for the labels.
#
# What the ROM does **not** carry is the fine type (`pot`, `grass`, `tree`).
# Measured 14 ago: the slice is not it — a slice holds 18 different kinds of
# actor, because the slice is *which drop of the actor* this is, not what the
# actor is. That type never reaches an address, though: every xflag goes to
# the same target, and the panel only prints it. So it stays a label.
#
# The scene is the other half-exception. Only chest, collectible, sf and the
# xflags carry it in the key; npc, gs, cow, shop, scrub, sr and fish live in a
# global id space and their scene byte is 0. Those seven do not need the scene
# to be addressed (see TYPE_TARGET: they are all bitmaps indexed by the global
# id), so for them the scene stays a label too, out of scenes.yml.


def scene_names(scenes):
    """{(game, scene id): SCENE_NAME} out of scenes.yml, prefix stripped.

    The pool CSVs write the scene without the game prefix and the overlay
    groups its region panel by that exact string, so a check the CSVs do not
    name still needs one or it lands in a region called None.

    Measured 14 ago 2026: the 210 entries give 210 distinct (game, id) pairs,
    so this inverts with no ambiguity.
    """
    out = {}
    for k, v in scenes.items():
        juego, _, resto = k.partition("_")
        out[(juego.lower(), v)] = resto
    return out


def synthetic_name(game, scene, scene_id, tipo_ov, csv_id, xflag):
    """A readable name for a key no pool row names.

    The separator is " · ", which no row uses, so a made-up name can never
    collide with a real one — and the overlay files everything by name.

    It also has to be unique among themselves, so it carries every part of the
    key that varies: for an xflag the room, the actor and, when they are not
    zero, the drop and the setup.

    There is no honest way to say `pot` here: the ROM's ov only gives the
    slice, and the slice is which drop of the actor this is, not what the actor
    is (see check_from_key). So it says what it knows, room and actor, instead
    of guessing a type.
    """
    juego = "OoT" if game == "oot" else "MM"
    if scene and scene != "UNKNOWN":
        # "UNKNOWN" is only there so the region sort has a string to work with;
        # in a name it would just be noise.
        donde = scene.replace("_", " ").title()
    elif scene_id is not None:
        donde = f"scene {scene_id:#04x}"
    else:
        donde = None                       # the seven global-id types

    partes = [juego] + ([donde] if donde else [])
    if xflag is not None:
        detalle = f"room {xflag['room']} · actor {xflag['actor']}"
        if xflag["slice"]:
            detalle += f" · drop {xflag['slice']}"
        if xflag["setup"]:
            detalle += f" · setup {xflag['setup']}"
        partes.append(detalle)
    else:
        partes.append(f"{tipo_ov or 'check'} {csv_id}")
    return " · ".join(partes)


# {ov: type name}, the inverse of placement.OV. Filled on first use because
# placement is imported lazily throughout this file.
_OV_TYPE = {}


def _ov_types():
    import placement

    if not _OV_TYPE:
        _OV_TYPE.update({v: k for k, v in placement.OV.items()})
    return _OV_TYPE


# --------------------------------------------------------------------------
# Recovering a scene id scenes.yml does not have
# --------------------------------------------------------------------------
#
# scenes.yml was the last file left holding a version constant, and it failed
# in the worst way available: a row whose scene it does not know forms no
# override key, so it cannot be matched against the ROM at all. Emitting it
# anyway duplicates every check of that scene —once nameless from the ROM, once
# addressless from the CSV— and dropping it makes checks vanish.
#
# But the id is recoverable, by the same trick used for the xflag tables and
# kItemNames: **by content**. An override key is (ov << 24) | (scene << 16) |
# rest, and the CSV knows every part but the scene. So take a scene name's rows,
# strip the scene byte off their keys, and look for the scene id under which the
# ROM lists exactly that set. Nothing else in the ROM looks like it.
_RECOVER_MIN = 0.90    # of the name's rows that the candidate must account for
_RECOVER_GAP = 1.50    # how far ahead of the runner-up it has to be


def _subkey(game, row, npcs):
    """A row's override key with the scene byte blanked, or None."""
    import placement

    try:
        csv_id = int(row["id"], 0)
    except ValueError:
        csv_id = npcs.get(f"{game.upper()}_{row['id']}")
    if csv_id is None:
        return None
    target = TYPE_TARGET.get(row["type"], ("xflags", None))[0]
    xflag = unpack_xflag_key(csv_id) if target == "xflags" else None
    if xflag is None and row["type"] not in placement.CON_ESCENA:
        return None                       # its key carries no scene anyway
    key = placement.override_key(row["type"], 0, csv_id, xflag)
    return None if key is None else key & 0xFF00FFFF


def recover_scene_ids(claves_rom, scenes, npcs, verbose=True):
    """{OOT_GROTTOS: id} for scene names scenes.yml has no index for."""
    import placement

    if not claves_rom:
        return {}

    # what the ROM lists, by scene id, keeping only the keys that carry a scene
    por_escena = {}
    for game, key in claves_rom:
        ov = key >> 24
        tipo = _ov_types().get(ov)
        if ov < placement.OV_XFLAG0 and tipo not in placement.CON_ESCENA:
            continue
        por_escena.setdefault((game, (key >> 16) & 0xFF), set()).add(key & 0xFF00FFFF)

    faltan = {}
    for game, pool in (("oot", "pool_oot.csv"), ("mm", "pool_mm.csv")):
        for row in load_pool(DATA / pool):
            nombre = f"{game.upper()}_{row['scene']}"
            if nombre in scenes:
                continue
            sk = _subkey(game, row, npcs)
            if sk is not None:
                faltan.setdefault((game, nombre), set()).add(sk)
    if not faltan:
        return {}

    ocupados = {(g, v) for k, v in scenes.items()
                for g in (k.partition("_")[0].lower(),)}
    out = {}
    for (game, nombre), subs in sorted(faltan.items()):
        marcas = []
        for (g, sid), tiene in por_escena.items():
            if g != game or (g, sid) in ocupados:
                continue
            marcas.append((len(subs & tiene), sid))
        marcas.sort(reverse=True)
        mejor = marcas[0] if marcas else (0, None)
        segundo = marcas[1][0] if len(marcas) > 1 else 0
        cubre = mejor[0] / len(subs)
        if cubre >= _RECOVER_MIN and mejor[0] >= max(1, segundo * _RECOVER_GAP):
            out[nombre] = mejor[1]
            if verbose:
                print(f"scenes: {nombre} is not in scenes.yml; the ROM says it is"
                      f" {mejor[1]:#04x} ({mejor[0]}/{len(subs)} of its rows,"
                      f" runner-up {segundo})")
        elif verbose:
            print(f"scenes: {nombre} is not in scenes.yml and the ROM does not"
                  f" pin it down ({mejor[0]}/{len(subs)}, runner-up {segundo});"
                  " its rows come out with no address")
    return out


def check_without_key(game, etiquetas, scene_id, csv_id):
    """A pool row that forms no override key: kept, and with no address.

    It happens when scenes.yml does not know the row's scene, or npc.yml does
    not know its symbol. With no key there is no way to ask the ROM anything
    about it, so there is no address to be had — and in every one of those
    cases there never was one anyway: the types that carry the scene need it to
    be addressed, and the ones that do not need an id this row has not got.

    What matters is that it still comes out. Skipping it would make a check
    **disappear**, which is worse than the pending row it replaced, and it is
    exactly the kind of silence this project keeps getting bitten by.
    """
    tipo = etiquetas.get("type")
    target, sub = TYPE_TARGET.get(tipo, ("xflags", None))
    return {
        "name": etiquetas.get("name"),
        "target": target,
        "target_field": sub,
        "kind": None,
        "game": game,
        "type": tipo,
        "scene": etiquetas.get("scene"),
        "scene_id": scene_id,
        "field": TYPE_FIELD.get(tipo),
        "bit": csv_id,
        "csv_id": csv_id,
        "addr": None,
        "vanilla": etiquetas.get("vanilla"),
        "mq": etiquetas.get("mq", False),
        # so nobody has to work out why this one has no address
        "no_key": True,
    }


def check_from_key(game, key, etiquetas=None, tables=None, xflag_errors=None,
                   escenas=None):
    """One check, with the ROM's key as the source of everything but the labels.

    `etiquetas` is the row name_index() filed under this key, or None for a key
    the CSVs do not name. Nothing here needs it to work out an address.

    `escenas` is scene_names(), used only to give an unnamed key a scene and a
    readable name.
    """
    import placement

    lay = LAYOUT[game]
    et = etiquetas or {}
    ov = key >> 24

    if ov >= placement.OV_XFLAG0:
        room_byte = (key >> 8) & 0xFF
        xflag = {
            "slice": ov - placement.OV_XFLAG0,
            "setup": (room_byte >> 6) & 3,
            "room": room_byte & 0x3F,
            "actor": key & 0xFF,
        }
        # the packed form the CSV used to carry, rebuilt rather than copied
        csv_id = ((xflag["slice"] << 16) | ((xflag["setup"] & 3) << 14)
                  | (xflag["room"] << 8) | xflag["actor"])
        scene_id = (key >> 16) & 0xFF
        tipo_ov = None
        target, sub, field = "xflags", None, None
    else:
        xflag = None
        csv_id = key & 0xFF
        tipo_ov = _ov_types().get(ov)
        # the seven global-id types put 0 in that byte; theirs is a label
        scene_id = ((key >> 16) & 0xFF if tipo_ov in placement.CON_ESCENA
                    else et.get("scene_id"))
        target, sub = TYPE_TARGET.get(tipo_ov, ("xflags", None))
        field = TYPE_FIELD.get(tipo_ov)

    bit = csv_id
    addr = None
    # MM stray fairies live in the scene's own flag table, split by id the way
    # setStrayFairyMarkMm() in mark.c does it: 0x30 and up are collectible
    # bits, 0x20..0x2F switch1, below that switch0 -- always bit `id & 0x1f`.
    if target == "mm_stray_fairy" and bit is not None and game == "mm":
        field = "collect" if bit >= 0x30 else "switch1" if bit >= 0x20 else "switch0"
        bit = bit & 0x1F
    if (
        field
        and scene_id is not None
        and bit is not None
        and 0 <= bit < 32
        and lay["scene_flags"] is not None
        and scene_id < lay["scene_count"]
    ):
        addr = (lay["base"] + lay["scene_flags"]
                + scene_id * lay["scene_size"] + lay["fields"][field])

    kind = "u32be" if addr is not None else None
    custom = CUSTOM_OOT if game == "oot" else CUSTOM_MM
    if target == "custom" and sub in custom and bit is not None:
        # BITMAP8_SET: bit i lives in byte i/8, at position i%8
        addr = CUSTOM_BASE + custom[sub] + bit // 8
        kind = "u8"
        bit = bit % 8
    elif target == "gs_flags" and game == "oot" and bit is not None and bit >= 8:
        i = bit - 8
        addr = LAYOUT["oot"]["base"] + OOT_GS_OFF + (i >> 5) * 4
        kind = "u32be"
        bit = i & 0x1F
    elif target == "cow_flags" and bit is not None and bit < 32:
        # Both games' cows share gCowFlags, and it lives in OoT's save
        # whichever one is running -- see SAVE_EXTRA_RECORD above.
        addr = LAYOUT["oot"]["base"] + EXTRA_RECORD_OFF + 9 * 0x1C
        kind = "u32be"

    # A key with no row still needs a name and a scene: the overlay files
    # everything by name and groups its regions by scene. Both are made up
    # here, and only ever for a key the CSVs do not have — nought on every
    # seed measured.
    nombre_escena = et.get("scene")
    if nombre_escena is None and escenas is not None and scene_id is not None:
        nombre_escena = escenas.get((game, scene_id))
    if nombre_escena is None and not et:
        # It has to be a string even when there is nothing to put in it: the
        # overlay sorts its region bars by this field, and one None among the
        # strings raises TypeError. The seven global-id types really do have no
        # scene in their key, so for them this is the honest answer.
        nombre_escena = (f"SCENE_{scene_id:02X}" if scene_id is not None
                         else "UNKNOWN")
    nombre = et.get("name") or synthetic_name(
        game, nombre_escena, scene_id, tipo_ov, csv_id, xflag)

    entry = {
        "name": nombre,
        "target": target,
        "target_field": sub,
        "kind": kind,
        "game": game,
        # the ROM has no fine type for an xflag; the slice is not it
        "type": et.get("type") or tipo_ov or "xflag",
        "scene": nombre_escena,
        "scene_id": scene_id,
        "field": field,
        "bit": bit,
        # The id **exactly as the CSV wrote it**, rebuilt from the key. `bit`
        # gets rewritten above —for bitmaps it becomes the bit within its byte,
        # for xflags the bitpos— and the placement lookup needs this one.
        "csv_id": csv_id,
        "addr": addr,
        "vanilla": et.get("vanilla"),
        # vanilla and MQ share a flag; in a given seed only one exists
        "mq": et.get("mq", False),
    }

    if xflag is not None:
        entry["xflag"] = xflag
        if tables and game in tables:
            try:
                bitpos = tables[game].bitpos(scene_id, csv_id)
            except (ValueError, IndexError, struct.error) as ex:
                if xflag_errors is not None:
                    xflag_errors.append((entry["name"], str(ex)))
            else:
                base = CUSTOM_BASE + custom["xflags"]
                entry["bitpos"] = bitpos
                entry["addr"] = base + bitpos // 8
                entry["bit"] = bitpos % 8
                entry["kind"] = "u8"
                entry["flash_off"] = (
                    FLASH_CUSTOM_SAVE
                    + (CUSTOM_MM_OFF if game == "mm" else 0)
                    + bitpos // 8
                )

    # Absolute addresses only hold while the bases stay put, and they do not:
    # crossing between OoT and MM reorganises RAM completely (which is why
    # locate_saves exists). We also store the anchor and the offset, which are
    # stable, so the overlay can relocate everything on every startup.
    if entry["addr"] is not None:
        anchor = ANCHOR_OF.get(entry["target"])
        if anchor == "game":
            anchor = game
        if anchor:
            entry["anchor"] = anchor
            entry["off"] = entry["addr"] - ANCHOR_BASE[anchor]
    return entry


# --------------------------------------------------------------------------
# Who gets the bit when two checks want the same one
# --------------------------------------------------------------------------
#
# With the ROM as the census, a row the ROM does not list is not automatically
# wrong —Master Quest rows, and a handful the generator filtered out— but it
# has no authority either. On a seed from another version there are hundreds of
# them, and measured on Siixg4Kf they are exactly what produced the 30
# collisions: 45 of the 60 checks involved were rows only the CSV knew.
#
# So the rule, which is just "the ROM decides" carried down to the bit:
#
#   * A check the ROM lists keeps its bit, always.
#   * A row only the CSV knows keeps its bit only if it is free.
#   * If two rows only the CSV knows want the same bit, **neither** takes it:
#     nothing here says which of the two is the real one, and ticking the wrong
#     name is worse than showing both as pending.
#
# Sharing between a vanilla check and its Master Quest twin stays legitimate,
# same as in collisions(): in a given seed only one of the two exists.
#
# What a check loses is its address, not its existence: it goes back to the
# shape an unresolved xflag has, plus `bit_taken_by` saying who took it. This
# never happens quietly.


def _drop_address(c, ganador):
    """Put a check back to unresolved, and write down who took its bit."""
    for k in ("bitpos", "flash_off", "anchor", "off"):
        c.pop(k, None)
    c["addr"] = None
    c["kind"] = None
    c["bit"] = c["csv_id"]
    c["bit_taken_by"] = ganador


def apply_bit_priority(checks, solo_csv, verbose=True):
    """Strip the address off CSV-only checks whose bit belongs to someone else.

    `solo_csv` is a set of id() of the checks the ROM does not list. Returns
    the list of (check, winner) that lost their address.
    """
    fuera = []
    for game in XFLAG_TABLES:
        cs = [c for c in checks if c["game"] == game and "bitpos" in c]
        # the ROM's first, so they claim before anyone else can
        cs.sort(key=lambda c: id(c) in solo_csv)
        dueno = {}
        for c in cs:
            previos = dueno.setdefault(c["bitpos"], [])
            # sharing is only legitimate between a vanilla and its MQ twin
            choca = [p for p in previos if p["mq"] == c["mq"]]
            if choca and id(c) in solo_csv:
                fuera.append((c, choca[0]))
                continue
            previos.append(c)

    # A holder that is itself CSV-only never had a claim either, so it does not
    # get to keep a bit it only won by being looked at first.
    perdedores = {id(c) for c, _ in fuera}
    for c, contra in list(fuera):
        if id(contra) in solo_csv and id(contra) not in perdedores:
            fuera.append((contra, c))
            perdedores.add(id(contra))

    for c, ganador in fuera:
        _drop_address(c, ganador["name"])

    if verbose:
        print(f"bit priority: {len(fuera)} CSV-only checks lost a bit the ROM"
              " had already placed")
        for c, ganador in fuera[:3]:
            print(f"   {c['name']}  ->  bit taken by {ganador['name']}")
    return fuera


def collisions(checks):
    """[(name, name)] of checks sharing a bit without being a vanilla/MQ pair.

    Vanilla and MQ do share a bit, by design. Anything else means the row was
    unpacked against the wrong table and two checks now tick each other.
    """
    out = []
    for game in XFLAG_TABLES:
        seen = {}
        for c in checks:
            if c["game"] != game or "bitpos" not in c:
                continue
            prev = seen.setdefault(c["bitpos"], c)
            if prev is not c and prev["mq"] == c["mq"]:
                out.append((prev["name"], c["name"]))
    return out


def _payload_json(pl):
    """payload.locate()'s answer, JSON-shaped: only what the overlay uses."""
    if not pl:
        return None
    out = {}
    for game in ("oot", "mm"):
        b = pl.get(game, {})
        if "custom" not in b or "foreign_base" not in b:
            continue
        out[game] = {
            "custom": b["custom"][0], "custom_size": b["custom"][1],
            "foreign_base": b["foreign_base"], "foreign_size": b["foreign"][1],
            "own": b.get("own", (None,))[0], "custom_gap": b["custom_gap"],
        }
        # Which extra record counts Triforce pieces in THIS build (payload.py):
        # it has moved between versions, so it is measured, not assumed. Null
        # when the records look like no build seen before, and then the figure
        # simply does not show.
        if "triforce" in b:
            out[game]["triforce"] = b["triforce"]
        # Which instance of a shared scene the player is in (GROTTOS,
        # FAIRY_FOUNTAIN): gLastScene's address in the payload's BSS and the
        # grotto byte's offset in the save context, both read off the code
        # (payload.last_scene / payload.grotto_data). Null when the code did
        # not give them, and then the panel lists every instance.
        for key in ("last_scene", "last_scene_entrance", "grotto_data"):
            if key in b:
                out[game][key] = b[key]
    lay = pl.get("layout")
    if lay:
        out["layout"] = {g: {k: v for k, v in lay[g].items() if not k.startswith("_")}
                         for g in ("oot", "mm")}
    return out or None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rom", help="the seed's .z64 ROM; without it the xflags stay unresolved")
    ap.add_argument("--spoiler", help="spoiler log, to know which dungeons are Master Quest")
    args = ap.parse_args(argv)

    scenes = load_scenes()
    npcs = load_npcs()
    print(f"scenes.yml: {len(scenes)} scenes, npc.yml: {len(npcs)} npcs")

    tables = {}
    rom_bytes = None
    located = None
    if args.rom:
        # careful: do not call it `rom`, that shadows the module of that name
        rom_bytes = pathlib.Path(args.rom).read_bytes()
        try:
            rom.extra_dma(rom_bytes)
        except ValueError as ex:
            sys.exit(str(ex))
        # before anything reads XFLAGS_COUNT / CUSTOM_*: XflagTables takes its
        # bit limit from it at construction
        located = apply_payload_layout(rom_bytes)
        vroms = locate_xflag_tables(rom_bytes, scenes) or {}
        for g in XFLAG_TABLES:
            tables[g] = XflagTables(rom_bytes, g, vroms.get(g))
            t = tables[g]
            print(f"xflags {g}: ROM tables read ({t.limit} bits of room)")
    # The spoiler's list, when there is one, is kept only to check the answer
    # against: which scenes are Master Quest is worked out from the ROM once
    # the placement is read (placement.master_quest_scenes), below.
    mq = load_mq(args.spoiler)
    if mq is not None:
        print(f"spoiler: Master Quest = {sorted(mq) or 'none'}")

    checks = []
    unresolved_scenes = set()
    xflag_errors = []

    # Every check is built from its override key: the CSV row is walked to keep
    # the order and to carry the labels, but nothing that reaches an address
    # comes out of its columns any more. See check_from_key().
    #
    # And the ROM is the census. A row it does not list is kept —Master Quest
    # rows, and the handful the generator filtered out— but it is CSV-only, and
    # that is what apply_bit_priority() uses to decide who keeps a bit.
    import placement

    claves_rom = set(placement.read_tables(rom_bytes)) if rom_bytes else set()
    # a scene scenes.yml does not know can still be pinned down by the ROM
    scenes.update(recover_scene_ids(claves_rom, scenes, npcs))
    escenas = scene_names(scenes)
    solo_csv = set()
    vistas = set()
    sin_clave = 0

    for game, pool in (("oot", "pool_oot.csv"), ("mm", "pool_mm.csv")):
        n = 0
        for row in load_pool(DATA / pool):
            n += 1
            scene_id, csv_id, xflag = row_parts(game, row, scenes, npcs,
                                                unresolved_scenes)
            key = placement.override_key(row["type"], scene_id, csv_id, xflag)
            etiquetas = {
                "name": row["location"],
                "hint": row["hint"],
                "vanilla": row["item"],
                "type": row["type"],
                "scene": row["scene"],
                "scene_id": scene_id,
                "mq": row["location"].startswith("MQ "),
            }
            if key is None:
                # It still comes out, just with no address. See
                # check_without_key: dropping it would make a check vanish.
                sin_clave += 1
                checks.append(
                    check_without_key(game, etiquetas, scene_id, csv_id))
                continue
            entry = check_from_key(game, key, etiquetas,
                                   tables, xflag_errors, escenas)
            vistas.add((game, key))
            if claves_rom and (game, key) not in claves_rom:
                solo_csv.add(id(entry))
            checks.append(entry)
        print(f"{pool}: {n} locations")
    if sin_clave:
        print(f"warning: {sin_clave} rows form no override key; they come out"
              " with no address (scenes.yml or npc.yml does not know them)")

    # A key the ROM places and no row names. Zero on the three seeds measured,
    # but if a future version adds a location the CSVs do not have, this is
    # what stops it from simply not existing: it comes out with its bit right
    # and a made-up name, which is the whole point of the ROM being the census.
    huerfanas = []
    for game, key in sorted(claves_rom - vistas):
        entry = check_from_key(game, key, None, tables, xflag_errors, escenas)
        huerfanas.append(entry)
        checks.append(entry)
    if huerfanas:
        print(f"warning: {len(huerfanas)} keys the ROM places have no row in"
              " the pool CSVs; they come out with a made-up name")
        for entry in huerfanas[:5]:
            print(f"   {entry['name']}")

    # Who keeps the bit when two want it. Only meaningful with a ROM: without
    # one there are no xflag bits resolved and nothing to arbitrate.
    if claves_rom:
        apply_bit_priority(checks, solo_csv)

    # Barrier: if the ROM tables do not match what we expect, what comes out
    # is garbage and it **is not written**. That happens when the seed is from
    # another OoTMM version: the table VROMs are v32.0 constants, so they point
    # at data that is not theirs and negative bitPos values pour out.
    #
    # Clobbering a good checks.json with a useless one is worse than doing
    # nothing, above all because the overlay triggers this by itself at start.
    n_xflags = sum(1 for c in checks if c["target"] == "xflags")
    if tables and n_xflags and len(xflag_errors) > n_xflags * 0.02:
        pct = 100 * len(xflag_errors) / n_xflags
        print()
        print(f"ABORTED: {len(xflag_errors)} of {n_xflags} xflags give an impossible bit ({pct:.0f}%).")
        print("The ROM tables do not match the v32.0 constants.")
        print("This seed is almost certainly from another OoTMM version.")
        print("checks.json is left untouched; whatever was there still stands.")
        for name, err in xflag_errors[:3]:
            print(f"   example: {name}: {err}")
        return 1

    # Second barrier, and it only shows up now that the tables are located by
    # shape instead of by a constant. With an older seed the addresses come out
    # right, every bit lands in range and the first barrier says nothing — but
    # the pool CSVs are still v32.0's, and that version has actors this ROM
    # does not, so their rows land on top of other checks' bits. Two different
    # checks sharing a bit is only legitimate between a vanilla/MQ pair.
    choques = collisions(checks)
    if tables and choques:
        print()
        print(f"ABORTED: {len(choques)} pairs of checks share a bit without being")
        print("a vanilla/MQ pair. The tables were found, so the addresses are right;")
        print("what does not match this ROM is data/pool_*.csv, which is v32.0's.")
        print("checks.json is left untouched; whatever was there still stands.")
        for a, b in choques[:3]:
            print(f"   example: {a} / {b}")
        return 1

    # What item sits in each spot, from the ROM itself. This is what the
    # spoiler log used to be needed for, and here it comes out of nowhere but
    # the ROM you are already playing.
    colocacion = None
    misma_version = None
    mundo = None
    # null means "not worked out": without the placement there is nothing to
    # tell the twins apart with, and the reader keeps the vanilla rows
    mq_scenes = None
    dudosas = set()
    if args.rom:
        import placement

        # The spoiler's worlds, when there is one: which of them matches what
        # the ROM placed is the surest way of knowing which world this ROM is,
        # and it needs no address, so it outlives any version.
        por_mundo = None
        if args.spoiler:
            try:
                import ootmm as _o
                por_mundo = _o.load_spoiler_worlds(args.spoiler)
            except Exception as ex:
                print(f"spoiler: could not be read by world ({type(ex).__name__}: {ex})")

        print()
        try:
            ok, sin_clave, no_estan, misma_version, mundo = placement.resolve(
                rom_bytes, checks, spoiler_worlds=por_mundo)
        except Exception as ex:
            # missing placement does not invalidate checks.json: the overlay
            # keeps working and a spoiler can still be loaded by hand
            print(f"placement: could not be read ({type(ex).__name__}: {ex})")
            print("  checks.json is written anyway, just without each spot's item")
        else:
            activos = sum(1 for c in checks if c["addr"] is not None)
            print(f"placement (COMBO_VROM_CHECKS): {ok} locations with an item")
            print(f"  no key could be formed: {sin_clave}   not in the table: {no_estan}")
            colocacion = {"resolved": ok, "no_key": sin_clave, "not_found": no_estan}
            # Which dungeons this seed lays out as Master Quest. It needs the
            # keys resolve() just wrote, so it happens here and not with the
            # rest of the settings.
            son_mq, dudosas = placement.master_quest_scenes(rom_bytes, checks, True)
            mq_scenes = sorted(son_mq)
            if mq is not None and len(mq) != len(son_mq):
                print(f"  NOTE: the spoiler names {len(mq)} Master Quest dungeons"
                      f" and the ROM's placement says {len(son_mq)}; going with the ROM")
            if ok < activos * 0.9:
                print("  warning: under 90% of the checks with an address have an item;")
                print("           the overlay will need a spoiler to filter")
            if not misma_version:
                # The names disagreeing means this seed is from another OoTMM
                # version than data/. The addresses can still be right —the
                # xflag tables are found in the ROM— but the pool CSVs, which
                # is where every check NAME comes from, are v32.0's. So the
                # tracker can mark a bit that belongs to a different actor and
                # call it by the wrong name, in the wrong region. It has to say
                # so: this is not something the user can work out from looking.
                print("  WARNING: this seed is from a different OoTMM version than")
                print("           data/. Addresses come from the ROM and hold, but")
                print("           the check names and regions come from the v32.0")
                print("           CSVs and can be wrong for this seed.")

    # The shuffled entrances, read from the ROM like the placement (see
    # entrances.py). Null without a ROM; an empty list on a seed that does not
    # shuffle any (the fixed OoT<->MM link is in the list, flagged `link`).
    entradas = None
    if rom_bytes is not None:
        try:
            import entrances as entrances_mod
            entradas = entrances_mod.resolve(rom_bytes)
            reales = sum(1 for e in entradas if not e["link"])
            print(f"entrances: {reales} shuffled in this seed" if reales
                  else "entrances: none shuffled in this seed")
        except Exception as ex:
            print(f"entrances: could not be read ({type(ex).__name__}: {ex})")

    # The soul shuffle's catalogue and bitmaps, read from the ROM (souls.py,
    # payload.souls_block). Null when built without a ROM, like the entrances.
    almas = None
    if rom_bytes is not None:
        try:
            import souls as souls_mod
            almas = souls_mod.build(rom_bytes, checks, located)
        except Exception as ex:
            print(f"souls: could not be read ({type(ex).__name__}: {ex})")

    # Which alternate headers each scene has, and which xflag rows are the same
    # actor in more than one of them (setups.py). The pool lists such an actor
    # once, under one setup, and the game marks that row whichever setup is
    # loaded; the overlay must not set it aside as "another setup". Adds
    # `setups` to those rows. Null without a ROM.
    capas = None
    if rom_bytes is not None:
        try:
            import setups as setups_mod
            capas = setups_mod.annotate(checks, rom_bytes)
        except Exception as ex:
            print(f"setups: could not be read ({type(ex).__name__}: {ex})")

    resolved = [c for c in checks if c["addr"] is not None]
    import collections
    bytarget = collections.Counter(c["target"] for c in checks)
    out = {
        "version": "v32.0",
        "source": "OoTMM/OoTMM data/ (pool_*.csv, defs/scenes.yml, defs/gi.yml)",
        "rom": args.rom,
        # where each row's item came from; null when generated without a ROM
        "placement": colocacion,
        "entrances": entradas,
        # The SCENE alternate headers each OoT scene has, by scene id: the list
        # the game walks to resolve the loaded setup (setups.py). Null without
        # a ROM; then the overlay falls back to the setups the checks mention.
        "scene_layers": capas,
        # False when the ROM's item names disagree with data/gi.yml, i.e. the
        # seed is from another OoTMM version. Null when built without a ROM.
        "same_version_as_data": misma_version,
        # Which world of a multiworld this ROM plays (placement.world_of_rom).
        # 1 on any single-player seed; null when nothing could say, and then
        # the first world is assumed, as it always was.
        "world": mundo,
        "layout": LAYOUT,
        "custom_save": {
            # gSharedCustomSave with the game running OoT. It is one single
            # shared block, so MM's flags are readable from OoT too; running MM
            # the base is a different one, handled at runtime by the overlay.
            "base": CUSTOM_BASE,
            "valid_while": "oot",
            "oot": CUSTOM_OOT,
            "mm": CUSTOM_MM,
            "flash": {"off": FLASH_CUSTOM_SAVE, "stride": FLASH_FILE_STRIDE},
        },
        # every check carries "anchor" and "off"; these are the bases the
        # absolute "addr" was computed with, so it can be relocated at start
        "anchors": ANCHOR_BASE,
        # What the ROM's own code says about where its globals live, per
        # running game (payload.py). Null when it could not be read, and then
        # everything above is the v32.0 constant. The overlay uses it to put
        # gSharedCustomSave where the ROM says instead of measuring it from
        # bits that a fresh save does not have.
        "payload": _payload_json(located),
        # scenes whose Master Quest version is the one this seed has. With a
        # spoiler and no MQ it is an empty list, meaning "every MQ row is
        # surplus"; without a spoiler it is null, meaning "unknown".
        # Which scenes are Master Quest, and which could not be told apart
        # (nothing shuffled in them); the reader keeps the vanilla twin there.
        "mq": {"scenes": mq_scenes, "unknown": sorted(dudosas)} if mq_scenes is not None else None,
        # the soul shuffle catalogue and bitmaps; null when built without a ROM
        "souls": almas,
        "checks": checks,
    }
    (OUT / "checks.json").write_text(json.dumps(out, indent=1), encoding="utf-8")

    print()
    print(f"total          {len(checks)} locations")
    print(f"with address   {len(resolved)}")
    print(f"pending        {len(checks) - len(resolved)}")
    print()
    print("by target (per mark.c), resolved / total:")
    ok = collections.Counter(c["target"] for c in resolved)
    for k, v in bytarget.most_common():
        note = {
            "scene": "MM scene layout missing",
            "custom": "caughtFishFlags missing",
            "gs_flags": "gsFlags not located (bit = id - 8)",
            "cow_flags": "gCowFlags not located",
            "mm_stray_fairy": "",
            "xflags": "" if tables else "needs --rom",
        }.get(k, "")
        if ok[k] == v:
            note = ""
        print(f"   {k:16} {ok[k]:5d} / {v:<5d} {note}")

    # Vanilla and MQ share a bit on purpose, so a repeat is expected and worth
    # counting. A repeat that is *not* a vanilla/MQ pair is a defect, and that
    # one never gets this far: collisions() aborts above.
    if tables:
        for game in XFLAG_TABLES:
            seen = {}
            dup = 0
            for c in checks:
                if c["game"] != game or "bitpos" not in c:
                    continue
                if seen.setdefault(c["bitpos"], c) is not c:
                    dup += 1
            n = sum(1 for c in checks if c["game"] == game and "bitpos" in c)
            print(f"   xflags {game}: {n} resolved, {len(seen)} distinct bits, {dup} vanilla/MQ pairs")
    if xflag_errors:
        print(f"   xflags with an error: {len(xflag_errors)}")
        for name, err in xflag_errors[:5]:
            print(f"      {name}: {err}")
    if unresolved_scenes:
        print(f"scenes with no index in scenes.yml: {len(unresolved_scenes)}")
        for s in sorted(unresolved_scenes)[:10]:
            print(f"   {s}")
    print()
    print("-> checks.json")


if __name__ == "__main__":
    sys.exit(main())
