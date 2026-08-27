#!/usr/bin/env python3
"""
guard_roms.py - what the tracker reads from a ROM, checked against the truth.

For every seed under the folders given (forge/out by default, but a folder of
real seeds works too), per ROM:

  locate      payload.locate() against the linker's symbol table (`oot.sym`,
              `mm.sym` next to the seed, from the build): gSharedCustomSave,
              the other game's save buffer, gSaveContext, Flash_ReadWrite,
              with sizes. This is the check that used to be a census by hand.
  names       kItemNames read from each payload, count against the symbol's
              size (one pointer per name).
  mkchecks    checks.json built from the ROM without aborting, in a sandbox:
              nothing touches the tracker's own checks.json.
  placement   each check's item against the spoiler log next to the ROM, and
              in a multiworld whose item it is. The names are compared the
              way the overlay vets a spoiler (`bare_item`) plus the spellings
              the two sides are known to differ on (a trap's cloak, `Bottle
              of X`, the shared/split variants): a genuinely different item
              still shows. A location the seed did not shuffle (a cow with
              cowsanity off) has no item and is not a mismatch.
  entrances   shuffled entrances read from the ROM against the spoiler's.

Without a symbol table the first two are skipped and said so. Exit code 1
when anything FAILs; WARNs are for what is known and worth a look.

    python forge/guard_roms.py [forge/out/v32.3 | some/folder | seed.z64 ...]
"""

import contextlib
import copy
import io
import json
import pathlib
import re
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
TRACKER = HERE.parent
sys.path.insert(0, str(TRACKER))

import mkchecks      # noqa: E402
import overlay       # noqa: E402
import paths         # noqa: E402
import payload       # noqa: E402
import placement     # noqa: E402
import rom           # noqa: E402


class Report:
    def __init__(self, title):
        self.title = title
        self.rows = []      # (check, status, detail)

    def add(self, check, status, detail=""):
        self.rows.append((check, status, detail))

    @property
    def fails(self):
        return [r for r in self.rows if r[1] == "FAIL"]

    @property
    def warns(self):
        return [r for r in self.rows if r[1] == "WARN"]

    def print(self):
        worst = "FAIL" if self.fails else ("WARN" if self.warns else "ok")
        print(f"{worst:4} {self.title}")
        for check, status, detail in self.rows:
            mark = {"ok": "  ", "WARN": "! ", "FAIL": "XX", "skip": "- "}[status]
            print(f"       {mark} {check:10} {detail}")


# --------------------------------------------------------------------------
# what the build left next to the seed: symbols and the version's own data
# --------------------------------------------------------------------------

def load_syms(path):
    """{name: (addr, size or None)} from `nm -n -S` output."""
    out = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 4:
            out[parts[3]] = (int(parts[0], 16), int(parts[1], 16))
        elif len(parts) == 3:
            out[parts[2]] = (int(parts[0], 16), None)
    return out


def check_locate(rep, rb, syms):
    try:
        loc = payload.locate(rb)
    except Exception as ex:     # a failure to locate is the finding itself
        rep.add("locate", "FAIL", f"payload.locate raised {type(ex).__name__}: {ex}")
        return None
    if syms is None:
        rep.add("locate", "skip", "no symbol table next to this seed")
        return loc
    bad = []
    for game, foreign in (("oot", "gMmSave"), ("mm", "gOotSave")):
        s = syms[game]
        got = loc.get(game) or {}
        want = {
            "custom": "gSharedCustomSave",
            "foreign": foreign,
            "own": "gSaveContext",
            "flash_readwrite": "Flash_ReadWrite",
        }
        for key, sym in want.items():
            mine = got.get(key)
            truth = s.get(sym)
            if mine is None:
                bad.append(f"{game} {key}: not located (nm: {sym} {truth[0]:#x})" if truth else f"{game} {key}: not located")
                continue
            if truth is None:
                bad.append(f"{game} {key}: {sym} is not in the symbol table")
                continue
            if mine[0] != truth[0]:
                bad.append(f"{game} {key}: tracker {mine[0]:#x}, nm {sym} {truth[0]:#x}")
            elif truth[1] and mine[1] and mine[1] != truth[1]:
                bad.append(f"{game} {key}: size tracker {mine[1]:#x}, nm {truth[1]:#x}")
    if bad:
        rep.add("locate", "FAIL", "; ".join(bad))
    else:
        c = loc["oot"]["custom"]
        rep.add("locate", "ok", f"8 addresses match nm; gSharedCustomSave {c[0]:#x} ({c[1]:#x} bytes)")
    return loc


