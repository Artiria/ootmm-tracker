# Placement Without the Spoiler

Every OoTMM autotracker I could find asks you to upload a spoiler log first. It
turns out the seed ROM already knows — the generator has to write the placement
into it, because the game needs to know what to hand you when you open a chest.

Notes from building a read-only tracker, verified on v32.0 and a dev build.
Everything below is reading data OoTMM already writes: no patching, no memory
writes.

The implementation is in this repository — `placement.py`, `rom.py` and
`mkchecks.py` — and it is MIT, like OoTMM itself. Take any of it.

## Why bother

A spoiler log is a second artifact the player has to find, keep next to the ROM,
and match to the right seed. It is also the thing that makes autotracking
conditional: *"autotracking only works after you import a spoiler log."* Drop
the dependency and the tracker just works from the ROM that is already loaded.

The four moves below are what that took. They are independent — you can take the
first one and leave the rest.

## 1. The placement table is in the ROM

The generator writes it to `COMBO_VROM_CHECKS`, one per build, in the extra DMA:

```c
oot  0xF0400000
mm   0xF0500000

typedef struct ComboOverrideData {  /* 16 bytes, sorted by key */
    u32 key;      /* (ovType << 24) | (sceneId << 16) | (roomId << 8) | id */
    s16 player;   /* multiworld: whose item this is */
    u16 value;    /* <-- the item, an index into GI_* */
    s16 giCloak;
    s16 unused[3];
} ComboOverrideData;
```

The game binary-searches it at runtime (`overrideData` in `item.c`); a tracker
can just read the whole thing in one pass and build a dictionary. Entries where
`key >> 24 == 0xFF` are the end sentinel. `player` is free multiworld
information: anything other than `1` means the item belongs to another world.

The address is a structural constant from `combo/defs.h`, not a VROM that moves
every release the way the xflag tables do.

## 2. Forming the key

The layout is four bytes:

| Byte | Field | Notes |
|---|---|---|
| 3 | `ovType` | chest 0x01, collectible 0x02, npc 0x03, gs 0x04, sf 0x05, cow 0x06, shop 0x07, scrub 0x08, sr 0x09, fish 0x0A — and `0x10 + slice` for xflags |
| 2 | `sceneId` | only for three of them; zero for the rest |
| 1 | `roomId` | packed with the setup for xflags; zero for chests |
| 0 | `id` | the CSV's global index, not the bit within its byte |

The part that cost the most time: **only `chest`, `collectible` and `sf` carry
the scene in the key.** The other seven live in global id spaces and their scene
byte is zero. On the first attempt `npc`, `gs`, `cow`, `shop`, `scrub`, `sr` and
`fish` all missed at once, which — luckily — was too tidy to be seven separate
bugs.

For xflags the key is built the same way `comboXflagItemQuery()` does it:

```c
room = (roomId & 0x3F) | ((setupId & 3) << 6)
ov   = 0x10 + sliceId
key  = (ov << 24) | (sceneId << 16) | (room << 8) | actorId
```

## 3. Item names come out of the ROM too

Reading `value` gives a GI index. Turning that into a name from a bundled copy
of `gi.yml` is where trackers rot silently: the index is a *position in that
file*, so one item inserted in the middle shifts every name behind it and
nothing complains.

I checked every OoTMM seed on my disk against the `gi.yml` I was shipping. **30
of 42 carry a table of a different length** — three generations of the
generator, and the two older ones agree with the file on 17% of names:

| `kItemNames` length | seeds | agreement with the bundled `gi.yml` |
|---|---|---|
| 936 (current) | 12 | 97% |
| 829 | 29 | **17%** |
| 784 | 1 | **17%** |

Where they diverge it is not a near miss:

```
gi 200   gi.yml: Dungeon Map (Jabu)    ROM 829: Compass (Water)
                                       ROM 784: Silver Rupee (Spirit Lobby)
gi 600   gi.yml: Giant's Mask          ROM 829: Goron Lullaby
                                       ROM 784: Dungeon Map (Great Bay)
```

Those 30 are seeds from **older generator versions**, not current ones — this
does not bite someone playing today's build. It bites when a tracker is pointed
at an older seed, which is exactly the case where nobody is suspicious, because
it starts up and looks fine.

And that is the asymmetry worth noticing: the *address* path has a guard, so an
old seed makes `mkchecks` abort with impossible bit positions and says so. The
*names* path had none. It was the last place where a version mismatch was wrong
in silence.

> Multiworld makes no difference here, in case it looks like it might: the two
> ROMs of a multiworld seed carry byte-identical name tables. The split is
> purely by generator version.

The names are in the payload, which is another extra-DMA file loaded whole at a
fixed address, so a pointer inside it is just `PAYLOAD_RAM + offset in the file`:

```c
oot  0xF0000000 -> 0x80400000
mm   0xF0100000 -> 0x80720000

const char* const kItemNames[]   /* text.c, indexed by gi - 1 */
```

