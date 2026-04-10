#!/usr/bin/env python3
"""Deep diagnostic of Falcon execution: explore PRIV_LEVEL_MASKs,
EXCI exception info, and full IMEM fill behavior.

Goal: figure out why CPUCTL=02 doesn't actually run our code.
"""

import os
import sys
import struct
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from transport.tinygrad_transport import PascalGPU

FECS = 0x409000

# Key Falcon PRIV_LEVEL_MASK registers
PLMS = [
    ("FALCON_DMA_PLM",      0x308),
    ("FALCON_HS_PLM",       0x30c),
    ("FALCON_DMACTL_PLM",   0x310),
    ("FALCON_IRQ_PLM",      0x314),
    ("FALCON_BOOTVEC_PLM",  0x318),
    ("FALCON_BOOT_PLM",     0x31c),
    ("FALCON_SCTL_PLM",     0x340),  # might be here
    ("FALCON_CPUCTL_PLM",   0x408),
    ("FALCON_DEBUG_PLM",    0x40c),
    ("FALCON_RESET_PLM",    0x410),
    ("FALCON_EXE_PLM",      0x414),
    ("FALCON_REG_PLM",      0x418),
    ("FALCON_INTR_PLM",     0x41c),
    ("FALCON_INTR2_PLM",    0x428),
    ("FALCON_DEBUGINFO_PLM",0x42c),
]


