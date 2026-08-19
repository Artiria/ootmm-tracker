#!/usr/bin/env python3
"""A fake BizHawk MMF script: serves a RAM dump over the shared memory the
tracker creates, exactly as tracker-bizhawk.lua does. Lets the whole overlay
be exercised on the MMF path without an emulator, the way fakelua.py does for
the Project64 socket path.

    python ootmm.py overlay --no-window --bizhawk
    python fake_mmf.py ram-fresh-mm.bin

Careful: over a dump nothing changes, so this proves the plumbing, not the
reading of live memory.
"""
import mmap
import struct
import sys
import time

import mmflink

NAME = mmflink.DEFAULT_NAME


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: fake_mmf.py DUMP.bin")
    ram = open(sys.argv[1], "rb").read()

    # The tracker creates the mappings; wait for them like the Lua does.
    for _ in range(600):
        try:
            req = mmap.mmap(-1, mmflink.REQ_SIZE, tagname=NAME + ".req")
            resp = mmap.mmap(-1, mmflink.RESP_CAP, tagname=NAME + ".resp")
            seq = mmap.mmap(-1, mmflink.SEQ_SIZE, tagname=NAME + ".seq")
            break
        except OSError:
            time.sleep(0.1)
    else:
        sys.exit("the tracker's shared memory never appeared")
    print(f"fake-mmf: serving {sys.argv[1]} ({len(ram)} bytes) as '{NAME}'", flush=True)

    last = 0
    served = 0
    while True:
        s = struct.unpack_from("<I", req, 0)[0]
        if s != last:
            op = req[4]
            addr = struct.unpack_from("<I", req, 8)[0]
            length = struct.unpack_from("<I", req, 12)[0]
            if op == mmflink.OP_PING:
                data = mmflink.MAGIC
            else:
                off = addr & 0x1FFFFFFF
                data = ram[off:off + length].ljust(length, b"\0")
            resp[0:4] = struct.pack("<I", len(data))
            resp[4:4 + len(data)] = data
            seq[0:4] = struct.pack("<I", s)
            last = s
            served += 1
        time.sleep(0.001)


if __name__ == "__main__":
    main()
