# OoTMM Autotracker

> **0.1.0-beta — still being refined.** Single player is tested and measured;
> **multiworld has never been run** (see below). It has only ever been built on
> one machine. Expect rough edges, and please report what you find — that is
> exactly what this stage is for.

Reads the state of an OoTMM run from Project64-EM and turns it into names:
inventory, songs, masks, equipment, upgrades and checks.

**No spoiler log needed.** What item sits in each location is read from the
seed ROM itself.

## What works and what does not

| | |
|---|---|
| ✅ Single player, OoT and MM, crossing between them | tested against live saves and four RAM dumps |
| ✅ 5,981 of 6,043 checks | the missing 62 are `caughtFishFlags` and MM stray fairies |
| ✅ Item placement and names from the ROM | no spoiler log, on v32.0 and dev builds |
| ✅ OBS overlay, one URL per panel | transparent background, five standalone panels |
| ❓ Multiworld | **never run.** Two Lua scripts at once is question P4, open since day one |
| ❓ Emulators other than Project64-EM | the tracker only needs `read` and `read_block`; BizHawk is a five-step task in the backlog |
| ❌ Entrance tracking, logic and maps | not attempted — [The Last Tracker](https://www.thelasttracker.org) does that well |

The **how and why** are in
[`ootmm-autotracker-poc.md`](ootmm-autotracker-poc.md): addresses, offsets,
what was verified and how, and the loose ends. Read it before changing
anything.

If you are writing a tracker yourself, start with
[**Placement Without the Spoiler**](placement-without-the-spoiler.md) — how the
item placement, the names and the xflag bit positions come straight out of the
seed ROM. It is the part most worth stealing.

## Getting started

### With the `.exe` (no Python needed)

Double-click `ootmm-tracker.exe` and that is it: it finds the ROM, generates
its tables and icons the first time, drops `tracker.lua` into the emulator's
`Scripts\` folder and opens the overlay. In the emulator, with the ROM loaded:
**Debugger > Scripts**, run `tracker.lua`. Either order works.

The subcommands below work the same way: `ootmm-tracker.exe items`.

What it generates does **not** sit next to the executable — it goes to
`%LOCALAPPDATA%\OoTMM-Tracker\` (`checks.json`, `icons.*`, the cache, and the
`icons\` folder where you can drop your own).

> Windows Defender and friends are suspicious of unsigned PyInstaller
> executables. This one is not signed, so SmartScreen may warn you the first
> time — "More info → Run anyway". If you would rather not trust a binary, the
> source is right here and does exactly the same thing.

### In multiworld (not tested yet)

Every player runs **their own** ROM, save and tracker: each `.exe` finds its
own things and never talks to anyone else's. The tracker already understands
that an item can belong to another world — it reads that from the ROM's table
and records it as `player`.

**Before you sit down for a long session, please run this two-minute test**,
because it decides whether this works at all and it has never been checked:

> With the ROM loaded, open **Debugger > Scripts** and start the MultiClient's
> `adapter.lua` **and** `tracker.lua`, in that order. See whether both stay
> alive and whether the overlay reaches `ready`.

- **If both hold**: nothing else to do. The tracker behaves exactly as in
  single-player.
- **If the emulator only allows one**: then today you **cannot** run the
  tracker and the multiworld client at the same time, and the session has to
  be played without the tracker. There is no workaround: `proxy` mode is
  **not** one — it is a diagnostic tool that sits in the middle to log which
  addresses the multiworld client uses, and it does not feed the overlay. What
  would be needed is a Lua script doing both jobs, and it is not written.

Either way, **write down what the emulator said**: this is question P4 in the
POC, it has been open since the beginning of the project, and that two-minute
test closes it.

### From source

1. Copy `Scripts\tracker.lua` into Project64-EM's `Scripts\` folder (or run
   `python ootmm.py install-lua`, which finds it on its own). With the ROM
   loaded: **Debugger > Scripts**, run `tracker.lua`.
2. Then:

```
python ootmm.py items
```

It locates the save contexts by signature, calibrates noise for six seconds
(do not touch anything) and from there reports every change. It survives
switching between OoT and MM: when you cross over, it relocates the bases by
itself.

Order does not matter — `tracker.lua` retries the connection until the daemon
is up, and reconnects if you restart it.

## Subcommands

| | |
|---|---|
| `items` | both games' inventory in a loop, reporting changes |
| `checks` | completed checks, resolved to the spoiler's names |
| `watch ADDR:SIZE,…` | poll individual addresses |
| `dump ADDR:LEN` | dump a region (accepts `oot`, `mm`) |
| `find file PATTERN` | search a dump for a signature |
| `diff a b` | compare dumps |
| `proxy` | log which addresses the MultiClient uses |
| `install-lua` | copy `tracker.lua` into the emulator's folder |

To list the checks with the item in each one:

```
python ootmm.py checks --spoiler C:\...\OoTMM-f5PCTnhD\OoTMM-Spoiler-f5PCTnhD.txt
```

**The overlay needs no spoiler**: what item sits in each location is read from
the ROM itself (`placement.py`, the `COMBO_VROM_CHECKS` table), which is where
the game gets it from. That is enough for the junk filter and for showing
pending checks with their item, with no file to find or load.

The **names** come from the ROM too, from `kItemNames[]` in the payload. That
way they do not depend on `data/gi.yml` being from the same OoTMM version as
the seed: that file is indexed by position, so with an older seed the names
came out shifted and nothing said a word. It is kept only for the symbolic id
(`OOT_BOMBS_5`), and only while its names still agree with the ROM's.

The **Load spoiler…** button in the director view stays as a fallback, for
ROMs whose table cannot be read. It checks version, name agreement and
coverage before accepting the file, and whatever you load takes precedence
over what was read from the ROM.

## Files

| | |
|---|---|
| `ootmm.py` | the tool |
| `paths.py` | what ships with the program and what it generates (matters inside the `.exe`) |
| `ootmm.spec` | PyInstaller recipe: `python -m PyInstaller ootmm.spec` |
| `Scripts/tracker.lua` | the memory server that runs inside the emulator |
| `inventory.py` | both games' inventory map and the id table |
| `mkchecks.py` | generates `checks.json` from `data/` and the ROM |
| `placement.py` | what item is in each location and what it is called, read from the ROM (replaces the spoiler) |
| `mkicons.py` | extracts the icons from the ROM |
| `discover.py` | finds the ROM and spoiler, regenerates whatever is stale |
| `overlay.py` / `overlay.html` | the tracker you actually look at |
| `rom.py` | reading the ROM: Yaz0, dmadata, extra DMA |
| `fakelua.py` | a fake `tracker.lua` that serves a dump: testing without an emulator |
| `checks.json` | 6043 locations; 5981 with a resolved address |
| `data/` | data from the OoTMM repository (pool, scenes, npc) |
| `data/ref/` | the sources the mapping leans on: `mark.c`, `xflags.c`, `items.h` |
| `LICENSE` | this project's MIT license |
| `THIRD-PARTY.md` | which files come from OoTMM and under what license |

### Reference dumps

They exist to test changes **without starting the emulator**, and they are the
two possible RAM layouts:

```
ram-en-oot.bin      playing OoT:  OoT save 0x8011A5D0 · MM 0x8044BE18
ram-en-mm.bin       playing MM:   MM save  0x801EF678 · OoT 0x8076C4F0
fla-deswapeado.bin  the .fla file with its words already unswapped
```

The bases moving when you cross between games is the most dangerous gotcha in
the project: with fixed offsets the tracker works in OoT and reads garbage in
MM without raising a single error. That is why they are located by signature,
and why it is worth testing against both dumps.

```
python ootmm.py items --dump ram-en-oot.bin
python ootmm.py checks --dump ram-en-oot.bin
```

And the whole overlay, which has no `--dump`, with the fake Lua: the tracker
(or the `.exe`) in one console, the dump in another.

```
python ootmm.py overlay --no-window --port 13261
python fakelua.py ram-en-mm.bin 13261
```

## Distributing the tracker

**Code is distributed, never art and never save data.** Everyone generates
their own from their own copy of the game, and that keeps the package free of
Nintendo material.

What ships:

```
ootmm.py  overlay.py  overlay.html  mkchecks.py  mkicons.py  discover.py
inventory.py  placement.py  rom.py  paths.py  fakelua.py  data/
Scripts/tracker.lua  ootmm.spec  README.md  LICENSE  THIRD-PARTY.md
```

Or the `.exe`, which carries all of that inside and **also** carries no art
and no save data: build it with `python -m PyInstaller ootmm.spec` and you get
`dist/ootmm-tracker.exe`, 8.9 MB.

Licensed **MIT** (see [`LICENSE`](LICENSE)), the same as OoTMM: download it,
use it, modify it and redistribute it without asking. The third-party files
that ship inside are declared in [`THIRD-PARTY.md`](THIRD-PARTY.md).

`adapter-tracker.lua`, used by `proxy` mode, is **not** distributed: it is a
verbatim copy of the MultiClient's `adapter.lua` with the port changed, which
makes it someone else's code. Anyone who wants that mode can make it from
their own.

What is **not** distributed, because every machine generates it (and it is in
`.gitignore`):

| File | What it is | Where it comes from |
|---|---|---|
| `icons.png` / `icons.json` | the item icons | `mkicons.py`, extracted from **your** ROM |
| `checks.json` | the 6,043 locations | `mkchecks.py`, from **your** ROM |
| `discover-cache.json` | paths and hashes | your emulator |
| `icons/*` | images you drop in yourself | you |

Whoever installs it only has to open the `.exe` (or run
`python ootmm.py overlay`): it finds their ROM on its own — by the hash of
Project64's save folder — looks for a spoiler next to it, and generates icons
and tables on their machine the first time. There is no manual step and no
paths to pass in.

> The icons come out of each user's own ROM, **both games'**. OoT's live in
> `icon_item_static`; MM's in a CmpDma archive with every icon compressed
> separately, and that is where the 24 masks come from. Anyone who wants to
> replace one with a different image can drop it in `icons/`
> (see [`icons/README.md`](icons/README.md)); that never ships in the package.

## Support

The tracker is free and will stay free. Everything it does is in this
repository and **nothing sits behind a payment**: no paid version, no reserved
features, no keys.

If it has been useful and you feel like buying me something:
[Ko-fi](https://ko-fi.com/artiria) · [PayPal](https://paypal.me/JuanRamos633),
or the sponsor button on the repository page.

To be clear about what the donation is for: **it is for the tracker**, which is
my own code and distributes nothing from the games. The randomizer, the ROMs
and the OoTMM team's work have nothing to do with it.

## Credits

None of this would exist without **[OoTMM](https://github.com/OoTMM/OoTMM)**,
the randomizer that combines Ocarina of Time and Majora's Mask, or without its
team. The tracker reads the structures they invented — the xflags, the check
table, the payload's extra DMA — and leans on several data files from their
repository, listed in [`THIRD-PARTY.md`](THIRD-PARTY.md).

Thanks as well to the people in the OoTMM Discord, where the format questions
that are written down nowhere else get answered.

## License

MIT — see [`LICENSE`](LICENSE). Third-party material is covered in
[`THIRD-PARTY.md`](THIRD-PARTY.md).

## Next

In order of value:

1. Confirm MM's `perm` and OoT's `gsFlags` in game.
2. The 80 missing checks: `caughtFishFlags`, MM stray fairies, `cow`.
3. The multiworld side: two Lua scripts at once, and the co-op mailbox.
