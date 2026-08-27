#!/usr/bin/env python3
"""
forge.py - seeds of any OoTMM version, built from source, to test the tracker against.

The corpus of real seeds only has the versions friends happened to play. OoTMM
is MIT and its CI runs in a public Docker image with the N64 toolchain inside,
so any tag —or any commit of master— can be built here in about a minute and
asked for seeds with whatever settings the test needs: a preset, a multiworld
of N players, or the exact settings of a real spoiler log.

What makes this more than "more ROMs": the build also yields the linker's
symbol table of each payload (`oot.sym`, `mm.sym`). The tracker locates
`gSharedCustomSave`, the other game's save buffer and the rest by reading
the payload's MIPS code; with the symbol table next to the ROM, the guard
compares that against the truth instead of against a census made by hand.

    python forge/forge.py roms  <oot rom> <mm rom>      once: the vanilla ROMs
    python forge/forge.py make  v29.0 v32.3 master ...   build + generate
    python forge/forge.py guard [dir|rom ...]            run the checks
    python forge/forge.py list                           what is in forge/out

`make` builds each ref in its own container (see build-one.sh), in parallel
(`--jobs`), and generates one Default seed unless told otherwise:

    --preset NAME        one seed per preset given (Default, Allsanity, Hell...)
    --players N          plus a multiworld of N players (default settings)
    --from-spoiler FILE  plus a seed with that spoiler's SettingsString
    --image IMAGE        the toolchain image, when the default guess is wrong

Everything lands in forge/out/<version>/<job>/ and is in .gitignore: the ROMs
carry Nintendo's code, like every seed. Only the Python standard library and
Docker are needed. Nothing is written outside forge/.
"""

import argparse
import concurrent.futures
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import threading

HERE = pathlib.Path(__file__).resolve().parent
ROMS = HERE / "roms"
OUT = HERE / "out"
JOBS = HERE / ".jobs"
REPO = "https://github.com/OoTMM/OoTMM.git"

# The image each version's CI used. Before v29 the toolchain came from an apt
# repository, not an image; ootmm-ci is the closest thing and is untested there.
IMAGE_NEW = "ghcr.io/ootmm/toolchain:1.4"
IMAGE_OLD = "ghcr.io/ootmm/ootmm-ci:1.0.0"

# CRC pairs (bytes 0x10..0x18 of the header) of the ROMs OoTMM accepts.
KNOWN = {
    "ec7011b77616d72b": ("oot", "Ocarina of Time NTSC 1.0"),
    "5354631c03a2def0": ("mm", "Majora's Mask USA"),
}


# --------------------------------------------------------------------------
# roms: the vanilla ROMs, in the byte order OoTMM wants
# --------------------------------------------------------------------------

def to_big_endian(data):
    """A .z64 whatever the file's byte order was (.n64/.v64 are swapped)."""
    magic = data[:4].hex()
    if magic == "80371240":
        return data
    b = bytearray(data)
    if magic == "37804012":          # byteswapped, every pair
        b[0::2], b[1::2] = b[1::2], b[0::2]
    elif magic == "40123780":        # little endian, every word
        b[0::4], b[1::4], b[2::4], b[3::4] = b[3::4], b[2::4], b[1::4], b[0::4]
    else:
        raise SystemExit(f"not an N64 ROM (magic {magic})")
    return bytes(b)


def cmd_roms(args):
    ROMS.mkdir(exist_ok=True)
    seen = {}
    for p in args.roms:
        data = to_big_endian(pathlib.Path(p).read_bytes())
        crc = data[0x10:0x18].hex()
        name = data[0x20:0x34].decode("ascii", "replace").strip()
        if crc in KNOWN:
            game, desc = KNOWN[crc]
        elif name.startswith("THE LEGEND OF ZELDA"):
            game, desc = "oot", f"'{name}' (crc {crc}: not the 1.0 OoTMM wants?)"
        elif "MAJORA" in name:
            game, desc = "mm", f"'{name}' (crc {crc}: not the USA one OoTMM wants?)"
        else:
            raise SystemExit(f"{p}: '{name}' is neither game")
        (ROMS / f"{game}.z64").write_bytes(data)
        seen[game] = desc
        print(f"{game}.z64  <- {p}  [{desc}]")
    missing = {"oot", "mm"} - set(seen)
    if missing:
        print(f"still missing: {', '.join(sorted(missing))}")


def roms_ready():
    return all((ROMS / f"{g}.z64").exists() for g in ("oot", "mm"))


# --------------------------------------------------------------------------
# make: one container per ref
# --------------------------------------------------------------------------

def guess_image(ref):
    m = re.match(r"^v(\d+)\.", ref)
    if m and int(m.group(1)) < 31:
        return IMAGE_OLD
    return IMAGE_NEW


