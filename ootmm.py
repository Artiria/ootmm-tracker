#!/usr/bin/env python3
"""
ootmm.py - memory tools for the OoTMM autotracker on Project64-EM.

P64-EM's Lua socket can only connect, not listen, so in every mode this tool
is the one listening and the Lua script is the client.

Subcommands
-----------
  watch    Poll addresses and show the ones that change.      (tracker.lua)
  dump     Dump a region of RDRAM to a file.                  (tracker.lua)
  diff     Compare two dumps. No emulator needed.
  items    Read the inventory in a loop, calling every change.(tracker.lua)
  checks   List the completed checks, resolved to names.      (tracker.lua)
  overlay  The watchable tracker: server + its own window.    (tracker.lua)
  proxy    Sit between the multi script and MultiClient       (adapter-tracker.lua)
           to capture which addresses the multi uses.

Examples
--------
  python ootmm.py watch 0x80000318:4
  python ootmm.py dump 0x80000000:0x400000 -o before.bin
  python ootmm.py diff before.bin after.bin --base 0x80000000 --bits-set
  python ootmm.py proxy
"""

import argparse
import json
import os
import re
import socket
import struct
import sys
import threading
import time
from collections import Counter, OrderedDict

# opcode -> (name, argument bytes after the opcode, reply bytes)
OPS = {
    2: ("R8", 4, 1),
    3: ("R16", 4, 2),
    4: ("R32", 4, 4),
    6: ("W8", 5, 0),
    7: ("W16", 6, 0),
    8: ("W32", 8, 0),
}
OP_PING = 0x01
OP_READ_BLOCK = 0x10
MAGIC = 0x54524B31  # 'TRK1'
MAX_BLOCK = 0x10000

RDRAM_LO = 0x80000000
RDRAM_HI = 0x807FFFFF

# 0x80000318 holds the RDRAM size: 0x800000 with the Expansion Pak (which
# OoTMM requires), 0x400000 without. Handy as a check that we are reading real
# memory.
OSMEMSIZE_ADDR = 0x80000318
OSMEMSIZE_OK = (0x00400000, 0x00800000)

FMT = {1: "B", 2: "H", 4: "I"}

# Bases located by signature (ZELDAZ / ZELDA3) on OoTMM v32.0 and confirmed by
# activity: the "live" ones concentrate the changes, the others are static
# copies. Tied to the generator version; revalidate if it changes.
#
# Offsets inside "oot" (see inventory.py for the full inventory map, taken from
# combo/oot/save.h and validated in game):
#   +0x030 u16  health; seen going from 44 to 48 with a Recovery Heart
#   +0x034 s16  rupees
#   +0x038 u16  naviTimer; climbs on its own. We used to call it "time of day":
#               it is not, but it is still the noise to exclude from the diff.
#   +0x0A4 u32  questItems; bit 6 = Minuet of Forest (cross-checked with spoiler)
#   +0x537 u8   UNIDENTIFIED. Went 0->11->15 after a Large Magic Jar and we
#               took it for magic, but magic is at +0x33. Still open.
#   +0x1368..+0x1371  changes on scene change; temporary flags
#
# Per-scene flag table, inside "oot":
#   +0xD4 + scene*0x1C, 124 scenes of 0x1C bytes each
#     +0x00 chest   +0x04 swch   +0x08 clear   +0x0C collect
#     +0x10 unk     +0x14 rooms  +0x18 floors
#   Verified: scene 40 chest=0x0F (the 4 chests in Mido's House), scene 85
#   chest=0x01 (Kokiri Sword Chest). Cross-checked with the v32.0 spoiler.
#
# CAREFUL: the chest flag is NOT written when you open it. It gets flushed to
# the save context on leaving the scene, or on saving. A tracker that only
# reads this table sees the checks late.
SAVE_REGIONS = {
    "oot": (0x8011A5D0, 0x1500),
    "mm": (0x8044BE18, 0x1500),
    "oot-alt": (0x800FBFB8, 0x1500),
    "mm-alt": (0x80442248, 0x1500),
}


def in_rdram(v):
    return RDRAM_LO <= v <= RDRAM_HI


def resolve_base(spec):
    """Accepts an address or the name of a known region."""
    if spec in SAVE_REGIONS:
        return SAVE_REGIONS[spec][0]
    try:
        return int(spec, 0)
    except ValueError:
        sys.exit(f"invalid base: {spec!r}. Use an address or: {', '.join(SAVE_REGIONS)}")


def parse_targets(spec):
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(0[xX][0-9a-fA-F]+|\d+):([124])", part)
        if not m:
            sys.exit(f"invalid format: {part!r}. Use ADDR:SIZE, e.g. 0x80000318:4")
        out.append((int(m.group(1), 0), int(m.group(2))))
    if not out:
        sys.exit("no address given")
    return out


def parse_region(spec):
    spec = spec.strip()
    if spec in SAVE_REGIONS:
        return SAVE_REGIONS[spec]
    m = re.fullmatch(r"(0[xX][0-9a-fA-F]+|\d+):(0[xX][0-9a-fA-F]+|\d+)", spec)
    if not m:
        sys.exit(f"invalid format: {spec!r}. Use ADDR:LEN, e.g. 0x80000000:0x400000")
    addr, length = int(m.group(1), 0), int(m.group(2), 0)
    if addr % 4 or length % 4:
        sys.exit("address and length must be multiples of 4")
    return addr, length


# --------------------------------------------------------------------------
# Capture and summary
# --------------------------------------------------------------------------


