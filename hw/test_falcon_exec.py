#!/usr/bin/env python3
"""Test whether each writable Falcon will EXECUTE unsigned code.

For each writable Falcon engine:
  1. Reset cleanly
  2. Try writing SCTL = 0 (drop UCODE_LEVEL to 0 = unsigned)
  3. Load `halt` instruction into IMEM[0]
  4. Set MAILBOX0 to a sentinel
  5. Set CPUCTL = STARTCPU (0x02)
  6. Wait briefly
  7. Read back: if Falcon executed and halted, CPUCTL = 0x10 (HALTED)
                if signature failed, STARTCPU stays set (0x12 or similar)

Falcon ISA halt: byte sequence 00 00 02 f8 (= u32 0xf8020000 little-endian)
"""

import os
import sys
import struct
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from transport.tinygrad_transport import PascalGPU


# Falcons to test (name, base)
TARGETS = [
    ("FECS",   0x409000),
    ("MSVLD",  0x084000),
    ("MSENC",  0x1c8000),
    ("SEC2",   0x087000),
]

# Falcon halt instruction (4 bytes, little-endian = 0xf8020000)
HALT = 0xf8020000


def test_falcon(gpu, name, base):
    print(f"\n--- {name} @ 0x{base:06x} ---")

    # 1. Initial state
    cpuctl_in = gpu.rd32(base + 0x100)
    sctl_in   = gpu.rd32(base + 0x240)
    print(f"  initial: CPUCTL=0x{cpuctl_in:08x} SCTL=0x{sctl_in:08x}")

    # 2. Try to drop SCTL to 0 (unsigned)
    gpu.wr32(base + 0x240, 0)
    sctl1 = gpu.rd32(base + 0x240)
    print(f"  SCTL after write 0: 0x{sctl1:08x}  (lvl drop {'OK' if sctl1 == 0 else 'FAIL'})")

    # 3. Reset Falcon: write CPUCTL = 0x4 (HRESET)
    gpu.wr32(base + 0x10c, 0)            # DMACTL = 0
    gpu.wr32(base + 0x014, 0xffffffff)   # IRQMCLR
    gpu.wr32(base + 0x004, 0xffffffff)   # IRQSCLR
    time.sleep(0.01)

    # 4. Load halt into IMEM[0]
    gpu.wr32(base + 0x180, (1 << 24))    # IMEMC: word 0 + AINCW
    gpu.wr32(base + 0x188, 0)            # IMEMT: tag 0
    gpu.wr32(base + 0x184, HALT)         # write halt
    # Fill rest of block with halts (for safety)
    for _ in range(63):
        gpu.wr32(base + 0x184, HALT)

    # 5. Verify IMEM
    gpu.wr32(base + 0x180, (1 << 25))    # AINCR
    v0 = gpu.rd32(base + 0x184)
    v1 = gpu.rd32(base + 0x184)
    print(f"  IMEM[0..1]: 0x{v0:08x} 0x{v1:08x}")
    if v0 != HALT:
        print(f"  IMEM verify FAILED — got 0x{v0:08x}, want 0x{HALT:08x}")
        return False

    # 6. Set sentinel in MAILBOX0
    gpu.wr32(base + 0x040, 0xA5A5A5A5)

    # 7. Set BOOTVEC = 0
    gpu.wr32(base + 0x104, 0)

    # 8. Start CPU
    print(f"  Setting CPUCTL = 0x02 (STARTCPU)...")
    gpu.wr32(base + 0x100, 0x02)
    time.sleep(0.05)

    # 9. Read back state
    cpuctl_out = gpu.rd32(base + 0x100)
    mb0_out    = gpu.rd32(base + 0x040)
    os_out     = gpu.rd32(base + 0x080)
    print(f"  after start: CPUCTL=0x{cpuctl_out:08x} MAILBOX0=0x{mb0_out:08x} OS=0x{os_out:08x}")

    # 10. Decode result
    halted = (cpuctl_out & 0x10) != 0
    startcpu_stuck = (cpuctl_out & 0x02) != 0
    if halted and not startcpu_stuck:
        print(f"  *** EXECUTED AND HALTED ✓ ***")
        return True
    elif halted and startcpu_stuck:
        print(f"  Halted but STARTCPU stuck = signature check failed")
        return False
    elif startcpu_stuck:
        print(f"  STARTCPU stuck, no halt = stuck in fetch/auth phase")
        return False
    else:
        print(f"  Unknown state")
        return False


def main():
    print("=" * 70)
    print("  Falcon Execution Test (unsigned code)")
    print("=" * 70)

    gpu = PascalGPU()
    if gpu.rd32(0) == 0xffffffff:
        print("GPU dead"); return

    cmd = gpu.cfg_read(0x04, 2)
    if not (cmd & 0x06):
        gpu.cfg_write(0x04, cmd | 0x06, 2)

    gpu.wr32(0x000200, 0xffffffff)
    time.sleep(0.05)

    results = {}
    for name, base in TARGETS:
        results[name] = test_falcon(gpu, name, base)

    print("\n" + "=" * 70)
    print("  Summary")
    print("=" * 70)
    for name, ok in results.items():
        print(f"  {name:8} {'EXECUTES UNSIGNED ✓' if ok else 'blocked/stuck'}")

    gpu.close()


if __name__ == "__main__":
    main()