def resolve_ref(ref):
    """(REF for the container, out label, VERSION for the spoiler)."""
    if re.match(r"^v\d+\.\d+(\.\d+)?$", ref):
        return ref, ref, ref
    if re.match(r"^[0-9a-f]{7,40}$", ref):
        sha = ref
        if len(sha) < 40:
            sha = ls_remote(sha)
        return sha, f"dev-{sha[:7]}", f"dev-{sha[:7]}"
    if ref in ("master", "dev"):
        sha = ls_remote("HEAD")
        return sha, f"dev-{sha[:7]}", f"dev-{sha[:7]}"
    raise SystemExit(f"'{ref}': give a tag (v32.3), a commit sha, or master")


def ls_remote(what):
    """The full sha of HEAD, or of an abbreviated sha, without a local clone."""
    if what == "HEAD":
        out = subprocess.run(["git", "ls-remote", REPO, "HEAD"], capture_output=True, text=True, check=True).stdout
        return out.split()[0]
    # git cannot expand a short sha remotely; GitHub's API can
    import urllib.request
    with urllib.request.urlopen(f"https://api.github.com/repos/OoTMM/OoTMM/commits/{what}") as r:
        return json.load(r)["sha"]


def spoiler_settings(path):
    """(settings string, players) from a spoiler log's header."""
    settings = players = None
    for line in pathlib.Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[:400]:
        if line.startswith("SettingsString:"):
            settings = line.split(":", 1)[1].strip()
        m = re.match(r"^\s+players:\s*(\d+)", line)
        if m:
            players = int(m.group(1))
    if not settings:
        raise SystemExit(f"{path}: no SettingsString line")
    return settings, players or 1


def plan_jobs(args, label):
    """[(job name, kind, arg)] plus the config files those need, written to
    JOBS/<label>/. Seeds are named after version and job so a rerun gives
    the same ROM."""
    jdir = JOBS / label
    jdir.mkdir(parents=True, exist_ok=True)
    for old in jdir.iterdir():
        old.unlink()
    jobs = []
    for preset in args.preset or ["Default"]:
        jobs.append((re.sub(r"[^a-z0-9]+", "-", preset.lower()).strip("-"), "preset", preset))
    if args.players:
        name = f"multi{args.players}"
        (jdir / f"{name}.yml").write_text(
            f"seed: forge-{label}-{name}\nsettings:\n  mode: multi\n  players: {args.players}\n", newline="\n")
        jobs.append((name, "config", f"{name}.yml"))
    for sp in args.from_spoiler or []:
        settings, players = spoiler_settings(sp)
        m = re.search(r"OoTMM-Spoiler-([A-Za-z0-9]+)", pathlib.Path(sp).name)
        name = "spoiler-" + (m.group(1) if m else pathlib.Path(sp).stem)
        (jdir / f"{name}.yml").write_text(f'seed: forge-{label}-{name}\nsettings: "{settings}"\n', newline="\n")
        jobs.append((name, "config", f"{name}.yml"))
    # LF, not the platform's: bash splits these on \n and would keep the \r
    (jdir / "jobs.tsv").write_text("".join(f"{n}\t{k}\t{a}\n" for n, k, a in jobs), newline="\n")
    # bash needs LF; a checkout with autocrlf would have turned them into CRLF
    script = (HERE / "build-one.sh").read_bytes().replace(b"\r\n", b"\n")
    (jdir / "build-one.sh").write_bytes(script)
    return jobs


_print_lock = threading.Lock()


def say(label, line):
    with _print_lock:
        print(f"[{label}] {line}")


def run_ref(container_ref, label, version, image, args):
    """One container: clone+build+generate for a ref already resolved and
    de-duplicated by the caller. Returns (label, rc, seconds); never raises,
    so one bad ref cannot sink the others' summary or the guard."""
    out = OUT / label
    try:
        jobs = plan_jobs(args, label)
    except (Exception, SystemExit) as ex:      # e.g. --from-spoiler without a SettingsString
        say(label, f"FAILED before launch: {ex}")
        return label, 1, 0.0
    out.mkdir(parents=True, exist_ok=True)
    name = f"forge-{label}-{os.getpid()}"
    cmd = [
        "docker", "run", "--rm", "--name", name,
        "-v", f"{ROMS}:/roms:ro",
        "-v", f"{OUT}:/out",
        "-v", f"{JOBS / label}:/jobs:ro",
        "-e", f"REF={container_ref}", "-e", f"LABEL={label}", "-e", f"VERSION_LABEL={version}",
        image, "bash", "/jobs/build-one.sh",
    ]
    t0 = datetime.datetime.now()
    say(label, f"{image}  jobs: {', '.join(j[0] for j in jobs)}")
    killed = threading.Event()

    def kill():
        killed.set()
        # docker rm -f stops the container; proc.kill() unblocks the reader in
        # case docker.exe itself wedged (a stuck pull) before the container
        # existed, when rm would find nothing to remove
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        proc.kill()

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    watchdog = threading.Timer(args.timeout, kill)
    watchdog.start()
    try:
        with open(out / "forge.log", "w", encoding="utf-8") as log:
            try:
                for line in proc.stdout:      # a killed pipe may raise here
                    log.write(line)
                    if line[:2] in ("==", "!!", "--"):
                        say(label, line.rstrip())
            except (ValueError, OSError):
                pass
        rc = proc.wait()
    finally:
        watchdog.cancel()
    if killed.is_set():
        rc = -1
        say(label, f"!! killed after {args.timeout}s")
    secs = (datetime.datetime.now() - t0).total_seconds()
    if rc != 0:
        say(label, f"FAILED (rc {rc}) after {secs:.0f}s; see {out / 'forge.log'}")
    return label, rc, secs


