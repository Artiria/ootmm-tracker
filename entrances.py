#!/usr/bin/env python3
"""
entrances.py - which entrance leads where, read from the ROM.

Entrance shuffle is the one thing every OoTMM tracker either leaves out or asks
the spoiler for. It does not have to: the generator writes the shuffled
entrances into the ROM the same way it writes the item placement, because the
game needs them to know where a door goes.

    COMBO_VROM_ENTRANCES = 0xF0800000 (OoT build) / 0xF0900000 (MM build)
    typedef struct { s32 key; s32 value; } Entrance;   -- entrance.c
    ... terminated by key == -1; MASK_FOREIGN_ENTRANCE (0x80000000) on `value`
    means "the other game's entrance"; only the entrances that CHANGE are
    written (randomizer/entrances.ts), so a seed without entrance shuffle
    carries just the OoT<->MM link.

`key` and `value` are entrance ids in each game's own numbering (OoT: the
entrance table index; MM: scene << 9 | spawn << 4, plus OoTMM's own ids past
0x10000). What they are called, and where each one is, comes from OoTMM's
`data/defs/entrances.yml` (602 entries: id, type, the area on each side, the
reverse entrance), which is a label dictionary exactly like the pool CSVs.

The live side is `gSaveContext.entrance` (OoT `entranceIndex` at +0x00, MM
`entrance` at MmSave+0x00): Play_TransitionDone in OoTMM resolves the override
and loads the scene with the DESTINATION id, so when the save's entrance
changes to a value that is a `value` in the table, the player just came
through the matching `key`. That is what the overlay watches. Ambiguity is
real and is reported as such: if the destination could also be reached by its
vanilla entrance (one that is not itself shuffled), the pairing is "probable",
and the scene the player was in before the transition is used to settle it
when it can (`maps` in the yml names that scene).

    python entrances.py ROM.z64          # print the seed's shuffled entrances
"""

import pathlib
import re
import struct
import sys

import paths
import rom as romlib

VROM = {"oot": 0xF0800000, "mm": 0xF0900000}   # combo/defs.h
FOREIGN = 0x80000000                          # MASK_FOREIGN_ENTRANCE

_ROW = re.compile(r"^([A-Z0-9_]+):\s*\{(.*)\}\s*$", re.M)


def load_defs(path=None):
    """{(game, id): {"name", "type", "areas": [from, to], "maps": [from, to],
    "reverse"}} out of entrances.yml. Parsed by hand: it is one flow-mapping per
    line and nothing else, and it keeps the tracker free of a YAML dependency."""
    p = pathlib.Path(path) if path else pathlib.Path(paths.res("data", "entrances.yml"))
    out = {}
    for m in _ROW.finditer(p.read_text(encoding="utf-8")):
        name, body = m.group(1), m.group(2)
        g = re.search(r"\bgame:\s*(\w+)", body)
        i = re.search(r"\bid:\s*(0x[0-9a-fA-F]+|\d+)", body)
        if not g or not i:
            continue
        t = re.search(r"\btype:\s*([\w-]+)", body)
        areas = re.search(r"\bareas:\s*\[(.*?)\]", body)
        maps = re.search(r"\bmaps:\s*\[(.*?)\]", body)
        rev = re.search(r"\breverse:\s*([A-Z0-9_]+)", body)
        out[(g.group(1), int(i.group(1), 0))] = {
            "name": name,
            "type": t.group(1) if t else "?",
            "areas": _split(areas.group(1)) if areas else ["", ""],
            "maps": _split(maps.group(1)) if maps else ["", ""],
            "reverse": rev.group(1) if rev else None,
        }
    return out


def _split(s):
    """The two items of a flow sequence, quotes off."""
    parts = re.findall(r"""'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)"|([^,\s][^,]*)""", s)
    vals = [(a or b or c).strip() for a, b, c in parts]
    return (vals + ["", ""])[:2]


def read_overrides(rom_bytes):
    """[(game, key, value, value_game)] from both tables, in ROM order."""
    out = []
    for game, vrom in VROM.items():
        try:
            blob = romlib.read_extra_vrom(rom_bytes, vrom)
        except (KeyError, ValueError, IndexError, struct.error):
            continue
        for i in range(len(blob) // 8):
            k, v = struct.unpack_from(">II", blob, i * 8)
            if k == 0xFFFFFFFF:
                break
            vg = game
            if v & FOREIGN:
                vg = "mm" if game == "oot" else "oot"
                v &= ~FOREIGN
            out.append((game, k & ~FOREIGN, v, vg))
    return out


def resolve(rom_bytes, defs=None):
    """The seed's shuffled entrances, labelled. Each row:

        game        the game the entrance is taken in
        src, dst    ids; dst_game the game arrived in
        src_name, dst_name        symbols from entrances.yml (or "?0x..")
        type                      the source entrance's type
        from_area, to_area        where you take it / where you land
        from_map, to_map          scene-ish names of the same, for the live side
        link        True for the fixed OoT<->MM crossing every seed carries
    """
    defs = defs or load_defs()
    rows = []
    for game, k, v, vg in read_overrides(rom_bytes):
        s = defs.get((game, k))
        d = defs.get((vg, v))
        rows.append({
            "game": game, "src": k, "dst": v, "dst_game": vg,
            "src_name": s["name"] if s else f"?{k:#x}",
            "dst_name": d["name"] if d else f"?{v:#x}",
            "type": s["type"] if s else "?",
            "from_area": s["areas"][0] if s else "",
            "to_area": d["areas"][1] if d else "",
            "from_map": s["maps"][0] if s else "",
            "to_map": d["maps"][1] if d else "",
            # the two `none` entries are the Clock Tower <-> Mask Shop link
            "link": bool(s and s["type"] == "none"),
        })
    return rows


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        sys.exit(__doc__)
    rb = pathlib.Path(argv[0]).read_bytes()
    rows = resolve(rb)
    real = [r for r in rows if not r["link"]]
    print(f"{len(rows)} overrides in the ROM, {len(real)} shuffled entrances")
    unknown = sum(1 for r in rows if r["src_name"].startswith("?") or r["dst_name"].startswith("?"))
    if unknown:
        print(f"  {unknown} with an id entrances.yml does not know")
    for r in real:
        print(f"  [{r['game']}] {r['from_area']}  ->  {r['to_area']}"
              f"   ({r['src_name']} -> {r['dst_name']}, {r['type']})")


if __name__ == "__main__":
    main()
