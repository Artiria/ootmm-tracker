"""OoTMM v33's actor-to-bit dictionary, read from COMBO_VROM_XFLAGS.

Unlike the older three-level tables, this file maps actor identities (and
additional drops of the same actor) to shared, 16-bit xflag IDs. The IDs in
the ROM are authoritative; XML ordering is never used to assign bits.
"""
import collections
import struct

import rom

VROM = 0xF0500000
SIBLING = 0x40000000
MM = 0x80000000


def match_key(game, identity):
    return ((MM if game == "mm" else 0) | (identity["scene"] << 18)
            | (identity.get("setup", 0) << 16)
            | (identity.get("room", 0) << 8) | identity.get("actor", 0))


class Index:
    def __init__(self, blob):
        if not blob or len(blob) % 8:
            raise ValueError("xflag dictionary must contain 8-byte records")
        self.matches = {}
        previous = -1
        for key, ident, zero in struct.iter_unpack(">IHH", blob):
            if key <= previous or zero or ident == 0xffff:
                raise ValueError("invalid xflag dictionary record")
            # Actor keys have no bits 26..30; sibling keys use bits 0..21.
            if key & SIBLING:
                if key & ~0x403fffff or not (key >> 16) & 0x3f:
                    raise ValueError("invalid sibling xflag key")
            elif key & ~0x83ff3fff:
                raise ValueError("invalid actor xflag key")
            self.matches[key] = ident
            previous = key
        self.count = max(self.matches.values()) + 1
        self.by_id = collections.defaultdict(list)
        for key, ident in self.matches.items():
            if not key & SIBLING:
                self.by_id[ident].append({
                    "game": "mm" if key & MM else "oot",
                    "scene": (key >> 18) & 0xff,
                    "setup": (key >> 16) & 3, "room": (key >> 8) & 0x3f,
                    "actor": key & 0xff, "slice": 0,
                })
        for key, ident in self.matches.items():
            if key & SIBLING:
                bases = tuple(self.by_id.get(key & 0xffff, ()))
                if ident == (key & 0xffff) or not bases or any(b["slice"] for b in bases):
                    raise ValueError("xflag sibling has no actor base")
                self.by_id[ident].extend(dict(b, slice=(key >> 16) & 0x3f) for b in bases)

    def lookup(self, game, identity):
        ident = self.matches.get(match_key(game, identity))
        if ident is not None and identity.get("slice", 0):
            ident = self.matches.get(SIBLING | (identity["slice"] << 16) | ident)
        return ident

    def identities(self, game, ident):
        return [i for i in self.by_id.get(ident, ()) if i["game"] == game]


def read(rom_bytes):
    """None for legacy ROMs (where this VROM holds MM's placement table)."""
    try:
        return Index(rom.read_extra_vrom(rom_bytes, VROM))
    except (KeyError, ValueError, IndexError, struct.error):
        return None
