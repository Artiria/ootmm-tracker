#!/usr/bin/env python3
"""
souls.py - the seed's souls, read from the ROM: which exist, which game and
group each belongs to, which bit records it, and where that bit lives.

OoTMM's soul shuffle turns enemies, bosses, NPCs, animals and a couple of
oddities into items ("Soul of Stalfos"). Picking one up runs, in souls.c:

    param = kAddItemParams[gi - 1];   type = param >> 12;   index = param & 0xfff;
    BITMAP8_SET(gSharedCustomSave.souls<Type><Game>, index);     /* bit index */

so three things place a soul: which game's handler its item uses
(kAddItemFuncs[gi - 1]: one id for OoT souls, another for MM), its type, and
its index. All three are tables in the payload, generated next to kItemNames[]
from the same list (codegen.ts), and all three are found here **by shape**,
never by address:

  * kAddItemParams is u16[N], N = len(kItemNames). At every "Soul of" position
    the value decodes to type <= 4 and index < 64, and each (type, index)
    shows up at most twice -- once per game. Zeros do not pass, because many
    distinct pairs are demanded. One fit in every ROM of the corpus.
  * kAddItemFuncs is u8[N]. The souls take exactly two values and nothing
    else takes either. The lower one is OoT: the handler table lists the OoT
    variant first, and OoT's items come first in the list, so the first soul
    of the table carries it too -- both are read and must agree.

The type codes are positional in the code's switch, and the set of types
changes with the version: the 784 generation (Nov 2025) had no animal souls
and numbered misc as 3; from 829 on it is enemy 0, boss 1, npc 2, animals 3,
misc 4. The count of distinct codes decides which naming applies, and the
misc group has to contain the Business Scrubs to be believed.

The arrays themselves are located by payload.py (souls_block), from the
references the code makes into gSharedCustomSave; this module only decodes
their bytes.

    python souls.py ROM.z64 [ROM.z64 ...]     what each ROM says
    python souls.py --corpus DIR              every .z64 in DIR, one line each
"""

import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path

import placement
import rom

# Codes are positional; which names they take depends on how many there are.
TYPE_NAMES = {
    5: ["enemy", "boss", "npc", "animals", "misc"],
    4: ["enemy", "boss", "npc", "misc"],
}
GROUP_ORDER = ["enemy", "boss", "npc", "animals", "misc"]
GROUP_LABEL = {"enemy": "Enemy souls", "boss": "Boss souls", "npc": "NPC souls",
               "animals": "Animal souls", "misc": "Other souls"}

# The bitmaps, in struct order (save.h), with their sizes in bytes. The
# animal pair only exists where the animal type does.
ARRAYS = [("enemy", "oot", 8), ("enemy", "mm", 8), ("boss", "oot", 2), ("boss", "mm", 1),
          ("npc", "oot", 8), ("npc", "mm", 8), ("animals", "oot", 2), ("animals", "mm", 2),
          ("misc", "oot", 1), ("misc", "mm", 1)]

# A soul's bit index has to fit its bitmap; 64 covers the biggest one and is
# what separates a real table from noise.
MAX_INDEX = 64
MAX_TYPE = 4


def is_soul_name(name):
    return bool(name) and name.lower().startswith("soul of ")


def label_of(name):
    """"Soul of Stalfos" -> "Stalfos": inside a group called souls the prefix
    says nothing, and the cell is small."""
    return name[len("Soul of "):] if is_soul_name(name) else name


# --------------------------------------------------------------------------
# The three tables
# --------------------------------------------------------------------------


def _names_run(rom_bytes, game):
    """(payload blob, offset of kItemNames, names) using placement's search."""
    vrom, ram = placement.PAYLOAD[game]
    blob = rom.read_extra_vrom(rom_bytes, vrom)
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
            if i - start >= 64:
                cand = placement._score_run(blob, words, start, i - start, ram, game)
                if cand and (best is None or len(cand) > len(best[1])):
                    best = (start, cand)
            start = None
    if best is None:
        return None
    return blob, best[0] * 4, best[1]