def cmd_make(args):
    if not roms_ready():
        raise SystemExit(f"no vanilla ROMs in {ROMS}: run `forge.py roms <oot> <mm>` first")
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        raise SystemExit("docker is not answering: is Docker Desktop running?")
    # Resolve every ref up front: fail fast on a bad one (before any container
    # runs) and drop duplicates, so two refs that name the same build cannot
    # collide on the container name, the .jobs folder or forge.log.
    plan, seen = [], {}
    for ref in args.refs:
        container_ref, label, version = resolve_ref(ref)   # may raise SystemExit: intended, pre-launch
        if label in seen:
            print(f"note: '{ref}' is the same build as '{seen[label]}' ({label}); skipped")
            continue
        seen[label] = ref
        m = re.match(r"^v(\d+)\.", ref)
        if m and int(m.group(1)) < 29:
            print(f"[{label}] note: versions before v29.0 have not been built with forge; expect to adjust build-one.sh")
        plan.append((container_ref, label, version, args.image or guess_image(ref)))

    results = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            futs = [pool.submit(run_ref, cref, label, ver, img, args) for cref, label, ver, img in plan]
            for f in concurrent.futures.as_completed(futs):
                results.append(f.result())
    except KeyboardInterrupt:
        print("\ninterrupted; stopping containers...")
        for _, label, _, _ in plan:
            subprocess.run(["docker", "rm", "-f", f"forge-{label}-{os.getpid()}"], capture_output=True)
        return 130
    print()
    bad = 0
    for label, rc, secs in sorted(results):
        b = OUT / label / "build.json"
        info = json.loads(b.read_text()) if b.exists() else {}
        j = info.get("jobs", {})
        status = "ok" if rc == 0 else "FAILED"
        print(f"  {label:16} {status:7} {secs:5.0f}s  seeds ok/skipped/failed: "
              f"{j.get('ok', '?')}/{j.get('skipped', '?')}/{j.get('failed', '?')}  sha {info.get('sha', '?')[:7]}")
        bad += rc != 0
    if args.guard:
        print()
        cmd_guard(argparse.Namespace(paths=[str(OUT / l) for l, _, _ in sorted(results)]))
    return 1 if bad else 0


# --------------------------------------------------------------------------
# guard, list
# --------------------------------------------------------------------------

def cmd_guard(args):
    sys.path.insert(0, str(HERE))
    import guard_roms
    return guard_roms.main(args.paths or [str(OUT)])


def cmd_list(args):
    if not OUT.exists():
        print(f"nothing in {OUT}")
        return 0
    for label in sorted(p for p in OUT.iterdir() if p.is_dir() and not p.name.startswith("_")):
        b = label / "build.json"
        info = json.loads(b.read_text()) if b.exists() else {}
        print(f"{label.name}  sha {info.get('sha', '?')[:7]}  {info.get('generator', '')}  node {info.get('node', '?')}")
        for job in sorted(p for p in label.iterdir() if p.is_dir() and p.name != "data"):
            jj = job / "job.json"
            j = json.loads(jj.read_text()) if jj.exists() else {}
            roms = sorted(p.name for p in job.glob("*.z64"))
            print(f"   {job.name:18} {j.get('status', '?'):8} {', '.join(roms)}")
    return 0


def main(argv=None):
    # progress lines are the point when this runs for minutes; do not hold them
    # back because stdout is a pipe
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("roms", help="import the vanilla ROMs (any byte order)")
    p.add_argument("roms", nargs="+")
    p.set_defaults(fn=cmd_roms)

    p = sub.add_parser("make", help="build refs and generate seeds")
    p.add_argument("refs", nargs="+", help="tags (v32.3), commit shas, or master")
    p.add_argument("--preset", action="append", help="a preset per seed wanted (default: Default)")
    p.add_argument("--players", type=int, help="also a multiworld of N players")
    p.add_argument("--from-spoiler", action="append", metavar="FILE", help="also a seed with this spoiler's settings")
    p.add_argument("--image", help="toolchain image override")
    p.add_argument("--jobs", type=int, default=2, help="containers in parallel (default 2)")
    p.add_argument("--timeout", type=int, default=1800, help="seconds per container (default 1800)")
    p.add_argument("--guard", action="store_true", help="run the guard on what was built")
    p.set_defaults(fn=cmd_make)

    p = sub.add_parser("guard", help="check the tracker against built (or any) seeds")
    p.add_argument("paths", nargs="*", help="forge/out subfolders, folders of seeds, or ROMs (default: all of forge/out)")
    p.set_defaults(fn=cmd_guard)

    p = sub.add_parser("list", help="what forge/out holds")
    p.set_defaults(fn=cmd_list)

    args = ap.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