class Capture:
    """Aggregated state of what was observed. Thread-safe."""

    def __init__(self, jsonl_path=None, log_all=False, verbose=False):
        self.lock = threading.Lock()
        self.stats = OrderedDict()  # (op, addr) -> dict
        self.endian_votes = Counter()
        self.total = 0
        self.log_all = log_all
        self.verbose = verbose
        self.jsonl = open(jsonl_path, "w", encoding="utf-8") if jsonl_path else None
        self.t0 = time.time()

    def decode_addr(self, raw):
        """Return (addr, endian), trying both byte orders.

        An N64 address in big endian starts with 0x80; read the other way round
        it falls outside RDRAM, so it is almost always unambiguous. If both
        fit, the running vote decides.
        """
        be = struct.unpack(">I", raw)[0]
        le = struct.unpack("<I", raw)[0]
        be_ok, le_ok = in_rdram(be), in_rdram(le)
        if be_ok and not le_ok:
            self.endian_votes[">"] += 1
            return be, ">"
        if le_ok and not be_ok:
            self.endian_votes["<"] += 1
            return le, "<"
        if be_ok and le_ok:
            win = self.endian_votes.most_common(1)
            e = win[0][0] if win else ">"
            return (be if e == ">" else le), e
        return None, None

    def record(self, op, addr, value, size):
        with self.lock:
            self.total += 1
            key = (op, addr)
            st = self.stats.get(key)
            changed = st is None or st["last"] != value
            if st is None:
                st = {
                    "count": 0,
                    "size": size,
                    "first": value,
                    "last": value,
                    "min": value,
                    "max": value,
                    "changes": 0,
                }
                self.stats[key] = st
            st["count"] += 1
            if value is not None:
                if st["last"] != value:
                    st["changes"] += 1
                st["last"] = value
                st["min"] = value if st["min"] is None else min(st["min"], value)
                st["max"] = value if st["max"] is None else max(st["max"], value)

            if self.verbose and (self.log_all or changed):
                vs = "-" if value is None else f"0x{value:0{size * 2}X}"
                t = time.time() - self.t0
                print(f"  [{t:7.1f}s] {op:<3} 0x{addr:08X}  {vs}")

            if self.jsonl and (self.log_all or changed):
                self.jsonl.write(
                    json.dumps(
                        {
                            "t": round(time.time() - self.t0, 3),
                            "op": op,
                            "addr": addr,
                            "value": value,
                        }
                    )
                    + "\n"
                )
                self.jsonl.flush()

    def summary(self, gap=128):
        with self.lock:
            if not self.stats:
                return "\nNo operations were captured.\n"

            by_addr = {}
            for (op, addr), st in self.stats.items():
                by_addr.setdefault(addr, []).append((op, st))
            addrs = sorted(by_addr)

            out = ["", "=" * 74]
            out.append(f"  {self.total} operations over {len(addrs)} distinct addresses")
            e = self.endian_votes.most_common(1)
            if e:
                name = "big endian" if e[0][0] == ">" else "little endian"
                out.append(f"  Protocolo: {name} ({self.endian_votes[e[0][0]]} confirmaciones)")
            out.append("=" * 74)

            regions = []
            cur = [addrs[0], addrs[0]]
            for a in addrs[1:]:
                if a - cur[1] <= gap:
                    cur[1] = a
                else:
                    regions.append(cur)
                    cur = [a, a]
            regions.append(cur)

            out += ["", "REGIONS  (save context / mailbox candidates)", "-" * 74]
            for lo, hi in regions:
                sel = [a for a in addrs if lo <= a <= hi]
                ops = Counter()
                for a in sel:
                    for op, st in by_addr[a]:
                        ops[op] += st["count"]
                mix = " ".join(f"{k}:{v}" for k, v in sorted(ops.items()))
                out.append(
                    f"  0x{lo:08X}-0x{hi:08X}  ({hi - lo + 4:6d} B)  "
                    f"{len(sel):4d} dir  {sum(ops.values()):7d} ops   {mix}"
                )

            out += ["", "ADDRESSES  (* = the value changed during the capture)", "-" * 74]
            out.append(f"   {'address':<12}{'op':<6}{'ops':>8}{'changes':>9}  first -> last")
            for a in addrs:
                for op, st in sorted(by_addr[a]):
                    w = st["size"] * 2
                    f0 = "-" if st["first"] is None else f"0x{st['first']:0{w}X}"
                    l0 = "-" if st["last"] is None else f"0x{st['last']:0{w}X}"
                    mark = "*" if st["changes"] else " "
                    out.append(
                        f" {mark} 0x{a:08X}  {op:<6}{st['count']:>8}{st['changes']:>9}  {f0} -> {l0}"
                    )
            out.append("")
            return "\n".join(out)


# --------------------------------------------------------------------------
# Connection with tracker.lua
# --------------------------------------------------------------------------


class Link:
    """Connection to tracker.lua, with the byte order already settled."""

    def __init__(self, sock, endian):
        self.sock = sock
        self.endian = endian

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("the Lua script closed the connection")
            buf += chunk
        return buf

    def read(self, addr, size):
        op = {1: 2, 2: 3, 4: 4}[size]
        self.sock.sendall(struct.pack("B", op) + struct.pack(self.endian + "I", addr))
        return struct.unpack(self.endian + FMT[size], self._recv_exact(size))[0]

    def read_block(self, addr, length):
        """Return the bytes in real memory order.

        The block travels as u32s packed by binary.pack_u32. If that packing is
        little endian, what arrives is reversed within each word relative to
        N64 memory, which is big endian; that has to be undone here or the
        dumps come out unreadable.
        """
        out = bytearray()
        while length > 0:
            n = min(length, MAX_BLOCK)
            self.sock.sendall(
                struct.pack("B", OP_READ_BLOCK) + struct.pack(self.endian + "II", addr, n)
            )
            chunk = self._recv_exact(n)
            if self.endian == "<":
                sw = bytearray(len(chunk))
                for i in range(0, len(chunk) - 3, 4):
                    sw[i : i + 4] = chunk[i : i + 4][::-1]
                chunk = bytes(sw)
            out += chunk
            addr += n
            length -= n
        return bytes(out)


def listen_for_lua(host, port, label):
    ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind((host, port))
    ls.listen(1)
    print(f"[{label}] listening on {host}:{port}")
    print(f"[{label}] start tracker.lua in P64-EM (File > Lua Scripts...)\n")
    sock, _ = ls.accept()
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    ls.close()
    print(f"[{label}] Lua script connected")
    return sock


def handshake(sock, label):
    """PING to confirm this is tracker.lua and pin down the byte order."""
    sock.sendall(struct.pack("B", OP_PING))
    raw = b""
    while len(raw) < 4:
        chunk = sock.recv(4 - len(raw))
        if not chunk:
            sys.exit(f"[{label}] the script closed the connection during the PING")
        raw += chunk

    endian = None
    for e in (">", "<"):
        if struct.unpack(e + "I", raw)[0] == MAGIC:
            endian = e
    if endian is None:
        print(f"[{label}] unexpected reply to PING: {raw.hex()}")
        print(f"[{label}] looks like adapter.lua, not tracker.lua. Check which script you started.")
        sys.exit(1)

    link = Link(sock, endian)
    name = "big" if endian == ">" else "little"
    size = link.read(OSMEMSIZE_ADDR, 4)
    if size in OSMEMSIZE_OK:
        mb = size // (1024 * 1024)
        print(f"[{label}] protocol {name} endian, RDRAM {mb} MB - memory reads OK\n")
    else:
        print(f"[{label}] protocol is {name} endian, but osMemSize = 0x{size:08X} (unexpected)")
        print(f"[{label}] the ROM may not be running yet\n")
    return link


# --------------------------------------------------------------------------
# Subcomandos
# --------------------------------------------------------------------------


def cmd_watch(args):
    targets = parse_targets(args.targets)
    cap = Capture(args.jsonl, log_all=args.all, verbose=True)
    sock = listen_for_lua(args.host, args.port, "watch")
    link = handshake(sock, "watch")

    print("[watch] polling (only changes are printed). Ctrl+C for the summary.\n")
    try:
        while True:
            for addr, size in targets:
                cap.record({1: "R8", 2: "R16", 4: "R32"}[size], addr, link.read(addr, size), size)
            time.sleep(args.interval)
    except (ConnectionError, OSError) as ex:
        print(f"[watch] connection lost: {ex}")
    finally:
        finish(cap, args)


def cmd_dump(args):
    addr, length = parse_region(args.region)
    sock = listen_for_lua(args.host, args.port, "dump")
    link = handshake(sock, "dump")

    print(f"[dump] reading 0x{addr:08X}, {length} bytes ({length / 1024:.0f} KB)")
    t0 = time.time()
    done = 0
    with open(args.out, "wb") as f:
        while done < length:
            n = min(MAX_BLOCK, length - done)
            f.write(link.read_block(addr + done, n))
            done += n
            pct = 100 * done / length
            print(f"\r[dump] {pct:5.1f}%  {done // 1024} KB", end="", flush=True)
    dt = time.time() - t0
    print(f"\n[dump] {length} bytes in {dt:.1f}s -> {args.out}")
    print("[dump] note: the game keeps running, the dump is not one exact instant.")


