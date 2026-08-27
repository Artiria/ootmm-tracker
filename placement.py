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

**Why this beats depending on the spoiler**: there is no file to find, load or
validate — it comes out of the ROM you are already playing.

This used to claim the address was a structural constant as well, unlike the
xflag tables' VROMs. Gen 943 (master, the release after v32.3) ended that: it
merged the two files into one at `0xF0400000` and marks an MM key with bit 31.
The tables are found by their shape now (`locate_tables`) and the addresses are
what the finding is checked against.

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

import collections
import pathlib
import re
import struct

import paths
import rom

DATA = pathlib.Path(paths.res("data"))

# combo/defs.h. Where the generator wrote these two files up to v32.3. They are
# the CONTRAST for what locate_tables() finds and the net under it, not the way
# in: on gen 943 -- master, the release after v32.3 -- the second one stopped
# existing, and asking for it by address raised a KeyError nobody caught.
VROM_CHECKS = {"oot": 0xF0400000, "mm": 0xF0500000}

# Sixteen 0xFF bytes end a table and the game stops there (overrideData in
# item.c).
KEY_END = 0xFFFFFFFF

# From gen 943 the two games share ONE table and an MM key carries this bit.
# It is stripped on the way out, so a key is the same number whichever layout
# the ROM uses and override_key() never has to know which one it is.
KEY_MM = 0x80000000

# Below this many records it is not a placement table. The smallest real one
# measured here has 381 rows; a few words that happen to ascend are noise.
MIN_RECORDS = 64

# The generator writes one file per world with that world's settings, and up
# to v32.3 its first byte is the world number. Gen 943 moved it (the payload
# carries an `sWorldId` byte instead), so like every other address here it is
# checked before it is believed. See world_of_rom().
VROM_CONFIG = 0xF0200000

# A world number above this is not one; it is whatever else got read. OoTMM
# generates far fewer, but the point is only to rule out garbage.
MAX_WORLD = 64

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

# The override types as they appear in a key's top byte, for telling a
# placement table from anything else in the extra DMA.
_TIPOS_OV = set(OV.values())


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


