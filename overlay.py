#!/usr/bin/env python3
"""
overlay.py - the watchable tracker: state server + self-refreshing page.

`ootmm.py overlay` brings this up. It does three things:

  1. a thread that polls the game's memory through tracker.lua and keeps the
     state (checks done, both games' inventories, a feed of what is new)
  2. an HTTP server on localhost serving overlay.html and /state.json
  3. opens a window in app mode, with no browser chrome

For OBS there are two routes and both come from here: capture that window, or
point a Browser Source at the same URL, which is the better one because it
allows a transparent background (`?chroma=none`) and clean scaling.

Rebasing
--------
The addresses in checks.json are absolute and the bases move when crossing
between OoT and MM. Every check also carries `anchor` + `off`, so on each
startup the anchors are resolved by signature and recomputed. See rebase().

What can be read depending on the active game
---------------------------------------------
Everything, in both. Each game's save is located by signature, and the custom
save (`gSharedCustomSave`, where the xflags live) sits **right in front of the
buffer of the game that is not running**; see CUSTOM_BEFORE.

Even so, confidence is measured —what fraction of the bits that are set land
on a known check— because that is how we know we are reading what we think we
are, and how we choose between the two possible addresses. Below the threshold
the panel is marked untrustworthy rather than showing garbage. See
confidence().
"""

import collections
import hashlib
import json
import os
import re
import struct
import sys
import threading
import time
import urllib.parse
import webbrowser

import paths
from payload import MM_BASE_DELTA
# Both of these live in placement.py so that vetting a spoiler here and
# deciding which world a ROM is there cannot judge two spellings differently.
from placement import same_item
from version import STAGE_NOTE, __version__

# One block per anchor: how much to read from it to cover its checks. The
# amount is computed from checks.json itself in build_plan(); this is just the
# safety margin.
BLOCK_PAD = 0x40

# Below this we do not trust what is in the custom save.
CONFIDENCE_MIN = 0.90

POLL_SECONDS = 0.5
FEED_MAX = 40

# How long to wait before scanning RDRAM again for the PlayState. The scan
# reads all 8 MB, and without this it would run on every poll for as long as
# you sit on the title screen.
PLAY_RESCAN_SECONDS = 10.0

# And how many polls in a row the PlayState has to fail to validate before
# that scan is even considered. A scene transition rewrites the PlayState for
# exactly one poll, and with the cooldown alone every zone change that came
# more than ten seconds after the last one paid for a full 8 MB read. Over
# P64-EM that went unnoticed; over BizHawk, where every request is served
# between frames, it froze the game for seconds at every door (seen live,
# 19 Aug 2026: six zone changes, six scans). The game state is allocated once
# per boot, so a real move is one that stays.
PLAY_MISSES_BEFORE_SCAN = 6

# Same idea for the custom save window: it is one read plus a few hundred local
# scorings, so it must not run on every poll while a run has no progress yet.
CUSTOM_RESCAN_SECONDS = 10.0

# How often to look at which ROM the emulator has open. Project64 writes the
# path it loads to `Recent Rom 0` in its .cfg, and that is the one signal that
# says "the ROM changed under the tracker": the placement ratio it used to go
# by is a property of the seed (a seed with little shuffled sits at 0.36 with
# nothing wrong) and cannot tell one seed from another of the same version.
ROM_CHECK_SECONDS = 10.0

# How many locations the ROM's placement table has to resolve before "the ROM
# did not place it" is taken to mean "this spot is not a check in this seed".
# Any real seed places hundreds (chests alone are 500-odd); below this the
# table is treated as unread and every row is shown, as it always was.
PLACEMENT_KNOWN_MIN = 100
# How many of a scene's other-room pending checks travel in pending_here for the
# "whole scene" toggle. Every normal scene is well under this; GROTTOS, the one
# scene that holds hundreds, is capped and the page says how many it left out.
OTHER_ROOM_CAP = 80
# Scenes whose "remaining" panel ignores the room by default. Gerudo Fortress's
# archery crates are filed under room 1 and shot from horseback across the
# scene, so a room filter hides the very checks being hit. The Rooms control
# and ?rooms= still apply everywhere; this only flips the default here.
WHOLE_SCENE = {("oot", "GERUDO_FORTRESS")}

# Scenes that several places share, and how OoTMM tells the instances apart.
# GROTTOS holds every grotto of its game and FAIRY_FOUNTAIN the five fairy
# fountains, one room each, so the live scene and room say nothing about which
# one you are in. The game itself uses two things (payload.py says where they
# come from): the grotto byte the hole writes when it swallows you, whose low
# five bits are the generic grotto's id, and gLastScene, the place you came in
# from. The maps below are OoTMM's own -- EnElf_Aliases (En_Elf.c) for the
# fountains, ObjComb_InitXflag (Obj_Comb.c) for the scrub grottos and MM's cow
# grotto, comboXflagInit (xflags.c) for the generic ones -- and give the room
# checks.json lists that instance's checks under.
FOUNTAIN_ROOMS = {"HYRULE_FIELD": 0x20, "ZORA_RIVER": 0x21, "SACRED_FOREST_MEADOW": 0x22,
                  "ZORA_DOMAIN": 0x23, "GERUDO_FORTRESS": 0x24}
SCRUB2_ROOMS = {"SACRED_FOREST_MEADOW": 0x21, "ZORA_RIVER": 0x24,
                "GERUDO_VALLEY": 0x25, "DESERT_COLOSSUS": 0x26}
SCRUB3_ROOMS = {"LON_LON_RANCH": 0x27, "GORON_CITY": 0x2A,
                "DEATH_MOUNTAIN_CRATER": 0x2B, "LAKE_HYLIA": 0x2D}
COW_ROOMS = {"TERMINA_FIELD": 0x0A, "GREAT_BAY_COAST": 0x0F}
# (game, scene) -> {live room: ("grotto" | "last_scene", came-from -> room)}
SHARED_SCENES = {
    ("oot", "FAIRY_FOUNTAIN"): {0: ("last_scene", FOUNTAIN_ROOMS)},
    ("oot", "GROTTOS"): {0: ("grotto", None), 9: ("last_scene", SCRUB2_ROOMS),
                         0xC: ("last_scene", SCRUB3_ROOMS)},
    ("mm", "GROTTOS"): {4: ("grotto", None), 0xA: ("last_scene", COW_ROOMS)},
}
GROTTO_ID_MASK = 0x1F
# What the panel tags a check with, in a scene that is many places at once,
# when nothing in its data says which of them it is in -- neither an xflag nor
# its own name (mkchecks.assign_virtual_rooms). It is listed wherever you
# stand, as it always was, but no longer with the silence that reads as "here".
UNPLACED_IN_SHARED = "which one is not known"
# Outside a shared scene gLastScene IS the live scene; polls in a row where it
# is not before the address is given up as another build's. One poll can
# straddle a scene load (the PlayState is written before the hook that copies
# the scene), so a single disagreement means nothing.
LAST_SCENE_ODD_PERSIST = 6
# A spoiler is refused when, over at least this many spots both it and the
# ROM name, it agrees on fewer than this fraction (see Tracker._vet_spoiler).
SPOILER_MIN_COMPARABLE = 100
SPOILER_MIN_AGREEMENT = 0.5


# The live scene flags, inside the PlayState. Chests and collectibles are the
# two kinds of check that only reach the save context when the scene is left:
# OoTMM's mark.c calls SetChestFlag(play, ...) / Flags_SetCollectible(play, ...)
# for the scene you are in, and those write play->actorCtx flags, which the
# game copies into perm[scene] on exit. Measured live (16 Aug 2026): Mido's
# four chests appeared the second the player stepped out; pots and trees
# (xflags, written straight to gSharedCustomSave) appeared at once. So for the
# scene you are standing in, the live copy is OR-ed onto the saved one.
#
# Offsets are vanilla structs, verified against combo/{oot,mm}/play.h and
# actor.h (ASSERT_OFFSETs) and zeldaret/mm z64actor.h:
#   OoT  PlayState.actorCtx 0x1C24 + ActorContext.flags 0x104 -> 0x1D28
#        { swch, tempSwch, unk0, unk1, chest, clear, tempClear, collect, tempCollect }
#   MM   PlayState.actorCtx 0x1CA0 + ActorContext.sceneFlags 0x1B8 -> 0x1E58
#        { switches[4], chest, clearedRoom, clearedRoomTemp, collectible[4] }
#        (collectible[0] is the permanent word: Play_SaveCycleSceneFlags)
# In both, chest sits at +0x10 and the permanent collect word at +0x1C.
LIVE_FLAGS = {"oot": (0x1D28, 0x24), "mm": (0x1E58, 0x2C)}
# live word -> the perm field it is saved into. MM's stray fairies are switch
# bits (mark.c, setStrayFairyMarkMm), so its two permanent switch words count
# too; OoT keeps only chests and collectibles here.
LIVE_FIELDS = {
    "oot": {"chest": 0x10, "collect": 0x1C},
    "mm": {"switch0": 0x00, "switch1": 0x04, "chest": 0x10, "collect": 0x1C},
}
# MM keeps one flag table for a scene and its seasonal / inverted twin
# (mmSceneId() in mark.c); the twin's live flags belong to the base scene.
# The pairs live in mkchecks (MM_SCENE_ALIASES) because the same fold has to
# reach the rows' addresses when the table is built. Holding the list here as
# well is what broke the inverted Stone Tower chests for a whole release: this
# half folded the live scene, the table half did not exist, and three chests
# -- one of them a song -- could not be marked by opening them.

# How many polls in a row a big drop in the done count has to hold before it
# is believed. A poll that lands mid-crossing between OoT and MM reads the old
# bases while RAM is being rebuilt and everything comes back as zeros -- 23
# done -> 0 -> 23 within two polls, measured 16 Aug 2026 -- and believing that
# empty poll made the feed announce every check of the session again as new.
# Nothing a run has done ever comes undone (xflags and perm bits are only ever
# set), so a drop is either that transient or a different file being loaded,
# and the second keeps looking that way. Three polls is a second and a half.
DONE_DROP_POLLS = 3
# The mirror image of the drop: a JUMP of many checks in one poll. Mid-crossing
# the old bases can also read garbage instead of zeros -- 18 -> 126 -> 18 in
# three polls, measured 17 Aug 2026 -- and believing the middle one fed 108
# checks that did not exist and then re-announced the 18 real ones as new. A run
# does not do twenty checks in half a second; a coop save or a savestate loading
# does, and holds, so it goes through after the same DONE_DROP_POLLS.
DONE_JUMP_MAX = 20
# A feed entry whose check is not done any more this soon after appearing was a
# transient, not progress: it is taken back. Nothing a run has done ever comes
# undone, so nothing real is ever lost to this.
FEED_RETRACT_SECONDS = 5.0

# How many scene checks have to be done before "and not one xflag" counts as
# evidence that the custom save base is wrong. Low, because the two kinds of
# check are spread all over the game and doing several of one and none at all
# of the other does not happen by chance -- but not 1, so a single chest on a
# fresh file cannot trip it.
SCENE_CHECKS_SUSPICIOUS = 3

# Each panel is also served on its own at /p/<name>, so the streamer captures
# only the ones they want to show, wherever they want them. The names have to
# match the data-panel attributes in overlay.html.
PANELS = ["summary", "regions", "items", "activity", "remaining", "entrances", "hints", "souls", "notes"]

# The hint ladder: what a hint about an item gives away at each level.
HINT_LEVELS = {1: "game", 2: "region", 3: "check"}

# The old Spanish names still answer. They are what any Browser Source set up
# before the rename points at, and a renamed URL breaks an OBS scene silently:
# the source just goes blank. Cheap to keep, expensive to get wrong.
PANEL_ALIAS = {
    "resumen": "summary",
    "regiones": "regions",
    "actividad": "activity",
    "pendientes": "remaining",
}

# On top of that, any panel accepts ?game=oot|mm and narrows to a single game,
# so two separate overlays can be built. The director view generates those URLs
# as well.
GAME_PANELS = ["items", "regions", "summary"]


# --------------------------------------------------------------------------
# Plan de lectura
# --------------------------------------------------------------------------


# What counts as filler. It goes **by the item inside**, not by the kind of
# location: with the pool shuffled, any patch of grass can hold the Hover
# Boots, and in this very run `Kokiri Forest Rupee Child 2` gave a Swamp
# Skulltula Token.
#
# Frequency does not work either, which was the first idea: `Gold Skulltula
# Token` shows up 100 times and is not filler, and neither are the Stray
# Fairies. By name it separates cleanly, and the split confirms it: rupees,
# "Nothing", hearts, loose fairies and jars are 81% of the pool.
# Watch out for two traps that surfaced when checking the list against the
# spoiler:
#
#   - Puzzle rupees come in parentheses —`Silver Rupee (Shadow Temple -
#     Scythe)`— and are **not filler**: they can be needed to progress. That
#     is why the rupee pattern is exact and takes no suffix.
#   - Ammo can carry the game behind it (`5 Arrows (OoT)`), so that suffix has
#     to be allowed or it sneaks through as important.
JUEGO = r"( \((OoT|MM)\))?"
JUNK_PATTERNS = [
    r"^Nothing$",
    r"^(Green|Blue|Red|Purple|Silver|Gold|Huge|Rainbow) Rupee" + JUEGO + r"$",
    r"^Recovery Heart" + JUEGO + r"$",
    r"^(Small|Large) Magic Jar" + JUEGO + r"$",
    r"^(Big )?Fairy" + JUEGO + r"$",   # Stray Fairies are NOT: those are checks
    r"^\d+ (Arrows?|Bombs?|Bombchus?|Deku Seeds?|Deku Nuts?|Deku Sticks?)" + JUEGO + r"$",
    # the singular too: the spoiler writes "1 Bomb" and the ROM "Bomb"
    r"^(Deku Sticks?|Deku Nuts?|Deku Seeds?|Arrows?|Bombs?|Bombchus?)" + JUEGO + r"$",
    # "Milk" in the spoiler, "some Lon Lon Milk" in the ROM
    r"^(Lon Lon )?Milk" + JUEGO + r"$",
    r"^(Red|Green|Blue) Potion" + JUEGO + r"$",
    # The rest of that same family: what tops a bottle up. The potions, the
    # fairy and the milk above are OoTMM's `add: BOTTLE_REFILL` too, and these
    # were simply never listed -- a `Bug` sat in the feed as something that
    # mattered (his report, 25 ago 2026). The ROM writes the singular, the
    # spoiler sometimes the plural ("Bug" / "Bugs (OoT)"), so both are here.
    # NOT the bottle itself: "Empty Bottle", "bottled Poe", "Bottle of Milk"
    # and friends give you a bottle and stay checks -- the ^ anchor keeps them
    # out. Blue Fire and Gold Dust are deliberately NOT here either: red ice
    # and the Gilded Sword are the two things a refill is actually spent on.
    r"^(Fish|Bugs?|(Big )?Poes?|Seahorse|Magic Mushroom|Chateau Romani)" + JUEGO + r"$",
    # A Piece of Heart is a quarter of a heart and nothing else depends on it.
    # `Heart Container` joined it on 18 Aug (his call; on 16 Aug it had stayed
    # out for being a whole heart). `Recovery Heart` is already filler above.
    r"^Piece of Heart" + JUEGO + r"$",
    r"^Heart Container" + JUEGO + r"$",
    # Tingle's maps: nothing depends on them, and on a seed that shuffles his
    # shop the six of them sat as pending "key" checks in Clock Town South.
    # The ROM writes "World Map (Clock Town)", the spoiler "World Map of Clock
    # Town"; both are covered.
    r"^World Map\b",
    # Traps are the opposite of an item. The ROM names the trap; the spoiler
    # adds what it pretends to be: "Ice Trap (cloaked as Cojiro)".
    r"^(Ice|Fire|Shock|Drain|Anti-Magic|Knockback) Trap( \(cloaked as .*\))?" + JUEGO + r"$",
    # and a Rupoor takes rupees away
    r"^Rupoor" + JUEGO + r"$",
    # Dungeon maps and compasses: nothing depends on them (his call, 16 Aug).
    # The ROM says "Dungeon Map (Deku)" / "Compass (Deku)" and, unqualified,
    # "Dungeon Map" / "Compass"; the spoiler "Map (Deku Tree)".
    r"^(Dungeon )?Map( \(.*\))?" + JUEGO + r"$",
    r"^Compass( \(.*\))?" + JUEGO + r"$",
]

# Skulltula tokens sit in every "remaining" panel as pending noise on a run
# that is not collecting them: 144 Gold Skulltulas and the 60 of MM's spider
# houses (his complaint, 25 ago 2026). Whether they are noise or the point of
# the run is the player's call, and the player makes it: the `tokens_junk`
# switch, remembered between runs. See Tracker.set_tokens_junk.
#
# It was worked out from the seed at first, and that lasted a few hours: junk
# when every token still sat in its vanilla spot, a real check when the seed
# had moved one. The rule had to ask whether a token was YOURS, and at the
# time that was precisely what it could not tell on a multiworld ROM:
# placement.py called anything that was not player 1 somebody else's, so on
# world 2's ROM your own tokens read as your partner's. The rule then saw no
# tokens of yours at all, decided the seed had shuffled them, and filtered
# nothing; the same
# reading also made a seed look like it had tokensanity on when it did not.
# The tracker knows its world now (placement.world_of_rom), so that reason has
# gone -- but the switch stays, because it never needed to know: it does not
# care whose world anything is in, and there is nothing left in it to get
# wrong.
TOKEN_KINDS = {
    "GS_TOKEN": "Gold Skulltula Token",
    "GS_TOKEN_SWAMP": "Swamp Skulltula Token",
    "GS_TOKEN_OCEAN": "Ocean Skulltula Token",
}

# MM's stray fairies are the same kind of noise, and the same answer: 61 spots
# (15 in each of the four temples plus Clock Town's) that fill the "remaining"
# panel of a temple you are walking through (his complaint, 27 ago 2026 --
# "Woodfall Temple Center Chest -> Stray Fairy"). Whether they are noise or the
# point of the run is his call again: the `fairies_junk` switch.
#
# What the switch does NOT touch, and it matters: the Fairies tiles keep
# counting. They read strayFairies[] out of RAM and the Great Fairy rewards out
# of `done` (see fairy_info), neither of which asks anything about `junk` -- so
# the panel stops listing 61 rows and the "15/15, ready to deliver" still lights
# up. Hiding the count as well would be hiding the one thing a fairy is for.
#
# Matched by name, like the tokens, and by prefix on purpose: OoTMM has a
# generic "Stray Fairy" and one per dungeon ("Stray Fairy (Woodfall)" and
# friends, MM_STRAY_FAIRY_WF.. in data/gi.yml). This seed places the generic
# one in all 61 spots, but a seed that hands out the per-dungeon ones would
# leave every single fairy on the panel with an exact match -- and it would do
# it quietly, which is the failure this project keeps finding.
# "no world was asked for", which is not the same as world None -- None is
# yours. A name typed by hand into the item box carries no world and should go
# on matching any copy, the way it always did.
ANY_WORLD = object()

FAIRY_ITEM = re.compile(r"^Stray Fairy\b")
FAIRY_KINDS = {
    "STRAY_FAIRY_WF", "STRAY_FAIRY_SH", "STRAY_FAIRY_GB",
    "STRAY_FAIRY_ST", "STRAY_FAIRY_TOWN",
}

# --- pond fish --------------------------------------------------------------
#
# A fish is filler *or not depending on how much it weighs*: the fishing pond
# hands out a prize for a big enough one, and the logic says exactly how big
# (data/world/oot/overworld/lake_hylia.yml in OoTMM/OoTMM):
#
#   "Fishing Pond Child": ... has_pond_fish(CHILD_FISH, 7, 14) || ...
#   "Fishing Pond Adult": ... has_pond_fish(ADULT_FISH, 8, 25) || ...
#
# and the signature is has_pond_fish(ageAndType, minPounds, maxPounds), checked
# in packages/logic/src/expr/parser.ts. So a child fish counts from 7 pounds
# and an adult one from 8; under that it is filler.
#
# Loaches are deliberately not here. They only exist from 14 (child) and 29
# (adult) pounds, which are their own thresholds, so **every loach counts** and
# none of them is ever junk.
#
# The weight is parsed rather than baked into the digits of a pattern, so a
# version that adds new weights cannot quietly reclassify them.
PECES_MIN = {"Child": 7, "Adult": 8}
_PEZ = re.compile(r"^(Child|Adult) Fish \((\d+) pounds?\)" + JUEGO + r"$")


def pond_fish_junk(item):
    """True/False for a pond fish, None when the item is not one at all."""
    m = _PEZ.match(item)
    if not m:
        return None
    return int(m.group(2)) < PECES_MIN[m.group(1)]


def is_junk(item):
    """Whether that item is filler. With no item known, returns False."""
    if not item:
        return False
    pez = pond_fish_junk(item)
    if pez is not None:
        return pez
    return any(re.search(p, item) for p in JUNK_PATTERNS)


