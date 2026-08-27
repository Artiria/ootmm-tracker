# POC — OoTMM Autotracker

**Timebox: 4–6 h.** The goal is not to build anything usable, it is to answer the binary questions that block the design. If something drags on, write it down and move on.

---

## A note on the emulator

**The target platform is Project64-EM, single player included.** The OoTMM community is there (the wiki recommends P64-EM, Maro's prebuilt P64, RetroArch and Ares; BizHawk does not appear). An overlay for BizHawk would be an overlay for nobody.

Two consequences:

- **The offsets are emulator-independent.** The save context's base is a property of the ROM. Whatever you find on any emulator that runs it holds on all of them. The only specific parts are the Lua API and the byte order of the memory domain.
- **P64-EM's Lua API you get from the multiworld script.** There is no public documentation. That script is your reference and it has to be read before writing a single line.

BizHawk is optional and only as a development convenience: since 2.9 it carries the Ares64 core and OoTMM recommends Ares, so the ROM should boot. If you cannot manage it in 15 minutes, do not push it and do everything in P64-EM.

---

## Questions this spike has to answer

- [x] **P1** — Can I read the game's memory from Lua? **Yes.** Directly in P64-EM, without going through BizHawk.
- [x] **P2** — Where is the save context's base, and can it be located? **Yes, and by signature.** OoT at `0x8011A5D0`, MM at `0x8044BE18`.
- [x] **P3** — Is there a bitfield of completed checks? **Yes**, the per-scene flag table, with the mapping verified against the spoiler.
- [x] **P4** — Does P64-EM allow loading my script **at the same time** as the multiworld script? **Yes.** A real multiworld session on 14 Aug 2026 tracked the player's own world throughout. What it cannot do is see the partner's world, which is not a limitation of the emulator: their progress is not in this machine's memory. See "Multiworld, first session" below.
- [x] **P5** — Does P64-EM's Lua have sockets? **Yes.** `socket.tcp`, `send`, `recv`, `sleep`.
- [ ] **P6** (optional) — In co-op mode, does the mailbox also carry local items or only crossed ones?

**P1, P2, P3 and P5 answered: the project is viable in single player.** P4 was closed on 14 Aug 2026 by a real session; **P6 is the only one left**, and it only constrains co-op.

> Everything below was verified on **OoTMM v32.0** (seed `f5PCTnhD`), on Project64-EM 1.0.3, with the Expansion Pak. Without touching BizHawk at any point.

---

## Preparation (30 min)

1. Generate **two single-player seeds on the same OoTMM version**, different seed. Keep both spoiler logs.
2. Write down the generator's exact version. Everything you find from here on is tied to it.
3. Clone `github.com/OoTMM/OoTMM`. Search the code for the save definitions:
   - `grep -rn "gOotSave\|gMmSave\|gSharedCustomSave\|CustomSave" --include=*.h`
   - See whether the build produces a `.map` or a symbol file. If it does, **you have finished P2 without touching the emulator**.
4. Unpack the `multi-client` release and find the Lua script. Read it end to end before writing anything of your own — it is your documentation for P64-EM's real API.

---

## Phase 1 — Read something (1 h)

> **ANSWERED.** It was done directly in P64-EM. The `gui.text` example below does not apply: there is no graphics API. The check was done by reading `osMemSize` at `0x80000318`, whose correct value (`0x00800000`) is known in advance.

Goal: close the whole loop *address → read from Lua → correct value on screen* with something trivial like rupees. Without this, none of the rest matters.

**Locating the rupees.** Project64 drags along a debugger with memory search, symbol management and read/write breakpoints. The mechanics are the same in any tool:

1. Note your rupee counter and search for that exact value (2 bytes).
2. Spend or pick up rupees and filter again by the new value.
3. Three or four iterations and few candidates are left.
4. Write to one of them and see whether the HUD changes. That is the one.

> **Shortcut:** write breakpoints are a better tool than iterative searching. Put a watchpoint on a candidate, pick up a rupee, and you see directly which code touches which address. It will be even more useful in phase 3.

**Reading it from Lua.** Take the exact functions from the multiworld script. The BizHawk equivalent, purely as a reference for the shape the loop should have:

```lua
-- REFERENCE (BizHawk's API, NOT P64-EM's)
local ADDR = 0x000000

while true do
  local v = memory.read_u16_be(ADDR, "RDRAM")
  gui.text(10, 10, "Rupees: " .. v)
  emu.frameadvance()
end
```

Write P64-EM's real function names into the results template. They are the foundation of everything you write afterwards.

**Address conversion:** the game's virtual addresses (`0x80xxxxxx`) translate to a physical offset with `addr & 0x00FFFFFF`.

> **Endianness gotcha:** the RDRAM domain may expose bytes swapped in groups of 4, and this varies between emulators and cores. If 16/32-bit reads line up but 8-bit ones come out shifted, try `XOR 3` on the address for byte reads. Confirm it here, not later.

✅ **P1 answered** once the number on screen follows the game's in real time.

---

## Phase 2 — The save context's base (1.5–2 h)

> **ANSWERED through a route that was not planned:** searching for the `ZELDAZ` signature in a RAM dump, and validating it against the `.fla` file. Neither the iterative rupee search nor the debugger were needed.

This is the project's real risk.

**Route A (preferred): from the source.** If you got the address from the `.map` during preparation, validate it by reading a known field (rupees, hearts) and comparing against what you see in game.

**Route B: by searching.** The rupee address you already have is *inside* the save context. Dump the surroundings to a file and look for the header:

```lua
-- REFERENCE. Replace the read with P64-EM's real API.
local BASE = 0x000000
local f = io.open("dump.bin", "wb")
for i = 0, 4095 do
  f:write(string.char(memory.read_u8(BASE + i, "RDRAM")))
end
f:close()
```

Open it in a hex editor and look for the structure: file name, magic, counters. Cross-reference with the structs in the code.

**Mandatory validation — do not skip this:**
- [ ] Read 4–5 different fields (rupees, hearts, an upgrade, a song) and verify each one against the game.
- [ ] Repeat with the **second seed**. If the base changes between seeds of the same version, your static-offset strategy is no good and you have to locate by signature in RAM.
- [ ] Save state, reload, check it still holds.
- [ ] Check what is at that address on the **title screen**. You need a validity check before emitting anything.

✅ **P2 answered** once you read correctly on two different seeds.

---

## Phase 3 — The check bitfield (1 h)

> **ANSWERED.** It exists, it is the per-scene flag table, and the bit → check mapping is verified against the spoiler. Step 2 below (diffing two dumps) is what worked, but only after narrowing to the regions that get persisted to the save file.

This is what separates an item tracker from a real progress tracker.

1. In the code, look for how OoTMM marks a check as collected. Terms: `checks`, `locations`, `SAVE_BIT`, `setCheck`.
2. Put a write breakpoint on the candidate area and open a chest: you will see exactly which address is touched. Alternative without a debugger: dump before and after opening the chest and diff the two files. You are looking for a bit that goes to 1 and does not come back.
3. With the spoiler log in front of you, verify that the bit's index corresponds to the check you opened.

If it turns up: check tracking comes almost for free and the project gains a lot of value.
If it does not: you still have a perfectly valid item tracker. Not a blocker.

---

## Phase 4 — Coexisting with the multiworld script (1 h)

> **PENDING.** The only block left untouched. It does not constrain single player.

This only applies to multiworld sessions: in single player there is no multiworld script running and the problem does not exist.

- [ ] Load your script while the multiworld script is running. Do they coexist or step on each other?
- [ ] Are there sockets in the API? If not: write to a file and have a local watcher follow it.
- [ ] If you used BizHawk in phases 1–3, revalidate the save base in P64-EM. It should be identical —same ROM— but confirm it before calling it done.

**If it does not allow two scripts:** not fatal. The multiworld version becomes a fork of the multiworld script with your emitter inside, and you keep a separate script of your own for single player. Two artifacts instead of one, the same parsing behind them.

---

## Gotchas to verify during the spike

| Situation | Status |
|---|---|
| Title screen / file select | **Confirmed**: memory is garbage before boot. Saw `0x00C8083C` → `0` → `0x00800000`. Validity flag: `osMemSize` plus the signature. |
| OoT ↔ MM switch | **The most dangerous gotcha of all, and our first answer to it was incomplete.** It is true that there are two save contexts alive at once. But when you **cross** from one game to the other, RAM is reorganised entirely: the active game moves to the low area and the other to the high one. With fixed offsets the tracker would work perfectly in OoT and start reading garbage on entering MM, **without raising any error**. Locating by signature solves it on its own, and as a bonus it tells you which game the player is in: whichever has its signature in the low area. <br><br>`Playing OoT:` OoT `0x8011A5D0` · MM `0x8044BE18`<br>`Playing MM:` MM `0x801C6954` · OoT `0x8076C4F0` |
| **Check flag latency** | **A new gotcha, not on the list, and the one that affects the design most.** A chest's flag is not written when you open it: it is flushed to the save context **when you leave the scene** or when you save. A tracker that only reads the table sees checks late. For immediate reaction you also have to read the active scene's temporary flags (`+0x1357`–`+0x137B`). |
| 3-day cycle reset (MM) | Untested. The *latch* is still a good idea. |
| Save & quit + reload | Partially seen: the flags persist across dumps and survive saving. A full reload is still untested. |
| Polling frequency | Does not apply as written: **there is no frame callback**. The loop is free-running, with `socket.sleep`. Polling every 100–250 ms is more than enough. |
| Torn reads | With no frame synchronisation, you can catch a structure half-written. It has caused no problems reading the save context, but the *latch* is advisable. |

---

## Results

This is the future daemon's `offsets/v32.0.json` entry.

```
OoTMM version:        v32.0  (seed f5PCTnhD)
Emulator:             Project64-EM 1.0.3, Lua 5.4, RDRAM 8 MB (Expansion Pak)
u8 needs XOR 3:       NO. read_u8 is consistent with read_u32.
Protocol endianness:  binary.pack_* is LITTLE endian, and N64 memory is
                      big endian. Individual values come out right if you
                      pack and unpack the same way, but blocks of bytes
                      have to be reversed in 4-byte words.

Save context base:    OoT  0x8011A5D0      MM  0x8044BE18
  Method:             signature in RAM. "ZELDAZ" (OoT) / "ZELDA3" (MM) at base+0x1C.
  Static copies:      OoT  0x800FBFB8      MM  0x80442248   (do not use: they never change)
  Stable across seeds:        NOT VALIDATED with a second seed.
                              Does not matter: with signature search it stops mattering.
  Valid on title screen:      NO. Before boot there is garbage.
                              Validity flag: osMemSize at 0x80000318 == 0x00800000,
                              plus the presence of the signature.

Verified fields (offsets relative to OoT's base):
  dayTime             +0x00C   u16   time of day; always running, it is noise
  deathCount          +0x022   u16
  healthCapacity      +0x02E   u16
  health              +0x030   u16   16 per heart; saw a heal 44 -> 48
  magicLevel/magic    +0x032   s8+s8
  rupees              +0x034   s16
  swordHealth         +0x036   u16   Giant's Knife durability; comes out as 8
  naviTimer           +0x038   u16   counts up on its own; also noise
  items[24]           +0x074   ---   item id per slot, 0xFF = empty
  ammo[15]            +0x08C   ---
  beans               +0x09B   u8
  equipment           +0x09C   u16   4 nibbles: swords/shields/tunics/boots
  upgrades            +0x0A0   u32   8 fields of 2-3 bits
  questItems          +0x0A4   u32   songs, medallions, stones
  dungeonItems[20]    +0x0A8
  dungeonKeys[19]     +0x0BC
  goldTokens          +0x0D0   u16
  scene flags         +0x0D4   ---   see below
  temporary flags     +0x1357..+0x137B   active scene; they change on room change
  checksum            +0x1352   u16

  The structure closes exactly at +0xD4, which is `perm`, verified separately
  with the chests. That makes the block consistent from end to end.

  CORRECTION to an earlier version of this document: +0x38 is not the time of
  day but naviTimer (the clock is +0x0C), and +0x537 is NOT magic (that is at
  +0x33). That +0x537 is still unidentified.

Check bitfield:  YES
  base    +0xD4 + scene*0x1C,  124 scenes of 0x1C bytes
  fields  +0x00 chest   +0x04 swch   +0x08 clear   +0x0C collect
          +0x10 unk     +0x14 rooms  +0x18 floors
  size    0xD90 bytes in total
  Bit → check mapping confirmed against the spoiler: YES
    scene 40 (0x28) chest = 0x0F  -> the 4 chests in Mido's House
    scene 85 (0x55) chest = 0x01  -> Kokiri Sword Chest
  The `unk` field is unused in vanilla OoT and here it does carry data: it is
  the candidate for OoTMM storing non-chest checks there.

Regions persisted to the .fla (everything else is volatile):
  0x800FBF00-0x800FC000   OoT's copy
  0x8011A5C0-0x8011BA20   OoT's save
  0x8044BE10-0x8044CE10   MM's save
  The .fla is word-swapped and OoT's save starts at its offset 0x20.

P64-EM:
  Two simultaneous scripts:  UNCONFIRMED (the Scripts dialog is a list and the
                             error is "script is already running", per script
                             rather than global: points to yes)
  Sockets in Lua:            YES, but only as a client. The daemon has to be
                             the one listening.
  Reading:      memory.read_u8/s8/u16/s16/u32/s32/f32/f64  (raw 0x80xxxxxx
                address, no domain and no mask). Writing: memory.write_*
  Packing:      binary.pack_u8/../f64 and binary.unpack_u8/../f64
  Per-frame callback:   DOES NOT EXIST. Nor is there a graphics API (no gui.text).
                        The script runs on its own thread, with a free loop.
  Save base same as in BizHawk:  n/a, BizHawk dropped.
```

---

## Decision criteria

**Verdict: go ahead, and the spike overshot.** P1, P2, P3 and P5 answered, and on top of that it produced a tracker that already reads both games' inventory live. What is left is overlay design, not reverse engineering.

> **Status in one line:** the full inventory (325 ids), songs, medallions, stones, masks, equipment bit by bit, upgrades, counters and 713 checks are read and translated to names, live and for both games. It survives the game switch. Anything unrecognised is reported raw with its address.

P3 worked out, so the project is a real progress tracker and not just an item one.

The risk flagged as lethal —the save base moving unpredictably within a single version— is now covered twice over, and it did not even take waiting for it to fail:

- **Locating by signature**, which was plan B, turned out to be trivial and is now the main method. The offsets are relative to the base, so a version change that moves the structure breaks nothing as long as the signature is still there.
- **Cross-validation against the save file.** The RAM bytes at `0x8011A5D0` are identical one by one to the `.fla`'s. That turns any doubt about the base into something checkable in seconds and without an emulator.

Validating with a second seed is still pending, but with signature search it has stopped being a project risk.

### Things in the original plan that turned out to be false

Worth writing down, because they diverted work:

- **`XOR 3` does not apply.** It is a BizHawk gotcha. In P64-EM byte reads are consistent with 32-bit ones. There is a word swap, but it is in the protocol's packing (`binary.pack_*` is little endian), not in memory.
- **Phase 1 cannot be done as written.** There is no `gui.text` or any graphics API: the overlay cannot be drawn inside the emulator and has to be an external window. For a streamer overlay that makes no difference, but it shapes the design from the start.
- **There is no frame callback.** The script runs on its own thread and the loop is free-running.
- **BizHawk was a waste of time.** A different API and an endianness gotcha that does not exist on the target.
- **The bitfield was not where it was first looked for.** Hunting it with full RAM dumps gives 22% of bytes differing and is unworkable. What works is narrowing to what gets persisted to the save file: there the same experiment goes from 46,000 candidate bytes to 2.

---

## Next step if it works out

The increment proposed —a dumb Lua that emits bytes, Python that parses— **is already built and is what was used for everything above**. With one difference: instead of writing to a file it goes over a socket, because P64-EM's Lua has sockets.

### Tools

**`Scripts/tracker.lua`** — a memory server, independent of the multiworld script. Its own port (13251) so both can run at once. It speaks the same protocol as `adapter.lua` on opcodes 2/3/4/6/7/8, and adds `PING` (identifies the script and pins the byte order) and `READ_BLOCK` (dumps a region in one request). It reconnects on its own, so the daemon can be restarted without touching the emulator or the ROM.

> **It lives in the project, at `Scripts/tracker.lua`.** The emulator loads it from
> its own `Scripts\` folder, so there is a **hard link** there to the project's
> copy: a single file with two names, with no copies that drift apart.
> If it ever stops being linked —an editor that rewrites by creating a new file
> instead of truncating breaks the link— it is remade with
> `New-Item -ItemType HardLink -Path <emulator path> -Target <project path>`,
> which does not need administrator rights because both are on C:.
>
> Careful editing it from PowerShell: `Set-Content -Encoding utf8` on 5.1
> writes a **BOM**, and three `EF BB BF` bytes at the start blow up Lua's
> parser. It happened while setting up the link; you can see it by looking at
> the first bytes.

**`ootmm.py`** — seven subcommands:

| | |
|---|---|
| `items` | **reads both games' inventory in a loop and reports every change** |
| `checks` | **lists the completed checks, by name**; live or from a dump |
| `watch ADDR:SIZE,…` | polls addresses and prints only what changes |
| `dump ADDR:LEN` | dumps a region (accepts names: `oot`, `mm`) |
| `find dump.bin PATTERN` | searches for a signature; `--swapped` tries both views |
| `diff a.bin b.bin` | compares dumps |
| `proxy` | captures which addresses the MultiClient uses |

**`mkchecks.py`** — generates `checks.json` by cross-referencing OoTMM's data in `data/`. With `--rom <seed.z64>` it also resolves the 4,751 xflags by reading the ROM's three lookup tables; with `--spoiler` it knows which dungeons are Master Quest.

**`overlay.py` + `overlay.html`** — the tracker you actually look at. `ootmm.py overlay` starts a polling thread, an HTTP server and a window of its own. See the section below.

**`inventory.py`** — both games' inventory map, with the id table.

**`data/ref/`** — copies of the files from the OoTMM repo that everything leans on: `mark.c`, `xflags.c`, `items.h`.

**`Scripts/adapter-tracker.lua`** — a clone of the adapter with the port changed, only for `proxy` mode.

### The method that found the bitfield

`diff`'s filters matter more than the tool, because the real problem is noise: a full RAM dump gives 22% of bytes differing.

```
--exclude noise.bin   discards what changes on its own (time, RNG, actors)
--bits-set            only bytes that gain bits without losing any
--one-bit             only bytes that turn on exactly one bit
--max-run N           discards wide runs, which are buffers
--range               narrows to a region
```

The one that actually solves it is **narrowing to the regions persisted to the `.fla`**. Everything else is volatile by definition, so a check cannot be there. With that, the Recovery Heart experiment went from 46,626 candidate bytes to **2**.

A reproducible procedure for locating a new check:

1. Dump `a`, with the save loaded and the player standing still.
2. Dump `noise` ten seconds later, **without touching anything**.
3. Take the item, **leave the scene** (this is essential: the flag is not written until then) and dump `b`.
4. `diff a b --exclude noise` narrowed to the three persisted regions.

### Bit → check name mapping: **done** (for chests)

The table did not have to be deduced. It is in the OoTMM repo, across three files:

| File | What it contributes |
|---|---|
| `data/pool/pool_oot.csv` | `location, type, hint, scene, id, item` for 3236 locations |
| `data/pool/pool_mm.csv` | the same for MM's 2807 |
| `data/defs/scenes.yml` | scene name → numeric index |

**The CSV's `id` field is the bit number directly.** And it matches what was measured in RAM without touching anything:

```
Mido's House Top Left/Right/Bottom L/R   chest  KOKIRI_MIDO    id 0x00-0x03
Kokiri Forest Kokiri Sword Chest         chest  KOKIRI_FOREST  id 0x00
OOT_KOKIRI_MIDO:   0x28   -> scene 40, chest = 0x0F   ✓
OOT_KOKIRI_FOREST: 0x55   -> scene 85, chest = 0x01   ✓
```

`mkchecks.py` cross-references the three files and generates `checks.json` with the address and bit for each location. `ootmm.py checks` reads the state and resolves it to names, cross-referencing the spoiler to show what item is in each one:

```
OoT CHESTS: 5 / 305  (1.6%)

  KOKIRI_MIDO  (scene 0x28)
    [x] bit  1  Mido's House Top Right     ->  Minuet of Forest
    [x] bit  3  Mido's House Bottom Right  ->  Blast Mask
  KOKIRI_FOREST  (scene 0x55)
    [x] bit  0  Kokiri Forest Kokiri Sword Chest  ->  Recovery Heart
```

### Where each type of check goes

`packages/generator/src/common/mark.c` (copy in `data/ref/`) has the full switch:

```c
OV_CHEST        perm[scene].chests       |= 1 << id
OV_COLLECTIBLE  perm[scene].collectibles |= 1 << id
OV_GS           BITMAP32_SET(gsFlags, id - 8)
OV_COW          gCowFlags |= 1 << id
OV_NPC/SHOP/SCRUB/SR/FISH   BITMAP8_SET(gSharedCustomSave…, id)
default         xflags → BITMAP8_SET(gSharedCustomSave.oot.xflags, bitPos)
```

**`gSharedCustomSave` (with the game in OoT): `0x8044B570`.** It fell out of a single measured fact: buying `Kokiri Shop Item 2` (id 1) turned on bit 1 of byte `0x8044B88A`, which is `shops[0]`. Subtracting the `OotCustomSave` layout and `XFLAGS_COUNT_OOT = 0x2FA` gives the base.

```
oot.xflags[0x2FA]  0x8044B570      oot.scrubs[8]  0x8044B892
oot.npc[32]        0x8044B86A      oot.sr[16]     0x8044B89A
oot.shops[8]       0x8044B88A  ← measured
```

### The xflags: the ROM's three tables

An xflag's `bitPos` is not in the CSV, it comes from three chained tables that live in the ROM (`xflags.c`):

```c
setupIndex = sceneTable[sceneId] + setupId
roomIndex  = setupTable[setupIndex] + roomId*12 + sliceId
bitPos     = roomTable[roomIndex] + actorId     // roomTable is s16
```

**How you get to the tables.** v32.0's `custom.h` gives their VROMs; since they are `>= 0x08000000`, `comboDmaLookup` sends them to the *extra DMA*: a `u32` at `COMBO_META_ROM = 0x03FFF000` gives the physical address of a `DmaEntry` table, and the next `u32` its entry count. All six come out uncompressed (`pend == 0`), so `pstart` is the direct offset inside the `.z64`:

| Table | VROM | ROM (this seed) | Entries |
|---|---|---|---|
| OoT scenes | `0x80B0F00` | `0x3D2F280` | 101 |
| OoT setups | `0x80B0FD0` | `0x3D2F350` | 142 |
| OoT rooms | `0x80B10F0` | `0x3D2F470` | 6252 |
| MM scenes | `0x80B41D0` | `0x3D32550` | 114 |
| MM setups | `0x80B42C0` | `0x3D32640` | 118 |
| MM rooms | `0x80B43B0` | `0x3D32730` | 4524 |

**The CSV's `id` was a packed key, not a bit number.** It is generated by `packages/generator/scripts/xsanity.ts`, which is also what builds the tables:

```
key = (sliceId << 16) | ((setupId & 3) << 14) | (roomId << 8) | actorId
```

That is why the xflag rows' ids have five hex digits and the chests' have four. The 6-bit `roomId` leaves plenty of room for the grotto trick (`0x20 | grottoData`) that `comboXflagInit` does.

**Vanilla and MQ share a bit.** Of OoT's 2336 xflags, 149 pairs collide on `bitPos`, and all 149 are exactly a vanilla check against its `MQ …` equivalent: the tables do not distinguish the two versions because in a given seed only one exists. Zero collisions within the same group, and zero in MM (2415 out of 2415 distinct). The spoiler says which one applies (`Master Quest Dungeons:`); this seed is `none`. `mkchecks.py` marks those rows with `"mq": true` and leaves the decision to the consumer.

**Verified against the save.** Of the 12 bits set in `oot.xflags`, all 12 map to a named check and all of them fall in Kokiri Forest and Saria's house, which is exactly where the save is. Not one orphan bit. Cross-checked two independent ways: the RAM dump and the `.fla`.

### MM's custom save: it was sitting right behind OoT's

`MmCustomSave` goes right after `OotCustomSave` inside `SharedCustomSave`. `OotCustomSave` ends at `0x377` (`sr` ends at `0x33A`, padding to `0x33C`, two `OotRespawnData` of `0x1C`, `powderKegTimer` s16 at `0x374`, bitfield at `0x376`) and carries `ALIGNED(16)`, so **`MmCustomSave` starts at `+0x380`**.

```
mm.xflags[0x350]  0x8044B8F0      mm.shops[4]    0x8044BC60
mm.npc[32]        0x8044BC40      mm.halfDays    0x8044BC64
```

Confirmed twice over against the `.fla`: `OotCustomSave`'s trailing bitfield shows up exactly at `+0x376`, and `mm.halfDays` reads `0x3F` exactly at `+0x6F4`, which is where this layout puts it. As a bonus, `mm.npc` byte 12 bit 0 set = `Initial Song of Healing`, which is the save's first MM check.

> Why the previous hunt failed: the byte was being searched for in RAM, and `gSharedCustomSave` sits at a different address depending on which half of the game is running. With OoT running the base is `0x8044B570` and from there you read MM's flags **too**, because the block is shared. With MM running the base is a different one and is still unlocated.

**A third way to read it, without an emulator.** `save.c` stores the whole block in flash: `Flash_ReadWrite(0x18000 + 0x4000 * fileIndex, &gSharedCustomSave, …)`. The same offsets work on an unswapped `.fla`.

### MM's scene layout

It comes out entirely from walking the structure, with nothing hunted. **Careful with MM's base**: the one the project uses (`0x8044BE18`) was pinned by the signature, using OoT's convention that `newf` is at `base+0x1C`. In `MmSave`, `newf` is at `+0x24`, so that base is really `MmSave+0x08`. It is not being changed: every offset in `inventory.py` is relative to it and measured in game.

From there `info = base+0x1C`, and `MmSaveInfo` walks itself:

```
playerData   0x00  (0x28)      inventory  0x4C  (0x88)
itemEquips   0x28  (0x22)      perm       0xD4  <- permanentSceneFlags[120]
```

**MM's `permanentSceneFlags` = `base+0xF0` = `0x8044BF08`** with OoT running. The field order is **not** OoT's: MM has two `switch` fields, so `collectible` lands at `+0x10` and not at `+0x0C`.

```c
chest 0x00   switch0 0x04   switch1 0x08   clearedRoom 0x0C
collectible 0x10   clearedFloors 0x14   rooms 0x18
```

**Verification status: derived, not measured.** The table is all zeros in both dumps, because MM has only one check done (`Initial Song of Healing`) and no chest opened. What *is* measured in game are the six offsets that the very same arithmetic produces —`items 0x68`, `ammo 0x98`, `upgrades 0xB0`, `quest 0xB4`, `strayFairies 0xCC` and `skullCountSwamp 0xEB8`, this last one already **behind** the table— and with those six anchored there is no slack left to put `perm` anywhere else.

> **A falsifiable prediction, for the next play session.** The first chest you open in Termina must set a bit at `0x8044BF08 + scene*0x1C`. The grotto ones (scene 0x07) all go to the u32 at `0x8044BFCC`: e.g. `Termina Field Dodongo Grotto` → bit 0, `Deku Palace Grotto Chest` → bit 5. Remember the flag is not written when the chest is opened but when you leave the scene.

### The gold skulltulas

They are not custom save, they are one more field of `OotSaveInfo`, so they come out of the same structure arithmetic:

```c
OV_GS   BITMAP32_SET(gOotSave.info.gsFlags, id - 8)
#define BITMAP32_SET(m,b)  ((m)[(b) >> 5] |= (1 << ((b) & 0x1f)))
```

**`gsFlags[6]` = `base+0xE9C` = `0x8011B46C`.** LSB first within the u32, no tricks. The CSV's ids here really **are** bit numbers, unlike the xflags; they go in blocks of 8 per scene group (block 0 is reserved, hence the `−8`) and reach 179, i.e. bits 0..171 of the 192 available.

The 144 rows are **100 vanilla + 44 Master Quest**, and the 44 MQ ones collide one to one with a vanilla, just like in the xflags. The 100 vanilla give 100 distinct `(addr, bit)` pairs.

Walking the structure **closes from both ends**, which is what makes it reliable without measuring it: forwards, `perm` ends at `0xE64`, `fw` takes `0x28` → `0xE8C` (which is literally the name of the next field, `unk_e8c`), `+0x10` → `0xE9C`; backwards, `unk_EB4` pins the end of `gsFlags[6]` at `0xEB4 − 0x18 = 0xE9C`. And carrying on from there lands exactly on `eventsMisc = 0xEF8`, which does have an `ASSERT_OFFSET`.

> **A falsifiable prediction, and how it resolved.** The prediction was: if the player has killed 3 skulltulas, 3 bits must show; if they have 3 *tokens* but have killed none, zero bits, because in OoTMM the tokens are items that drop from any location. The result was **zero bits with 3 tokens in the inventory**: the second branch. Cross-referencing the 88 completed checks against the spoiler gives exactly `3 Gold Skulltula Token`, all of them from other locations. The offset is not confirmed, but neither is it falsified. It is still waiting on the first skulltula actually killed: the one in Kokiri Forest sets a bit at `0x8011B478` (`GS Soil` → bit 0, `GS Night Child` → bit 1), the Deku Tree ones go to `0x8011B46C`.

### The overlay, live over the real save

The 13 August session with the overlay running in OBS gave **96 out of 4,995**, and it confirms with data two things that until then were derivations:

- **MM's xflags are read with OoT running.** `TERMINA_FIELD 28/277`, `GROTTOS 26/450`, `SOUTHERN_SWAMP 12/45`, `MILK_ROAD`, `CLOCK_TOWN_SOUTH`… all in their colour, while the active game is Ocarina. That validates the **rebasing of the `custom` anchor** and, with it, MM's custom save `+0x380`.
- The **confidence measure** stayed above the threshold for the whole session, which is the continuous check that the bases are where we think they are.

Still unconfirmed, and still waiting on the same gesture: `gsFlags` until a gold skulltula is actually killed (the Kokiri Forest ones were still on the pending list), and MM's `perm` until a chest is opened in Termina.

### Live validation: 88 checks on a real save

The live read of 13 August, with the save already in Termina, gave **88 completed checks out of 5,963**, and it is the best validation the project has:

- **All 88 names exist in the spoiler, all 88.** Not one orphan, not one invented name.
- **The items that add up match the real inventory**: Powder Keg, Blast Mask, Hover Boots, Bombchu Bag, Cojiro, Minuet of Forest, Bolero of Fire, Zelda's Lullaby, Progressive Sword, Deku Stick Upgrade…
- **MM's xflags fire on real progress** and grouped where the player has been: Termina Field, Southern Swamp, Milk Road, Clock Town South, Termina Field's cow grotto. If the mapping were wrong there would be stray bits in scenes never entered, which is exactly what does not happen.
- **`Initial Song of Healing` shows as marked**, and that is MM's custom save (`mm.npc` byte 12 bit 0) read live from the derived base `+0x380`. It matches what was already visible in the `.fla`. The `+0x380` stops being just structure arithmetic.
- **MM's `perm` is still unconfirmed**: no MM chest has been opened yet. OoT's is confirmed, with the four in Mido's House and the Kokiri Sword Chest.

> A presentation gotcha that came out of this: scene ids **repeat across games** (0x2D is `KOKIRI_SHOP` in OoT and `TERMINA_FIELD` in MM). Grouping by `scene_id` without the game merges two different scenes under one heading. `cmd_checks` already groups by `(game, scene_id, scene)` and prefixes `[OOT]` / `[MM]`.

### Mapping status: 5963 of 6043

| Destination | Checks | Resolved | Status |
|---|---|---|---|
| `xflags` | 4751 | 4751 | ✅ ROM tables, verified against the save |
| `scene` (chest + collectible) | 562 | 562 | ✅ OoT measured; MM derived, pending a chest |
| `gs_flags` | 144 | 144 | ✅ `gsFlags[6]`; derived, pending a skulltula |
| `custom` (shop, scrub, sr, npc, fish) | 539 | 506 | the 33 `fish` (`caughtFishFlags`) are missing |
| `mm_stray_fairy` | 29 | 0 | still to be located |
| `cow_flags` | 18 | 0 | `SAVE_EXTRA_RECORD(u32, 9)`, the macro has to be resolved |

`mkchecks.py --rom <seed.z64> --spoiler <spoiler.txt>` is what produces this table.

### What is missing from the check system

**80 checks are left, 1.3%**, and the three blocks are the kind that cost an experiment in the real save. It is not worth going at them head-on: pick them up opportunistically, with a savestate, when you happen to pass one.

- **Confirm in game** MM's `perm` and OoT's `gsFlags`. It is not work, it is looking at a byte when the moment comes; the concrete predictions are above.
- **`caughtFishFlags`** (33): it is in `SharedCustomSave`, behind both custom saves and the soul blocks. It is one more piece of structure arithmetic, but with bitfields in the way.
- **`cow_flags`** (18): `SAVE_EXTRA_RECORD(u32, 9)`, we need to see what that macro is.
- **MM's stray fairies** (29).
- **P4 and P6**, the multiworld side.
- Validate with a second seed. There are three new things to validate: that the xflag tables are at the same VROMs (they should be: they are `custom.h` constants and do not depend on the seed), that MM's custom save `+0x380` holds, and `perm`'s `+0xF0`.

## The overlay

`python ootmm.py overlay` starts three things: a thread that polls memory through `tracker.lua`, an HTTP server on `127.0.0.1:8013`, and a window of its own in app mode (Edge or Chrome's `--app=`, with no browser chrome).

For OBS there are two routes and both come from there: capture that window, or point a **Browser Source** at the same URL, which is the one to prefer because it allows a transparent background and clean scaling.

### Compressed ROMs, and why `checks.json` belongs to one specific version

It came up when starting the overlay with another seed, one from May: `ValueError: table 0x80b0f00 is compressed`. Two assumptions of mine that do not hold in general, both in the same place:

- **OoTMM can generate the seed compressed**, and then the DMA entries carry Yaz0.
- **The six xflag tables can share a single DMA entry**, instead of having one each. You have to keep the slice that starts at the requested VROM, not the whole file.

Both now live in `rom.py`, used by `mkchecks.py` and `mkicons.py`.

**But fixing it uncovered what was underneath**: with that ROM the tables are read and out come **3,414 of 4,751 xflags with an impossible bit**, negative ones included. The `custom.h` VROMs are constants **of v32.0**, so with a seed from another version they point at data that is not the data.

And the serious part: `mkchecks` wrote `checks.json` anyway. Since the overlay triggers this on its own at startup, it was clobbering a good file with a useless one without anyone noticing. Now there is a barrier: if more than 2% of the xflags give an impossible bit, it **aborts without touching anything** and says why, and the overlay warns that the checks it is about to use come from a different ROM.

> The **icons do work across versions**: `icon_item_static` is at the same index of OoT's dmadata in all of them, so `mkicons.py` works with any seed. What is tied to v32.0 are the xflag tables.

### Nothing has to be told to it: the ROM is detected on its own

`discover.py`. Before, you had to pass `--rom` and `--spoiler` by hand and remember to regenerate `checks.json` and `icons.json` when changing seed. Now it falls out of what the emulator already knows.

**The key is the save folder's name.** Project64 saves into `Save/OOT+MM COMBO-<hash>/`, and that hash is the **MD5 of the ROM with its 4-byte words swapped**, which is the order the emulator stores it in internally. Measured, not assumed: the MD5 of the file as it is gives `5DFC2740…` and matches nothing; the one of the swap4 version gives `7EE74762…`, which is exactly this seed's folder.

From there comes a chain that does not depend on guessing:

1. the most recently written save folder says **which seed you are playing**
2. its hash identifies the ROM **unambiguously**
3. that ROM is looked up among `Project64.cfg`'s recent ones (`Recent Rom N`)
4. the spoiler is taken from next to the ROM: for `OoTMM-<id>.z64` it prefers `OoTMM-Spoiler-<id>.txt`

If nothing matches it falls back to `Recent Rom 0`, which is the last one opened. `checks.json` and `icons.json` record which ROM they came from, so they regenerate themselves when you change seed and are left alone when they are already current. The hash is cached by `(path, mtime, size)` so as not to re-read 64 MB on every start.

All of it can be bypassed with explicit `--rom` / `--spoiler`, or turned off with `--no-auto`.

> It was looked into first whether Lua could give the ROM's path directly, which is the first thing anyone tries. The P64-EM API that `tracker.lua` uses only exposes `memory`, `socket`, `binary` and `print`, and there is no documentation; the emulator's config turned out to be a better route and, above all, one verifiable without having the emulator open.

### One URL per panel

`/` is the **director view**, with everything together, for the player's own monitor. Each panel is also served on its own at `/p/<name>`, with no chrome and filling the whole source, so it can be added as its own Browser Source and placed wherever you like. That way the streamer shows only what they care about.

| Panel | URL |
|---|---|
| Summary and counts | `/p/summary` |
| Progress by region | `/p/regions` |
| Item grid | `/p/items` |
| Activity feed | `/p/activity` |
| Remaining in the area | `/p/remaining` |

> The old Spanish names (`/p/resumen`, `/p/regiones`, `/p/actividad`,
> `/p/pendientes`) **still respond**, with a 301 to the new one and keeping the
> query string. They are what any Browser Source set up before the language
> change points at, and renaming a URL breaks an OBS scene silently: the source
> goes blank and does not say why.

It is a single page: the server serves the same `overlay.html` for `/` and for `/p/*`, and the page looks at its own path, removes the blocks that are not its own from the DOM and keeps one. The director view carries a dropdown that generates the panels' URLs with the options already applied, and a copy button for each.

### Filtering out the junk

`?junk=hide`, or the **Show** selector in the director view. It leaves progress-by-region and the remaining list with **only what matters**: from 4,995 checks down to **612**, and an area's remaining list drops from 73 to 2.

Two things had to be solved, both written down at the time in `BACKLOG.md`:

- **The filter goes by the item inside, not by the location's type.** With the pool shuffled, a patch of grass can hold the Hover Boots; in this save `Kokiri Forest Rupee Child 2` held a Swamp Skulltula Token.
- **And therefore it needs the spoiler**, which clashed with `?spoiler=off`. It is solved by **classifying on the server**: the page receives a `junk: true/false` per check and filters on that, without ever seeing the item's name. With no spoiler the filter disables itself instead of leaving everything at zero.

The rule goes by name, not by frequency, and that was a measured decision: `Gold Skulltula Token` appears **100 times** and is not junk, nor are the Stray Fairies. Cross-referencing the list with the spoiler turned up two more traps:

- **Puzzle rupees** come in parentheses —`Silver Rupee (Shadow Temple - Scythe)`— and **are not junk**: they can be needed to progress. The rupee pattern had to become exact and suffix-free.
- **Ammo can carry the game behind it** (`5 Arrows (OoT)`), and was slipping through as important.

### The selectors apply to what you are looking at

The director view's three selectors —Show, Spoiler, Background— only called
`renderUrls()`. They composed the panels' links with the option applied, but
**the view right next to them stayed the same**: you switched to "only what
matters" and the numbers did not move. That does not read as "this control is
for something else", it reads as a broken filter, and rightly so.

Now they apply in place. Three small changes and one that was not obvious:

- The options were `const`s read from the URL once. They are **state**, not
  constants: the URL only says how it starts up.
- **Painting is separate from polling.** `tick()` did both, so the only way to
  repaint was to wait for the next poll. Split into `tick()` (fetches and
  stores into `lastState`) and `render(s)`, which is what `applyOpts()` calls
  to repaint instantly.
- The chosen option is written into the address bar with
  `history.replaceState`, so **reloading does not lose it** and the URL at the
  top works just like the panels'.

> **The one that was not obvious: applying the background live hid the controls
> themselves.** There is a `body.chroma-none .director { display: none }` —
> deliberate, because `/?chroma=green` is used to capture the whole window and
> the controls get in the way there. But applied live, choosing "transparent"
> made the very selector you had just used disappear, with no way back short of
> editing the URL by hand. It is told apart by origin: if the background comes
> from the URL the director hides as always; if you have just touched it, the
> `body` carries `opts-live` and it stays.

While at it, the same class of trap one step down: **with no spoiler loaded the
junk filter cannot work** (the server's `can_filter`) and it disabled itself,
silently. Now the option comes out disabled and says why, "only what matters
(needs the spoiler)" — and there is a button to load it without restarting,
which is the next section.

Verified by driving Edge over CDP against the real overlay served on top of
`ram-en-oot.bin` —the real `Tracker`, the real `checks.json` and the real
spoiler, with a `link` that reads from the dump instead of `tracker.lua`—:

| | unfiltered | "only what matters" |
|---|---|---|
| summary | 18 / 4,995 | 4 / 612 |
| regions with data | 4 | 3 |
| remaining in the area | 25 (capped) | 2 |

`spoiler=full` makes the `→ item` appear in the remaining list and `off`
removes it, the three options survive a reload, `/?chroma=green` still hides
the director, the standalone panels do not change and the console comes out
clean.

### Loading the spoiler from the page

The spoiler was only loaded at startup, with `--spoiler` or detected next to
the ROM. If it did not turn up, you were left with no junk filter and no way to
know what is in the remaining checks **until you restarted the overlay**, which
mid-stream is not an option. Now there is a button in the director view:
`POST /spoiler` with the file's contents, and `Tracker.set_spoiler` recomputes
on the fly the only things that depend on it —the junk classification and the
per-region totals—.

**The contents are uploaded, not the path.** An endpoint that opened whatever
path it was handed would be an arbitrary file read, and the server can end up
listening outside `127.0.0.1` with `--http-host`.

Loading the wrong spoiler is worse than loading none: the names half-match and
the filter claims things are junk that are not. Three numbers guard against it,
and the third one came out of testing it:

| Check | What it catches |
|---|---|
| the header's `Version:` against `checks.json`'s | a different OoTMM version |
| how many of its locations exist in `checks.json` | not a spoiler, or from another version |
| **coverage**: how many of our checks it names | another seed, or a spoiler that is not enough to classify with |

> **Coverage was not planned for and it is the one that matters.** The spoiler
> from another v32.0 seed brought 980 locations and **all 980 matched**, so it
> sailed through the first two barriers. But it covered 934 of 4,995 checks,
> and about what it does not name `is_junk` can say nothing: the filter was
> left unclassified and "only what matters" went on showing all 4,995 — a
> filter that does not filter, which is exactly the bug that had just been
> fixed above. It is not rejected, because a seed with different settings
> genuinely has fewer locations, but it warns. The right one covers 4,939 of
> 4,995.

Measured with the overlay served over the dump, uploading four files over CDP
(`DOM.setFileInputFiles`):

| File | Result |
|---|---|
| v30.1 spoiler | rejected, "it is from v30.1 and checks.json is from v32.0" |
| `README.md` | rejected, "there are no locations in there" |
| empty | rejected |
| another v32.0 seed | accepted **with a warning**: covers 934 / 4,995 |
| the seed's own | accepted: 5,018 locations, covers 4,939 / 4,995 |

And behind it, what this was for: with the right one loaded, "only what
matters" goes from 18 / 4,995 to **4 / 612** and the area's remaining from 25
to 2, without restarting anything. A rejection does not touch the state: the
option stays disabled.

**It applies itself, because loading it and having nothing change is the same
trap.** The spoiler is precisely what makes it possible to filter junk and to
say what is in each remaining check, so loading it turns both on —`junk=hide`
and `spoiler=full`— and the selectors move to match, which is the
acknowledgement. The **apply to the panels** checkbox turns that off for anyone
who would rather load it without anything moving.

Measured, with the dump's freshly started save:

| | before | on loading |
|---|---|---|
| summary | 18 / 4,995 | 4 / 612 |
| remaining in Kokiri Forest | 25 (capped) | 2 |
| first remaining | `GS Soil`, with no item | `Grass Adult 07 → Goron Lullaby` |
| regions with data | 4 | 3 |

With the box unchecked, the four numbers stay as they were and only the message
changes.

> **And a caution that came out of this.** `spoiler=full` also goes into the
> panels' links, and those go to OBS: showing what is left is a legitimate
> option on the player's own monitor, but in a capture source the audience sees
> it. The director view warns when the level is `full`.

### One game per overlay

`?game=oot` or `?game=mm` leaves the overlay with a single game, so the Ocarina tracker and the Majora one can be set up separately. It is not just a visual filter on the grid: it affects everything.

- The **summary's percentage** becomes that game's, not both together — there are per-game totals in the state (`totals`, `done_by_game`).
- The **badge** identifies the overlay instead of the active game, and the meter takes that game's colour.
- The **feed** is filtered by game (each entry carries its own).
- The **remaining** are those of the area you are in, so an overlay filtered to the other game says "you are currently in …" instead of going mute.
- With a filter on, the repeated game heading inside the panel is dropped, because the title already says it.

The director view generates these URLs too: `/p/items?game=oot`, `/p/items?game=mm`, and the same for regions and summary.

### The icons come out of the ROM

`mkicons.py --rom <seed.z64>` produces `icons.png` and `icons.json`. Nothing comes from outside: the icons are the game's own.

- **`icon_item_static`** is file 8 of OoT's `dmadata` (at `0x7430`, per `combo/dma.h`), uncompressed, with 32×32 RGBA32 icons. **The icon's index is the item's id**, so `items.h` names them all and there is no counting positions by eye.
- The valid range was **measured**, not assumed: an icon is marked valid if its four corners are transparent and it has between 80 and 1000 opaque pixels, and out comes a clean run `0x00..0x58` followed by noise.
- Medallions and stones (`0x66..0x79`) are not there but in **`icon_item_24_static`** (file 9), at 24×24, where the index is `id − 0x66`. They are centred in a 32-pixel cell.

For the `item:` slots no table is needed: the value read **is already the id**, i.e. the index into the sheet. One is needed for what is boolean or a level, and for knowing which icon to show **greyed out**: an empty slot reads `0xFF` and does not say which item belonged there, so without that everything you do not have yet would come out as text, which is the opposite of what a grid is for.

### MM's icons: a CmpDma archive

I got this badly wrong and it is worth writing down why, which is more useful than the result.

I assumed they were not there because I searched for **images** and what is there is **compressed data**. MM's files 8 and 9 (`icon_item_static`, `icon_item_24_static`) really are marked as absent in the dmadata, and I took that as proof. But MM does not load them through the dmadata: it uses `CmpDma_LoadFile`, and the art lives in a **CmpDma archive**, a table of offsets followed by the files, **each one compressed separately with Yaz0**. Raw, that does not look like an icon in any format, so no pixel sweep —RGBA32, RGBA16, CI4, CI8, sliding window— could have found it.

What solved it was not more scanning, but **reading the documentation**: MM's decomp (`zeldaret/mm`) names the asset (`icon_item_static_yar`), the mechanism (`sys_cmpdma.c`) and the drawing format (`G_IM_FMT_RGBA, G_IM_SIZ_32b`).

The format, from `src/code/sys_cmpdma.c`:

```
u32 dataStart      the table's size; there are dataStart/4 - 1 files
u32 offs[...]      each file's start, relative to seg + dataStart
                   (file 0 starts at 0; its end is offs[1])
```

And `CmpDma_LoadFile(segment, id, ...)` uses **the item's id as the index**, so entry `i` is the icon for `ITEM_MM_* == i`. Verified: 0x32 is the Deku Mask, and from 0x32 to 0x49 come the 24 masks in order.

The archive is not in a fixed place, so it is located **by its shape**: start from every `Yaz0` block in the ROM and test whether `dataStart` bytes earlier there is a header pointing exactly there. Of the seven CmpDma archives that turn up, take the one with the most entries of exactly 4096 bytes, which is the icons': **98**.

It is in the combined ROM just as it is in the base game, so it comes out of each person's own seed.

**The lesson**: "I cannot find it" is not "it is not there", and when something must exist —the game draws it— what is failing is the search method. Before the fifth pass of heuristics, read the format's documentation.

**Where the index stops holding.** The archive is packed and **the twelve songs have no entry**: the game draws them all with a single note texture that lives in `code`, not in the archive (`gItemIconSongNoteTex`). Since they are missing, everything after them is shifted — entry `0x61` is the Bombers' Notebook, not the Sonata. That is why the map stops at `0x60` (`ITEM_REMAINS_TWINMOLD`), which is the last one that lines up. It was discovered when trying to use the note: colour smears came out instead of notes.

With that, out of the ROM come the 24 masks, the four boss remains and the rest of Termina's items. Song notes are still drawn, with the game's colours, and in the shape of the menu's: a **single eighth note** —head tilted down to the left, stem to the right and a flag—, not the double beamed one that was there at first.

### What is in each panel, and why

- **MM's equipment.** MM has no strength and no scale: those fields are in the structure because `MmUpgrades` copies OoT's, but the game does not use them. In their place goes the **sword** (Kokiri, Razor, Gilded) and **shield** (Hero's, Mirror) progression, which come from the nibbles of `MmItemEquips` at `base+0x64`. Verified on both dumps: `0x0010` in each, i.e. shield 1 and no sword, which is how a seed starts.
- **MM's empty slots now come out greyed.** The slot order comes from the decomp (`z64item.h`) and matches the item ids: slot 0 is the ocarina, 1 the bow, 8 the sticks. The last six are bottles and all show the empty one.
- **Stick and nut upgrades removed**, in both games: they took up a cell and do not say much.
- **Zelda's Lullaby carries a triforce** instead of a note, which tells it apart from the rest at a glance.
- **A feather for the Song of Soaring**: the up arrow did not read.
- **MM's progress is reordered.** It comes out in bit order, which mixes remains and songs and leaves the notebook and the Goron's half loose at the end. With the four remains, the notebook and the half placed in the first row, the twelve songs take up **two full rows** and in the game's canonical order, that of their ids: Sonata, Goron Lullaby, Bossa Nova, Elegy, Oath, Saria · Time, Healing, Epona, Soaring, Storms, Sun.

Along the way a **Yaz0** decompressor got written in 30 lines, needed because half of MM comes compressed.

### What the ROM does not carry gets drawn

The songs are not items and have no icon anywhere in the ROM, so they are drawn as SVG on a 24×24 canvas, scaling with the cell.

**The six warp songs carry their colour, and the colour is the data**: forest green, fire red, water blue, spirit orange, shadow purple, light yellow. They are the game's, not chosen by taste. The other six the game paints white, so there what distinguishes them is the symbol: a bolt for storms, a horseshoe for Epona, a leaf for Saria, a sun, an hourglass for time. In MM there are also a heart (healing), an up arrow (soaring), a wave (New Wave), a notebook and a skull for each boss's remains, this last one in the boss's colour.

> **MM's masks are in the ROM after all, and they are extracted.** I called it impossible after several sweeps and I was wrong: the user pushed back with the correct argument —the game draws them in its menu, therefore the art is there— and was right. See the section below. The drawn silhouettes are still in the code as a fallback, in case one day the archive is not found.

### Initials that can be told apart

With two-letter labels they clashed: `RG` for "Remains of Goht" and "Remains of Gyorg", four `SS` among MM's songs, three `GM` among the masks.

The rule is not to extend the last word —in the masks it is always "Mask", and extending it gives `KafMas` / `KamMas`, long and unreadable— but to **strip what all the clashing ones share** and disambiguate with what is left: `Kaf`, `Kam`, `Gib`, `Gar`, `Gia`. A cap of four characters, and the font size drops with length so it does not spill out of the cell.

### Hand-placed images

The `icons/` folder, with its own `README.md`. Whatever you drop there overrides the ROM's icon, and it is meant for covering what the ROM does not have — MM's own masks. **Nothing is downloaded**: the user supplies the images.

The file name is matched normalised, so both the name the overlay shows (`deku-mask.png`) and `items.h`'s (`mask-deku.png`) work, and `icons/mm/` only applies to Majora while `icons/` works for both.

Two details that came up while testing it, both with the same kind of cause — a path and a name are not the same written down as in memory:

- **The apostrophe is dropped, it does not split.** `Garo's Mask` normalised to `garo-s-mask`, so the file `garos-mask.png` —which is how anyone would write it— did not match.
- **The URL has to be decoded.** A file with spaces arrives as `%20` and did not match what the scan had stored.

The server only serves files that were present when the folder was scanned, comparing the exact path against that list, so nothing outside can be requested by URL: verified that `/usericon/../overlay.py` gives a 404.

### The grid has the shape of the game's menu

No layout had to be found anywhere: **it is already in the data we read**.

| Grid | Columns | Where it comes from |
|---|---|---|
| Items | 6 | `items[24]` and the game's screen is 6 columns wide: the array **is** the grid, slot by slot |
| Masks (MM) | 6 | MM stores 48 slots: the first 24 are items and the next 24 masks, another 6×4 page |
| Equipment | 3 | `OotEquipment`'s nibbles are swords, shields, tunics and boots, three of each |
| Progress | 6 | with 6 columns OoT's 24 bits fall into their rows on their own: medallions, warp songs, ocarina songs, and stones with the rest |

The consequence is that **the rows mean something**: a row of swords, a row of shields, a row of medallions. And the empty slots stay in place, because they are part of the menu's shape and not noise — an unnamed empty slot is painted as a free cell, with no label.

### The number in the corner

`ammo[]` is indexed by inventory slot just like `items[]`, so the number falls in the same cell the game paints it in: 10 deku sticks, 20 nuts, 40 arrows. On upgrades the number is the level. On a boolean nothing is shown — a "1" on top of the icon only gets in the way.

> **The bug that asking for it uncovered.** In an inventory slot, empty is `0xFF`, and **0 is a legitimate item id**: Deku Stick in OoT, Ocarina of Time in MM. The "I have it" check treated 0 as empty, which is the natural thing in every other field, so **the first slot of both grids came out greyed forever**. It looked like one more grey cell among twenty-four, and it only surfaced when cross-referencing the ammo counts against the raw data.

### The bridge from names to MM icons

Matching by exact name falls short: the two games order the words differently (`MASK_KEATON` against `KEATON_MASK`) and insert linking words (`OCARINA_OF_TIME` against `OCARINA_TIME`). Comparing the **set of words** without the linking ones, the bridge goes from 59 to 74 ids: in come the Keaton, Goron, Zora and Truth masks, the Ocarina of Time, the Lens and the Hero's Shield.

What is **not** done is fuzzy matching. It would pair `BOMBS_10` with `BOMBCHU_10` and `LENS_OF_TRUTH` with `MASK_OF_TRUTH`, which are different items; anything that does not fall out of the word rule goes in an explicit alias table.

### The grid does not scroll

A scrollbar in an OBS source is a visible defect, and it also shifts the composition as the run goes on. But it is not enough for it merely to fit either: **the groups sit side by side when there is width**. Stacked in a single column the height dominates, the cells end up tiny and half the source is left empty — which is exactly what showed up when it was set up in OBS for real. Below 420 px of width they stack again, because in a narrow column putting them in parallel gives a tall thin mess.

With that, the cell **is made as large as possible** across three steps, from the most faithful to the one that always fits:

1. the game's layout, with fixed columns, trying from 52 px downwards
2. the same but compact: the headers, which in a narrow column weigh more than the cells, are shrunk
3. free flow: the menu's shape is lost but it fits in a narrow column

With fixed columns it can also overflow **horizontally**, not just vertically, so the check looks at both. The icon scales with the cell via `background-size`, with no fixed sizes.

Measured: a dedicated 400×600 panel fits exactly (`scrollHeight 440 = clientHeight 440`). The **full view** with both games does not fit even at the third step and scrolls; that is acceptable because it is the control surface, not a capture source — for capturing there are the standalone panels.

### Spoiler levels

**What you have already picked up is not a spoiler** —whoever is watching saw you pick it up— but what is in an unopened location is. That is why the default level shows what has been obtained and stays quiet about what is pending.

| `?spoiler=` | Feed | Remaining |
|---|---|---|
| `off` | only the check's name | only the name |
| `item` *(default)* | item obtained | only the name |
| `full` | item obtained | the item inside |

| `?chroma=` | What for |
|---|---|
| `none` | transparent background, for a Browser Source |
| `green` | green chroma `#00b140`, for window capture |

**Transparent means transparent.** For a while `chroma=none` left the `body` transparent but the cards at **82% opacity**, put there for legibility: the result was not transparent, it was a dark panel. Now the card paints nothing, the text leans on a shadow to stay readable over any scene, and a minimal veil is left behind the grids so the greyed-out icons do not get lost on light backgrounds.

Measured two ways: the computed styles give `rgba(0, 0, 0, 0)` on `body` and on `.card`, and a capture with alpha comes out **94% transparent**, 5% translucent and 1% opaque.

> **It only looks transparent in OBS.** In a browser window —including the one `ootmm.py overlay` opens— there will always be a dark background behind it: the browser's canvas, which with `color-scheme: dark` is black. The transparency is only composited by the Browser Source. This threw me off while checking it too: the headless captures came out dark even though the page was transparent.

### (solved) The scene we read now is where you are

**Done 14 Aug 2026.** The remaining panel came out of the **save context**, and
that is not where the player is:

```
OoT:  info.sceneId              ASSERT_OFFSET(OotSave, info.sceneId, 0x66)
MM:   playerData.savedSceneNum  info+0x26 -> base+0x42
```

**The defect was in both games, not just MM.** It was measured against both
dumps, and OoT's —which the backlog had down as "better"— was failing too:

| Dump | PlayState | Save context | |
|---|---|---|---|
| OoT | `0x2D` KOKIRI_SHOP | `0x55` KOKIRI_FOREST | one scene behind |
| MM | `0x6F` CLOCK_TOWN_SOUTH | `0x08` | not even the previous one |

That is, the player was inside the Kokiri shop and the overlay was showing them
what was left in the forest.

**Where the live value is.** OoTMM keeps `PlayState* gPlay` (`combo.h:186`),
so the structure is the decomp's and its offsets come from the repo's own
headers, with nothing hunted:

```
GameState (0xA4, combo/game_state.h)
  +0x00 gfxCtx*  +0x04 main  +0x08 destroy  +0x0C nextGameStateInit
  +0x10 nextGameStateSize  +0x14 input[4]  +0x74 tha  +0x84 unk[0x17]
  +0x9B running(u8)  +0x9C frameCount
PlayState (combo/{oot,mm}/play.h)
  +0xA4 sceneId u16     +0xB0 sceneSegment*
  roomCtx.curRoom.num s8:  OoT +0x11CBC   MM +0x186E0
```

> **The trap: `running` is at `+0x9B`, not `+0x98`.** `tha` ends at `0x84` and
> `unk_84` is `0x17` long, which leaves it at an odd address. With the rounded
> offset the sweep finds **absolutely nothing**, and there is no other symptom
> to give it away: it looks as though the PlayState is not there.

**`gPlay` did not have to be located.** The game state is allocated once per
boot and always lands in the same place, which turns out to be the one the
practice tools for both games have been using forever:

```
OoT  0x801C84A0      MM  0x803E6B20
```

Both dumps confirm them, with a bonus test that works as a signature:
**`main` points inside OoTMM's payload** —`0x80430A90` in OoT, `0x80750488` in
MM, against `PAYLOAD_RAM` `0x80400000` and `0x80720000` from `combo/defs.h`—,
i.e. it is the patched `Play_Main`. It is not just any address that happens to
look right: it is this build's.

**The fallback sweep's funnel**, in case the known address stops holding. Eight
filters that cost nothing, and over the two dumps they leave **exactly one
candidate** each:

| Filter | OoT | MM |
|---|---|---|
| three pointers into RDRAM | 10232 | 11058 |
| and distinct from each other | 5511 | 6179 |
| `nextGameStateInit` and `Size` zero | 148 | 132 |
| `running == 1` | 4 | 3 |
| `frameCount` < 2²⁸ | 2 | 3 |
| plausible `sceneId` | 1 | 2 |
| `sceneSegment` is a pointer | 1 | 1 |
| `curRoom.num` between −1 and 30 | **1** | **1** |

The "distinct from each other" one is what removes the stale buffers full of a
single repeated pointer, which sail through everything else.

**The sweep is on a countdown, and that is not a detail.** It reads the 8 MB of
RDRAM, and unchecked it would fire on every poll while you are on the title
screen or changing scene — which is exactly the problem `locate_saves` already
had. `PLAY_RESCAN_SECONDS` limits it to one every ten seconds. In practice it
runs **zero times**: the known address hits first try and the poll costs 12
reads, 0.02 MB.

If there is no PlayState —title screen, boot— it falls back to the old save
context, and the state carries `live: false` so it is known which of the two is
in use.

### The distance to the custom save belongs to the version, not to the project

**Done 14 Aug 2026**, from the dump of the experimental seed `dev-542a121`.
Symptom: per-region progress and activity empty, and pending checks not being
marked when picked up, while the remaining panel worked fine.

The cause, measured: **in the dev build the whole block has moved**.

| | MM's buffer | `gSharedCustomSave` | distance |
|---|---|---|---|
| v32.0 | `0x8044BE18` | `0x8044B570` | `0x8A8` |
| dev-542a121 | `0x8044CF78` | `0x8044C6A0` | **`0x8D8`** |

And with MM running, measured afterwards on `ram-dev-mm.bin`:

| | OoT's buffer | `gSharedCustomSave` | distance |
|---|---|---|---|
| v32.0 | `0x8076C4F0` | `0x8076BC50` | `0x8A0` |
| dev-542a121 | `0x8076D400` | `0x8076CB30` | **`0x8D0`** |

Both sides grew by **exactly `0x30`**, which is the prediction that followed
from the block getting fatter at the tail, and it settles the matter: with the
old constant that dump gave confidence **0.167** and 4 garbage checks; with
`0x8D0`, **1.000** and the 18 real ones.

MM's base moved up by `0x1160` —which `locate_saves` absorbs on its own,
because it goes by signature— but the distance to the custom save grew by
`0x30`, and that **was a constant**. With it the anchor landed at
`0x8044C6D0`, `0x30` too early: confidence **0.077**, below the threshold, so
the whole `custom` anchor was discarded and with it **4,751 xflags and 506
bitmaps**. Exactly the symptom.

> What sits in between is the other game's save, so the distance measures the
> size of a generator structure. There is no reason for it not to change: the
> odd part is that it held for as long as it did.

Now it is **measured**: if the known distances do not validate, a window of
`0x800`–`0x1000` backwards from the inactive game's buffer is swept. A single
read per game covers the whole window and the candidates are scored locally
—reading each one over the link would be a megabyte per poll— and the result is
cached as a distance, so the sweep runs once per session. With v32.0 seeds it
never runs: the constant is right.

**Three things were needed for it to choose correctly, and all three failed first.**

1. **`bits > 0`, not `bits >= 0`.** An address that lands in zeros gives
   confidence 1.0 vacuously. With `best_bits` starting at −1, a candidate with
   **zero bits** validated, won, and the sweep never got to run. It is the same
   trap already documented further up, biting for the third time.
2. **Confidence first, bits as the tie-breaker.** Sorting by bit count,
   `0x8044C754` won —14 bits at confidence 0.929— over the right one, 7 bits at
   1.000. The overlay went on to report progress in **Stone Tower and Spirit
   Temple in a save that had not left Link's House**. Confidence is what says
   "this is what I think it is"; the bits only break ties between equally
   credible addresses.
3. **Alignment to 16.** `gSharedCustomSave` is a global with `ALIGNED(16)`, and
   the three measured bases satisfy it (`…B570`, `…BC50`, `…C6A0`). Sweeping in
   steps of 4, `0x8044C6B4` won, which **also gave confidence 1.0 with the same
   7 bits** — but mapped to Lair Gohma and Zora River instead of Link's House
   and Kokiri Forest. With few bits set, confidence alone is not enough;
   aligning removes three quarters of the candidates and leaves the right one
   first, 1.000 against 0.857 for the next.

**Two more bugs, which turned up when crossing to Majora with the dev seed.**

> **First, a false alarm worth not repeating.** On seeing
> `MOUNTAIN_VILLAGE_WINTER 6/25` in a save that had only just reached Termina I
> wrote it off as garbage, reasoning from vanilla MM progression: that area is
> past Snowhead. **It was correct.** The player had picked up the item
> `Owl Statue (Mountain Village)` in Kokiri Forest's grass, warped there,
> activated the statue and broke five snowballs — and that is exactly the six
> checks. In a randomizer with the owls in the pool there is no late area.
> **Coherent data is not suspicious for being unexpected**; what to look at is
> the live scene and the item each check gives, which here said everything.
>
> The two bugs below are real and came out of looking at that closely, but the
> symptom that uncovered them was not a symptom.

- **Constants cannot win by being good enough.** Accepting the first candidate
  that passed the threshold meant that, on a version that moves the structure, a
  skewed address could pass —its few bits still land on *some* known check— and
  **the search that would have found the right one never got to run**. Now it
  is always measured, and the constant only orders where to start looking. On
  v32.0 the sweep returns exactly `0x8A8` and `0x8A0`, so it validates them
  instead of assuming them.
- **The distance is cached per INACTIVE game, which is what it hangs off.** The
  one measured while MM was idle says nothing when MM is the one running: its
  buffer has moved elsewhere. Reusing it on crossing put the anchor at an
  address with no meaning at all. And the sweep only looks at the inactive
  game's side, because an address hanging off the running one can only win by
  coincidence.

Crossing between games also rearms the sweep's countdown: it is a legitimate
reason to look again, and without it the overlay would read the wrong anchor
for ten seconds right after the switch.

**And the underlying hole, which was the real one.** All of this broke
silently, and that is worse than breaking. The confidence measure's second
signal —"there were bits and now there are none"— **needs to have seen bits
earlier in the session** (`_xflag_peak > 8`), so starting the overlay with the
base already wrong it never got armed: the overlay showed an empty save,
calmly, and marked as trustworthy. Now there is a second way into the same
alarm:

> **There are scene checks done and not a single xflag.** The scene ones hang
> off a different anchor, located by signature, so they are trustworthy **from
> the very first poll**, which is exactly when the other signal cannot fire.

With a guard against crying wolf: a seed generated **without xsanity** has no
xflags, and there "zero xflags" is not an anomaly, it is the truth. It is told
apart because those checks only carry `item` if they are in the ROM's placement
table; without xsanity they do not, and the alarm disables itself. And the
threshold is 3 scene checks, not 1, so a single chest in a new save does not
trip it.

Measured on `ram-en-mm.bin`, forcing the base into a region of zeros:

| Scenario | confidence | does it warn? |
|---|---|---|
| bad base + scene progress + xsanity | **0.000** | **yes** |
| the same, but a seed without xsanity | 1.000 | no, and that is right |
| everything fine, with progress | 1.000 | no |

Before the change, the first case gave confidence 1.0 and `trusted: true`.

**The check that closes it**, and it is two independent routes. Locating by the
bit pattern of that save's `.fla` gives `0x8044C6A0` as the only address with
confidence 1.000; and with the overlay reading there, the regions that come out
are `LINK_HOUSE 1/1`, `KOKIRI_FOREST 5/85`, `HYRULE_FIELD 1/177` — the same 7
checks as the `.fla`, scene by scene.

> And along the way what looked like the obvious cause got ruled out: **the dev
> build's xflag tables are identical to v32.0's**, 0 differences across the
> 4,751 `bitpos` values and across every address. The problem was never there.

### Two menus, not one

The settings lived **inside** the "Capture in OBS" disclosure, and read like a
footnote to a capture guide. They are different things: the options are touched
while you play, the OBS URLs are set up once and never opened again. Now there
are two:

| | |
|---|---|
| **Display options** | open by default: spoiler, show, background, load spoiler |
| **Capture in OBS** | collapsed: the explanation and the URL table |

Three decisions that are not about arranging boxes:

- **The spoiler switch is in neither of them.** It is the one you reach for in a
  hurry, live, and behind a disclosure it does not do its job —a comment in the
  code already said so, and it was sitting inside one—. It goes loose at the
  top, always visible.
- **The `spoiler=full` warning moves to the options menu**, which is where the
  decision is made. It was next to the URL table ("the links below reveal…"),
  and with the OBS menu collapsed nobody saw it.
- **The hiding rules move to a `.dirtools` wrapper.** They named `.director`,
  and split in two they would have had to enumerate each piece — with a
  guarantee that the next one would be forgotten. It is what keeps the awkward
  case above working: choosing "transparent" live cannot hide the control you
  just used to choose it. Verified that it still behaves that way.

### The "unknown area" flicker, and the missing latch

Reported on 14 Aug while crossing Termina Field: the panel's title kept going
back and forth between `Remaining in TERMINA_FIELD` and `Remaining here /
unknown area`.

Reproduced and explained. **A scene transition rewrites the `PlayState`**, so
for a poll or two it stops validating —`running` at 0, pointers half set—. And
the code then fell back to the save context, which in MM gives `savedSceneNum`:
in the dump it reads `0x08`, a scene that is not in the table, so `scene_names`
returns `None` and the panel writes "unknown area". Hence the flicker, and
hence it only being noticeable in areas with many transitions.

The fix is the **latch** the POC has been recommending since the spike: if the
live read fails, keep the last good one rather than jumping to worse data. The
state's `live` field still says which of the two it is.

```
normal poll                       scene=MOUNTAIN_VILLAGE_WINTER  live=True
PlayState fails to validate       scene=MOUNTAIN_VILLAGE_WINTER  live=False
and another poll the same         scene=MOUNTAIN_VILLAGE_WINTER  live=False
validates again                   scene=MOUNTAIN_VILLAGE_WINTER  live=True
```

> The general lesson: **the fallback has to be better than nothing, not worse
> than what you already had**. Falling from live data to stale data seemed
> prudent and turned out to be the source of the defect.

### The bases are chosen in pairs

This came out of looking into why the summary was showing rupees and hearts
that were not the save's. **It is not confirmed that this was the cause** —a
dump at the moment it happens is needed— but looking at it turned up a real
hole.

`locate_saves` chose each game's base **separately**, the first one on the list
that validated. And validating is not enough: on crossing between games RAM is
reorganised, and the buffer left behind **keeps its signature and perfectly
plausible contents**. With MM running, OoT's base from the other arrangement
(`0x8011A5D0`) is tried before the right one (`0x8076D400`), so if there are
still remnants there, it wins — and the overlay goes on reading the rupees and
hearts of an old snapshot for the rest of the session.

What rules it out is not the contents but **where it is**: the running game has
its save in the low area and the other in the high one, always. So of the two
bases, **exactly one** falls below `RDRAM_MID`. Both low means one of them is a
remnant.

Now the **pair** that validates and fits is chosen, the fallback sweep prefers
the one that pairs with what is already there, and each poll's revalidation
checks it too — otherwise a bad pair would stay in the cache forever. The
threshold (`0x80300000`) sits in the empty gap between the low ones
(`0x8011`–`0x801F`) and the high ones (`0x8044`–`0x8076`), with margin on both
sides.

### Two columns

Requested on 14 Aug: the item grid alone on the left, and on the right the
three lists stacked —progress by region, remaining in the area, activity—. It
is the split that makes sense: **the grid is the only one that gains from
width**, because `fitItems` sizes the cell to the box it is given; the lists
only need to be readable. That is why it takes the slightly wider column (1.1
against 1).

The column swap was done **in the markup, not with CSS `order`**, so that
reading order and visual order do not come apart — and as a bonus it decides
correctly what comes first when, below 1100 px, everything collapses into a
single column.

What cost effort was not moving the cards but the height. The panels'
`max-height` values subtract a constant that is the surrounding chrome, and
with three cards in one column there are two more headers and two more gaps. It
was **measured** rather than eyeballed: at 1080p the body gave 1132 against a
viewport of 985, and of the 147 excess pixels most came from the **Display
options menu being open** (236 px). Collapsed —the spoiler switch is outside,
which is the only thing you need to reach in a hurry— and with `pane-third` at
`(100vh − 604px) / 3` and `pane-tall` at `100vh − 438px`, the view fits exactly:
**985 = 985**, no scrollbar.

Also verified that what this area breaks easily still holds: the five
standalone panels leave no empty containers, a narrow window collapses to one
column with no horizontal scrolling, and `/p/items` at 400×600 —an OBS source's
size— still fits without scrolling.

### Hiding completed regions

`?done=hide`, or the **Regions** selector in the options. With the list full of
finished areas, what is left to do gets lost among them.

**What counts as done is defined by whichever filter you have on.** With "only
what matters", a region counts as finished when its important checks are,
even if junk is left. Otherwise the two options would fight each other and the
panel would show regions at `3 / 3` under a heading saying they are pending.

And it says so, as with everything else: `165 regions not started · 1
completed`. If hiding them leaves none, the panel does not go mute — it says
*every region you have touched is done*, which is not the same as "no data".

### The region panel only shows where you have been, and says so

This came out of the user counting: with the important filter on they saw three
regions adding up to **29** checks, and the header said **4 / 670**. Locations
appeared to be missing.

They were not. The panel lists **only the regions where you have done
something** (`if got:`), and the numbers add up exactly:

```
  5 regions shown            ->  29 important
165 regions with no progress -> 641 important
                                ---
                                670
```

The fault was presentational: it said nothing about the 165 it was keeping
quiet about. Now it says `165 regions not started` underneath, with the count
matching the filter —109 regions have at least one important check, out of 170
in total—. It does not appear with `?game=`, because there the count and the
list would not be talking about the same set.

> That the user had to add things up by hand to understand a panel is the
> signal. The missing number is almost always "how many am I not showing".

### Scene setups: why Hyrule Field looked broken

**Done 14 Aug 2026**, from a bug the user reported: in Hyrule Field they cut a
bush, the feed announced it, and the remaining list still showed one with
almost the same name. It looked as though the tracker was not noticing.

That was not it. They are **two different bushes**:

```
Hyrule Field Bush 09               setup=1  actor=39  bitpos=2438
Hyrule Field Grass Pack 3 Bush 09  setup=0  actor=60  bitpos=2322
```

An OoT scene exists in several versions —the *alternate headers*:
child/adult, day/night— and **each one has its own actors**, hence its own
checks. Hyrule Field has three (setups 0, 1 and 2). Only one is loaded, so half
of what the panel listed was unreachable at that moment and stayed pending
forever. What made it look like a detection bug is that the names are similar
and **the numbers coincide** —07, 08, 09, 11, 12 in both families— so they read
as the same place.

It is the same class of bug already fixed for Master Quest, which was being
filtered.

**Where the setup comes from.** `gSaveContext.sceneSetupId`, and it is not in
the save but in the `SaveContext` that wraps it:

```
ASSERT_OFFSET(OotSaveContext, sceneSetupId, 0x1360)   OotSaveContext{OotSave save; …}
ASSERT_OFFSET(MmSaveContext,  sceneSetupId, 0x3cac)   MmSaveContext{MmSave save; …}
```

The project's MM base is `MmSave+0x08`, so there it is `0x3CAC − 8`. Verified
on both dumps: with that offset MM reads 0, and taking the base as `MmSave` it
reads garbage. OoT's `0x1360` falls inside what the POC called "the active
scene's temporary flags" (`+0x1354`…), which fits: that area is not the
`OotSave`'s, it belongs to the `SaveContext` behind it.

**But the requested one is not the loaded one.** OoTMM resolves one to the
other in `oot/room.c`: if the scene does not have that alternate header, it
falls back to the highest that exists and otherwise to 0; and above 3 it is a
cutscene and uses 0. The result lives in `g.sceneSetupId`, which is in the
payload with no known address, so `setup_loaded()` **repeats the resolution**
using the setups the scene's own xflags mention.

```
sceneSetupId=0 -> 0    sceneSetupId=2 -> 2    sceneSetupId=7 -> 0
sceneSetupId=1 -> 1    sceneSetupId=3 -> 2    (HYRULE_FIELD has 0,1,2)
```

**The guard that avoids the symmetric bug.** If the resolved setup is not among
the ones we know —a scene whose only xflags live in an alternate header—
`setup_loaded` returns `None` and **nothing is filtered**. Without that, the
panel would empty out entirely, which is worse than showing leftovers.

Measured in Hyrule Field, 177 remaining:

| `sceneSetupId` | in the list | `Bush NN` | `Grass Pack … Bush` | set aside |
|---|---|---|---|---|
| 0 | 67 | 0 | 48 | 110 |
| 1 | 108 | 58 | 0 | 69 |

That is: in the user's case —the feed announced `Bush NN`, therefore setup 1—
the `Grass Pack` ones disappear from the remaining list, which is exactly what
was surplus.

**They are not removed from the totals, and it says how many there are.** The
checks from another setup genuinely exist and are reachable by coming back at
the other age, so only the "what is left here" panel is filtered, and
underneath it reads `and 83 more · 69 in another setup of this scene`. Hiding
them without saying so is the trap this project has already stepped in twice.

> And along the way, **`area cleared` had to mean cleared**. With an empty list
> the panel always said that, even when it was empty because of the filter. Now
> it distinguishes all three: `nothing for this version of the scene · N in
> another setup`, `nothing important left · N junk`, and `area cleared` only
> when it is. The junk one was a bug that was already there: with `junk=hide`
> and only junk left, the list came out empty **with no message at all**,
> because the counter was looking at the unfiltered list.

### The cows, and the mystery of the `unk` field solved along the way

**Done 14 Aug**, and it came out of a complaint from the user: inside the cow
grotto, with the important filter on, the panel did not list **the cow** —
which was exactly what they had left to get. It did not show up because the
`cow_flags` were among the 80 checks with no address, and with no address they
do not enter the panel.

The missing macro is in `combo/save.h`:

```c
#define SAVE_EXTRA_RECORD(type, index) (gOotSave + 0xd4 + 0x1c*(index) + 0x10)
#define gCowFlags   SAVE_EXTRA_RECORD(u32, 9)
```

And `0xD4 + N*0x1C + 0x10` is **scene N's `unk` field** in OoT's flag table.
That is: OoTMM stores a couple of dozen u32s of its own by tucking them into
the slot vanilla OoT does not use in each scene.

> **That closes the "loose end: the `unk` field"** that had been open for days
> at the end of this document. The items that set bits in the `unk` of **two**
> scenes at once were writing two of these records, and the measured pair
> confirms it: Cojiro touched scenes 0 and 10, which are exactly
> `gOotExtraTrade` (index 0) and `gOotExtraTradeSave` (index 10). There was no
> geometric rule to deduce; it was a table of indices.

`gCowFlags` is index 9, so it lives at `oot_base + 0x1E0`, it is a `u32` with
`1 << id`, and **both games' cows share the same field** — it is in OoT's save
whichever one is running, and that is always located.

With that, **18 of 18 `cow_flags` resolved** and the checks left to map drop
from 80 to **62**. The 18 give 18 distinct `(addr, bit)` pairs.

> **Derived, not measured.** In all three dumps `oot_base + 0x1E0` reads 0,
> which is consistent —no cow has been milked— but proves nothing, just as
> happened with `gsFlags`. **Falsifiable prediction:** milking the cow at the
> back of Termina Field's grotto must set **bit 20** of `0x…1E0`; the one at the
> front is 19, and Romani Ranch's three are bits 16, 17 and 18.

### The room filters, and the grottos

**Fixed 14 Aug**, from being inside a grotto and seeing 440 remaining:
`Remaining in GROTTOS · room 10` followed by every grotto in the game.

Two things. The first is that **the room had to filter, not just sort** — which
is what the backlog had been asking for from the start, "narrow the whole
scene's remaining list down to the room you are in". It was left as a reordering
out of caution, and in a normal scene it barely shows; in `GROTTOS`, which is
**a single scene with every grotto in the game inside it**, the difference is
between useful and useless. Anything with no room —chests, NPCs, shops— is
never discarded.

The second is `comboXflagInit`'s `0x20 | grottoData`. Those are not a room
number, so they cannot be compared… unless you know whether you are **in** the
generic grotto room or not. And that **falls out of the data, with no constant
at all**:

> The generic room is the one that **has no checks of its own**, precisely
> because its actors were renumbered to `0x20 | …`. In MM's `GROTTOS` the rooms
> with checks are 0, 2, 5, 6 and 9–15 — there is no 4 — and 4 is exactly the one
> `comboXflagInit` rewrites. So if you are in a room the scene recognises as its
> own, **none of the `0x20 |` ones can be yours**.

Measured in Termina Field's cow grotto (room 10): from **452 to 99**. The 99
are that room's 76 plus 23 chests and NPCs that carry no room — the loose end
that was already noted. And in the generic room (4) none of them is filtered,
which is correct: there they are all candidates.

In a normal scene it does what was expected: Water Temple, room 21, goes from
42 to 21 —6 from the room and 15 with no room— and says `21 in other rooms`.

### And as a bonus, the room

From the same `PlayState` comes `roomCtx.curRoom.num`, and **only the xflags
carry a room** in `checks.json` (4,440 of 6,043). With that, the remaining
checks for the room you are in come out **first and marked**, and the title
says "· room N" when the scene has more than one.

Two decisions, both for the same reason —that a filter which hides things
without saying so is worse than no filter—:

- **It reorders, it does not filter.** Chests, NPCs and shops have no room;
  filtering would make them all disappear. Measured in the Water Temple with
  the player in room 21: 42 remaining, 6 marked and at the top, and the 15 with
  no room still in the list.
- **It is not applied in grottos.** There the xflag's `room` is
  `comboXflagInit`'s `0x20 | grottoData` trick, not a room number, while
  `curRoom.num` reads 0. Comparing them, all 311 grotto checks would come out
  as "in another room". They are marked as having no room and that is that.

### Rebasing: why `checks.json` carries an anchor

`checks.json`'s addresses are absolute, and the bases **move** when crossing between OoT and MM: RAM is reorganised entirely, which is exactly why `locate_saves` exists. An overlay that runs for hours has to rebase, so each check also carries `anchor` + `off`:

| Anchor | Resolved by | What hangs off it |
|---|---|---|
| `oot` | the `ZELDAZ` signature | OoT's scene flags, `gsFlags` |
| `mm` | the `ZELDA3` signature | MM's scene flags |
| `custom` | a fixed offset from `mm` | the 4,751 xflags and the custom bitmaps |

The custom save has no signature of its own, but it hangs off MM's buffer by a distance that is a per-version constant: both are globals of the same build, so they move together.

### The signature is not enough to locate a save

This came up playing another seed: with OoT running, MM's panel filled with garbage —`SWAMP SKULLTULAS 7680`, when the maximum is 30— and MM's regions invented progress.

`locate_saves` searched for the `ZELDA3` signature and **kept the first match by address, without checking anything**. But the signature also appears in static copies and in stale buffers, so the first is usually not the live one. In earlier sessions it worked by luck: the known base was right and no scanning was needed.

Now each candidate passes a cheap plausibility check, using fields whose range is known:

| Field | Invariant |
|---|---|
| `healthCapacity` | greater than zero, up to 20 hearts, and **a multiple of 0x10** |
| `health` | between zero and the capacity |

| `rupees` | from 0 to 9999 |
| swamp and ocean skulltulas (MM) | 30 at most |

Verified against both dumps: in OoT's it discards MM's static copy (`0x80442248`) and keeps the live one (`0x8044BE18`); in MM's it discards `0x801C6954` and picks `0x801EF678`. And on crossing between games it relocates on its own, both ways.

> **And along the way, a worse problem.** `locate_saves` runs on every poll, and when a base is not among the known ones it **scans 8 MB of RDRAM**. Twice a second. With the bases moved, every poll was a sweep of the entire memory. Now the bases are cached and only relocated when they stop validating.

### The confidence measure

`gSharedCustomSave` is only located with **OoT running**; with MM its address is a different one and has not been worked out. Rather than show garbage, the overlay measures what fraction of the set bits falls on a known check and below 90% marks the panel as untrustworthy. If the base is wrong, the bits land where nothing is mapped and the measure sinks on its own.

It is measured **only over the xflag ranges**, which are pure bitmap. Measuring it over the whole block gave 0.93 with the correct base, because inside there are fields that are not check flags —`OotCustomSave`'s trailing bitfield, `mm.halfDays`, the counters— whose set bits are legitimate but map to nothing. Narrowed to the xflags it gives 1.0.

**The fraction alone is not enough, and this was a hole that stayed open for a while.** A wrong base that lands in a region of zeros gives 1.0 vacuously, without a single bit: that is not a warning, it is silence, and the overlay simply counted fewer checks without saying anything. Now the number of bits is also compared against the highest seen. If there were bits before and now there are none, either the base is wrong or you have started a new save, and **what tells them apart is that in a new save the scene checks drop too**, and those are read through a different anchor: if those are still there and the xflags are not, the base is wrong.

That guard is what exposed that **with MM running the custom save was not being read at all** — and with that located, it was fixed. See below.

### The custom save with MM running

It was the last big limitation, and it was solved without touching the emulator, using the two dumps that already existed: they are from the same save and **the custom save is shared**, so its contents have to appear literally in both memories. Searching for OoT's RAM block inside MM's turns up **a single match**, and the non-zero offsets line up one to one: `0xd6`, `0x1b2..0x1c1` of xflags, `0x31a` shops, `0x376` `OotCustomSave`'s trailing bitfield, `0x6f4` `halfDays`. The only differences are the two checks the player did between one dump and the other.

The rule turned out to be symmetric: **`gSharedCustomSave` sits right in front of the buffer of the game that is NOT running.**

| Active game | The other's buffer | Custom save | Distance |
|---|---|---|---|
| OoT | MM at `0x8044BE18` | `0x8044B570` | `0x8A8` |
| MM | OoT at `0x8076C4F0` | `0x8076BC50` | `0x8A0` |

The distances differ because what sits in between is the other game's save, and they are not the same size. The poll tries both addresses and keeps whichever gives more mapped bits, so it does not depend on getting it right first time.

**The check that closes it**: with MM's dump the tracker goes from 5 checks to **20**, and under OoT it gives 18. The difference of 2 is exactly the one between the two dumps — an xflag bit at `0x300` and the `Song of Healing` at `0x6dc`.

### Gotchas that came out of building it

- **`read_block` travels in 4-byte words.** Asking for a field at an unaligned address (`info.sceneId` is at `+0x66`) returns garbage over the real link. You have to read the word containing it and pull the halfword out. With a fake link over a dump this **does not** show, so it is one of those that passes the test and fails live.
- **The first poll has to set the baseline silently**, or the feed starts by spitting out all at once the hundreds of checks you already had.
- **Variable shadowing.** The region loop used `scene` as a variable and clobbered the scene id coming from the poll; after the loop it held the *name* of the last region, so `scene_id == scene` compared an integer against a string and the remaining list always came out empty, with no error.
- **The meter's track cannot look like the fill.** With the track at 26% of the hue, a bar at 0.4% read as full. In a stream overlay that is misinforming whoever is watching.
- **Controls that generate URLs have to start by reading the URL.** The director view's `<select>`s came up with their default value from the HTML, so opening `/?spoiler=off` generated links without `spoiler=off`: the option looked set and did not propagate.
- **A class collision between two components.** The region rows carry `row-oot` / `row-mm` to inherit the game's colour, and the item grid's container carried the same ones. On giving those classes `display:flex` to put the groups in parallel, **the meters vanished from every region**: the row stopped being a grid and `.meter` shrank to nothing. A class that only contributes colour variables cannot also be used as a layout hook; the grid's container is now `.gamegrid`.
- **`const`'s temporal dead zone.** The block that sets up panel mode used `GAME` for the title, and `const GAME` was declared further down: `Cannot access 'GAME' before initialization` blew up the entire script and the panels came out empty, with nothing visibly broken. The screenshot only showed an empty card; what gave it away was reading the console with `--enable-logging=stderr`.
- **Careful with headless Edge screenshots on scaled displays.** Asking for `--window-size=420` gave a viewport of 504 CSS px and a PNG of 420, cropping the right side: it looked as though the regions' counters were missing. There was no such bug. Before fixing something that only shows in a screenshot, **measure the DOM** (`--dump-dom` with a probe that writes the measurements into the `<title>`) — and delete the probe afterwards.

### A technique worth using from now on

**Savestates.** Saving a savestate before taking a check lets you dump, reload, and have the check available again untaken. Mapping locations stops consuming the save and the A/B experiment becomes exact: same save state, the only difference being the check. It was discovered late; it would have saved several hours.

---

## The placement comes out of the ROM: the spoiler is no longer needed

**Done 13 Aug 2026.** It came out of asking whether, instead of loading a
spoiler, the ROM could be read for what item is in each place. It can, and it
is what `placement.py` does now: **5,371 locations with their item, without
asking anyone for anything**.

### The table

`comboItemOverride()` (`src/common/item/item.c`) resolves a query into an item,
and reads from a file in the ROM, `COMBO_VROM_CHECKS`:

```c
typedef struct ComboOverrideData {   /* 16 bytes, SORTED by key */
    u32 key;      /* (ovType << 24) | (sceneId << 16) | (roomId << 8) | id */
    s16 player;   /* whose item it is, for multiworld */
    u16 value;    /* <-- the item, a GI */
    s16 giCloak;
    s16 unused[3];
} ComboOverrideData;
```

The game walks it with a **binary search** on the key, with a 64-entry cache.
We can read the whole thing in one go.

**Where it is, and this is the best news:** `COMBO_VROM_CHECKS` is
`COMBO_EXTRA_DMA_VROM | 0x00400000` in the OoT build and `| 0x00500000` in the
MM one (`combo/defs.h`), i.e. **`0xF0400000` and `0xF0500000`**. Structural
constants, not addresses that move with every version the way the xflag tables'
`0x80b0f00` do. And they are read with `rom.read_extra_vrom`, which already
exists.

### What was measured on seed f5PCTnhD

```
0xF0400000  OoT build   36,000 bytes = 2250 entries
0xF0500000  MM build    44,320 bytes = 2770 entries
                                       ----
                         minus 2 sentinels (ovType 0xFF)  = 5018