SIG_OOT = b"ZELDAZ"
SIG_MM = b"ZELDA3"
SIG_OFFSET = 0x1C  # the signature lives at base + 0x1C (the newf field)

# Bases seen so far, IN PRIORITY ORDER: the first one that answers wins.
# Taking the lowest address will not do, because in OoT the static copy
# (0x800FBFB8) sits below the live one (0x8011A5D0).
KNOWN_BASES = [
    # jugando OoT
    (0x8011A5D0, "oot"),
    (0x8044BE18, "mm"),
    # running MM: the MM one the .fla persists is 0x801EF678
    (0x801EF678, "mm"),
    (0x8076C4F0, "oot"),
    # dev-542a121 moved the buffers of the game that is NOT running. Every one
    # of these is validated by save_looks_sane before it is used, so a stale
    # entry costs nothing -- but a MISSING one costs an 8 MB scan over the Lua
    # link, which is why they are worth keeping as versions come and go.
    (0x8044CF78, "mm"),    # running OoT
    (0x8076D400, "oot"),   # running MM
    # copies and secondaries, only if there is nothing better
    (0x801C6954, "mm"),
    (0x800FBFB8, "oot"),
    (0x80442248, "mm"),
]


# Fields that tell whether a candidate is the live save or garbage. Offsets
# relative to the base (with `info` at base+0x1C), taken from the struct
# arithmetic of OotSavePlayerData / MmSavePlayerData.
SANITY = {
    "oot": {"cap": 0x2E, "health": 0x30, "rupees": 0x34, "skulls": None},
    "mm": {"cap": 0x2C, "health": 0x2E, "rupees": 0x32, "skulls": 0xEB8},
}


def save_looks_sane(link, game, base):
    """Whether what sits at that base looks like a real save file.

    The signature alone is not enough: `ZELDA3` also shows up in static copies
    and stale buffers, and taking the first one by address is how you end up
    reading garbage —swamp skulltulas at 7680, for instance, when the maximum
    is 30—. These checks are cheap and cut that dead.
    """
    f = SANITY[game]
    try:
        blk = link.read_block(base + 0x2C, 16)
        cap = struct.unpack_from(">h", blk, f["cap"] - 0x2C)[0]
        health = struct.unpack_from(">h", blk, f["health"] - 0x2C)[0]
        rupees = struct.unpack_from(">h", blk, f["rupees"] - 0x2C)[0]
    except (ConnectionError, OSError, struct.error):
        return False

    # health capacity is always a whole number of hearts
    if not (0 < cap <= 0x140 and cap % 0x10 == 0):
        return False
    if not (0 <= health <= cap):
        return False
    if not (0 <= rupees <= 9999):
        return False

    if f["skulls"] is not None:
        try:
            s = link.read_block(base + f["skulls"], 4)
            swamp, ocean = struct.unpack(">HH", s)
        except (ConnectionError, OSError, struct.error):
            return False
        if swamp > 30 or ocean > 30:
            return False
    return True


# Crossing between OoT and MM reorganises RDRAM: the running game's save drops
# to the low zone and the other goes high. Every base ever measured obeys it --
# low ones live around 0x8011..0x801F, high ones around 0x8044..0x8076 -- so
# this threshold sits in the empty gap between the two, with room on both sides
# for a version to shift things.
RDRAM_MID = 0x80300000


def bases_coherentes(bases):
    """Whether a pair of bases matches one of the two RAM layouts.

    Exactly one of the two has to be in the low zone. Both low (or both high)
    means one of them is a leftover from the layout before the last crossing:
    the signature and the contents still look fine, which is precisely why it
    has to be ruled out by where it sits rather than by what it holds.
    """
    if len(bases) < 2:
        return True
    return sum(1 for b in bases.values() if b < RDRAM_MID) == 1


def locate_saves(link, verbose=True, hints=()):
    """Locate the save contexts by signature, and **validate** what turns up.

    This has to happen on every startup: crossing between OoT and MM
    reorganises RAM and the bases change completely. The active game is the one
    whose base sits low in memory.

    The signature is necessary but not sufficient: static copies and stale
    buffers carry it too. Every candidate is tried and the first one that also
    has plausible contents wins.

    `hints` are (base, game) pairs to try BEFORE the known list: the overlay
    passes the buffers the ROM's own code names (payload.py, via checks.json),
    so a build that moved them is found without the 8 MB scan and without
    anyone adding a line to KNOWN_BASES.
    """
    cands = {"oot": [], "mm": []}
    seen = set()
    for base, game in list(hints) + KNOWN_BASES:
        if (base, game) in seen:
            continue
        seen.add((base, game))
        sig = SIG_OOT if game == "oot" else SIG_MM
        if link.read_block(base + SIG_OFFSET, 8)[:6] == sig:
            cands[game].append(base)

    validos = {g: [b for b in cands[g] if save_looks_sane(link, g, b)] for g in ("oot", "mm")}

    # Pick the PAIR, not each one on its own. Crossing between games moves both
    # buffers at once, so only certain combinations can be real -- see
    # bases_coherentes. Choosing independently, first-that-validates in list
    # order, can take a buffer left over from the previous layout: it still
    # carries a good signature and plausible contents, and it would then be
    # read as the live save for the rest of the session, quietly reporting
    # somebody else's rupees and hearts.
    out = {}
    for b_oot in validos["oot"]:
        for b_mm in validos["mm"]:
            if bases_coherentes({"oot": b_oot, "mm": b_mm}):
                out = {"oot": b_oot, "mm": b_mm}
                break
        if out:
            break
    if not out:
        for game in ("oot", "mm"):
            if validos[game]:
                out[game] = validos[game][0]

    # We scan if EITHER one is missing, not only if both are: crossing
    # between games moves both bases, and finding one does not imply the other
    # is still where it was.
    faltan = [g for g in ("oot", "mm") if g not in out]
    if faltan:
        if verbose:
            print(f"[items] cannot locate {', '.join(faltan)} among the known bases; scanning RDRAM...")
        blob = link.read_block(0x80000000, 0x800000)
        for game in faltan:
            sig = SIG_OOT if game == "oot" else SIG_MM
            hits = []
            start = 0
            while True:
                i = blob.find(sig, start)
                if i < 0:
                    break
                hits.append(0x80000000 + i - SIG_OFFSET)
                start = i + 1
            buenos = [b for b in hits if save_looks_sane(link, game, b)]
            if verbose and hits:
                print(f"[items] {game}: {len(hits)} signatures, {len(buenos)} with plausible contents")
            # Prefer one that pairs up with what we already have: a stale buffer
            # on the wrong side of RDRAM passes every content check there is.
            otro = {g: b for g, b in out.items() if g != game}
            coherentes = [b for b in buenos if bases_coherentes({**otro, game: b})]
            elegidos = coherentes or buenos or hits
            if elegidos:
                out[game] = elegidos[0]
                if verbose and len(elegidos) > 1:
                    otras = ", ".join(f"0x{c:08X}" for c in elegidos[1:])
                    print(f"[items] {game}: picking 0x{elegidos[0]:08X}; there are more ({otras})")
                if verbose and not buenos:
                    print(f"[items] {game}: none pass the checks; whatever comes out may be garbage")
    return out