def main():
    print("=" * 70)
    print("  Falcon Diagnostic — PLMs and exception state")
    print("=" * 70)

    gpu = PascalGPU()
    if gpu.rd32(0) == 0xffffffff:
        print("GPU dead"); return
    cmd = gpu.cfg_read(0x04, 2)
    if not (cmd & 0x06):
        gpu.cfg_write(0x04, cmd | 0x06, 2)
    gpu.wr32(0x000200, 0xffffffff)
    time.sleep(0.05)

    print("\nFECS PRIV_LEVEL_MASK registers:")
    for name, off in PLMS:
        v = gpu.rd32(FECS + off)
        if v == 0xffffffff or (v & 0xfff00000) == 0xbad00000:
            note = " (PLM unreachable)"
        else:
            note = f"  read={'protect_l3' if v == 0x44 else 'open_l0' if v == 0xff else 'mixed'}"
        print(f"  {name:20} +0x{off:03x}: 0x{v:08x}{note}")

    # Try unlocking SCTL via DEBUG PLM
    print("\nUnlock attempt: write 0xffffffff to all FECS PLMs")
    for name, off in PLMS:
        gpu.wr32(FECS + off, 0xffffffff)
    time.sleep(0.05)

    print("\nPLMs after unlock:")
    for name, off in PLMS:
        v = gpu.rd32(FECS + off)
        print(f"  {name:20} +0x{off:03x}: 0x{v:08x}")

    # Try SCTL write again
    sctl_before = gpu.rd32(FECS + 0x240)
    gpu.wr32(FECS + 0x240, 0)
    sctl_after = gpu.rd32(FECS + 0x240)
    print(f"\nSCTL write 0: 0x{sctl_before:08x} -> 0x{sctl_after:08x}")

    # Check EXCI (exception cause / info) register
    # On Pascal Falcon EXCI is at 0x144 (IMSTAT) and 0x140 (DEBUGINFO)
    # Also 0x16c (HWCFG2/RSTAT3) shows reset info
    print("\nFalcon state / exception info:")
    for name, off in [
        ("IMSTAT",     0x144),
        ("TRACEIDX",   0x148),
        ("TRACEPC",    0x14c),
        ("EXCI",       0x150),  # or 0x16c
        ("CPUCTL",     0x100),
        ("CPUCTL_ALIAS", 0x130),
        ("DBGCTL",     0x134),
        ("RSTAT0",     0x168),
        ("RSTAT3",     0x16c),
        ("PRIVSTATE",  0x060),
        ("FHSTATE",    0x05c),
        ("OS",         0x080),
    ]:
        v = gpu.rd32(FECS + off)
        print(f"  {name:14}+0x{off:03x}: 0x{v:08x}")

    # Now: completely fill IMEM with halt, set BOOTVEC=0, run
    print("\n[Test] Fill ALL IMEM with halt and run")
    HALT = 0xf8020000
    # 24KB IMEM = 6144 words = 96 blocks
    NUM_BLOCKS = 96
    for blk in range(NUM_BLOCKS):
        word_addr = (blk * 256) // 4
        gpu.wr32(FECS + 0x180, (word_addr << 2) | (1 << 24))
        gpu.wr32(FECS + 0x188, blk)
        for _ in range(64):
            gpu.wr32(FECS + 0x184, HALT)

    # Reset, set sentinel
    gpu.wr32(FECS + 0x040, 0xA5A5A5A5)
    gpu.wr32(FECS + 0x044, 0xB5B5B5B5)
    gpu.wr32(FECS + 0x10c, 0)        # DMACTL = 0
    gpu.wr32(FECS + 0x104, 0)        # BOOTVEC = 0
    gpu.wr32(FECS + 0x100, 0x10)     # halt first
    time.sleep(0.01)

    print(f"  IMEM[0]: 0x{gpu.rd32(FECS + 0x184):08x}  (after fill)")
    pc_before = gpu.rd32(FECS + 0x14c)
    cpuctl_before = gpu.rd32(FECS + 0x100)
    print(f"  before:  CPUCTL=0x{cpuctl_before:08x} PC=0x{pc_before:08x}")

    gpu.wr32(FECS + 0x100, 0x02)
    time.sleep(0.05)

    pc_after = gpu.rd32(FECS + 0x14c)
    cpuctl_after = gpu.rd32(FECS + 0x100)
    rstat0 = gpu.rd32(FECS + 0x168)
    rstat3 = gpu.rd32(FECS + 0x16c)
    excinfo = gpu.rd32(FECS + 0x150)
    privstate = gpu.rd32(FECS + 0x060)
    print(f"  after:   CPUCTL=0x{cpuctl_after:08x} PC=0x{pc_after:08x}")
    print(f"           RSTAT0=0x{rstat0:08x} RSTAT3=0x{rstat3:08x}")
    print(f"           EXCI=0x{excinfo:08x} PRIVSTATE=0x{privstate:08x}")

    # Same test on MSENC (different Falcon)
    print("\n[Test] Same test on MSENC @ 0x1c8000")
    MSENC = 0x1c8000
    sctl = gpu.rd32(MSENC + 0x240)
    print(f"  MSENC SCTL: 0x{sctl:08x}")
    # Try unlocking PLMs first
    for name, off in PLMS:
        gpu.wr32(MSENC + off, 0xffffffff)
    gpu.wr32(MSENC + 0x240, 0)
    sctl2 = gpu.rd32(MSENC + 0x240)
    print(f"  MSENC SCTL after unlock+write 0: 0x{sctl2:08x}")

    # Fill IMEM with halts
    msenc_imem_size = (gpu.rd32(MSENC + 0x108) & 0x1ff) * 256
    msenc_blocks = msenc_imem_size // 256
    for blk in range(msenc_blocks):
        word_addr = (blk * 256) // 4
        gpu.wr32(MSENC + 0x180, (word_addr << 2) | (1 << 24))
        gpu.wr32(MSENC + 0x188, blk)
        for _ in range(64):
            gpu.wr32(MSENC + 0x184, HALT)
    gpu.wr32(MSENC + 0x044, 0xC5C5C5C5)
    gpu.wr32(MSENC + 0x100, 0x10)
    time.sleep(0.01)
    print(f"  before MSENC: CPUCTL=0x{gpu.rd32(MSENC + 0x100):08x} PC=0x{gpu.rd32(MSENC + 0x14c):08x}")
    gpu.wr32(MSENC + 0x100, 0x02)
    time.sleep(0.05)
    print(f"  after  MSENC: CPUCTL=0x{gpu.rd32(MSENC + 0x100):08x} PC=0x{gpu.rd32(MSENC + 0x14c):08x}")

    gpu.close()


if __name__ == "__main__":
    main()