```

**5018 is exactly the number of locations in the spoiler log.** Both tables come
out sorted by key and with no repeated keys.

And the breakdown by `ovType` gives the three blocks that were left to map:

| ovType | | OoT | MM | |
|---|---|---|---|---|
| 1 | chest | 179 | 188 | |
| 2 | collectible | 37 | 22 | |
| 3 | npc | 95 | 123 | |
| 4 | gs | **100** | — | the 100 vanilla ones, exactly |
| 5 | sf | — | **29** | the missing stray fairies |
| 6 | cow | 9 | 8 | |
| 7 | shop | 64 | 22 | |
| 8 | scrub | 36 | — | |
| 9 | sr | 80 | — | |
| 10 | fish | **33** | — | the missing `caughtFishFlags` |
| 16–27 | xflag0–11 | 1225+ | 1685+ | |

> Careful: this does **not** resolve the 80 pending checks. They are pending
> because we do not know **where their flag lives**, not because we do not know
> what item is there. What the table does is enumerate them exactly, and
> confirm the counts along the way.

### The key, and the trap it had

Each check's key, as `placement.override_key` forms it:

```
xflags:  ov = 0x10 + slice
         room = (room & 0x3F) | ((setup & 3) << 6)      <- same as comboXflagItemQuery
         key = (ov << 24) | (scene << 16) | (room << 8) | actor
