#!/usr/bin/env python3
"""
capture.py - run the overlay against a live session and keep the whole record.

For a "fresh save" test: generate a seed, open it in Project64-EM, run this,
then run tracker.lua and start a new file. It

  1. reads the ROM first and PRINTS THE PREDICTION -- where the ROM's own code
     says gSharedCustomSave, the other game's buffer and the layout are
     (payload.py), and which generation the seed is;
  2. launches `ootmm.py overlay --rom ROM` with its console going to a log;
  3. polls /state.json once a second, appends every sample to a .jsonl and
     prints the fields that matter whenever one of them changes:
     bases, custom_base, custom_source, custom_ok, custom_bits, confidence,
     trusted, scene, done_total...;
  4. on stop (Ctrl-C, --minutes, or the presence of capture.stop next to the
     log) prints observed-vs-predicted, kills the overlay and, with --dump,
     runs `ootmm.py dump` so the fresh save is kept as a reference RAM image
     (tracker.lua reconnects on its own).

    python capture.py --rom PATH [--minutes 30] [--dump ram-fresh-oot.bin] [-- overlay args]

Without --rom the newest .z64 under Downloads is taken, and said out loud.
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
WATCH = ["ready", "waiting", "error", "active", "bases", "custom_base", "custom_source",
         "custom_ok", "custom_bits", "confidence", "trusted", "done_total", "done_by_game",
         "placement_ratio", "same_version_as_data", "rom_of_table", "custom_n", "items_n"]


def newest_rom():
    root = pathlib.Path.home() / "Downloads"
    cands = [p for p in root.rglob("*.z64") if "z64-corpus" not in p.parts]
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


def predict(rom_path):
    import payload
    import placement
    import rom as romlib

    rb = pathlib.Path(rom_path).read_bytes()
    romlib.extra_dma(rb)  # raises if not an OoTMM seed
    names = placement.find_item_names(rb, "oot") or []
    res = payload.locate(rb)
    print(f"[capture] ROM: {rom_path}")
    print(f"[capture] kItemNames: {len(names)} entries (784 / 829 / 936 = the three generations seen so far)")
    for game in ("oot", "mm"):
        b = res.get(game, {})
        if "custom" in b:
            print(f"[capture] PREDICTION running {game}: gSharedCustomSave 0x{b['custom'][0]:08X}"
                  f" ({b['custom'][1]:#x} bytes), other game's buffer 0x{b['foreign_base']:08X}"
                  f" (tracker base), own save 0x{b.get('own', (0,))[0]:08X}")
        else:
            print(f"[capture] PREDICTION running {game}: the ROM's code did NOT give the buffers")
    lay = res.get("layout", {})
    for game in ("oot", "mm"):
        d = {k: v for k, v in lay.get(game, {}).items() if not k.startswith("_")}
        print(f"[capture] PREDICTION layout {game}: " + ", ".join(
            f"{k}={v:#x}" if isinstance(v, int) else f"{k}={v}" for k, v in d.items()))
    return res


def get_state(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/state.json", timeout=2) as r:
            return json.load(r)
    except Exception:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rom")
    ap.add_argument("--minutes", type=float, default=45.0)
    ap.add_argument("--http-port", type=int, default=8013)
    ap.add_argument("--port", type=int, default=13251)
    ap.add_argument("--dump", help="after stopping, dump RDRAM to this file (needs tracker.lua still running)")
    ap.add_argument("--out", default=str(HERE / "capture"), help="prefix for the log files")
    ap.add_argument("overlay_args", nargs="*", help="extra args for `ootmm.py overlay` (after --)")
    args = ap.parse_args(argv)

    rom = args.rom or newest_rom()
    if not rom:
        sys.exit("no ROM given and none found under Downloads")
    rom = str(rom)
    out = pathlib.Path(args.out)
    log_path = pathlib.Path(str(out) + "-overlay.log")
    jsonl_path = pathlib.Path(str(out) + "-state.jsonl")
    stop_path = pathlib.Path(str(out) + ".stop")
    stop_path.unlink(missing_ok=True)

    try:
        pred = predict(rom)
    except Exception as ex:
        print(f"[capture] cannot read the ROM as an OoTMM seed: {type(ex).__name__}: {ex}")
        return 1
    json.dump({"rom": rom, "prediction": {
        g: {k: v for k, v in pred.get(g, {}).items()} for g in ("oot", "mm")},
        "layout": pred.get("layout")}, open(str(out) + "-prediction.json", "w"), indent=1, default=str)

    print(f"[capture] overlay log -> {log_path}")
    print(f"[capture] state samples -> {jsonl_path}")
    print(f"[capture] to stop: Ctrl-C, or create {stop_path}")
    log = open(log_path, "w", encoding="utf-8")
    # -u: the overlay's prints are what we are here to keep, and with stdout
    # redirected to a file Python buffers them -- kill it and the log is empty
    cmd = [sys.executable, "-u", str(HERE / "ootmm.py"), "overlay", "--rom", rom,
           "--http-port", str(args.http_port), "--port", str(args.port)] + args.overlay_args
    ov = subprocess.Popen(cmd, cwd=HERE, stdout=log, stderr=subprocess.STDOUT)

    last = {}
    t_end = time.time() + args.minutes * 60
    samples = 0
    try:
        with open(jsonl_path, "a", encoding="utf-8") as jf:
            while time.time() < t_end and not stop_path.exists():
                if ov.poll() is not None:
                    print(f"[capture] the overlay exited with {ov.returncode}; see the log")
                    break
                st = get_state(args.http_port)
                if st is not None:
                    samples += 1
                    st["_t"] = round(time.time(), 1)
                    jf.write(json.dumps(st) + "\n")
                    jf.flush()
                    cur = {k: st.get(k) for k in WATCH}
                    changed = {k: v for k, v in cur.items() if last.get(k, "<unset>") != v}
                    if changed:
                        stamp = time.strftime("%H:%M:%S")
                        print(f"[{stamp}] " + "  ".join(f"{k}={json.dumps(v)}" for k, v in changed.items()),
                              flush=True)
                        last = cur
                time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        print(f"[capture] {samples} samples")
        # observed vs predicted
        st = last
        active = st.get("active")
        p = pred.get(active or "", {})
        if active and "custom" in p:
            other = "mm" if active == "oot" else "oot"
            obs_c = st.get("custom_base")
            obs_f = (st.get("bases") or {}).get(other)
            print(f"[capture] running {active}: custom_base observed {obs_c} vs predicted 0x{p['custom'][0]:08X}"
                  f" -> {'MATCH' if obs_c and int(obs_c, 16) == p['custom'][0] else 'DIFFERENT'}")
            print(f"[capture] running {active}: {other} buffer observed {obs_f} vs predicted 0x{p['foreign_base']:08X}"
                  f" -> {'MATCH' if obs_f and int(obs_f, 16) == p['foreign_base'] else 'DIFFERENT'}")
            print(f"[capture] custom_source={st.get('custom_source')} custom_ok={st.get('custom_ok')}"
                  f" bits={st.get('custom_bits')} confidence={st.get('confidence')} trusted={st.get('trusted')}")
        try:
            ov.kill()
        except Exception:
            pass
        log.close()
        if args.dump:
            print(f"[capture] dumping RDRAM to {args.dump} (waiting for tracker.lua to reconnect)...")
            subprocess.run([sys.executable, str(HERE / "ootmm.py"), "dump", "0x80000000:0x800000",
                            "-o", args.dump, "--port", str(args.port)], cwd=HERE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
