#!/usr/bin/env python3
"""
payload.py - where OoTMM's own globals live, read out of the payload's code.

The tracker needs three RAM addresses that are not vanilla's: the save buffer
of the game that is NOT running (`gMmSave` when OoT runs, `gOotSave` when MM
runs), `gSharedCustomSave` (xflags, npc, shops, scrubs, silver rupees, MM's
half-days...) and the layout inside that struct. All three are globals of the
payload, so they move whenever OoTMM's build changes -- v32.0 and dev-542a121
already differ by 0x30 -- and until now they were either constants
(`KNOWN_BASES`, `CUSTOM_OOT`, `CUSTOM_MM`) or measured at run time from the
bits that happen to be set, which is exactly the signal a fresh save does not
have.

But the payload is MIPS code linked at a fixed address, and code that touches
a global carries its address in the instructions themselves (`lui` for the
high half, `addiu`/`lw`/`sb`... for the low half). `save.c` in particular does

    Flash_ReadWrite(0x8000 + 0x8000 * fileIndex,  &gMmSave,           sizeof(gMmSave),           OS_READ);
    Flash_ReadWrite(0x18000 + 0x4000 * fileIndex, &gSharedCustomSave, sizeof(gSharedCustomSave), OS_READ);

so the same call site hands over both the address and the size. Nothing here
compares against an expected value: `Flash_ReadWrite` is recognised by how it
is called, the three buffers by where they sit and which sizes repeat between
the two payloads, and the fields inside the custom save by the shape of the
struct (an array of N bytes followed by arrays of 32, 8, 8 and 16). The old
constants stay in mkchecks.py / ootmm.py as the cross-check, the way the
xflag-table VROMs do.

What comes out (per payload, i.e. per *running* game):

    own       gSaveContext of the running game -- vanilla, outside the payload
    foreign   the other game's save buffer, inside the payload's .bss
    custom    gSharedCustomSave, same size in both payloads
    layout    offsets of the arrays inside gSharedCustomSave

Everything is measured over the corpus of 42 seeds (see FINDINGS in the
corpus folder) and against the four RAM dumps before being trusted.
"""

import argparse
import collections
import json
import pathlib
import struct

import rom as romlib

# combo/defs.h. PAYLOAD_RAM/PAYLOAD_SIZE: where the payload is loaded and the
# most it may take, .bss included. These are link-time constants of the
# generator, not addresses of one version, and the MM one is derived
# (0x80780000 - 0x60000) exactly as defs.h does it.
PAYLOAD = {
    "oot": {"vrom": 0xF0000000, "ram": 0x80400000, "size": 0x80000},
    "mm": {"vrom": 0xF0100000, "ram": 0x80780000 - 0x60000, "size": 0x60000},
}
RDRAM_LO, RDRAM_HI = 0x80000000, 0x80800000

# The tracker's "MM save base" is the address that puts the ZELDA3 signature at
# +0x1C, the same offset OoT has it at, and that is MmSave + 8: vanilla MM keeps
# `newf` at 0x24. It is a property of the vanilla struct, not of OoTMM.
MM_BASE_DELTA = 8

# --------------------------------------------------------------------------
# MIPS, the little that is needed
# --------------------------------------------------------------------------

_LOADSTORE = {
    0x20: "lb", 0x21: "lh", 0x22: "lwl", 0x23: "lw", 0x24: "lbu", 0x25: "lhu",
    0x26: "lwr", 0x28: "sb", 0x29: "sh", 0x2A: "swl", 0x2B: "sw", 0x2E: "swr",
    0x31: "lwc1", 0x35: "ldc1", 0x37: "ld", 0x39: "swc1", 0x3D: "sdc1", 0x3F: "sd",
}


def _sext16(v):
    return v - 0x10000 if v & 0x8000 else v