```

**The other types do not all carry the scene.** Only `chest`, `collectible` and
`sf` use it; in `npc`, `gs`, `cow`, `shop`, `scrub`, `sr` and `fish` the scene
byte is **0**, because they are global id spaces. It was found by looking at
the ROM's real keys, after a first attempt in which all of that failed as a
block.

> **The key's id is the bitmap's global index, and `checks.json` did not have
> it.** In `npc`, `gs`, `shop`, `scrub` and `sr`, `mkchecks.py` rewrites `bit`
> to leave the bit within its byte, and with that 61 keys ended up claimed by
> several checks at once: `Hatch Chicken`, `Malon Egg`, `Lost Woods Target` and
> `Saria's Song` shared `0x03000000`. The good id only exists at the moment the
> CSV is read, so now it is kept separately in the **`csv_id`** field and the
> key is formed from that.

### The proof that it really is the placement

Two, and the second is the one that counts.

**One**: if the table is what we think it is, each `gi` has to correspond to a
single item name in the spoiler. Over the types with a 1:1 key there are **63
distinct `gi` and not one real conflict**. The two that appear with several
names are `Small Key (…)` and `Stray Fairy (…)`, which the generator names by
dungeon — correct behaviour, not a misreading.

**Two, the good one**: what matters is not that the text matches letter for
letter, but that **the junk classification comes out the same**, which is what
it is used for. Over the 5,018 locations that have an item by both routes:

```
junk classification, ROM against spoiler:  5018 agree, 0 disagree  (100%)
names:  4755 identical · 17 differ only in the (OoT)/(MM) suffix · 246 different
```

The 246 different names are not errors: the spoiler says `Progressive Sword`
and the ROM `Kokiri Sword`, the spoiler `Gold Rupee` and the ROM `Huge Rupee`.
The ROM names the concrete item and the spoiler the pool entry.

> **The 34 disagreements there were, and why they mattered.** Four cases turned
> up: `Milk` against `some Lon Lon Milk` and `1 Bomb` against `Bomb`. Both are
> about form rather than content, but both **made junk pass for important**.
> They were fixed on both sides: `limpia_nombre` also strips a leading `some`,
> and the junk patterns accept the singular and `Lon Lon`. That is where the
> 100% comes from.

### Coverage, and what falls outside

```
active checks:                     5074
  with an address:                 5012
  with an item from the ROM:       4956   (98.9%)
without an item, in total:          672
  of those, Master Quest:           616   correct: they do not exist in this seed
  active and with an address:        56   (1.1%)
```

> Re-measured on 14 Aug 2026 after the cows: the two middle figures were 4995
> and 4939 before `cow_flags` resolved 18 more. The ratio and the 56 left over
> are unchanged.