# ---------------------------------------------------------------------------
# Where the player actually is: the PlayState
#
# The save context also carries a scene, but it is the *saved* one:
#   OoT  info.sceneId          base+0x66
#   MM   playerData.savedSceneNum  base+0x42
# Measured against the two dumps, both lag, and MM's is not even the previous
# scene:
#
#   dump    PlayState        save context
#   OoT     0x2D KOKIRI_SHOP   0x55 KOKIRI_FOREST   one scene behind
#   MM      0x6F CLOCK_TOWN_S  0x08                 unrelated
#
# The live one lives in the PlayState, which starts with a GameState. Layout
# from combo/game_state.h and combo/{oot,mm}/play.h:
#
#   GameState (0xA4)
#     +0x00 gfxCtx*  +0x04 main  +0x08 destroy  +0x0C nextGameStateInit
#     +0x10 nextGameStateSize    +0x14 input[4]  +0x74 tha
#     +0x84 unk[0x17]  +0x9B running(u8)  +0x9C frameCount
#   PlayState
#     +0xA4 sceneId u16    +0xB0 sceneSegment*
#     roomCtx.curRoom.num s8: OoT +0x11CBC, MM +0x186E0
#
# CAREFUL: `running` is at +0x9B, not +0x98. tha ends at 0x84 and unk_84 is
# 0x17 bytes, which lands it on an odd address. Getting that wrong makes the
# scan find nothing at all, with no other symptom.
PLAY_LAYOUT = {
    #        room offset within PlayState, highest plausible scene id
    "oot": {"room": 0x11CBC, "max_scene": 110},
    "mm": {"room": 0x186E0, "max_scene": 120},
}

# The game state is allocated once per boot and lands in the same place every
# time, so these hit on the first try and the scan below is the fallback. They
# are the addresses OoT and MM practice tools have always used, and both dumps
# confirm them.
KNOWN_PLAY = {"oot": 0x801C84A0, "mm": 0x803E6B20}

PLAY_HEADER = 0xB4  # enough to cover gfxCtx .. sceneSegment


def _play_header_ok(buf, off, game):
    """Whether the 0xB4 bytes at `off` look like the head of a live PlayState.

    Takes an offset rather than a slice because the fallback scan calls this
    two million times, and slicing there costs more than the checks do.
    """
    gfx, main, dtor, nxt, nsz = struct.unpack_from(">IIIII", buf, off)
    if not (in_rdram(gfx) and in_rdram(main) and in_rdram(dtor)):
        return False
    if gfx % 4 or main % 4 or dtor % 4:
        return False
    # three different subsystems: stale buffers full of one repeated pointer
    # sail through everything else
    if gfx == main or main == dtor or gfx == dtor:
        return False
    # nothing is scheduled while a state is simply running
    if nxt or nsz:
        return False
    if buf[off + 0x9B] != 1:  # running
        return False
    if struct.unpack_from(">I", buf, off + 0x9C)[0] >= 0x10000000:
        return False  # a frame counter, not a pointer that happens to sit here
    if struct.unpack_from(">H", buf, off + 0xA4)[0] > PLAY_LAYOUT[game]["max_scene"]:
        return False
    seg = struct.unpack_from(">I", buf, off + 0xB0)[0]
    return in_rdram(seg) and seg % 4 == 0


def read_play(link, game, addr):
    """(sceneId, roomNum) out of the PlayState at `addr`, or None if it does
    not look like one any more."""
    room_off = PLAY_LAYOUT[game]["room"]
    if not in_rdram(addr) or not in_rdram(addr + room_off + 3):
        return None
    try:
        hdr = link.read_block(addr, PLAY_HEADER)
        room = link.read_block(addr + room_off, 4)
    except (ConnectionError, OSError, struct.error):
        return None
    if len(hdr) < PLAY_HEADER or not _play_header_ok(hdr, 0, game):
        return None
    num = struct.unpack_from(">b", room, 0)[0]
    if not (-1 <= num <= 30):
        return None
    return struct.unpack_from(">H", hdr, 0xA4)[0], num


def locate_play(link, game, verbose=True):
    """Find the running game's PlayState. Known address first, scan after.

    Same shape as locate_saves, and for the same reason: the scan reads all of
    RDRAM, so it must not happen on every poll. The caller caches the address
    and only comes back when read_play stops validating.
    """
    known = KNOWN_PLAY.get(game)
    if known is not None and read_play(link, game, known) is not None:
        return known

    if verbose:
        print(f"[play] {game}: not at 0x{known:08X}; scanning RDRAM...")
    try:
        blob = link.read_block(RDRAM_LO, 0x800000)
    except (ConnectionError, OSError, struct.error):
        return None

    room_off = PLAY_LAYOUT[game]["room"]
    hits = []
    for off in range(0, len(blob) - room_off - 4, 4):
        if not _play_header_ok(blob, off, game):
            continue
        num = struct.unpack_from(">b", blob, off + room_off)[0]
        if -1 <= num <= 30:
            hits.append(RDRAM_LO + off)

    if not hits:
        if verbose:
            print(f"[play] {game}: no PlayState found")
        return None
    if verbose and len(hits) > 1:
        otras = ", ".join(f"0x{a:08X}" for a in hits[1:])
        print(f"[play] {game}: picking 0x{hits[0]:08X}; there are more ({otras})")
    elif verbose:
        print(f"[play] {game}: found at 0x{hits[0]:08X}")
    return hits[0]


def parse_spoiler(lines):
    """Pull 'location -> item' out of a spoiler log's lines. The useful ones
    carry the game in front: '    OOT Mido's House Top Right: Minuet'.

    Kept separate from load_spoiler because the overlay allows loading a
    spoiler from the page itself, and there what you have is uploaded text,
    not a path.
    """
    items = {}
    pat = re.compile(r"^\s{2,}(OOT|MM) (.+?): (.+?)\s*$")
    for line in lines:
        m = pat.match(line.rstrip("\n"))
        if m:
            items[(m.group(1).lower(), m.group(2))] = m.group(3)
    return items


def spoiler_version(lines):
    """The OoTMM version the spoiler header declares ('Version: v32.0').

    Used to avoid loading a spoiler from another version onto a checks.json
    that is not its own: location names change between versions.
    """
    for line in list(lines)[:40]:
        if line.startswith("Version:"):
            return line.split(":", 1)[1].strip()
    return None


