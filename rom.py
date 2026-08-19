#!/usr/bin/env python3
"""
rom.py - read data out of the ROM, compressed or not.

Used by mkchecks.py (xflag tables), mkicons.py (icons) and placement.py (the
item placement table). Each of those used to have its own reader, and both of
them assumed two things that do not always hold:

  1. **That the files were uncompressed.** OoTMM can generate the seed
     compressed, and then the DMA entries carry Yaz0.
  2. **That every table was its own DMA entry.** In compressed ROMs the six
     xflag tables all fall inside a single entry, so you have to keep the slice
     that starts at the requested VROM, not the whole file.

Both blew up with a seed other than the development one, and both with the
same symptom: `table 0x80b0f00 is compressed`.
"""

import os
import struct

COMBO_META_ROM = 0x03FFF000   # combo/defs.h
OOT_DMA_ADDR = 0x7430         # combo/dma.h


def yaz0(src):
    """Yaz0 decompressor. Returns None if the header is not there."""
    if src[:4] != b"Yaz0":
        return None
    size = struct.unpack_from(">I", src, 4)[0]
    dst = bytearray(size)
    p, o, code, bits = 16, 0, 0, 0
    while o < size:
        if bits == 0:
            code = src[p]
            p += 1
            bits = 8
        if code & 0x80:
            dst[o] = src[p]
            p += 1
            o += 1
        else:
            b1, b2 = src[p], src[p + 1]
            p += 2
            dist = ((b1 & 0x0F) << 8) | b2
            n = b1 >> 4
            if n == 0:
                n = src[p] + 0x12
                p += 1
            else:
                n += 2
            back = o - dist - 1
            for i in range(n):
                dst[o] = dst[back + i]
                o += 1
        code <<= 1
        bits -= 1
    return bytes(dst)


def entry_data(rom, vstart, vend, pstart, pend):
    """The bytes of a DMA entry, decompressing when needed."""
    if pstart == 0xFFFFFFFF:
        raise ValueError("the file is not in the ROM")
    if pend in (0, 0xFFFFFFFF):
        return rom[pstart : pstart + (vend - vstart)]
    out = yaz0(rom[pstart:pend])
    if out is None:
        raise ValueError(f"entry compressed with something other than Yaz0 at {pstart:#x}")
    return out


def extra_dma(rom):
    """(address, entry count) of the extra DMA table.

    Its header sits at a fixed offset, well past the end of a plain 8 MB ROM,
    so this is also where "that .z64 is not an OoTMM seed" gets caught. Without
    it the failure is a struct.error about buffer sizes, which says nothing to
    anyone.
    """
    if len(rom) < COMBO_META_ROM + 8:
        raise ValueError(
            f"the ROM has no extra DMA at {COMBO_META_ROM:#x} "
            f"({len(rom)} bytes); it does not look like an OoTMM seed")
    return struct.unpack_from(">II", rom, COMBO_META_ROM)


def is_ootmm_file(path):
    """Whether the .z64 at `path` is an OoTMM seed, read cheaply.

    Only the 8-byte extra-DMA header is read, at a fixed offset well past the
    end of any ordinary N64 ROM, so a normal game (Mario 64, OoT vanilla) or a
    wrong file is ruled out without loading 64 MB. Used to decide, before ever
    connecting, whether an EmuHawk that is running is even ours to touch.
    """
    try:
        size = os.path.getsize(path)
        if size < COMBO_META_ROM + 8:
            return False
        with open(path, "rb") as f:
            f.seek(COMBO_META_ROM)
            addr, count = struct.unpack(">II", f.read(8))
    except (OSError, struct.error):
        return False
    # the header must name a table that actually fits inside the file
    return 0 < addr < size and 0 < count < 0x10000 and addr + count * 16 <= size


def read_extra_vrom(rom, vrom, length=None):
    """Data at a VROM of OoTMM's 'extra DMA' (the ones >= 0x08000000).

    Returned **from the requested VROM onwards**, not from the start of the
    file: several tables can share one entry.
    """
    addr, count = extra_dma(rom)
    for i in range(count):
        vs, ve, ps, pe = struct.unpack_from(">IIII", rom, addr + i * 16)
        if vrom == vs or vs < vrom < ve:
            data = entry_data(rom, vs, ve, ps, pe)
            off = vrom - vs
            trozo = data[off : off + length] if length else data[off:]
            if not trozo:
                raise ValueError(f"{vrom:#x} falls outside its entry's data")
            return trozo
    raise KeyError(f"{vrom:#x} is not in this ROM's extra DMA")


def extra_entries(rom, uncompressed_only=True):
    """[(vstart, vend, data)] of the extra DMA, to hunt through by shape.

    `uncompressed_only` because that is what the hunting is for and
    decompressing 260-odd entries to look at each one would cost seconds. Every
    table that gets located this way is stored raw; if some future version
    compresses them, the caller falls back to its constants and says so.
    """
    addr, count = extra_dma(rom)
    out = []
    for i in range(count):
        vs, ve, ps, pe = struct.unpack_from(">IIII", rom, addr + i * 16)
        if ve <= vs or ps == 0xFFFFFFFF:
            continue
        if uncompressed_only and pe not in (0, 0xFFFFFFFF):
            continue
        out.append((vs, ve, rom[ps : ps + (ve - vs)]))
    return out


def read_native_file(rom, index, dma_addr=OOT_DMA_ADDR):
    """A file of OoT's native dmadata, by index."""
    vs, ve, ps, pe = struct.unpack_from(">IIII", rom, dma_addr + index * 16)
    return entry_data(rom, vs, ve, ps, pe)
