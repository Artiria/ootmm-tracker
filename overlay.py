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

# Same idea for the custom save window: it is one read plus a few hundred local
# scorings, so it must not run on every poll while a run has no progress yet.
CUSTOM_RESCAN_SECONDS = 10.0

# How many scene checks have to be done before "and not one xflag" counts as
# evidence that the custom save base is wrong. Low, because the two kinds of
# check are spread all over the game and doing several of one and none at all
# of the other does not happen by chance -- but not 1, so a single chest on a
# fresh file cannot trip it.
SCENE_CHECKS_SUSPICIOUS = 3

# Each panel is also served on its own at /p/<name>, so the streamer captures
# only the ones they want to show, wherever they want them. The names have to
# match the data-panel attributes in overlay.html.
PANELS = ["summary", "regions", "items", "activity", "remaining"]

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
    # `Heart Container` is NOT here: that is a whole heart. Neither is
    # `Recovery Heart`, which is already filler two patterns up.
    r"^Piece of Heart" + JUEGO + r"$",
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
        self._rebuild_items()
        self.lock = threading.Lock()
        self.state = {
            "ready": False,
            "error": None,
            "active": None,
            "trusted": True,
            "confidence": 1.0,
            "done_total": 0,
            "total": sum(n for _, _, n, _k in self.regions),
            "total_key": sum(k for _, _, _n, k in self.regions),
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
        }
        self._done = set()
        self._seeded = False
        self._bases = None
        self._play = {}          # game -> PlayState address, cached like the bases
        self._play_retry = {}    # game -> when the next full scan is allowed
        self._last_scene = {}    # game -> last (scene, room) the PlayState gave
        # inactive game -> measured distance from its buffer to the custom save.
        # Keyed by the INACTIVE game on purpose: the block hangs off whichever
        # one is not running, so a distance measured before crossing does not
        # carry over after it.
        self._custom_gap = {}
        self._custom_retry = 0
        self._custom_active = None
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

    def is_active(self, c):
        """Whether this row is the one that exists in this seed (vanilla or MQ)."""
        if c.get("mq"):
            return c["scene"] in self.mq_scenes
        return c["scene"] not in self.mq_scenes

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

        bases = self.locate(self.link, verbose=False)
        self._bases = bases or None
        return bases

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
                return got
        self._play.pop(game, None)

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
                    return addr
            self._custom_gap.pop(ajeno, None)

        medida = self.find_custom(bases, active)
        if medida is not None:
            return medida
        # Nothing validated -- a fresh file with no progress anywhere looks like
        # this too. Fall back to the constant so the anchor is not left unset.
        cands = custom_candidates(bases, active)
        return cands[0] if cands else None

    def poll_once(self):
        import inventory

        bases = self.locate_cached()
        # the active game is the one whose save sits low in RDRAM
        active = min(bases, key=lambda g: bases[g]) if bases else None

        mejor = self.custom_base(bases, active)
        anchors = rebase(self.table, bases, active, mejor)

        done = set()
        conf, bits = 1.0, 0
        por_ancla = {}
        for anchor, p in self.plan.items():
            if anchor not in anchors:
                continue
            blob = self.link.read_block(anchors[anchor], p["span"])
            if anchor == "custom":
                conf, bits = confidence(blob, p["checks"], self.xflag_ranges)
            por_ancla[anchor] = read_flags(blob, p["checks"])

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
        if bits == 0 and escena > 0 and (antes or arranque):
            conf = 0.0
        elif escena == 0 and bits == 0:
            self._xflag_peak = 0      # partida nueva: se olvida lo visto
        self._xflag_peak = max(self._xflag_peak, bits)

        for anchor, hechos in por_ancla.items():
            if anchor == "custom" and conf < CONFIDENCE_MIN:
                continue
            done |= hechos

        items = {}
        if "oot" in bases:
            items["oot"] = inventory.snapshot(self.link.read_block(bases["oot"], 0x1500))
        if "mm" in bases:
            items["mm"] = inventory.mm_snapshot(self.link.read_block(bases["mm"], 0x1500))

        # Where the player is. The PlayState is the live answer; the save
        # context is the fallback, and it lags (see SCENE_OFF).
        scene, room, live = None, None, False
        if active is not None:
            got = self.play_cached(active)
            if got is not None:
                scene, room = got
                live = True
                self._last_scene[active] = got

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

        active, done, conf, items, scene, room, live, setup_raw = self.poll_once()

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

        regions = []
        for rgame, rscene, total, total_key in self.regions:
            got = by_scene.get((rgame, rscene), 0)
            if got:
                regions.append({
                    "game": rgame, "scene": rscene, "done": got, "total": total,
                    "done_key": by_scene_key.get((rgame, rscene), 0),
                    "total_key": total_key,
                })
        regions.sort(key=lambda r: (-r["done"] / r["total"], -r["total"]))
        # How many regions exist at all, so the panel can say how many it is
        # NOT showing. It only lists the ones you have touched, and without
        # saying so the totals read as if locations were missing: five regions
        # adding up to 29 key checks next to a headline of 670.
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
                    "type": c["type"],
                    "junk": self.junk.get(c["name"], False),
                    "room": croom,
                    "here": croom is not None and croom == room,
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
            s["version"] = __version__
            s["active"] = active
            s["confidence"] = round(conf, 3)
            s["trusted"] = conf >= CONFIDENCE_MIN
            s["bases"] = {g: f"0x{b:08X}" for g, b in (self._bases or {}).items()}
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
            s["items"] = {g: item_grid(g, v, self.icons, self.user_icons)
                          for g, v in items.items()}
            s["scalars"] = {g: item_scalars(g, v) for g, v in items.items()}
            s["feed"] = (feed + s["feed"])[:FEED_MAX]
            s["pending_here"] = here
            s["uptime"] = int(time.time() - self._started)

    def run(self, interval=POLL_SECONDS):
        while True:
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