def load_spoiler(path):
    """The same, from a file."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        return parse_spoiler(fh)


def cmd_items(args):
    """Read the inventory in a loop and call every change. To hunt items: pick
    one up, watch which line comes out."""
    import inventory

    if args.dump:
        data = open(args.dump, "rb").read()
        base = args.base if args.base != 0x80000000 else 0x8011A5D0
        save = data[base - 0x80000000 : base - 0x80000000 + 0x1500]
        st = inventory.snapshot(save)
        print(f"OoT inventory at 0x{base:08X}\n")
        for k, v in st.items():
            if v not in (0, 0xFF):
                print(f"  {k:32} {inventory.fmt(k, v)}")
        return

    sock = listen_for_lua(args.host, args.port, "items")
    link = handshake(sock, "items")

    bases = locate_saves(link)
    if args.oot_base:
        bases["oot"] = args.oot_base
    if args.mm_base:
        bases["mm"] = args.mm_base
    if "oot" not in bases:
        sys.exit("[items] cannot find OoT's save context")
    for game, addr in sorted(bases.items()):
        activo = " (active)" if addr == min(bases.values()) else ""
        print(f"[items] {game.upper()} save: 0x{addr:08X}{activo}")
    base = bases["oot"]
    mm_base = bases.get("mm")
    print()

    prev = None
    prev_raw = None
    prev_mm = None
    prev_mm_raw = None
    covered = inventory.covered_offsets()
    mm_covered = inventory.mm_covered_offsets()

    # Calibration: the block we read includes volatile state (positions, RNG,
    # timers) that changes every tick and drowns the log. Rather than keeping a
    # list of offsets by hand, we watch what moves on its own for a few seconds
    # and silence it. It is the live equivalent of `diff`'s --exclude.
    noise = set(inventory.NOISE)
    mm_noise = set(inventory.MM_NOISE)
    hits = {}
    if args.calibrate > 0:
        print(f"[items] calibrating noise for {args.calibrate:.0f}s: DO NOT TOUCH ANYTHING...")
        t_end = time.time() + args.calibrate
        ref = link.read_block(base, 0x1500)
        ref_mm = link.read_block(mm_base, 0x1500) if mm_base else None
        muestras = 0
        while time.time() < t_end:
            time.sleep(args.interval)
            muestras += 1
            cur = link.read_block(base, 0x1500)
            noise.update(i for i in range(len(cur)) if cur[i] != ref[i])
            ref = cur
            if mm_base:
                cur_mm = link.read_block(mm_base, 0x1500)
                mm_noise.update(i for i in range(len(cur_mm)) if cur_mm[i] != ref_mm[i])
                ref_mm = cur_mm
        print(
            f"[items] {muestras} samples: silenced {len(noise)} bytes of OoT "
            f"and {len(mm_noise)} of MM\n"
        )
    t0 = time.time()
    try:
        while True:
            save = link.read_block(base, 0x1500)

            # Crossing between OoT and MM reorganises RAM and the bases move.
            # The signature travels inside the very block we just read, so
            # checking it does not cost a single extra read.
            if save[SIG_OFFSET : SIG_OFFSET + 6] != SIG_OOT:
                print("\n[items] the signature is no longer where it was: you switched games.")
                print("[items] relocating...")
                bases = locate_saves(link)
                if "oot" not in bases:
                    print("[items] cannot find OoT's save; retrying in 2s")
                    time.sleep(2)
                    continue
                base = bases["oot"]
                mm_base = bases.get("mm")
                for g, a in sorted(bases.items()):
                    print(f"[items] {g.upper()} save: 0x{a:08X}")
                print()
                prev = prev_mm = prev_raw = prev_mm_raw = None
                continue

            st = inventory.snapshot(save)

            if prev is None:
                print("=== initial state (only what is not empty) ===")
                for k, v in st.items():
                    if v not in (0, 0xFF):
                        print(f"  {k:32} {inventory.fmt(k, v)}")
                print("\n=== changes (Ctrl+C to quit) ===")
            else:
                t = time.time() - t0
                for k, v in st.items():
                    if prev[k] != v:
                        flecha = "+" if v and not prev[k] else " "
                        print(
                            f"[{t:7.1f}s] {flecha} {k:32} "
                            f"{inventory.fmt(k, prev[k])} -> {inventory.fmt(k, v)}"
                        )
                # and anything that changes outside the named fields: that is
                # what we cannot read yet, and it must not slip by unnoticed
                if not args.no_raw:
                    for off, a, b in inventory.unmapped_changes(prev_raw, save, covered, noise):
                        # An item is picked up once; a counter changes
                        # nonstop. On the third change we call the byte noise
                        # and silence it for the rest of the session.
                        hits["oot", off] = hits.get(("oot", off), 0) + 1
                        if hits["oot", off] > args.max_hits:
                            noise.add(off)
                            print(f"[{t:7.1f}s] . +0x{off:04X} changes nonstop: silencing it")
                            continue
                        etiqueta = inventory.perm_label(off)
                        extra = f"  [{etiqueta}]" if etiqueta else ""
                        print(
                            f"[{t:7.1f}s] ? unidentified  +0x{off:04X} "
                            f"(0x{base + off:08X})  {a:02X} -> {b:02X}{extra}"
                        )
            # MM: barely anything mapped yet, so almost all of it goes raw
            mm_save = link.read_block(mm_base, 0x1500) if mm_base else None
            if mm_save is not None:
                mm_st = inventory.mm_snapshot(mm_save)
                if prev_mm is not None:
                    t = time.time() - t0
                    for k, v in mm_st.items():
                        if prev_mm[k] != v:
                            print(
                                f"[{t:7.1f}s] + MM {k:29} "
                                f"{inventory.fmt(k, prev_mm[k], 'mm')} -> "
                                f"{inventory.fmt(k, v, 'mm')}"
                            )
                    if not args.no_raw:
                        for off, a, b in inventory.unmapped_changes(
                            prev_mm_raw, mm_save, mm_covered, mm_noise
                        ):
                            hits["mm", off] = hits.get(("mm", off), 0) + 1
                            if hits["mm", off] > args.max_hits:
                                mm_noise.add(off)
                                print(f"[{t:7.1f}s] . MM +0x{off:04X} changes nonstop: silencing it")
                                continue
                            print(
                                f"[{t:7.1f}s] ? MM unidentified +0x{off:04X} "
                                f"(0x{mm_base + off:08X})  {a:02X} -> {b:02X}"
                            )
                prev_mm = mm_st
                prev_mm_raw = mm_save

            prev = st
            prev_raw = save
            time.sleep(args.interval)
    except (ConnectionError, OSError) as ex:
        print(f"[items] connection lost: {ex}")
    except KeyboardInterrupt:
        pass


def cmd_checks(args):
    import paths

    with open(paths.user("checks.json"), encoding="utf-8") as fh:
        table = json.load(fh)

    resolved = [c for c in table["checks"] if c["addr"] is not None]
    if not resolved:
        sys.exit("checks.json has no resolved addresses; run mkchecks.py")

    lo = min(c["addr"] for c in resolved)
    hi = max(c["addr"] for c in resolved) + 4

    if args.dump:
        data = open(args.dump, "rb").read()
        base = args.base

        def word(addr):
            off = addr - base
            if off < 0 or off + 4 > len(data):
                return None
            return struct.unpack_from(">I", data, off)[0]
    else:
        sock = listen_for_lua(args.host, args.port, "checks")
        link = handshake(sock, "checks")
        blob = link.read_block(lo, ((hi - lo) + 3) // 4 * 4)

        def word(addr):
            return struct.unpack_from(">I", blob, addr - lo)[0]

    spoiler = load_spoiler(args.spoiler) if args.spoiler else {}

    done, pending, unreadable = [], [], 0
    for c in resolved:
        # "u32be" are the per-scene table fields; "u8" the BITMAP8 bitmaps of
        # the custom save, where the address already points at the byte.
        w = word(c["addr"])
        if w is None:
            unreadable += 1
            continue
        if c.get("kind") == "u8":
            w >>= 24  # the address's byte is the most significant of the u32
        (done if w & (1 << c["bit"]) else pending).append(c)

    if unreadable:
        print(f"[checks] {unreadable} checks outside the dump range\n")

    total = len(done) + len(pending)
    pct = 100 * len(done) / total if total else 0
    print(f"CHECKS: {len(done)} / {total} mapped  ({pct:.1f}%)\n")

    # scene ids repeat across games (0x2D is Kokiri Shop in OoT and Termina
    # Field in MM), so the game is part of the key
    by_scene = {}
    for c in done:
        by_scene.setdefault((c["game"], c["scene_id"], c["scene"]), []).append(c)

    # xflags carry their global bit position; for the rest the bit within the
    # field is identifier enough
    def slot(c):
        return c.get("bitpos", c["bit"])

    for (game, sid, sname), group in sorted(by_scene.items()):
        print(f"  [{game.upper():3}] {sname}  (scene 0x{sid:02X})")
        width = max(len(c["name"]) for c in group)
        for c in sorted(group, key=slot):
            item = spoiler.get((c["game"], c["name"]))
            suffix = f"  ->  {item}" if item else ""
            print(f"    [x] bit {slot(c):5d}  {c['name']:<{width}}{suffix}")
        print()

    if args.pending:
        print(f"--- pending ({len(pending)}) ---")
        for c in sorted(pending, key=lambda x: (x["scene_id"], slot(x))):
            item = spoiler.get((c["game"], c["name"]))
            suffix = f"   -> {item}" if item else ""
            print(f"    [ ] {c['name']}{suffix}")


def cmd_overlay(args):
    """Bring up the watchable tracker: polling thread + server + window."""
    import overlay

    # With no --rom or --spoiler they are worked out from what the emulator
    # already knows, and the tables get regenerated if they are another seed's.
    spoiler_path = args.spoiler
    if not args.no_auto:
        try:
            import discover

            rom_path, spoiler_path, hecho = discover.resolve(args.rom, args.spoiler)
            # Always said, --rom or not: the one time the tables came out as
            # another seed's, nothing in the console named the ROM they were
            # built from, and that is the first thing anyone needs to see.
            print(f"[auto] ROM: {rom_path or '(none found)'}")
            if hecho:
                print(f"[auto] rebuilt: {', '.join(hecho)}")
            # From the .exe the script is inside the bundle, so nobody can copy
            # it by hand: put it where the emulator will look for it.
            discover.ensure_lua()
        except Exception as ex:
            print(f"[auto] auto-detection failed ({ex}); carrying on with whatever is there")

    table = overlay.load_table()
    resolved = sum(1 for c in table["checks"] if c["addr"] is not None)
    print(f"[overlay] checks.json: {resolved} of {len(table['checks'])} with an address")

    spoiler = load_spoiler(spoiler_path) if spoiler_path else {}
    if spoiler_path:
        print(f"[overlay] spoiler: {len(spoiler)} locations")

    # The page goes up FIRST, before the emulator side has connected. Waiting
    # for the Lua here used to mean no HTTP server at all until the script was
    # running, so anyone who opened the tracker before the emulator got a
    # refused connection and an OBS Browser Source that stayed blank without
    # saying why.
    tracker = overlay.Tracker(None, table, spoiler=spoiler, locate=locate_saves)

    def wait_for_lua():
        try:
            sock = listen_for_lua(args.host, args.port, "overlay")
            tracker.attach(handshake(sock, "overlay"))
        except SystemExit as ex:
            # handshake reports with sys.exit, which from a thread kills only
            # the thread: without catching it the page would wait for ever.
            tracker.fail(str(ex.code) if ex.code else "the Lua handshake failed")
        except Exception as ex:
            tracker.fail(f"{type(ex).__name__}: {ex}")

    threading.Thread(target=wait_for_lua, daemon=True).start()
    threading.Thread(target=tracker.run, args=(args.interval,), daemon=True).start()

    try:
        overlay.serve(tracker, args.http_host, args.http_port, open_window=not args.no_window)
    except KeyboardInterrupt:
        print("\n[overlay] closed")


def cmd_install_lua(args):
    """Put tracker.lua where Project64-EM's script menu will find it."""
    import discover

    # A wrong --emu must not fall back to the detected emulator: it is only a
    # hint for the search, so pointing it at the wrong folder would quietly
    # install the script somewhere else and report success.
    if args.emu and not os.path.isfile(os.path.join(args.emu, "Config", "Project64.cfg")):
        sys.exit(f"{args.emu} does not look like the emulator folder"
                 " (no Config\\Project64.cfg in it)")

    dst, status = discover.ensure_lua(args.emu, force=args.force)
    if not dst:
        sys.exit("cannot find the emulator; pass --emu with its folder")
    if status is None:
        sys.exit(f"could not write {dst}")
    print({
        "written": f"tracker.lua -> {dst}",
        "same": f"tracker.lua was already there, same version: {dst}",
        "kept": f"tracker.lua left alone (another version): {dst}",
    }[status])
    if status != "kept":
        print("In the emulator, with the ROM loaded: File > Lua Scripts...,"
              " and run tracker.lua")


