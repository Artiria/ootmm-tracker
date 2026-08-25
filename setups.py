#!/usr/bin/env python3
"""
setups.py - the alternate scene headers, read off the ROM.

A scene exists in up to four versions -- "setups": child day, child night,
adult day, adult night -- and each has its own actor list, so its own checks.
Two things about that only the ROM can say:

  1. **Which versions a scene has.** OoTMM resolves the loaded setup by walking
     the SCENE's alternate header list down from the layer the save asks for
     (updateSceneSetup, oot/room.c). The overlay used to guess that list from
     the setups its checks mention, which is the ROOM side; this reads it.

  2. **Which checks are the same actor in more than one version.** xsanity.ts
     gives twins -- same room, same kind, same position, another setup -- one
     name (letterChecks), and the pool keeps a single row under one of the
     setups. In game the actor's override aliases the other setups onto that
     row (EnWood02_Alias, ObjHamishi_Alias and co.), so cutting a Hyrule Field
     bush by day marks the row filed under the night setup. Not knowing that,
     the overlay set 105 of Hyrule Field's 110 checks aside as "in another
     setup" while the player was cutting them (25 ago 2026).

Only OoT. MM's extra setups are another mechanism (mm/room.c) with its own
scene table; its rows keep their single setup.
"""

import struct
import sys

import rom

# gSceneTable of OoT NTSC 1.0, inside `code` (xsanity.ts CONFIGS.oot). Entry:
# vromStart, vromEnd, titleStart, titleEnd, u8 unk, u8 config, u8 unk, u8 pad.
SCENE_TABLE_VROM = 0xB71440
SCENE_TABLE_ENTRY = 0x14
SCENE_COUNT = 101
MQ_SCENE_BASE = 0x70      # Play_ExpandMQ: MQ dungeons are the vanilla ids + 0x70
GENERIC_ROOM = 0x20       # comboXflagInit's `0x20 | grottoId` is not a room number

CMD_ACTORS, CMD_ROOMS, CMD_END, CMD_ALT_HEADERS = 0x01, 0x04, 0x14, 0x18
EN_WOOD02 = 0x0077        # trees and bushes are one actor; the type byte tells which


class _Files:
    """OoT's native dmadata, decompressed on demand and kept."""

    def __init__(self, rom_bytes):
        self.rom = rom_bytes
        self.entries = []
        for i in range(4000):
            vs, ve, ps, pe = struct.unpack_from(">IIII", rom_bytes, rom.OOT_DMA_ADDR + i * 16)
            if i > 2 and vs == 0 and ve == 0:
                break
            self.entries.append((vs, ve, ps, pe))
        self._cache = {}

    def file_of(self, vrom):
        for vs, ve, ps, pe in self.entries:
            if vs <= vrom < ve:
                if vs not in self._cache:
                    self._cache[vs] = rom.entry_data(self.rom, vs, ve, ps, pe)
                return self._cache[vs], vs
        raise KeyError(f"no DMA entry holds {vrom:#x}")

    def u32(self, vrom):
        data, base = self.file_of(vrom)
        return struct.unpack_from(">I", data, vrom - base)[0]

    def u8(self, vrom):
        data, base = self.file_of(vrom)
        return data[vrom - base]


def _find_cmd(files, vrom, op):
    """xsanity.ts findHeaderOffset: 8-byte commands up to END."""
    for _ in range(256):
        code = files.u8(vrom)
        if code == op:
            return vrom
        if code == CMD_END:
            return None
        vrom += 8
    return None


def _alt_headers(files, file_vrom, header_vrom):
    """The three alternate header pointers of a scene or room (0 = none)."""
    cmd = _find_cmd(files, header_vrom, CMD_ALT_HEADERS)
    if cmd is None:
        return None
    lst = file_vrom + (files.u32(cmd + 4) & 0xFFFFFF)
    return [files.u32(lst + i * 4) for i in range(3)]


def _actors(files, file_vrom, header_vrom):
    """[(type, (x, y, z), params)] of a header's actor list; the index is the xflag id."""
    cmd = _find_cmd(files, header_vrom, CMD_ACTORS)
    if cmd is None:
        return []
    n = (files.u32(cmd) >> 16) & 0xFF
    base = file_vrom + (files.u32(cmd + 4) & 0xFFFFFF)
    data, fbase = files.file_of(base)
    out = []
    for i in range(n):
        typ, x, y, z, _rx, _ry, _rz, params = struct.unpack_from(
            ">HhhhHHHH", data, base - fbase + i * 16)
        out.append((typ, (x, y, z), params))
    return out


