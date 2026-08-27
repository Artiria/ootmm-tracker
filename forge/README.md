# forge — seeds of any OoTMM version, to test the tracker against

The tracker reads everything from the ROM: where OoTMM's globals sit, the
placement, the names, the tables. All of that moves with OoTMM's build, and
the only seeds around to check it against were the ones friends happened to
play. OoTMM is MIT and its CI builds inside a public Docker image that already
holds the N64 toolchain, so any tag —or any commit of master— can be built
here in about a minute and asked for seeds with the settings a test needs.

What makes this more than "more ROMs": the build also yields the linker's
symbol table of each payload. The tracker finds `gSharedCustomSave`, the
other game's save buffer and the rest by reading the payload's MIPS code;
with `oot.sym` / `mm.sym` next to the ROM, the guard compares that against
the truth instead of against a census made by hand.

## Setup, once

Docker Desktop, running. Then the vanilla ROMs OoTMM needs (Ocarina of Time
1.0, Majora's Mask USA), in any byte order — they are converted and checked:

```
python forge/forge.py roms "D:\roms\oot.z64" "D:\roms\mm.n64"
```

They land in `forge/roms/`, which is in `.gitignore` like everything else
forge produces.

## Building seeds

```
python forge/forge.py make v32.3
python forge/forge.py make v29.0 v30.0 v31.0 v32.3 master --preset Default --preset Allsanity --players 2 --jobs 3
python forge/forge.py make v32.0 --from-spoiler "C:\Users\me\Downloads\OoTMM-abc\OoTMM-Spoiler-abc.txt"
```

Each ref gets its own container (`--jobs` of them at once), the image its
CI used (`ghcr.io/ootmm/toolchain:1.4` from v31 on, `ootmm-ci:1.0.0` before;
`--image` overrides), and generates one seed per job:

| | |
|---|---|
| `--preset NAME` | one seed per preset (`Default` if none is given): Default, Beginner, Blitz, Allsanity, Hell, Only OoT, Only MM… |
| `--players N` | a multiworld of N players with default settings: one `.ootmm` and one ROM per world |
| `--from-spoiler FILE` | the exact settings of a real seed, from its spoiler's `SettingsString` — the way to get *your* usual settings at another version |

**Keep a Master Quest seed in the matrix.** No preset turns MQ on, and MQ is
where the twins share an xflag bit, so nothing else exercises the code that
tells them apart. `--from-spoiler` on any seed that had MQ dungeons gives one;
the settings pick the dungeons afresh, so the built seed's own spoiler is what
says which. That is how the spoiler's MQ list turned out to have been read
wrong for months — it is written as `- Name` lines under the header, and only
the header was being read, so every MQ seed looked like it had none.

A ref is a tag (`v32.3`), `master`, or a commit sha; the label of a dev
build is `dev-<sha7>`, the same the website would print. Seeds are named
after version and job (`forge-v32.3-default`), so a rerun gives the same ROM.

Everything lands under `forge/out/<version>/`:

```
v32.3/
  oot.sym, mm.sym      the payloads' symbol tables (nm -n -S)
  data/                that version's gi.yml, names.ts, pool CSVs, scenes/npc/entrances
  build.json           sha, image, node, timings; setup.log, build.log, forge.log
  default/             OoTMM-<hash>.z64, the .ootmm patch, the spoiler, job.json
  multi2/              OoTMM-<hash>-Player1.z64, -Player2.z64, both patches, the spoiler
```

About a minute per version plus 25 s per seed on a 30-core machine; a 64 MB
ROM and ~70 MB per seed on disk. `forge.py list` says what is there.

## The guard

```
python forge/forge.py guard                     everything under forge/out
python forge/forge.py guard forge/out/v30.0
python forge/forge.py guard "C:\seeds\OoTMM-abc"   a folder of real seeds works too
python forge/forge.py make v32.3 --guard         build, then guard what was built
```

Per ROM:

| check | what it compares | without the build's files |
|---|---|---|
| `locate` | `payload.locate()` against `nm`: `gSharedCustomSave`, `gMmSave`/`gOotSave`, `gSaveContext`, `Flash_ReadWrite`, with sizes | skipped |
| `names` | `kItemNames` read from each payload, count against the symbol's size | skipped |
| `mkchecks` | `checks.json` builds without aborting — in a sandbox, the tracker's own `checks.json` is never touched | same |
| `placement` | each check's item, and whose it is in a multiworld, against the spoiler next to the ROM | same |
| `entrances` | shuffled entrances read from the ROM against the spoiler's | same |

The placement check compares the item the tracker read from the ROM against
the one the spoiler placed there. Both come from OoTMM's own naming but by
different paths, so the spellings the two are known to differ on are
normalised away — the game tag (`(OoT)`/`(MM)`), a trap's cloak (`Ice Trap
(cloaked as …)`), `Bottle of X`, `Gold Rupee`/`Huge Rupee`, `Small Key (Fire
Temple)`/`Small Key`, `Progressive Hookshot`/`Longshot` — so a genuinely
different item stands out. A location the seed did not shuffle (a cow with
cowsanity off) has no item read for it and is counted separately, not as a
mismatch.

`FAIL` is the guard's one provable-wrong signal: a location that lines up by
name yet holds a different item, or a wrong owner, on a version whose data the
tracker claims to support — or an abort/crash in `mkchecks`. `WARN` is
everything else worth a look, and the guard names each reason rather than
drowning in it:

- a location the spoiler and the tracker name differently (`rom-only` /
  `unknown-loc`) — two naming systems, not a placement error;
- positional collectibles (pots, grass, rocks…) and souls, which the spoiler
  and the ROM number or name differently, so they cannot be lined up by name
  (`soft` in the log);
- on a non-Player-1 multiworld ROM every owner reads backwards, because the
  tracker assumes it is Player 1 (a real, already-tracked limitation);
- MM's `kItemNames` is not located on v30 and older;
- a seed from another version than the tracker's bundled `data/` (v32.0): the
  tracker reads placement and names from the ROM but the pool CSVs and `gi.yml`
  no longer line up, so a few items come out wrong — the tracker flags this
  itself (`same_version_as_data`), so the guard treats those as expected;
- a Master Quest or ultra-shuffled (Allsanity) seed, where MQ locations carry
  an `MQ ` prefix the tracker's names lack and `mkchecks` cannot map MQ to
  scenes — placement verification there is best-effort.

So on the versions the tracker targets (v31–v32.3) a clean run is all `WARN`,
and a `FAIL` means a real placement bug or a version the tracker cannot handle
at all (the first run flagged the unreleased 943-generation on `master`, whose
check table moved and crashes `placement.read_tables`). The exit code is 1 when
any seed fails. Every seed's details go to `guard-<rom>.log` next to it (or
`forge/out/_guard/` for seeds elsewhere): the `mkchecks` output in full and
every placement disagreement, `HARD`/`soft`-tagged.

## What this does not cover

Only the static half: ROM → tables, payload, `checks.json`, placement. The
RAM dumps and the `/state.json` comparison with `fakelua.py` are still the
way to test what happens in a running game. And versions before v29.0 have
not been built with this: the layout of the repository changed more than
once before that, and `build-one.sh` knows the two layouts since v29.

## Files

| | |
|---|---|
| `forge.py` | the driver: `roms`, `make`, `guard`, `list` |
| `build-one.sh` | what runs inside the container: clone, install, build, generate, symbols |
| `guard_roms.py` | the checks; also runs on its own |
| `roms/`, `out/`, `.jobs/` | produced; in `.gitignore` |