The 56 are 25 `tree`, 7 `grass`, 5 `crate`, 5 `butterfly`, 4 `pot`, 3
`boulder-silver`, 3 `collectible`, 2 `rock`, 1 `snowball` and 1 `npc`. They are
left with no item and whoever consumes them knows it: `is_junk(None)` gives
`False`, so they count as important, which is the safe side to fail on.

### How it is wired up

| | |
|---|---|
| `placement.py` | reads both tables from the ROM and `data/gi.yml`, and forms the keys |
| `data/gi.yml` | a copy of the repo's `data/defs/gi.yml`; the `gi` index is the position + 1 |
| `mkchecks.py` | stores `csv_id`, and writes `item`, `item_id`, `gi` and `ovkey` on each row |
| `overlay.py` | `rom_items` from `checks.json`; a hand-loaded spoiler goes **on top** |

The load-spoiler button **stays**, but becomes the fallback route: it is there
for when the table cannot be read from the ROM. The director view says which
one is in use — "5,371 items read from the ROM · no spoiler needed".

Measured with the overlay served over the dump and **with no spoiler at all**:
`can_filter` comes out `true` on its own, and "only what matters" gives **4 /
612** and 2 remaining in Kokiri Forest — the exact same numbers it gave with
the spoiler loaded by hand.