Rather than hardcode its address — one more version constant, which is the whole
thing I was trying to get rid of — it is found **by content**: the longest run of
consecutive words that all point inside the payload and all land on a
NUL-terminated string. One test separates it from its neighbour: **at least half
the strings must contain a text control byte.** Item names carry colour macros;
the region-name table for hints sitting right next to it is plain text, and
without that check the two are indistinguishable.

Cleaning differs per game — OoT writes a colour as `0x05` plus an argument byte,
MM as a single byte below `0x20`.

> Once the names come from the ROM, there is no version-dependent lookup left
> that can go wrong quietly. It either finds the table or says it did not.

`gi.yml` stays for one thing only — the symbolic id (`OOT_BOMBS_5`), which is a
build symbol and does not survive compilation. It is used only while it still
agrees with the ROM on 90% of names, and dropped with a warning when it does not.

## 4. Finding the xflag tables by shape

The bit position for an xflag comes from three chained tables (`xflags.c`):

```c
setupIndex = sceneTable[sceneId]     + setupId
roomIndex  = setupTable[setupIndex]  + roomId * 12 + sliceId
bitPos     = roomTable[roomIndex]    + actorId
```

Their VROMs are in `custom.h`, and that is the one constant that had already
broken once across a big enough version jump. It does not have to be a constant,
because a chain has a recognisable shape:

```
scenes[]  u16, non-decreasing, starts at 0, indexes setups[]
setups[]  u16, non-decreasing, starts at 0, indexes rooms[]
rooms[]   s16, the bit itself, no ordering
```

So a candidate is **three consecutive uncompressed extra-DMA entries** where the
first two have that shape and each one indexes inside the next:
`max(scenes) < len(setups)` and `max(setups) < len(rooms)`.

Across the **42 OoTMM seeds** on my disk, spanning three generations of the
generator, that finds **exactly two chains per ROM** — one per build — with no
false positives, and on current versions it returns precisely the `custom.h`
constants. Those constants are worth keeping as a cross-check that prints when
the two disagree.

Pointed at a ROM that is not an OoTMM seed —the base ROM, vanilla OoT, an
unrelated N64 game— it does not guess: there is no extra-DMA header, and that
is reported rather than papered over.

## Reading the extra DMA without tripping

The header is at `COMBO_META_ROM = 0x03FFF000`: a u32 with the physical address
of a DmaEntry table, then a u32 with the entry count. Two things break a naive
reader, and both took a second seed to discover because the development seed
happened to avoid them:

- **Seeds can be compressed.** Then the entries are Yaz0 and every table you read
  is garbage. Decompress when `pend` is neither 0 nor `0xFFFFFFFF`.
- **Several tables can share one entry.** In compressed ROMs all six xflag tables
  fall inside a single entry, so you have to return the slice starting at the
  requested VROM, not the whole file.

A useful side effect: a plain 8 MB ROM has nothing at `0x03FFF000`, so the
absence of that header is a clean way to say *"this .z64 is not an OoTMM seed"*
instead of failing later with a struct-unpacking error that tells nobody
anything.

## Three more traps

- **`slice` is not the type.** OoT's slice 0 holds 19 different check types. It is
  which drop of the actor, not what kind of thing it is.
- **`roomId` is zero for chests**, even though the key's layout suggests
  otherwise.
- **The pool CSVs are a superset.** The ROM is the census, not the CSVs — rows
  exist there for checks the ROM never places. When two rows want the same bit,
  the one the ROM lists wins.

## What the ROM will not give you

Five things, and all of them are labels: the location's display name, its region,
the vanilla item, the fine-grained type, and the scene for the seven types that
live in global id spaces. None of them is needed to resolve an address — they are
what you still want the CSVs for.

## How well it works

| Measurement | Result |
|---|---|
| Active checks with an address, resolved straight from the ROM | 4,956 / 5,012 |
| Agreement with the spoiler on classifying filler | 5,018 / 5,018 |
| Checks mapped overall | 5,981 / 6,043 |
| xflag chains found by shape, across 42 seeds | 2 per ROM, 0 false positives |
| Versions | v32.0 and dev |

The two denominators are different on purpose: 6,043 is every row in the pool,
while 5,012 counts only the ones **active in this seed** — a vanilla check and
its Master Quest twin both exist in the table, but only one of them is in any
given ROM.

The 56 active checks that have an address but do not appear in the placement
table, and the 62 with no address at all, are known gaps rather than surprises
— mostly caught-fish flags and MM stray fairies.

## Credit where it is due

None of this is a new capability — it is reading what the generator already
writes, and the structures are all in the OoTMM tree: `item.c`, `xflags.c`,
`mark.c`, `text.c`, `custom.h`, `defs.h`. Thanks to the
[OoTMM team](https://github.com/OoTMM/OoTMM) for a codebase legible enough to
work this out from.

Posted in case it is useful to anyone else writing a tracker.