def _written(ins):
    """The GPR this instruction writes, or None. Approximate but careful with
    the cases that matter: stores, branches and jr write nothing; jal writes
    ra; COP1 only reaches a GPR through mfc1/cfc1."""
    op = ins >> 26
    rs = (ins >> 21) & 31
    rt = (ins >> 16) & 31
    rd = (ins >> 11) & 31
    if op == 0:
        fn = ins & 0x3F
        if fn == 0x08:                          # jr
            return None
        if fn == 0x09:                          # jalr
            return rd
        if fn in (0x0C, 0x0D, 0x11, 0x13, 0x18, 0x19, 0x1A, 0x1B):
            return None                         # syscall/break/mthi/mtlo/mult/div
        return rd
    if op == 1:                                 # REGIMM: the *al forms link
        return 31 if rt in (0x10, 0x11, 0x12, 0x13) else None
    if op == 3:                                 # jal
        return 31
    if op in (2, 4, 5, 6, 7, 0x14, 0x15, 0x16, 0x17):
        return None                             # j and branches
    if op in (0x28, 0x29, 0x2A, 0x2B, 0x2E, 0x2F, 0x39, 0x3D, 0x3F):
        return None                             # stores, cache
    if op == 0x11:                              # COP1
        return rt if rs in (0, 2) else None
    return rt


_CALLER_SAVED = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 24, 25, 31)


class Ref:
    """One instruction that touches an absolute address.

    `addr` is the full address the instruction forms. `sym` is the address the
    register held before the displacement, when that is known -- for
    `lui/addiu` it is `addr` itself (the address is being taken), for
    `lw v1, 0x1354(s0)` with s0 = &gSaveContext it is &gSaveContext -- so
    `addr - sym` is the field offset. `indexed` says a register was added on
    the way, i.e. the address is the base of an array.
    """
    __slots__ = ("pc", "kind", "addr", "sym", "reg", "indexed")

    def __init__(self, pc, kind, addr, sym, reg, indexed):
        self.pc, self.kind, self.addr, self.sym, self.reg, self.indexed = pc, kind, addr, sym, reg, indexed

    def __repr__(self):
        return f"Ref({self.pc:#x} {self.kind} {self.addr:#x} sym={self.sym:#x} +{self.addr - self.sym:#x}{' []' if self.indexed else ''})"


class Call:
    """A jal (or a tail-call j) with what the argument registers held."""
    __slots__ = ("pc", "target", "tail", "args")

    def __init__(self, pc, target, tail, args):
        self.pc, self.target, self.tail, self.args = pc, target, tail, args

    def __repr__(self):
        return f"Call({self.pc:#x} -> {self.target:#x}{' tail' if self.tail else ''} {self.args})"