def cmd_find(args):
    """Search a dump for a pattern. Used to locate the save context by signature
    rather than by fixed offset, which survives version changes."""
    data = open(args.dump, "rb").read()

    if args.hex:
        try:
            pat = bytes.fromhex(args.pattern.replace(" ", ""))
        except ValueError:
            sys.exit(f"invalid hex pattern: {args.pattern!r}")
    else:
        pat = args.pattern.encode("ascii")

    # We un-swap the buffer, not the pattern: that way it works with patterns
    # of any length and the offset found is already the real memory one.
    variants = [("tal cual", data)]
    if args.swapped:
        sw = bytearray(len(data))
        for i in range(0, len(data) - 3, 4):
            sw[i : i + 4] = data[i : i + 4][::-1]
        variants.append(("des-swapeado", bytes(sw)))

    print(f"[find] pattern: {pat.hex(' ')}  ({len(pat)} bytes)\n")
    total = 0
    for label, buf in variants:
        offs = []
        start = 0
        while len(offs) < args.max_hits:
            i = buf.find(pat, start)
            if i < 0:
                break
            offs.append(i)
            start = i + 1
        print(f"[find] {label:14} -> {len(offs)} matches")
        for i in offs:
            print(f"           0x{args.base + i:08X}  (+0x{i:X})")
        total += len(offs)

    if not total:
        print("\n[find] nothing. Try --swapped, or check that the save is loaded.")