def _find_params(blob, n, souls):
    """Offsets of every u16[N] whose soul positions decode as soul params."""
    hits = []
    for po in range(0, len(blob) - 2 * n, 2):
        pairs = Counter()
        ok = True
        for i in souls:
            v = (blob[po + 2 * i] << 8) | blob[po + 2 * i + 1]
            t, ix = v >> 12, v & 0xFFF
            if t > MAX_TYPE or ix >= MAX_INDEX:
                ok = False
                break
            pairs[(t, ix)] += 1
        if ok and len(pairs) >= len(souls) // 2 and max(pairs.values()) <= 2:
            hits.append(po)
    return hits


def _find_funcs(blob, n, souls):
    """Offsets of every u8[N] where the souls use exactly two ids and no other
    position uses either. Returns [(offset, [id_a, id_b])]."""
    souls_set = set(souls)
    hits = []
    for fo in range(0, len(blob) - n):
        s = {blob[fo + i] for i in souls}
        if len(s) != 2:
            continue
        for i in range(n):
            if blob[fo + i] in s and i not in souls_set:
                break
        else:
            hits.append((fo, sorted(s)))
    return hits


def find_tables(rom_bytes, game="oot"):
    """The three tables and what they say about every soul, or None.

    {
      "names_off", "params_off", "funcs_off", "n",
      "func_oot", "func_mm",           # the two handler ids
      "types": [codes present],
      "type_names": {code: name} or None when the code count is unknown,
      "souls": [{"gi", "pos", "name", "game", "tcode", "index"}],
      "why": reason when something is missing
    }
    Only kItemNames is looked up by content the way placement.py does; the
    other two are demanded to fit uniquely, or the answer is None.
    """
    got = _names_run(rom_bytes, game)
    if not got:
        return None
    blob, names_off, names = got
    n = len(names)
    souls = [i for i, nm in enumerate(names) if is_soul_name(nm)]
    if len(souls) < 16:
        return {"why": f"only {len(souls)} soul names in kItemNames", "n": n, "names_off": names_off}
    params = _find_params(blob, n, souls)
    if len(params) != 1:
        return {"why": f"kAddItemParams: {len(params)} fits", "n": n, "names_off": names_off}
    funcs = _find_funcs(blob, n, souls)
    if len(funcs) != 1:
        return {"why": f"kAddItemFuncs: {len(funcs)} fits", "n": n, "names_off": names_off}
    po, (fo, fids) = params[0], funcs[0]
    vals = struct.unpack_from(f">{n}H", blob, po)
    fbytes = blob[fo:fo + n]

    # Which id is OoT: the lower one, and the one on the first soul. Both or
    # nothing.
    first = fbytes[souls[0]]
    if first != fids[0]:
        return {"why": f"handler ids {fids}: the lower is not the first soul's ({first})",
                "n": n, "names_off": names_off, "params_off": po, "funcs_off": fo}
    func_oot, func_mm = fids

    out_souls = []
    for i in souls:
        v = vals[i]
        out_souls.append({
            "gi": i + 1, "pos": i, "name": names[i],
            "game": "oot" if fbytes[i] == func_oot else "mm",
            "tcode": v >> 12, "index": v & 0xFFF,
        })
    types = sorted({s["tcode"] for s in out_souls})
    type_names = None
    names_for = TYPE_NAMES.get(len(types))
    if names_for and types == list(range(len(types))):
        type_names = dict(enumerate(names_for))
        # the misc group is the anchor of the naming: it holds the scrubs
        misc = [s["name"] for s in out_souls if type_names[s["tcode"]] == "misc"]
        if not any("business scrub" in m.lower() for m in misc):
            type_names = None
    return {
        "names_off": names_off, "params_off": po, "funcs_off": fo, "n": n,
        "func_oot": func_oot, "func_mm": func_mm,
        "types": types, "type_names": type_names, "souls": out_souls,
        "why": None if type_names else f"cannot name the type codes {types}",
    }