class Scan:
    """The payload of one game, disassembled far enough to see its globals."""

    def __init__(self, rom_bytes, game):
        p = PAYLOAD[game]
        self.game = game
        self.ram = p["ram"]
        self.size = p["size"]
        self.blob = romlib.read_extra_vrom(rom_bytes, p["vrom"])
        n = len(self.blob) // 4
        self.words = struct.unpack_from(f">{n}I", self.blob, 0)
        self.refs = []
        self.calls = []
        self._scan()

    # -- the linear pass ---------------------------------------------------
    def _scan(self):
        words, ram = self.words, self.ram
        hi = {}     # reg -> high half from a lui, waiting for its low half
        hi_idx = set()  # regs whose high half came through an addu (an index was added)
        full = {}   # reg -> (symbol address, indexed) once the low half arrived
        writes = {} # reg -> ("li", value) | ("addr", addr) | ("expr",) last write, for the calls
        clobber_after = None  # index after which a call has clobbered the caller-saved registers
        for i, ins in enumerate(words):
            pc = ram + i * 4
            if clobber_after is not None and i > clobber_after:
                # a0..a3, v0/v1, t0..t9, at, ra are the callee's to trash
                for r in _CALLER_SAVED:
                    writes.pop(r, None)
                    hi.pop(r, None)
                    hi_idx.discard(r)
                    full.pop(r, None)
                clobber_after = None
            op = ins >> 26
            rs = (ins >> 21) & 31
            rt = (ins >> 16) & 31
            rd = (ins >> 11) & 31
            imm = ins & 0xFFFF
            fn = ins & 0x3F

            if op == 0x0F:                                      # lui
                hi[rt] = imm
                hi_idx.discard(rt)
                full.pop(rt, None)
                writes[rt] = ("expr",)
                continue

            if op in (3, 2):                                    # jal / j
                target = ((pc + 4) & 0xF0000000) | ((ins & 0x3FFFFFF) << 2)
                # the delay slot still belongs to the call
                delay = words[i + 1] if i + 1 < len(words) else 0
                self._note_call(pc, target, op == 2, writes, hi, full, delay, i + 1)
                if op == 3:
                    clobber_after = i + 1       # once the delay slot has run

            new_full = None
            recorded = False
            if op == 0x09 or op == 0x0D:                        # addiu / ori
                if rs in hi:
                    addr = (hi[rs] << 16) + (_sext16(imm) if op == 0x09 else imm)
                    addr &= 0xFFFFFFFF
                    self.refs.append(Ref(pc, "addiu" if op == 0x09 else "ori", addr, addr, rt, rs in hi_idx))
                    new_full = (addr, rs in hi_idx)
                    writes[rt] = ("addr", addr)
                    recorded = True
                elif rs == 0:
                    writes[rt] = ("li", _sext16(imm) if op == 0x09 else imm)
                    recorded = True
                elif rs in full:
                    # pointer arithmetic on a known symbol: keep following it
                    sym, idx = full[rs]
                    new_full = (sym, idx)
                    writes[rt] = ("expr",)
                    recorded = True
            elif op in _LOADSTORE:
                kind = _LOADSTORE[op]
                if rs in hi:
                    # lui / (addu idx) / lbu lo(reg): the low half is in the load
                    addr = ((hi[rs] << 16) + _sext16(imm)) & 0xFFFFFFFF
                    self.refs.append(Ref(pc, kind, addr, addr, rt, rs in hi_idx))
                elif rs in full:
                    sym, idx = full[rs]
                    addr = (sym + _sext16(imm)) & 0xFFFFFFFF
                    self.refs.append(Ref(pc, kind, addr, sym, rt, idx))
            elif op == 0 and fn in (0x21, 0x25, 0x2D) and (rs == 0 or rt == 0):
                # `move rd, rX` is `or rd, rX, zero` (or addu); `li rd, 0` is
                # `or rd, zero, zero`. The value travels with it.
                src_reg = rt if rs == 0 else rs
                if src_reg == 0:
                    writes[rd] = ("li", 0)
                elif src_reg in writes:
                    writes[rd] = writes[src_reg]
                else:
                    writes[rd] = ("expr",)
                recorded = True
                if src_reg in full:
                    new_full = full[src_reg]
                elif src_reg in hi:
                    new_full = ("hi", hi[src_reg])
            elif op == 0 and fn in (0x21, 0x2D):                # addu / daddu
                # base + index: the result still points into the same symbol
                for r in (rs, rt):
                    if r in hi:
                        # lui + addu + load/store with the low half in the load
                        hi_val = hi[r]
                        new_full = ("hi", hi_val)
                        break
                    if r in full:
                        new_full = (full[r][0], True)
                        break

            w = _written(ins)
            if w is not None:
                hi.pop(w, None)
                hi_idx.discard(w)
                full.pop(w, None)
                if not recorded:
                    writes[w] = ("expr",)
                if new_full is not None:
                    if new_full[0] == "hi":
                        hi[w] = new_full[1]
                        if op == 0 and fn in (0x21, 0x2D) and not (rs == 0 or rt == 0):
                            hi_idx.add(w)       # came through an addu: indexed
                        elif new_full[1] is not None and (rs in hi_idx or rt in hi_idx):
                            hi_idx.add(w)       # a move keeps the flag
                    else:
                        full[w] = new_full

    def _note_call(self, pc, target, tail, writes, hi, full, delay, delay_i):
        """Snapshot a0..a3 as they are at the call, delay slot included."""
        args = {}
        for reg in (4, 5, 6, 7):
            args[reg] = writes.get(reg, ("expr",))
        # a `li`/`addiu` in the delay slot lands before the callee runs
        op = delay >> 26
        rs = (delay >> 21) & 31
        rt = (delay >> 16) & 31
        imm = delay & 0xFFFF
        if rt in (4, 5, 6, 7):
            if op == 0x09 and rs == 0:
                args[rt] = ("li", _sext16(imm))
            elif op == 0x0D and rs == 0:
                args[rt] = ("li", imm)
            elif op == 0x09 and rs in hi:
                args[rt] = ("addr", ((hi[rs] << 16) + _sext16(imm)) & 0xFFFFFFFF)
            elif op == 0x0D and rs in hi:
                args[rt] = ("addr", ((hi[rs] << 16) | imm) & 0xFFFFFFFF)
            elif op == 0x0F:
                args[rt] = ("expr",)
            elif _written(delay) == rt:
                args[rt] = ("expr",)
        rd = (delay >> 11) & 31
        if op == 0 and (delay & 0x3F) in (0x21, 0x25, 0x2D) and rd in (4, 5, 6, 7) and (rs == 0 or rt == 0):
            src_reg = rt if rs == 0 else rs
            args[rd] = ("li", 0) if src_reg == 0 else writes.get(src_reg, ("expr",))
        self.calls.append(Call(pc, target, tail, args))

    # -- what the tracker asks ---------------------------------------------
    def in_payload(self, addr):
        return self.ram <= addr < self.ram + self.size

    def calls_to(self, target):
        return [c for c in self.calls if c.target == target]