def _shape(blob):
    """(records, mm rows, override types) if `blob` starts a placement table.

    None when it does not. What identifies one without being told where it
    lives, and none of it an address:

      * 16-byte records, keys **strictly ascending** -- the game binary-searches
        them (`overrideData` in item.c), so a table out of order would not work;
      * a known override type in the top byte: one of OV, or an xflag slice
        from OV_XFLAG0 up, either of them with KEY_MM on top;
      * at least one row of a plain OV type, which every seed has (chests) and
        a run of ascending garbage has no reason to;
      * KEY_END to close it.
    """
    n = mm = 0
    prev = -1
    tipos = set()
    cerrada = False
    for n in range(len(blob) // 16):
        key = struct.unpack_from(">I", blob, n * 16)[0]
        if key == KEY_END:
            cerrada = True
            break
        tipo = (key & ~KEY_MM) >> 24
        if tipo not in _TIPOS_OV and tipo < OV_XFLAG0:
            return None
        if key <= prev:
            return None
        prev = key
        tipos.add(tipo)
        mm += bool(key & KEY_MM)
    if not cerrada or n < MIN_RECORDS or not (tipos & _TIPOS_OV):
        return None
    return n, mm, tipos


def locate_tables(rom_bytes, verbose=False):
    """[(game, blob)] of the placement tables, found by shape.

    `game` is None for a table that holds both, which is what gen 943 made of
    them: one file, and KEY_MM on every MM key. Before that there were two
    files, and which is which is read off what they carry -- MM is the one with
    stray fairies, OoT the one with gold skulltulas -- and not off the order,
    because the order is exactly what a version is free to change.

    The constants are the net: if the shape hunt comes back empty -- a build
    that compressed these files would do it, since extra_entries only walks the
    raw ones -- they are tried, and the fallback is announced.
    """
    hallados = []
    for vs, _ve, blob in rom.extra_entries(rom_bytes):
        forma = _shape(blob)
        if forma:
            hallados.append((vs, blob, forma))
    hallados.sort(key=lambda h: h[0])

    tablas = []
    mezcladas = [h for h in hallados if 0 < h[2][1] < h[2][0]]
    if mezcladas:
        tablas = [(None, blob) for _vs, blob, _f in mezcladas]
    elif hallados:
        # Separate files, so each has to be named. Three rules, in this order,
        # and only one of them is an address:
        #
        #   1. what is inside. A stray fairy override is MM's alone and a gold
        #      skulltula one is OoT's, so a single row of either settles it.
        #   2. where it sits, if that is where defs.h put that game's table.
        #      The constant as a hint, which is all it is good for.
        #   3. the order, lowest VROM first -- and only with two of them, so a
        #      lone unmarked table is left unread instead of guessed at.
        pendientes = list(hallados)

        def toma(juego, cual):
            tablas.append((juego, cual[1]))
            pendientes.remove(cual)

        for juego, marca in (("mm", OV["sf"]), ("oot", OV["gs"])):
            marcadas = [h for h in pendientes if marca in h[2][2]]
            if len(marcadas) == 1:
                toma(juego, marcadas[0])
        for juego in ("oot", "mm"):
            if juego in [j for j, _ in tablas]:
                continue
            en_sitio = [h for h in pendientes if h[0] == VROM_CHECKS[juego]]
            if len(en_sitio) == 1:
                toma(juego, en_sitio[0])
        if len(hallados) > 1:
            for juego in ("oot", "mm"):
                if juego in [j for j, _ in tablas] or not pendientes:
                    continue
                toma(juego, pendientes[0] if juego == "oot" else pendientes[-1])

    if tablas:
        if verbose:
            _di_donde(hallados, tablas)
        return tablas

    # Nothing had the shape. Fall back to the addresses and say so: silence
    # here is how a build that moved them would go unnoticed.
    for juego, vrom in VROM_CHECKS.items():
        try:
            tablas.append((juego, rom.read_extra_vrom(rom_bytes, vrom)))
        except (KeyError, ValueError, IndexError, struct.error):
            continue
    if verbose:
        print("placement: no table in this ROM has the shape of one;"
              f" falling back to the defs.h addresses ({len(tablas)} of 2 are there)")
    return tablas


def _di_donde(hallados, tablas):
    """Where the tables turned up -- in full only when it is not where defs.h says.

    A seed of one game only carries one table, the other being an empty file,
    and that is not news: what counts as news is a table somewhere the constants
    do not name, or one holding both games.
    """
    donde = {vs: forma for vs, _b, forma in hallados}
    if all(j for j, _ in tablas) and set(donde) <= set(VROM_CHECKS.values()):
        cuantas = "both tables" if len(tablas) == 2 else "the one table this seed has"
        print(f"placement: {cuantas} located by shape,"
              " and where defs.h v32.0 says")
        return
    print("placement: tables located by shape, and they are NOT where defs.h v32.0 says:")
    for vs, (n, mm, _tipos) in sorted(donde.items()):
        cual = f"both games, {mm} MM rows of {n}" if mm else f"{n} rows"
        print(f"   {vs:#010x}: {cual}")
    print(f"   defs.h v32.0 has two, oot {VROM_CHECKS['oot']:#010x} and"
          f" mm {VROM_CHECKS['mm']:#010x}")


def read_tables(rom_bytes, verbose=False):
    """{(game, key): (gi, player)} with the placement of both games.

    The key is stored without KEY_MM, so it is the number override_key() builds
    in every version and the game travels in the tuple, the way it always did.

    Empty -- and a line saying why -- when the tables cannot be read at all: a
    layout this does not recognise must not take the whole build down with it,
    because checks.json is still worth writing without each spot's item.
    """
    out = {}
    try:
        tablas = locate_tables(rom_bytes, verbose)
    except Exception as ex:
        print(f"placement: the tables could not be read ({type(ex).__name__}: {ex})")
        return out
    for juego, blob in tablas:
        for i in range(len(blob) // 16):
            key, player, value, _cloak = struct.unpack_from(">IhHh", blob, i * 16)
            if key >> 24 == 0xFF:
                continue          # end-of-table sentinel
            g = juego or ("mm" if key & KEY_MM else "oot")
            out[(g, key & ~KEY_MM)] = (value, player)
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


def world_of_rom(rom_bytes, tabla=None, por_mundo=None, verbose=False):
    """(world, how): which world of a multiworld this ROM plays.

    It decides one line and that line was wrong. An override carries the
    PLAYER its item belongs to, and anything that was not player 1 used to be
    called somebody else's — so on the second world's ROM your own items are
    labelled as another world's and your partner's arrive with no label at
    all, which is exactly backwards. The panel then says it with a straight
    face, which is the worst kind of wrong here.

    Three signals, and the answer says which one spoke:

      * **config** — the ROM's own word for it, `VROM_CONFIG`'s first byte.
        Exact up to v32.3, and gen 943 moved it, so it counts only if it is a
        plausible world AND one that actually owns items in this ROM.
      * **spoiler** — the world whose section of the Location List agrees with
        what the ROM placed. No address at all, so it survives any version.
        The caller counts the agreement and passes `por_mundo` as
        {world: (agree, comparable)}: only it can join a location name to an
        override key.
      * **majority** — most of the items in your world are yours. True with
        room to spare on the seeds measured here (778 of 967 on a fresh
        multi), but only 1656 of 3002 on a real one, so it never decides
        alone: it confirms, or it answers when nothing else can and says that
        it is a guess.

    None when nothing can say, and then the caller is on its own — but at
    least it knows it is.
    """
    duenos = collections.Counter(p for _, p in (tabla or {}).values() if p)
    presentes = set(duenos)
    mayoria = duenos.most_common(1)[0][0] if duenos else None

    config = None
    try:
        b = rom.read_extra_vrom(rom_bytes, VROM_CONFIG, 1)[0]
        if 1 <= b <= MAX_WORLD and (not presentes or b in presentes):
            config = b
    except (KeyError, ValueError, IndexError, struct.error):
        pass

    # A clear winner among the spoiler's worlds, not just the best of them:
    # two worlds of the same seed share every filler item, so a thin lead is
    # noise. A quarter of the comparable spots is not.
    spoiler = None
    if por_mundo:
        marcador = sorted(((a / c, w) for w, (a, c) in por_mundo.items() if c),
                          reverse=True)
        if marcador and (len(marcador) == 1 or marcador[0][0] - marcador[1][0] >= 0.25):
            spoiler = marcador[0][1]

    elegido = config or spoiler or mayoria
    como = ("the ROM's own config" if config else
            "the spoiler's world sections" if spoiler else
            "a guess from who owns most of the items" if mayoria else None)
    if elegido is None:
        if verbose:
            print("world: this ROM does not say which world it plays, and nothing"
                  " else could tell; assuming the first, as before")
        return None, None

    if verbose and (len(presentes) > 1 or config != elegido or spoiler):
        print(f"world: this ROM plays world {elegido}, by {como}")
        for etiqueta, valor in (("config", config), ("spoiler", spoiler),
                                ("majority", mayoria)):
            if valor is not None and valor != elegido:
                print(f"   NOTE: {etiqueta} says {valor} instead; going with {elegido}")
    return elegido, como


def resolve(rom_bytes, checks, gi_path=None, verbose=True, spoiler_worlds=None):
    """Fill in `item` and `item_id` on every check that shows up in the table.

    Returns (resolved, no_key, not_found, names_aligned). Checks that do not
    show up are left alone: they end up without `item`, and whoever consumes
    them can tell that it is unknown rather than empty.

    `names_aligned` is False when `gi.yml` no longer matches this ROM, and that
    is worth carrying upwards: it means the seed comes from a different OoTMM
    version than the bundled `data/`, so the pool CSVs may not line up either
    and the check names —which do come from those CSVs— can be wrong.
    """
    tabla = read_tables(rom_bytes, verbose)
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
        resueltos += 1

    # Which world this is has to be settled before any item can be called
    # somebody else's, so the labels go on in a second pass. The spoiler's
    # worlds are scored here because this is where a location name and an
    # override key are both in hand.
    mundo, _como = world_of_rom(
        rom_bytes, tabla, _score_worlds(checks, spoiler_worlds), verbose)
    for c in checks:
        hit = tabla.get((c["game"], c["ovkey"])) if "ovkey" in c else None
        player = hit[1] if hit else None
        if player and player != (mundo or 1):
            c["player"] = player   # multiworld: the item belongs to someone else
    return resueltos, sin_clave, no_estan, alineado, mundo


# What follows tells "the same item, spelled another way" from "another item".
# It grew in forge's guard, comparing every ROM against its spoiler, and it
# lives here because two things need the very same judgement: whether a spoiler
# is this seed's at all (Tracker._vet_spoiler) and which world a ROM is
# (_score_worlds). Two copies would answer differently the first time one of
# them learnt a new spelling -- and on a real seed this is not a detail: 390 of
# 3002 spots disagreed by wording alone, which read as 87% agreement with the
# seed's OWN spoiler.
# The spellings a spoiler and kItemNames are known to differ on. Only for
# seeds without the version's data; with it the comparison needs no guessing.
FUZZY_ALIASES = {
    "milk": "lon lon milk",
    "romani milk": "milk",
    "bottle of milk": "bottle of lon lon milk",
    "gold rupee": "huge rupee",
    "double defense": "defense upgrade",
    "bow": "hero's bow",
    "ocarina": "ocarina of time",
    "gerudo's membership card": "gerudo's card",
    "world map of ranch": "world map (romani ranch)",
}
PROGRESSIVE = {
    "strength": ("bracelet", "gauntlet"),
    "sword": ("sword", "knife"),
    "goron sword": ("knife", "biggoron", "sword"),
    "hookshot": ("hookshot", "longshot"),
    "scale": ("scale",),
    "wallet": ("wallet",),
    "ocarina": ("ocarina",),
    "bow": ("bow",),
}
# a "Shared X" (after "shared " is stripped) is whichever game's weapon the
# location holds; accept the ROM name if it carries any of these words
SHARED_FAMILIES = {
    "bow": ("bow",),
    "bomb bag": ("bomb bag",),
    "magic upgrade": ("magic",),
}


def _tokens(s):
    """Words that survive apostrophes, hyphens, parentheses and 'of': the
    spoiler's "World Map of Clock Town" is the ROM's "World Map (Clock Town)"."""
    s = s.replace("'", "").replace("-", " ").replace("(", " ").replace(")", " ")
    return [t.rstrip("s") for t in re.sub(r"[^a-z0-9 /]", "", s).split() if t != "of"]


def same_item(spoiler_name, rom_name):
    """Whether the spoiler's item and the one the tracker read from the ROM are
    the same thing. Both come from OoTMM's own naming, but by different paths:
    the spoiler tags the game (`(OoT)`/`(MM)`), spells a trap by its cloak
    (`Ice Trap (cloaked as ...)`), writes `Bottle of X` and the shared/split
    variants differently. None of those are placement errors, so they are
    normalised away; a genuinely different item still shows."""
    # a trap is the item; the cloak it wears is cosmetic, strip it first
    spoiler_name = re.sub(r"\s*\(cloaked as .*\)\s*$", "", spoiler_name)
    a, b = bare_item(spoiler_name), bare_item(rom_name)
    # bare_item only strips an exact "(OoT)"/"(OOT)"/"(MM)"; older spoilers
    # write "(Oot)", so drop any game tag case-insensitively too
    a = re.sub(r"\s*\((?:oot|mm)\)$", "", a)
    b = re.sub(r"\s*\((?:oot|mm)\)$", "", b)
    if a == b:
        return True
    # Souls (soul shuffle): the spoiler and the ROM name the same soul by
    # different NPCs, often a "/"-list ("Soul of Malon/Romani/Cremia" vs
    # "Soul of Romani/Cremia"). Same soul if the NPC token sets overlap.
    ms = re.match(r"^soul?d? of (?:the )?(.*)", a)
    mr = re.match(r"^soul?d? of (?:the )?(.*)", b)
    if ms and mr:
        ta = {t for part in ms.group(1).split("/") for t in _tokens(part)}
        tb = {t for part in mr.group(1).split("/") for t in _tokens(part)}
        return bool(ta & tb)
    # Silver rupees vs silver-rupee pouches are different items, but each is
    # named with a full dungeon in the spoiler and an abbreviation in the ROM;
    # the group is not comparable, so match on rupee-vs-pouch alone.
    if a.startswith("silver") and b.startswith("silver"):
        return ("pouch" in a) == ("pouch" in b)
    a = re.sub(r"^shared ", "", a)
    a = re.sub(r"^1 ", "", a)
    a = re.sub(r"^bottle of ", "", a)
    b = re.sub(r"^bottle of ", "", b)
    a = re.sub(r" refill$", "", a)
    b = re.sub(r" refill$", "", b)
    # a "Shared X" weapon is whichever game's version the location holds: the
    # shared bow is OoT's Fairy Bow or MM's Hero's Bow
    if a in SHARED_FAMILIES and any(w in b for w in SHARED_FAMILIES[a]):
        return True
    a = FUZZY_ALIASES.get(a, a)
    # "Hylian/Hero Shield": either
    if "/" in a and "(" not in a:
        head, _, tail = a.partition(" ")
        if any(_tokens(f"{alt} {tail}") == _tokens(b) for alt in head.split("/")):
            return True
    # "Small Key (Fire Temple)" vs "Small Key"; "Silver Rupee (Shadow Temple -
    # Scythe)" vs "Silver Rupee (Shadow Scythe)": the group is not compared
    base_a, paren_a = re.match(r"^([^(]*?)\s*(\(.*\))?$", a).groups()
    base_b, paren_b = re.match(r"^([^(]*?)\s*(\(.*\))?$", b).groups()
    if paren_a and base_a == "map":     # "Map (Deku Tree)" is the ROM's "Dungeon Map"
        base_a = "dungeon map"
    if paren_a and _tokens(base_a) == _tokens(base_b):
        return True
    if _tokens(a) == _tokens(b):
        return True
    if a.startswith("progressive "):
        fam = a[len("progressive "):]
        words = PROGRESSIVE.get(fam, (fam.split()[-1],))
        return any(w in b for w in words)
    return False


def _score_worlds(checks, spoiler_worlds):
    """{world: (agree, comparable)} of each spoiler world against the ROM.

    The one that matches is the world this ROM plays: a spoiler names every
    world's placement and only one of them is the one in your hands. Items are
    compared bare, the way _vet_spoiler does, because the spoiler writes the
    owner in front of the name and the ROM keeps it in its own field.
    """
    if not spoiler_worlds:
        return None
    de_rom = {(c["game"], c["name"]): c["item"] for c in checks if c.get("item")}
    out = {}
    for mundo, sitios in spoiler_worlds.items():
        acuerdo = comparables = 0
        for clave, item in sitios.items():
            mio = de_rom.get(clave)
            if mio is None:
                continue
            comparables += 1
            nombre = item[0] if isinstance(item, tuple) else item
            acuerdo += same_item(nombre, mio)
        out[mundo] = (acuerdo, comparables)
    return out


def bare_item(name):
    """An item name reduced to what the ROM and a spoiler can agree on.

    Two names for the same item differ in ways that are not disagreements: a
    multiworld spoiler writes `Player N ` in front, and a spoiler tags which
    game an item belongs to with a ` (OoT)` / ` (MM)` suffix that the ROM's own
    `kItemNames` does not carry. Left in, that suffix alone dropped a seed's
    agreement with its OWN spoiler from 86% to 21% and got it wrongly refused
    (24 ago 2026). Only the game tag is stripped, never a real parenthesis
    like `Rupee (5)`.

    It lives here rather than in the overlay because two things depend on it
    agreeing with itself: which world a ROM is (_score_worlds) and whether a
    spoiler is this seed's at all (Tracker._vet_spoiler). Two copies of this
    would decide those two with different rules the first time one changed.
    """
    s = re.sub(r"^Player \d+ ", "", name or "")
    s = re.sub(r"\s*\((?:OoT|OOT|MM)\)\s*$", "", s)
    return s.strip().lower()
