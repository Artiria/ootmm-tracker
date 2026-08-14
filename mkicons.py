#!/usr/bin/env python3
"""
mkicons.py - pull the item icons out of the ROM and build the overlay sheet.

No icons are fetched from anywhere: they come out of your own ROM. OoT's
`icon_item_static` (file 8 of the dmadata, at 0x7430) holds 112 icons of 32x32
RGBA32, and **the icon index is the item id**, so `items.h` names them all
without having to count positions by eye.

Output:
  icons.png    a 16-column sheet: OoT's, and MM's shifted after them
  icons.json   item name/id -> index in the sheet

About MM
--------
MM brings its own too, but not through the dmadata: its files 8 and 9
(`icon_item_static`, `icon_item_24_static`) are marked as missing. It keeps
them in a **CmpDma archive**, each icon compressed separately with Yaz0, and
that is where the 24 masks and the rest of Termina's items come from. See
mm_icons() and the comment in find_cmpdma_archives().

Usage: python mkicons.py --rom <seed.z64>
"""

import argparse
import json
import pathlib
import re
import struct
import zlib

import paths
import rom

DATA = pathlib.Path(paths.res("data"))
OUT = pathlib.Path(paths.USER_DIR)

OOT_DMA_ADDR = 0x7430        # combo/dma.h
ICON_ITEM_STATIC = 8         # data/files/files-oot.txt, line 9
ICON_24_STATIC = 9           # line 10
ICON_SIDE = 32
ICON_BYTES = ICON_SIDE * ICON_SIDE * 4
COLS = 16

# The icons are not all in one place, and the cutoff was measured, not
# guessed: an icon counts as valid when its four corners are transparent and
# it has between 80 and 1000 opaque pixels, which gives a clean run of
# 0x00..0x58 followed by noise. Medallions and stones live in the 24x24 sheet,
# where the index is `id - 0x66`.
BIG_LAST = 0x58              # last id with a 32x32 icon
SMALL_FIRST = 0x66           # ITEM_OOT_MEDALLION_FOREST
SMALL_SIDE = 24
SMALL_BYTES = SMALL_SIDE * SMALL_SIDE * 4
ICON_COUNT = 0x7A            # up to ITEM_OOT_MAGIC_BIG


def png(w, h, rgba):
    """RGBA8 PNG using only the standard library."""
    raw = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def dma_entry(rom_bytes, index):
    """File `index` of OoT's dmadata, decompressed when needed.

    OoTMM can generate the seed compressed, and then this arrives as Yaz0.
    """
    return rom.read_native_file(rom_bytes, index)


# --- Majora's Mask icons ----------------------------------------------------
#
# They are not in the dmadata: MM keeps them in a **CmpDma archive**, which is
# a table of offsets followed by the files, each one compressed separately with
# Yaz0. That is why no pixel sweep could find them: raw, they are compressed
# data, not images.
#
# The format comes from `src/code/sys_cmpdma.c` in the MM decomp:
#
#   u32 dataStart      also the size of the table, so there are
#                      dataStart/4 - 1 files
#   u32 offs[...]      start of each file, relative to seg + dataStart
#                      (file 0 starts at 0; its end is offs[1])
#
# And `CmpDma_LoadFile(SEGMENT_ROM_START(icon_item_static_yar), id, ...)` uses
# **the item id as the index**, so entry `i` of the archive is the icon for
# `ITEM_MM_* == i`. Verified: entry 0x32 is the Deku Mask.
#
# The archive is not at a fixed place, so it is located by its shape.
MM_ICON_BYTES = 32 * 32 * 4
MM_SHEET_BASE = 128          # where MM's icons start in the sheet


