#!/usr/bin/env python3
"""
proclink.py - the game's RDRAM read straight out of the emulator's process. No Lua.

OoTMM's own multiplayer client stopped using a Lua script in v33: Project64-EM 1.1.0
opens a channel (the pj64em-ipc named pipe) that the GAME drives through fake cartridge
registers at 0x1FE00000, and the client sits on the other end of the pipe. That channel
carries what the game decides to send and is no use for reading memory. What the tracker
takes from it is the idea -- attach to the emulator, ask the user for nothing -- and the
plain way to do that is to read the emulator's process memory, where the N64's RDRAM is
one contiguous block.

Finding it: every committed, readable run of the emulator's address space at least 8 MiB
long is a candidate, and the one holding the boot globals at 0x300 (osTvType, osRomBase
0xB0000000, osResetType, osMemSize of 4 or 8 MiB) is RDRAM. Project64 keeps RDRAM as
native 32-bit words, so the bytes of each word are reversed relative to what the N64 sees
(the same swap its .fla saves show). The signature is tested in both orders and the one
that matches is applied to every read, so read()/read_block() give N64 order, like the
socket and shared-memory links do.

Same read()/read_block() as ootmm.Link and mmflink.MmfLink. Windows only, no
dependencies: OpenProcess, VirtualQueryEx, ReadProcessMemory through ctypes. Reading is
all it does -- nothing is written, injected or hooked. The emulator must not run with more
rights than the tracker: an elevated Project64 refuses OpenProcess, and the error says so.
"""

import array
import ctypes
import os
import struct
import threading

RDRAM_LO = 0x80000000
RDRAM_SIZE = 0x00800000          # 8 MiB: OoTMM needs the Expansion Pak
OSMEMSIZE_OK = (0x00400000, 0x00800000)
OSROMBASE = 0xB0000000
BOOT_GLOBALS = 0x300             # osTvType .. osMemSize: seven words the boot code leaves

MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x102
ERROR_ACCESS_DENIED = 5
SCAN_STEP = 0x10000              # VirtualAlloc granularity: RDRAM can only start on one of these


class ProcDown(ConnectionError):
    """The emulator process is gone, or its RDRAM is (a ROM reload frees it)."""


class ProcDenied(ProcDown):
    """OpenProcess refused: the emulator runs with more rights than the tracker."""


_api = None


