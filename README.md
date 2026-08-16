# OoTMM Autotracker

An autotracker for the [OoTMM](https://github.com/OoTMM/OoTMM) randomizer.
It reads your run from Project64-EM and shows it as an overlay you can drop
straight into OBS: inventory, songs, masks, equipment, upgrades and checks.

**No spoiler log needed.** What item sits in each location is read from the
seed ROM itself.

> **0.1.0-beta.** Single player is tested and measured; multiworld has had one
> session. Expect rough edges, and please report what you find.

## Install

1. **[Download the latest release](../../releases/latest)** and unzip it.
2. Double-click `ootmm-tracker.exe`. It finds your ROM, builds its tables and
   icons from it the first time, and opens the overlay.
3. In Project64-EM, with the ROM loaded: **File > Lua Scripts…**, and run
   `tracker.lua`. The tracker has already put it in the emulator's `Scripts`
   folder for you. Either order works.

That is all. There is nothing to configure and no paths to type in.

> **Changing seed? Restart the tracker.** It works out which seed you are on
> once, when it starts. Swap the ROM without restarting and it keeps building on
> the previous seed's tables — every item it names is then from the wrong game.
> It now says so on the page when it notices, but restarting is the fix.

> **Windows will warn you.** The `.exe` is not signed, so SmartScreen shows
> *"unknown publisher"*: click **More info → Run anyway**. Some browsers also
> refuse the download, which is why the release ships a `.zip`. Every release
> lists the file's SHA-256 so you can check what you got, and if you would
> rather not trust a binary at all, running
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
| ✅ 5,981 of 6,043 checks | the missing 62 are `caughtFishFlags` and MM stray fairies |
| ✅ Item placement and names read from the ROM | no spoiler log, on v32.0 and dev builds |
| ✅ Addresses read from the ROM's own code | `gSharedCustomSave` and the other game's save buffer are found in the payload's code, so a build that moves them needs no update; measured over 42 seeds, six builds |
| ✅ OBS overlay, one URL per panel | transparent background, five standalone panels |
| ⚠️ Multiworld | tracks your own world; see below |
| ❓ Emulators other than Project64-EM | not tried yet |
| ❌ Entrance tracking, logic and maps | not attempted |

### Multiworld

Every player runs their own ROM, save and tracker. Set it up exactly as above.
From the first real session:

- **Your own world tracks correctly**, the same as in single player.
- **It cannot see your partner's world.** It reads your machine's memory, and
  their progress is not in it.
- **It marks the spots holding someone else's item** with a `world N` tag,
  wherever the ROM records it. No tag means "yours, as far as the ROM says".

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

MIT — see [`LICENSE`](LICENSE). Third-party material is covered in
[`THIRD-PARTY.md`](THIRD-PARTY.md).

## For developers

- [`DEVELOPING.md`](DEVELOPING.md) — running from source, the subcommands, the
  file map, and how to test without an emulator.
- [**Placement Without the Spoiler**](placement-without-the-spoiler.md) — how
  the item placement, the names and the xflag bit positions come straight out
  of the seed ROM. If you are writing a tracker yourself, this is the part
  worth stealing.
- [`ootmm-autotracker-poc.md`](ootmm-autotracker-poc.md) — the long one:
  addresses, offsets, what was verified and how, and the dead ends.
