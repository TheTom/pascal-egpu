#!/usr/bin/env python3
"""Race condition: try to write SCTL right after FECS comes out of PMC reset.

Theory: Falcon SCTL has a security latch that engages once the Falcon
sees its first signed code load. If we PMC-reset FECS and then write SCTL=0
before any other action, maybe the latch hasn't engaged yet.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from transport.tinygrad_transport import PascalGPU

FECS = 0x409000


def attempt_unlock(gpu):
    # 1. Disable PGRAPH (clear bit 6) — puts FECS in reset
    pmc = gpu.rd32(0x000200)
    gpu.wr32(0x000200, pmc & ~(1 << 6))
    time.sleep(0.001)
    # 2. Re-enable PGRAPH — FECS comes out of reset
    gpu.wr32(0x000200, pmc | (1 << 6))
    # 3. IMMEDIATELY write SCTL = 0 (no delay)
    gpu.wr32(FECS + 0x240, 0)
    # 4. Check
    sctl = gpu.rd32(FECS + 0x240)
    return sctl


def main():
    gpu = PascalGPU()
    if gpu.rd32(0) == 0xffffffff:
        print("GPU dead"); return
    cmd = gpu.cfg_read(0x04, 2)
    if not (cmd & 0x06):
        gpu.cfg_write(0x04, cmd | 0x06, 2)
    gpu.wr32(0x000200, 0xffffffff)
    time.sleep(0.05)

    # Initial state
    print(f"FECS SCTL initial: 0x{gpu.rd32(FECS + 0x240):08x}")

    # Try racing the SCTL write 100 times
    for i in range(20):
        sctl = attempt_unlock(gpu)
        if sctl != 0x3000:
            print(f"  Attempt {i}: SCTL = 0x{sctl:08x}  ← DIFFERENT!")
        if sctl == 0:
            print(f"  *** UNLOCKED on attempt {i} ***")
            break
    else:
        print("All attempts: SCTL stayed at 0x3000")

    # Try writing SCTL with various potential unlock keys
    print("\nTry SCTL with explicit values:")
    for v in [0x00000000, 0x00000003, 0x00000010, 0xa5a5a5a5, 0x00000001, 0x80000000]:
        gpu.wr32(FECS + 0x240, v)
        rb = gpu.rd32(FECS + 0x240)
        print(f"  write 0x{v:08x} -> read 0x{rb:08x}")

    # Try DBGCTL = 1 (enable debug interface)
    print("\nDBGCTL exploration:")
    gpu.wr32(FECS + 0x134, 0xffffffff)
    dbg = gpu.rd32(FECS + 0x134)
    print(f"  DBGCTL after write 0xffffffff: 0x{dbg:08x}")

    # Falcon ICD interface — used for debug
    # 0x180 IMEMC, 0x184 IMEMD, 0x188 IMEMT
    # 0x1c0 DMEMC, 0x1c4 DMEMD
    # 0x200 ICD_CMD, 0x204 ICD_ADDR, 0x208 ICD_WDATA, 0x20c ICD_RDATA

    print("\nFalcon ICD interface probe:")
    icd_cmd = gpu.rd32(FECS + 0x200)
    icd_addr = gpu.rd32(FECS + 0x204)
    print(f"  ICD_CMD:   0x{icd_cmd:08x}")
    print(f"  ICD_ADDR:  0x{icd_addr:08x}")

    gpu.close()


if __name__ == "__main__":
    main()
