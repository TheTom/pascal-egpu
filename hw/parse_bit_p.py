#!/usr/bin/env python3
"""Parse Pascal VBIOS BIT table → BIT P → RAMMAP → devinit scripts.

Goal: locate the host-runnable RAM init scripts that gp100_ram_init() in
nouveau executes via nvbios_exec(). If we can find these and interpret
the devinit opcodes on the host, we may be able to bring up DRAM/FBP
without a working PMU.

References:
  - drivers/gpu/drm/nouveau/nvkm/subdev/bios/bit.c
  - drivers/gpu/drm/nouveau/nvkm/subdev/bios/rammap.c
  - drivers/gpu/drm/nouveau/nvkm/subdev/fb/ramgp100.c

GP106 BIT layout (verified empirically from vbios_full.rom):
  BIT signature 'BIT\\0' at file offset 0x1e2
  +4..+5: bit_id, bit_ver = 0, 1
  +8:     num_entries     = 0x11 = 17
  +10:    first entry (each entry is 6 bytes: id, ver, len_u16, off_u16)

BIT P pointers are full u32 file offsets (e.g. 0x00019cc8 = 0x19cc8 in
the 512 KB ROM dump).
"""

import os
import struct
import sys

VBIOS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "firmware", "blobs", "gp106", "vbios_full.rom")


def rd8(buf, off):  return buf[off]
def rd16(buf, off): return struct.unpack_from('<H', buf, off)[0]
def rd32(buf, off): return struct.unpack_from('<I', buf, off)[0]


def find_bit(buf):
    sig = b'BIT\x00'
    return buf.find(sig)


def parse_bit(buf, bit_off):
    """Returns list of (id, ver, length, offset) entries."""
    bit_id      = rd8(buf, bit_off + 4)
    bit_ver     = rd8(buf, bit_off + 5)
    num_entries = rd8(buf, bit_off + 8)
    print(f"BIT @ 0x{bit_off:04x}: id={bit_id} ver={bit_ver} entries={num_entries}")
    entries = []
    e = bit_off + 10
    for i in range(num_entries):
        eid  = rd8(buf, e + 0)
        ever = rd8(buf, e + 1)
        elen = rd16(buf, e + 2)
        eoff = rd16(buf, e + 4)
        ch = chr(eid) if 32 <= eid < 127 else '?'
        print(f"  [{i:2}] id=0x{eid:02x} '{ch}'  ver={ever}  len={elen:4}  off=0x{eoff:04x}")
        entries.append((eid, ever, elen, eoff))
        e += 6
    return entries


def parse_bit_P(buf, bit_p):
    """BIT P entry → list of u32 sub-table pointers (full file offsets)."""
    eid, ever, elen, eoff = bit_p
    print(f"\nBIT P @ 0x{eoff:04x}  ver={ever}  len={elen}")
    print("u32 fields:")
    n = elen // 4
    for i in range(n):
        v = rd32(buf, eoff + i * 4)
        in_rom = " <- in ROM" if 0 < v < len(buf) else ""
        print(f"  +0x{i*4:02x}: 0x{v:08x}{in_rom}")

    # nouveau: for BIT P ver 2, rammap pointer is at +4
    rammap_ptr = rd32(buf, eoff + 4)
    print(f"\n  rammap pointer (bit_P+4) = 0x{rammap_ptr:08x}")
    return rammap_ptr


def parse_rammap(buf, off):
    """Parse RAMMAP table header (nvbios_rammapTe in nouveau).

    Layout (Pascal/Maxwell ver 0x11):
      +0x00 u8 version
      +0x01 u8 hdr_len
      +0x02 u8 cnt              (entry count)
      +0x03 u8 len              (entry length)
      +0x04 u8 snr              (sub-entries per entry)
      +0x05 u8 ssz              (sub-entry size)
      +0x06 u16 ???
    """
    if off >= len(buf) - 16:
        print(f"RAMMAP offset 0x{off:x} out of range")
        return None
    print(f"\nRAMMAP @ 0x{off:05x}")
    # Hex dump first 32 bytes
    for i in range(0, 32, 16):
        row = buf[off + i: off + i + 16]
        print(f"  +0x{i:02x}: {' '.join(f'{b:02x}' for b in row)}")

    ver  = rd8(buf, off + 0)
    hlen = rd8(buf, off + 1)
    cnt  = rd8(buf, off + 2)
    elen = rd8(buf, off + 3)
    snr  = rd8(buf, off + 4)
    ssz  = rd8(buf, off + 5)
    print(f"  ver=0x{ver:02x} hlen={hlen} cnt={cnt} elen={elen} snr={snr} ssz={ssz}")

    if ver not in (0x10, 0x11):
        print(f"  unexpected version 0x{ver:02x}")
        return None

    # Walk entries
    entry_off = off + hlen
    print(f"\n  Entries (each {elen} bytes + {snr}*{ssz} sub-entries):")
    for ei in range(cnt):
        e_base = entry_off
        e_data = buf[e_base: e_base + elen]
        # Common: first u16 is freq_min in MHz, second u16 freq_max,
        # later fields contain script pointers
        print(f"    [{ei}] @ 0x{e_base:05x}: {e_data[:elen].hex()}")
        # nvbios_rammapEp: data + 0x00 = bits, +0x02 = freq min/max,
        #                  script pointer at varying offset
        # On GP100 the "script" pointer is at +0x18 (u32) per ramgp100.c
        if elen >= 0x1c:
            script_ptr = rd32(buf, e_base + 0x18)
            print(f"         maybe script @ +0x18 = 0x{script_ptr:08x}")
        # Skip past entry + sub-entries
        entry_off += elen + snr * ssz

    return ver


def main():
    with open(VBIOS_PATH, 'rb') as f:
        buf = f.read()
    print(f"VBIOS: {len(buf)} bytes ({len(buf)//1024} KB)")

    bit_off = find_bit(buf)
    if bit_off < 0:
        print("BIT not found"); return
    entries = parse_bit(buf, bit_off)

    bit_p = next((e for e in entries if e[0] == 0x50), None)
    if not bit_p:
        print("No BIT P entry"); return

    rammap_ptr = parse_bit_P(buf, bit_p)
    if 0 < rammap_ptr < len(buf):
        parse_rammap(buf, rammap_ptr)


if __name__ == "__main__":
    main()