def check_names(rep, rb, syms):
    parts = []
    status = "ok"
    for game in ("oot", "mm"):
        names = placement.find_item_names(rb, game)
        truth = syms[game].get("kItemNames") if syms else None
        n_truth = truth[1] // 4 if truth and truth[1] else None
        if names is None:
            parts.append(f"{game}: NOT FOUND" + (f" (nm says {n_truth} at {truth[0]:#x})" if truth else ""))
            # MM's table is not located on v30 and older: a known gap, not news
            if game == "mm" and n_truth and n_truth < 936:
                status = "WARN" if status == "ok" else status
            else:
                status = "FAIL"
        elif n_truth and len(names) != n_truth:
            parts.append(f"{game}: {len(names)} read, nm says {n_truth}")
            status = "FAIL"
        else:
            parts.append(f"{game}: {len(names)}")
    rep.add("names", status, "  ".join(parts))


# --------------------------------------------------------------------------
# mkchecks in a sandbox
# --------------------------------------------------------------------------

def run_mkchecks(rep, romp, spoiler, logp):
    """checks.json for this ROM, written under a temporary folder, or None."""
    with tempfile.TemporaryDirectory(prefix="forge-guard-") as tmp:
        tmp = pathlib.Path(tmp)
        old_out, old_user = mkchecks.OUT, paths.USER_DIR
        mkchecks.OUT = tmp
        paths.USER_DIR = str(tmp)
        # apply_payload_layout rewrites these module globals in place from the
        # ROM's payload; a ROM whose payload cannot be pinned would otherwise
        # inherit the previous ROM's addresses, so the verdict for one seed
        # would depend on which seed was guarded before it. Snapshot and
        # restore around every call (deepcopy: LAYOUT/ANCHOR_BASE are mutated,
        # not rebound).
        GLOBALS = ("CUSTOM_BASE", "CUSTOM_OOT", "CUSTOM_MM_OFF", "CUSTOM_MM",
                   "XFLAGS_COUNT", "LAYOUT", "ANCHOR_BASE")
        saved = {k: copy.deepcopy(getattr(mkchecks, k)) for k in GLOBALS}
        buf = io.StringIO()
        argv = ["--rom", str(romp)] + (["--spoiler", str(spoiler)] if spoiler else [])
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                try:
                    rc = mkchecks.main(argv)
                except SystemExit as ex:
                    rc = f"exit({ex.code})"
                except Exception as ex:
                    rc = f"{type(ex).__name__}: {ex}"
        finally:
            mkchecks.OUT, paths.USER_DIR = old_out, old_user
            for k, v in saved.items():
                setattr(mkchecks, k, v)
        logp.write_text(buf.getvalue(), encoding="utf-8")
        text = buf.getvalue()
        if rc not in (0, None) or not (tmp / "checks.json").exists():
            why = next((l.strip() for l in text.splitlines() if l.startswith("ABORTED") or "Error" in l), str(rc))
            rep.add("mkchecks", "FAIL", f"{why}  (log: {logp.name})")
            return None
        data = json.loads((tmp / "checks.json").read_text(encoding="utf-8"))
    n = len(data["checks"])
    pending = sum(1 for c in data["checks"] if c.get("addr") is None)
    pl = data.get("placement") or {}
    notes = []
    if data.get("same_version_as_data") is False:
        notes.append("data/ is another version's")
    if not data.get("payload"):
        notes.append("payload not located")
    rep.add("mkchecks", "WARN" if pending else "ok",
            f"{n} checks, {pending} without address, placement {pl.get('resolved', '?')} resolved"
            + (f"  [{'; '.join(notes)}]" if notes else ""))
    return data


# --------------------------------------------------------------------------
# the spoiler as the placement's truth
# --------------------------------------------------------------------------