def catalogue(tables):
    """[{"gi", "game", "type", "index", "name", "label"}] sorted by game, group
    order and index; [] when the tables cannot be named."""
    tn = tables.get("type_names") if tables else None
    if not tn:
        return []
    order = {g: i for i, g in enumerate(GROUP_ORDER)}
    out = [{"gi": s["gi"], "game": s["game"], "type": tn[s["tcode"]], "index": s["index"],
            "name": s["name"], "label": label_of(s["name"])}
           for s in tables["souls"]]
    out.sort(key=lambda s: (s["game"] != "oot", order[s["type"]], s["index"]))
    return out


def arrays_for(type_names):
    """The bitmaps this version has, in struct order, with sizes."""
    have = set(type_names.values()) if type_names else set()
    return [(t, g, sz) for t, g, sz in ARRAYS if t in have]


# --------------------------------------------------------------------------
# What mkchecks writes
# --------------------------------------------------------------------------


def build(rom_bytes, checks, located=None, verbose=True):
    """The `souls` block of checks.json, or None when the ROM gives nothing.

    `checks` are mkchecks' rows after placement.resolve(): a soul is in the
    seed when some row carries its gi. `located` is payload.locate()'s answer,
    used to place the block; without it the catalogue still comes out and the
    overlay says the block is missing.
    """
    import payload

    tables = find_tables(rom_bytes)
    if not tables or tables.get("why"):
        if verbose:
            print(f"souls: {(tables or {}).get('why') or 'kItemNames not found'}; no souls table")
        return None
    cat = catalogue(tables)
    placed = defaultdict(Counter)
    gis = Counter(c.get("gi") for c in checks if c.get("gi") is not None)
    for s in cat:
        s["in_seed"] = gis.get(s["gi"], 0) > 0
        if s["in_seed"]:
            placed[s["game"]][s["type"]] += 1

    block = None
    if located and "oot" in located and "mm" in located and "layout" in located \
            and "custom" in located["oot"] and "custom" in located["mm"]:
        try:
            block = payload.souls_block(
                rom_bytes, located, arrays_for(tables["type_names"]))
        except Exception as ex:  # a scan bug must not take mkchecks down
            block = {"why": f"{type(ex).__name__}: {ex}"}
    if verbose:
        n_placed = sum(sum(v.values()) for v in placed.values())
        groups = sum(len(v) for v in placed.values())
        print(f"souls: {len(cat)} in the ROM's tables (kAddItemParams +{tables['params_off']:#x},"
              f" kAddItemFuncs +{tables['funcs_off']:#x}), {n_placed} placed in this seed"
              f" in {groups} groups")
        if block and block.get("arrays"):
            first = min(block["arrays"].values())
            print(f"souls: bitmaps at custom+{first:#x}..{block['end']:#x} ({block['by']})")
        else:
            print(f"souls: bitmaps not located in the custom save"
                  f" ({(block or {}).get('why') or 'payload not located'}); the panel stays off")
    return {
        "tables": {"names": tables["names_off"], "params": tables["params_off"],
                   "funcs": tables["funcs_off"], "n": tables["n"],
                   "func_oot": tables["func_oot"], "func_mm": tables["func_mm"]},
        "types": [tables["type_names"][t] for t in tables["types"]],
        "block": block,
        "catalogue": cat,
        "placed": {g: dict(v) for g, v in placed.items()},
    }


# --------------------------------------------------------------------------
# What the overlay reads
# --------------------------------------------------------------------------


