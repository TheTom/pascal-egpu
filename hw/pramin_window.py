#!/usr/bin/env python3
"""Try PRAMIN window to access registers we can't reach via direct BAR0.

PRAMIN is a 1MB window inside BAR0 (at offset 0x700000) that maps to
GPU memory space. The base of the window is set by PMC_BAR0_WINDOW
(0x001700). By pointing the window at register space, we can sometimes
access regs through a different priv ring path that has different
PLM checks.

Also test:
- BAR1 access (VRAM aperture) — was reported dead but worth re-checking
- PCI extended capability scan for vendor-specific reset
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from transport.tinygrad_transport import PascalGPU


def test_pramin_window(gpu, target_addr, label):
    """Set PRAMIN window to point near target and read via PRAMIN."""
    # Window base is in 64KB chunks, lower 16 bits of target are window offset
    window_base = target_addr & ~0xffff
    window_offset = target_addr & 0xffff
    # PRAMIN window register accepts base >> 16
    gpu.wr32(0x001700, window_base >> 16)
    time.sleep(0.001)
    # Read via PRAMIN at BAR0+0x700000+offset
    val = gpu.rd32(0x700000 + window_offset)
    print(f"  {label:30} (target 0x{target_addr:06x}, window=0x{window_base:06x}): 0x{val:08x}")
    return val


def main():
    print("=" * 70)
    print("  PRAMIN window indirection probe")
    print("=" * 70)

    gpu = PascalGPU()
    if gpu.rd32(0) == 0xffffffff:
        print("GPU dead"); return

    cmd = gpu.cfg_read(0x04, 2)
    if not (cmd & 0x06):
        gpu.cfg_write(0x04, cmd | 0x06, 2)
    gpu.wr32(0x000200, 0xffffffff)
    time.sleep(0.05)

    print("\nDirect BAR0 reads (baseline):")
    for label, addr in [
        ("FBPA 0x9a0530",   0x9a0530),
        ("PWR 0x17e244",    0x17e244),
        ("CLK 0x132800",    0x132800),
        ("PFB 0x100eb8",    0x100eb8),
        ("PPRIV_FBP",       0x12a270),
    ]:
        v = gpu.rd32(addr)
        print(f"  {label:30}: 0x{v:08x}")

    print("\nVia PRAMIN window:")
    for label, addr in [
        ("FBPA 0x9a0530",   0x9a0530),
        ("PWR 0x17e244",    0x17e244),
        ("CLK 0x132800",    0x132800),
        ("PFB 0x100eb8",    0x100eb8),
        ("PPRIV_FBP",       0x12a270),
    ]:
        test_pramin_window(gpu, addr, label)

    # Look at PCI extended capabilities (offset 0x100+)
    print("\nPCI Extended Capabilities (>0x100):")
    cap_off = 0x100
    while cap_off and cap_off < 0x1000:
        try:
            hdr = gpu.cfg_read(cap_off, 4)
        except Exception:
            break
        if hdr == 0 or hdr == 0xffffffff:
            break
        cap_id = hdr & 0xffff
        cap_ver = (hdr >> 16) & 0xf
        next_off = (hdr >> 20) & 0xfff
        print(f"  Cap @ 0x{cap_off:03x}: id=0x{cap_id:04x} ver={cap_ver} next=0x{next_off:03x}")
        # Look for vendor-specific (id = 0x000b) which may have NV reset
        if cap_id == 0x000b:
            # Vendor-specific extended capability
            vsec = gpu.cfg_read(cap_off + 4, 4)
            print(f"    VSEC header: 0x{vsec:08x}")
            for o in range(8, 32, 4):
                v = gpu.cfg_read(cap_off + o, 4)
                print(f"    +0x{o:02x}: 0x{v:08x}")
        cap_off = next_off

    # Try BAR1 (VRAM aperture)
    print("\nBAR1 (VRAM aperture) read attempt:")
    try:
        bar1 = gpu.bar1
        b0 = bar1[0:16]
        print(f"  BAR1[0..16]: {bytes(b0).hex()}")
    except Exception as e:
        print(f"  BAR1 access failed: {e}")

    gpu.close()


if __name__ == "__main__":
    main()
