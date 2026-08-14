#!/usr/bin/env python3
"""
placement.py - what item sits in each location, read from the ROM.

This replaces the spoiler log. The generator writes the placement into the ROM
itself —the game needs it to know what to hand you when you open a chest— in
the `COMBO_VROM_CHECKS` file, and from there the whole thing can be read:

    typedef struct ComboOverrideData {   /* 16 bytes, SORTED by key */
        u32 key;      /* (ovType << 24) | (sceneId << 16) | (roomId << 8) | id */
        s16 player;   /* whose item it is, in multiworld */
        u16 value;    /* <-- the item, an index into GI_* */
        s16 giCloak;
        s16 unused[3];
    } ComboOverrideData;

The game walks it with a binary search (`overrideData` in item.c); we just read
it in one go.

**Why this beats depending on the spoiler**: the address is a structural
constant (`COMBO_EXTRA_DMA_VROM | 0x400000`), not a VROM that moves with every
version the way the xflag tables do. And there is no file to find, load or
validate: it comes out of the ROM you are already playing.

The names come out of the ROM too, out of `kItemNames[]`. That array lives in
the payload, which is another file of the extra DMA, and it is
`const char* const kItemNames[]` indexed by `gi - 1` (see `text.c`). Reading
it there instead of from a copy of `gi.yml` is what keeps the names from going
quietly wrong: the index is the position in that file, so one new item in the
middle shifts everything behind it and nothing complains.

`data/gi.yml` stays for the **symbolic id** (`OOT_BOMBS_5`), which is a build
symbol and does not survive compilation. It is only used if it still lines up
with the ROM, and it says so when it does not.
"""

import pathlib
import re
import struct

import paths
import rom

DATA = pathlib.Path(paths.res("data"))

# combo/defs.h. Structural constants, not addresses of one version.
VROM_CHECKS = {"oot": 0xF0400000, "mm": 0xF0500000}

# combo/item.h
OV = {
    "chest": 0x01, "collectible": 0x02, "npc": 0x03, "gs": 0x04, "sf": 0x05,
    "cow": 0x06, "shop": 0x07, "scrub": 0x08, "sr": 0x09, "fish": 0x0A,
}
OV_XFLAG0 = 0x10

# Only these carry the scene in the key. The rest live in global id spaces and
# their scene byte is 0 — found by looking at the real keys, after a first
# attempt in which npc, gs, cow, shop, scrub, sr and fish all failed at once.
CON_ESCENA = {"chest", "collectible", "sf"}


def override_key(tipo, scene_id, ident, xflag=None):
    """This location's key in the table, or None if it cannot be formed.

    `ident` is the id **from the CSV**, which for the bitmap types is the
    global index; the bit within its byte —what checks.json ends up storing—
    will not do.
    """
    if xflag is not None:
        # same as comboXflagItemQuery() in xflags.c
        if scene_id is None:
            return None
        room = (xflag["room"] & 0x3F) | ((xflag["setup"] & 3) << 6)
        ov = OV_XFLAG0 + xflag["slice"]
        return (ov << 24) | ((scene_id & 0xFF) << 16) | (room << 8) | (xflag["actor"] & 0xFF)
    ov = OV.get(tipo)
    if ov is None or ident is None:
        return None
    escena = scene_id if tipo in CON_ESCENA else 0
    if escena is None:
        return None
    return (ov << 24) | ((escena & 0xFF) << 16) | (ident & 0xFF)