# --------------------------------------------------------------------------
# Flash_ReadWrite and the three save buffers
# --------------------------------------------------------------------------

def _flash_sites(scan):
    """{callee: [Call]} restricted to calls that look like
    Flash_ReadWrite(devAddr, &global, sizeof, OS_READ|OS_WRITE)."""
    out = collections.defaultdict(list)
    for c in scan.calls:
        a1, a2, a3 = c.args[5], c.args[6], c.args[7]
        if a1[0] != "addr" or a2[0] != "li" or a3[0] != "li":
            continue
        if not (0x100 <= a2[1] <= 0x8000) or a3[1] not in (0, 1):
            continue
        if not (RDRAM_LO <= a1[1] < RDRAM_HI):
            continue
        out[c.target].append(c)
    return out


def find_flash_readwrite(scan):
    """(address of Flash_ReadWrite, its qualifying call sites) or (None, [])."""
    sites = _flash_sites(scan)
    if not sites:
        return None, []
    # The real one is called with several distinct buffers, some inside the
    # payload (the foreign save, the custom save) and one outside (the running
    # game's own save context). Anything else that happens to match the
    # signature is called with one buffer, if that.
    def score(item):
        tgt, cs = item
        addrs = {c.args[5][1] for c in cs}
        inside = sum(1 for a in addrs if scan.in_payload(a))
        outside = len(addrs) - inside
        return (min(inside, 2) + min(outside, 1), len(cs))
    tgt, cs = max(sites.items(), key=score)
    addrs = {c.args[5][1] for c in cs}
    if sum(1 for a in addrs if scan.in_payload(a)) < 2:
        return None, []
    return tgt, cs


def buffers(scan, other=None):
    """{'own'|'foreign'|'custom': (address, size)} for this payload.

    `other` is the other game's Scan when available: the custom save is the
    buffer whose size is the same in both payloads (it is one shared struct),
    which is what tells it apart from the foreign save without knowing either
    size. Without `other`, the fallback is the flash geometry: the custom save
    is the buffer whose flash offset is fileIndex * 0x4000 (save.c), the
    foreign one uses its own game's stride.
    """
    flash, cs = find_flash_readwrite(scan)
    if flash is None:
        return {}
    sizes = collections.defaultdict(set)
    for c in cs:
        sizes[c.args[5][1]].add(c.args[6][1])
    inside = {a: s for a, s in sizes.items() if scan.in_payload(a)}
    outside = {a: s for a, s in sizes.items() if not scan.in_payload(a)}
    out = {"flash_readwrite": (flash, 0)}
    if outside:
        a = max(outside, key=lambda a: len(outside[a]))
        out["own"] = (a, max(outside[a]))
    if len(inside) < 2:
        return out

    custom = None
    if other is not None:
        _, ocs = find_flash_readwrite(other)
        other_sizes = set()
        for c in ocs:
            if other.in_payload(c.args[5][1]):
                other_sizes.add(c.args[6][1])
        shared = [a for a, s in inside.items() if s & other_sizes]
        if len(shared) == 1:
            custom = shared[0]
    if custom is None:
        # flash geometry: which call shifts its file index by 14 (x 0x4000)
        by_shift = {}
        for c in cs:
            a = c.args[5][1]
            if a in inside:
                sh = _shift_before(scan, c)
                by_shift.setdefault(a, set()).add(sh)
        cands = [a for a, s in by_shift.items() if 14 in s]
        if len(cands) == 1:
            custom = cands[0]
    if custom is None:
        return out
    out["custom"] = (custom, max(inside[custom]))
    foreign = [a for a in inside if a != custom]
    # if more than one is left, take the one adjacent to the custom save: they
    # are defined next to each other in save.c and land next to each other
    foreign.sort(key=lambda a: abs(a - custom))
    out["foreign"] = (foreign[0], max(inside[foreign[0]]))
    return out