def find_cmpdma_archives(rom_bytes):
    """CmpDma archive headers, found by their structure.

    Start from every Yaz0 block and test whether `dataStart` bytes earlier
    there is a header pointing exactly there. Much faster than sweeping the
    whole ROM word by word.
    """
    encontrados = []
    vistos = set()
    pos = 0
    while True:
        pos = rom_bytes.find(b"Yaz0", pos)
        if pos < 0:
            break
        inicio = pos
        pos += 1
        for ds in range(8, 0x1004, 4):
            seg = inicio - ds
            if seg < 0 or seg in vistos:
                continue
            if struct.unpack_from(">I", rom_bytes, seg)[0] != ds:
                continue
            n = ds // 4
            offs = struct.unpack_from(f">{n}I", rom_bytes, seg)
            cuerpo = offs[1:]
            if len(cuerpo) < 8 or any(cuerpo[i] >= cuerpo[i + 1] for i in range(len(cuerpo) - 1)):
                continue
            vistos.add(seg)
            encontrados.append((seg, ds, list(offs)))
            break
    return encontrados


def read_cmpdma(rom_bytes, seg, ds, offs):
    """The files of a CmpDma archive, already decompressed."""
    base = seg + ds
    out = []
    for i in range(len(offs) - 1):
        ini = 0 if i == 0 else offs[i]
        try:
            out.append(rom.yaz0(rom_bytes[base + ini : base + offs[i + 1]]))
        except Exception:
            out.append(None)
    return out


# Past this point the archive index stops being the item id. The twelve songs
# **have no entry**: the game draws them all with a single note texture that
# lives in `code`, not in the archive (`gItemIconSongNoteTex` in z_inventory.c).
# Since the archive is packed without them, everything after is shifted, and it
# gets discarded rather than hang the wrong icons: entry 0x61 is the Bombers'
# notebook, not a note.
MM_ID_LAST = 0x60            # ITEM_REMAINS_TWINMOLD, the last one that lines up


def mm_icons(rom_bytes):
    """{MM item id -> 32x32 RGBA32 icon}, pulled from the ROM.

    Of the CmpDma archives we take the one with the most entries of exactly
    icon size; the item icon one has around 98.
    """
    mejor, mejor_n = None, 0
    for seg, ds, offs in find_cmpdma_archives(rom_bytes):
        files = read_cmpdma(rom_bytes, seg, ds, offs)
        n = sum(1 for f in files if f and len(f) == MM_ICON_BYTES)
        if n > mejor_n:
            mejor, mejor_n = files, n
    if mejor_n < 40:
        return {}
    return {i: f for i, f in enumerate(mejor)
            if f and len(f) == MM_ICON_BYTES and i <= MM_ID_LAST}


def load_item_ids():
    """ITEM_OOT_* / ITEM_MM_* -> id, from data/ref/items.h."""
    src = (DATA / "ref" / "items.h").read_text(encoding="utf-8", errors="replace")
    out = {}
    for m in re.finditer(r"#define\s+(ITEM_(?:OOT|MM)_[A-Z0-9_]+)\s+(0x[0-9a-fA-F]+|\d+)", src):
        out[m.group(1)] = int(m.group(2), 0)
    return out


