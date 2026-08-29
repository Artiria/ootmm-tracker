#!/usr/bin/env python3
"""
mmflink.py - talk to tracker-bizhawk.lua over shared memory, no socket.

BizHawk's Lua socket only exists when EmuHawk is started with --socket flags,
which forces the tracker to relaunch the emulator. Its memory-mapped files do
NOT: `comm.mmf*` is always available, so the user can open EmuHawk by hand,
load the script, and it just works -- the same two-step flow as Project64.

The catch is that a memory-mapped file has no signalling: both sides poll a
shared region and take turns. Three small mappings, all created by the tracker
(Python) so it owns their size, the script opening them by name:

  * <name>.req  (tracker -> script): the request.
      u32 seq @0 | u8 op @4 | u32 addr @8 | u32 len @12
    The tracker writes op/addr/len first and the seq LAST; the script reads the
    whole block at once, so it either sees the new seq with the new payload
    (x86 is write-ordered) or the old seq and ignores it. No torn request.

  * <name>.resp (script -> tracker): the answer.
      u32 len @0 | bytes @4
  * <name>.seq  (script -> tracker): the answer's sequence, u32 @0.
    The script writes .resp FIRST and .seq AFTER, in separate mappings, so when
    the tracker sees .seq change the data behind it is already there.

Strictly ping-pong: the tracker sends one request and waits for its answer
before the next, so only one is ever in flight and there is nothing to
interleave. `comm.mmfWriteBytes`/`mmfReadBytes` always work from offset 0,
which is why the answer's seq lives in its own mapping rather than after the
data.
"""

import mmap
import os
import struct
import time

MAGIC = b"BIZ1"
MAX_BLOCK = 0x10000        # 64 KB per request; a normal poll fits in one or two

# The name the Lua script and the tracker agree on. Fixed in production, so the
# script needs no configuration and one tracker talks to one emulator. The env
# var only exists for tests: it lets a guard run its own overlay + fake against
# a private name without colliding with a live session (the production Lua does
# not read it, so never set it when a real BizHawk is loaded).
DEFAULT_NAME = os.environ.get("OOTMM_MMF_NAME") or "OoTMMTracker"

REQ_SIZE = 32
SEQ_SIZE = 8
RESP_CAP = MAX_BLOCK + 8   # u32 len + data

OP_PING = 0x50             # 'P'
OP_BLOCK = 0x42            # 'B'

# 0x80000318 holds RDRAM size; a sane value proves we are reading real memory.
OSMEMSIZE_ADDR = 0x80000318
OSMEMSIZE_OK = (0x00400000, 0x00800000)


class MmfDown(Exception):
    """The script is not answering (not loaded yet, or gone)."""


class MmfLink:
    """Tracker side. Same read()/read_block() as the socket links."""

    def __init__(self, name, timeout=2.0):
        self.name = name
        self.timeout = timeout
        self._seq = 0
        self.req = mmap.mmap(-1, REQ_SIZE, tagname=name + ".req")
        self.resp = mmap.mmap(-1, RESP_CAP, tagname=name + ".resp")
        self.seq = mmap.mmap(-1, SEQ_SIZE, tagname=name + ".seq")
        # Start the answer seq at 0 so the first real request (seq 1) is new.
        self.seq[0:4] = struct.pack("<I", 0)

    def close(self):
        for m in (self.req, self.resp, self.seq):
            try:
                m.close()
            except Exception:
                pass

    def _exchange(self, op, addr, length, timeout=None):
        self._seq = (self._seq % 0xFFFFFFFF) + 1
        s = self._seq
        # payload first, seq last: see the module docstring
        self.req[4] = op
        self.req[8:12] = struct.pack("<I", addr & 0xFFFFFFFF)
        self.req[12:16] = struct.pack("<I", length & 0xFFFFFFFF)
        self.req[0:4] = struct.pack("<I", s)

        deadline = time.perf_counter() + (self.timeout if timeout is None else timeout)
        while True:
            if struct.unpack_from("<I", self.seq, 0)[0] == s:
                break
            if time.perf_counter() >= deadline:
                raise MmfDown(f"no answer to seq {s} on {self.name}")
            time.sleep(0.0005)
        n = struct.unpack_from("<I", self.resp, 0)[0]
        if n > RESP_CAP - 4:
            raise MmfDown(f"answer claims {n} bytes, mapping holds {RESP_CAP - 4}")
        # A block answer has to be the size that was asked: a short one would
        # read as a number made of fewer bytes, with nothing to show for it.
        if op == OP_BLOCK and n != length:
            raise MmfDown(f"asked {length} bytes at 0x{addr:08X}, the answer holds {n}")
        return self.resp[4:4 + n]

    def ping(self, timeout=None):
        return self._exchange(OP_PING, 0, 4, timeout) == MAGIC

    def read_block(self, addr, length):
        lo = addr & ~3
        span = ((addr + length + 3) & ~3) - lo
        out = bytearray()
        a, left = lo, span
        while left > 0:
            n = min(left, MAX_BLOCK)
            out += self._exchange(OP_BLOCK, a, n)
            a += n
            left -= n
        off = addr - lo
        return bytes(out[off:off + length])

    def read(self, addr, size):
        return int.from_bytes(self.read_block(addr, size), "big")