def _shift_before(scan, call, window=8):
    """The `sll` amount applied to a0 in the instructions before the call, or
    None. save.c computes the flash offset as (fileIndex + k) << 14 for the
    custom save and (fileIndex + 1) << 15 for MM's."""
    i = (call.pc - scan.ram) // 4
    for k in range(i + 1, max(-1, i - window), -1):
        if k >= len(scan.words):
            continue
        ins = scan.words[k]
        if ins >> 26 == 0 and ins & 0x3F == 0 and (ins >> 11) & 31 == 4:  # sll a0
            return (ins >> 6) & 31
    return None


# --------------------------------------------------------------------------
# The layout of gSharedCustomSave
# --------------------------------------------------------------------------
#
#   typedef struct ALIGNED(16) { u8 xflags[XFLAGS_COUNT_OOT]; u8 npc[32];
#       u8 shops[8]; u8 scrubs[8]; u8 sr[16]; ... } OotCustomSave;
#   typedef struct ALIGNED(16) { u8 xflags[XFLAGS_COUNT_MM];  u8 npc[32];
#       u8 shops[4]; u8 halfDays; ... } MmCustomSave;
#   typedef struct ALIGNED(16) { OotCustomSave oot; MmCustomSave mm; ... }
#
# XFLAGS_COUNT_* is generated per version (xsanity.ts) and it is what shifts
# everything behind it. But the code that marks a check indexes those arrays,
# so their bases show up as `sym + offset` with an index added, and four
# arrays of 32/8/8/16 bytes in a row have a shape: N, N+0x20, N+0x28, N+0x30.

OOT_SHAPE = (0x00, 0x20, 0x28, 0x30)          # npc, shops, scrubs, sr
MM_SHAPE = (0x00, 0x20, 0x24)                  # npc, shops, halfDays


def custom_offsets(scan, custom, size):
    """{offset: (count, indexed_count)} of every reference into the custom save.

    Two addressing forms reach a field: relative to the struct's base held in a
    register (`sym == custom`), or absolute -- `lui`/`addiu` or `lui`/`lbu` of
    the field's own address, with or without an index added on the way
    (`sym == addr`). Both are the same struct; both count.
    """
    out = {}
    for r in scan.refs:
        if not (custom <= r.addr < custom + size):
            continue
        if r.sym == custom or r.sym == r.addr:
            c, ic = out.get(r.addr - custom, (0, 0))
            out[r.addr - custom] = (c + 1, ic + (1 if r.indexed else 0))
    return out