def load_table(path=None):
    """The check tables. A missing file is not fatal: the ROM was not found at
    startup, so mkchecks never ran. Come up with an empty table and let the
    page say so, instead of dying with a traceback in a console window that
    then vanishes. Everything downstream reads table["checks"] as a list and
    table.get(...) for the rest, so an empty table is a valid degraded state.
    A corrupt file is a different problem and still surfaces."""
    try:
        with open(path or paths.user("checks.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {"checks": []}


def build_plan(table, active_pred, junk_pred=None):
    """Group the checks by anchor and work out the range to read from each.

    Returns (plan, regions):
      plan[anchor]  = {"span": bytes to read, "checks": [...]}
      regions       = [(game, scene, total)] sorted, for the bars
    """
    plan = {}
    for c in table["checks"]:
        if c["addr"] is None or "anchor" not in c:
            continue
        p = plan.setdefault(c["anchor"], {"span": 0, "checks": []})
        p["checks"].append(c)
        end = c["off"] + 4
        if end > p["span"]:
            p["span"] = end
    for p in plan.values():
        p["span"] = (p["span"] + BLOCK_PAD + 3) // 4 * 4

    totals = collections.Counter()
    clave = collections.Counter()
    for c in table["checks"]:
        if c["addr"] is not None and "anchor" in c and active_pred(c):
            totals[(c["game"], c["scene"])] += 1
            # (game, name), never the bare name: see read_flags
            if junk_pred and not junk_pred(c["game"], c["name"]):
                clave[(c["game"], c["scene"])] += 1
    regions = sorted(totals.items(), key=lambda kv: (kv[0][0], -kv[1], kv[0][1]))
    return plan, [(g, s, n, clave[(g, s)]) for (g, s), n in regions]


def xflag_ranges(table):
    """The stretches of the `custom` anchor that are pure xflag bitmap.

    The confidence measure can only look here. The rest of the block holds
    fields that are not check flags —OotCustomSave's trailing bitfield,
    `mm.halfDays`, the counters— and their set bits are legitimate but map to
    nothing, so they would sink the measure with nothing actually wrong.
    """
    cs = table.get("custom_save", {})
    out = []
    for game, nxt in (("oot", "npc"), ("mm", "npc")):
        blk = cs.get(game) or {}
        if "xflags" in blk and nxt in blk:
            out.append((blk["xflags"], blk[nxt]))
    return out


# Where gSharedCustomSave lives. It has no signature of its own, but it **sits
# right in front of the buffer of the game that is NOT running**, at a distance
# that is a per-version constant.
#
# The distance was measured, not guessed. Both dumps are from the same run and
# the custom save is shared, so its contents have to appear verbatim in both:
# searching for the block from OoT's RAM inside MM's gives exactly one match,
# and the non-zero offsets line up one for one (0xd6, 0x1b2..0x1c1 of xflags,
# 0x31a shops, 0x376 the trailing bitfield, 0x6f4 halfDays). The only
# differences are the two checks the player did between one dump and the other.
#
#   running OoT:  MM buffer  0x8044BE18 -> custom 0x8044B570   (-0x8A8)
#   running MM:   OoT buffer 0x8076C4F0 -> custom 0x8076BC50   (-0x8A0)
#
# The distances differ because what sits in between is the other game's save,
# and they are not the same size.
#
# **AND THE DISTANCE MOVES BETWEEN VERSIONS.** On dev-542a121 the MM buffer
# went to 0x8044CF78 and the custom save to 0x8044C6A0, so the gap grew 0x30 to
# 0x8D8 -- the save in between got bigger. Held as a constant it put the anchor
# at 0x8044C6D0, confidence 0.077, which is below the threshold, so the whole
# `custom` anchor was thrown away: 4751 xflags and 506 bitmap checks silently
# stopped counting. That is why regions and the feed came out empty on the
# experimental seed while the pending list still looked fine.
#
# So these are the FIRST GUESS, not the answer. Miss, and we search the window
# below and measure the real gap.
CUSTOM_BEFORE = {"mm": 0x8A8, "oot": 0x8A0}

# How far back from the other game's buffer to look for it. Wide enough for the
# gaps seen so far (0x8A0, 0x8A8, 0x8D8) with room for the struct to keep
# growing, and it is only ever walked when the first guess fails.
CUSTOM_WINDOW = (0x800, 0x1000)


def custom_candidates(bases, active=None):
    """Possible custom save addresses, most likely first.

    The right one hangs off the *inactive* game; both are returned so we can
    fall back to the other if the first does not validate.
    """
    ajeno = "mm" if active == "oot" else "oot" if active == "mm" else None
    orden = [ajeno] if ajeno else []
    orden += [g for g in ("mm", "oot") if g != ajeno]
    return [bases[g] - CUSTOM_BEFORE[g] for g in orden if g in bases]


def custom_score(c, bits):
    """How good a custom save candidate is: CONFIDENCE FIRST, then bits.

    The order matters and getting it wrong picks the wrong address. On the
    dev-542a121 dump the real base (0x8044C6A0) had 7 bits at confidence 1.000
    while a neighbour 0xB4 further along had 14 bits at 0.929 -- going by bit
    count alone chose the neighbour, and the overlay then reported progress in
    Stone Tower and Spirit Temple on a run that had not left Link's House.
    Confidence is the measure that says "this is the thing we think it is";
    bits only break ties between addresses that are equally believable.
    """
    return (round(c, 3), bits)


# gSharedCustomSave is a 16-byte aligned global (OotCustomSave carries
# ALIGNED(16)), and every base ever measured obeys it: 0x8044B570, 0x8076BC50,
# 0x8044C6A0. Stepping by 4 instead let a misaligned neighbour win -- on the
# dev dump 0x8044C6B4 also scored confidence 1.0 with the same 7 bits, but they
# mapped to Lair Gohma and Zora River instead of Link's House and Kokiri
# Forest. Aligning drops three quarters of the candidates and, on that dump,
# leaves the real base first at 1.000 against 0.857 for the runner-up.
CUSTOM_ALIGN = 16


def custom_window(bases, active=None):
    """(game, base, [addresses]) to sweep when the known gaps do not validate.

    The inactive game first, and within a game the addresses closest to the
    known gap first, so a version that moved the struct a little is found in
    the first few tries.
    """
    # ONLY the inactive game when we know which it is. Sweeping both let a
    # stray address hanging off the running game's buffer win by coincidence,
    # and the block does not live there.
    ajeno = "mm" if active == "oot" else "oot" if active == "mm" else None
    orden = [ajeno] if ajeno else ["mm", "oot"]
    lo, hi = CUSTOM_WINDOW
    out = []
    for game in orden:
        if game not in bases:
            continue
        base = bases[game]
        ini = -(-(base - hi) // CUSTOM_ALIGN) * CUSTOM_ALIGN  # round up to align
        esperada = base - CUSTOM_BEFORE[game]
        addrs = sorted(range(ini, base - lo, CUSTOM_ALIGN), key=lambda a: abs(a - esperada))
        out.append((game, base, addrs))
    return out


def rebase(table, bases, active=None, custom=None):
    """The real address of each anchor, given the bases located by signature."""
    out = {}
    if "oot" in bases:
        out["oot"] = bases["oot"]
    if "mm" in bases:
        out["mm"] = bases["mm"]
    if custom is not None:
        out["custom"] = custom
    else:
        cands = custom_candidates(bases, active)
        if cands:
            out["custom"] = cands[0]
    return out


# --------------------------------------------------------------------------
# Lectura de estado
# --------------------------------------------------------------------------


def read_flags(blob, checks, off0=0):
    """Which checks are done inside a block already read, as (game, name).

    The game has to be part of the key. Six check names exist in BOTH games --
    the Goron and Zora shop slots, which each game has three of -- and keying
    by the bare name let one game's purchase mark the other's slot as done,
    hide it from the panel, and (through the `junk` map, keyed the same way)
    lend it the other game's classification: OoT's Goron Shop Item 1 held a
    green rupee and read as something that mattered because MM's slot of that
    name held a Mask of Truth (his report, 25 ago 2026).
    """
    done = set()
    for c in checks:
        o = c["off"] - off0
        if o < 0 or o + 4 > len(blob):
            continue
        if c["kind"] == "u8":
            word = blob[o]
        else:
            word = struct.unpack_from(">I", blob, o)[0]
        if word & (1 << c["bit"]):
            done.add((c["game"], c["name"]))
    return done


def confidence(blob, checks, ranges):
    """(fraction of set bits landing on a known check, how many there are).

    This is the proof that we are reading what we think we are: if the custom
    save base is wrong, the bits land where nothing is mapped and the fraction
    collapses. It is measured only over the xflag stretches, which are pure
    bitmap; see xflag_ranges().

    **The fraction alone is not enough**, which is why the count comes back
    too: a wrong base landing in a zeroed area gives 1.0 vacuously, with no
    bits at all, and that would be silence rather than a warning. The caller
    compares the count against the highest it has seen; see Tracker.refresh().
    """
    known = {(c["off"], c["bit"]) for c in checks if c["kind"] == "u8"}
    hits = total = 0
    for lo, hi in ranges:
        for o in range(lo, min(hi, len(blob))):
            byte = blob[o]
            if not byte:
                continue
            for b in range(8):
                if byte >> b & 1:
                    total += 1
                    if (o, b) in known:
                        hits += 1
    return (hits / total if total else 1.0), total


# --- the player's own switches ---------------------------------------------
#
# One file for the whole tracker and not one per seed: what lives here is a
# preference ("I never care about skulltulas"), and having to tick the box
# again after every restart is the friction the switch exists to remove. A file
# that cannot be read is no options at all: a corrupt one must not stop the
# overlay from starting.
OPTIONS_FILE = "options.json"


def load_options():
    try:
        opts = json.loads(open(paths.user(OPTIONS_FILE), encoding="utf-8").read())
    except (OSError, ValueError):
        return {}
    return opts if isinstance(opts, dict) else {}


# --- notes: a memorandum the player writes while playing ------------------
#
# One line each, stamped with the game, scene and room it was written in, kept
# per ROM in notes.json next to options.json. The note key is registered
# system-wide on Windows (RegisterHotKey), so it works with the emulator in
# front: the server remembers which window had the focus, brings the tracker's
# own window forward, and tells the page to open its box (`note_prompt` in the
# state). When the note is posted -- or cancelled -- the focus goes back.
NOTES_FILE = "notes.json"
DEFAULT_NOTE_KEY = "F9"
# what the page titles itself, and therefore what the --app window is called
WINDOW_TITLE = "OoTMM Tracker"
VK_KEYS = {f"F{i}": 0x6F + i for i in range(1, 13)}
VK_KEYS.update({"INSERT": 0x2D, "PAUSE": 0x13, "SCROLLLOCK": 0x91, "NUMLOCK": 0x90})


def hotkey_thread(tracker):
    """Windows only: hold the note key system-wide and hand every press to
    the tracker. Anything that goes wrong leaves the page's own key handling
    in place and says so once."""
    if sys.platform != "win32":
        return
    key = str(tracker.note_key or "").upper().replace(" ", "")
    if key in ("", "NONE", "OFF"):
        return
    vk = VK_KEYS.get(key)
    if vk is None:
        print(f"[overlay] note key {tracker.note_key!r} is not one this build can hold"
              f" ({', '.join(VK_KEYS)}); the box still opens from the page")
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 1, 0x4000, vk):   # MOD_NOREPEAT
            print(f"[overlay] {key} is taken by another program; the note box still opens from the page")
            return
        print(f"[overlay] note key: {key}, anywhere -- press it in the emulator to write a note")
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:   # WM_HOTKEY
                tracker.request_note()
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    except Exception as ex:  # a hotkey must never take the tracker down
        print(f"[overlay] note key off: {type(ex).__name__}: {ex}")


def save_options(opts):
    ruta = paths.user(OPTIONS_FILE)
    try:
        tmp = ruta + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(opts, f)
        os.replace(tmp, ruta)
    except OSError as ex:
        print(f"[overlay] could not save the options: {ex}")


class Tracker:
    """Live state. One thread refreshes it, the HTTP server serves it."""

    def __init__(self, link, table, spoiler=None, locate=None):
        # `link` may be None: the page is served before the emulator side has
        # connected, so that a Browser Source pointed at it shows "waiting for
        # the emulator" instead of a refused connection. attach() fills it in.
        self.link = link
        self.table = table
        self.spoiler = spoiler or {}
        self.locate = locate
        # see _vet_spoiler(): a spoiler is checked against the ROM once
        self._vetted_spoiler = None
        self.spoiler_agreement = None   # [agree, comparable] or None
        self.spoiler_rejected = None    # why the spoiler was dropped, or None
        # vanilla and MQ share a flag; in one seed only one version of each
        # dungeon exists, and which one is recorded by mkchecks
        # Which scenes this seed lays out as Master Quest, worked out from the
        # ROM's own placement when the tables were built
        # (placement.master_quest_scenes). Empty when it could not be worked
        # out, and then every vanilla twin stands, which is what the tracker
        # did before it could tell.
        self.mq_scenes = set((table.get("mq") or {}).get("scenes") or [])
        # what item sits in each spot. Normally it comes from the ROM, inside
        # checks.json; a hand-loaded spoiler is only needed when that ROM's
        # placement table could not be read.
        self.rom_items = {
            (c["game"], c["name"]): c["item"] for c in table["checks"] if c.get("item")
        }
        # Multiworld: whose item sits in each spot. It comes from the `player`
        # field of the ROM's placement table, which mkchecks only writes when it
        # is not yours, so a plain seed leaves this empty. Without it the feed
        # announces a Megaton Hammer that is really going to your partner.
        self.worlds = {
            (c["game"], c["name"]): c["player"] for c in table["checks"] if c.get("player")
        }
        # Which world of a multiworld this ROM plays, worked out when the
        # tables were built (placement.world_of_rom). The labels above are
        # relative to it: what is not yours carries a world, yours carries
        # none. Null means nothing could say, and then the first world was
        # assumed -- which is what the tracker always did, silently.
        self.world = table.get("world")
        # Whether the ROM's placement table was read for this checks.json. When
        # it was, a row without an item is a spot the generator did not make a
        # location in this seed -- see is_active().
        self.placement_known = (
            (table.get("placement") or {}).get("resolved", 0) >= PLACEMENT_KNOWN_MIN)
        # perm-table index for each MM scene, aliases resolved (see MM_SCENE_ALIASES)
        self.scene_alias = {}
        try:
            import mkchecks
            ids = mkchecks.load_scenes()
            for a, b in mkchecks.MM_SCENE_ALIASES.items():
                if a in ids and b in ids:
                    self.scene_alias[("mm", ids[a])] = ids[b]
        except Exception:
            pass
        # Entrance shuffle: the ROM's overrides (mkchecks, entrances.py) and
        # what the run has been seen to go through. See watch_entrance().
        self.entrances = [e for e in (table.get("entrances") or []) if not e.get("link")]
        self._ent_by_dst = collections.defaultdict(list)
        self._ent_keys = set()
        for e in self.entrances:
            self._ent_by_dst[(e["dst_game"], e["dst"])].append(e)
            self._ent_keys.add((e["game"], e["src"]))
        self._entrance = {}          # game -> last save entrance value seen
        self._entrance_pending = {}  # game -> (value seen once, scene before it)
        self._game_mode = None       # gSaveContext.gameMode last read (GAME_MODE_OFF)
        self._game_mode_odd = 0       # consecutive polls with an unrecognised gameMode
        self._game_mode_unread = False  # the field could not be read this poll, bases and all
        # Entrances the run has been seen to go through, and where they persist.
        # This lives only in RAM otherwise, so restarting the tracker mid-run
        # forgets every door already taken -- which is exactly what happened
        # when the overlay was restarted on 17 Aug 2026 and the panel dropped to
        # 0. The file is keyed by a fingerprint of THIS seed's shuffle, so a
        # different seed never reads another's doors, and only src ids still in
        # the table are loaded back.
        self._entrances_found = {}   # (game, src) -> {"t", "sure"}
        self._ent_seen_fp = hashlib.sha1(
            "|".join(sorted(f"{e['game']}:{e['src']}>{e['dst_game']}:{e['dst']}"
                            for e in self.entrances)).encode()).hexdigest()[:12]
        self._ent_seen_path = paths.user("entrances-seen.json")
        self._load_entrances_seen()
        # Hints the streamer gave themselves (see hint() / reveal()): by item,
        # with the level reached, and checks whose item was revealed outright.
        self.hints = collections.OrderedDict()   # item -> {level, game, region, check, t}
        self.revealed = set()                    # "game:name"
        # The player's notes for this ROM (see NOTES_FILE), the pending
        # request to write one (the note key was pressed), the window to hand
        # the focus back to, and the Bombers' code read off MM's save.
        self.note_key = load_options().get("note_key") or DEFAULT_NOTE_KEY
        self.notes = self._load_notes()
        self.note_prompt = None
        self._note_prev_hwnd = None
        self._bombers_code = None
        # The soul bitmaps of the shared custom save and the ROM's catalogue
        # (souls.py), read from checks.json's `souls` block. The Decoder is
        # harmless on a seed that shuffles no souls: it reports not-ok and the
        # panel stays hidden.
        import souls as souls_mod
        self.souls = souls_mod.Decoder.from_table(table)
        # Where this build keeps the Triforce count inside OoT's save, measured
        # by payload.py when the tables were built (it is not the same record in
        # every OoTMM version). None on a checks.json built without a ROM or by
        # a tracker older than this, and then the figure stays out.
        self.triforce = ((table.get("payload") or {}).get("oot") or {}).get("triforce")
        if self.triforce is None and table["checks"]:
            print("[overlay] Triforce count: this build does not hand the piece to a"
                  " handler that could be followed, so the figure will not be shown")
        # The player's own "skulltulas are junk" and "stray fairies are junk"
        # switches, remembered between runs. Read before the first
        # _rebuild_items, or a tracker started with one on would show them until
        # the page came up and said so. One read of the file, not two.
        opts = load_options()
        self.tokens_junk = bool(opts.get("tokens_junk"))
        self.tokens_n = 0
        self.fairies_junk = bool(opts.get("fairies_junk"))
        self.fairies_n = 0
        self._rebuild_items()
        self.lock = threading.Lock()
        self.state = {
            # from the very first request, so the badge is there while waiting
            "version": __version__,
            "ready": False,
            "error": None,
            # No check tables at all: the ROM was not found at startup, so they
            # were never built. Items still work once the emulator connects;
            # the page explains this instead of showing an empty run as normal.
            "no_tables": not table["checks"],
            "waiting": link is None,
            "active": None,
            "trusted": True,
            "confidence": 1.0,
            "done_total": 0,
            "total": sum(n for _, _, n, _k in self.regions),
            "total_key": sum(k for _, _, _n, k in self.regions),
            # The two filler switches and how many checks each one covers: the
            # page only offers a control on a seed that has something to hide,
            # and they have to be here from the first request or the boxes would
            # flicker in half a second after the first poll.
            "tokens_junk": self.tokens_junk,
            "tokens_n": self.tokens_n,
            "fairies_junk": self.fairies_junk,
            "fairies_n": self.fairies_n,
            # Rows with an address that this seed does not shuffle, so they are
            # left out of every count. Said out loud, like every other thing a
            # panel leaves out: on a seed with grass and rocks vanilla it is
            # thousands, and a total that silently shrank would look wrong.
            "not_in_seed": self.not_in_seed,
            "regions": [],
            "items": {"oot": [], "mm": []},
            "feed": [],
            "pending_here": {
                "scene": None, "game": None, "room": None, "live": False,
                "setup": None, "other_setup": 0, "other_room": 0,
                "other_room_listed": 0, "list": [],
            },
            "spoiler_n": len(self.spoiler),
            # how the spoiler squares with the ROM (see _vet_spoiler)
            "spoiler_agreement": self.spoiler_agreement,
            "spoiler_rejected": self.spoiler_rejected,
            # where the items come from: normally the ROM, with a
            # hand-loaded spoiler covering only what the ROM did not give
            "items_n": len(self.items),
            "rom_items_n": len(self.rom_items),
            # How much of this seed the ROM's placement table actually covered.
            # A current seed lands around 90%; an older one lower but usable;
            # anything tiny means checks.json was built from a DIFFERENT seed,
            # which happens when the ROM is changed without restarting the
            # tracker, because discovery only runs at startup. Without this the
            # page reports "991 items from the ROM" as a plain fact and the run
            # looks fine while every item shown belongs to another world.
            "placement_ratio": round(
                len(self.rom_items) / max(1, sum(
                    1 for c in table["checks"] if c["addr"] is not None)), 3),
            "rom_of_table": table.get("rom"),
            # This ROM's own world. The `world N` on an item is somebody
            # else's; without this the reader cannot tell whose.
            "world": self.world,
            # Whether any spot holds an item that is not this world's, which is
            # what makes the number above worth showing. A single-player seed
            # is world 1 and saying so would be noise.
            "multiworld": bool(self.worlds),
            # The ROM the emulator has open right now, and whether it is the
            # one the tables were built from. Read from Project64's own config
            # every ROM_CHECK_SECONDS; null when there is no emulator to ask.
            "rom_open": None,
            "rom_mismatch": False,
            # Set while the tables are being rebuilt for a ROM the user just
            # opened. That takes the best part of a minute, and the page has to
            # say so instead of showing the previous seed's numbers as though
            # nothing had happened.
            "rom_rebuilding": False,
            # Whether anything is watching for that change and will rebuild on
            # its own. False when the tables were pinned with --rom or
            # --no-auto, and then a restart really is the only cure.
            "follows_rom": False,
            # Entrance shuffle: how many entrances this seed moves, which have
            # been gone through (newest first) and, for ?spoiler=full, all of
            # them. Empty `all` means the seed does not shuffle entrances.
            "entrances": {"total": len(self.entrances), "found": [], "all": self.entrances},
            # Hints given so far, and what can still be asked about (the items
            # of the checks not done, filler left out). Both audience-visible:
            # the point is that viewers see the same hint the streamer took.
            "hints": {"used": 0, "items": [], "checks": []},
            "hint_items": [],
            # The seed is from another OoTMM version than data/. Addresses hold
            # —they come from the ROM— but every check NAME comes from the
            # v32.0 CSVs, so a bit can be marked under the wrong name and the
            # wrong region. This is the one the user cannot possibly work out
            # on their own, which is why it gets said out loud.
            "same_version_as_data": table.get("same_version_as_data"),
            # What the "not trustworthy" warning is actually about, in numbers.
            # It used to name only the two save bases —which are located by
            # signature and were never the problem— and then blame a stale MM
            # copy, which is one cause out of several. The address it is
            # accusing is `custom_base`, and `custom_bits` says whether there
            # was anything to measure at all: zero bits is "the block was not
            # found", not "the bits landed elsewhere", and the two need
            # different words. `custom_n` is what stops counting when it fails.
            "custom_base": None,
            "custom_bits": 0,
            "custom_ok": None,
            "custom_n": len((self.plan.get("custom") or {}).get("checks", [])),
            # Where the address came from: "rom" (the ROM's own code names it,
            # see payload.py), "measured" (swept and validated by its bits) or
            # "guess" (the constant, nothing validated). A fresh save with no
            # bits anywhere used to end up as "guess"; with the ROM it is
            # "rom", and that is the difference between a first minute that
            # says "not trustworthy" and one that just works.
            "custom_source": None,
        }
        self._done = set()
        self._seeded = False
        self._drop_polls = 0
        self._bases = None
        self._bases_from_rom = None   # running game's base == the ROM's own (None: payload gave none)
        self._play = {}          # game -> PlayState address, cached like the bases
        self._play_retry = {}    # game -> when the next full scan is allowed
        self._play_misses = {}   # game -> polls in a row it failed to validate
        self._last_scene = {}    # game -> last (scene, room) the PlayState gave
        # inactive game -> measured distance from its buffer to the custom save.
        # Keyed by the INACTIVE game on purpose: the block hangs off whichever
        # one is not running, so a distance measured before crossing does not
        # carry over after it.
        self._custom_gap = {}
        self._custom_retry = 0
        self._custom_active = None
        # Whether the address in use was validated or is just the constant we
        # fall back to. The page says something different for each.
        self._custom_ok = None
        self._custom_addr = None
        self._custom_bits = 0
        self._custom_source = None
        self._custom_blob = None
        self._rom_check_at = 0.0
        self._rom_open = None
        # Set by cmd_overlay when its follower thread starts. It changes no
        # behaviour here, only what the console and the page ADVISE.
        self.follows_rom = False
        # What the ROM's code says about its own globals, per running game
        # (mkchecks writes it, payload.py reads it). Empty on a checks.json
        # built without a ROM or before this existed: then everything below
        # falls back to sweeping, as it always did.
        self.payload = table.get("payload") or {}
        self._payload_warned = set()
        self._xflag_peak = 0
        self._started = time.time()
        self.scene_names = {
            (c["game"], c["scene_id"]): c["scene"]
            for c in table["checks"]
            if c["scene_id"] is not None
        }
        self.xflag_ranges = xflag_ranges(table)
        self.icons = load_icons()
        self.user_icons = scan_user_icons()
        self._user_icons_stamp = _icons_dir_stamp()
        # Every (game, name) in the seed. `check_game` cannot be a name -> game
        # map: the six shop slots below share a name between the two games and
        # one would overwrite the other (see read_flags).
        self.check_keys = {(c["game"], c["name"]) for c in table["checks"]}
        # Which alternate scene headers each scene actually has, taken from the
        # setups its own xflags mention. Needed to resolve the loaded setup the
        # same way the game does -- see setup_loaded().
        self.scene_setups = collections.defaultdict(set)
        # The SCENE's own alternate headers, read off the ROM by setups.py
        # (checks.json "scene_layers"): the list the game walks. Empty on a
        # checks.json built before it existed, and then scene_setups stands in.
        self.scene_layers = {
            (g, int(sid)): set(layers)
            for g, per in (table.get("scene_layers") or {}).items()
            for sid, layers in per.items()
        }
        # Which real rooms each scene has checks in. Used to tell whether the
        # room you are standing in is one of its own -- see the grotto note in
        # refresh().
        self.scene_rooms = collections.defaultdict(set)
        for c in table["checks"]:
            xf = c.get("xflag")
            if xf is not None and c["scene_id"] is not None:
                # a row that is the same actor in several setups counts for each
                for s in xf.get("setups") or (xf["setup"],):
                    self.scene_setups[c["game"], c["scene_id"]].add(s)
                if xf["room"] < 0x20:
                    self.scene_rooms[c["game"], c["scene_id"]].add(xf["room"])
        # per-game totals, so an overlay filtered with ?game= shows its own
        # percentage and not the two together
        self.state["totals"] = collections.Counter()
        for game, _, n, _k in self.regions:
            self.state["totals"][game] += n
        self.state["totals"] = dict(self.state["totals"])
        # Which instance of a shared scene you are in (SHARED_SCENES): the
        # address of gLastScene in the payload and the offset of the grotto
        # byte in the save context, both measured off the ROM by payload.py
        # when the tables were built. Either can be null -- a build the code
        # walk did not resolve -- and then every instance is listed, as before.
        pl = table.get("payload") or {}
        self.last_scene_addr = {g: (pl.get(g) or {}).get("last_scene") for g in ("oot", "mm")}
        self.grotto_off = {g: (pl.get(g) or {}).get("grotto_data") for g in ("oot", "mm")}
        self._last_scene_val = {}       # game -> gLastScene as read this poll
        self._grotto_val = {}           # game -> the grotto byte this poll
        self._last_scene_odd = 0
        self._last_scene_off = set()    # games whose address proved wrong
        self.instance_labels = self._instance_labels(table)
        if table["checks"] and all(v is None for v in self.last_scene_addr.values()):
            print("[overlay] gLastScene was not found in this build's code: grottos and"
                  " fairy fountains will list every instance sharing the scene")

    def _vet_spoiler(self):
        """Refuse a spoiler that contradicts the ROM. Said once per spoiler.

        Comparable spots are the ones both name. A multiworld spoiler writes
        `Player N ` in front of every item and the ROM keeps the owner apart
        (see `worlds`), so that prefix is not a disagreement. The right spoiler
        agrees on every comparable spot; another seed's agrees on the handful
        that happen to hold the same filler.
        """
        if not self.spoiler or id(self.spoiler) == self._vetted_spoiler:
            return
        self._vetted_spoiler = id(self.spoiler)
        agree = comparable = 0
        for key, item in self.spoiler.items():
            mine = self.rom_items.get(key)
            if mine is None:
                continue
            comparable += 1
            if same_item(item, mine):
                agree += 1
        self.spoiler_agreement = [agree, comparable]
        self.spoiler_rejected = None
        if comparable >= SPOILER_MIN_COMPARABLE and agree < SPOILER_MIN_AGREEMENT * comparable:
            self.spoiler_rejected = (f"it agrees with the ROM on only {agree} of {comparable} "
                                     "spots: another seed's")
            print(f"[overlay] WARNING: the spoiler is not this seed's -- it agrees with the ROM "
                  f"on {agree} of {comparable} spots. Ignored; items come from the ROM.")
            self.spoiler = {}
        elif comparable:
            print(f"[overlay] spoiler agrees with the ROM on {agree} of {comparable} spots")

    def _rebuild_items(self):
        """Recompute everything that depends on knowing each spot's item.

        The ROM rules whenever its placement was read. It names every check
        the seed actually shuffles, and it is the authority on which checks
        those are, so with placement known the spoiler adds nothing to the
        item map -- and it used to go on top all the same, on the theory that
        whoever loads one wants it. That was two bugs waiting: on 24 ago 2026
        the spoiler on top was ANOTHER seed's, picked out of the same Downloads
        folder, and every check announced that seed's items with a straight
        face; and a spoiler naming a spot the ROM left unshuffled used to
        revive it as a check (eight vanilla MM cows, 18 ago). Ignoring it when
        the ROM is authoritative kills both. Refuse outright a spoiler that
        contradicts the ROM, so the not-authoritative path below cannot be fed
        the wrong seed either.

        Only when the placement table could NOT be read (an old or foreign
        ROM) is the spoiler the source of truth, and then it is used in full.
        """
        self._vet_spoiler()
        items = dict(self.rom_items)
        if not self.placement_known:
            items.update(self.spoiler)
        self.items = items
        self.junk = {
            (c["game"], c["name"]): is_junk(items.get((c["game"], c["name"])))
            for c in self.table["checks"]
        }
        fichas = self.token_checks(items)
        if self.tokens_junk:
            for k in fichas:
                self.junk[k] = True
        hadas = self.fairy_checks(items)
        if self.fairies_junk:
            for k in hadas:
                self.junk[k] = True
        # How many of them this seed actually counts, which is the number the
        # totals move by: a token row in a Master Quest dungeon is in the table
        # twice, vanilla and MQ, and only one of the two is in the seed. 204
        # token spots, 160 counted, on a seed with MQ dungeons.
        self.tokens_n = self._counted(fichas)
        self.fairies_n = self._counted(hadas)
        self.hay_spoiler = bool(items)
        self.plan, self.regions = build_plan(
            self.table, self.is_active,
            (lambda g, n: self.junk.get((g, n), False)) if items else None)
        # The soul bitmaps sit past the last check of the custom save on some
        # seeds (no pond fish placed, say): read that far.
        if self.souls is not None and self.souls.ok and "custom" in self.plan:
            need = (self.souls.end + BLOCK_PAD + 3) // 4 * 4
            self.plan["custom"]["span"] = max(self.plan["custom"]["span"], need)
        self.not_in_seed = sum(
            1 for c in self.table["checks"]
            if c["addr"] is not None and "anchor" in c
            and self._active_version(c) and not self.is_active(c))
        # Whether this seed actually placed anything on the custom anchor: those
        # checks carry an item only if they are in the ROM's placement table. A
        # seed generated without xsanity simply has no xflags, and then "no
        # xflags set" is not evidence of a bad base, it is the truth. Without
        # this, the guard in poll_once would cry wolf on every such seed.
        self._custom_live = sum(
            1 for c in (self.plan.get("custom") or {}).get("checks", []) if c.get("item")
        ) >= 100

    def token_checks(self, items):
        """Every spot holding a skulltula token, whoever the token is for.

        What the switch hides. It asks nothing about the seed or about worlds,
        which is what the rule it replaced got wrong: the player has decided a
        skulltula is not a check worth showing, and on a multiworld ROM the
        tracker's idea of "yours" is not to be trusted anyway. With no item map
        at all -- no placement read, no spoiler -- what a spot holds is unknown
        and the vanilla token locations are the closest thing to an answer.
        """
        fichas = set(TOKEN_KINDS.values())
        if items:
            return {k for k, it in items.items() if it in fichas}
        return {(c["game"], c["name"]) for c in self.table["checks"]
                if c.get("vanilla") in TOKEN_KINDS}

    def fairy_checks(self, items):
        """Every spot holding a stray fairy, whoever the fairy is for.

        The token rule, with a prefix match instead of a set of names, because
        OoTMM spells the item six ways (see FAIRY_ITEM). Same fallback: with no
        item map at all the vanilla stray fairy spots are the best answer there
        is.
        """
        if items:
            return {k for k, it in items.items() if it and FAIRY_ITEM.match(it)}
        return {(c["game"], c["name"]) for c in self.table["checks"]
                if c.get("vanilla") in FAIRY_KINDS}

    def _counted(self, spots):
        """How many of those spots this seed actually counts.

        The number the totals move by, which is not len(spots): a row in a
        Master Quest dungeon is in the table twice, vanilla and MQ, and only one
        of the two is in the seed.
        """
        return sum(
            1 for c in self.table["checks"]
            if (c["game"], c["name"]) in spots and c["addr"] is not None
            and "anchor" in c and self.is_active(c))

    def item_de(self, game, name):
        return self.items.get((game, name))

    def world_de(self, game, name):
        """Which world the item in that spot belongs to, or None if it is yours."""
        return self.worlds.get((game, name))

    def set_spoiler(self, spoiler):
        """Swap the spoiler without restarting anything.

        Without item data the filler filter cannot work —what decides is the
        item inside— and until this existed the only way to load one was at
        startup. Recomputing is cheap: the filler classification and the
        per-region totals, which is all that depends on it.
        """
        with self.lock:
            self.spoiler = spoiler or {}
            self._rebuild_items()
            self.state["total_key"] = sum(k for _, _, _n, k in self.regions)
            self.state["can_filter"] = self.hay_spoiler
            self.state["spoiler_n"] = len(self.spoiler)
            self.state["spoiler_agreement"] = self.spoiler_agreement
            self.state["spoiler_rejected"] = self.spoiler_rejected
            self.state["items_n"] = len(self.items)
            # The filtered totals, right now: otherwise, between this and the
            # next poll the page shows the new total against the old progress
            # —"18 / 612"— and that number has never existed. The per-region
            # ones catch up on the poll, half a second later.
            hechos = [k for k in self._done if not self.junk.get(k, False)]
            self.state["done_key_total"] = len(hechos)
            self.state["done_key_by_game"] = {
                g: sum(1 for gg, _n in hechos if gg == g) for g in ("oot", "mm")
            }
            return {
                "n": len(self.spoiler),
                "rejected": self.spoiler_rejected,
                "total": self.state["total"],
                "total_key": self.state["total_key"],
            }

    def set_tokens_junk(self, on):
        """Count every skulltula token as filler from now on, or stop."""
        return self._set_junk_switch("tokens", on)

    def set_fairies_junk(self, on):
        """Count every stray fairy as filler from now on, or stop.

        The Fairies tiles are not affected and that is the point: they come
        from strayFairies[] and from `done`, so "12/15" and the orange "ready to
        deliver" carry on working while the 61 rows leave the panel.
        """
        return self._set_junk_switch("fairies", on)

    def _set_junk_switch(self, cual, on):
        """Flip one of the filler switches and settle everything that reads it.

        Server side and not a view switch like the room or the spoiler level,
        because what it changes is the classification itself: the "only what
        matters" totals, the per-region key counts and what the hint picker
        will offer all read `junk`. A page-side filter would empty the list and
        leave those numbers claiming the skulltulas still matter.

        Remembered in the options file, so a restart mid-run does not put 160
        spiders back on the panel. Both switches go through here so neither can
        drift into settling a different set of totals than the other.
        """
        campo, clave = f"{cual}_junk", f"{cual}_n"
        with self.lock:
            setattr(self, campo, bool(on))
            opts = load_options()
            opts[campo] = getattr(self, campo)
            save_options(opts)
            self._rebuild_items()
            # Same as set_spoiler: the totals that depend on the classification,
            # now, so the page never shows a new total against an old progress.
            self.state["total_key"] = sum(k for _, _, _n, k in self.regions)
            self.state[campo] = getattr(self, campo)
            # Both counts, not just this switch's: _rebuild_items has just
            # recomputed the two and a stale one would be a number nobody
            # noticed going wrong.
            self.state["tokens_n"] = self.tokens_n
            self.state["fairies_n"] = self.fairies_n
            hechos = [k for k in self._done if not self.junk.get(k, False)]
            self.state["done_key_total"] = len(hechos)
            self.state["done_key_by_game"] = {
                g: sum(1 for gg, _n in hechos if gg == g) for g in ("oot", "mm")
            }
            return {"ok": True, campo: getattr(self, campo), "n": getattr(self, clave),
                    "total_key": self.state["total_key"]}

    def _active_version(self, c):
        """The vanilla row or its Master Quest twin: only one exists in a seed."""
        if c.get("mq"):
            return c["scene"] in self.mq_scenes
        return c["scene"] not in self.mq_scenes

    def is_active(self, c):
        """Whether this row exists in this seed.

        Two things decide it: the vanilla / Master Quest choice, and -- once the
        ROM's placement table has been read -- whether the ROM placed anything
        there at all. The pool CSVs are a superset: the generator drops a
        location entirely when its category is not shuffled (grass, rocks,
        pots...) or when it is unreachable, and only the survivors get an entry
        in COMBO_VROM_CHECKS. So a row the ROM does not list is not a check in
        this seed. It used to be shown as pending all the same: on a seed with
        grass and rocks left vanilla, 31 of the 52 "still to do" in Kokiri
        Forest were things nobody had to look at.

        Without a placement table (no ROM, or one that could not be read) the
        old behaviour stands and every row is shown.
        """
        if not self._active_version(c):
            return False
        if self.placement_known and (c["game"], c["name"]) not in self.items:
            return False
        return True

    # -- lectura ----------------------------------------------------------

    def locate_cached(self):
        """The bases, relocated only when they stop validating.

        This matters: `locate_saves` scans 8 MB of RDRAM when a base is not
        among the known ones, and this runs twice a second. Without the cache,
        a seed with moved bases would turn every poll into a sweep of the
        entire memory.
        """
        import ootmm

        if self._bases:
            # Plausible contents are not enough: a leftover buffer from before
            # the last crossing has those too. The pair also has to sit the way
            # the two RAM layouts put it, or we would keep a bad base cached
            # for the rest of the session.
            if (ootmm.bases_coherentes(self._bases)
                    and all(ootmm.save_looks_sane(self.link, g, b)
                            for g, b in self._bases.items())
                    and not self.own_save_appeared(self._bases)):
                return self._bases
            # crossing between games moves both: they have to be found again
            self._bases = None

        hints = self.payload_hints()
        bases = self.locate(self.link, verbose=False, hints=hints) if hints \
            else self.locate(self.link, verbose=False)
        self._bases = bases or None
        return bases

    def own_base(self, game):
        """The save context of `game` as this ROM's code names it, in the
        tracker's convention (MM's base is MmSave + 8, signature at +0x1C),
        or None when the payload did not give it."""
        b = (self.payload.get(game) or {}).get("own")
        return None if not b else b + (8 if game == "mm" else 0)

    def own_save_appeared(self, bases):
        """Whether the running game's live save has shown up where the ROM's
        code keeps it while the base in use is some other buffer.

        A base is found once and kept while it validates, and a copy of a
        save validates forever: OoT keeps a debug save -- ZELDAZ, fourteen
        hearts, 150 rupees, every item, eight keys per dungeon -- in a static
        buffer below the live one (0x800FBFB8 under 0x8011A5D0). Start the
        tracker on the title screen, where the live context is not a save
        yet, and that copy is what answers -- and it went on answering for a
        whole session on 28 Aug 2026: the inventory full, 78 phantom checks
        done, the confidence measure at 1.0 because it only watches the
        custom save. So while the base in use is not the ROM's own, the own
        address is looked at on every poll, and the moment it carries a sane
        save the bases are found again (the own one is first in line there).
        """
        import ootmm

        if not bases:
            return False
        running = min(bases, key=lambda g: bases[g])
        own = self.own_base(running)
        if own is None or bases[running] == own:
            return False
        try:
            sig = ootmm.SIG_OOT if running == "oot" else ootmm.SIG_MM
            if self.link.read_block(own + ootmm.SIG_OFFSET, 8)[:6] != sig:
                return False
        except (ConnectionError, OSError):
            return False
        if not ootmm.save_looks_sane(self.link, running, own):
            return False
        print(f"[overlay] {running}'s save has appeared at {own:#x}, where this ROM's code keeps it;"
              f" until now a copy at {bases[running]:#x} was answering. Locating the bases again.")
        return True

    def payload_hints(self):
        """(base, game) pairs the ROM's code names, for locate_saves to try
        first. The foreign buffer of each layout, plus the running game's own
        save context (vanilla, and already first in KNOWN_BASES, but it costs
        nothing to say it)."""
        out = []
        for running, foreign in (("oot", "mm"), ("mm", "oot")):
            b = self.payload.get(running) or {}
            if b.get("foreign_base"):
                out.append((b["foreign_base"], foreign))
            if b.get("own"):
                # the tracker's MM base is MmSave + 8 (signature at +0x1C)
                out.append((b["own"] + (8 if running == "mm" else 0), running))
        return out

    def rom_custom(self, bases, active):
        """gSharedCustomSave for the running game, as the ROM's code has it.

        Returns (address, agrees) or (None, None). `agrees` says whether the
        buffer the signature found for the OTHER game is where the ROM puts it:
        when it is not, either checks.json was built from another ROM or the
        signature picked a stale buffer, and the address is handed over with
        that doubt attached -- the confidence measure gets the last word.
        """
        if not active:
            return None, None
        b = self.payload.get(active) or {}
        addr = b.get("custom")
        if not addr:
            return None, None
        ajeno = "mm" if active == "oot" else "oot"
        agrees = None
        if ajeno in bases and b.get("foreign_base"):
            agrees = bases[ajeno] == b["foreign_base"]
            if not agrees and ("foreign", active) not in self._payload_warned:
                self._payload_warned.add(("foreign", active))
                print(f"[overlay] the ROM's code puts the {ajeno} buffer at "
                      f"0x{b['foreign_base']:08X} while the signature found it at "
                      f"0x{bases[ajeno]:08X}: checks.json may be another ROM's, "
                      "or that is a stale buffer. gSharedCustomSave will be "
                      "trusted only if its bits validate.")
        return addr, agrees

    def setup_loaded(self, game, scene_id, wanted):
        """Which alternate scene header is really loaded, or None if unknown.

        A scene can exist in several versions -- child/adult, day/night -- and
        each one has its OWN actors, so its own checks. `wanted` is what the
        save context asks for, but a scene only has the headers it has: OoTMM
        walks down from there to the highest one that exists and falls back to
        0 (oot/room.c, mm/room.c), and above 3 it is a cutscene and uses 0.
        This mirrors that, using the setups the scene's own xflags mention.

        Returns None when it cannot be resolved, and None means "do not
        filter": guessing wrong here would empty the panel, and a list that
        hides what you can actually reach is worse than one with extras.
        """
        have = self.scene_setups.get((game, scene_id))
        if wanted is None or not have:
            return None
        # What the game walks is the SCENE's alternate header list (oot/room.c
        # updateSceneSetup); checks.json carries it when setups.py could read
        # the ROM, and the setups the checks mention stand in otherwise.
        headers = self.scene_layers.get((game, scene_id), have)
        got = 0
        if 0 <= wanted <= 3:
            for s in range(wanted, 0, -1):
                if s in headers:
                    got = s
                    break
        # A scene whose only xflags live in an alternate header would resolve
        # to 0 and then filter out everything it has. Bail out instead.
        return got if got in have else None

    def play_cached(self, game):
        """(sceneId, roomNum) where the player actually is, or None.

        Cached address first, then the known one, and only then the scan --
        which reads all of RDRAM. Doing that on every poll is what made
        locate_saves a problem, and here it would fire on every title screen
        and every scene transition, so the scan is on a cooldown. In practice
        it runs at most once: the game state is allocated once per boot.
        """
        import ootmm

        seen = set()
        for addr in (self._play.get(game), ootmm.KNOWN_PLAY.get(game)):
            if addr is None or addr in seen:
                continue
            seen.add(addr)
            got = ootmm.read_play(self.link, game, addr)
            if got is not None:
                self._play[game] = addr
                self._play_misses[game] = 0
                return got
        self._play.pop(game, None)

        # One miss is a scene transition, not a moved game state: the caller
        # holds the last reading and the next poll validates again. Only a
        # miss that persists earns the scan (PLAY_MISSES_BEFORE_SCAN).
        self._play_misses[game] = self._play_misses.get(game, 0) + 1
        if self._play_misses[game] < PLAY_MISSES_BEFORE_SCAN:
            return None
        if time.time() < self._play_retry.get(game, 0):
            return None
        self._play_retry[game] = time.time() + PLAY_RESCAN_SECONDS
        addr = ootmm.locate_play(self.link, game, verbose=False)
        got = ootmm.read_play(self.link, game, addr) if addr is not None else None
        if got is not None:
            self._play[game] = addr
        return got

    def find_custom(self, bases, active):
        """Measure where gSharedCustomSave really is, when the known gaps miss.

        Needed because the gap is not a constant of the project, it is a
        constant of the OoTMM version: dev-542a121 moved it from 0x8A8 to
        0x8D8. Held fixed, the anchor lands 0x30 off, confidence drops to 0.08
        and every xflag stops counting without a word.

        One read per game covering the whole window, then the candidates are
        scored locally -- reading each one over the link would be a megabyte a
        poll. The result is cached as a gap, so this runs once per session.
        """
        plan_custom = self.plan.get("custom")
        if plan_custom is None:
            return None
        # Crossing between games is a legitimate reason to look again -- the
        # block now hangs off the other buffer -- so the cooldown starts over
        # instead of making the overlay read the wrong anchor for ten seconds.
        if active != self._custom_active:
            self._custom_active, self._custom_retry = active, 0
        if time.time() < self._custom_retry:
            return None
        self._custom_retry = time.time() + CUSTOM_RESCAN_SECONDS

        span = plan_custom["span"]
        lo, hi = CUSTOM_WINDOW
        mejor, mejor_score, mejor_bits, mejor_game, mejor_gap = None, (0.0, 0), 0, None, None
        for game, base, addrs in custom_window(bases, active):
            desde = base - hi
            try:
                blob = self.link.read_block(desde, hi - lo + span)
            except (ConnectionError, OSError, struct.error):
                continue
            for addr in addrs:
                off = addr - desde
                c, bits = confidence(
                    blob[off : off + span], plan_custom["checks"], self.xflag_ranges
                )
                # bits > 0 as well: an address landing on zeros scores 1.0
                # vacuously, which is how this measure has fooled us before.
                if c >= CONFIDENCE_MIN and bits > 0 and custom_score(c, bits) > mejor_score:
                    mejor, mejor_score = addr, custom_score(c, bits)
                    mejor_bits, mejor_game, mejor_gap = bits, game, base - addr
        if mejor is not None:
            self._custom_gap[mejor_game] = mejor_gap
            if mejor_gap != CUSTOM_BEFORE[mejor_game]:
                print(
                    f"[overlay] custom save at 0x{mejor:08X}: {mejor_bits} bits, "
                    f"gap 0x{mejor_gap:X} from the {mejor_game} buffer "
                    f"(expected 0x{CUSTOM_BEFORE[mejor_game]:X}); "
                    "this OoTMM version moved it"
                )
        return mejor

    def custom_base(self, bases, active):
        """Where gSharedCustomSave is, measured once and then cached.

        Two things here were wrong at first, and both showed up as the overlay
        reporting progress in places the run had never reached:

        **The cache is keyed by the game it hangs off, which is the one NOT
        running.** A distance measured while MM sat idle says nothing once MM
        is the one running: its buffer has moved somewhere else entirely.
        Reusing it across the crossing put the anchor on a plain wrong address.

        **And the known constants do not get to win by being good enough.**
        Accepting the first candidate over the confidence threshold meant that
        on a version which moved the struct, an address 0x30 off could pass —
        its few bits still land on *some* known check — and the search that
        would have found the right one never ran. So: use a distance already
        measured this session, and otherwise measure, always.
        """
        plan_custom = self.plan.get("custom")
        if plan_custom is None:
            return None
        span = plan_custom["span"]

        # The ROM's own code names the address (payload.py). It needs no bits
        # to be believed, which is the whole point: on a save with no progress
        # the sweep below has nothing to score and used to end in a guess. The
        # bits still get the last word when there ARE any -- if they do not
        # land on this table's checks, the table is another ROM's or the
        # buffer is stale, and then it is measured as before.
        rom_addr, agrees = self.rom_custom(bases, active)
        if rom_addr is not None:
            try:
                blob = self.link.read_block(rom_addr, span)
            except (ConnectionError, OSError, struct.error):
                blob = None
            if blob is not None:
                c, bits = confidence(blob, plan_custom["checks"], self.xflag_ranges)
                # With no bits the ROM's word is all there is, and it is enough
                # -- unless the other buffer is not where the ROM puts it, in
                # which case this may be another build's address and there is
                # nothing here to tell; then it is swept for like before.
                if (bits == 0 and agrees is not False) or (bits > 0 and c >= CONFIDENCE_MIN):
                    if self._custom_source != "rom":
                        print(f"[overlay] gSharedCustomSave at 0x{rom_addr:08X}: "
                              f"named by the ROM's code"
                              + (f", {bits} bits validate" if bits else ", no bits yet"))
                    self._custom_ok = True
                    self._custom_source = "rom"
                    return rom_addr
                if ("bits", active) not in self._payload_warned:
                    self._payload_warned.add(("bits", active))
                    print(f"[overlay] the ROM's code puts gSharedCustomSave at "
                          f"0x{rom_addr:08X} but only {c:.0%} of its {bits} bits land on "
                          "known checks; sweeping for it instead")

        ajeno = "mm" if active == "oot" else "oot" if active == "mm" else None
        gap = self._custom_gap.get(ajeno) if ajeno else None
        if gap is not None and ajeno in bases:
            addr = bases[ajeno] - gap
            try:
                blob = self.link.read_block(addr, span)
            except (ConnectionError, OSError, struct.error):
                blob = None
            if blob is not None:
                c, bits = confidence(blob, plan_custom["checks"], self.xflag_ranges)
                if c >= CONFIDENCE_MIN and bits > 0:
                    self._custom_ok = True
                    self._custom_source = "measured"
                    return addr
            self._custom_gap.pop(ajeno, None)

        medida = self.find_custom(bases, active)
        if medida is not None:
            self._custom_ok = True
            self._custom_source = "measured"
            return medida
        # Nothing validated -- a fresh file with no progress anywhere looks like
        # this too. Fall back to the constant so the anchor is not left unset.
        # It is a guess, and the page has to be able to say so: read off a
        # version that moved the struct it lands on nothing at all, and then
        # "0% of the bits that are set land on a known check" is a sentence
        # about zero bits.
        if self._custom_ok is not False:
            print("[overlay] gSharedCustomSave did not validate at any address in "
                  f"the window off the {ajeno or '?'} buffer; falling back to the "
                  "known gap. Every xflag hangs off it, so they stop counting "
                  "until it does validate.")
        self._custom_ok = False
        self._custom_source = "guess"
        # the ROM's address is a better guess than the v32.0 constant, doubt
        # and all: it at least belongs to this build
        if rom_addr is not None:
            return rom_addr
        cands = custom_candidates(bases, active)
        return cands[0] if cands else None

    def hint(self, item, level, world=ANY_WORLD, copy=None):
        """A hint about where `item` is, up to `level` (1 game, 2 region, 3 the
        check itself). Levels only ever go up. Returns the hint entry or None.

        A hint is about one check -- the holder it settled on -- and that is
        what it is filed under, (item, world, check): asking again lands on the
        same one, and two namesakes from different worlds, or two copies of a
        progressive item, are two hints with their own levels.

        `world` narrows it to the copy belonging to that world, None being
        yours; left out (a name typed by hand) it matches any, yours first.
        `copy` is which of the holders still out there, in the table's order,
        the item list numbered ("Hookshot · 2 of 2"): the way to the second
        copy, which one entry per name and "asking again lands on the same one"
        kept out of reach (his report, 28 Aug 2026). Without it, an entry
        already given about a copy still out there is answered again, else the
        first one still out there, else -- all collected -- the first at all.
        """
        level = max(1, min(3, int(level)))
        holders = [c for c in self.table["checks"]
                   if c["addr"] is not None and self.is_active(c)
                   and self.item_de(c["game"], c["name"]) == item
                   and (world is ANY_WORLD
                        or self.world_de(c["game"], c["name"]) == world)]
        if not holders:
            return None
        # Unnarrowed and the name exists in both worlds: yours first. A bare
        # name is a question about your own item; settling on the partner's
        # copy because the table lists it earlier answered about the wrong
        # thing and looked like the first hit (his report, 28 Aug 2026:
        # "Deku Mask" typed, the partner's copy told). Stable, so within a
        # world the order is still the table's.
        if world is ANY_WORLD:
            holders.sort(key=lambda h: self.world_de(h["game"], h["name"]) is not None)
        pending = [h for h in holders if (h["game"], h["name"]) not in self._done]
        target = None
        if copy is not None and 1 <= copy <= len(pending):
            target = pending[copy - 1]
        else:
            given = {(h["game"], h["check"]) for (k_item, k_world, _k_check), h in self.hints.items()
                     if k_item == item and (world is ANY_WORLD or k_world == world)}
            target = next((p for p in pending if (p["game"], p["name"]) in given), None) \
                or (pending[0] if pending else holders[0])
        de_quien = self.world_de(target["game"], target["name"])
        key = (item, de_quien, target["name"])
        cur = self.hints.get(key)
        if cur is None:
            # the region is the scene, made readable: the pool's `hint` column
            # is a hint-group id and NONE on 98% of the rows, so it will not do
            region = (target["scene"] or "").replace("_", " ").title()
            cur = {"item": item, "world": de_quien, "level": 0, "game": target["game"],
                   "region": region, "check": target["name"], "t": time.time()}
            self.hints[key] = cur
        if level > cur["level"]:
            cur["level"] = level
            cur["t"] = time.time()
        return cur

    def reveal(self, game, name):
        """Show what one pending check holds, from now on, at any spoiler
        level: the location-side hint."""
        key = f"{game}:{name}"
        if any(c["game"] == game and c["name"] == name for c in self.table["checks"]):
            self.revealed.add(key)
            return True
        return False

    def hints_state(self, done):
        used = sum(h["level"] for h in self.hints.values()) + len(self.revealed)
        out = []
        for h in self.hints.values():
            e = {"item": h["item"], "world": h.get("world"), "level": h["level"],
                 "t": h["t"], "game": h["game"],
                 "done": (h["game"], h["check"]) in done}
            if h["level"] >= 2:
                e["region"] = h["region"]
            if h["level"] >= 3:
                e["check"] = h["check"]
            out.append(e)
        out.sort(key=lambda e: -e["t"])
        return {"used": used, "items": out, "checks": sorted(self.revealed)}

    def hint_items(self, done):
        """What can be asked about: one entry per item AND world, filler out.

        It used to be a set of names, and on a multiworld that quietly merged
        two different things (his report, 27 ago 2026): 58 of the 162 names his
        seed offers exist in BOTH worlds, so his Forest Medallion and his
        partner's sat under one entry and `hint` picked whichever came first in
        table order without ever saying which one it had told him about.

        Copies inside one world stay one entry -- five Silver Rupees of the same
        kind are five identical entries and listing them five times is noise --
        but the count rides along, so the entry can say that a hint gives away
        one of several rather than the only one.
        """
        n = collections.Counter()
        for c in self.table["checks"]:
            if (c["addr"] is None or (c["game"], c["name"]) in done
                    or not self.is_active(c)):
                continue
            it = self.item_de(c["game"], c["name"])
            if it and not self.junk.get((c["game"], c["name"]), False):
                n[(it, self.world_de(c["game"], c["name"]))] += 1
        # None sorts before any world, which puts yours first among namesakes
        return [{"item": it, "world": w, "n": k}
                for (it, w), k in sorted(n.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0))]

    def _load_entrances_seen(self):
        """Bring back the doors this seed has been seen to go through. Only
        entries whose src is still in the table survive, so a stale file cannot
        invent an entrance."""
        try:
            store = json.loads(open(self._ent_seen_path, encoding="utf-8").read())
        except (OSError, ValueError):
            return
        for k, v in (store.get(self._ent_seen_fp) or {}).items():
            game, _, src = k.partition(":")
            try:
                key = (game, int(src))
            except ValueError:
                continue
            if key in self._ent_keys:
                self._entrances_found[key] = {"t": v.get("t", 0.0), "sure": bool(v.get("sure"))}

    def _save_entrances_seen(self):
        """Write the found doors under this seed's fingerprint, leaving other
        seeds' entries in the file untouched."""
        try:
            store = json.loads(open(self._ent_seen_path, encoding="utf-8").read())
            if not isinstance(store, dict):
                store = {}
        except (OSError, ValueError):
            store = {}
        store[self._ent_seen_fp] = {
            f"{g}:{s}": {"t": v["t"], "sure": v["sure"]}
            for (g, s), v in self._entrances_found.items()
        }
        try:
            tmp = self._ent_seen_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(store, f)
            os.replace(tmp, self._ent_seen_path)
        except OSError as ex:
            print(f"[overlay] could not save seen entrances: {ex}")

    def game_mode(self, active, bases):
        """gSaveContext.gameMode of the running game (see GAME_MODE_OFF), or
        None when it cannot be read: no bases yet, or the link failed. None
        never gates -- a link that answers nothing behaves as before instead
        of freezing the page on a guess."""
        if active is None or active not in bases:
            return None
        try:
            raw = self.link.read_block(bases[active] + GAME_MODE_OFF[active], 4)
            return struct.unpack(">i", raw)[0]
        except (ConnectionError, OSError, struct.error):
            return None

    def watch_entrance(self, game, value, prev_scene):
        """The save's entrance changed: was it one of the shuffled ones?

        `value` is what gSaveContext.entrance now holds -- the DESTINATION id,
        because Play_TransitionDone resolves the override before the scene
        loads. If it is a destination in the table, the player came through
        the matching source. That is certain when the destination's own
        vanilla entrance is itself shuffled (then nothing else lands here) and
        merely probable otherwise; the scene the player was in before the
        transition (`from_map` in entrances.yml) settles it when it can.
        """
        cands = self._ent_by_dst.get((game, value)) or (
            self._ent_by_dst.get((game, value & ~0xF)) if game == "mm" and value < 0x10000 else None)
        if not cands:
            return
        prev_name = self.scene_names.get((game, prev_scene)) if prev_scene is not None else None
        for e in cands:
            key = (e["game"], e["src"])
            if key in self._entrances_found:
                continue
            sure = (e["dst_game"], e["dst"]) in self._ent_keys
            fm = e.get("from_map") or ""
            if not sure and fm and fm != "NONE" and prev_name:
                sure = fm == f"{game.upper()}_{prev_name}"
            elif not sure and (not fm or fm == "NONE"):
                sure = prev_scene is None      # a spawn: nothing before it
            self._entrances_found[key] = {"t": time.time(), "sure": bool(sure)}
            self._save_entrances_seen()   # so a restart does not forget it
            print(f"[overlay] entrance: {e['from_area']} -> {e['to_area']}"
                  + ("" if sure else " (probable)"))

    def with_live_flags(self, blob, game, scene_id):
        """The save-context block with the live scene's chest and collectible
        flags OR-ed onto its perm entry, so those checks count the moment they
        happen instead of when the scene is left. See LIVE_FLAGS.

        Only the scene the PlayState says is loaded, only those two words, and
        only OR: nothing the save already has is ever cleared. If anything is
        off -- no PlayState address, a scene outside the table, a short read --
        the block comes back untouched.
        """
        addr = self._play.get(game)
        lay = (self.table.get("layout") or {}).get(game)
        if addr is None or lay is None or scene_id is None:
            return blob
        scene_id = self.scene_alias.get((game, scene_id), scene_id)
        if not (0 <= scene_id < lay["scene_count"]):
            return blob
        off, size = LIVE_FLAGS[game]
        try:
            raw = self.link.read_block(addr + off, size)
        except (ConnectionError, OSError, struct.error):
            return blob
        if len(raw) < size:
            return blob
        out = None
        for field, at in LIVE_FIELDS[game].items():
            live = struct.unpack_from(">I", raw, at)[0]
            if not live or field not in lay["fields"]:
                continue
            pos = lay["scene_flags"] + scene_id * lay["scene_size"] + lay["fields"][field]
            if pos + 4 > len(blob):
                continue
            saved = struct.unpack_from(">I", blob, pos)[0]
            if live & ~saved:
                if out is None:
                    out = bytearray(blob)
                struct.pack_into(">I", out, pos, saved | live)
        return bytes(out) if out is not None else blob

    def poll_once(self):
        import inventory

        bases = self.locate_cached()
        # the active game is the one whose save sits low in RDRAM
        active = min(bases, key=lambda g: bases[g]) if bases else None

        # The running game's save has to be where this ROM's code keeps it.
        # Whatever else validates is a copy -- OoT's static debug save, a
        # buffer left over from before a crossing -- and then the file is
        # simply not loaded yet: title screen, file select, a crossing half
        # done. Read on and the copy's flags pass for progress (28 Aug 2026:
        # 78 phantom checks and a full inventory, for a whole session). So
        # wait, and say so; locate_cached looks at the own address every poll
        # (own_save_appeared). A payload that named no own base gates nothing.
        own = self.own_base(active) if active else None
        self._bases_from_rom = None if own is None else bases[active] == own
        if self._bases_from_rom is False:
            self._game_mode = GAME_MODE_COPY
            return None

        # Not in a run (title screen, file select, credits): stop here, before
        # anything is read. What the RAM holds then is not this file, and every
        # reader below -- checks, feed, entrances, souls -- would take it as
        # progress. See GAME_MODE_OFF. Only the KNOWN not-in-a-run modes gate:
        # None (could not read) never has, and neither does a value that fits no
        # mode at all. That last one is almost always one poll caught mid-cross
        # OoT<->MM, and freezing a live game on it -- flashing "Not in a game,
        # mode 1549556828" over a run that was reading fine -- was the 24 ago
        # 2026 bug. Read on; the crossing is handled by the drop/undone guards
        # below. Only when it PERSISTS is the offset actually wrong for this
        # build, and then it is said once.
        self._game_mode = self.game_mode(active, bases)
        # None with the bases in hand is a read that failed, not a mode: the
        # guard is not on this poll and the state has to say so (round 2 of
        # the 29 Aug 2026 review), even though reading goes on as before
        self._game_mode_unread = self._game_mode is None and active in bases
        if self._game_mode in GAME_MODE_NOT_PLAYING:
            self._game_mode_odd = 0
            return None
        if self._game_mode in (None, 0):
            self._game_mode_odd = 0
        else:
            self._game_mode_odd += 1
            if self._game_mode_odd == GAME_MODE_ODD_PERSIST:
                print(f"[overlay] gameMode has read unknown values ({self._game_mode}) for "
                      f"{GAME_MODE_ODD_PERSIST} polls -- its offset looks wrong for this OoTMM "
                      "build, so the title-screen guard is off. Progress still reads from RAM.")

        mejor = self.custom_base(bases, active)
        anchors = rebase(self.table, bases, active, mejor)
        # kept for the state: the warning has to be able to name the address it
        # is accusing, and it is not one of the two the page used to list
        self._custom_addr = anchors.get("custom")

        # Where the player is, first: the live scene flags below hang off it.
        # The PlayState is the live answer; the save context is the fallback,
        # and it lags (see SCENE_OFF).
        prev_scene = self._last_scene.get(active, (None, None))[0] if active else None
        scene, room, live = None, None, False
        got = self.play_cached(active) if active is not None else None
        if got is not None:
            scene, room = got
            live = True
            self._last_scene[active] = got

        done = set()
        conf, bits = 1.0, 0
        por_ancla = {}
        custom_blob = None
        for anchor, p in self.plan.items():
            if anchor not in anchors:
                continue
            blob = self.link.read_block(anchors[anchor], p["span"])
            if anchor == "custom":
                custom_blob = blob
                conf, bits = confidence(blob, p["checks"], self.xflag_ranges)
            elif anchor == active and got is not None:
                blob = self.with_live_flags(blob, active, scene)
            por_ancla[anchor] = read_flags(blob, p["checks"])
        # kept for refresh(): the souls are decoded off this same block
        self._custom_blob = custom_blob

        # A bad base landing in a zeroed area gives confidence 1.0 without a
        # single bit: that is not a warning, it is silence.
        #
        # Two independent signals catch it, and they cover different moments:
        #
        #   1. Bits were seen earlier this session and now there are none.
        #   2. There are scene checks done and not one xflag. Scene checks hang
        #      off another anchor, located by signature, so they can be trusted
        #      from the very first poll -- which is exactly when signal 1 cannot
        #      fire, because nothing has armed _xflag_peak yet. That was the
        #      hole: start the overlay with the base already wrong and it would
        #      report a clean, confident, empty run.
        #
        # Both have to rule out a genuinely fresh file, where nothing is set
        # anywhere -- hence requiring scene checks to be there.
        escena = len(por_ancla.get("oot", ())) + len(por_ancla.get("mm", ()))
        antes = self._xflag_peak > 8
        arranque = escena >= SCENE_CHECKS_SUSPICIOUS and self._custom_live
        # Neither signal applies when the ROM's own code named the address:
        # then "no xflag set yet" is a fact about the run, not about the base.
        # This is the fresh-file case -- three chests opened, no pot broken --
        # and it used to read as "not trustworthy" for exactly as long as the
        # player had not yet found a bit to validate with.
        if bits == 0 and escena > 0 and (antes or arranque) and self._custom_source != "rom":
            conf = 0.0
        elif escena == 0 and bits == 0:
            self._xflag_peak = 0      # partida nueva: se olvida lo visto
        self._xflag_peak = max(self._xflag_peak, bits)
        self._custom_bits = bits

        for anchor, hechos in por_ancla.items():
            if anchor == "custom" and conf < CONFIDENCE_MIN:
                continue
            done |= hechos

        items = {}
        oot_blk = self.link.read_block(bases["oot"], 0x1500) if "oot" in bases else None
        if oot_blk is not None:
            items["oot"] = inventory.snapshot(oot_blk, self._triforce(oot_blk))

        # The two signals that tell a shared scene's instances apart (see
        # SHARED_SCENES): a word of the payload and a byte of the save context.
        self._read_instance_signals(active, bases, oot_blk)

        # The running game's save entrance: OoT keeps it at +0x00, MM at
        # MmSave+0x00, eight bytes before the base this project uses.
        if active in bases and self.entrances:
            try:
                if active == "oot":
                    ent = struct.unpack_from(">i", oot_blk, 0)[0]
                else:
                    ent = struct.unpack(">i", self.link.read_block(bases["mm"] - 8, 4))[0]
            except (ConnectionError, OSError, struct.error):
                ent = None
            # A new value counts once it has been read on two polls in a row.
            # Mid-crossing the block can read zeros for a poll, and 0 is a real
            # id (ENTR_DEKU_TREE_0), a shuffled destination in an ER seed: the
            # very ghost the title screen produced (see GAME_MODE_OFF). The
            # scene the player was in goes with the first sighting, because by
            # the second the PlayState already shows the new one.
            if ent is not None and ent != self._entrance.get(active):
                pend = self._entrance_pending.get(active)
                if pend is not None and pend[0] == ent:
                    self._entrance[active] = ent
                    self._entrance_pending.pop(active, None)
                    self.watch_entrance(active, ent & 0xFFFFFFFF, pend[1])
                else:
                    self._entrance_pending[active] = (ent, prev_scene)
            elif ent is not None:
                self._entrance_pending.pop(active, None)
        if "mm" in bases:
            # MM's clocks live in the shared custom save (mm.halfDays); the
            # block was read above for the checks, and the offset is the one
            # mkchecks took from the ROM
            half_days = None
            cs = (self.table.get("custom_save") or {}).get("mm") or {}
            if custom_blob is not None and cs.get("halfDays") is not None \
                    and cs["halfDays"] < len(custom_blob) and self._custom_ok:
                half_days = custom_blob[cs["halfDays"]]
            mm_blk = self.link.read_block(bases["mm"], 0x1500)
            items["mm"] = inventory.mm_snapshot(mm_blk, oot_blk, half_days)
            # the Bombers' code lives in the same block, whichever game runs
            # (the other game's copy carries it too)
            self._bombers_code = inventory.bombers_code(mm_blk)

        # A scene transition rewrites the PlayState, so for a poll or two it
        # stops validating. Falling straight through to the save context there
        # made the panel FLICKER: the saved scene is a different one and often
        # not even in the table, so the heading blinked between the real area
        # and "unknown area" several times while crossing Termina Field.
        # Holding the last good reading is the latch the POC kept asking for.
        if scene is None and active in self._last_scene:
            scene, room = self._last_scene[active]
        elif scene is None and active in bases:
            # read_block travels in 4-byte words, so the address has to be
            # aligned: read the word containing the field and pull the halfword
            # out of it. Over the real link, asking for 0x66 directly returns
            # garbage.
            off = SCENE_OFF[active]
            raw = self.link.read_block(bases[active] + (off & ~3), 4)
            scene = struct.unpack_from(">h", raw, off & 3)[0]

        # Which version of the scene is loaded. This one IS in the save
        # context and there is no live copy to prefer: OoTMM resolves it into
        # its own `g.sceneSetupId`, which lives in the payload with no known
        # address, so we redo the resolution ourselves in setup_loaded().
        setup = None
        if active in bases:
            raw = self.link.read_block(bases[active] + SETUP_OFF[active], 4)
            setup = struct.unpack(">i", raw)[0]

        return active, done, conf, items, scene, room, live, setup

    def refresh(self):
        # so you can drop an image in icons/ and see it without restarting
        stamp = _icons_dir_stamp()
        if stamp != self._user_icons_stamp:
            self.user_icons = scan_user_icons()
            self._user_icons_stamp = stamp

        polled = self.poll_once()
        if polled is None:
            # Title screen / file select / credits: the last picture stands
            # (nothing is fed, no baseline is taken) and the page says why.
            with self.lock:
                s = self.state
                s["ready"] = True
                s["error"] = None
                s["in_game"] = False
                s["game_mode"] = GAME_MODES.get(self._game_mode, f"mode {self._game_mode}")
                s["bases"] = {g: f"0x{b:08X}" for g, b in (self._bases or {}).items()}
                s["bases_from_rom"] = self._bases_from_rom
                s["uptime"] = int(time.time() - self._started)
            return
        active, done, conf, items, scene, room, live, setup_raw = polled
        self._check_last_scene(active, scene, live)

        # Nothing a run has done ever comes undone, so a poll where any check
        # that WAS done is not any more is either a transient or another file
        # being loaded -- and so is a jump of many at once. Neither is believed
        # until it holds: see DONE_DROP_POLLS and DONE_JUMP_MAX. Until then last
        # poll's picture stands and nothing is fed. (Counting was not enough: the
        # simulated crossing went 18 -> 38, a different set entirely, and a
        # threshold on the size let it through and then re-announced the 18.)
        undone = (self._done - done) if self._seeded else set()
        big_jump = self._seeded and len(done) - len(self._done) > DONE_JUMP_MAX
        if undone or big_jump:
            self._drop_polls += 1
            if self._drop_polls < DONE_DROP_POLLS:
                done = self._done
        else:
            self._drop_polls = 0

        # the first poll sets the baseline silently: otherwise the feed starts
        # by spitting out everything you had already done
        feed = []
        if self._seeded:
            # the game comes with the key now, so the item is looked up in
            # the right one instead of asking OoT first and MM if that missed
            for game, name in sorted(done - self._done):
                feed.append(
                    {
                        "check": name,
                        "game": game,
                        "item": self.item_de(game, name),
                        "world": self.world_de(game, name),
                        "t": time.time(),
                    }
                )
        self._done = done
        self._seeded = True

        by_scene = collections.Counter()
        by_scene_key = collections.Counter()
        for c in self.table["checks"]:
            if (c["game"], c["name"]) in done and self.is_active(c):
                by_scene[(c["game"], c["scene"])] += 1
                if not self.junk.get((c["game"], c["name"]), False):
                    by_scene_key[(c["game"], c["scene"])] += 1

        # Every region goes to the page, the untouched ones included: whether
        # to list those is the viewer's choice (?untouched=show), and the page
        # says how many it is leaving out when it does not. Touched first, by
        # progress; the rest by name, so a "what is left" list reads like a map.
        regions = []
        for rgame, rscene, total, total_key in self.regions:
            got = by_scene.get((rgame, rscene), 0)
            regions.append({
                "game": rgame, "scene": rscene, "done": got, "total": total,
                "done_key": by_scene_key.get((rgame, rscene), 0),
                "total_key": total_key,
            })
        regions.sort(key=lambda r: (r["done"] == 0, -r["done"] / r["total"], -r["total"],
                                    r["game"], r["scene"]))
        # How many regions exist at all. The page works out what it is not
        # showing from the list itself now that the whole list travels, but
        # this stays: it is cheap and older panels read it.
        regions_n = {
            "all": len(self.regions),
            "key": sum(1 for _, _, _n, k in self.regions if k),
        }

        setup = self.setup_loaded(active, scene, setup_raw)
        here = {
            "scene": self.scene_names.get((active, scene)),
            "game": active,
            "room": room,
            "live": live,
            "setup": setup,
            "other_setup": 0,
            "other_room": 0,
            "list": [],
        }
        if active is not None and scene is not None:
            pend = [
                c
                for c in self.table["checks"]
                if c["game"] == active
                and c["scene_id"] == scene
                and c["addr"] is not None
                and self.is_active(c)
                and (c["game"], c["name"]) not in done
            ]
            # A scene in another setup is a different set of actors, so its
            # checks cannot be reached while this one is loaded. Leaving them in
            # is what made Hyrule Field look broken: you cut the bush, "Bush 09"
            # got marked, and its twin from the other header —"Grass Pack 3
            # Bush 09", a different actor entirely— stayed pending for good.
            # They are NOT gone from the totals: come back as the other age and
            # they are reachable, so this filters the "what is left HERE" panel
            # only, and says how many it put aside.
            # GROTTOS is ONE scene holding every grotto in the game, so the
            # room is the only thing that separates them -- except that the
            # generic grottos all share a room, and comboXflagInit gives those
            # actors `0x20 | grottoData` instead of a room number.
            #
            # Which room is the generic one is not hardcoded, it falls out of
            # the data: it is the one with no checks of its own, precisely
            # because its actors were renumbered away. In MM's GROTTOS the
            # rooms with checks are 0,2,5,6,9..15 -- no 4 -- and 4 is the one
            # comboXflagInit rewrites. So if you are standing in a room the
            # scene owns, none of the `0x20 |` ones can be yours.
            sala_propia = room is not None and room in self.scene_rooms.get((active, scene), ())
            # some scenes are one place however many rooms they have
            whole = (active, self.scene_names.get((active, scene))) in WHOLE_SCENE
            # ... and some are many places in one: which grotto or fountain
            # this is, when the game's own two signals say (SHARED_SCENES).
            # Its checks are listed under `vroom`, so that is the room they are
            # compared against; unresolved, they stay candidates as before.
            shared = (active, here["scene"]) in SHARED_SCENES
            vroom, how = self.instance_room(active, here["scene"], room)
            cmp_room = vroom if vroom is not None else room

            lista, otras, otros, fuera = [], [], 0, 0
            for c in pend:
                xf = c.get("xflag") or {}
                # A twin -- the same actor in several headers -- is one row
                # filed under one setup and collectable in all of `setups`
                # (setups.py); a row without it lives in its own setup only.
                if setup is not None and setup not in (xf.get("setups") or (xf.get("setup", setup),)):
                    otros += 1
                    continue
                # A chest, a scrub, an npc, a cow or a skulltula carries no
                # xflag, so in a scene that is many places at once it had no
                # room and every grotto's chests were listed in every grotto.
                # mkchecks.assign_virtual_rooms gives it the room its own name
                # names, when exactly one room answers to that name; `vroom` is
                # a room like any other from here on, and rows it could not
                # place still have none.
                croom = xf.get("room")
                if croom is None:
                    croom = c.get("vroom")
                if croom is not None and croom >= 0x20:
                    if sala_propia:
                        fuera += 1
                        continue
                    if vroom is None:
                        croom = None   # in the generic room they are all candidates
                if whole:
                    croom = None   # no room of its own: listed, untagged, unfiltered
                entry = {
                    "name": c["name"],
                    "item": self.item_de(c["game"], c["name"]),
                    "world": self.world_de(c["game"], c["name"]),
                    "type": c["type"],
                    "junk": self.junk.get((c["game"], c["name"]), False),
                    "room": croom,
                    "here": croom is not None and croom == cmp_room,
                    # In a shared scene a room is a place with a name -- and a
                    # row with NO room is not in the one you are standing in,
                    # it is in a grotto nothing in its data names. Under a
                    # heading that names your grotto it read as belonging
                    # there: his report, 30 Aug 2026, "Deku Theater Nuts
                    # Upgrade" listed under "Remaining in Lake Hylia Grotto",
                    # and again from a second grotto -- "es fallo general de
                    # todos los agujeros", which it was. So it is tagged AND
                    # taken out of the default list (`unplaced`, below): the
                    # panel answers "what is left HERE", and "it could be any
                    # of them" is not an answer to that. Only once the
                    # instance is known -- when it is not, the title already
                    # says everything is listed.
                    "place": (self.instance_labels.get((active, scene, croom))
                              if shared and croom is not None
                              else UNPLACED_IN_SHARED if sitio_sabido
                              else None),
                    # Carried, counted and said out loud, never dropped: they
                    # are real checks with real items, just not here.
                    "unplaced": sitio_sabido and croom is None,
                    # the item shows regardless of the spoiler level once the
                    # streamer asked for it, or a level-3 hint named this check
                    "revealed": (f"{c['game']}:{c['name']}" in self.revealed
                                 or any(h["level"] >= 3 and h["check"] == c["name"] and h["game"] == c["game"]
                                        for h in self.hints.values())),
                }
                # The room FILTERS the default panel: GROTTOS is one scene with
                # every grotto in it, so standing in one you would get 440
                # pending from all of them. Checks with no room of their own
                # are never filtered -- a shop's, and the grotto rows no name
                # placed. But the ones that
                # belong to another room are now carried in `otras` too, so the
                # "whole scene" toggle shows them with no second request -- and a
                # room whose number does not line up with where you stand
                # (Gerudo Fortress's archery crates read room 1) is one click
                # away instead of counted-but-invisible. Capped, since GROTTOS'
                # other rooms run to the hundreds.
                if room is not None and room >= 0 and croom is not None and croom != cmp_room:
                    fuera += 1
                    if len(otras) < OTHER_ROOM_CAP:
                        otras.append(entry)
                    continue
                lista.append(entry)
            # What is in this very room first, then the ones with no room of
            # their own; the other rooms last, only shown when unified.
            lista.sort(key=lambda e: not e["here"])
            here["list"] = lista + otras
            here["other_setup"] = otros
            here["other_room"] = fuera
            # How many of the other-room checks actually travelled (the rest are
            # over the cap): the page needs it to say what it is still hiding.
            here["other_room_listed"] = len(otras)
            # ...and how many are in a grotto nothing names, so the page can
            # say that too instead of listing them as if they were here.
            here["unplaced"] = sum(1 for e in lista if e.get("unplaced"))
            here["whole"] = whole
            if shared and how is not None:
                # standing where the instances are told apart: what the page
                # says in the title is the place, or that it could not be told
                # and everything is listed. A real room of GROTTOS is a place
                # of its own and keeps its room number.
                here["instance"] = {
                    "room": vroom,
                    "label": self.instance_labels.get((active, scene, vroom)) if vroom is not None else None,
                    "how": how,
                }

        with self.lock:
            s = self.state
            s["ready"] = True
            s["error"] = None
            s["in_game"] = True
            s["game_mode"] = GAME_MODES.get(self._game_mode, "playing")
            s["instance_signals"] = {
                "last_scene": self._last_scene_val.get(active),
                "grotto": self._grotto_val.get(active),
            }
            s["active"] = active
            s["confidence"] = round(conf, 3)
            s["trusted"] = conf >= CONFIDENCE_MIN
            s["bases"] = {g: f"0x{b:08X}" for g, b in (self._bases or {}).items()}
            # whether the running game's save is read where this ROM's code
            # keeps it (None: the payload named no own base, nothing gated)
            s["bases_from_rom"] = self._bases_from_rom
            # whether this ROM's code proved the custom-save layout the
            # tables were built with; False means the rows that hang off it
            # were left without an address (mkchecks.LAYOUT_FROM_ROM)
            s["layout_from_rom"] = self.table.get("layout_from_rom")
            # which signal settled the world (config / spoiler / a guess)
            s["world_by"] = self.table.get("world_by")
            # the title-screen guard: "off" once gameMode has read values that
            # fit no mode for GAME_MODE_ODD_PERSIST polls (the offset is not
            # this build's), "unavailable" when the field could not be read at
            # all this poll; progress reads on regardless (see poll_once)
            s["game_mode_guard"] = ("unavailable" if self._game_mode_unread
                                    else "off" if self._game_mode_odd >= GAME_MODE_ODD_PERSIST
                                    else "on")
            s["custom_base"] = (
                f"0x{self._custom_addr:08X}" if self._custom_addr else None)
            s["custom_bits"] = self._custom_bits
            s["custom_ok"] = self._custom_ok
            s["custom_source"] = self._custom_source
            # The souls are read off the same custom-save block as the checks,
            # and only once that block validated: a guessed base would light
            # souls up out of whatever bytes it landed on. On a seed with no
            # souls the state reports not-ok and the panel stays hidden.
            s["souls"] = self.souls.state(self._custom_blob, bool(self._custom_ok))
            s["not_in_seed"] = self.not_in_seed
            s["hints"] = self.hints_state(done)
            s["hint_items"] = self.hint_items(done)
            found = sorted(self._entrances_found.items(), key=lambda kv: -kv[1]["t"])
            by_key = {(e["game"], e["src"]): e for e in self.entrances}
            s["entrances"] = {
                "total": len(self.entrances),
                "found": [dict(by_key[k], **v) for k, v in found if k in by_key][:FEED_MAX],
                "all": self.entrances,
            }
            s["done_total"] = len(done)
            s["done_key_total"] = sum(1 for k in done if not self.junk.get(k, False))
            s["can_filter"] = self.hay_spoiler
            s["tokens_junk"] = self.tokens_junk
            s["tokens_n"] = self.tokens_n
            s["fairies_junk"] = self.fairies_junk
            s["fairies_n"] = self.fairies_n
            s["done_by_game"] = {
                g: sum(v for (gg, _), v in by_scene.items() if gg == g) for g in ("oot", "mm")
            }
            s["done_key_by_game"] = {
                g: sum(v for (gg, _), v in by_scene_key.items() if gg == g) for g in ("oot", "mm")
            }
            s["totals_key"] = {
                g: sum(k for gg, _, _n, k in self.regions if gg == g) for g in ("oot", "mm")
            }
            s["regions"] = regions
            s["regions_n"] = regions_n
            # FEATURE: fairy readiness. The Fairies group needs to know which
            # Great Fairy rewards are already collected, which lives in `done`,
            # not in the item snapshot. Computed once and passed to item_grid.
            fairy_info = {
                "rewards_done": {name: any(("mm", rc) in done for rc in checks)
                                 for name, checks in GF_REWARD.items()},
                "clock_in_seed": ("mm", CLOCK_STRAY_CHECK) in self.check_keys,
                "clock_have": ("mm", CLOCK_STRAY_CHECK) in done,
                "clock_reward_done": any(("mm", rc) in done for rc in CLOCK_REWARD_CHECKS),
            }
            s["items"] = {g: item_grid(g, v, self.icons, self.user_icons,
                                       fairy_info if g == "mm" else None)
                          for g, v in items.items()}
            s["scalars"] = {g: item_scalars(g, v) for g, v in items.items()}
            # see FEED_RETRACT_SECONDS: what appeared moments ago and is not
            # done any more was a transient poll, not progress
            now = time.time()
            kept = [f for f in s["feed"]
                    if (f["game"], f["check"]) in done
                    or now - f["t"] > FEED_RETRACT_SECONDS]
            s["feed"] = (feed + kept)[:FEED_MAX]
            s["pending_here"] = here
            s["uptime"] = int(time.time() - self._started)
            # only the BizHawk link keeps them: what a request costs the
            # emulator, so the per-frame budget is set from numbers, not guesses
            if hasattr(self.link, "stats"):
                s["link"] = self.link.stats()

    def attach(self, link):
        """The emulator side connected: start polling through it."""
        with self.lock:
            self.link = link
            self.state["waiting"] = False
            self.state["error"] = None

    def fail(self, msg):
        """The emulator side will not be coming. Say so on the page too.

        The handshake reports its problems with sys.exit, and from a thread
        that kills nothing: without this the page would sit on "waiting for
        the emulator" for ever while the console explained why it never would.
        """
        print(f"[overlay] {msg}")
        with self.lock:
            self.state["waiting"] = False
            self.state["error"] = msg

    def check_rom_open(self):
        """Which ROM the emulator has open, against the one the tables are from.

        Project64 writes the ROM it loads to `Recent Rom 0` the moment it opens
        it, so this catches "changed seed, did not restart the tracker" without
        guessing from the data. Cheap (one small file every ROM_CHECK_SECONDS)
        and honest: with no emulator config to read it says nothing.
        """
        if time.time() < self._rom_check_at:
            return
        self._rom_check_at = time.time() + ROM_CHECK_SECONDS
        try:
            import discover
            emu = discover.find_emulator()
            recent = discover.recent_roms(emu) if emu else []
        except Exception:
            recent = []
        abierta = recent[0] if recent else None
        de_tabla = self.table.get("rom")
        mismatch = bool(abierta and de_tabla and not discover._same(abierta, de_tabla))
        with self.lock:
            self.state["rom_open"] = abierta
            self.state["rom_mismatch"] = mismatch
        if mismatch and self._rom_open != abierta:
            que_toca = ("Rebuilding them for it." if self.follows_rom
                        else "Restart the tracker to rebuild them.")
            print(f"[overlay] the emulator has another ROM open: {abierta}; the tables are "
                  f"{de_tabla}'s. {que_toca}")
        self._rom_open = abierta

    def rom_open_elsewhere(self):
        """The ROM the emulator has open when it is not the tables', else None.

        check_rom_open() works this out every ROM_CHECK_SECONDS; this is how
        another thread asks for the answer without reaching into `state`.
        """
        with self.lock:
            return self.state["rom_open"] if self.state["rom_mismatch"] else None

    def rebuilding(self, yes):
        """Say on the page that the tables are being built for the new ROM."""
        with self.lock:
            self.state["rom_rebuilding"] = bool(yes)

    def set_follows_rom(self, yes):
        """Record that a follower thread is watching for a change of ROM."""
        self.follows_rom = bool(yes)
        with self.lock:
            self.state["follows_rom"] = self.follows_rom

    def run(self, interval=POLL_SECONDS):
        while True:
            self.check_rom_open()
            if self.link is None:
                time.sleep(0.5)      # nothing to poll until the Lua connects
                continue
            try:
                self.refresh()
            except Exception as ex:  # the Lua link drops when the ROM is reloaded
                with self.lock:
                    self.state["error"] = f"{type(ex).__name__}: {ex}"
                    self.state["ready"] = False
                time.sleep(2.0)
                continue
            time.sleep(interval)

    @staticmethod
    def _instance_labels(table):
        """{(game, scene_id, room): what the checks under that room call the place}.

        A shared scene's instances exist only as rows -- "Zora River Fairy
        Fountain Fairy 3", "Path to Snowhead Grotto Grass 07" -- so the words
        every row of a room shares, minus the trailing type and number, name
        the place. That is what the panel shows instead of a room number
        nobody would recognise.
        """
        by_room = collections.defaultdict(list)
        for c in table["checks"]:
            xf = c.get("xflag")
            if not xf or c["scene_id"] is None or xf.get("room") is None:
                continue
            by_room[c["game"], c["scene_id"], xf["room"]].append((c["name"], (c.get("type") or "").lower()))
        out = {}
        for key, rows in by_room.items():
            prefix = rows[0][0].split()
            for name, _ in rows[1:]:
                words = name.split()
                k = 0
                while k < len(prefix) and k < len(words) and prefix[k] == words[k]:
                    k += 1
                prefix = prefix[:k]
            types = {t for _, t in rows}
            while prefix and (prefix[-1].isdigit() or prefix[-1].lower() in types
                              or prefix[-1].lower().rstrip("s") in types):
                prefix.pop()
            if prefix:
                out[key] = " ".join(prefix)
        return out

    def _read_instance_signals(self, active, bases, oot_blk):
        """gLastScene and the grotto byte of the running game, for this poll."""
        self._last_scene_val.pop(active, None)
        self._grotto_val.pop(active, None)
        if active not in bases:
            return
        addr = self.last_scene_addr.get(active)
        if addr is not None and active not in self._last_scene_off:
            try:
                self._last_scene_val[active] = struct.unpack(">i", self.link.read_block(addr, 4))[0]
            except (ConnectionError, OSError, struct.error):
                pass
        off = self.grotto_off.get(active)
        if off is not None:
            try:
                if active == "oot" and oot_blk is not None and off < len(oot_blk):
                    self._grotto_val[active] = oot_blk[off]
                else:
                    # the offset is from gSaveContext; MM's base here is 8 past it
                    own = bases[active] - (MM_BASE_DELTA if active == "mm" else 0)
                    raw = self.link.read_block(own + (off & ~3), 4)
                    self._grotto_val[active] = raw[off & 3]
            except (ConnectionError, OSError, struct.error, IndexError):
                pass

    def instance_room(self, active, scene_name, room):
        """Which instance of a shared scene the player is in.

        (room its checks are listed under, how it was told) -- or (None, why)
        when it cannot be told, and (None, None) for a scene that is not
        shared, where the caller carries on as for any other.
        """
        rules = SHARED_SCENES.get((active, scene_name))
        if not rules or room not in rules:
            return None, None
        how, table = rules[room]
        if how == "grotto":
            b = self._grotto_val.get(active)
            if b is None:
                return None, "the grotto byte could not be read"
            return 0x20 | (b & GROTTO_ID_MASK), f"grotto byte {b:#04x}"
        v = self._last_scene_val.get(active)
        if v is None:
            if self.last_scene_addr.get(active) is None or active in self._last_scene_off:
                return None, "this build's gLastScene is not known"
            return None, "gLastScene could not be read"
        came_from = self.scene_names.get((active, v))
        r = table.get(came_from)
        if r is None:
            return None, f"came from {came_from or v}, not one of the {len(table)} this scene knows"
        return r, f"came from {came_from}"

    def _check_last_scene(self, active, scene, live):
        """Outside a shared scene gLastScene IS the live scene; when it keeps
        not being, the address is another build's, and the instance is then
        left unnamed rather than misnamed. Said once."""
        if active in self._last_scene_off or not live or scene is None:
            return
        v = self._last_scene_val.get(active)
        if v is None or (active, self.scene_names.get((active, scene))) in SHARED_SCENES:
            return
        if v == scene:
            self._last_scene_odd = 0
            return
        self._last_scene_odd += 1
        if self._last_scene_odd >= LAST_SCENE_ODD_PERSIST:
            self._last_scene_off.add(active)
            self._last_scene_odd = 0
            print(f"[overlay] gLastScene at {self.last_scene_addr[active]:#x} has read {v} while"
                  f" the live scene was {scene} for {LAST_SCENE_ODD_PERSIST} polls: not this"
                  f" build's, so {active}'s grottos and fountains will list every instance")

    def _triforce(self, oot_blk):
        """The Triforce count, out of whichever buffer this build keeps it in.

        Up to v32.3 it is a u32 in OoT's save; gen 943 moved it to a u16 in
        gSharedCustomSave. checks.json says which and how wide
        (payload.triforce_counter), so nothing here has to know.
        """
        d = self.triforce
        if not d:
            return None
        blob = oot_blk if d.get("buffer") == "oot" else self._custom_blob
        off, ancho = d.get("off"), d.get("width", 4)
        if blob is None or off is None or off + ancho > len(blob):
            return None
        return int.from_bytes(blob[off:off + ancho], "big")

    # -- notes ---------------------------------------------------------------

    def _notes_key(self):
        return os.path.basename(self.table.get("rom") or "") or "no-rom"

    def _read_notes_file(self):
        try:
            d = json.loads(open(paths.user(NOTES_FILE), encoding="utf-8").read())
        except (OSError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

    def _load_notes(self):
        lst = self._read_notes_file().get(self._notes_key()) or []
        return [n for n in lst if isinstance(n, dict) and n.get("text")]

    def _save_notes(self):
        d = self._read_notes_file()
        d[self._notes_key()] = self.notes
        ruta = paths.user(NOTES_FILE)
        try:
            tmp = ruta + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            os.replace(tmp, ruta)
        except OSError as ex:
            print(f"[overlay] notes not saved: {ex}")

    def add_note(self, text, game=None, scene=None, room=None):
        """One line, stamped with where it was written. Empty text is no note."""
        text = " ".join(str(text or "").split())[:400]
        if not text:
            return None
        n = {"id": f"{int(time.time() * 1000):x}", "t": time.time(), "text": text,
             "game": game if game in ("oot", "mm") else None,
             "scene": str(scene) if scene else None,
             "room": room if isinstance(room, int) else None}
        with self.lock:
            self.notes.append(n)
        self._save_notes()
        return n

    def delete_note(self, nid):
        with self.lock:
            before = len(self.notes)
            self.notes = [n for n in self.notes if n.get("id") != nid]
            changed = len(self.notes) != before
        if changed:
            self._save_notes()
        return changed

    def notes_state(self):
        """Newest first, the automatic ones on top. Called under the lock."""
        out = sorted(self.notes, key=lambda n: -(n.get("t") or 0))
        if self._bombers_code:
            out.insert(0, {"id": "bombers", "auto": True, "t": None, "game": "mm",
                           "scene": None, "room": None,
                           "text": f"Bombers' code: {'-'.join(self._bombers_code)}"})
        return out

    def request_note(self):
        """The note key was pressed, wherever the focus was: remember the place
        and the window to go back to, bring the tracker's window forward, and
        ask the page to open its box."""
        with self.lock:
            here = self.state.get("pending_here") or {}
            self.note_prompt = {"t": time.time(), "game": here.get("game"),
                                "scene": here.get("scene"), "room": here.get("room")}
        if sys.platform != "win32":
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            prev = user32.GetForegroundWindow()
            hwnd = user32.FindWindowW(None, WINDOW_TITLE)
            if hwnd and hwnd != prev:
                self._note_prev_hwnd = prev
                user32.SetForegroundWindow(hwnd)
        except Exception as ex:
            print(f"[overlay] could not bring the window forward: {ex}")

    def note_done(self):
        """The note was posted or cancelled: hand the focus back."""
        with self.lock:
            self.note_prompt = None
        prev, self._note_prev_hwnd = self._note_prev_hwnd, None
        if not prev or sys.platform != "win32":
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            # a process that is not in front may not put another there; a tap
            # of ALT first is the long-standing way Windows lets it through
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 2, 0)
            user32.SetForegroundWindow(prev)
        except Exception as ex:
            print(f"[overlay] could not hand the focus back: {ex}")

    def snapshot(self):
        with self.lock:
            d = dict(self.state)
            # what the page needs beyond the poll: the memorandum, whether a
            # note is being asked for, the key, and the code
            d["notes"] = self.notes_state()
            d["note_prompt"] = self.note_prompt
            d["note_key"] = self.note_key
            d["bombers_code"] = self._bombers_code
            return json.dumps(d)

    def has_tables(self):
        return bool(self.table["checks"])

    def reload_from_table(self, new_table, spoiler=None):
        """Adopt tables built AFTER startup, without a restart.

        When the ROM is not found at startup (the seed not opened in the
        emulator yet, or the tracker started first) the tables are empty and
        the page shows "no ROM found". Discovery keeps retrying, and when it
        finally builds a checks.json this swaps it in: a fresh Tracker is built
        on the same link and this object adopts its whole state, keeping its
        identity —the HTTP server and the poll thread hold it— and its lock and
        uptime.

        Two situations reach here and cmd_overlay's follower thread drives
        both: the tables were never built (the seed not opened in the emulator
        yet, or the tracker started first), or the user changed seed while this
        ran, which Project64 announces in `Recent Rom 0`. What gets thrown away
        differs. From the no-tables state nothing had been tracked yet; after a
        change of seed what goes is the previous seed's progress, which is the
        point of it going — it is not this ROM's.
        """
        fresh = Tracker(self.link, new_table, spoiler=spoiler or self.spoiler, locate=self.locate)
        with self.lock:
            keep_lock, keep_started = self.lock, self._started
            keep_follows = self.follows_rom
            d = dict(fresh.__dict__)
            d["lock"] = keep_lock          # same lock object other threads sync on
            d["_started"] = keep_started   # keep uptime continuous across the swap
            # The swap replaces every attribute, this one included, and losing
            # it would put the page back to advising a restart nobody needs.
            d["follows_rom"] = keep_follows
            d["state"]["follows_rom"] = keep_follows
            self.__dict__ = d
        print(f"[overlay] tables loaded without a restart: {len(new_table['checks'])} checks")


# The inventory comes out of inventory.py with prefixed keys, so the grid is
# grouped by prefix instead of listing two hundred entries by hand. A tracker
# grid has to show what you do NOT have as well: that is exactly its value,
# seeing at a glance what is missing.
GROUPS = [
    ("eq", "Equipment"),
    ("quest", "Progress"),
    ("item", "Items"),
    ("up", "Upgrades"),
    ("key", "Keys"),
    ("boss", "Bosses"),
    ("fairy", "Fairies"),
    ("owl", "Owls"),
]

# Which dungeon each `dungeonKeys` index is, and which ones have keys. They
# are always shown, even at zero, so the whole set reads at a glance; the
# game's -1 means "no keys" and comes out here as 0.
KEYED_DUNGEONS = {
    "oot": [
        (3, "Forest"), (4, "Fire"), (5, "Water"), (6, "Spirit"),
        (7, "Shadow"), (8, "Well"), (11, "Gerudo"), (12, "Fortress"),
        (13, "Ganon"),
    ],
    "mm": [(0, "Woodfall"), (1, "Snowhead"), (2, "Great Bay"), (3, "Stone Tower")],
}

# The ones with a boss key. The bit comes from `dungeonItems`, whose bitfield
# is {maxKeys:5, map:1, compass:1, bossKey:1}, i.e. bit 0.
BOSS_DUNGEONS = {
    "oot": [(3, "Forest"), (4, "Fire"), (5, "Water"), (6, "Spirit"),
            (7, "Shadow"), (13, "Ganon")],
    "mm": [(0, "Woodfall"), (1, "Snowhead"), (2, "Great Bay"), (3, "Stone Tower")],
}

# MM's stray fairies, fifteen per dungeon, coloured like each dungeon.
# strayFairies[] (DungeonSceneIndex) only has the four temples: Clock Town's
# Great Fairy takes a single stray fairy, which is not held in this array but
# tracked as its own check ("Clock Town Stray Fairy").
FAIRY_DUNGEONS = [(0, "Woodfall", "#ff9ee0"), (1, "Snowhead", "#8ee6a2"),
                  (2, "Great Bay", "#b48cff"), (3, "Stone Tower", "#e0c25a")]
FAIRIES_PER_DUNGEON = 15

# Each fountain's set is "ready to redeem" once complete AND its Great Fairy
# reward has not been collected yet -- then the count goes orange. The reward is
# a check, so once it is done the badge stops being orange (the game keeps the
# count at 15 forever otherwise, see En_Elforg). Stone Tower's fairies restore
# the Great Fairy in Ikana, so that is where its reward lives.
GF_REWARD = {
    "Woodfall": ("Woodfall Great Fairy",),
    "Snowhead": ("Snowhead Great Fairy",),
    "Great Bay": ("Great Bay Great Fairy",),
    "Stone Tower": ("Ikana Great Fairy",),
}
# Clock Town's Great Fairy takes a single stray fairy, not a count of 15
# (En_Elforg's Clock Town path sets a switch flag instead of bumping
# strayFairies[]). So its tile is driven by the check, threshold 1.
CLOCK_STRAY_CHECK = "Clock Town Stray Fairy"
CLOCK_REWARD_CHECKS = ("Clock Town Great Fairy", "Clock Town Great Fairy Alt")
CLOCK_FAIRY_COLOR = "#ff9d3c"

# Width of each grid, in cells. Not an aesthetic choice: it is the shape of
# the game's own screen, and it falls out of the data itself.
#
#   Items      OoT and MM both store items[24] and the screen is 6 columns
#              wide, so the array IS the grid, slot for slot.
#   Masks      MM stores 48 slots: the first 24 are items and the next 24
#              masks, another 6x4 page.
#   Equipment  the nibbles of OotEquipment are swords, shields, tunics and
#              boots, three of each: 3 columns x 4 rows, like the menu.
#   Progress   with 6 columns OoT's 24 bits fall into their own rows:
#              medallions, warp songs, ocarina songs, and the stones with the
#              rest.
COLS = {"Items": 6, "Masks": 6, "Equipment": 3, "Progress": 6, "Upgrades": 4,
        "Keys": 5, "Bosses": 6, "Fairies": 5, "Owls": 5}
MM_MASK_FIRST = 24   # from here on, MM's item[] entries are masks

# Icons: mkicons.py pulls them from the ROM and the sheet index is the item
# id. For `item:` slots no map is needed —the value read IS the id—, so this
# only covers what is a boolean or a level.
#
# The names on the right come from icons.json, which itself comes from
# items.h; none of them were written from memory.
ICON_BY_KEY = {
    "eq:Kokiri Sword": "SWORD_KOKIRI",
    "eq:Master Sword": "SWORD_MASTER",
    "eq:Giant's Knife": "SWORD_KNIFE_BIGGORON",
    "eq:Deku Shield": "SHIELD_DEKU",
    "eq:Hylian Shield": "SHIELD_HYLIAN",
    "eq:Mirror Shield": "SHIELD_MIRROR",
    "eq:Kokiri Tunic": "TUNIC_KOKIRI",
    "eq:Goron Tunic": "TUNIC_GORON",
    "eq:Zora Tunic": "TUNIC_ZORA",
    "eq:Kokiri Boots": "BOOTS_KOKIRI",
    "eq:Iron Boots": "BOOTS_IRON",
    "eq:Hover Boots": "BOOTS_HOVER",
    "quest:Forest Medallion": "MEDALLION_FOREST",
    "quest:Fire Medallion": "MEDALLION_FIRE",
    "quest:Water Medallion": "MEDALLION_WATER",
    "quest:Spirit Medallion": "MEDALLION_SPIRIT",
    "quest:Shadow Medallion": "MEDALLION_SHADOW",
    "quest:Light Medallion": "MEDALLION_LIGHT",
    "quest:Kokiri's Emerald": "STONE_EMERALD",
    "quest:Goron's Ruby": "STONE_RUBY",
    "quest:Zora's Sapphire": "STONE_SAPPHIRE",
    "quest:Stone of Agony": "STONE_OF_AGONY",
    "quest:Gerudo Card": "GERUDO_CARD",
    "quest:Gold Skulltula Token": "GS_TOKEN",
}
# An empty slot does not say which item belongs in it: the value read is 0xFF
# and there is no id to get an icon from. Without this, everything you do not
# have yet would come out as initials, which is the opposite of what a grid is
# for — you want to see the icon greyed out and know what you are missing.
#
# OoT's slot order is fixed and matches the names inventory.py uses.
DEFAULT_ICON_BY_KEY = {
    "item:deku stick": "STICK",
    "item:deku nut": "NUT",
    "item:bomb": "BOMB",
    "item:bow": "BOW",
    "item:fire arrow": "ARROW_FIRE",
    "item:Din's Fire": "SPELL_FIRE",
    "item:slingshot": "SLINGSHOT",
    "item:ocarina": "OCARINA_FAIRY",
    "item:bombchu": "BOMBCHU_10",
    "item:hookshot": "HOOKSHOT",
    "item:ice arrow": "ARROW_ICE",
    "item:Farore's Wind": "SPELL_WIND",
    "item:boomerang": "BOOMERANG",
    "item:lens of truth": "LENS",
    "item:magic bean": "MAGIC_BEAN",
    "item:Megaton hammer": "HAMMER",
    "item:light arrow": "ARROW_LIGHT",
    "item:Nayru's Love": "SPELL_LOVE",
    "item:bottle 1": "BOTTLE_EMPTY",
    "item:bottle 2": "BOTTLE_EMPTY",
    "item:bottle 3": "BOTTLE_EMPTY",
    "item:bottle 4": "BOTTLE_EMPTY",
    "item:adult trade": "CLAIM_CHECK",
    "item:child trade": "WEIRD_EGG",
    # the five MM masks that also exist in OoT
    "item:Keaton Mask": "KEATON_MASK",
    "item:Bunny Hood": "BUNNY_HOOD",
    "item:Goron Mask": "GORON_MASK",
    "item:Zora Mask": "ZORA_MASK",
    "item:Mask of Truth": "MASK_OF_TRUTH",
    "item:Hover Boots": "BOOTS_HOVER",
}

# Songs and other things with no icon in the ROM: these get drawn.
#
# Where the game paints a song in a colour of its own, that colour is used and
# not invented: OoT's six warp songs (z_kaleido_collect.c, sSongsPrim*) and
# MM's five area songs (z_kaleido_collect.c, sQuestSongsPrim*: Sonata green,
# Goron Lullaby red, Bossa Nova blue, Elegy orange, Oath magenta). Every other
# song both games paint white, and there each one gets a glyph AND a colour
# so that at a glance -- on a stream, at chip size -- they read apart: leaf
# green for Saria, bolt yellow for storms, heart pink for healing, orange sun,
# blue hourglass for time, chestnut horseshoe for Epona, pale feather for
# soaring. Where a song exists in both games it keeps one look.
#
# (glyph, colour) — colour None = normal ink
GLYPH_BY_KEY = {
    # OoT, warp songs: the colour IS the data
    "quest:Minuet of Forest": ("note", "#3fae55"),
    "quest:Bolero of Fire": ("note", "#e0483c"),
    "quest:Serenade of Water": ("note", "#3f83e0"),
    "quest:Requiem of Spirit": ("note", "#e08b33"),
    "quest:Nocturne of Shadow": ("note", "#9459d6"),
    "quest:Prelude of Light": ("note", "#e6d44f"),
    # the songs the games paint white: glyph and colour both carry the name
    "quest:Zelda's Lullaby": ("triforce", "#e6d44f"),
    "quest:Epona's Song": ("horseshoe", "#c8813f"),
    "quest:Saria's Song": ("leaf", "#4cc25f"),
    "quest:Sun's Song": ("sun", "#f0862e"),
    "quest:Song of Time": ("hourglass", "#5fa8e6"),
    "quest:Song of Storms": ("bolt", "#f0d24a"),
    "quest:Song of Healing": ("heart", "#ea6fae"),
    "quest:Song of Soaring": ("soar", "#a9dcef"),
    # MM, area songs: the game's own colours (sQuestSongsPrim*)
    "quest:New Wave Bossa Nova": ("wave", "#6496ff"),
    "quest:Elegy of Emptiness": ("elegy", "#ffa000"),
    "quest:Oath to Order": ("oath", "#ff64ff"),
    "quest:Goron Lullaby": ("lullaby", "#ff5028"),
    "quest:Goron Lullaby (half)": ("lullaby", "#ff5028"),
    "quest:Song of Awakening": ("sprout", "#96ff64"),
    "quest:Bombers' Notebook": ("book", None),
    # owl statues and clocks: one glyph each, the label tells them apart
    **{f"owl:{n}": ("owl", "#d9b56a") for n in
       ["Great Bay", "Zora Cape", "Snowhead", "Mountain Village", "Clock Town",
        "Milk Road", "Woodfall", "Southern Swamp", "Ikana Canyon", "Stone Tower"]},
    **{f"clock:{n}": ("clock", "#9ad0ff" if n.startswith("Day") else "#7f86c9") for n in
       ["Day 1", "Night 1", "Day 2", "Night 2", "Day 3", "Night 3"]},
    "quest:Odolwa's Remains": ("remains", "#4faa5a"),
    "quest:Goht's Remains": ("remains", "#d0761f"),
    "quest:Gyorg's Remains": ("remains", "#3f83e0"),
    "quest:Twinmold's Remains": ("remains", "#c8b23f"),
}

# MM's own 19 masks, as a fallback. The real icons DO live in the ROM, inside
# a CmpDma archive —see the POC— and mkicons.py extracts them; these drawings
# are what gets used if that archive cannot be located. The colour follows each
# mask's look in the game.
#
# The five MM shares with OoT (Keaton, Bunny Hood, Goron, Zora, Truth) are not
# here: those have a real icon of their own.
GLYPH_BY_KEY.update({
    "item:Postman's Hat": ("mask-postman", "#4a7fd0"),
    "item:All-Night Mask": ("mask-allnight", "#8a6ab5"),
    "item:Blast Mask": ("mask-blast", "#8f949b"),
    "item:Stone Mask": ("mask-stone", "#9a9a90"),
    "item:Great Fairy's Mask": ("mask-fairy", "#e87ba4"),
    "item:Deku Mask": ("mask-deku", "#8fbf3f"),
    "item:Bremen Mask": ("mask-bremen", "#6b7280"),
    "item:Don Gero's Mask": ("mask-frog", "#4faa5a"),
    "item:Mask of Scents": ("mask-scents", "#b06fc0"),
    "item:Romani's Mask": ("mask-romani", "#d9534f"),
    "item:Circus Leader's Mask": ("mask-circus", "#d8d2c4"),
    "item:Kafei's Mask": ("mask-kafei", "#9b7ad6"),
    "item:Couple's Mask": ("mask-couple", "#e6c04a"),
    "item:Kamaro's Mask": ("mask-kamaro", "#dcdcd4"),
    "item:Gibdo Mask": ("mask-gibdo", "#c9b98f"),
    "item:Garo's Mask": ("mask-garo", "#7a5fb0"),
    "item:Captain's Hat": ("mask-captain", "#a3b4c4"),
    "item:Giant's Mask": ("mask-giant", "#c2b48a"),
    "item:Fierce Deity's Mask": ("mask-fierce", "#e8e4d8"),
})

# What does not get shown. The stick and nut upgrades are noise in the grid,
# and **strength and scale do not exist in MM** —the fields are in the struct
# because MmUpgrades copies OoT's, but the game does not use them—; in their
# place go sword and shield progression, which are MM's own.
HIDE_KEYS = {
    "oot": {"up:deku sticks", "up:deku nuts"},
    "mm": {"up:deku sticks", "up:deku nuts", "up:strength", "up:scale"},
}

# Order of MM's progress screen. Without this it comes out in bit order, which
# mixes remains and songs and leaves the notebook and the Goron half stranded
# at the end. This way the first row is the four remains, the notebook and the
# half, and the twelve songs take two full rows in the game's order.
MM_PROGRESO_ORDER = [
    "Odolwa's Remains", "Goht's Remains", "Gyorg's Remains", "Twinmold's Remains",
    "Bombers' Notebook", "Goron Lullaby (half)",
    # ocarina songs first and dungeon ones after, like the menu
    "Song of Time", "Song of Healing", "Epona's Song",
    "Song of Soaring", "Song of Storms", "Sun's Song",
    "Song of Awakening", "Goron Lullaby", "New Wave Bossa Nova",
    "Elegy of Emptiness", "Oath to Order", "Saria's Song",
]

# Same in OoT: the six ocarina songs on top and the six warp songs below. The
# rest of the screen (medallions above, stones and the rest below) already came
# out right in bit order.
OOT_PROGRESO_ORDER = [
    "Forest Medallion", "Fire Medallion", "Water Medallion",
    "Spirit Medallion", "Shadow Medallion", "Light Medallion",
    "Zelda's Lullaby", "Epona's Song", "Saria's Song",
    "Sun's Song", "Song of Time", "Song of Storms",
    "Minuet of Forest", "Bolero of Fire", "Serenade of Water",
    "Requiem of Spirit", "Nocturne of Shadow", "Prelude of Light",
    "Kokiri's Emerald", "Goron's Ruby", "Zora's Sapphire",
    "Stone of Agony", "Gerudo Card", "Gold Skulltula Token",
]

# MM's boss remains: they are real items and have their icon in the ROM, so
# there is no need to draw them.
QUEST_MM_ITEM = {
    "quest:Odolwa's Remains": "REMAINS_ODOLWA",
    "quest:Goht's Remains": "REMAINS_GOHT",
    "quest:Gyorg's Remains": "REMAINS_GYORG",
    "quest:Twinmold's Remains": "REMAINS_TWINMOLD",
}

# The song note does **not** come from the ROM: the game draws them all with a
# single static texture that lives in `code`, not in the icon archive
# (`gItemIconSongNoteTex` in z_inventory.c). The drawn note stays, in the
# game's colours, which are the part that carries the information anyway.
SONG_NOTE_ITEM = None

# MM's sword and shield progression, by level.
MM_LEVEL_ITEM = {
    "up:sword": [None, "SWORD_KOKIRI", "SWORD_RAZOR", "SWORD_GILDED"],
    "up:shield": [None, "SHIELD_HERO", "SHIELD_MIRROR"],
}

# Upgrades are levels: the icon changes with the level you have.
ICON_BY_LEVEL = {
    "up:quiver": [None, "QUIVER", "QUIVER2", "QUIVER3"],
    "up:bomb bag": [None, "BOMB_BAG", "BOMB_BAG2", "BOMB_BAG3"],
    "up:bullet bag": [None, "BULLET_BAG", "BULLET_BAG2", "BULLET_BAG3"],
    "up:strength": [None, "GORON_BRACELET", "SILVER_GAUNTLETS", "GOLDEN_GAUNTLETS"],
    "up:scale": [None, "SILVER_SCALE", "GOLDEN_SCALE"],
    "up:wallet": [None, "WALLET2", "WALLET3"],
}


def load_icons():
    p = paths.user("icons.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------
# The user's own icons
# --------------------------------------------------------------------------
#
# An `icons/` folder to drop images in by hand, for anything you would rather
# replace. Nothing is downloaded: you put them there, and the overlay uses them
# if present. They override the ROM icon.
#
#   icons/mm/deku-mask.png     Majora's Mask only
#   icons/oot/...              Ocarina of Time only
#   icons/something.png        both
#
# The filename is compared normalised, so both the name the overlay shows
# ("Deku Mask" -> deku-mask.png) and the items.h one ("MASK_DEKU" ->
# mask-deku.png) work. .png, .gif, .webp and .jpg are served.
USER_ICON_DIR = paths.user("icons")
USER_ICON_EXT = (".png", ".gif", ".webp", ".jpg", ".jpeg")


def normalize_icon_name(text):
    # Apostrophes are **dropped**, they do not split: "Garo's Mask" has to
    # match `garos-mask.png`, which is how anyone would write it, and not
    # `garo-s-mask.png`.
    limpio = str(text).lower().replace("'", "").replace("’", "")
    out = [ch if ch.isalnum() else "-" for ch in limpio]
    return "-".join(x for x in "".join(out).split("-") if x)


def _icons_dir_stamp():
    """A cheap fingerprint of the folder, to tell whether to re-scan it."""
    if not os.path.isdir(USER_ICON_DIR):
        return None
    marcas = []
    for root, _dirs, files in os.walk(USER_ICON_DIR):
        marcas.append((root, len(files), int(os.path.getmtime(root))))
    return tuple(sorted(marcas))


def scan_user_icons():
    """{(juego|None, nombre normalizado): ruta relativa}."""
    found = {}
    if not os.path.isdir(USER_ICON_DIR):
        return found
    for root, _dirs, files in os.walk(USER_ICON_DIR):
        rel_dir = os.path.relpath(root, USER_ICON_DIR).replace("\\", "/")
        game = rel_dir if rel_dir in ("oot", "mm") else None
        for f in files:
            stem, ext = os.path.splitext(f)
            if ext.lower() not in USER_ICON_EXT:
                continue
            rel = f if rel_dir == "." else f"{rel_dir}/{f}"
            found[(game, normalize_icon_name(stem))] = rel
    return found
# scalars that go to the figures row, not to the grid. Each game keeps its own
# rupees and hearts (the other game's save only moves when you cross), so both
# lists carry them; the page shows the running game's when it is not filtered
# to one. `hearts` folds `max hearts` in: one figure in hearts, `14/20`, not
# two raw counts of sixteenths.
SCALARS = {
    "oot": ["rupees", "hearts", "skulltulas", "deaths", "triforce"],
    "mm": ["rupees", "hearts", "heart pieces", "swamp skulltulas", "ocean skulltulas"],
}
# Only shown when non-zero: a seed without a Triforce hunt would otherwise
# carry a permanent "triforce 0".
SCALARS_OPTIONAL = {"triforce"}
# the padding bits of the equipment nibbles and the unnamed slots add nothing
# to a grid
JUNK = ("bit 3",)


def is_on(prefix, value):
    """Whether this entry counts as obtained.

    Careful with inventory slots: there, empty is 0xFF and **0 is a legitimate
    item id** (Deku Stick in OoT, Ocarina of Time in MM). Treating it as empty,
    which is the natural thing in every other field, leaves the first slot of
    both grids greyed out forever.
    """
    if prefix == "item":
        return isinstance(value, int) and value != 0xFF
    return value not in (0, 0xFF, None, "")


# Which MM item belongs to each named slot. Needed to grey out what you do
# **not** have: an empty slot reads 0xFF and does not say which mask goes there.
#
# Done by hand rather than by name similarity, because the names are not
# similar enough: the label says "Postman's Hat" and items.h `MASK_POSTMAN`,
# and "Circus Leader's Mask" is `MASK_TROUPE_LEADER`, which shares not one
# word.
MM_SLOT_ITEM = {
    "Postman's Hat": "MASK_POSTMAN",
    "All-Night Mask": "MASK_ALL_NIGHT",
    "Blast Mask": "MASK_BLAST",
    "Stone Mask": "MASK_STONE",
    "Great Fairy's Mask": "MASK_GREAT_FAIRY",
    "Deku Mask": "MASK_DEKU",
    "Keaton Mask": "MASK_KEATON",
    "Bremen Mask": "MASK_BREMEN",
    "Bunny Hood": "MASK_BUNNY",
    "Don Gero's Mask": "MASK_DON_GERO",
    "Mask of Scents": "MASK_SCENTS",
    "Goron Mask": "MASK_GORON",
    "Romani's Mask": "MASK_ROMANI",
    "Circus Leader's Mask": "MASK_TROUPE_LEADER",
    "Kafei's Mask": "MASK_KAFEI",
    "Couple's Mask": "MASK_COUPLE",
    "Mask of Truth": "MASK_TRUTH",
    "Zora Mask": "MASK_ZORA",
    "Kamaro's Mask": "MASK_KAMARO",
    "Gibdo Mask": "MASK_GIBDO",
    "Garo's Mask": "MASK_GARO",
    "Captain's Hat": "MASK_CAPTAIN",
    "Giant's Mask": "MASK_GIANT",
    "Fierce Deity's Mask": "MASK_FIERCE_DEITY",
    "Powder Keg": "POWDER_KEG",
    "Hover Boots": "BOOTS_HOVER",
}

_MM_BY_LABEL = None
_MM_BY_ITEM = None


def mm_sheet(icons, nombre):
    """Sheet index of the MM item called that in items.h."""
    global _MM_BY_ITEM
    if _MM_BY_ITEM is None:
        import inventory

        mm = (icons or {}).get("mm") or {}
        _MM_BY_ITEM = {
            n: mm[str(v)]
            for v, n in inventory.item_ids().get("mm", {}).items()
            if str(v) in mm
        }
    return _MM_BY_ITEM.get(nombre)


def mm_icon_for_label(icons, label):
    """Sheet index of the item that belongs in that MM slot."""
    global _MM_BY_LABEL
    if _MM_BY_LABEL is None:
        import inventory

        por_nombre = {n: v for v, n in inventory.item_ids().get("mm", {}).items()}
        mm = (icons or {}).get("mm") or {}
        _MM_BY_LABEL = {}
        for etiqueta, nombre in MM_SLOT_ITEM.items():
            idx = mm.get(str(por_nombre.get(nombre)))
            if idx is not None:
                _MM_BY_LABEL[etiqueta] = idx
    return _MM_BY_LABEL.get(label)


def icon_index(icons, game, key, value, on, slot=None):
    """Sheet cell for this inventory entry, or None.

    For `item:` slots the value read already IS the item id, which is the sheet
    index; in MM it has to go through the name bridge because its ids are
    different. The rest are booleans or levels and go through a table.
    """
    if not icons:
        return None
    if key.startswith("item:"):
        got = None
        if on and isinstance(value, int):
            if game == "oot":
                got = value if value < icons["count"] else None
            else:
                # the real one, out of the ROM's CmpDma archive; the name
                # bridge to OoT is only a fallback
                got = (icons.get("mm") or {}).get(str(value))
                if got is None:
                    got = icons["mm_bridge"].get(str(value))
        if got is not None:
            return got
        # empty: show the icon that belongs to the slot, greyed out
        if game == "mm":
            got = mm_icon_for_label(icons, key.partition(":")[2])
            if got is not None:
                return got
            # With no name, go by position. MM's slot order comes from the
            # decomp (z64item.h) and matches the item ids: slot 0 is the
            # ocarina, 1 the bow, 8 the sticks... The last six are the bottles,
            # and they all show the empty one.
            if slot is not None and slot < 24:
                mm = (icons.get("mm") or {})
                return mm.get(str(min(slot, 0x12) if slot >= 0x12 else slot))
        return icons["oot"].get(DEFAULT_ICON_BY_KEY.get(key, ""))
    if game == "mm" and key in MM_LEVEL_ITEM:
        ramp = MM_LEVEL_ITEM[key]
        lvl = value if isinstance(value, int) else 0
        nombre = ramp[lvl] if 0 <= lvl < len(ramp) else ramp[-1]
        return mm_sheet(icons, nombre or ramp[1])
    if game == "mm" and key in QUEST_MM_ITEM:
        return mm_sheet(icons, QUEST_MM_ITEM[key])
    if key in ICON_BY_LEVEL:
        ramp = ICON_BY_LEVEL[key]
        lvl = value if isinstance(value, int) else 0
        name = ramp[lvl] if 0 <= lvl < len(ramp) else ramp[-1]
        # when off, show the first step so the cell is not left blank
        return icons["oot"].get(name or ramp[1])
    name = ICON_BY_KEY.get(key)
    return icons["oot"].get(name) if name else None


def user_icon_for(user_icons, game, label, value):
    """The image you placed yourself for this entry, if there is one.

    Two names are tried: the one the overlay shows ("Deku Mask") and the
    items.h one for whatever id is in the slot ("MASK_DEKU"), looking first in
    the game's folder and then in the shared one.
    """
    if not user_icons:
        return None
    import inventory

    nombres = []
    if label:
        nombres.append(normalize_icon_name(label))
    if isinstance(value, int):
        n = inventory.item_ids().get(game, {}).get(value)
        if n:
            nombres.append(normalize_icon_name(n))
    for n in nombres:
        for g in (game, None):
            rel = user_icons.get((g, n))
            if rel:
                return rel
    return None


def item_grid(game, snap, icons=None, user_icons=None, fairy=None):
    import inventory

    groups = collections.OrderedDict((label, []) for _, label in GROUPS)
    groups["Masks"] = []
    slot = 0
    for key, value in snap.items():
        if any(j in key for j in JUNK):
            continue
        prefix, _, label = key.partition(":")
        group = dict(GROUPS).get(prefix)
        if not group:
            continue
        if key in HIDE_KEYS.get(game, ()):
            continue
        on = is_on(prefix, value)
        if prefix == "item":
            # the grid reproduces the game's screen, so empty slots stay:
            # they are part of the drawing, not noise. In MM the second half is
            # the mask page.
            if game == "mm" and slot >= MM_MASK_FIRST:
                group = "Masks"
            slot += 1
        # The figure in the corner: ammo on items, level on upgrades.
        # `ammo[]` is indexed by inventory slot just like `items[]`, so the
        # number lands in the same cell the game paints it in. On a boolean
        # nothing is written: a "1" over the icon only gets in the way.
        badge = ""
        if on and prefix == "up" and isinstance(value, int) and value > 0:
            badge = str(value)
        elif on and prefix == "item":
            n = snap.get(f"ammo:{slot - 1}")
            if isinstance(n, int) and n > 0:
                badge = str(n)
        # A slot inventory.py does not name comes out as "slot 7", and from
        # that you get an "S7" chip that says nothing. If it is occupied, the
        # id read is enough to give it its real name; if it is empty, it is a
        # free cell of the menu and goes unlabelled.
        if label.startswith("slot "):
            if on and isinstance(value, int):
                label = (inventory.item_ids().get(game, {}).get(value)
                         or f"0x{value:02X}").replace("_", " ").title()
            else:
                label = ""

        glyph, color = GLYPH_BY_KEY.get(key, (None, None))
        # songs use the ROM's note texture as a mask, with the overlay
        # supplying the colour: exactly what the game does when it draws it
        mask = None
        if glyph == "note" and SONG_NOTE_ITEM:
            mask = mm_sheet(icons, SONG_NOTE_ITEM)
            if mask is not None:
                glyph = None
        img = user_icon_for(user_icons, game, label, value if on else None)
        groups[group].append(
            {
                "label": label,
                "on": on,
                # an image of yours overrides the ROM icon
                "img": f"/usericon/{img}" if img else None,
                "icon": icon_index(icons, game, key, value, on, slot - 1 if prefix == "item" else None),
                "glyph": glyph,
                "mask": mask,
                "color": color,
                "badge": badge,
                "value": inventory.fmt(key, value, game) if on else "",
            }
        )
    # MM's progress panel comes out in bit order, which mixes remains and
    # songs. Reordered like this, the first row is the four remains with the
    # notebook and the Goron half, and the songs take two full lines, like the
    # game's screen. The order of the twelve songs is the canonical one, that
    # of their ids (SONATA, LULLABY, NOVA, ELEGY, OATH, SARIA | TIME, HEALING,
    # EPONA, SOARING, STORMS, SUN).
    # Keys and fairies do not come out of the walk above: they are arrays
    # indexed by dungeon, and only the ones that carry them matter.
    llave_icon = (mm_sheet(icons, "SMALL_KEY") if game == "mm" else None) or \
        (icons or {}).get("oot", {}).get("SMALL_KEY")
    groups["Keys"] = []
    for idx, nombre in KEYED_DUNGEONS.get(game, ()):
        n = snap.get(f"key:{idx}")
        n = n if isinstance(n, int) and n > 0 else 0
        groups["Keys"].append({
            "label": f"{nombre} keys", "on": n > 0, "icon": llave_icon,
            "glyph": None, "mask": None, "color": None,
            "badge": str(n) if n else "", "value": str(n),
        })

    jefe_icon = (icons or {}).get("oot", {}).get("BOSS_KEY")
    groups["Bosses"] = []
    for idx, nombre in BOSS_DUNGEONS.get(game, ()):
        tiene = bool(snap.get(f"boss:{idx}"))
        groups["Bosses"].append({
            "label": f"{nombre} boss key", "on": tiene, "icon": jefe_icon,
            "glyph": None, "mask": None, "color": None,
            "badge": "", "value": "yes" if tiene else "no",
        })

    if game == "mm":
        fi = fairy or {}
        rewards_done = fi.get("rewards_done", {})
        groups["Fairies"] = []
        for idx, nombre, col in FAIRY_DUNGEONS:
            n = snap.get(f"fairy:{idx}")
            n = n if isinstance(n, int) and 0 <= n <= FAIRIES_PER_DUNGEON else 0
            rdone = rewards_done.get(nombre, False)
            ready = n >= FAIRIES_PER_DUNGEON and not rdone
            groups["Fairies"].append({
                "label": f"{nombre} fairies", "on": n > 0 or rdone, "icon": None,
                "glyph": "fairy", "color": col,
                "mask": None, "ready": ready,
                "badge": str(n) if n else "", "value": f"{n}/{FAIRIES_PER_DUNGEON}",
            })
        # Clock Town: a single stray fairy, driven by its check (there is no
        # count in strayFairies[]). Only shown when the seed has that check.
        if fi.get("clock_in_seed"):
            have = fi.get("clock_have", False)
            rdone = fi.get("clock_reward_done", False)
            groups["Fairies"].append({
                "label": "Clock Town fairy", "on": have or rdone, "icon": None,
                "glyph": "fairy", "color": CLOCK_FAIRY_COLOR,
                "mask": None, "ready": have and not rdone,
                "badge": "1" if have else "", "value": f"{1 if have else 0}/1",
            })

    orden_prog = MM_PROGRESO_ORDER if game == "mm" else OOT_PROGRESO_ORDER
    if groups.get("Progress"):
        pos = {n: i for i, n in enumerate(orden_prog)}
        groups["Progress"].sort(key=lambda it: pos.get(it["label"], len(pos)))

    order = ["Items", "Masks", "Equipment", "Upgrades", "Keys", "Bosses", "Fairies", "Owls", "Progress"]
    return [
        {"name": k, "cols": COLS.get(k, 6), "items": groups[k]}
        for k in order
        if groups.get(k)
    ]


def item_scalars(game, snap):
    import inventory

    out = []
    for key in SCALARS.get(game, []):
        if key not in snap:
            continue
        if key in SCALARS_OPTIONAL and not snap[key]:
            continue
        if key == "hearts":
            value = inventory.hearts_text(snap["hearts"], snap.get("max hearts"))
        else:
            value = inventory.fmt(key, snap[key], game)
        out.append({"label": key, "value": value})
    return out


# Scene according to the save context. FALLBACK ONLY: the live scene comes
# from the PlayState (Tracker.play_cached). These lag, measured against the two
# dumps -- OoT gave KOKIRI_FOREST with the player inside KOKIRI_SHOP, and MM's
# is the *saved* scene, which had nothing to do with where the player was.
#   OoT: info.sceneId,             ASSERT_OFFSET(OotSave, info.sceneId, 0x66)
#   MM:  playerData.savedSceneNum, at info+0x26 -> base+0x42
SCENE_OFF = {"oot": 0x66, "mm": 0x42}

# gSaveContext.sceneSetupId: WHICH VERSION of the scene is loaded. A scene can
# exist as child/adult and day/night, each with its own actors and therefore
# its own checks, so without this the panel lists twins you cannot reach.
#
# It is not in the save, it is in the SaveContext that wraps it:
#   ASSERT_OFFSET(OotSaveContext, sceneSetupId, 0x1360)   OotSaveContext{OotSave save; ...}
#   ASSERT_OFFSET(MmSaveContext,  sceneSetupId, 0x3cac)   MmSaveContext{MmSave save; ...}
# The base this project uses for MM is MmSave+0x08 (see the POC), hence the -8.
# Confirmed on both dumps: with the right offset MM reads 0, with the base
# taken as MmSave it reads garbage.
SETUP_OFF = {"oot": 0x1360, "mm": 0x3CAC - 0x08}

# gSaveContext.gameMode: whether a game is being PLAYED at all. It sits right
# before sceneSetupId in both games (zeldaret decomps, include/z64save.h):
#   OoT  /* 0x135C */ s32 gameMode;   0 NORMAL, 1 TITLE_SCREEN, 2 FILE_SELECT, 3 END_CREDITS
#   MM   /* 0x3CA8 */ s32 gameMode;   the same four, plus 4 OWL_SAVE
# Only the running game's own context has it; the other game's buffer is a bare
# save. Anything but 0 means the RAM is not a run: on the title screen and in
# the file select it still holds whatever was there before, and every reader
# took that as progress -- 213 checks done and an entrance "gone through" on a
# file that had not even been created, measured 17 Aug 2026 (the tracker was
# started from the main menu; entrance 0 there is a real id, ENTR_DEKU_TREE_0,
# and it matched a shuffled destination). So while it is not 0 nothing is read,
# the last picture stands, and the page says why.
GAME_MODE_OFF = {"oot": 0x135C, "mm": 0x3CA8 - 0x08}
GAME_MODES = {0: "playing", 1: "title screen", 2: "file select", 3: "credits", 4: "owl save"}
# Not a gameMode the game has: what poll_once reports while the running
# game's save is not yet where this ROM's code keeps it (see own_save_appeared).
GAME_MODE_COPY = "copy"
GAME_MODES[GAME_MODE_COPY] = "menu, the save is not loaded yet"
# The modes that mean "not in a run" and DO stop the read. 0 is playing; None
# is "could not read"; anything else fits no mode at all -- almost always a
# single poll caught mid-cross between OoT and MM, when the bases are flipping
# and this fixed offset reads rubbish, and not a reason to freeze a live game.
GAME_MODE_NOT_PLAYING = {1, 2, 3, 4}
# How many polls in a row an unknown gameMode has to last before it is the
# offset being wrong for this build (a real problem) rather than a crossing
# transient (which lasts a poll or two). At POLL_SECONDS this is several
# seconds, well past any OoT<->MM switch.
GAME_MODE_ODD_PERSIST = 12


# --------------------------------------------------------------------------
# Servidor
# --------------------------------------------------------------------------


def cargar_spoiler(tracker, raw):
    """Validate the uploaded spoiler and, if it fits, load it live.

    Loading the wrong spoiler is worse than loading none: the names would half
    match and the filler filter would call things surplus that are not. Two
    cheap barriers, in order of strength:

      1. the version its header declares against checks.json's
      2. how many of its locations exist in checks.json

    What CANNOT be detected is another seed of the same version: it has the
    same locations with different items. That is why COVERAGE comes back too
    —how many of our checks it names— and the page shows it.

    Coverage matters more than it looks, and that surfaced while testing: the
    spoiler from another v32.0 seed carried 980 locations and all 980 matched,
    so it sailed past the barrier above. But it covered 20% of the checks, and
    about what it does not name `is_junk` can say nothing: the filter ended up
    classifying nothing and "only what matters" still showed all 4,995. It is
    not rejected —a seed with other settings genuinely has fewer locations—
    but it is flagged, because a filter that does not filter looks broken all
    the same.
    """
    import ootmm

    lineas = raw.decode("utf-8", "replace").splitlines()
    # A multiworld log carries every world's placement: only this ROM's
    # section describes the seed in front of you (see parse_spoiler_worlds).
    spoiler = ootmm.parse_spoiler(lineas, getattr(tracker, "world", None))
    if not spoiler:
        return {"ok": False, "error": "no locations in there; that is not an OoTMM spoiler log"}

    ver = ootmm.spoiler_version(lineas)
    esperada = tracker.table.get("version")
    if ver and esperada and ver != esperada:
        return {"ok": False, "error": f"that spoiler is from {ver} and checks.json from {esperada}"}

    conocidas = {(c["game"], c["name"]) for c in tracker.table["checks"]}
    casan = sum(1 for k in spoiler if k in conocidas)
    if casan < len(spoiler) * 0.5:
        return {"ok": False, "error": f"only {casan} of {len(spoiler)} locations match "
                                      f"checks.json; it is from another seed or version"}

    # the same checks that count towards the total, so the fraction can be
    # compared against what the summary shows
    activos = [c for c in tracker.table["checks"]
               if c["addr"] is not None and "anchor" in c and tracker.is_active(c)]
    cubiertos = sum(1 for c in activos if (c["game"], c["name"]) in spoiler)

    res = tracker.set_spoiler(spoiler)
    # The name guard above passes a same-version spoiler from ANOTHER seed --
    # same locations, different items. set_spoiler vets the items against the
    # ROM and drops it when they disagree; say so instead of reporting a
    # coverage for a spoiler that was thrown away.
    if res.get("rejected"):
        return {"ok": False, "error": f"it is not this seed's — it {res['rejected']}"}
    res.update(ok=True, casan=casan, version=ver,
               cubiertos=cubiertos, activos=len(activos))
    print(f"[overlay] spoiler loaded from the page: {res['n']} locations, "
          f"covers {cubiertos}/{len(activos)} checks, {res['total_key']} matter")
    return res


def serve(tracker, host, port, open_window=True):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    page = paths.res("overlay.html")
    if host not in ("127.0.0.1", "localhost", "::1"):
        # The POST guard in do_POST tells other WEBSITES apart from this
        # page; it cannot tell other MACHINES apart from this one. Off the
        # loopback, anyone on that network can load a spoiler, reveal a
        # check, take a hint or write a note on this tracker.
        print(f"[overlay] listening on {host}: every machine on that network can drive"
              " this tracker's page actions (spoiler, hints, notes) -- there is no login.")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass  # the server must not clutter the tracker console

        def _send(self, body, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj):
            self._send(json.dumps(obj).encode("utf-8"), "application/json")

        def do_POST(self):
            # The spoiler's CONTENTS get uploaded, not its path. An endpoint
            # that opened whatever path it was handed would be an arbitrary
            # file read, and the server can end up listening off 127.0.0.1
            # with --http-host.
            route = self.path.split("?", 1)[0]
            if route not in ("/spoiler", "/hint", "/reveal", "/junk", "/note"):
                self.send_error(404)
                return
            # A guard against OTHER WEBSITES, and only that. Any website you
            # happen to have open can POST to 127.0.0.1 —nothing stops it— and
            # loading a spoiler turns `spoiler=full` on, so a stranger's page
            # could reveal what is left on your stream. Browsers label their
            # own requests and cannot be made to lie about these two headers.
            # A program on this machine (curl, a script) sends neither and is
            # let through: it is yours, and driving the tracker by hand is a
            # feature. What this does NOT guard is another machine, which is
            # why serve() says so when --http-host leaves the loopback.
            site = self.headers.get("Sec-Fetch-Site")
            origin = self.headers.get("Origin")
            mine = f"http://{self.headers.get('Host', '')}"
            if (site and site != "same-origin") or (origin and origin != mine):
                self._json({"ok": False, "error": "spoilers can only be loaded"
                            " from the tracker's own page"})
                return
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 16 * 1024 * 1024:
                self._json({"ok": False, "error": "file empty or too large"})
                return
            try:
                raw = self.rfile.read(n)
                if route == "/hint":
                    req = json.loads(raw.decode("utf-8"))
                    # `world` only when the request names it: absent means "any
                    # copy", which is what a name typed by hand means, and that
                    # is not the same question as "the one that is mine".
                    kw = {"world": req["world"]} if "world" in req else {}
                    # which copy, when the list numbered them
                    if req.get("copy") is not None:
                        try:
                            kw["copy"] = int(req["copy"])
                        except (TypeError, ValueError):
                            pass
                    got = tracker.hint(str(req.get("item", "")), req.get("level", 1), **kw)
                    self._json({"ok": got is not None, "hint": got,
                                "error": None if got else "no pending check holds that item"})
                elif route == "/reveal":
                    req = json.loads(raw.decode("utf-8"))
                    ok = tracker.reveal(str(req.get("game", "")), str(req.get("name", "")))
                    self._json({"ok": ok, "error": None if ok else "unknown check"})
                elif route == "/note":
                    # add (text + where), delete (by id), or cancel the box the
                    # note key opened; add and cancel both hand the focus back
                    req = json.loads(raw.decode("utf-8"))
                    if req.get("cancel"):
                        tracker.note_done()
                        self._json({"ok": True})
                    elif "delete" in req:
                        self._json({"ok": tracker.delete_note(str(req["delete"]))})
                    else:
                        n = tracker.add_note(req.get("text"), req.get("game"),
                                             req.get("scene"), req.get("room"))
                        tracker.note_done()
                        self._json({"ok": n is not None, "note": n,
                                    "error": None if n else "an empty note is no note"})
                elif route == "/junk":
                    # One switch per request, named by the key that is there:
                    # an older page that only knows "tokens" keeps working, and
                    # a body naming neither is a no-op rather than a silent flip
                    # of the wrong one.
                    req = json.loads(raw.decode("utf-8"))
                    if "fairies" in req:
                        self._json(tracker.set_fairies_junk(bool(req.get("fairies"))))
                    elif "tokens" in req:
                        self._json(tracker.set_tokens_junk(bool(req.get("tokens"))))
                    else:
                        self._json({"ok": False, "error": "no switch named"})
                else:
                    self._json(cargar_spoiler(tracker, raw))
            except Exception as ex:
                self._json({"ok": False, "error": f"{type(ex).__name__}: {ex}"})

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/state.json":
                self._send(tracker.snapshot().encode("utf-8"), "application/json")
            # `/` is the director view, everything together. `/p/<panel>`
            # serves the same page in single-panel mode: one block, no chrome,
            # to capture separately in OBS. The page decides the mode by
            # looking at its own path.
            elif path.startswith("/usericon/"):
                # only what the scan found gets served: no building paths out
                # of whatever comes in the URL. The %20 has to be decoded or a
                # name with spaces would never match.
                rel = urllib.parse.unquote(path[len("/usericon/"):])
                if rel not in set(tracker.user_icons.values()):
                    self.send_error(404)
                    return
                full = os.path.join(USER_ICON_DIR, rel.replace("/", os.sep))
                tipo = {
                    ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
                    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                }.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
                with open(full, "rb") as fh:
                    self._send(fh.read(), tipo)
            elif path == "/icons.png":
                # without the sheet the overlay still works: the cells fall
                # back to text, so this is a plain 404 and not an error
                p = paths.user("icons.png")
                if not os.path.exists(p):
                    self.send_error(404)
                    return
                with open(p, "rb") as fh:
                    self._send(fh.read(), "image/png")
            elif path in ("/", "/index.html") or path.startswith("/p/"):
                # A Browser Source pointing at an old name must not go blank:
                # the page picks its panel from its own path, so serving the
                # HTML as-is would strip every block. Redirect instead, query
                # string included.
                viejo = PANEL_ALIAS.get(path[len("/p/"):]) if path.startswith("/p/") else None
                if viejo:
                    qs = self.path.split("?", 1)
                    self.send_response(301)
                    self.send_header(
                        "Location", f"/p/{viejo}" + (f"?{qs[1]}" if len(qs) > 1 else ""))
                    self.end_headers()
                    return
                with open(page, "rb") as fh:
                    self._send(fh.read(), "text/html; charset=utf-8")
            else:
                self.send_error(404)

    class Servidor(ThreadingHTTPServer):
        # Off, and deliberately. Python turns it on for every HTTP server, and
        # on Windows that means a second tracker binds a port the first is
        # already serving on -- both listening, and which one the browser or
        # OBS reaches is a coin toss. With it off the second one says the port
        # is taken, which is the thing worth knowing.
        allow_reuse_address = False

    try:
        srv = Servidor((host, port), Handler)
    except OSError as ex:
        print(f"[overlay] cannot serve on {host}:{port} ({ex})")
        print("[overlay] another tracker is probably already running; close it,")
        print("[overlay] or start this one with --http-port.")
        raise SystemExit(1)
    url = f"http://{host}:{port}/"
    print(f"[overlay] OoTMM Tracker {__version__} - {STAGE_NOTE}")
    print(f"[overlay] full view: {url}")
    print("[overlay] single panels, one per OBS Browser Source:")
    for name in PANELS:
        print(f"           {url}p/{name}")
    print("[overlay] add ?chroma=none for a transparent background")
    print("[overlay] spoiler: ?spoiler=off (nothing) | item (default) | full\n")
    if open_window:
        threading.Thread(target=open_app_window, args=(url,), daemon=True).start()
    # the note key, held system-wide (Windows); see hotkey_thread
    threading.Thread(target=hotkey_thread, args=(tracker,), daemon=True).start()
    srv.serve_forever()


def open_app_window(url):
    """Open a window of our own, with no browser chrome.

    Chrome and Edge do this with --app=. With neither around we fall back to
    the default browser, which works the same but keeps its bar.
    """
    time.sleep(0.3)
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    import subprocess

    for exe in candidates:
        if os.path.exists(exe):
            try:
                subprocess.Popen(
                    [exe, f"--app={url}", "--window-size=1280,760", "--new-window"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except OSError:
                pass
    webbrowser.open(url)