def parse_spoiler(path):
    """{world: {(game, location): (item, owner)}}, the shuffled entrances
    {world: [(from, to)]} and the Master Quest dungeons the header names.
    Single-player spoilers are world 1 with every item its own."""
    worlds, entrances, mq = {}, {}, set()
    section = None
    world = 1
    # a multiworld spoiler opens each world with "  World N (984)" / "  World N"
    # inside the section; the lines under it carry no world of their own
    hdr = re.compile(r"^\s{1,3}World (\d+)\b")
    pat = re.compile(r"^\s{2,}(OOT|MM) (.+?): (?:Player (\d+) )?(.+?)\s*$")
    ent = re.compile(r"^\s{2,}(\S.*?)\s+->\s+(\S.*?)\s*$")
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line and not line[0].isspace():
            section = line.split("(")[0].strip()
            world = 1
            continue
        m = hdr.match(line)
        if m:
            world = int(m.group(1))
            continue
        if section == "Location List":
            m = pat.match(line)
            if m:
                owner = int(m.group(3) or world)
                worlds.setdefault(world, {})[(m.group(1).lower(), m.group(2))] = (m.group(4), owner)
        elif section == "Entrances":
            m = ent.match(line)
            if m:
                entrances.setdefault(world, []).append((m.group(1), m.group(2)))
        elif section == "World Flags" and line.strip().startswith("Master Quest Dungeons:"):
            val = line.split(":", 1)[1].strip()
            mq = set() if val in ("none", "") else {d.strip() for d in val.split(",")}
    return worlds, entrances, mq


# The spellings a spoiler and kItemNames are known to differ on. Only for
# seeds without the version's data; with it the comparison needs no guessing.
FUZZY_ALIASES = {
    "milk": "lon lon milk",
    "romani milk": "milk",
    "bottle of milk": "bottle of lon lon milk",
    "gold rupee": "huge rupee",
    "double defense": "defense upgrade",
    "bow": "hero's bow",
    "ocarina": "ocarina of time",
    "gerudo's membership card": "gerudo's card",
    "world map of ranch": "world map (romani ranch)",
}
PROGRESSIVE = {
    "strength": ("bracelet", "gauntlet"),
    "sword": ("sword", "knife"),
    "goron sword": ("knife", "biggoron", "sword"),
    "hookshot": ("hookshot", "longshot"),
    "scale": ("scale",),
    "wallet": ("wallet",),
    "ocarina": ("ocarina",),
    "bow": ("bow",),
}
# a "Shared X" (after "shared " is stripped) is whichever game's weapon the
# location holds; accept the ROM name if it carries any of these words
SHARED_FAMILIES = {
    "bow": ("bow",),
    "bomb bag": ("bomb bag",),
    "magic upgrade": ("magic",),
}


def _tokens(s):
    """Words that survive apostrophes, hyphens, parentheses and 'of': the
    spoiler's "World Map of Clock Town" is the ROM's "World Map (Clock Town)"."""
    s = s.replace("'", "").replace("-", " ").replace("(", " ").replace(")", " ")
    return [t.rstrip("s") for t in re.sub(r"[^a-z0-9 /]", "", s).split() if t != "of"]


def same_item(spoiler_name, rom_name):
    """Whether the spoiler's item and the one the tracker read from the ROM are
    the same thing. Both come from OoTMM's own naming, but by different paths:
    the spoiler tags the game (`(OoT)`/`(MM)`), spells a trap by its cloak
    (`Ice Trap (cloaked as ...)`), writes `Bottle of X` and the shared/split
    variants differently. None of those are placement errors, so they are
    normalised away; a genuinely different item still shows."""
    # a trap is the item; the cloak it wears is cosmetic, strip it first
    spoiler_name = re.sub(r"\s*\(cloaked as .*\)\s*$", "", spoiler_name)
    a, b = overlay.bare_item(spoiler_name), overlay.bare_item(rom_name)
    # bare_item only strips an exact "(OoT)"/"(OOT)"/"(MM)"; older spoilers
    # write "(Oot)", so drop any game tag case-insensitively too
    a = re.sub(r"\s*\((?:oot|mm)\)$", "", a)
    b = re.sub(r"\s*\((?:oot|mm)\)$", "", b)
    if a == b:
        return True
    # Souls (soul shuffle): the spoiler and the ROM name the same soul by
    # different NPCs, often a "/"-list ("Soul of Malon/Romani/Cremia" vs
    # "Soul of Romani/Cremia"). Same soul if the NPC token sets overlap.
    ms = re.match(r"^soul?d? of (?:the )?(.*)", a)
    mr = re.match(r"^soul?d? of (?:the )?(.*)", b)
    if ms and mr:
        ta = {t for part in ms.group(1).split("/") for t in _tokens(part)}
        tb = {t for part in mr.group(1).split("/") for t in _tokens(part)}
        return bool(ta & tb)
    # Silver rupees vs silver-rupee pouches are different items, but each is
    # named with a full dungeon in the spoiler and an abbreviation in the ROM;
    # the group is not comparable, so match on rupee-vs-pouch alone.
    if a.startswith("silver") and b.startswith("silver"):
        return ("pouch" in a) == ("pouch" in b)
    a = re.sub(r"^shared ", "", a)
    a = re.sub(r"^1 ", "", a)
    a = re.sub(r"^bottle of ", "", a)
    b = re.sub(r"^bottle of ", "", b)
    a = re.sub(r" refill$", "", a)
    b = re.sub(r" refill$", "", b)
    # a "Shared X" weapon is whichever game's version the location holds: the
    # shared bow is OoT's Fairy Bow or MM's Hero's Bow
    if a in SHARED_FAMILIES and any(w in b for w in SHARED_FAMILIES[a]):
        return True
    a = FUZZY_ALIASES.get(a, a)
    # "Hylian/Hero Shield": either
    if "/" in a and "(" not in a:
        head, _, tail = a.partition(" ")
        if any(_tokens(f"{alt} {tail}") == _tokens(b) for alt in head.split("/")):
            return True
    # "Small Key (Fire Temple)" vs "Small Key"; "Silver Rupee (Shadow Temple -
    # Scythe)" vs "Silver Rupee (Shadow Scythe)": the group is not compared
    base_a, paren_a = re.match(r"^([^(]*?)\s*(\(.*\))?$", a).groups()
    base_b, paren_b = re.match(r"^([^(]*?)\s*(\(.*\))?$", b).groups()
    if paren_a and base_a == "map":     # "Map (Deku Tree)" is the ROM's "Dungeon Map"
        base_a = "dungeon map"
    if paren_a and _tokens(base_a) == _tokens(base_b):
        return True
    if _tokens(a) == _tokens(b):
        return True
    if a.startswith("progressive "):
        fam = a[len("progressive "):]
        words = PROGRESSIVE.get(fam, (fam.split()[-1],))
        return any(w in b for w in words)
    return False