def _kernel():
    """kernel32 with the five calls typed, plus the MEMORY_BASIC_INFORMATION of this
    Python's pointer size (the 64-bit layout has a PartitionId word the 32-bit one lacks)."""
    global _api
    if _api is not None:
        return _api
    if os.name != "nt":
        raise ProcDown("reading the emulator's memory directly is Windows only")
    from ctypes import wintypes

    fields = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
              ("AllocationProtect", wintypes.DWORD)]
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        fields.append(("PartitionId", wintypes.WORD))
    fields += [("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
               ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]
    MBI = type("MEMORY_BASIC_INFORMATION", (ctypes.Structure,), {"_fields_": fields})

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.CloseHandle.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.ReadProcessMemory.restype = wintypes.BOOL
    k.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    k.VirtualQueryEx.restype = ctypes.c_size_t
    k.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _api = (k, MBI)
    return _api


def _read(handle, address, n):
    """n bytes of the process at that address; OSError when Windows will not give them."""
    k, _ = _kernel()
    buf = ctypes.create_string_buffer(n)
    got = ctypes.c_size_t(0)
    ok = k.ReadProcessMemory(handle, ctypes.c_void_p(address), buf, n, ctypes.byref(got))
    if not ok or got.value != n:
        err = ctypes.get_last_error()
        why = ctypes.FormatError(err).strip() if err else "short read"
        raise OSError(err, f"ReadProcessMemory at 0x{address:X}, {got.value} of {n} bytes: {why}")
    return buf.raw


def _regions(handle):
    """(base, size, state, protect) of every region of the process, low to high."""
    k, MBI = _kernel()
    mbi = MBI()
    addr = 0
    while True:
        if not k.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            return
        base = mbi.BaseAddress or 0
        size = mbi.RegionSize
        yield base, size, mbi.State, mbi.Protect
        if base + size <= addr:
            return
        addr = base + size


def _readable_runs(handle):
    """(start, end) of the maximal runs of committed pages that can be read.

    Project64 marks the RDRAM pages it has compiled code from read-only to catch
    self-modifying code, so one RDRAM can show up as several regions with different
    protections: they are joined here, since all that matters is that they can be read."""
    start = end = None
    for base, size, state, protect in _regions(handle):
        ok = state == MEM_COMMIT and not protect & PAGE_GUARD and (protect & 0xFF) != PAGE_NOACCESS
        if ok and end == base:
            end = base + size
            continue
        if start is not None:
            yield start, end
        start, end = (base, base + size) if ok else (None, None)
    if start is not None:
        yield start, end


def word_order_at(handle, base):
    """'<' or '>' when the N64 boot globals sit at base + 0x300 in that word order, else None.

    osRomBase is always 0xB0000000 and osMemSize 4 or 8 MiB; osTvType is 0-2 and
    osResetType 0-1. Four words agreeing in one order and not the other is the test."""
    try:
        raw = _read(handle, base + BOOT_GLOBALS, 7 * 4)
    except OSError:
        return None
    for order in ("<", ">"):
        tv, _romtype, rombase, reset, _cic, _ver, memsize = struct.unpack(order + "7I", raw)
        if rombase == OSROMBASE and memsize in OSMEMSIZE_OK and tv <= 2 and reset <= 1:
            return order
    return None


def find_rdram(handle):
    """(base, word order) of the RDRAM block in that process, or None.

    The start of every readable run of at least 8 MiB is tried first -- Project64 commits
    RDRAM at the head of its reservation -- and then every 64 KiB boundary inside those
    runs, for an emulator that keeps it further into a bigger block."""
    runs = [(s, e) for s, e in _readable_runs(handle) if e - s >= RDRAM_SIZE]
    for s, _ in runs:
        order = word_order_at(handle, s)
        if order:
            return s, order
    for s, e in runs:
        p = (s + SCAN_STEP - 1) & ~(SCAN_STEP - 1)
        while p + RDRAM_SIZE <= e:
            if p != s:
                order = word_order_at(handle, p)
                if order:
                    return p, order
            p += SCAN_STEP
    return None


# array.array("I") is 4 bytes on Windows; the loop is the fallback for a build where it is not.
_WORD = next((code for code in ("I", "L") if array.array(code).itemsize == 4), None)


def _swap_words(raw):
    """Each 4-byte word reversed: host little-endian storage to N64 order (and back)."""
    if _WORD:
        words = array.array(_WORD, raw)
        words.byteswap()
        return words.tobytes()
    out = bytearray(len(raw))
    for i in range(0, len(raw), 4):
        out[i:i + 4] = raw[i:i + 4][::-1]
    return bytes(out)


class ProcLink:
    """Tracker side. Same read()/read_block() as the socket and shared-memory links."""

    def __init__(self, pid, exe, handle, base, order):
        self.pid = pid
        self.exe = exe
        self.handle = handle
        self.base = base
        self.order = order
        self._lock = threading.Lock()
        self._reads = 0
        self._bytes = 0

    @classmethod
    def attach(cls, pid, exe=None):
        """Open the process and find its RDRAM, or raise ProcDown (ProcDenied when Windows
        refuses the process: it runs elevated and the tracker does not)."""
        k, _ = _kernel()
        name = f" ({os.path.basename(exe)})" if exe else ""
        handle = k.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | SYNCHRONIZE, False, pid)
        if not handle:
            err = ctypes.get_last_error()
            if err == ERROR_ACCESS_DENIED:
                raise ProcDenied(f"Windows will not open process {pid}{name}: it runs with more rights "
                                 "than the tracker (as administrator?). Start the tracker the same way.")
            raise ProcDown(f"cannot open process {pid}{name}: {ctypes.FormatError(err).strip()}")
        try:
            found = find_rdram(handle)
        except BaseException:
            k.CloseHandle(handle)
            raise
        if found is None:
            k.CloseHandle(handle)
            raise ProcDown(f"no RDRAM in process {pid}{name}: no ROM running in it yet")
        base, order = found
        return cls(pid, exe, handle, base, order)

    def describe(self):
        name = os.path.basename(self.exe) if self.exe else "process"
        words = "little" if self.order == "<" else "big"
        return f"{name} (pid {self.pid}), RDRAM at 0x{self.base:X}, {words}-endian words"

    def process_running(self):
        k, _ = _kernel()
        return self.handle is not None and k.WaitForSingleObject(self.handle, 0) == WAIT_TIMEOUT

    def alive(self):
        """Whether the process is still there AND its RDRAM is still where it was: a ROM
        reload frees the block and allocates a new one, which is a new link's job."""
        return self.process_running() and word_order_at(self.handle, self.base) == self.order

    def read_block(self, addr, length):
        """The bytes at that N64 address, in N64 order."""
        if length < 0:
            raise ValueError("negative length")
        a = addr & 0xFFFFFFFF
        if 0xA0000000 <= a < 0xA0000000 + RDRAM_SIZE:
            a -= 0x20000000                      # KSEG1 alias of the same RDRAM
        off = a - RDRAM_LO
        if off < 0 or off + length > RDRAM_SIZE:
            raise OSError(0, f"0x{addr:08X} + {length} bytes is outside RDRAM")
        lo = off & ~3
        span = ((off + length + 3) & ~3) - lo
        if self.handle is None:
            raise ProcDown(f"{self.describe()}: link closed")
        try:
            raw = _read(self.handle, self.base + lo, span)
        except OSError as ex:
            if not self.process_running():
                raise ProcDown(f"{self.describe()}: the process has exited") from ex
            raise
        if self.order == "<":
            raw = _swap_words(raw)
        with self._lock:
            self._reads += 1
            self._bytes += span
        return raw[off - lo:off - lo + length]

    def read(self, addr, size):
        return int.from_bytes(self.read_block(addr, size), "big")

    def stats(self):
        with self._lock:
            return {"transport": "direct", "pid": self.pid, "base": f"0x{self.base:X}",
                    "word_order": "little" if self.order == "<" else "big",
                    "reads": self._reads, "bytes": self._bytes}

    def close(self):
        with self._lock:
            if self.handle is not None:
                k, _ = _kernel()
                k.CloseHandle(self.handle)
                self.handle = None


def emulator_processes():
    """(pid, exe path) of the Project64 builds running now, newest first (discover's list)."""
    import discover
    return [(pid, exe) for pid, exe, _ in discover.running_emulator_processes()]
