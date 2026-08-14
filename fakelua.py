#!/usr/bin/env python3
"""
fakelua.py - a stand-in for tracker.lua, so the tracker can be run without
the emulator.

It connects to the port the tracker listens on and answers the same protocol
(`PING` -> `TRK1`, the 1/2/4-byte reads and `READ_BLOCK`), serving a RAM dump
instead of a running game. That covers the whole program end to end —
detection, tables, the polling thread, the HTTP server, the page— which
`--dump` does not: `--dump` skips the link entirely.

It is also the only way to test the .exe, which has no `--dump` shortcut into
the overlay.

    python ootmm.py overlay --no-window --port 13261     # or the .exe
    python fakelua.py ram-en-oot.bin 13261

Careful: over a dump nothing ever changes, so this proves the plumbing, not
the reading of live memory. Unaligned reads, for one, only misbehave with the
real link (see the POC).
"""

import socket
import struct
import sys
import time

BASE = 0x80000000
MAGIC = 0x54524B31  # 'TRK1', same as ootmm.py

# addr comes big endian: the Lua packs u32 in the N64's own order
SIZES = {2: 1, 3: 2, 4: 4}


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: fakelua.py DUMP.bin [PORT] [BASE]")
    data = open(sys.argv[1], "rb").read()
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 13251
    base = int(sys.argv[3], 0) if len(sys.argv) > 3 else BASE

    def mem(addr, n):
        off = addr - base
        if off < 0 or off + n > len(data):
            return b"\x00" * n
        return data[off:off + n]

    s = socket.socket()
    # the tracker may not be listening yet, same as the real script
    for _ in range(120):
        try:
            s.connect(("127.0.0.1", port))
            break
        except OSError:
            time.sleep(0.25)
    else:
        sys.exit(f"nobody listening on {port}")
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print(f"[fakelua] serving {sys.argv[1]} on port {port}", flush=True)

    buf = b""
    while True:
        try:
            chunk = s.recv(65536)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        while buf:
            op = buf[0]
            if op == 0x01:  # PING
                buf = buf[1:]
                s.sendall(struct.pack(">I", MAGIC))
            elif op in SIZES:
                if len(buf) < 5:
                    break
                addr = struct.unpack_from(">I", buf, 1)[0]
                buf = buf[5:]
                s.sendall(mem(addr, SIZES[op]))
            elif op == 0x10:  # READ_BLOCK
                if len(buf) < 9:
                    break
                addr, n = struct.unpack_from(">II", buf, 1)
                buf = buf[9:]
                s.sendall(mem(addr, n))
            else:
                sys.exit(f"[fakelua] unknown opcode {op:#x}")
    print("[fakelua] connection closed")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