def layout(scan_oot, scan_mm, custom_oot, custom_mm, size):
    """{'oot': {...}, 'mm': {...}} field offsets inside gSharedCustomSave.

    Both payloads carry both games' marking code (xflags.c and mark.c are
    common), so the references are pooled. The oot half always starts at 0.
    Every array in the shape has to be seen **with an index added** -- that is
    what says "array base" rather than "some byte" -- and the answer is the
    smallest fit; the alternatives are kept under `_candidates` so a caller can
    tell a clean hit (one) from a lucky one. Returns None where it could not
    pin something, never a guess.
    """
    idx = collections.Counter()      # offset -> indexed references
    any_ = collections.Counter()     # offset -> references of any kind
    for sc, base in ((scan_oot, custom_oot), (scan_mm, custom_mm)):
        for off, (c, ic) in custom_offsets(sc, base, size).items():
            idx[off] += ic
            any_[off] += c

    out = {"oot": {"xflags": 0}, "mm": {}}
    # OoT: npc[32] shops[8] scrubs[8] sr[16] right behind xflags[N]
    cands = [n for n in range(0x40, size - 0x40)
             if all(idx.get(n + d, 0) > 0 for d in OOT_SHAPE)]
    if cands:
        n = cands[0]
        out["oot"].update({"npc": n, "shops": n + 0x20, "scrubs": n + 0x28,
                           "sr": n + 0x30, "xflags_count": n, "_candidates": cands})
    else:
        out["oot"].update({"npc": None, "shops": None, "scrubs": None,
                           "sr": None, "xflags_count": None, "_candidates": []})

    # MM: at a 16-aligned B past the OoT half, xflags[M] npc[32] shops[4] halfDays
    lo = ((out["oot"].get("sr") or 0x40) + 0x10 + 15) & ~15
    mm_cands = []
    for b in range(lo, size - 0x40, 16):
        if idx.get(b, 0) == 0:
            continue                          # the xflags array itself is indexed
        for m in range(0x40, size - b - 0x30):
            if idx.get(b + m, 0) > 0 and idx.get(b + m + 0x20, 0) > 0                     and any_.get(b + m + 0x24, 0) > 0:
                mm_cands.append((b, m))
    if mm_cands:
        b, m = mm_cands[0]
        out["mm"] = {"base": b, "xflags": b, "npc": b + m, "shops": b + m + 0x20,
                     "halfDays": b + m + 0x24, "xflags_count": m, "_candidates": mm_cands}
    else:
        out["mm"] = {"base": None, "xflags": None, "npc": None, "shops": None,
                     "halfDays": None, "xflags_count": None, "_candidates": []}

    # caughtFishFlags[5], the last check bitmap of the struct. It sits behind
    # caughtChildFishWeight[20] and caughtAdultFishWeight[20] (save.h), and the
    # code indexes it (BITMAP8_SET) and the adult weights (weight[len]); the
    # child array shows mostly as its length byte. So: F indexed, F-0x14
    # indexed, F-0x28 touched, past the MM half. One fit or nothing. Measured:
    # 0x83D on v32.0 (adult 0x829, child 0x815, RespawnData at 0x844 right
    # behind), 0x869 on dev-542a121; older builds have no such trio.
    fish = None
    mm_end = (out["mm"].get("base") or 0) + (out["mm"].get("xflags_count") or 0)
    fish_cands = [f for f in range(mm_end + 0x40, size - 5)
                  if idx.get(f, 0) >= 2 and idx.get(f - 0x14, 0) >= 1 and any_.get(f - 0x28, 0) >= 1]
    if len(fish_cands) == 1:
        fish = fish_cands[0]
    out["oot"]["caughtFishFlags"] = fish
    out["oot"]["_fish_candidates"] = fish_cands

    # The OoT half comes first in the struct, so the smallest fit is the one --
    # but only if every other fit falls inside the MM half (MmCustomSave has
    # npc[32] shops[4] and then arrays too, and on v32.0 that mimics the OoT
    # shape at 0x6D0). A second fit BEFORE the MM base would mean the shape
    # is not telling the two apart, and then nothing is claimed.
    b = out["mm"].get("base")
    others = [n for n in cands[1:] if b is None or n < b]
    if others:
        out["oot"].update({"npc": None, "shops": None, "scrubs": None,
                           "sr": None, "xflags_count": None})
        out["oot"]["_ambiguous"] = others
    return out


def layout_complete(lay):
    """Whether both halves came out pinned and unambiguous."""
    if not lay:
        return False
    o, m = lay.get("oot", {}), lay.get("mm", {})
    return all(o.get(k) is not None for k in ("npc", "shops", "scrubs", "sr", "xflags_count"))         and all(m.get(k) is not None for k in ("base", "npc", "shops", "halfDays", "xflags_count"))


# --------------------------------------------------------------------------
# The soul bitmaps (souls.py)
# --------------------------------------------------------------------------
#
#   u16 coins[4]; u16 ocarinaButtonMaskOot; u16 ocarinaButtonMaskMm;
#   u8 soulsEnemyOot[8]; u8 soulsEnemyMm[8]; u8 soulsBossOot[2]; u8 soulsBossMm[1];
#   u8 soulsNpcOot[8]; u8 soulsNpcMm[8]; [u8 soulsAnimalsOot[2]; u8 soulsAnimalsMm[2];]
#   u8 soulsMiscOot[1]; u8 soulsMiscMm[1];
#   u8 caughtChildFishWeight[20]; u8 caughtAdultFishWeight[20]; u8 caughtFishFlags[5];
#
# The bitmaps sit between the ocarina button masks and the child fish weights,
# and every one of them is referenced by the code (souls.c loads each array's
# address to pass it to BITMAP8_SET). Two anchors, independent of each other,
# and they must agree when both are there:
#   * the fish: caughtFishFlags is already located by shape (layout), the child
#     weights start 0x28 before it, and the block ends right there;
#   * the coins: four u16 in a row, then two more (the ocarina masks), all six
#     referenced, then the block starts.
# Which arrays exist comes from the ROM's own soul tables (souls.arrays_for):
# the 784 generation had no animal pair. Measured: v32.0 +0x7EC..0x815,
# gen-829 +0x7CC..0x7F5, gen-784 +0x6BC..0x6E1 (fish anchor there too).

