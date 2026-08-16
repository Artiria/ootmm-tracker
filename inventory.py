"""
inventory.py - map of the OoT inventory inside the save context.

Offsets taken from packages/generator/include/combo/oot/save.h in the OoTMM
repo, and validated against the game's RAM at three independent points:

    questItems  +0xA4   bit 6 lit up on picking up the Minuet of Forest
    health      +0x30   went from 44 to 48 on picking up a Recovery Heart
    perm        +0xD4   the per-scene flag table, verified with chests

The struct closes exactly at 0xD4, which is where `perm` begins, so the whole
block is consistent from end to end.

The bitfields read as MIPS big endian: the first field declared in the struct
takes the highest bits of the word.
"""

# --- scalar fields: name -> (offset, size, signed) ---
OOT_SCALARS = [
    ("rupees", 0x34, 2, True),
    ("hearts", 0x30, 2, True),
    ("max hearts", 0x2E, 2, True),
    ("magic level", 0x32, 1, True),
    ("magic", 0x33, 1, True),
    ("deaths", 0x22, 2, True),
    ("skulltulas", 0xD0, 2, False),
    ("beans", 0x9B, 1, False),
    ("double defense", 0x3D, 1, False),
    # Hunted live: it showed up as 8 on picking up the Giant's Knife. It is
    # its durability, and it counts down until the sword breaks.
    ("sword durability", 0x36, 2, False),
]

# --- questItems: u32 at +0xA4, one bit per thing ---
OOT_QUEST_ADDR = 0xA4
OOT_QUEST_BITS = {
    0: "Forest Medallion",
    1: "Fire Medallion",
    2: "Water Medallion",
    3: "Spirit Medallion",
    4: "Shadow Medallion",
    5: "Light Medallion",
    6: "Minuet of Forest",
    7: "Bolero of Fire",
    8: "Serenade of Water",
    9: "Requiem of Spirit",
    10: "Nocturne of Shadow",
    11: "Prelude of Light",
    12: "Zelda's Lullaby",
    13: "Epona's Song",
    14: "Saria's Song",
    15: "Sun's Song",
    16: "Song of Time",
    17: "Song of Storms",
    18: "Kokiri's Emerald",
    19: "Goron's Ruby",
    20: "Zora's Sapphire",
    21: "Stone of Agony",
    22: "Gerudo Card",
    23: "Gold Skulltula Token",
}

# --- equipment: u16 at +0x9C, four nibbles ---
# boots is declared first in the struct, so it takes the high bits.
OOT_EQUIPMENT_ADDR = 0x9C
OOT_EQUIPMENT = [
    ("swords", 0, 4),
    ("shields", 4, 4),
    ("tunics", 8, 4),
    ("boots", 12, 4),
]

# Bit-by-bit breakdown of each nibble. Verified live:
#   swords bit 0 -> the nibble went from 4 to 5 on getting the Kokiri Sword
#   swords bit 2 -> the nibble went from 0 to 4 on getting the Giant's Knife
#   boots  bit 2 -> went from 1 to 5 on getting the Hover Boots, with bit 0
#                   (Kokiri) already set
# The rest follow OoT's standard order, consistent with those two.
OOT_EQUIP_BITS = {
    "swords": ["Kokiri Sword", "Master Sword", "Giant's Knife", "sword bit 3"],
    "shields": ["Deku Shield", "Hylian Shield", "Mirror Shield", "shield bit 3"],
    "tunics": ["Kokiri Tunic", "Goron Tunic", "Zora Tunic", "tunic bit 3"],
    "boots": ["Kokiri Boots", "Iron Boots", "Hover Boots", "boots bit 3"],
}

# --- upgrades: u32 at +0xA0 ---
# Declared unused:9, dekuNut:3, dekuStick:3, bulletBag:3, wallet:2,
# dive:3, strength:3, bombBag:3, quiver:3 -> quiver ends up in the low bits.
OOT_UPGRADES_ADDR = 0xA0
OOT_UPGRADES = [
    ("quiver", 0, 3),
    ("bomb bag", 3, 3),
    ("strength", 6, 3),
    ("scale", 9, 3),
    ("wallet", 12, 2),
    ("bullet bag", 14, 3),
    ("deku sticks", 17, 3),
    ("deku nuts", 20, 3),
]