def build_sheet(big, small, mm_ico=None):
    """A single sheet of 32x32 cells.

    OoT ids take 0..0x79; MM's are shifted by MM_SHEET_BASE, because both
    games number their items from zero.
    """
    mm_ico = mm_ico or {}
    total = ICON_COUNT if not mm_ico else MM_SHEET_BASE + max(mm_ico) + 1
    rows = (total + COLS - 1) // COLS
    W, H = COLS * ICON_SIDE, rows * ICON_SIDE
    buf = bytearray(W * H * 4)
    have = []

    def blit(idx, src, side):
        cx = (idx % COLS) * ICON_SIDE + (ICON_SIDE - side) // 2
        cy = (idx // COLS) * ICON_SIDE + (ICON_SIDE - side) // 2
        for y in range(side):
            o = ((cy + y) * W + cx) * 4
            buf[o:o + side * 4] = src[y * side * 4:(y + 1) * side * 4]
        have.append(idx)

    for i in range(BIG_LAST + 1):
        blit(i, big[i * ICON_BYTES:(i + 1) * ICON_BYTES], ICON_SIDE)
    n_small = len(small) // SMALL_BYTES
    for k in range(n_small):
        idx = SMALL_FIRST + k
        if idx >= ICON_COUNT:
            break
        blit(idx, small[k * SMALL_BYTES:(k + 1) * SMALL_BYTES], SMALL_SIDE)

    mm_map = {}
    for item_id, px in sorted(mm_ico.items()):
        idx = MM_SHEET_BASE + item_id
        blit(idx, px, ICON_SIDE)
        mm_map[item_id] = idx

    return W, H, bytes(buf), sorted(have), mm_map


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rom", required=True, help="the seed's .z64 ROM (or any OoTMM one)")
    args = ap.parse_args(argv)

    rom_bytes = pathlib.Path(args.rom).read_bytes()
    big = dma_entry(rom_bytes, ICON_ITEM_STATIC)
    small = dma_entry(rom_bytes, ICON_24_STATIC)
    print(f"icon_item_static:    {len(big)} bytes -> ids 0x00..0x{BIG_LAST:02X}")
    print(f"icon_item_24_static: {len(small)} bytes -> {len(small)//SMALL_BYTES} icons from 0x{SMALL_FIRST:02X}")

    mm_ico = mm_icons(rom_bytes)
    print(f"MM icons (CmpDma archive): {len(mm_ico)}")

    W, H, rgba, have, mm_map = build_sheet(big, small, mm_ico)
    (OUT / "icons.png").write_bytes(png(W, H, rgba))
    print(f"-> icons.png  {W}x{H}, {len(have)} icons in {COLS} columns")

    ids = load_item_ids()
    # OoT indices only: MM's live shifted and must not slip in here, or an
    # OoT item with a high id would end up using an MM icon
    haveset = {i for i in have if i < ICON_COUNT}
    oot = {k[len("ITEM_OOT_"):]: v for k, v in ids.items()
           if k.startswith("ITEM_OOT_") and v in haveset}
    mm = {k[len("ITEM_MM_"):]: v for k, v in ids.items() if k.startswith("ITEM_MM_")}

    # MM bridge. Matching on the exact name falls short because the two games
    # order the words differently (MASK_KEATON against KEATON_MASK) and throw
    # in linking words (OCARINA_OF_TIME against OCARINA_TIME), so what gets
    # compared is the **set of words**, minus the linking ones.
    #
    # No fuzzy matching: it would pair BOMBS_10 with BOMBCHU_10 and
    # LENS_OF_TRUTH with MASK_OF_TRUTH, which are different items. Whatever
    # the rule misses goes in an explicit table.
    STOP = {"OF", "THE"}
    def canon(name):
        return tuple(sorted(t for t in name.split("_") if t and t not in STOP))

    by_canon = {}
    for name, idx in oot.items():
        by_canon.setdefault(canon(name), idx)

    ALIAS = {
        "LENS_OF_TRUTH": "LENS",
        "SHIELD_HERO": "SHIELD_HYLIAN",
        "HEART_PIECE": "HEART_PIECE2",
        "BOMBCHU": "BOMBCHU_10",
        "BOMBCHU_5": "BOMBCHU_10",
        "BOMBCHU_20": "BOMBCHU_10",
        "BOMBCHU_ALT": "BOMBCHU_10",
    }

    bridge, exact, by_words, by_alias = {}, 0, 0, 0
    for name, v in mm.items():
        if name in oot:
            bridge[v] = oot[name]
            exact += 1
        elif canon(name) in by_canon:
            bridge[v] = by_canon[canon(name)]
            by_words += 1
        elif name in ALIAS and ALIAS[name] in oot:
            bridge[v] = oot[ALIAS[name]]
            by_alias += 1
    print(f"MM bridge: {exact} by name, {by_words} by words, {by_alias} by alias")

    out = {
        # which ROM they came from, so discover.py knows whether they are current
        "rom": args.rom,
        "sheet": "icons.png",
        "side": ICON_SIDE,
        "cols": COLS,
        "count": ICON_COUNT,
        # canonical name -> index in the sheet (== OoT item id)
        "oot": oot,
        # MM item id -> index in the sheet. These are the real ones,
        # pulled from the ROM's CmpDma archive.
        "mm": {str(k): v for k, v in mm_map.items()},
        # name-based fallback, for whatever has no icon of its own
        "mm_bridge": {str(k): v for k, v in bridge.items()},
    }
    (OUT / "icons.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"-> icons.json  {len(oot)} from OoT, {len(mm_map)} from MM, {len(bridge)} bridged")


if __name__ == "__main__":
    main()