# Positional collectibles: the spoiler and the ROM both number them per scene,
# but from different orderings, so "Pot 3" on one side is not "Pot 3" on the
# other. Matching them by name is unsound, so a mismatch on one of these is
# reported (soft) but does not fail the seed. Bit-level matching would be
# needed to check them properly; that is a separate job.
_UNSTABLE = re.compile(
    r"\b(Pot|Grass|Rock|Crate|Barrel|Beehive|Snowball|Icicle|Flower|Pack|Ledge|"
    r"Cliffs|Platform|Wonder Item|Bean|Ruins Pillar|Web|Bombable)\b|"
    r"\bRupee\b(?! Room)", re.I)


def location_unstable(name):
    return bool(_UNSTABLE.search(name))


def item_unverifiable(spoiler_item, rom_item):
    """Items the spoiler and the ROM name by conventions too different to line
    up by string: souls (named by unrelated NPCs, e.g. 'Tourist Center Owner'
    vs 'Boat Cruise Man') and the ambiguous bare 'Ocarina'."""
    a = overlay.bare_item(spoiler_item)
    return "soul" in a or a in ("ocarina",)


def check_placement(rep, data, spoiler, world, logp):
    if spoiler is None:
        rep.add("placement", "skip", "no spoiler next to the ROM")
        return
    worlds, _, mq_dungeons = parse_spoiler(spoiler)
    if not worlds:
        rep.add("placement", "FAIL", f"{spoiler.name}: no Location List found")
        return
    truth = worlds.get(world)
    if truth is None:
        rep.add("placement", "FAIL", f"the spoiler has worlds {sorted(worlds)}, this ROM is world {world}")
        return
    # Vanilla and MQ rows of a dungeon share their keys, so both resolve to an
    # item; only the layout the seed has is real. mkchecks maps the spoiler's
    # MQ list to scenes only when it is empty (else mq_scenes stays null).
    mq_scenes = data.get("mq_scenes")
    notes = []
    # the tracker's bundled data/ is one version's (v32.0); on a seed from
    # another version it reads placement and names from the ROM but the pool
    # CSVs and gi.yml no longer line up, so some items come out wrong. mkchecks
    # says so (same_version_as_data False); those mismatches are that documented
    # consequence, not a regression, so they warn rather than fail.
    disclaimed = data.get("same_version_as_data") is False
    if mq_scenes is None and mq_dungeons:
        notes.append(f"MQ seed ({len(mq_dungeons)} dungeons): mkchecks cannot map them, MQ rows ignored")
    mq_scenes = set(mq_scenes or [])
    # every location the tracker knows, and the item it resolved for the ones
    # in this seed's layout. A check with no item is one the seed did not
    # shuffle (a cow on a no-cowsanity seed): the tracker is right to leave it.
    all_names = {(c["game"], c["name"]) for c in data["checks"]}
    resolved = {}
    for c in data["checks"]:
        if c.get("item") is None or bool(c.get("mq")) != (c.get("scene") in mq_scenes):
            continue
        resolved[(c["game"], c["name"])] = (c["item"], c.get("player") or world, c.get("type"))

    comparable = agree = owner_wrong = owner_wrong_p1 = 0
    # a "hard" disagreement is on a stable-named check and gates FAIL; a "soft"
    # one is where the spoiler and the ROM name the spot or the item by
    # conventions that diverge and cannot be lined up (see location_unstable /
    # item_unverifiable), so it is reported but does not fail the seed
    hard_item, soft_item, wrong_owner = [], [], []
    not_shuffled = unknown_loc_hard = unknown_loc_soft = 0
    for key, (item, owner) in truth.items():
        got = resolved.get(key)
        if got is None:
            if key in all_names:
                not_shuffled += 1        # the tracker has it, unshuffled: fine
            elif location_unstable(key[1]):
                unknown_loc_soft += 1    # a pot/grass/rock numbered differently
                soft_item.append(f"unknown location (positional): {key[0].upper()} {key[1]}: spoiler '{item}'")
            else:
                unknown_loc_hard += 1    # the tracker never heard of it
                hard_item.append(f"unknown location: {key[0].upper()} {key[1]}: spoiler '{item}'")
            continue
        comparable += 1
        if same_item(item, got[0]):
            agree += 1
        elif location_unstable(key[1]) or item_unverifiable(item, got[0]):
            soft_item.append(f"{key[0].upper()} {key[1]}: spoiler '{item}' / rom '{got[0]}'")
        else:
            hard_item.append(f"{key[0].upper()} {key[1]}: spoiler '{item}' / rom '{got[0]}'")
        if owner != got[1]:
            owner_wrong += 1
            # checks.json marks an item as someone else's only when it is not
            # Player 1's, so a ROM of another world reads every owner as its own
            owner_wrong_p1 += owner == 1 and got[1] == world
            wrong_owner.append(f"{key[0].upper()} {key[1]}: spoiler P{owner} / rom P{got[1]} ('{item}')")
    rom_only = [k for k in resolved if k not in truth]
    rom_only_hard = [k for k in rom_only if not location_unstable(k[1])]
    known_p2_bug = world != 1 and owner_wrong and owner_wrong_p1 == owner_wrong

    with logp.open("a", encoding="utf-8") as fh:
        fh.write(f"\n== placement vs spoiler (world {world}): {agree}/{comparable} items agree, "
                 f"{owner_wrong} wrong owner, {not_shuffled} not shuffled, "
                 f"{unknown_loc_hard + unknown_loc_soft} unknown to tracker, {len(rom_only)} rom-only\n")
        for d in hard_item:
            fh.write(f"   HARD {d}\n")
        for d in wrong_owner:
            fh.write(f"   {d}\n")
        for d in soft_item:
            fh.write(f"   soft {d}\n")
        for k in rom_only:
            fh.write(f"   rom-only{' (positional)' if location_unstable(k[1]) else ''}: {k[0].upper()} {k[1]}: '{resolved[k][0]}'\n")

    # The one provable-wrong signal is a location that lines up by name yet
    # holds a different item, or (in a multiworld) a wrong owner. A location
    # the spoiler names and the tracker does not, or vice versa, is a name
    # divergence between two naming systems, not a placement error -- it is
    # surfaced (counts, and the log) but does not fail the seed.
    bad_items = len(hard_item)
    if soft_item or len(rom_only) != len(rom_only_hard):
        notes.append(f"{len(soft_item)} soft name-mismatches (positional collectibles / souls named "
                     "differently by the spoiler and the ROM), not placement errors")
    if unknown_loc_hard or len(rom_only_hard):
        notes.append(f"{unknown_loc_hard} spoiler locations unknown to the tracker, "
                     f"{len(rom_only_hard)} tracker locations not in the spoiler (name divergence; read the log)")
    detail = (f"{agree}/{comparable} items agree, owner wrong {owner_wrong}, "
              f"not-shuffled {not_shuffled}, unknown-loc {unknown_loc_hard}(+{unknown_loc_soft} positional), "
              f"rom-only {len(rom_only_hard)}(+{len(rom_only) - len(rom_only_hard)} positional)")
    if disclaimed and (bad_items or owner_wrong):
        notes.append(f"{bad_items} item / {owner_wrong} owner mismatches, but the tracker flagged this "
                     "as another version than its data/ (names/pool shifted) -- expected, not a regression")
    if known_p2_bug:
        notes.append(f"all {owner_wrong} wrong owners are Player 1's items read as world {world}'s: "
                     "the tracker assumes the ROM is Player 1 (known multiworld limit)")
    if notes:
        detail += "  [" + "; ".join(notes) + "]"

    real_fail = not disclaimed and (bad_items or (owner_wrong and not known_p2_bug))
    if not comparable:
        rep.add("placement", "FAIL", "nothing comparable: the spoiler's locations match no resolved check")
    elif real_fail:
        rep.add("placement", "FAIL", detail)
    elif known_p2_bug or soft_item or unknown_loc_hard or len(rom_only) or notes:
        rep.add("placement", "WARN", detail)
    else:
        rep.add("placement", "ok", detail)