COINS_SHAPE = (0, 2, 4, 6, 8, 10)     # coins[4], ocarinaButtonMaskOot, ocarinaButtonMaskMm
CHILD_FISH_BEFORE_FLAGS = 0x28        # caughtChildFishWeight[20] + caughtAdultFishWeight[20]


def _pooled_refs(scans, located, size):
    """(indexed, any) reference counters into the custom save, both payloads."""
    idx = collections.Counter()
    any_ = collections.Counter()
    for game in ("oot", "mm"):
        sc = scans.get(game)
        base = located.get(game, {}).get("custom")
        if sc is None or not base:
            continue
        for off, (c, ic) in custom_offsets(sc, base[0], size).items():
            idx[off] += ic
            any_[off] += c
    return idx, any_


def souls_block(rom_bytes, located, arrays):
    """Where the soul bitmaps sit in gSharedCustomSave.

    `located` is locate()'s answer (its scans are reused when it kept them);
    `arrays` is [(type, game, size)] in struct order, from souls.arrays_for.
    Returns {"arrays": {"enemy_oot": off, ...}, "start", "end", "by"} with
    offsets relative to the custom save, or {"why": reason}. Never a guess: if
    the two anchors disagree, or the derived array starts are not the offsets
    the code references, it says so instead.
    """
    if not arrays:
        return {"why": "no soul arrays for this version"}
    scans = located.get("_scans") or {g: Scan(rom_bytes, g) for g in ("oot", "mm")}
    size = max(located["oot"]["custom"][1], located["mm"]["custom"][1])
    idx, any_ = _pooled_refs(scans, located, size)
    length = sum(sz for _, _, sz in arrays)
    lay = located.get("layout") or {}
    mm = lay.get("mm") or {}
    mm_end = (mm.get("base") or 0) + (mm.get("xflags_count") or 0)

    fish = (lay.get("oot") or {}).get("caughtFishFlags")
    by_fish = fish - CHILD_FISH_BEFORE_FLAGS - length if fish is not None else None

    # the coins anchor: six referenced u16 in a row, then a block whose every
    # array start is referenced and that ends on a referenced byte (the fish)
    def _fits(start):
        off = start
        for _, _, sz in arrays:
            if any_.get(off, 0) == 0:
                return False
            off += sz
        return any_.get(off, 0) > 0

    # u16 fields sit at even offsets; the MM half may end on an odd one
    by_coins = [c + 12 for c in range(mm_end + (mm_end & 1), size - 12 - length, 2)
                if all(any_.get(c + d, 0) > 0 for d in COINS_SHAPE) and _fits(c + 12)]

    if by_fish is not None and by_coins:
        if by_coins != [by_fish]:
            return {"why": f"anchors disagree: fish says +{by_fish:#x}, coins say "
                           f"{[hex(x) for x in by_coins]}"}
        start, by = by_fish, "fish+coins"
    elif by_fish is not None:
        start, by = by_fish, "fish"
    elif len(by_coins) == 1:
        start, by = by_coins[0], "coins"
    elif by_coins:
        return {"why": f"coins shape fits at {[hex(x) for x in by_coins]}, no fish anchor to choose"}
    else:
        return {"why": "neither the fish nor the coins anchor is there"}
    if not _fits(start):
        return {"why": f"block at +{start:#x} has an array start the code never references"}

    out = {}
    off = start
    for t, g, sz in arrays:
        out[f"{t}_{g}"] = off
        off += sz
    # nothing else may be referenced strictly inside the block: a reference to
    # a byte that is not an array start means the struct is not this one
    inside = [o for o in any_ if start < o < off and o not in out.values()]
    if inside:
        return {"why": f"unexpected references inside the block: {[hex(o) for o in inside]}"}
    return {"arrays": out, "start": start, "end": off, "by": by, "length": length}


# --------------------------------------------------------------------------
# One call for the tracker
# --------------------------------------------------------------------------