# --- arrays ---
OOT_ITEMS_ADDR = 0x74   # items[24]: item id per slot, 0xFF = empty
OOT_ITEMS_LEN = 0x18
OOT_AMMO_ADDR = 0x8C    # ammo[15]: ammo per slot
OOT_AMMO_LEN = 0x0F
# dungeonKeys[19] at +0xBC. The arithmetic closes on its own: 0xBC + 19 = 0xCF,
# which is defenseHearts, and 0xD0 is the skulltula count, already measured in
# game. -1 (0xFF) means that dungeon has no keys or has not been entered.
OOT_DUNG_ADDR = 0xA8      # dungeonItems[20], right before the keys
OOT_DUNG_LEN = 20
OOT_KEYS_ADDR = 0xBC
OOT_KEYS_LEN = 19

# OoT inventory slots. Only 22 is verified live (Cojiro, 0x2F, showed up there
# on pickup). The rest is the game's canonical order.
# Careful: boots and shields are NOT here, they live in `equipment`.
OOT_ITEM_SLOTS = {
    0: "deku stick",
    1: "deku nut",
    2: "bomb",
    3: "bow",
    4: "fire arrow",
    5: "Din's Fire",
    6: "slingshot",
    7: "ocarina",
    8: "bombchu",
    9: "hookshot",
    10: "ice arrow",
    11: "Farore's Wind",
    12: "boomerang",
    13: "lens of truth",
    14: "magic bean",
    15: "Megaton hammer",
    16: "light arrow",
    17: "Nayru's Love",
    18: "bottle 1",
    19: "bottle 2",
    20: "bottle 3",
    21: "bottle 4",
    22: "adult trade",  # verificado: Cojiro
    23: "child trade",
}


def read(buf, base, off, size, signed=False):
    """Read a big-endian integer out of the dump."""
    i = off if base is None else off
    v = int.from_bytes(buf[i : i + size], "big", signed=signed)
    return v


def snapshot(save):
    """save = the save context bytes. Returns {label: value}."""
    st = {}
    for name, off, size, signed in OOT_SCALARS:
        st[name] = read(save, None, off, size, signed)

    quest = read(save, None, OOT_QUEST_ADDR, 4)
    for bit, name in OOT_QUEST_BITS.items():
        st[f"quest:{name}"] = 1 if quest & (1 << bit) else 0

    eq = read(save, None, OOT_EQUIPMENT_ADDR, 2)
    for name, shift, width in OOT_EQUIPMENT:
        nib = (eq >> shift) & ((1 << width) - 1)
        for bit, label in enumerate(OOT_EQUIP_BITS[name]):
            st[f"eq:{label}"] = 1 if nib & (1 << bit) else 0

    up = read(save, None, OOT_UPGRADES_ADDR, 4)
    for name, shift, width in OOT_UPGRADES:
        st[f"up:{name}"] = (up >> shift) & ((1 << width) - 1)

    for i in range(OOT_ITEMS_LEN):
        v = save[OOT_ITEMS_ADDR + i]
        label = OOT_ITEM_SLOTS.get(i, f"slot {i}")
        st[f"item:{label}"] = v

    for i in range(OOT_AMMO_LEN):
        st[f"ammo:{i}"] = save[OOT_AMMO_ADDR + i]
    for i in range(OOT_DUNG_LEN):
        # OotDungeonItems: maxKeys:5, map:1, compass:1, bossKey:1 -> bit 0
        st[f"boss:{i}"] = save[OOT_DUNG_ADDR + i] & 1
    for i in range(OOT_KEYS_LEN):
        st[f"key:{i}"] = read(save, None, OOT_KEYS_ADDR + i, 1, signed=True)

    return st


# --------------------------------------------------------------------------
# Majora's Mask. Far less mapped than OoT: for now only the fields we managed
# to anchor. The rest comes out raw, which is how they get hunted.
# --------------------------------------------------------------------------

# Measured: on picking up the Swamp Skulltula Token, byte +0xEB9 went to 1,
# i.e. the low byte of the u16 at +0xEB8. In combo/mm/save.h skullCountOcean
# justo detras de skullCountSwamp.
MM_SCALARS = [
    ("swamp skulltulas", 0xEB8, 2, False),
    ("ocean skulltulas", 0xEBA, 2, False),
]