def cmd_diff(args):
    a = open(args.a, "rb").read()
    b = open(args.b, "rb").read()
    n = min(len(a), len(b))
    if len(a) != len(b):
        print(f"[diff] different sizes ({len(a)} vs {len(b)}), comparing the first {n} bytes\n")

    # Narrow to the requested regions; without --range the whole dump is used.
    if args.range:
        spans = []
        for r in args.range:
            if r in SAVE_REGIONS:
                addr, ln = SAVE_REGIONS[r]
            else:
                addr, ln = parse_region(r)
            spans.append((addr - args.base, ln))
            print(f"[diff] region 0x{addr:08X} +0x{ln:X}" + (f"  ({r})" if r in SAVE_REGIONS else ""))
    else:
        spans = [(0, n)]

    # Bytes that change on their own, with nothing done: pure noise to drop.
    # We walk with zip over slices rather than indexing: on 8 MB dumps that is
    # the difference between tens of seconds and less than one.
    noise = set()
    if args.exclude:
        c = open(args.exclude, "rb").read()
        for lo, ln in spans:
            lo = max(0, lo)
            hi = min(lo + ln, n, len(c))
            for k, (x, y) in enumerate(zip(a[lo:hi], c[lo:hi])):
                if x != y:
                    noise.add(lo + k)
        print(f"[diff] dropping {len(noise)} bytes that were already changing on their own ({args.exclude})")

    hits = []
    for lo, ln in spans:
        lo = max(0, lo)
        hi = min(lo + ln, n)
        for k, (x, y) in enumerate(zip(a[lo:hi], b[lo:hi])):
            if x == y:
                continue
            i = lo + k
            if i in noise:
                continue
            # A check bitfield only accumulates: a bit that goes to 1 does
            # not come back. We drop every byte that loses any bit, even if it
            # gains others.
            if (args.bits_set or args.one_bit) and (a[i] & ~b[i]) != 0:
                continue
            # Marking a check sets exactly one bit. Audio and video buffers
            # change arbitrarily, so this throws them out.
            if args.one_bit and (b[i] & ~a[i]).bit_count() != 1:
                continue
            hits.append(i)
    hits.sort()

    if not hits:
        print("\n[diff] no differences with the filters applied.")
        return
    hitset = set(hits)

    # Agrupar offsets contiguos en rachas.
    runs = []
    start = prev = hits[0]
    for i in hits[1:]:
        if i - prev <= args.gap:
            prev = i
        else:
            runs.append((start, prev))
            start = prev = i
    runs.append((start, prev))

    # A check flag is a lone byte. A wide run is a buffer.
    if args.max_run:
        runs = [(lo, hi) for lo, hi in runs if sum(1 for i in hits if lo <= i <= hi) <= args.max_run]
        keep = {i for i in hits for lo, hi in runs if lo <= i <= hi}
        hits = [i for i in hits if i in keep]
        hitset = keep
        if not hits:
            print(f"\n[diff] nothing survives --max-run {args.max_run}.")
            return

    what = "bytes that only gain bits" if (args.bits_set or args.one_bit) else "bytes differ"
    plural = "run" if len(runs) == 1 else "runs"
    print(f"[diff] {len(hits)} {what} in {len(runs)} {plural}\n")
    shown = 0
    for lo, hi in runs:
        count = sum(1 for i in range(lo, hi + 1) if i in hitset)
        print(f"  0x{args.base + lo:08X}  (+0x{lo:X})  {count} bytes in {hi - lo + 1}")
        if args.quiet:
            continue
        for i in range(lo, min(hi + 1, lo + args.max_bytes)):
            if i not in hitset:  # only what passes the filters, not the whole run
                continue
            diff_bits = b[i] & ~a[i]
            extra = f"   bits nuevos: {diff_bits:08b}" if diff_bits else ""
            print(f"      0x{args.base + i:08X}  {a[i]:02X} -> {b[i]:02X}{extra}")
        shown += 1
        if shown >= args.max_runs:
            print(f"\n  ... {len(runs) - shown} more runs (use --max-runs to see more)")
            break


# --------------------------------------------------------------------------
# Proxy mode (with adapter-tracker.lua, not with tracker.lua)
# --------------------------------------------------------------------------


def parse_commands(buf, cap, state):
    i = 0
    while i < len(buf):
        op = buf[i]
        if op not in OPS:
            raise ValueError(f"opcode desconocido 0x{op:02X}")
        name, arglen, resplen = OPS[op]
        if len(buf) - i - 1 < arglen:
            break
        args = buf[i + 1 : i + 1 + arglen]
        addr, endian = cap.decode_addr(args[:4])
        if addr is None:
            raise ValueError(f"address outside RDRAM in op {name}")
        if resplen:
            state["pending"].append((name, addr, resplen, endian))
        else:
            size = arglen - 4
            cap.record(name, addr, struct.unpack(endian + FMT[size], args[4:])[0], size)
        i += 1 + arglen
    return buf[i:]


def pump_commands(src, dst, cap, state):
    """MultiClient -> Lua. Parse before forwarding, so no replies get lost."""
    buf = b""
    try:
        while True:
            data = src.recv(8192)
            if not data:
                break
            if state["parsing"]:
                buf += data
                try:
                    buf = parse_commands(buf, cap, state)
                except Exception as ex:
                    print(f"[proxy] parsing disabled ({ex}); still forwarding")
                    state["parsing"] = False
                    buf = b""
            dst.sendall(data)
    except OSError:
        pass
    finally:
        state["done"].set()


def pump_responses(src, dst, cap, state):
    """Lua -> MultiClient. Pair each reply with its pending read."""
    buf = b""
    try:
        while True:
            data = src.recv(8192)
            if not data:
                break
            dst.sendall(data)
            if not state["parsing"]:
                continue
            buf += data
            while state["pending"] and len(buf) >= state["pending"][0][2]:
                name, addr, n, endian = state["pending"].pop(0)
                cap.record(name, addr, struct.unpack(endian + FMT[n], buf[:n])[0], n)
                buf = buf[n:]
    except OSError:
        pass
    finally:
        state["done"].set()


def cmd_proxy(args):
    cap = Capture(args.jsonl, log_all=args.all, verbose=args.verbose)
    ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind((args.host, args.port))
    ls.listen(1)
    ls.settimeout(0.5)
    print(f"[proxy] listening on {args.host}:{args.port} (adapter-tracker.lua)")
    print(f"[proxy] forwarding to {args.host}:{args.upstream} (MultiClient)")
    print("[proxy] start adapter-tracker.lua in P64-EM. Ctrl+C for the summary.\n")

    try:
        while True:
            try:
                lua, _ = ls.accept()
            except socket.timeout:
                continue
            lua.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            print("[proxy] Lua script connected")

            try:
                up = socket.create_connection((args.host, args.upstream), timeout=5)
                up.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError as ex:
                print(f"[proxy] cannot connect to MultiClient at {args.upstream}: {ex}")
                print("[proxy] start it first, or use 'watch'/'dump' with tracker.lua")
                lua.close()
                continue

            print("[proxy] MultiClient connected - capturing\n")
            state = {"pending": [], "parsing": True, "done": threading.Event()}
            for fn, a, b in ((pump_commands, up, lua), (pump_responses, lua, up)):
                threading.Thread(target=fn, args=(a, b, cap, state), daemon=True).start()
            while not state["done"].wait(0.5):
                pass
            for s in (lua, up):
                try:
                    s.close()
                except OSError:
                    pass
            print("[proxy] session closed; waiting for a new connection\n")
    finally:
        finish(cap, args)


def finish(cap, args):
    text = cap.summary(gap=args.gap)
    print(text)
    if args.summary:
        with open(args.summary, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"[ootmm] summary in {args.summary}, events in {args.jsonl}")


