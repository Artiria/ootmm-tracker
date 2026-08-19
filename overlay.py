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
import threading
import time
import urllib.parse
import webbrowser

import paths
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
MM_SCENE_ALIASES = {
    "MM_TEMPLE_STONE_TOWER_INVERTED": "MM_TEMPLE_STONE_TOWER",
    "MM_SOUTHERN_SWAMP_CLEAR": "MM_SOUTHERN_SWAMP",
    "MM_MOUNTAIN_VILLAGE_SPRING": "MM_MOUNTAIN_VILLAGE_WINTER",
    "MM_GORON_VILLAGE_SPRING": "MM_GORON_VILLAGE_WINTER",
    "MM_TWIN_ISLANDS_SPRING": "MM_TWIN_ISLANDS_WINTER",
    "MM_STONE_TOWER_INVERTED": "MM_STONE_TOWER",
}

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
PANELS = ["summary", "regions", "items", "activity", "remaining", "entrances", "hints", "souls"]

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
    with open(path or paths.user("checks.json"), encoding="utf-8") as fh:
        return json.load(fh)


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
            if junk_pred and not junk_pred(c["name"]):
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
    """Which checks are done inside a block already read."""
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
            done.add(c["name"])
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
        # vanilla and MQ share a flag; in one seed only one version of each
        # dungeon exists, and which one is recorded by mkchecks
        self.mq_scenes = set(table.get("mq_scenes") or [])
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
            for a, b in MM_SCENE_ALIASES.items():
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
        # The soul bitmaps of the shared custom save and the ROM's catalogue
        # (souls.py), read from checks.json's `souls` block. The Decoder is
        # harmless on a seed that shuffles no souls: it reports not-ok and the
        # panel stays hidden.
        import souls as souls_mod
        self.souls = souls_mod.Decoder.from_table(table)
        self._rebuild_items()
        self.lock = threading.Lock()
        self.state = {
            # from the very first request, so the badge is there while waiting
            "version": __version__,
            "ready": False,
            "error": None,
            "waiting": link is None,
            "active": None,
            "trusted": True,
            "confidence": 1.0,
            "done_total": 0,
            "total": sum(n for _, _, n, _k in self.regions),
            "total_key": sum(k for _, _, _n, k in self.regions),
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
                "setup": None, "other_setup": 0, "other_room": 0, "list": [],
            },
            "spoiler_n": len(self.spoiler),
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
            # The ROM the emulator has open right now, and whether it is the
            # one the tables were built from. Read from Project64's own config
            # every ROM_CHECK_SECONDS; null when there is no emulator to ask.
            "rom_open": None,
            "rom_mismatch": False,
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
        self.check_game = {c["name"]: c["game"] for c in table["checks"]}
        # Which alternate scene headers each scene actually has, taken from the
        # setups its own xflags mention. Needed to resolve the loaded setup the
        # same way the game does -- see setup_loaded().
        self.scene_setups = collections.defaultdict(set)
        # Which real rooms each scene has checks in. Used to tell whether the
        # room you are standing in is one of its own -- see the grotto note in
        # refresh().
        self.scene_rooms = collections.defaultdict(set)
        for c in table["checks"]:
            xf = c.get("xflag")
            if xf is not None and c["scene_id"] is not None:
                self.scene_setups[c["game"], c["scene_id"]].add(xf["setup"])
                if xf["room"] < 0x20:
                    self.scene_rooms[c["game"], c["scene_id"]].add(xf["room"])
        # per-game totals, so an overlay filtered with ?game= shows its own
        # percentage and not the two together
        self.state["totals"] = collections.Counter()
        for game, _, n, _k in self.regions:
            self.state["totals"][game] += n
        self.state["totals"] = dict(self.state["totals"])

    def _rebuild_items(self):
        """Recompute everything that depends on knowing each spot's item.

        The ROM rules and a hand-loaded spoiler goes on top: if someone bothers
        to load one, that is the one they want. They agree anyway —checked
        over 5,018 checks, same filler classification in 100% of them— so the
        order only matters when one of the two is missing.
        """
        items = dict(self.rom_items)
        items.update(self.spoiler)
        self.items = items
        self.junk = {
            c["name"]: is_junk(items.get((c["game"], c["name"])))
            for c in self.table["checks"]
        }
        self.hay_spoiler = bool(items)
        self.plan, self.regions = build_plan(
            self.table, self.is_active,
            (lambda n: self.junk.get(n, False)) if items else None)
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
            self.state["items_n"] = len(self.items)
            # The filtered totals, right now: otherwise, between this and the
            # next poll the page shows the new total against the old progress
            # —"18 / 612"— and that number has never existed. The per-region
            # ones catch up on the poll, half a second later.
            hechos = [n for n in self._done if not self.junk.get(n, False)]
            self.state["done_key_total"] = len(hechos)
            self.state["done_key_by_game"] = {
                g: sum(1 for n in hechos if self.check_game.get(n) == g)
                for g in ("oot", "mm")
            }
            return {
                "n": len(self.spoiler),
                "total": self.state["total"],
                "total_key": self.state["total_key"],
            }

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
                            for g, b in self._bases.items())):
                return self._bases
            # crossing between games moves both: they have to be found again
            self._bases = None

        hints = self.payload_hints()
        bases = self.locate(self.link, verbose=False, hints=hints) if hints \
            else self.locate(self.link, verbose=False)
        self._bases = bases or None
        return bases

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
        got = 0
        if 0 <= wanted <= 3:
            for s in range(wanted, 0, -1):
                if s in have:
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

    def hint(self, item, level):
        """A hint about where `item` is, up to `level` (1 game, 2 region, 3 the
        check itself). Picks one holder: the first not-done check carrying that
        item in table order, so asking again lands on the same one; a
        progressive item with several copies gives away one location, not all.
        Levels only ever go up. Returns the hint entry or None."""
        level = max(1, min(3, int(level)))
        cur = self.hints.get(item)
        if cur is None:
            holders = [c for c in self.table["checks"]
                       if c["addr"] is not None and self.is_active(c)
                       and self.item_de(c["game"], c["name"]) == item]
            if not holders:
                return None
            c = next((h for h in holders if h["name"] not in self._done), holders[0])
            # the region is the scene, made readable: the pool's `hint` column
            # is a hint-group id and NONE on 98% of the rows, so it will not do
            region = (c["scene"] or "").replace("_", " ").title()
            cur = {"item": item, "level": 0, "game": c["game"], "region": region,
                   "check": c["name"], "t": time.time()}
            self.hints[item] = cur
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
            e = {"item": h["item"], "level": h["level"], "t": h["t"], "game": h["game"],
                 "done": h["check"] in done}
            if h["level"] >= 2:
                e["region"] = h["region"]
            if h["level"] >= 3:
                e["check"] = h["check"]
            out.append(e)
        out.sort(key=lambda e: -e["t"])
        return {"used": used, "items": out, "checks": sorted(self.revealed)}

    def hint_items(self, done):
        """What can be asked about: items of the checks not done, filler out."""
        seen = set()
        for c in self.table["checks"]:
            if c["addr"] is None or c["name"] in done or not self.is_active(c):
                continue
            it = self.item_de(c["game"], c["name"])
            if it and not self.junk.get(c["name"], False):
                seen.add(it)
        return sorted(seen)

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

        # Not in a run (title screen, file select, credits): stop here, before
        # anything is read. What the RAM holds then is not this file, and every
        # reader below -- checks, feed, entrances, souls -- would take it as
        # progress. See GAME_MODE_OFF. None (could not read) never gates.
        self._game_mode = self.game_mode(active, bases)
        if self._game_mode not in (None, 0):
            return None

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
            items["oot"] = inventory.snapshot(oot_blk)

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
            items["mm"] = inventory.mm_snapshot(
                self.link.read_block(bases["mm"], 0x1500), oot_blk, half_days)

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
                s["uptime"] = int(time.time() - self._started)
            return
        active, done, conf, items, scene, room, live, setup_raw = polled

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
            for name in sorted(done - self._done):
                feed.append(
                    {
                        "check": name,
                        "game": self.check_game.get(name),
                        "item": self.item_de("oot", name) or self.item_de("mm", name),
                        "world": self.world_de("oot", name) or self.world_de("mm", name),
                        "t": time.time(),
                    }
                )
        self._done = done
        self._seeded = True

        by_scene = collections.Counter()
        by_scene_key = collections.Counter()
        for c in self.table["checks"]:
            if c["name"] in done and self.is_active(c):
                by_scene[(c["game"], c["scene"])] += 1
                if not self.junk.get(c["name"], False):
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
                and c["name"] not in done
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

            lista, otros, fuera = [], 0, 0
            for c in pend:
                xf = c.get("xflag") or {}
                if setup is not None and xf.get("setup", setup) != setup:
                    otros += 1
                    continue
                croom = xf.get("room")
                if croom is not None and croom >= 0x20:
                    if sala_propia:
                        fuera += 1
                        continue
                    croom = None   # in the generic room they are all candidates
                # The room FILTERS, it does not just sort. This is what the
                # panel was for: GROTTOS is a single scene holding every grotto
                # in the game, so standing in one you got 440 pending checks
                # from all of them. Checks with no room of their own —chests,
                # NPCs, shops— are never dropped, only the ones that say they
                # belong somewhere else.
                if room is not None and room >= 0 and croom is not None and croom != room:
                    fuera += 1
                    continue
                lista.append({
                    "name": c["name"],
                    "item": self.item_de(c["game"], c["name"]),
                    "world": self.world_de(c["game"], c["name"]),
                    "type": c["type"],
                    "junk": self.junk.get(c["name"], False),
                    "room": croom,
                    "here": croom is not None and croom == room,
                    # the item shows regardless of the spoiler level once the
                    # streamer asked for it, or a level-3 hint named this check
                    "revealed": (f"{c['game']}:{c['name']}" in self.revealed
                                 or any(h["level"] >= 3 and h["check"] == c["name"] and h["game"] == c["game"]
                                        for h in self.hints.values())),
                })
            # What is in this very room first, then the ones with no room of
            # their own. Otherwise the chests and NPCs of the whole scene sit
            # above what is at your feet.
            lista.sort(key=lambda e: not e["here"])
            here["list"] = lista
            here["other_setup"] = otros
            here["other_room"] = fuera

        with self.lock:
            s = self.state
            s["ready"] = True
            s["error"] = None
            s["in_game"] = True
            s["game_mode"] = GAME_MODES.get(self._game_mode, "playing")
            s["active"] = active
            s["confidence"] = round(conf, 3)
            s["trusted"] = conf >= CONFIDENCE_MIN
            s["bases"] = {g: f"0x{b:08X}" for g, b in (self._bases or {}).items()}
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
            s["done_key_total"] = sum(1 for n in done if not self.junk.get(n, False))
            s["can_filter"] = self.hay_spoiler
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
                "rewards_done": {name: any(rc in done for rc in checks)
                                 for name, checks in GF_REWARD.items()},
                "clock_in_seed": CLOCK_STRAY_CHECK in self.check_game,
                "clock_have": CLOCK_STRAY_CHECK in done,
                "clock_reward_done": any(rc in done for rc in CLOCK_REWARD_CHECKS),
            }
            s["items"] = {g: item_grid(g, v, self.icons, self.user_icons,
                                       fairy_info if g == "mm" else None)
                          for g, v in items.items()}
            s["scalars"] = {g: item_scalars(g, v) for g, v in items.items()}
            # see FEED_RETRACT_SECONDS: what appeared moments ago and is not
            # done any more was a transient poll, not progress
            now = time.time()
            kept = [f for f in s["feed"]
                    if f["check"] in done or now - f["t"] > FEED_RETRACT_SECONDS]
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
            print(f"[overlay] the emulator has another ROM open: {abierta}; the tables are "
                  f"{de_tabla}'s. Restart the tracker to rebuild them.")
        self._rom_open = abierta

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

    def snapshot(self):
        with self.lock:
            return json.dumps(self.state)


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
# scalars that go to the figures row, not to the grid
SCALARS = {
    "oot": ["rupees", "hearts", "max hearts", "skulltulas", "deaths", "triforce"],
    "mm": ["heart pieces", "swamp skulltulas", "ocean skulltulas"],
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
        if key in snap:
            if key in SCALARS_OPTIONAL and not snap[key]:
                continue
            out.append({"label": key, "value": inventory.fmt(key, snap[key], game)})
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
    spoiler = ootmm.parse_spoiler(lineas)
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
    res.update(ok=True, casan=casan, version=ver,
               cubiertos=cubiertos, activos=len(activos))
    print(f"[overlay] spoiler loaded from the page: {res['n']} locations, "
          f"covers {cubiertos}/{len(activos)} checks, {res['total_key']} matter")
    return res


def serve(tracker, host, port, open_window=True):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    page = paths.res("overlay.html")

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
            if route not in ("/spoiler", "/hint", "/reveal"):
                self.send_error(404)
                return
            # Only from our own page. Any website you happen to have open can
            # POST to 127.0.0.1 —nothing stops it— and loading a spoiler turns
            # `spoiler=full` on, so a stranger's page could reveal what is left
            # on your stream. Browsers label their own requests and cannot be
            # made to lie about these two headers; curl and scripts send
            # neither, so driving it by hand still works.
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
                    got = tracker.hint(str(req.get("item", "")), req.get("level", 1))
                    self._json({"ok": got is not None, "hint": got,
                                "error": None if got else "no pending check holds that item"})
                elif route == "/reveal":
                    req = json.loads(raw.decode("utf-8"))
                    ok = tracker.reveal(str(req.get("game", "")), str(req.get("name", "")))
                    self._json({"ok": ok, "error": None if ok else "unknown check"})
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

    srv = ThreadingHTTPServer((host, port), Handler)
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
