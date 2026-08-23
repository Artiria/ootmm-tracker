# Developing

Everything the README leaves out. The **how and why** — addresses, offsets,
what was verified and how, and the loose ends — is in
[`ootmm-autotracker-poc.md`](ootmm-autotracker-poc.md). Read that before
changing anything.

## Running from source

1. Copy `Scripts\tracker.lua` into Project64-EM's `Scripts\` folder — it has to
   be in there for the emulator to list it — or run `python ootmm.py
   install-lua`, which finds the folder on its own. With the ROM loaded:
   **File > Lua Scripts…**, and run `tracker.lua`.
2. Then:

```
python ootmm.py overlay
```

Order does not matter — `tracker.lua` retries the connection until the daemon
is up, and reconnects if you restart it. The page is served from the start, so
you can set up an OBS source before the emulator is even running.

Only Python's standard library is needed. Build the `.exe` with
`python -m PyInstaller ootmm.spec`; `python release.py` does that and then
signs, verifies and zips it — see [Releasing](#releasing).

## Subcommands

| | |
|---|---|
| `overlay` | the watchable tracker (the default from the `.exe`) |
| `items` | both games' inventory in a loop, reporting changes |
| `checks` | completed checks, resolved to names |
| `watch ADDR:SIZE,…` | poll individual addresses |
| `dump ADDR:LEN` | dump a region (accepts `oot`, `mm`) |
| `find file PATTERN` | search a dump for a signature |
| `diff a b` | compare dumps |
| `proxy` | log which addresses the MultiClient uses |
| `install-lua` | copy `tracker.lua` into the emulator's folder |

`items` locates the save contexts by signature, calibrates noise for six
seconds (do not touch anything) and from there reports every change. It
survives switching between OoT and MM: when you cross over, it relocates the
bases by itself.

## Where the item data comes from

**No spoiler is needed**: what item sits in each location is read from the ROM
itself (`placement.py`, the `COMBO_VROM_CHECKS` table), which is where the game
gets it from. That is enough for the junk filter and for showing pending checks
with their item. The whole technique is written up in
[Placement Without the Spoiler](placement-without-the-spoiler.md).

The **names** come from the ROM too, from `kItemNames[]` in the payload. That
way they do not depend on `data/gi.yml` being from the same OoTMM version as
the seed: that file is indexed by position, so with an older seed the names
came out shifted and nothing said a word. It is kept only for the symbolic id
(`OOT_BOMBS_5`), and only while its names still agree with the ROM's.

The **Load spoiler…** button in the director view stays as a fallback, for ROMs
whose table cannot be read. It checks version, name agreement and coverage
before accepting the file, and whatever you load takes precedence over what was
read from the ROM.

## Files

| | |
|---|---|
| `ootmm.py` | the tool |
| `paths.py` | what ships with the program and what it generates (matters inside the `.exe`) |
| `ootmm.spec` | PyInstaller recipe |
| `version.py` | the version, in one place |
| `Scripts/tracker.lua` | the memory server that runs inside Project64-EM, over its Lua socket |
| `Scripts/tracker-bizhawk.lua` | the same for BizHawk, over shared memory (`comm.mmf*`): no socket, no relaunch |
| `mmflink.py` | the tracker's side of that shared memory, with the same `read`/`read_block` as the socket link |
| `inventory.py` | both games' inventory map and the id table |
| `mkchecks.py` | generates `checks.json` from `data/` and the ROM |
| `placement.py` | what item is in each location and what it is called, read from the ROM |
| `payload.py` | where OoTMM's own globals live (`gSharedCustomSave`, the other game's save buffer, the layout inside), read from the payload's MIPS code |
| `souls.py` | the soul shuffle's catalogue and bitmaps, read from the ROM |
| `entrances.py` | the seed's shuffled entrances, read from the ROM (`COMBO_VROM_ENTRANCES`) and labelled with `data/entrances.yml` |
| `mkicons.py` | extracts the icons from the ROM |
| `discover.py` | finds the ROM and spoiler, regenerates whatever is stale |
| `overlay.py` / `overlay.html` | the tracker you actually look at |
| `rom.py` | reading the ROM: Yaz0, dmadata, extra DMA |
| `fakelua.py` | a fake `tracker.lua` that serves a dump: testing without an emulator |
| `fake_mmf.py` | the same for the BizHawk path: serves a dump over the shared memory |
| `capture.py` | records a live session: prints what the ROM predicts, runs the overlay, keeps `/state.json` and the console, dumps RAM at the end |
| `release.py` | builds, signs, verifies and zips the `.exe`; refuses to zip an unsigned build unless told to — see [Releasing](#releasing) |
| `data/` | data from the OoTMM repository (pool, scenes, npc) — see [`THIRD-PARTY.md`](THIRD-PARTY.md) |

## Testing without an emulator

`fakelua.py` stands in for `tracker.lua`: it connects to the daemon's port and
serves a RAM dump over the same protocol. That runs the whole overlay end to
end, which is the only way to test the `.exe` — the `--dump` shortcut exists
only on `items` and `checks`, and it skips the link, which is exactly the part
packaging can break.

```
python ootmm.py overlay --no-window --port 13261
python fakelua.py ram-en-mm.bin 13261
```

`fake_mmf.py` does the same for the BizHawk path, over the shared memory the
tracker creates. Set `OOTMM_MMF_NAME` on both sides to run it next to a live
EmuHawk without the two sharing a mapping:

```
python ootmm.py overlay --no-window --bizhawk
python fake_mmf.py ram-en-mm.bin
```

The dumps themselves are not in the repository (they are save data). There are
two RAM layouts worth keeping one of each for:

```
playing OoT:  OoT save 0x8011A5D0 · MM 0x8044BE18
playing MM:   MM save  0x801EF678 · OoT 0x8076C4F0
```

The bases moving when you cross between games is the most dangerous gotcha in
the project: with fixed offsets the tracker works in OoT and reads garbage in
MM without raising a single error. That is why they are located by signature,
and why it is worth testing against both layouts.

### Guards worth repeating

Whenever the `.exe` is rebuilt:

- Run the code and the `.exe` side by side against the same dump with
  `fakelua.py` and compare `/state.json`. It should be identical apart from
  `uptime`.
- **Delete `%LOCALAPPDATA%\OoTMM-Tracker\checks.json` first.** Otherwise the
  `.exe` reuses the old file instead of exercising its own `mkchecks`, and the
  comparison proves nothing.
- Check the panels still serve, including the Spanish 301 slugs.
- **Kill any running `ootmm-tracker.exe` before rebuilding**, or PyInstaller
  fails with `PermissionError` on `dist\`. The one-file build spawns a child
  process, so killing the one you started is not enough — kill them by name.
- If `Scripts/tracker.lua` was touched, check the hard link survived
  (`fsutil hardlink list`): an editor that rewrites the file breaks it, and the
  emulator then keeps an old copy without saying so.
- **Kill by name after the guard too** (`taskkill /IM ootmm-tracker.exe /F`):
  killing the process you started leaves the one-file child serving on the
  same ports, and the next comparison may be talking to the orphan. And read
  the `[auto] ROM:` line the console prints: once, on the very first launch of
  a fresh build, the tables came out as another seed's despite `--rom`; not
  reproduced since, but that line is what would show it.

## Releasing

```
python release.py
```

builds the `.exe` with the spec above and then, in this order: signs it, has
`signtool verify` check the signature (chain, timestamp, who signed), reads the
PE header itself to confirm a certificate table is there, runs the signed exe
once with `--help` (a one-file build finds its archive by scanning back from
the end of the file, which is exactly where the signature goes), zips it with
`LICENSE`, `THIRD-PARTY.md` and `tracker-bizhawk.lua` (BizHawk's Lua Console
opens a file, so the script ships next to the exe and the exe keeps that copy
current), reopens the zip to check that the exe inside
is the signed one, and writes the SHA-256 of both next to it. Any of those
failing stops the release; in particular **no certificate means no zip**, not
an unsigned zip that looks finished. `--unsigned` is the explicit way to
package without signing, and it names the file `*-unsigned.zip` so it cannot be
mistaken for a release later.

The certificate is Certum's *Open Source Code Signing* in the cloud
(SimplySign). It is only visible in `Cert:\CurrentUser\My` while SimplySign
Desktop is running and logged in, with the subject `Open Source Developer
<name>` — which is what the UAC prompt shows. `python release.py --check` says
which signtool and which certificate would be used, and whether the exe in
`dist/` is signed, without touching anything. `--no-build` signs and packages
the exe already in `dist/`; `--subject`, `--thumbprint` and `--tsa` override
the defaults (Certum's timestamp server first, DigiCert's as fallback). The
timestamp is not optional: it is what keeps the signature valid after the
certificate expires, and Certum's lasts a year.

Signing does not replace the guards above — the `/state.json` comparison, the
panels and the hard link are still by hand, and `release.py` says so at the
end. Then `gh release create v<version> dist/ootmm-tracker-<version>-win64.zip`.

What the user sees changes only in part: the UAC prompt names the publisher
instead of *unknown*, but SmartScreen builds trust from download counts, so a
fresh release can still warn until it has some. If it keeps warning after a
while, submit the signed file at Microsoft's
[Security Intelligence](https://www.microsoft.com/wdsi/filesubmission) page.

## What gets distributed

**Code, never art and never save data.** Everyone generates their own from
their own copy of the game, which keeps the package free of Nintendo material.
These are produced on each machine and are in `.gitignore`:

| File | What it is | Where it comes from |
|---|---|---|
| `icons.png` / `icons.json` | the item icons | `mkicons.py`, extracted from **your** ROM |
| `checks.json` | the 6,043 locations | `mkchecks.py`, from **your** ROM |
| `discover-cache.json` | paths and hashes | your emulator |
| `icons/*` | images you drop in yourself | you |

> The icons come out of each user's own ROM, **both games'**. OoT's live in
> `icon_item_static`; MM's in a CmpDma archive with every icon compressed
> separately, and that is where the 24 masks come from. Anyone who wants to
> replace one with a different image can drop it in `icons/` (see
> [`icons/README.md`](icons/README.md)); that never ships in the package.

`adapter-tracker.lua`, used by `proxy` mode, is **not** distributed: it is a
verbatim copy of the MultiClient's `adapter.lua` with the port changed, which
makes it someone else's code. Anyone who wants that mode can make it from their
own.