class Decoder:
    """Turns the bytes of gSharedCustomSave into the souls state.

    Built from checks.json's `souls` block. `.end` is where the last bitmap
    ends, so the overlay can make sure it reads that far; `.state(blob)` is
    what goes to /state.json. Groups the seed did not place stay out of the
    grid: a seed without NPC souls has 99 unlit chips nobody asked for.
    """

    COLS = 8

    def __init__(self, table_block):
        self.ok = False
        self.why = None
        self.catalogue = []
        self.arrays = {}
        self.end = 0
        if not table_block:
            self.why = "no souls table for this seed"
            return
        self.catalogue = table_block.get("catalogue") or []
        block = table_block.get("block") or {}
        self.arrays = block.get("arrays") or {}
        if not self.catalogue:
            self.why = "the ROM's soul tables could not be read"
            return
        if not self.arrays:
            self.why = block.get("why") or "the soul bitmaps were not located in the custom save"
            return
        self.end = block.get("end") or (max(self.arrays.values()) + 8)
        self.ok = True
        # (game, type) -> ordered souls, only groups the seed placed
        self.groups = defaultdict(list)
        for s in self.catalogue:
            if s.get("in_seed"):
                self.groups[(s["game"], s["type"])].append(s)
        self.total = sum(len(v) for v in self.groups.values())

    @classmethod
    def from_table(cls, table):
        return cls((table or {}).get("souls"))

    def has(self, blob, soul):
        off = self.arrays.get(f"{soul['type']}_{soul['game']}")
        if off is None:
            return False
        byte = off + (soul["index"] >> 3)
        if byte >= len(blob):
            return False
        return bool(blob[byte] & (1 << (soul["index"] & 7)))

    def state(self, blob, trusted=True):
        """{"ok", "why", "have", "total", "grid": {game: [group, ...]}}.

        The grid uses the item grid's cell shape (see overlay.item_grid), so
        the page draws it with the same code: label, on, and the rest empty --
        the ROM has no per-soul icon, souls are drawn as text chips.
        """
        if not self.ok:
            return {"ok": False, "why": self.why, "have": 0, "total": 0, "grid": {}}
        if blob is None or not trusted:
            return {"ok": True, "why": "custom save not readable yet", "have": 0,
                    "total": self.total, "grid": {}}
        grid = {}
        have = 0
        by_game = {}
        for game in ("oot", "mm"):
            groups = []
            g_have = g_total = 0
            for t in GROUP_ORDER:
                souls = self.groups.get((game, t))
                if not souls:
                    continue
                cells = []
                lit = 0
                for s in souls:
                    on = self.has(blob, s)
                    lit += on
                    cells.append({
                        "label": s["label"], "on": on, "icon": None, "img": None,
                        "glyph": None, "mask": None, "color": None,
                        "badge": "", "value": "yes" if on else "no",
                    })
                groups.append({"name": f"{GROUP_LABEL[t]} {lit}/{len(souls)}",
                               "kind": t, "cols": self.COLS, "items": cells,
                               "have": lit, "total": len(souls)})
                g_have += lit
                g_total += len(souls)
            if groups:
                grid[game] = groups
                by_game[game] = {"have": g_have, "total": g_total}
                have += g_have
        return {"ok": True, "why": None, "have": have, "total": self.total,
                "by_game": by_game, "grid": grid}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _describe(path):
    rom_bytes = Path(path).read_bytes()
    t = find_tables(rom_bytes)
    if not t:
        return f"{Path(path).name}: kItemNames not found"
    if t.get("why") and not t.get("type_names"):
        return f"{Path(path).name}: N={t.get('n')} {t['why']}"
    per = Counter((s["game"], t["type_names"][s["tcode"]]) for s in t["souls"])
    desc = " ".join(f"{g}:{'/'.join(str(per[(g, ty)]) for ty in GROUP_ORDER if (g, ty) in per)}"
                    for g in ("oot", "mm"))
    line = (f"{Path(path).name}: N={t['n']} souls={len(t['souls'])} types={t['types']} "
            f"params=+{t['params_off']:#x} funcs=+{t['funcs_off']:#x} ids={t['func_oot']}/{t['func_mm']} "
            f"{desc}")
    try:
        import payload
        loc = payload.locate(rom_bytes)
        blk = payload.souls_block(rom_bytes, loc, arrays_for(t["type_names"]))
        if blk and blk.get("arrays"):
            first = min(blk["arrays"].values())
            line += f" block=+{first:#x}..{blk['end']:#x} ({blk['by']})"
        else:
            line += f" block=NONE ({(blk or {}).get('why')})"
    except Exception as ex:
        line += f" block=ERROR {type(ex).__name__}: {ex}"
    return line


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 1
    paths = []
    if argv[0] == "--corpus":
        paths = sorted(Path(argv[1]).glob("*.z64"))
    else:
        paths = [Path(a) for a in argv]
    for p in paths:
        try:
            print(_describe(p))
        except Exception as ex:
            print(f"{p.name}: ERROR {type(ex).__name__}: {ex}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
