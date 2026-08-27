# OoTMM Autotracker

An autotracker for the [OoTMM](https://github.com/OoTMM/OoTMM) randomizer.
It reads your run from Project64-EM or BizHawk and shows it as an overlay you
can drop straight into OBS: inventory, songs, masks, equipment, upgrades and
checks.

**No spoiler log needed.** What item sits in each location is read from the
seed ROM itself.

> **0.1.6-beta.** Single player is tested and measured; multiworld has had a
> few sessions. Expect rough edges, and please report what you find.

## Install

1. **[Download the latest release](../../releases/latest)** and unzip it.
2. Double-click `ootmm-tracker.exe`. It finds your ROM, builds its tables and
   icons from it the first time, and opens the overlay.
3. With the ROM loaded in your emulator, run the tracker's script. It waits
   for whichever you open:
   - **Project64-EM**: **File > Lua Scripts…**, and run `tracker.lua`. The
     tracker has already put it in the emulator's `Scripts` folder for you (and
     there is a copy in the `Scripts` folder next to the exe if it hasn't).
   - **BizHawk**: **Tools > Lua Console > Script > Open Script…**, and pick
     `tracker-bizhawk.lua` from the `Scripts` folder next to the exe. It talks
     over shared memory: no restart, nothing to set up.

   The two scripts are **not** interchangeable — `tracker.lua` is for
   Project64-EM, `tracker-bizhawk.lua` for BizHawk; both are in `Scripts`.
   Either order works. If you restart the tracker and the page stays on
   *waiting*, load the script again.

That is all. There is nothing to configure and no paths to type in.

> **Changing seed? Just change it.** The tracker follows the ROM the emulator
> has open: it notices within seconds, rebuilds its tables for the new seed —
> the page says so while it works, and it takes a moment — and carries on with
> the new one. If the emulator stopped `tracker.lua` on the way (loading a ROM
> can), run it again: the tracker is still listening and picks it up. Nothing
> to close and reopen.

> **Windows may still warn you.** The `.exe` is signed as *Open Source
> Developer Juan Ramos Ruiz* (a Certum certificate), so Windows names the
> publisher instead of showing *unknown* — but SmartScreen builds trust from
> download counts, and a fresh release can still show *"Windows protected your
> PC"*: click **More info → Run anyway**. Some browsers also refuse `.exe`
> downloads, which is why the release ships a `.zip`. Every release lists the
> file's SHA-256 so you can check what you got, and if you would rather not
> trust a binary at all, running
> [from source](DEVELOPING.md#running-from-source) does exactly the same thing.

Anything the tracker generates goes to `%LOCALAPPDATA%\OoTMM-Tracker\`, not
next to the executable.

## In OBS

The whole thing is at `http://127.0.0.1:8013/`, and each panel is also served
on its own so you can place them separately. Add one **Browser Source** per
panel:

| Panel | URL |
|---|---|
| Summary and counts | `http://127.0.0.1:8013/p/summary` |
| Progress by region | `http://127.0.0.1:8013/p/regions` |
| Item grid | `http://127.0.0.1:8013/p/items` |
| Activity feed | `http://127.0.0.1:8013/p/activity` |
| Remaining in the area | `http://127.0.0.1:8013/p/remaining` |

Add `?chroma=none` for a transparent background. The main page has a menu that
builds these URLs for you with your options already in them, and a **Hide
spoilers** switch that is always within reach.

By default the overlay shows what you have already picked up and stays quiet
about what is still out there — someone watching saw you pick the first up
anyway. `?spoiler=full` reveals what is in the checks you have not done.

## What works and what does not

| | |
|---|---|
| ✅ Single player, OoT and MM, including crossing between them | tested against live saves and four RAM dumps |
| ✅ 6,043 of 6,043 checks | every location in the pool has an address (stray fairies and pond fish included) |
| ✅ Entrances gone through | read from the ROM, no spoiler; shown as you take them (`/p/entrances`), the whole list only with `?spoiler=full` |
| ✅ Souls (soul shuffle) | every soul read from the ROM, grouped by kind; a compact per-kind summary by default, the full names on demand (`/p/souls`) |
| ✅ Item placement and names read from the ROM | no spoiler log, on v32.0 and dev builds |
| ✅ Addresses read from the ROM's own code | `gSharedCustomSave` and the other game's save buffer are found in the payload's code, so a build that moves them needs no update; measured over 42 seeds, six builds |
| ✅ OBS overlay, one URL per panel | transparent background, a standalone panel each |
| ⚠️ Multiworld | tracks your own world; see below |
| ✅ Project64-EM and BizHawk | P64-EM over its Lua socket, BizHawk over shared memory; both tested live |
| ❓ Other emulators (RetroArch, Ares) | not tried yet |
| ❌ Logic and maps | not attempted — that is what The Last Tracker does |

### Multiworld

Every player runs their own ROM, save and tracker. Set it up exactly as above.
From the first real session:

- **Your own world tracks correctly**, the same as in single player.
- **It knows which world your ROM is**, and says so next to the version. That
  is not cosmetic: the ROM stamps every item with the player it belongs to,
  and the tracker used to take itself for world 1 — so on any world but the
  first the tags came out inverted, your own items marked as somebody else's
  and your partner's arriving unmarked.
- **It cannot see your partner's world.** It reads your machine's memory, and
  their progress is not in it.
- **It marks the spots holding someone else's item** with a `world N` tag,
  wherever the ROM records it. No tag means "yours".

## Support

The tracker is free and will stay free. Everything it does is in this
repository and nothing sits behind a payment.

If it has been useful and you feel like buying me something:
[Ko-fi](https://ko-fi.com/artiria) · [PayPal](https://paypal.me/JuanRamos633),
or the sponsor button on the repository page. To be clear about what the
donation is for: **it is for the tracker**, which is my own code and
distributes nothing from the games.

## Credits

None of this would exist without **[OoTMM](https://github.com/OoTMM/OoTMM)**,
the randomizer that combines Ocarina of Time and Majora's Mask, or without its
team. The tracker reads the structures they invented and leans on several data
files from their repository, listed in [`THIRD-PARTY.md`](THIRD-PARTY.md).

Thanks as well to the people in the OoTMM Discord, where the format questions
that are written down nowhere else get answered.

**No game assets are distributed here.** The icons are extracted from your own
ROM the first time you run it.

## License

MIT — see [`LICENSE`](LICENSE). Copyright © 2026 Juan Ramos Ruiz (Artiria).
Third-party material is covered in [`THIRD-PARTY.md`](THIRD-PARTY.md).

## For developers

- [`DEVELOPING.md`](DEVELOPING.md) — running from source, the subcommands, the
  file map, and how to test without an emulator.
- [**Placement Without the Spoiler**](placement-without-the-spoiler.md) — how
  the item placement, the names and the xflag bit positions come straight out
  of the seed ROM. If you are writing a tracker yourself, this is the part
  worth stealing.
- [`ootmm-autotracker-poc.md`](ootmm-autotracker-poc.md) — the long one:
  addresses, offsets, what was verified and how, and the dead ends.