# MmQuestItems, bit indices taken from combo/mm/save.h.
MM_QUEST_BITS = {
    0: "Odolwa's Remains",
    1: "Goht's Remains",
    2: "Gyorg's Remains",
    3: "Twinmold's Remains",
    6: "Song of Awakening",
    7: "Goron Lullaby",
    8: "New Wave Bossa Nova",
    9: "Elegy of Emptiness",
    10: "Oath to Order",
    11: "Saria's Song",
    12: "Song of Time",
    13: "Song of Healing",
    14: "Epona's Song",
    15: "Song of Soaring",
    16: "Song of Storms",
    17: "Sun's Song",
    18: "Bombers' Notebook",
    24: "Goron Lullaby (half)",
}
# MmInventory anclada a partir de dos mascaras cazadas en vivo:
#   Deku   (mascara 5,  slot 29) en +0x85
#   Romani (mascara 12, slot 36) en +0x8C
# The 7-slot difference matches MM's real mask order, and from that
# items[48] = +0x68. Also checked against a dump: slot 26 (mask 2 = Blast Mask)
# read 0x47, and the Blast Mask came out of Mido's chest early in the run.
#
# The rest falls out by subtraction from MmInventory's layout (combo/mm/save.h):
#   items[48] ammo[24] upgrades quest dungeonItems[10] dungeonKeys[9]
#   defenseHearts strayFairies[10]
# MmUpgrades has the same layout as OotSaveUpgrades except that `dive` is
# called `scale`. Confirmed live: on upgrading the deku sticks, the u32 went up
# by exactly 1<<17, which is where dekuStick sits in both.
MM_UPGRADES = [
    ("quiver", 0, 3),
    ("bomb bag", 3, 3),
    ("strength", 6, 3),
    ("scale", 9, 3),
    ("wallet", 12, 2),
    ("bullet bag", 14, 3),
    ("deku sticks", 17, 3),
    ("deku nuts", 20, 3),
]

MM_ITEMS_ADDR = 0x68
MM_ITEMS_LEN = 48
MM_AMMO_ADDR = 0x98
MM_AMMO_LEN = 24
MM_UPGRADES_ADDR = 0xB0
MM_EQUIP_ADDR = 0x64      # nibbles: botas, tunica, escudo, espada
MM_QUEST_ADDR = 0xB4  # verified: bit 12 = Song of Time, the Skull Kid one
MM_DUNGEON_ITEMS_ADDR = 0xB8
MM_DUNG_ADDR = 0xB8       # dungeonItems[10]
MM_DUNG_LEN = 10
MM_KEYS_ADDR = 0xC2       # dungeonKeys[9]; 0xC2+9 = 0xCB defenseHearts, 0xCC fairies
MM_KEYS_LEN = 9
MM_STRAY_FAIRIES_ADDR = 0xCC
MM_STRAY_FAIRIES_LEN = 10

# Slots 24..47 are the masks. Verified in game: 2, 5 and 12. The rest is MM's
# standard order, consistent with those three but not checked one by one.
MM_MASK_SLOTS = [
    "Postman's Hat", "All-Night Mask", "Blast Mask", "Stone Mask",
    "Great Fairy's Mask", "Deku Mask", "Keaton Mask", "Bremen Mask",
    "Bunny Hood", "Don Gero's Mask", "Mask of Scents", "Goron Mask",
    "Romani's Mask", "Circus Leader's Mask", "Kafei's Mask", "Couple's Mask",
    "Mask of Truth", "Zora Mask", "Kamaro's Mask", "Gibdo Mask",
    "Garo's Mask", "Captain's Hat", "Giant's Mask", "Fierce Deity's Mask",
]


# MM slots 0..23: the items. Only the ones we have seen drop.
MM_ITEM_SLOTS = {
    12: "Powder Keg",    # 0x0C, with its counter in ammo:12
    17: "Hover Boots",   # 0xB2, showed up on picking up the Hover Boots
}


def mm_slot_name(i):
    if i < 24 and i in MM_ITEM_SLOTS:
        return MM_ITEM_SLOTS[i]
    if i >= 24:
        m = i - 24
        if m < len(MM_MASK_SLOTS):
            return MM_MASK_SLOTS[m]
    return f"slot {i}"