# MM's stray fairies, fifteen per dungeon.
FAIRY_DUNGEONS = [(0, "Woodfall"), (1, "Snowhead"), (2, "Great Bay"), (3, "Stone Tower")]
FAIRIES_PER_DUNGEON = 15

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
        "Keys": 5, "Bosses": 6, "Fairies": 4}
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
# The six warp-song colours are the game's own, not invented: forest green,
# fire red, water blue, spirit orange, shadow purple, light yellow. The other
# songs the game paints white, and there what tells them apart is the glyph: a
# bolt for storms, a horseshoe for Epona, a sun for the Sun's Song...
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
    # OoT, the rest: the glyph is the data
    "quest:Zelda's Lullaby": ("triforce", "#e6d44f"),
    "quest:Epona's Song": ("horseshoe", None),
    "quest:Saria's Song": ("leaf", None),
    "quest:Sun's Song": ("sun", None),
    "quest:Song of Time": ("hourglass", None),
    "quest:Song of Storms": ("bolt", None),
    # MM
    "quest:Song of Healing": ("heart", None),
    "quest:Song of Soaring": ("soar", None),
    "quest:New Wave Bossa Nova": ("wave", None),
    "quest:Elegy of Emptiness": ("note", None),
    "quest:Oath to Order": ("note", None),
    "quest:Goron Lullaby": ("note", None),
    "quest:Goron Lullaby (half)": ("note", None),
    "quest:Song of Awakening": ("note", None),
    "quest:Bombers' Notebook": ("book", None),
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
    "oot": ["rupees", "hearts", "max hearts", "skulltulas", "deaths"],
    "mm": ["heart pieces", "swamp skulltulas", "ocean skulltulas"],
}
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


def item_grid(game, snap, icons=None, user_icons=None):
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
        groups["Fairies"] = []
        for idx, nombre in FAIRY_DUNGEONS:
            n = snap.get(f"fairy:{idx}")
            n = n if isinstance(n, int) and 0 <= n <= FAIRIES_PER_DUNGEON else 0
            groups["Fairies"].append({
                "label": f"{nombre} fairies", "on": n > 0, "icon": None,
                "glyph": "fairy", "color": "#ff9ee0",
                "mask": None,
                "badge": str(n) if n else "", "value": f"{n}/{FAIRIES_PER_DUNGEON}",
            })

    orden_prog = MM_PROGRESO_ORDER if game == "mm" else OOT_PROGRESO_ORDER
    if groups.get("Progress"):
        pos = {n: i for i, n in enumerate(orden_prog)}
        groups["Progress"].sort(key=lambda it: pos.get(it["label"], len(pos)))

    order = ["Items", "Masks", "Equipment", "Upgrades", "Keys", "Bosses", "Fairies", "Progress"]
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
            if self.path.split("?", 1)[0] != "/spoiler":
                self.send_error(404)
                return
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 16 * 1024 * 1024:
                self._json({"ok": False, "error": "file empty or too large"})
                return
            try:
                self._json(cargar_spoiler(tracker, self.rfile.read(n)))
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