def read_scenes(rom_bytes, verbose=True):
    """{scene_id: {"layers": [1, 2], "rooms": {room: {setup: [actors]}}}}.

    `layers` are the SCENE's alternate headers that exist -- the list
    updateSceneSetup walks. `rooms` hold each ROOM header's actor list per
    setup -- what xsanity enumerated, so an xflag id indexes straight into it.
    A scene that does not parse is left out and said so; nothing is guessed.
    """
    files = _Files(rom_bytes)
    out = {}
    bad = 0
    for sid in range(SCENE_COUNT):
        try:
            svrom = files.u32(SCENE_TABLE_VROM + sid * SCENE_TABLE_ENTRY)
            if not svrom:
                continue
            alt = _alt_headers(files, svrom, svrom)
            layers = [i + 1 for i, p in enumerate(alt or []) if p]
            rooms = {}
            rooms_cmd = _find_cmd(files, svrom, CMD_ROOMS)
            if rooms_cmd is not None:
                n = (files.u32(rooms_cmd) >> 16) & 0xFF
                lst = svrom + (files.u32(rooms_cmd + 4) & 0xFFFFFF)
                for r in range(n):
                    rv = files.u32(lst + r * 8)
                    per = {0: _actors(files, rv, rv)}
                    for s, p in enumerate(_alt_headers(files, rv, rv) or [], start=1):
                        if p:
                            per[s] = _actors(files, rv, rv + (p & 0xFFFFFF))
                    rooms[r] = per
            out[sid] = {"layers": layers, "rooms": rooms}
        except (KeyError, struct.error, IndexError):
            bad += 1
    if verbose and bad:
        print(f"setups: {bad} scenes could not be parsed; their rows keep a single setup")
    return out


def _identity(actor):
    """What makes two actors in different headers the same check.

    xsanity's letterChecks compares room, check type and position. The actor
    type and position cover that, except that a tree and a bush are the same
    actor (En_Wood02) told apart by the type byte, so that byte's class joins.
    """
    typ, pos, params = actor
    if typ == EN_WOOD02:
        return (typ, pos, (params & 0xFF) <= 0x0A)
    return (typ, pos)


def annotate(checks, rom_bytes, verbose=True):
    """Add `setups` to every OoT xflag row that is the same actor in more than
    one setup, and return {"oot": {scene_id: [layers]}} for checks.json.

    The row keeps its own `setup` (the key the pool filed it under); `setups`
    lists every setup it can be collected in, and is only written when there
    is more than one, so the table does not grow for the common case.
    """
    scenes = read_scenes(rom_bytes, verbose)
    if not scenes:
        if verbose:
            print("setups: the scene table could not be read; rows keep a single setup")
        return None
    twins = 0
    for c in checks:
        xf = c.get("xflag")
        sid = c.get("scene_id")
        if c.get("game") != "oot" or xf is None or sid is None or sid >= MQ_SCENE_BASE:
            continue
        if xf["room"] >= GENERIC_ROOM:
            continue
        per = scenes.get(sid, {}).get("rooms", {}).get(xf["room"])
        if not per:
            continue
        mine = per.get(xf["setup"])
        if not mine or xf["actor"] >= len(mine):
            continue
        ident = _identity(mine[xf["actor"]])
        present = sorted(s for s, acts in per.items()
                         if s == xf["setup"] or any(_identity(a) == ident for a in acts))
        if len(present) > 1:
            xf["setups"] = present
            twins += 1
    layers = {str(sid): sc["layers"] for sid, sc in scenes.items()}
    if verbose:
        con = sum(1 for v in layers.values() if v)
        print(f"setups: {con} scenes have alternate headers; "
              f"{twins} rows are the same actor in more than one setup")
    return {"oot": layers}


def main(argv=None):
    """`python setups.py <rom> [checks.json]`: the layers per scene and, with a
    table, the twin rows per scene -- to look at, and for the guards."""
    import collections
    import json

    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__)
        return 2
    rom_bytes = open(argv[0], "rb").read()
    scenes = read_scenes(rom_bytes)
    for sid, sc in sorted(scenes.items()):
        if sc["layers"]:
            rooms = ", ".join(f"r{r}:{'/'.join(str(s) for s in sorted(per))}" for r, per in sc["rooms"].items())
            print(f"scene {sid:#04x}: layers {sc['layers']}  rooms {rooms}")
    if len(argv) > 1:
        table = json.load(open(argv[1], encoding="utf-8"))
        annotate(table["checks"], rom_bytes)
        por = collections.Counter()
        for c in table["checks"]:
            xf = c.get("xflag") or {}
            if "setups" in xf:
                por[(c["scene"], tuple(xf["setups"]))] += 1
        for (scene, setups), n in sorted(por.items()):
            print(f"{scene}: {n} rows in setups {list(setups)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