### And a qualification about "more stable across future versions"

Yes, but it is worth not overselling it:

- ~~**It is more stable in the address**~~: **wrong, corrected 27 ago 2026.**
  Gen 943 —master, after v32.3— merged the two files into one at `0xF0400000`
  and tagged MM's keys with bit 31, so asking for `0xF0500000` raised a
  KeyError nobody caught and the tracker could not build tables for that build
  at all. Found by shape since (`placement.locate_tables`), with the constants
  as the contrast: over 92 ROMs here, 81 read identically to before, 0
  differences, and the four master seeds that used to crash come out at 967
  rows —583 OoT + 384 MM— which is the same split the two files had.
- **It is not version-independent**: the key's format, the `ovType` numbering
  and above all the `gi` index can change between versions — a new item in the
  middle of the list shifts everything behind it.

And a note that is not technical: reading the placement from the ROM **does not
give less information than the spoiler, it gives the same**. What is gained is
that there is no file to find, load or validate; not that the tracker knows
less.

## Reading the inventory

The item tracker does not need the check system: it reads the inventory straight from the save context, which is more immediate (it does not wait for a scene change) and covers both games.

### OoT's map

Taken from `combo/oot/save.h` and validated in game. See `inventory.py`.

```
items[24]  +0x74     ammo[15]  +0x8C     equipment +0x9C    upgrades +0xA0
questItems +0xA4     dungeonItems +0xA8  goldTokens +0xD0
```

`equipment` is four nibbles (swords, shields, tunics, boots), and `upgrades` eight fields of 2–3 bits (quiver, bomb bag, strength, scale, wallet, bullet bag, sticks, nuts). The bitfields are read the MIPS big-endian way: **the first field declared in the struct occupies the highest bits**.

### MM's map

It was nowhere to be found: it was anchored by hunting **two masks**.

```
Deku   (mask 5,  slot 29)  ->  +0x85
Romani (mask 12, slot 36)  ->  +0x8C     a difference of 7 slots
```

That difference matches MM's real mask order, and from there comes `items[48] = +0x68`. With `MmInventory`'s layout from the header, the rest by subtraction:

```
items[48] +0x68   ammo[24] +0x98   upgrades +0xB0   quest +0xB4
dungeonItems[10] +0xB8   dungeonKeys[9] +0xC2   strayFairies[10] +0xCC
skullCountSwamp +0xEB8   skullCountOcean +0xEBA
```