def check_entrances(rep, data, spoiler, world):
    if spoiler is None:
        return
    _, ents, _ = parse_spoiler(spoiler)
    want = len(ents.get(world, []))
    got = len(data.get("entrances") or [])
    if want and not got:
        rep.add("entrances", "FAIL", f"spoiler lists {want} shuffled entrances, the ROM read 0")
    elif want or got:
        status = "ok" if got == want else "WARN"
        rep.add("entrances", status, f"rom {got}, spoiler {want}")


# --------------------------------------------------------------------------
# walking the folders
# --------------------------------------------------------------------------

def find_seeds(targets):
    """[(rom path, spoiler or None, folder with the build's files or None)]."""
    seeds = []
    roms = []
    for t in targets:
        t = pathlib.Path(t).resolve()
        if t.is_file():
            roms.append(t)
        elif t.is_dir():
            roms.extend(p for p in t.rglob("*.z64") if rom.is_ootmm_file(p))
        else:
            print(f"no such path: {t}")
    for romp in sorted(set(roms)):
        m = re.match(r"OoTMM-([A-Za-z0-9]+)", romp.stem)
        spoiler = None
        if m:
            cands = sorted(romp.parent.glob(f"OoTMM-Spoiler-{m.group(1)}*.txt"))
            spoiler = cands[0] if cands else None
        if spoiler is None:
            cands = sorted(romp.parent.glob("OoTMM-Spoiler-*.txt"))
            spoiler = cands[0] if len(cands) == 1 else None
        symdir = next((d for d in (romp.parent, romp.parent.parent) if (d / "oot.sym").exists() and (d / "mm.sym").exists()), None)
        seeds.append((romp, spoiler, symdir))
    return seeds


