#!/usr/bin/env python3
r"""dataupdate.py - a newer OoTMM data/ than the one that ships, chosen by
measuring it against your ROM.

WHAT THIS IS FOR. Everything the tracker needs to *find* a check comes out of
the ROM: the addresses, the xflag tables, the placement, the bits. One thing
does not, and cannot -- what each location is CALLED. The names are the
randomizer's own labels and they live in OoTMM's repository, so the copy in
`data/` is pinned to whatever version it was taken from (v32.0), and it goes
stale on its own schedule.

Stale was thought to be harmless until 1 sep 2026. Measured between v32.0 and
v32.3: eighteen renames, all in Great Bay Coast, no id moved -- and two of
them are the bad kind. `Great Bay Coast Pot Ledge 1` still EXISTS in v32.3,
pointing at a different pot (id 0x0076 where it used to be 0x003d). A name
that disappears is visible; a name that survives and moves is a tracker
quietly telling you about the wrong pot, and there is no way to notice.

HOW IT DECIDES. Never by version number, because the ROM does not carry one
(measured on four: no "v32", no "OoTMM v...", no "dev-"). It decides the only
way this project decides anything: by measuring against the ROM in front of
it. A candidate data/ is downloaded, a checks.json is built with it IN A
SANDBOX, and the two builds are compared on what the ROM can arbitrate:

  * `same_version_as_data` -- do the ROM's own item names (kItemNames) agree
    with the candidate's gi.yml;
  * how many of the ROM's keys the pool can NAME, as opposed to falling back
    to a synthetic "Scene · room · actor";
  * that nothing regresses: same number of checks, same number resolved.

Only a candidate that wins one of those and loses none is adopted. Anything
else is reported and left alone -- which is the answer for a v32.0 seed
offered v32.3 data, where the newer names would be the wrong ones.

WHAT IT NEVER DOES. It does not touch the bundled `data/`: a download lives
on its own under `data-updates/<tag>/` and one line in `active.json` says
which is in force, so going back is deleting a file. It does not follow
`master`, only tags, because master's names are still moving. And it is the
only part of the tracker that goes near the network, on the one command that
asks for it.

Both layouts are understood, because the pool changed shape between them:
`data/pool/*.csv` up to v32.x, and `data/checks/**/*.xml` from gen 943 on
(mkchecks.load_rows reads either). The file list comes from the tag's own
tree, so neither is guessed at.

    python ootmm.py data status
    python ootmm.py data update --rom "...\OoTMM-abc.z64"
    python ootmm.py data update --tag v32.3 --rom "..." --dry-run
    python ootmm.py data revert
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import paths

REPO = "OoTMM/OoTMM"
API = f"https://api.github.com/repos/{REPO}"
RAW = "https://raw.githubusercontent.com/" + REPO + "/{ref}/{path}"
UA = {"User-Agent": "ootmm-tracker-dataupdate"}
TIMEOUT = 30

# What a data/ has to bring for mkchecks to read it. `world/` is the
# randomizer's logic and no concern of ours; `ref/` is documentation.
WANTED = ("data/pool/", "data/defs/", "data/checks/")
WANTED_EXT = (".csv", ".yml", ".xml")

# A name mkchecks made up because no pool row claimed the key. The separator
# is deliberate and no real row uses it (mkchecks.synthetic_name).
SYNTHETIC = " · "


def _get(url, binary=False):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = r.read()
    return data if binary else data.decode("utf-8")


def tags(limit=12):
    """The newest release tags, newest first. Never master: see the header."""
    out = json.loads(_get(f"{API}/tags?per_page={limit}"))
    return [t["name"] for t in out]


def latest_tag():
    for t in tags():
        # v32.3, v33.0... anything else (a release candidate, a one-off) is
        # not what "the latest version" means to a player.
        if re.fullmatch(r"v\d+(\.\d+)*", t):
            return t
    return None


def commit_of(tag):
    """The commit a tag points at, once.

    A tag is a name and a name can be moved. The tree is asked for at one
    moment and the files are fetched at another, so following the NAME both
    times can mix two different versions into one folder and call it that tag.
    Resolving it once and using the sha from then on means a download is of
    one state of the repository or of nothing. (Round 1 of the audit,
    proposal 1.)
    """
    o = json.loads(_get(f"{API}/git/ref/tags/{tag}"))["object"]
    # An annotated tag points at a tag object, and a tag object may point at
    # another one. Peeling exactly once returned a tag sha and called it a
    # commit, and the tree request then failed with nothing useful to say.
    # Peel until it is a commit, with a stop so a cycle cannot spin forever.
    # (Round 2 of the audit, 1 sep 2026.)
    for _ in range(10):
        if o["type"] == "commit":
            return o["sha"]
        if o["type"] != "tag":
            raise RuntimeError(f"{tag} points at a {o['type']}, not a commit")
        o = json.loads(_get(f"{API}/git/tags/{o['sha']}"))["object"]
    raise RuntimeError(f"{tag} is tags all the way down; giving up")


def files_of(ref):
    """Every data file of that tag, from the tag's own tree.

    Asking the tree instead of guessing paths is what makes this work across
    the layout change: v32.x answers with `data/pool/*.csv`, gen 943 and later
    with `data/checks/**/*.xml`, and neither is written down here.
    """
    t = json.loads(_get(f"{API}/git/trees/{ref}?recursive=1"))
    if t.get("truncated"):
        raise RuntimeError("the tag's tree came back truncated; refusing to"
                           " download half a data/")
    return sorted(x["path"] for x in t["tree"]
                  if x["type"] == "blob"
                  and x["path"].startswith(WANTED)
                  and x["path"].endswith(WANTED_EXT))


def download(tag, dest=None, say=print):
    """Fetch one tag's data/ into `data-updates/<tag>/`, atomically.

    Written aside and moved into place at the end: a folder that is there is
    a folder that finished, so nothing ever adopts half a download.
    """
    dest = Path(dest or paths.updates_dir(tag))
    # One state of the repository, named by its sha, for the listing AND for
    # every file. See commit_of.
    sha = commit_of(tag)
    lista = files_of(sha)
    if not lista:
        raise RuntimeError(f"{tag} has no data/ files; is it a real tag?")
    # A staging folder OF THIS RUN, with a name nobody else can guess, beside
    # the destination so the final rename stays on one filesystem.
    #
    # It used to be `<dest>.part`, one name shared by every invocation, and
    # two updates of the same tag at once could hand each other half a tree:
    # the second wipes the first's staging, fails midway, and the first
    # renames what is left into service -- over the good copy. Nothing here
    # locks anything now because nothing here is shared. (Round 2 of the
    # audit, 1 sep 2026.)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{dest.name}.part-", dir=str(dest.parent)))
    say(f"{tag} ({sha[:10]}): {len(lista)} files")
    try:
        return _fetch_and_swap(tag, sha, lista, tmp, dest, say)
    finally:
        # Whatever happened, this run's staging folder does not outlive it.
        # A failure used to leave one behind, and a folder full of half a
        # data/ sitting next to the good ones is the kind of thing somebody
        # eventually points a tool at.
        shutil.rmtree(tmp, ignore_errors=True)


def _fetch_and_swap(tag, sha, lista, tmp, dest, say):
    for i, path in enumerate(lista, 1):
        rel = path[len("data/"):]
        f = tmp / rel
        # A name from somebody else's repository decides a path on this disk.
        # GitHub does not serve `..` in a tree, but "does not today" is not a
        # reason to write outside the folder if it ever did.
        if os.path.commonpath([tmp.resolve(), f.resolve()]) != str(tmp.resolve()):
            raise RuntimeError(f"{path!r} would be written outside {tmp}")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(_get(RAW.format(ref=sha, path=path), binary=True))
        if i % 25 == 0 or i == len(lista):
            say(f"  {i}/{len(lista)}")
    # Look at what was staged before putting it in service. A tree that is
    # not a data/ never becomes one, whatever went wrong upstream.
    if not paths._looks_like_data(str(tmp)):
        raise RuntimeError(f"what was downloaded for {tag} is not an OoTMM data/"
                           " (no pool_oot.csv and no checks/); nothing was replaced")

    # Put the old one ASIDE, not in the bin, until the new one is in place.
    # Deleting first and renaming after leaves nothing at all if the rename
    # fails -- and then an `active.json` still naming this tag falls back to
    # the bundled data without a word. (Round 1 of the audit, 1 sep 2026.)
    #
    # And the old one goes somewhere that is NOT a tag name. It used to go to
    # `<dest>.old`, which is a perfectly good directory name for a tag called
    # `X.old`, so refetching `X` deleted the download of `X.old`. Tag names
    # are somebody else's namespace; ours has a dot in front. (Round 2.)
    viejo = None
    if dest.exists():
        viejo = Path(tempfile.mkdtemp(prefix=f".{dest.name}.old-", dir=str(dest.parent)))
        viejo.rmdir()              # mkdtemp made it; rename needs the name free
        dest.rename(viejo)
    try:
        tmp.rename(dest)
    except OSError:
        if viejo is not None:
            try:
                viejo.rename(dest)          # back exactly as it was
            except OSError as ex2:
                # Both renames failed, so the previous data/ is sitting under
                # a name nobody would guess. Losing it quietly is the one
                # outcome that must not happen: say where it is.
                raise RuntimeError(
                    f"could not put {tag} in place AND could not put the previous"
                    f" one back ({ex2}).\nThe previous download is intact at:\n"
                    f"    {viejo}\nRename that folder to {dest} by hand.") from ex2
        raise
    if viejo is not None:
        shutil.rmtree(viejo, ignore_errors=True)
    return dest


# --------------------------------------------------------------------------
# The measure
# --------------------------------------------------------------------------


def names_score(rom_bytes, data_dir):
    """(matching, comparable): how much of THIS ROM's item names that data/
    reproduces exactly.

    Measured straight, in process, against the file: the ROM's kItemNames are
    the truth and a gi.yml either says the same words or it does not. This is
    the sharp instrument. `same_version_as_data`, which the build also
    reports, is the same comparison rounded to a yes at 90% -- right for
    "can the symbolic ids be trusted", useless for choosing between two
    releases of the same series, where the whole drift is a couple of per
    cent and both sides answer yes.
    """
    import placement

    gi_path = (Path(data_dir) / "defs" / "gi.yml") if data_dir else None
    if gi_path is not None and not gi_path.is_file():
        gi_path = Path(data_dir) / "gi.yml"
    gi = placement.load_gi(str(gi_path) if gi_path else None)
    por_indice, _ = placement.names_from_rom(rom_bytes, gi, verbose=False)
    if not por_indice:
        return 0, 0
    return placement.names_agreement(por_indice, gi)


def named_rom_keys(rom_bytes, checks):
    """How many of the keys THE ROM PLACED this data can put a name to.

    Not "how many rows came out named", which is what this counted at first.
    The pool is a superset of what a seed places, so counting output rows
    rewards a data/ for carrying MORE rows regardless of whether the ROM has
    them: appending a single invented row to pool_mm.csv was enough to make a
    candidate win by "names 1 more of the ROM's keys", when the ROM had never
    heard of it. (Round 1 of the audit, 1 sep 2026.)

    The ROM's placement table is the census. A row counts only if its
    (game, override key) is in it, and only if the pool named it rather than
    falling back to a synthetic name.

    And DISTINCT keys, not rows. A Master Quest dungeon puts two rows on one
    key by design -- vanilla and its twin -- so counting rows would let a
    data/ win by carrying more rows for keys that were already named, which
    is the same defect one level down.
    """
    import placement

    claves = set(placement.read_tables(rom_bytes))
    return len({(c.get("game"), c.get("ovkey")) for c in checks
                if (c.get("game"), c.get("ovkey")) in claves
                and SYNTHETIC not in (c.get("name") or "")})


def build_with(data_dir, rom, say=print):
    """Build a checks.json with that data/, in a sandbox, and measure it.

    The sandbox is the point: mkchecks writes to paths.USER_DIR, which from
    source is the checkout, so measuring a candidate used to mean overwriting
    the tables of whatever seed was being played. OOTMM_TRACKER_HOME exists
    because that actually happened (1 sep 2026).
    """
    home = Path(tempfile.mkdtemp(prefix="ootmm-data-measure-"))
    env = dict(os.environ, OOTMM_TRACKER_HOME=str(home))
    # `None` means "what is in force", and it has to be NAMED, not left to the
    # child to work out. The sandbox moves USER_DIR, and `data-updates/` --
    # stamp included -- hangs off USER_DIR, so a child with a fresh home sees
    # no adopted tag and quietly measures the BUNDLED data instead. On a first
    # update the two are the same folder and it looks right; on the second one
    # the baseline is not what the tracker is actually using. (Round 1 of the
    # audit, 1 sep 2026.)
    resuelto = str(data_dir if data_dir is not None else paths.data_dir())
    env[paths.DATA_ENV] = resuelto
    here = Path(__file__).resolve().parent
    r = subprocess.run([sys.executable, str(here / "mkchecks.py"), "--rom", str(rom)],
                       cwd=str(here), env=env, capture_output=True, text=True)
    out = home / "checks.json"
    if r.returncode != 0 or not out.is_file():
        say((r.stdout or "")[-800:])
        say((r.stderr or "")[-800:])
        raise RuntimeError("mkchecks could not build with that data/")
    d = json.loads(out.read_text(encoding="utf-8"))
    checks = d.get("checks") or []
    rom_bytes = Path(rom).read_bytes()
    en_la_rom = named_rom_keys(rom_bytes, checks)
    casan, comparables = names_score(rom_bytes, resuelto)
    m = {
        "total": len(checks),
        "resolved": sum(1 for c in checks if c.get("addr") is not None),
        "named": en_la_rom,
        "item_names": f"{casan}/{comparables}",
        "_names": (casan, comparables),
        # Null when the placement could not be read at all. mkchecks writes
        # checks.json anyway in that case, on purpose, and the result looks
        # almost identical from out here -- same totals, same names. The first
        # download ever measured hit exactly that (a gi.yml it could not find)
        # and would have been judged on its merits rather than thrown out.
        "placement": d.get("placement") is not None,
        "same_version_as_data": bool(d.get("same_version_as_data")),
        # Which data/ the child ACTUALLY used, in its own words. Printed so
        # the baseline column can be checked against what is in force: the
        # sandbox moves USER_DIR, the adopted-tag stamp lives under it, and a
        # child left to work it out for itself measured the bundled copy
        # while the table said "in force". (Round 1 of the audit.)
        "version": d.get("version"),
        "source": d.get("source"),
    }
    shutil.rmtree(home, ignore_errors=True)
    return m


def verdict(now, cand):
    """Adopt the candidate? The ROM arbitrates; this only reads the scores.

    Better on something the ROM can judge, worse on nothing. A newer data/
    that names fewer of the ROM's keys, or whose item names stop agreeing
    with the ROM's, is a newer data/ for somebody else's seed.
    """
    razones, contra = [], []
    if not cand["placement"]:
        # Not a score at all: the candidate could not be read. Said first and
        # on its own, because every other number is meaningless then.
        return False, [], ["its placement could not be read at all"
                           " -- that data/ is not usable, whatever the rest says"]
    a, b = cand["_names"], now["_names"]
    if not a[1] or not b[1]:
        # Nothing comparable on one side: a gi.yml with no names in common
        # with the ROM, or a ROM whose kItemNames could not be read. Cross
        # multiplication makes 0/0 tie with everything, so without this a
        # candidate that lost every item name looked merely "equal" here and
        # could still be adopted on some other metric. No measurement is not
        # a draw; it is a reason not to move. (Round 1 of the audit.)
        contra.append(f"its item names cannot be compared with this ROM's"
                      f" ({cand['item_names']} against {now['item_names']})")
    elif a[0] * b[1] > b[0] * a[1]:    # a/b compared without dividing by zero
        razones.append(f"says {cand['item_names']} of the ROM's item names,"
                       f" against {now['item_names']}")
    elif a[0] * b[1] < b[0] * a[1]:
        contra.append(f"says only {cand['item_names']} of the ROM's item names,"
                      f" against {now['item_names']}")
    if cand["named"] > now["named"]:
        razones.append(f"names {cand['named'] - now['named']} more of the ROM's keys")
    if cand["named"] < now["named"]:
        contra.append(f"names {now['named'] - cand['named']} fewer of the ROM's keys")
    for k in ("total", "resolved"):
        if cand[k] < now[k]:
            contra.append(f"{k} drops from {now[k]} to {cand[k]}")
    return (bool(razones) and not contra), razones, contra


def set_active(tag):
    """Put a downloaded tag in force, or None to go back to the bundled data."""
    stamp = Path(paths.updates_dir(paths.ACTIVE_FILE))
    if tag is None:
        if stamp.is_file():
            stamp.unlink()
        return
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(json.dumps({"tag": tag}, indent=1), encoding="utf-8")


def downloaded():
    root = Path(paths.updates_dir())
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.endswith(".part"))