Checked three independent ways: slot 26 of one dump read `0x47` (Blast Mask, which came out of Mido's chest); `quest` had bit 12 (Song of Time, the Skull Kid's); and the heart pieces in that word's four high bits.

**`MmUpgrades` has the same layout as `OotSaveUpgrades`** (only `dive` changes to `scale`). Confirmed: on upgrading the deku sticks the `u32` went up by exactly `1<<17`, which is where `dekuStick` falls in both.

### The id table: 325 items with nothing hunted

`packages/generator/include/combo/data/items.h` (copy in `data/ref/`) defines the `ITEM_OOT_*` and `ITEM_MM_*`, **and the value stored in `items[]` is that id directly**. Validated against the eight ids hunted live:

```
ITEM_MM_BOMBCHU 0x07 · ITEM_MM_POWDER_KEG 0x0c · ITEM_MM_MASK_DEKU 0x32
ITEM_MM_MASK_ROMANI 0x3c · ITEM_MM_MASK_BLAST 0x47 · ITEM_MM_BOOTS_HOVER 0xb2
ITEM_OOT_POWDER_KEG 0xa7 · ITEM_OOT_COJIRO 0x2f
```

162 OoT items and 163 MM ones get named in one go. **There is no need to hunt item by item just to label the inventory**: reading the id is enough.

### Two things you only see by playing

- **OoTMM syncs upgrades across games.** The nut and stick upgrades touched `upgrades` and `ammo` in OoT *and* in MM at once. Two independent cases, so it is normal behaviour.
- **Items cross inventories.** The Powder Keg (MM's) takes the bomb slot in OoT with id `0xA7`; the Hover Boots (OoT's) show up in MM's slot 17 with id `0xB2`. For the tracker: **it is not enough to look at whether a slot is occupied, you have to read the id**.

### Live verifications

Fifteen items picked up with the tracker watching, each one confirming a different structure:

| Item | What it confirmed |
|---|---|
| Minuet · Serenade · Sun's Song | OoT's `questItems` |
| Recovery Heart | `health` |
| Large Magic Jar | (left `+0x537` unidentified) |
| Nut upgrade · stick upgrade | `upgrades` + `ammo`, and the cross-game sync |
| Deku Mask · Romani Mask | MM's `items[48]` and the order of the 24 masks |
| Song of Time · 2 heart pieces | MM's `quest` |
| Remains of Twinmold | MM's `quest` with a key object |
| Swamp skulltula | the `+0xEB8` counter |
| Piece of Heart (shop) | the custom save's `shops` bitmap |
| Cojiro | OoT's slot 22 |
| Giant's Knife | the sword nibble + `swordHealth` |
| Kokiri Sword | bit 0 of the sword nibble |
| Hover Boots | the boot nibble + MM's slot 17 |
| Powder Keg | MM's slot 12 + OoT's bomb slot |
| Bombchu Bag | MM's slot 7 + OoT's slot 8 |

### The hunter

`ootmm.py items` reads both saves in a loop and reports every change. Three things make it usable:

- **It locates the bases by signature and revalidates on every read.** If it detects that the signature moved, it relocates on its own: that is what makes it possible to cross from OoT to MM without restarting anything. The check is free, because the signature comes inside the block already being read.
- **It calibrates the noise at startup.** Six seconds watching what moves on its own, and that gets silenced. The block includes positions and timers that would otherwise drown the log.
- **It auto-silences whatever insists.** An unidentified byte that changes more than three times is taken for a counter and goes quiet. An item is picked up once; a clock is not.

Everything it does not recognise comes out with its offset and address, so no item goes unnoticed even if it writes somewhere new. That is what made it possible to hunt the fifteen above.

### Loose end: the `unk` field

Several items set bits in the `unk` field of the scene table, which vanilla OoT does not use:

| Item | Scenes | Bit |
|---|---|---|
| Minuet / Blast Mask | 0 and 10 | 27 |
| Cojiro | 0 and 10 | 2 |
| Powder Keg | 1 and 20 | 24 |

Always **two** scenes and the same bit in both, but the pair changes with the item, and in the Powder Keg's case the base values differed between the two scenes (so they are not identical copies). With three samples there is not enough to deduce the rule. Worth coming back here: if the index follows some order, it is another route for mapping checks.

> **SOLVED 14 Aug 2026, and there was no rule to deduce: it was a table.**
> `SAVE_EXTRA_RECORD(type, index)` from `combo/save.h` is
> `gOotSave + 0xd4 + 0x1c*index + 0x10`, i.e. **scene `index`'s `unk`**. OoTMM
> puts twenty-one u32s of its own in there. The measured pairs line up: Cojiro
> in scenes 0 and 10 are `gOotExtraTrade` (index 0) and `gOotExtraTradeSave`
> (index 10); the Powder Keg in 1 and 20 are `gOotExtraItems` and
> `gMmExtraAmmo`.
>
> And it really was "another route for mapping checks": `gCowFlags` (index 9)
> came out of it and with it the 18 `cow_flags`. See the cow section further up.
> Still unused are `gMmOwlFlags` (11) and the five `gOotSilverRupeeCounts`
> (13–17), which are the next candidates if they are ever needed.

---

## The `.exe`: distributing it without asking for Python

**Done 14 Aug 2026.** `python -m PyInstaller ootmm.spec` leaves
`dist/ootmm-tracker.exe`, **8.5 MB**, a single file with nothing to install.

What was needed was not packaging, which is one line, but separating two things
that until now were the same: **what travels with the program** and **what the
program produces**. Inside the `.exe` they stop being in the same place, and
each one fails differently.

### `paths.py`, the two folders

| | From source | From the `.exe` |
|---|---|---|
| `paths.res(...)` — what travels | the project folder | `sys._MEIPASS`, the temp folder it unpacks into |
| `paths.user(...)` — what is generated | the project folder | `%LOCALAPPDATA%\OoTMM-Tracker\` |

Running from source both return what they always did, so **nothing changes** in
the workflow here.

What goes in each:

```
res   data/ (pool CSVs, scenes.yml, npc.yml, gi.yml, ref/), overlay.html,
      Scripts/tracker.lua, icons/README.md, README.md
user  checks.json, icons.json, icons.png, discover-cache.json, icons/
```

Putting the generated files in `_MEIPASS` would have been the classic silent
failure: the folder is deleted on exit, so **every start would regenerate the
tables** —the only slow thing there is— and nobody would see an error, only a
tracker that always takes half a minute to open.

### The subprocess that could not work

`discover.py` launched the generators with
`subprocess.run([sys.executable, "mkchecks.py", ...])`. Inside the `.exe`,
`sys.executable` **is the tracker**, not an interpreter, and there is no `.py`
to hand it: that relaunches the tracker with arguments it does not understand.

Now `_generate()` imports the module and calls its `main(argv)`. Both `main()`s
were changed to accept `argv` (`ap.parse_args(argv)`), which is the whole
change, and they still work as standalone scripts. `SystemExit` is caught to
preserve the exit code, which is what distinguishes "I could not" from "done"
—and what keeps the *the checks are from another ROM* warning appearing.

Since they are called by name, they go in the `.spec`'s `hiddenimports`: static
analysis does not see them, and without that the `.exe` starts perfectly and
only fails when changing seed.

### What went wrong while building it

- **Excluding `email` from the bundle.** It looked dead and `http.server`
  imports it. The tracker started completely —detection, tables, icons, the
  link with the Lua, all correct— and blew up with
  `ModuleNotFoundError: No module named 'email'` **when bringing up the
  server**, which is the last thing that happens. The `excludes` list was left
  at `tkinter` and `unittest`; the megabyte the rest saved is not worth a
  traceback like that.
- **With no arguments it did nothing.** The subparser is mandatory, so a double
  click = print the usage and close the window before it can be read. From the
  `.exe`, no arguments now means `overlay`; from source it still prints the
  usage.
- **The console goes with the process when it dies.** `_run()` holds the window
  open with an `input()` at the end, but only if it is frozen, if `stdin` is a
  terminal and if **there was a double click or there was an error**: typing a
  subcommand in a console already leaves the output visible.
- **Wrapping `main()` in a `try` swallowed every error message.** Here failures
  go through `sys.exit("explanation")` everywhere, and that text is printed by
  the interpreter **only if nobody catches the `SystemExit`**. `_run()` was
  catching it to keep the exit code, so every one of those failures became a
  code and silence — and not only in the `.exe`, from source too. Now, if
  `ex.code` is not an integer, it is printed to `stderr`. It is the same old
  bug in a new outfit: it did not break, it went quiet.
- **A wrong `--emu` must not fall back to the detected emulator.** It is a hint
  for the search, so pointing it at the wrong folder installed the script into
  the real emulator and said everything was fine. If it is passed by hand and
  has no `Config\Project64.cfg`, that is an error.
- **Saying "installed" about something not written.** `ensure_lua()` returned
  the path in all four cases, so refusing to overwrite someone else's script was
  announced exactly like having placed it. Now it returns `(path, status)` with
  `written` / `same` / `kept`, and each is reported differently.

### `tracker.lua` inside the package

The script is in the `.exe`, so it can no longer be copied by hand.
`ensure_lua()` writes it into the emulator's `Scripts\` —which `discover`
already knew how to find— the first time the overlay starts, and there is
`ootmm-tracker.exe install-lua` to do it separately.

> **A new hard-link trap.** Editing `Scripts/tracker.lua` with a tool that
> writes the whole file **breaks the link**: a new file is created with that
> name and the emulator keeps the old copy, with nothing to warn you. It
> happened while translating its two comments. After touching it:
> `fsutil hardlink list`, and if there is only one name, remake it with
> `Remove-Item <emu>` and `New-Item -ItemType HardLink`. And rebuild the
> `.exe`, which carries its own copy inside.

Two guards: **it never overwrites a script that is already there** (if it
differs it says so and leaves it alone; you have to ask with `--force`), and
**from source it writes nothing** unless asked, because here the project's copy
and the emulator's are *the same file* through a hard link and overwriting it
would split it into two copies that then diverge silently. It is written in
binary, which is the other way of not inserting a BOM.

### How it was tested

A **fake `tracker.lua` in Python** —a client that connects to the port and
serves a dump with the same protocol: `PING` → `TRK1`, opcodes 2/3/4 and
`0x10`— makes it possible to run the whole overlay end to end without an
emulator. It is in the project as `fakelua.py`, because it is the only way to
test the `.exe`: the `--dump` shortcut is only on `items` and `checks`, and it
also skips the link, which is exactly the part packaging could break. It is
what produced the comparison below:

| Test | Result |
|---|---|
| `checks.json` regenerated from source | **byte for byte identical** to the one from before anything was touched |
| `icons.json` / `icons.png` | identical |
| `checks.json` generated by the `.exe` | identical across all 6,043 rows (only `rom` differs, because of the path's slashes) |
| the `.exe`'s `/state.json` vs. source's, same dump | **identical**, apart from `uptime` |
| `ootmm-tracker.exe checks --dump` vs. `python ootmm.py checks --dump` | identical output |
| `/`, `/p/regions`, `/icons.png` served by the `.exe` | 200, 60,561 and 311,835 bytes |
| A **clean** start (deleting `%LOCALAPPDATA%\OoTMM-Tracker`), no arguments, with `ram-en-mm.bin` | detects the ROM, regenerates tables and icons, `active: mm`, bases `MM 0x801EF678` / `OoT 0x8076C4F0`, confidence 1.0 |

The last one is worth all the rest: it is exactly what happens to whoever
downloads it.

### What is left

- **Antivirus software.** An unsigned PyInstaller executable gives false
  positives; it is flagged in the README. Signing it costs money, and the
  honest alternative is that anyone who does not trust it uses the source.
- It has only been tested on this machine (Windows 11, Python 3.14, PyInstaller
  6.22). The `.spec` has nothing Windows-specific in it, but nobody has built it
  anywhere else.
- Startup takes a couple of seconds to unpack, which is the price of the single
  file. With `--onedir` it would not, at the cost of distributing a folder.

---

## Item names come out of `kItemNames[]`

**Done 14 Aug 2026.** It was the last version failure that gave no warning: the
names came from `data/gi.yml`, and there **the `gi` index is the position in the
file**, so a new item in the middle shifts everything behind it. It does not
break: it gets the name wrong and says nothing.

### Where it is

It is written down in the repo, none of it had to be guessed:

- `packages/generator/include/combo/gi.h` → `extern const char* const kItemNames[];`
- `packages/generator/src/common/text/text.c` → `itemName = kItemNames[gi - 1];`
  (confirming the `- 1` already deduced from `data.ts`)
- `packages/generator/lib/combo/codegen.ts` generates it by walking `GI` in
  order, which is `gi.yml`'s order.

And it lives in the **payload**, which is another file of the extra DMA. From
`combo/defs.h`:

| | VROM | Loaded at | Size |
|---|---|---|---|
| OoT payload | `0xF0000000` | `0x80400000` | `0x80000` |
| MM payload | `0xF0100000` | `0x80720000` | `0x60000` |

Since the payload is loaded whole and in one piece, **a pointer inside it is
`PAYLOAD_RAM + offset in the file`**. That is what makes it readable from the
ROM without an emulator: the pointer is resolved by subtracting the base.

### Locating it by content, not by address

One more address would be one more version constant, i.e. the very problem this
is here to remove. It is searched for by shape: **the longest run of
consecutive u32s that fall inside the payload and all point at a string**.

In today's seed there is exactly one run of **936**, which is `gi.yml`'s 936
entries, in both payloads. But there is a second run that is also 100% strings:

```
+0x039084   936 ptrs   strings: 100.0%   <- kItemNames
+0x0480CC   822 ptrs   strings:  38.7%
+0x046308   254 ptrs   strings: 100.0%   <- region names, for the hints
```

What separates them: **item names carry a colour code and region names do
not.** 927 of 936 have a control byte; the 254 have none. With that second
filter the identification is unambiguous without depending on which is longer.

### The text, and why it is cleaned differently per game

Both payloads carry **the same words with different encodings**:

```
OoT:  b'the \x05AMegaton Hammer'      0x05 + a colour byte
MM:   b'the \x01Megaton Hammer'       a single byte
```

That is why the cleaner is per game. After that, the usual: strip the article
and collapse whitespace, so it ends up with the same shape the spoiler wrote,
which is what the junk rules are written against.

### What was measured

With the seed being played (`dockiNAq`):

- 936 names read, `gi.yml` agrees on **901 of 927**.
- The 26 that do not: all of them `Rusty Key (...)`, with the ROM giving the
  right name (`Rusty Key (Market Treasure Chest Game)`) and the file an old one
  (`Rusty Key (Treasure Chest Game)`).
- **None of those 26 is placed in this seed** (0 of the 317 `gi` it uses),
  because that feature —locking doors that have no lock— is not enabled. That is
  why `checks.json` comes out **byte for byte identical** after the change,
  which is the best regression test one could ask for.

And with the other ROMs in `Downloads`, which is where you see what this is for:

| | names | agreement with `gi.yml` |
|---|---|---|
| 17 files (`Siixg4Kf`, `7NxgFEzA`, `BIEwYjtP`…) | **829** | 136/822 |
| 11 files (`dHN9YY2c`, `f5PCTnhD`, `Lunes`, `xmMVaicW`…) | 936 | 927/927 |
| `dockiNAq` | 936 | 901/927 |

The 829 ones are from a considerably earlier version, and there the shift is
total:

```
gi 200   gi.yml: Dungeon Map (Jabu)     ROM: Compass (Water)
gi 600   gi.yml: Giant's Mask           ROM: Goron Lullaby
gi 800   gi.yml: Soul of Lulu           ROM: Nayru's Love
```

> **Re-measured on 15 Aug 2026 over every `.z64` on the disk, 42 OoTMM seeds by
> then, and it turned up a third generation.** The counts are 12 files at 936,
> **29 at 829 and one at 784** (`uuwB9jCT`), so **30 of 42** disagree with the
> bundled file. The 784 one shifts just as hard —`gi 200` is
> `Silver Rupee (Spirit Lobby)` and `gi 600` is `Dungeon Map (Great Bay)`— and
> agrees on 135/777, the same 17%.
>
> **It is not multiworld**, which was the obvious suspicion: the two ROMs of a
> multiworld seed carry byte-identical name tables. Checked on `7bFMIRol`
> (829/829) and `dHN9YY2c` (936/936). The split is purely by generator version.
>
> The seven files that return 0 are the base ROM, Super Mario 64 and vanilla
> OoT: no extra DMA header, so `find_item_names` returns `None` instead of
> blowing up, which is the intended behaviour.

So the bug was not a hypothesis: it was live across half the folder.

### When it cannot, it says so

`gi.yml` stays for the **symbol** (`OOT_BOMBS_5`), which does not survive
compilation and therefore is not in the ROM. But it is only used while the file
is still aligned: its names are compared with the ROM's and below **90%
agreement** the `item_id` is dropped and it explains why.

The proof that the guard works is inserting the fault by hand. With a fake item
inserted at position 20 of `gi.yml`:

```
names: 936 read from the ROM's kItemNames; gi.yml agrees on 29/928
  WARNING: data/gi.yml does not line up with this ROM.