def read_tables(rom_bytes):
    """{(game, key): (gi, player)} with both tables from the ROM.

    Each build carries its own: OoT's and MM's. An MM check is looked up in
    MM's table.
    """
    out = {}
    for juego, vrom in VROM_CHECKS.items():
        blob = rom.read_extra_vrom(rom_bytes, vrom)
        for i in range(len(blob) // 16):
            key, player, value, _cloak = struct.unpack_from(">IhHh", blob, i * 16)
            if key >> 24 == 0xFF:
                continue          # end-of-table sentinel
            out[(juego, key)] = (value, player)
    return out


# The names are in-game text: they carry colour codes and an article ("the
# <C1>Megaton Hammer"). The spoiler writes them bare, and the filler rules are
# written against that shape, so they have to end up the same.
_MACRO = re.compile(r"<[^>]*>")
_ARTICULO = re.compile(r"^(the|a|an|some) ", re.I)
_FILA = re.compile(r"^\s*-\s*\{.*?\bid:\s*([A-Za-z0-9_]+)")
_NOMBRE = re.compile(r'\bname:\s*"((?:[^"\\]|\\.)*)"')


def _tidy(txt):
    """Collapse the whitespace and drop the leading article."""
    txt = re.sub(r"\s+", " ", txt).strip()
    return _ARTICULO.sub("", txt).strip()


def limpia_nombre(txt):
    txt = _MACRO.sub("", txt)
    txt = txt.replace("\\n", " ").replace("\\", "")
    return _tidy(txt)


# --------------------------------------------------------------------------
# kItemNames[], out of the payload
# --------------------------------------------------------------------------
#
# The payload is one more file of the extra DMA, loaded whole at a fixed
# address, so a pointer inside it is `PAYLOAD_RAM + offset in the file`. Both
# builds carry the same names; only the text encoding differs (OoT writes a
# colour as 0x05 plus a colour byte, MM as a single byte), which is why the
# cleaning is per game.
PAYLOAD = {                     # combo/defs.h
    "oot": (0xF0000000, 0x80400000),
    "mm": (0xF0100000, 0x80720000),
}


def _clean_rom_name(raw, game):
    if game == "oot":
        out, i = bytearray(), 0
        while i < len(raw):
            if raw[i] == 0x05:      # colour: control byte + its argument
                i += 2
                continue
            out.append(raw[i])
            i += 1
    else:
        out = bytes(b for b in raw if b >= 0x20)
    return _tidy(out.decode("utf-8", "replace"))


def _string_at(blob, off, maxlen=128):
    """The NUL-terminated string at that offset, or None if it is not one."""
    if off < 0 or off >= len(blob):
        return None
    end = blob.find(b"\0", off, off + maxlen)
    if end < 0:
        return None
    s = blob[off:end]
    if s and sum(1 for b in s if 0x20 <= b <= 0x7E) / len(s) < 0.75:
        return None
    return s


def find_item_names(rom_bytes, game="oot", min_len=64):
    """[name] read from `kItemNames[]`, or None if the table is not found.

    Located **by content**, not by an address: an address would be one more
    version constant, which is the very thing this is here to remove. What
    identifies it is a run of consecutive pointers into the payload where
    every one lands on a string and most of those carry a text control code.
    That last part is what tells it apart from the other all-strings table in
    there (region names for the hints), which carries none.
    """
    vrom, ram = PAYLOAD[game]
    try:
        blob = rom.read_extra_vrom(rom_bytes, vrom)
    except (KeyError, ValueError, IndexError, struct.error):
        # struct.error is the one a ROM that is not OoTMM raises: the extra DMA
        # header sits at 0x3FFF000 and a plain 8 MB ROM has nothing there.
        return None

    n = len(blob) // 4
    words = struct.unpack_from(f">{n}I", blob, 0)
    hi = ram + len(blob)

    best = None
    start = None
    for i in range(n + 1):
        inside = i < n and ram <= words[i] < hi
        if inside and start is None:
            start = i
        elif not inside and start is not None:
            if i - start >= min_len:
                cand = _score_run(blob, words, start, i - start, ram, game)
                if cand and (best is None or len(cand) > len(best)):
                    best = cand
            start = None
    return best


def _score_run(blob, words, start, length, ram, game):
    """The run's names if it passes for kItemNames, None otherwise."""
    crudos = []
    for k in range(length):
        s = _string_at(blob, words[start + k] - ram)
        if s is None:
            return None                      # one bad pointer disqualifies it
        crudos.append(s)
    # An item name is written with a colour macro; the region-name table next
    # to it is plain text. Without this the two are indistinguishable.
    con_codigo = sum(1 for s in crudos if any(b < 0x20 for b in s))
    if con_codigo / length < 0.5:
        return None
    return [_clean_rom_name(s, game) or None for s in crudos]


def load_gi(path=None):
    """[(symbolic id, name)] indexed by gi. The index is the position plus one.

    `index = i + 1` comes from packages/generator/lib/combo/data.ts, and 0 is
    GI_NONE.
    """
    p = pathlib.Path(path) if path else DATA / "gi.yml"
    tabla = {0: ("NONE", None)}
    i = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        m = _FILA.match(line)
        if not m:
            continue
        i += 1
        n = _NOMBRE.search(line)
        tabla[i] = (m.group(1), limpia_nombre(n.group(1)) if n else None)
    return tabla


def names_from_rom(rom_bytes, gi, verbose=True):
    """(names by gi index, whether gi.yml still lines up).

    The names come from the ROM. `gi.yml` is only consulted to see whether its
    order still matches, because that is what says if its symbolic ids can be
    trusted: shifted by one item, every name would disagree.
    """
    tabla = None
    for game in ("oot", "mm"):
        tabla = find_item_names(rom_bytes, game)
        if tabla:
            break
    if not tabla:
        if verbose:
            print("names: kItemNames not found in the payload;"
                  " falling back to data/gi.yml, which is v32.0's")
        return None, False

    por_indice = {i + 1: n for i, n in enumerate(tabla)}
    comunes = [i for i in por_indice if i in gi and gi[i][1]]
    casan = sum(1 for i in comunes if gi[i][1] == por_indice[i])
    alineado = bool(comunes) and casan / len(comunes) >= 0.90
    if verbose:
        print(f"names: {len(por_indice)} read from the ROM's kItemNames; "
              f"gi.yml agrees on {casan}/{len(comunes)}")
        if not alineado:
            print("  WARNING: data/gi.yml does not line up with this ROM.")
            print("  The names are the ROM's and are right; the symbolic ids")
            print("  (item_id) are dropped, because those do come from the file.")
    return por_indice, alineado


def resolve(rom_bytes, checks, gi_path=None, verbose=True):
    """Fill in `item` and `item_id` on every check that shows up in the table.

    Returns (resolved, no_key, not_found). Checks that do not show up are left
    alone: they end up without `item`, and whoever consumes them can tell that
    it is unknown rather than empty.
    """
    tabla = read_tables(rom_bytes)
    gi = load_gi(gi_path)
    nombres, alineado = names_from_rom(rom_bytes, gi, verbose)
    resueltos = sin_clave = no_estan = 0
    for c in checks:
        key = override_key(
            c["type"], c.get("scene_id"), c.get("csv_id"), c.get("xflag"))
        if key is None:
            sin_clave += 1
            continue
        c["ovkey"] = key
        hit = tabla.get((c["game"], key))
        if hit is None:
            no_estan += 1
            continue
        idx, player = hit
        simbolo, nombre = gi.get(idx, (None, None))
        if nombres is not None:
            nombre = nombres.get(idx)
        c["gi"] = idx
        if alineado or nombres is None:
            c["item_id"] = simbolo
        c["item"] = nombre
        if player and player != 1:
            c["player"] = player   # multiworld: the item belongs to someone else
        resueltos += 1
    return resueltos, sin_clave, no_estan