def mm_snapshot(save):
    st = {}
    for name, off, size, signed in MM_SCALARS:
        st[name] = read(save, None, off, size, signed)

    q = read(save, None, MM_QUEST_ADDR, 4)
    for bit, name in MM_QUEST_BITS.items():
        st[f"quest:{name}"] = 1 if q & (1 << bit) else 0
    st["heart pieces"] = (q >> 28) & 0xF

    for i in range(MM_ITEMS_LEN):
        st[f"item:{mm_slot_name(i)}"] = save[MM_ITEMS_ADDR + i]
    for i in range(MM_AMMO_LEN):
        st[f"ammo:{i}"] = save[MM_AMMO_ADDR + i]
    for i in range(MM_DUNG_LEN):
        st[f"boss:{i}"] = save[MM_DUNG_ADDR + i] & 1
    for i in range(MM_KEYS_LEN):
        st[f"key:{i}"] = read(save, None, MM_KEYS_ADDR + i, 1, signed=True)
    for i in range(MM_STRAY_FAIRIES_LEN):
        st[f"fairy:{i}"] = save[MM_STRAY_FAIRIES_ADDR + i]
    up = read(save, None, MM_UPGRADES_ADDR, 4)
    for name, shift, width in MM_UPGRADES:
        st[f"up:{name}"] = (up >> shift) & ((1 << width) - 1)

    # MM has no strength or scale, but it does have sword and shield
    # progression, in the nibbles of MmItemEquips (itemEquips at info+0x28,
    # bitfield at +0x20). Verified in both dumps: 0x0010 in each, i.e. shield 1
    # (the hero's) and no sword, which is how an OoTMM seed starts.
    eq = read(save, None, MM_EQUIP_ADDR, 2)
    st["up:sword"] = eq & 0xF
    st["up:shield"] = (eq >> 4) & 0xF
    return st


def mm_covered_offsets():
    out = set()
    for _, off, size, _ in MM_SCALARS:
        out.update(range(off, off + size))
    out.update(range(MM_ITEMS_ADDR, MM_ITEMS_ADDR + MM_ITEMS_LEN))
    out.update(range(MM_AMMO_ADDR, MM_AMMO_ADDR + MM_AMMO_LEN))
    out.update(range(MM_UPGRADES_ADDR, MM_UPGRADES_ADDR + 4))
    out.update(range(MM_QUEST_ADDR, MM_QUEST_ADDR + 4))
    out.update(range(MM_STRAY_FAIRIES_ADDR, MM_STRAY_FAIRIES_ADDR + MM_STRAY_FAIRIES_LEN))
    return out


# In MM two fields run on their own and would drown the log. Measured live over
# 20 s with MM as the running game, in the same coordinates that put items[] at
# +0x68:
#
#   +0x04  climbs ~0x13C per second
#   +0x36  climbs 0x14 per second -- 20, the frame rate, exactly like OoT's
#          naviTimer at +0x38
#
# This used to be range(0x0C, 0x14), on the header's ASSERT_OFFSETs saying
# `time` sits at +0x0C. Nothing in 0x0C..0x13 moves: the old range silenced an
# empty gap while these four bytes came through as `unidentified`, 19 times each
# in 20 s. The calibration pass in `items` hid it by silencing them again at
# run time, which is why it never looked broken.
#
# And now checked against combo/mm/save.h, remembering that this base is
# MmSave + 8 (the one that puts the ZELDA3 signature at +0x1C):
#
#   +0x04 = MmSave+0x0C  u16 time        ASSERT_OFFSET(MmSave, time, 0x000c)
#   +0x36 = MmSave+0x3E  u16 tatlTimer   info(0x24) + playerData.tatlTimer(0x1A)
#
# The old comment had `time` "at +0x0C per the header" -- the header's 0x0C is
# from MmSave, not from this base, hence the 8-byte slip. And the payload's own
# code confirms the first: it reads MmSave+0x0C as the clock (payload.py, refs
# relative to gSaveContext). tatlTimer is MM's naviTimer, the same field OoT's
# NOISE silences at +0x38.
MM_NOISE = set(range(0x04, 0x06)) | set(range(0x36, 0x38))