```

and the names still come out right, because they no longer depend on that file:
`gi 24` gives `Spooky Mask`, which is what is there, instead of the shifted
`Skull Mask` it would have given before.

With no locatable payload it falls back to `gi.yml` **saying so**, and a ROM
that is not OoTMM's —tested with Super Mario 64 and with vanilla OoT— returns
`None` instead of blowing up: its extra DMA header does not exist and
`struct.error` was escaping because it was not in the `except`.

### What this does NOT fix

The **xflag tables** are still v32.0 `custom.h` constants. Those 17 still abort
in `mkchecks` with 72% impossible bits, and rightly so. This fixes the names,
not the addresses.

---

## The xflag tables are located by shape

**Done 14 Aug 2026.** It was the last hardcoded address of the important ones,
and the one that had already broken once. Now `locate_xflag_tables()` finds them
on its own and `custom.h`'s constants stay **as a cross-check**: if what turns
up is not where they say, it says so and uses what was found.

### What made it easy

Before writing anything, looking at the whole extra DMA of two ROMs from
different families. It was all there:

```
dockiNAq (current)                        Siixg4Kf (old)
0x080b0f00-0x080b0fca    202 B  raw       0x080948d0-0x0809499a    202 B  raw
0x080b0fd0-0x080b10ec    284 B  raw       0x080949a0-0x08094abc    284 B  raw
0x080b10f0-0x080b41c8  12504 B  raw       0x08094ac0-0x08097b98  12504 B  raw
0x080b41d0-0x080b42b4    228 B  raw       0x08097ba0-0x08097c84    228 B  raw
0x080b42c0-0x080b43ac    236 B  raw       0x08097c90-0x08097d7c    236 B  raw
0x080b43b0-0x080b6708   9048 B  raw       0x08097d80-0x0809a0d8   9048 B  raw
```

**Each table is its own entry and is uncompressed**, and the six sizes are
identical across versions: they had only moved by `0x1C630`. There was no need
to search inside any file.

As a bonus: `scenes` and `setups` are **byte for byte equal** in both families.
The only thing that changes in content is `rooms`, which is the data being read.

### The criterion

The three tables are a chain, and a chain is recognisable by its shape:

```
scenes[]  u16, non-decreasing, starts at 0, indexes setups[]
setups[]  u16, non-decreasing, starts at 0, indexes rooms[]
rooms[]   s16, the bit; no ordering at all
```

A candidate is three consecutive uncompressed entries where the first two have
that shape and **each one indexes inside the next**: `max(scenes) < len(setups)`
and `max(setups) < len(rooms)`. That last part is what makes the criterion
strong: it is not "it looks similar", it is that **the chain closes**.

Which one belongs to which game comes from `scenes.yml`: OoT's ids reach 100 and
MM's 113, so the table has to be long enough. The generator emits OoT's first
and that has held in all 29, but the fit is checked rather than assumed.

### Measured over the 29 ROMs in `Downloads`

| | |
|---|---|
| chains found | **exactly 2 per ROM**, 0 false positives |
| shape, across all 29 | OoT `101 / 142 / 6252`, MM `114 / 118 / 4524` |
| current ROMs (12) | returns **precisely the constants** `0x080B0F00` / `0x080B41D0` |
| old ROMs (17) | `0x080948D0` / `0x08097BA0` |

> **Re-run on 15 Aug 2026 over every `.z64` on the disk.** 49 files, of which
> 42 are OoTMM seeds: **all 42 give exactly two chains and both tables located,
> zero false positives**, now spanning three generations of the generator
> rather than two. The other seven —the base ROM, vanilla OoT, Super Mario 64—
> raise `ValueError` from `rom.extra_dma()`, which is the intended "that is not
> an OoTMM seed" and not a miss: vanilla OoT is the structurally closest thing
> there is and it still does not tempt the detector.

That it returns the constants on the current ones is what turns the change into
a **checkable no-op**: `checks.json` comes out byte for byte identical, and if
it did not, the locator would be wrong.

### The second barrier, which is the interesting part

With the tables located, the old seed stopped aborting and went on to resolve
the 4,751 xflags... writing a `checks.json` with **30 collisions** (22 OoT, 8
MM), all of them `Boulder`. The pool CSVs are v32.0's, that version has actors
the old one does not, and their rows land on other checks' bits.

That is: **the change, as it stood, made things worse.** Before, an old seed
aborted and left the good `checks.json` where it was; now it clobbered it with
one in which 30 checks mark each other. Exactly what the rule about the fallback
having to be better than nothing, not worse than what you already had, says.

So `collisions()` counts the pairs that share a bit without being vanilla/MQ
—which is the only legitimate coincidence— and aborts **before writing**:

```
ABORTED: 30 pairs of checks share a bit without being
a vanilla/MQ pair. The tables were found, so the addresses are right;
what does not match this ROM is data/pool_*.csv, which is v32.0's.
checks.json is left untouched; whatever was there still stands.
   example: Lost Woods Rupee Arrow 1 / Lost Woods Boulder Early
```

And the diagnosis is precise, which is what counts: **it does not say "I
cannot", it says what does not line up**. Before this, the same seed said "the
tables do not match the constants", which would now be a lie.

The collision warning already existed, but it was printed *after* writing the
file and only as one more line among forty. Existing is not the same as
stopping.

### Loose end

The **pool CSVs** are now the visible version dependency. Until the rows come
out of the ROM or the CSV is chosen by version, old seeds will stop at that
second barrier, which is the right behaviour.

And along the way: a ROM that is not OoTMM's gave a `struct.error` about buffer
sizes. Now `rom.extra_dma()` checks once and says
`it does not look like an OoTMM seed`.

---

## The payload's globals come out of its code

**Done 16 Aug 2026.** Everything measured is in the corpus folder's
`FINDINGS.md`; this is the short form.

Three things were still constants of one build, or measured at run time from
bits: the buffer of the game that is NOT running (`KNOWN_BASES`),
`gSharedCustomSave` (the window sweep, `CUSTOM_BEFORE`), and the layout inside
it (`CUSTOM_OOT`, `CUSTOM_MM`, `CUSTOM_MM_OFF`, `XFLAGS_COUNT`). All three are
globals of the payload, and the payload is MIPS code linked at a fixed address:
`save.c` calls `Flash_ReadWrite(0x18000 + 0x4000 * fileIndex,
&gSharedCustomSave, sizeof(gSharedCustomSave), dir)`, and that call site
carries the address (`lui`/`addiu`) and the size (`li`) in the instructions.

`payload.py` walks each payload once, keeps every `lui` + low-half pair
(with `addu` propagation, so array indexing is seen), snapshots `a0..a3` at
every `jal`/`j`, recognises `Flash_ReadWrite` by its calling shape (`a1 =
&global`, `a2 = literal`, `a3 in {0,1}`, several distinct globals), and reads
the buffers off it: the one outside the payload is the running game's
`gSaveContext`, the one whose size repeats in both payloads is the shared
custom save, the other is the foreign buffer. The layout is a shape over the
offsets the code indexes from the custom base (`xflags[N] npc[32] shops[8]
scrubs[8] sr[16]` -> N, N+0x20, N+0x28, N+0x30 all indexed; the MM half at a
16-aligned B: B, B+M, B+M+0x20 indexed, B+M+0x24 touched).

| | v32.0 | dev-542a121 | 829-gen (x3 builds) | 784-gen |
|---|---|---|---|---|
| `gSharedCustomSave`, running OoT | `0x8044B570` | `0x8044C6A0` | `0x804430D0` / `0x80443100` / `0x80443110` | `0x8043F210` |
| sizeof | `0x8A0` | `0x8D0` | `0x870` | `0x740` |
| `XFLAGS_COUNT` oot / mm | `0x2FA` / `0x350` | same | `0x2E8` / `0x34A` | `0x25D` / `0x2C9` |
| `MmCustomSave` at | `+0x380` | `+0x380` | `+0x370` | `+0x2E0` |

42 of 42 seeds, one fit each, 0.14 s per ROM; the four RAM dumps confirm every
derived address (signatures at the derived bases, `halfDays = 0x3F`, the 12 and
7 xflag bits, the `shops[0] = 0x02` byte `CUSTOM_BASE` was first worked out
from). **The 829 generation is three builds** with the same layout and three
custom-save addresses, so even a per-generation table would have been wrong.

Wired in three places, each guarded:

- **`mkchecks.apply_payload_layout()`** overrides the constants with the ROM's
  values and prints them next to v32.0's when they differ; `checks.json`
  carries a `payload` block. Guard: v32.0 seed -> byte-identical `checks.json`
  plus the new block; dev seed -> `off`/`bit` identical, absolute `addr` and
  `anchors` moved by the constant delta the overlay used to absorb at run time.
  On `babwDeM1` (829-gen) **2,912 checks change (offset, bit)**: all 2,406 MM
  xflags (16 bytes off) and every npc/shop/scrub/silver-rupee of both games.
  They had been read from the wrong byte on every seed of that generation.
- **`overlay.custom_base()`** asks the ROM first (`rom_custom`): the address is
  accepted with **zero bits** as long as the other buffer sits where the ROM
  says, and with bits only above the threshold -- else the sweep runs as
  before. `custom_source` (`rom` / `measured` / `guess`) goes to `/state.json`,
  and the "scene checks done but no xflag" suspicion in `poll_once` does not
  fire on a ROM-named address. Guard: the four dumps give `/state.json`
  identical key for key except `custom_source: measured -> rom`; the fresh-save
  simulation (custom save zeroed) goes from `trusted: false, guess` to
  `trusted: true, rom` at the same address. **This closes the "fresh save"
  item** that was blocking the release.
- **`ootmm.locate_saves(hints=...)`** tries the ROM's foreign buffers before
  `KNOWN_BASES`, and `discover` rebuilds a `checks.json` that predates the
  block.

Cross-checks that fell out of the same pass (offsets the code touches relative
to `gSaveContext`): OoT `gCowFlags` at `+0x1E0` (6 refs) and MM `perm` at
`MmSave+0xF8` = base+`0xF0` -- both were "pending in game" -- and MM `time` at
`MmSave+0x0C` = base+`0x04`, which identifies the `MM_NOISE` field; `+0x36` is
`tatlTimer` by the header. `gsFlags` is not referenced by the payload and stays
pending. There is **no version string** anywhere in the ROM; the code is the
only witness.

Fails closed: a build that passed the size through a register or wrapped
`Flash_ReadWrite` would leave `layout_complete()` false, the constants in place
and a line saying so.

---

## Live test of the fresh save, and two things it showed (16 Aug 2026)

Seed `8vXrrPP2`, generated and opened that day, `capture.py` recording
`/state.json` once a second plus the overlay's console. The prediction was
printed from the ROM alone before the emulator was touched, and it held:
bases `0x8011A5D0 / 0x8044BE18`, `gSharedCustomSave 0x8044B570` running OoT;
crossing to MM at 12:14:54 moved bases and custom save to `0x8076C4F0 /
0x801EF678 / 0x8076BC50` **in the same second**; `custom_source = rom`
throughout, confidence 1.0 at 0, 1 and 10 bits, `trusted` never dropped, and
Mido's four chests (scene checks with four xflags behind them) did not trip
the "scene checks and no xflag" suspicion. The RAM of that run is
`ram-fresh-mm.bin`, the fifth reference dump.

The same afternoon, the oldest seed on the disk (`uuwB9jCT`, Nov 2025, the
784-name generation): new file, then the co-op multiworld with its server.
Predicted `0x8043F210 / 0x8043F958` running OoT -- that MM buffer is not in
`KNOWN_BASES` and was found first try through the ROM's hint -- and
`0x8076F6C0 / 0x8076FE00` running MM, both observed; the co-op sync poured in
hundreds of checks and it ended at **368 checks, 190 xflag bits, confidence
1.0**, with the MM half at `+0x2E0` and `shops` at `+0x27D` (a shop check
marked right that the constant would have put 29 bytes off). Dump:
`ram-old-uuwB9jCT.bin`, the first from another generation.

Two things the record showed, both fixed and both guarded, plus two small
ones from the second session (Tingle's maps are filler now: `^World Map of `
in `JUNK_PATTERNS`; and a drop of the done count below half has to hold three
polls before it is believed -- a poll landing mid-crossing read everything as
zero and the feed re-announced the whole session, `DONE_DROP_POLLS`):

- **Rows the seed does not shuffle were shown as pending.** In Kokiri Forest
  31 of the 52 "still to do" had no item -- grass, rocks, rupees the ROM does
  not place because the category is vanilla. The rule is the pool's own: the
  CSVs are a superset and the ROM the census, so once the placement table has
  been read (`placement.resolved >= 100`) a row without an item is not a
  location in this seed. `is_active()` applies it, `not_in_seed` goes to the
  state and the page says it next to the percentage. On the four full-shuffle
  dumps the total goes 5,012 -> 4,956 and `not_in_seed = 56` -- exactly the 56
  the generator removes by hand, already counted in the essay; on the fresh
  seed 5,012 -> 2,415 with 2,597 out. Done counts unchanged everywhere.
- **Chests and collectibles appeared on leaving the scene; xflags and npc at
  once.** Not the tracker's doing: `mark.c` calls `SetChestFlag(play, ...)` /
  `Flags_SetCollectible(play, ...)` for the current scene -- the PlayState's
  live flags -- and `perm[scene]` only gets them when the game saves the scene
  on exit. `with_live_flags()` now reads the loaded scene's live flags
  (`play+0x1D28` OoT / `play+0x1E58` MM, `chest` at +0x10 and `collect` at
  +0x1C in both; vanilla structs, verified against the OoTMM headers and
  zeldaret/mm) and ORs those two words onto the live scene's perm entry before
  the checks are read. Only that scene, only those two words, only OR; MM's
  scene aliases (`mmSceneId()`) resolved by name from `scenes.yml`. Guard: a
  dump with collectible bit 10 set by hand in Clock Town South's PlayState
  marks `Clock Town Platform HP` (20 -> 21) and drops it from pending; the same
  bit in `clearedRoom` marks nothing; the five real dumps keep their counts.

---

## Multiworld, first session

**14 Aug 2026, and it closes P4** — the question that had been open since the
first day of the project.

It works. Through a real multiworld session the tracker picked up the player's
own checks exactly as in single player, which means the emulator does run
`tracker.lua` alongside the multiworld client's `adapter.lua`. The fallback plan
in the backlog —forking the multiworld script to carry the emitter inside— is
not needed.

Two limits, and only one of them was fixable:

- **It cannot see the partner's world**, and nothing here will change that. The
  tracker reads this machine's RAM; the other player's progress is not in it.
  Seeing it would mean talking to the multiworld server, which is a different
  program.
- **It did not say whose an item was.** This one was pure oversight: the ROM's
  placement table carries `player` per entry, `placement.py` was already reading
  it and `mkchecks` was already writing it into `checks.json` —2,821 rows in the
  seed being played— and then **neither `overlay.py` nor `overlay.html` ever
  looked at it**. The data had been sitting in the file all along.

Fixed: `Tracker.worlds` maps `(game, name) -> player`, the feed and the
remaining list carry `world`, and the page draws a `world N` marker. Measured on
`ram-en-oot.bin` with a multiworld seed's tables: of 8 remaining checks in
Kokiri Shop, 4 come out marked `world 2` and 4 unmarked.

> **What the absence of a marker means.** `mkchecks` only writes `player` when
> it is not yours, so no marker means "yours, as far as the ROM says" — not a
> guarantee. In a plain single-player seed the field is never set and nothing is
> drawn, which is the behaviour you want.

Still open: **P6**, whether the co-op mailbox carries local items or only
crossed ones, and whether a session survives hours with both scripts loaded. One
session is not a soak test.