def world_of(romp):
    m = re.search(r"-Player(\d+)\b", romp.stem)
    return int(m.group(1)) if m else 1


def main(targets):
    seeds = find_seeds(targets)
    if not seeds:
        print("no OoTMM seeds found")
        return 1
    reports = []
    for romp, spoiler, symdir in seeds:
        rel = romp.relative_to(HERE / "out") if romp.is_relative_to(HERE / "out") else romp
        rep = Report(str(rel))
        rb = romp.read_bytes()
        syms = {g: load_syms(symdir / f"{g}.sym") for g in ("oot", "mm")} if symdir else None
        check_locate(rep, rb, syms)
        if syms:
            check_names(rep, rb, syms)
        else:
            rep.add("names", "skip", "no symbol table")
        # the log sits next to a seed forge built; anything else is somebody's
        # folder and gets nothing written into it
        if romp.is_relative_to(HERE / "out"):
            logp = romp.parent / f"guard-{romp.stem}.log"
        else:
            (HERE / "out" / "_guard").mkdir(parents=True, exist_ok=True)
            logp = HERE / "out" / "_guard" / f"{romp.stem}.log"
        data = run_mkchecks(rep, romp, spoiler, logp)
        if data:
            world = world_of(romp)
            check_placement(rep, data, spoiler, world, logp)
            check_entrances(rep, data, spoiler, world)
        rep.print()
        reports.append(rep)
    n_fail = sum(1 for r in reports if r.fails)
    n_warn = sum(1 for r in reports if r.warns and not r.fails)
    print(f"\n{len(reports)} seeds: {len(reports) - n_fail - n_warn} ok, {n_warn} with warnings, {n_fail} failing")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or [str(HERE / "out")]))