def locate(rom_bytes, verbose=False):
    """Everything the tracker wants, or as much of it as the ROM gives.

    {
      "oot": {"own": (addr, size), "foreign": (addr, size), "custom": (addr, size),
              "foreign_base": addr,      # what KNOWN_BASES calls the MM base (+8)
              "custom_gap": custom -> foreign distance},
      "mm":  {... same, foreign is gOotSave, foreign_base == foreign ...},
      "layout": {"oot": {...}, "mm": {...}},
    }
    Keys are absent when they could not be pinned; nothing is filled in from
    a constant.
    """
    scans = {}
    for game in ("oot", "mm"):
        try:
            scans[game] = Scan(rom_bytes, game)
        except (KeyError, ValueError, IndexError, struct.error) as e:
            if verbose:
                print(f"payload: {game}: cannot read the payload ({e})")
    out = {}
    for game in ("oot", "mm"):
        if game not in scans:
            continue
        other = scans.get("mm" if game == "oot" else "oot")
        b = buffers(scans[game], other)
        if "custom" in b and "foreign" in b:
            f = b["foreign"][0]
            b["foreign_base"] = f + (MM_BASE_DELTA if game == "oot" else 0)
            b["custom_gap"] = f - b["custom"][0]
        out[game] = b
    if "oot" in scans and "mm" in scans and \
            "custom" in out.get("oot", {}) and "custom" in out.get("mm", {}):
        size = max(out["oot"]["custom"][1], out["mm"]["custom"][1])
        out["layout"] = layout(scans["oot"], scans["mm"],
                               out["oot"]["custom"][0], out["mm"]["custom"][0], size)
    # kept for whoever wants more out of the same scan (souls_block); private,
    # and mkchecks' _payload_json only copies what it names
    out["_scans"] = scans
    if verbose:
        for game in ("oot", "mm"):
            b = out.get(game, {})
            if "custom" in b:
                print(f"payload: running {game}: custom save {b['custom'][0]:#x} ({b['custom'][1]:#x} bytes),"
                      f" foreign save {b['foreign'][0]:#x} ({b['foreign'][1]:#x}), own {b.get('own', (0, 0))[0]:#x}")
            else:
                print(f"payload: running {game}: could not pin the save buffers")
    return out


# --------------------------------------------------------------------------
# CLI: one ROM, or a corpus
# --------------------------------------------------------------------------

def _fmt(v):
    if isinstance(v, tuple):
        return f"{v[0]:#x}/{v[1]:#x}"
    if isinstance(v, int):
        return f"{v:#x}"
    return str(v)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("roms", nargs="+", help=".z64 files or folders")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--offsets", action="store_true", help="dump every referenced offset of the custom save")
    args = ap.parse_args(argv)
    paths = []
    for p in args.roms:
        p = pathlib.Path(p)
        paths.extend(sorted(p.glob("*.z64")) if p.is_dir() else [p])
    rows = []
    for p in paths:
        rb = p.read_bytes()
        try:
            res = locate(rb)
        except Exception as e:  # a vanilla ROM, an SM64...
            print(f"{p.name}: {e}")
            continue
        rows.append((p.name, res))
        if args.json:
            continue
        print(f"== {p.name}")
        for game in ("oot", "mm"):
            b = res.get(game, {})
            print(f"   running {game}: " + ", ".join(f"{k}={_fmt(v)}" for k, v in b.items()))
        lay = res.get("layout", {})
        for game in ("oot", "mm"):
            d = {k: v for k, v in lay.get(game, {}).items() if not k.startswith("_")}
            print(f"   layout {game}: " + ", ".join(f"{k}={_fmt(v)}" for k, v in d.items()))
        if args.offsets and "layout" in res:
            for game in ("oot", "mm"):
                sc = Scan(rb, game)
                c = res[game]["custom"]
                offs = custom_offsets(sc, c[0], c[1])
                print(f"   offsets referenced from custom ({game} payload): " +
                      " ".join(f"{o:#x}{'[]' if ic else ''}x{n}" for o, (n, ic) in sorted(offs.items())))
    if args.json:
        def conv(o):
            if isinstance(o, dict):
                return {str(k): conv(v) for k, v in o.items()}
            if isinstance(o, (list, tuple)):
                return [conv(v) for v in o]
            return o
        print(json.dumps({n: conv(r) for n, r in rows}, indent=1))


if __name__ == "__main__":
    main()
