#!/usr/bin/env python3
"""Pascal GPU initialization — Phase 2: Enable engines and probe Falcons.

This script:
1. Connects to the GPU via TinyGPU
2. Reads GPU identification
3. Enables PMU, SEC2, PFIFO, PGRAPH engines
4. Reads Falcon status registers
5. Checks WPR (Write Protected Region) state
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from transport.tinygpu_client import TinyGPUClient
from hw.mmio import (MMIO, GPUEngine, PMC_BOOT_0, PMC_ENABLE,
                     PMC_ENABLE_PFIFO, PMC_ENABLE_PGRAPH, PMC_ENABLE_PMU,
                     PMC_ENABLE_CE0, PMC_ENABLE_SEC2,
                     PFB_PRI_MMU_WPR2_ADDR_LO, PFB_PRI_MMU_WPR2_ADDR_HI,
                     PTIMER_TIME_0, PTIMER_TIME_1)
from hw.falcon import create_falcons


def identify_gpu(mmio: MMIO) -> dict:
    """Read and decode GPU identification registers."""
    boot_0 = mmio.rd32(PMC_BOOT_0)
    arch = (boot_0 >> 20) & 0x1ff
    impl = (boot_0 >> 16) & 0xf
    rev = boot_0 & 0xff

    arch_names = {
        0x120: "Maxwell", 0x130: "Pascal", 0x136: "Pascal",
        0x140: "Volta", 0x160: "Turing", 0x170: "Ampere",
    }
    arch_name = "Unknown"
    for prefix, name in arch_names.items():
        if arch >= prefix and arch < prefix + 0x10:
            arch_name = name
            break

    return {
        "boot_0": boot_0,
        "arch": arch,
        "arch_name": arch_name,
        "implementation": impl,
        "revision": rev,
        "is_pascal": arch >= 0x130 and arch < 0x140,
    }


def read_timer(mmio: MMIO) -> int:
    """Read GPU timer in nanoseconds."""
    lo = mmio.rd32(PTIMER_TIME_0)
    hi = mmio.rd32(PTIMER_TIME_1)
    return (hi << 32) | lo


def check_wpr(mmio: MMIO) -> dict:
    """Check Write Protected Region state."""
    lo = mmio.rd32(PFB_PRI_MMU_WPR2_ADDR_LO)
    hi = mmio.rd32(PFB_PRI_MMU_WPR2_ADDR_HI)
    active = (lo != 0 or hi != 0)
    return {"lo": lo, "hi": hi, "active": active}


def main():
    print("=" * 60)
    print("  Pascal GPU Init — Phase 2: Engine Enable + Falcon Probe")
    print("=" * 60)

    # Connect
    client = TinyGPUClient()
    client.connect()
    mmio = MMIO(client)
    engines = GPUEngine(mmio)
    print("\n[1/5] Connected to TinyGPU")

    # Identify GPU
    gpu = identify_gpu(mmio)
    print(f"\n[2/5] GPU Identification")
    print(f"  PMC_BOOT_0:  0x{gpu['boot_0']:08x}")
    print(f"  Architecture: {gpu['arch_name']} (0x{gpu['arch']:03x})")
    print(f"  Revision:     0x{gpu['revision']:02x}")
    print(f"  Pascal:       {'YES' if gpu['is_pascal'] else 'NO'}")

    if not gpu["is_pascal"]:
        print("  WARNING: Not a Pascal GPU! Proceeding anyway...")

    # Check timer
    t0 = read_timer(mmio)
    time.sleep(0.01)
    t1 = read_timer(mmio)
    print(f"  Timer:        {t0} ns (delta: {t1-t0} ns)")

    # Read current engine state
    print(f"\n[3/5] Engine State (before)")
    print(f"  {engines.status_str()}")

    # Enable engines
    print(f"\n[4/5] Enabling engines...")
    enable_bits = PMC_ENABLE_PMU | PMC_ENABLE_SEC2 | PMC_ENABLE_PFIFO | PMC_ENABLE_PGRAPH | PMC_ENABLE_CE0

    for name, bit in [("PMU", PMC_ENABLE_PMU), ("SEC2", PMC_ENABLE_SEC2),
                       ("PFIFO", PMC_ENABLE_PFIFO), ("PGRAPH", PMC_ENABLE_PGRAPH),
                       ("CE0", PMC_ENABLE_CE0)]:
        if not engines.is_enabled(bit):
            print(f"  Enabling {name}...", end=" ", flush=True)
            engines.enable(bit)
            time.sleep(0.05)
            if engines.is_enabled(bit):
                print("OK")
            else:
                print("FAILED")
        else:
            print(f"  {name} already enabled")

    print(f"  {engines.status_str()}")

    # Probe Falcons
    print(f"\n[5/5] Falcon Status")
    falcons = create_falcons(mmio)

    for name in ["PMU", "SEC2", "FECS", "GPCCS"]:
        falcon = falcons[name]
        try:
            falcon.print_status()
        except Exception as e:
            print(f"  {name}: ERROR reading registers — {e}")

    # Check WPR
    print(f"\n--- Write Protected Region (WPR2) ---")
    wpr = check_wpr(mmio)
    print(f"  WPR2_ADDR_LO: 0x{wpr['lo']:08x}")
    print(f"  WPR2_ADDR_HI: 0x{wpr['hi']:08x}")
    if wpr["active"]:
        print(f"  Status: ACTIVE (secure boot has run before)")
    else:
        print(f"  Status: NOT SET (secure boot needed)")

    print(f"\n{'='*60}")
    print(f"  Phase 2 Complete")
    print(f"{'='*60}")

    client.close()


if __name__ == "__main__":
    main()