# --------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_capture_opts(sp):
        sp.add_argument("--jsonl", default="capture.jsonl", help="events (def: capture.jsonl)")
        sp.add_argument("--summary", default="summary.txt", help="summary (def: summary.txt)")
        sp.add_argument("--all", action="store_true", help="log everything, not only the changes")
        sp.add_argument("--gap", type=int, default=128, help="gap for grouping a region (def: 128)")

    w = sub.add_parser("watch", help="poll addresses with tracker.lua")
    w.add_argument("targets", metavar="ADDR:SIZE,...", help="e.g. 0x80000318:4,0x8011A5D0:2")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=13251)
    w.add_argument("--interval", type=float, default=0.25, help="seconds between polls")
    add_capture_opts(w)
    w.set_defaults(func=cmd_watch)

    d = sub.add_parser("dump", help="dump a region of RDRAM with tracker.lua")
    d.add_argument(
        "region",
        metavar="ADDR:LEN|nombre",
        help="e.g. 0x80000000:0x400000, or a region: " + ", ".join(SAVE_REGIONS),
    )
    d.add_argument("-o", "--out", default="dump.bin")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=13251)
    d.set_defaults(func=cmd_dump)

    it = sub.add_parser("items", help="read the inventory in a loop and call every change")
    it.add_argument("--dump", help="read from a dump instead of live")
    it.add_argument("--base", type=resolve_base, default=0x80000000)
    it.add_argument("--interval", type=float, default=0.5, help="seconds between reads")
    it.add_argument("--no-raw", action="store_true", help="do not report unidentified bytes")
    it.add_argument(
        "--calibrate",
        type=float,
        default=6.0,
        metavar="SECS",
        help="seconds of noise calibration at startup; 0 disables it (def: 6)",
    )
    it.add_argument(
        "--max-hits",
        type=int,
        default=3,
        help="changes of an unidentified byte before calling it noise (def: 3)",
    )
    it.add_argument("--oot-base", type=lambda x: int(x, 0), help="force OoT's save base")
    it.add_argument("--mm-base", type=lambda x: int(x, 0), help="force MM's save base")
    it.add_argument("--host", default="127.0.0.1")
    it.add_argument("--port", type=int, default=13251)
    it.set_defaults(func=cmd_items)

    k = sub.add_parser("checks", help="list the completed checks, by name")
    k.add_argument("--dump", help="read from a dump instead of live")
    k.add_argument("--base", type=resolve_base, default=0x80000000, help="address of the dump's offset 0")
    k.add_argument("--spoiler", help="spoiler log, to show what item sits in each check")
    k.add_argument("--pending", action="store_true", help="list the incomplete ones too")
    k.add_argument("--host", default="127.0.0.1")
    k.add_argument("--port", type=int, default=13251)
    k.set_defaults(func=cmd_checks)

    o = sub.add_parser("overlay", help="the watchable tracker: server + its own window")
    o.add_argument("--rom", help="the seed's ROM; detected on its own by default")
    o.add_argument("--spoiler", help="spoiler log; by default looked for next to the ROM")
    o.add_argument("--no-auto", action="store_true",
                   help="do not detect anything nor regenerate tables")
    o.add_argument("--http-port", type=int, default=8013, help="overlay port")
    o.add_argument("--http-host", default="127.0.0.1")
    o.add_argument("--interval", type=float, default=0.5, help="seconds between polls")
    o.add_argument("--no-window", action="store_true", help="do not open a window, just serve")
    o.add_argument("--host", default="127.0.0.1")
    o.add_argument("--port", type=int, default=13251, help="port tracker.lua connects to")
    o.set_defaults(func=cmd_overlay)

    n = sub.add_parser("find", help="search a dump for a signature")
    n.add_argument("dump")
    n.add_argument("pattern", help="ASCII text, or hex bytes with --hex")
    n.add_argument("--hex", action="store_true", help="the pattern is hex bytes")
    n.add_argument("--base", type=resolve_base, default=0x80000000)
    n.add_argument("--swapped", action="store_true", help="also try byte-swapping each 4-byte word")
    n.add_argument("--max-hits", type=int, default=50)
    n.set_defaults(func=cmd_find)

    f = sub.add_parser("diff", help="compare two dumps")
    f.add_argument("a")
    f.add_argument("b")
    f.add_argument(
        "--base",
        type=resolve_base,
        default=0x80000000,
        help="address of offset 0, or region name: " + ", ".join(SAVE_REGIONS),
    )
    f.add_argument(
        "--range",
        action="append",
        metavar="ADDR:LEN|nombre",
        help="narrow to a region; repeatable. Names: " + ", ".join(SAVE_REGIONS),
    )
    f.add_argument(
        "--exclude",
        metavar="RUIDO.BIN",
        help="a third dump with nothing done: drops whatever changes on its own",
    )
    f.add_argument(
        "--one-bit",
        action="store_true",
        help="only bytes that set exactly one bit (the signature of a flag)",
    )
    f.add_argument(
        "--max-run",
        type=int,
        metavar="N",
        help="drop runs with more than N changed bytes (buffers are wide)",
    )
    f.add_argument(
        "--bits-set",
        action="store_true",
        help="only bytes that gain bits without losing any (the signature of a check bitfield)",
    )
    f.add_argument("--gap", type=int, default=16, help="gap for grouping a run (def: 16)")
    f.add_argument("--max-runs", type=int, default=40)
    f.add_argument("--max-bytes", type=int, default=32, help="detailed bytes per run")
    f.add_argument("--quiet", "-q", action="store_true", help="runs only, no detail")
    f.set_defaults(func=cmd_diff)

    lu = sub.add_parser("install-lua", help="copy tracker.lua into the emulator's Scripts folder")
    lu.add_argument("--emu", help="emulator folder; found on its own by default")
    lu.add_argument("--force", action="store_true", help="replace it even if one is already there")
    lu.set_defaults(func=cmd_install_lua)

    x = sub.add_parser("proxy", help="capture the MultiClient traffic")
    x.add_argument("--host", default="127.0.0.1")
    x.add_argument("--port", type=int, default=13250, help="where the Lua connects (def: 13250)")
    x.add_argument("--upstream", type=int, default=13249, help="where MultiClient listens (def: 13249)")
    x.add_argument("--verbose", "-v", action="store_true", help="print operations live")
    add_capture_opts(x)
    x.set_defaults(func=cmd_proxy)

    # Double-clicking the .exe passes no arguments, and a subcommand is
    # required: it would print the usage and shut the window before anyone
    # could read it. From the executable, no arguments means the overlay,
    # which is what it is for. From source it still prints the usage.
    import paths

    argv = sys.argv[1:]
    if paths.FROZEN and not argv:
        argv = ["overlay"]

    args = p.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        pass


def _run():
    """`main()`, plus keeping the console open when there is nobody to read it.

    A .exe launched from Explorer takes its console with it when it exits, so
    an error message would flash by. That is the case worth holding: no
    arguments (which is what a double-click looks like) or a non-zero exit.
    Typing a subcommand at a shell already leaves the output on screen, so
    there it just exits.
    """
    import paths

    def hold(code):
        if not paths.FROZEN:
            return False
        if sys.argv[1:] and not code:
            return False
        try:
            return sys.stdin is not None and sys.stdin.isatty()
        except (AttributeError, ValueError):
            return False

    code = 0
    try:
        main()
    except SystemExit as ex:
        if ex.code is None:
            code = 0
        elif isinstance(ex.code, int):
            code = ex.code
        else:
            # `sys.exit("message")` is used all over as the way to fail with an
            # explanation, and the message is printed by the interpreter *only*
            # if nobody catches it. Catching it here and staying quiet would
            # turn every one of those into an exit code and nothing else.
            print(ex.code, file=sys.stderr)
            code = 1
    except Exception:
        if not paths.FROZEN:
            raise
        import traceback

        traceback.print_exc()
        code = 1
    if hold(code):
        try:
            input("\n-- press Enter to close --")
        except (EOFError, KeyboardInterrupt):
            pass
    sys.exit(code)


if __name__ == "__main__":
    _run()