# OoT's per-scene flag table: perm[124], 0x1C bytes per scene.
PERM_ADDR = 0xD4
PERM_SCENE_SIZE = 0x1C
PERM_SCENES = 124
PERM_FIELDS = {0x00: "chest", 0x04: "swch", 0x08: "clear", 0x0C: "collect",
               0x10: "unk", 0x14: "rooms", 0x18: "floors"}


def perm_label(off):
    """If the offset falls in the per-scene flag table, describe it.

    Returns None if it is outside. This is what lets the log say 'scene 0 unk
    bit 2' instead of dumping a bare offset: OoTMM's relevant items are
    recorded in the `unk` field, which OoT vanilla does not use.
    """
    if not (PERM_ADDR <= off < PERM_ADDR + PERM_SCENES * PERM_SCENE_SIZE):
        return None
    rel = off - PERM_ADDR
    scene, within = divmod(rel, PERM_SCENE_SIZE)
    field_off = within & ~3
    byte_in_word = within & 3
    field = PERM_FIELDS.get(field_off, f"+0x{field_off:02X}")
    # big endian: byte 0 of the word holds the high bits
    bit_base = (3 - byte_in_word) * 8
    return f"scene {scene} {field} bits {bit_base}-{bit_base + 7}"


def covered_offsets():
    """Save context offsets that snapshot() already covers with a name.

    Anything that changes outside this is uncharted territory, and that is
    exactly what you want to see when hunting for new items.
    """
    out = set()
    for _, off, size, _ in OOT_SCALARS:
        out.update(range(off, off + size))
    out.update(range(OOT_QUEST_ADDR, OOT_QUEST_ADDR + 4))
    out.update(range(OOT_EQUIPMENT_ADDR, OOT_EQUIPMENT_ADDR + 2))
    out.update(range(OOT_UPGRADES_ADDR, OOT_UPGRADES_ADDR + 4))
    out.update(range(OOT_ITEMS_ADDR, OOT_ITEMS_ADDR + OOT_ITEMS_LEN))
    out.update(range(OOT_AMMO_ADDR, OOT_AMMO_ADDR + OOT_AMMO_LEN))
    return out


# Bytes that move on their own and would drown the log if reported.
#   +0x0C u16 dayTime, the time of day: always running. This one really is the
#         clock, not the +0x38 we called that at first.
#   +0x0E unk_0e, moves along with the previous one
#   +0x38 naviTimer, climbs constantly
#   +0x1352 the save checksum
#   +0x1354.. volatile state of the active scene
NOISE = set(range(0x0C, 0x10)) | set(range(0x38, 0x3A)) | set(range(0x1352, 0x1500))


def unmapped_changes(prev, cur, covered=None, noise=None):
    """Bytes that changed and belong to no named field."""
    covered = covered_offsets() if covered is None else covered
    noise = NOISE if noise is None else noise
    out = []
    n = min(len(prev), len(cur))
    for i in range(n):
        if prev[i] == cur[i] or i in covered or i in noise:
            continue
        out.append((i, prev[i], cur[i]))
    return out


_ITEM_IDS = None


def item_ids():
    """id -> name, per game, from data/ref/items.h of the OoTMM repo.

    The value items[] stores is directly that id, checked against the eight we
    hunted live (MASK_DEKU 0x32, POWDER_KEG 0x0c, COJIRO 0x2f...).
    With this table there is no need to hunt item by item just to name them.
    """
    global _ITEM_IDS
    if _ITEM_IDS is None:
        import re

        import paths

        _ITEM_IDS = {"oot": {}, "mm": {}}
        path = paths.res("data", "ref", "items.h")
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    m = re.match(r"#define ITEM_(OOT|MM)_(\w+)\s+(0x[0-9a-fA-F]+|\d+)", line.strip())
                    if m:
                        _ITEM_IDS[m.group(1).lower()][int(m.group(3), 0)] = m.group(2)
        except OSError:
            pass  # without the table we still show the id in hex
    return _ITEM_IDS


def item_name(game, value):
    name = item_ids().get(game, {}).get(value)
    return f"{name} (0x{value:02X})" if name else f"0x{value:02X}"


def fmt(key, value, game="oot"):
    """Format a value according to its family."""
    if key.startswith("item:"):
        return "-" if value == 0xFF else item_name(game, value)
    if key.startswith("quest:"):
        return "YES" if value else "no"
    return str(value)
