#!/usr/bin/env python3
"""Find which PMC_ENABLE bits are writable individually.

PMC_ENABLE max writable from full 0xffffffff write = 0x5c6cf1e1.
Some bits silently drop. Test each bit to find:
  - Which bits stick (engine present and powered)
  - Which bits don't stick (engine missing / power-gated / locked)

Then try enabling locked engines via secondary paths:
  - PMC_ENABLE_PWR (separate register)
  - PMC_ELPG_ENABLE (engine power gate)
  - PMC_DEVICE_ENABLE_*
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from transport.tinygrad_transport import PascalGPU


# Pascal PMC_ENABLE bit mnemonics (from open-gpu-doc gp10x)
PMC_BITS = {
    0:  "HOST",
    1:  "TMR",
    4:  "PWR",
    5:  "PFIFO",
    6:  "PGRAPH",
    8:  "PCRTC",
    12: "PMEDIA",
    13: "PRMVIDEO",
    14: "PRMCIO",
    15: "PRMVGA",
    16: "SECENGINE",
    17: "PMSPPP",
    18: "PHOST",
    19: "PRAMHT",
    20: "PRAMRO",
    21: "PRMHT",
    22: "PVCRYPT",
    23: "PRMVRAM",
    24: "PERFMON",
    25: "VENC",
    26: "PCE0",
    27: "PCE1",
    28: "PCE2",
    29: "PVENC",
    30: "PNVDEC",
    31: "FBP",
}


def main():
    print("=" * 70)
    print("  PMC_ENABLE Bit Test")
    print("=" * 70)

    gpu = PascalGPU()

    # Make sure GPU is alive
    boot = gpu.rd32(0)
    if boot == 0xffffffff:
        print("GPU dead — recover first"); return
    print(f"GPU: 0x{boot:08x}")

    # Make sure memory space decode on
    cmd = gpu.cfg_read(0x04, 2)
    if not (cmd & 0x06):
        gpu.cfg_write(0x04, cmd | 0x06, 2)

    # Reset to clean state, then attempt full enable
    gpu.wr32(0x000200, 0x40003120)
    time.sleep(0.05)
    gpu.wr32(0x000200, 0xffffffff)
    time.sleep(0.05)
    pmc_max = gpu.rd32(0x000200)
    print(f"\nPMC_ENABLE max:    0x{pmc_max:08x}")
    print(f"  binary:          {pmc_max:032b}")

    # Find non-writable bits
    print("\nBit-by-bit:")
    for bit in range(32):
        enabled = (pmc_max >> bit) & 1
        name = PMC_BITS.get(bit, "?")
        mark = "✓" if enabled else "✗"
        print(f"  bit {bit:2}  ({name:10}): {mark}")

    # Look for related enable registers
    print("\nRelated enable registers:")
    related_regs = [
        ("PMC_ENABLE",                  0x000200),
        ("PMC_ELPG_ENABLE",             0x000140),  # Engine Low Power Gate
        ("PMC_DEVICE_ENABLE_0",         0x000600),
        ("PMC_DEVICE_ENABLE_1",         0x000604),
        ("PMC_DEVICE_ENABLE_2",         0x000608),
        ("PMC_DEVICE_ENABLE_3",         0x00060c),
        ("PMC_INTR_EN_0",               0x000640),
        ("PMC_INTR_EN_1",               0x000644),
        ("PWR_PMU_FALCON_CPUCTL",       0x10a100),
        ("PWR_FALCON_ENGINE",           0x10a3c0),
        ("PWR_FALCON_HWCFG",            0x10a108),
        ("PWR_FALCON_RESET",            0x10a130),  # Reset register
        ("PMC_BOOT_42",                 0x000a00),  # Some chips have this
    ]
    for name, addr in related_regs:
        val = gpu.rd32(addr)
        print(f"  {name:30} 0x{addr:06x} = 0x{val:08x}")

    # Try writing 0xffffffff to PMC_ELPG_ENABLE to ungate engines
    print("\nTry writing 0xffffffff to PMC_ELPG_ENABLE (0x140)...")
    gpu.wr32(0x000140, 0xffffffff)
    time.sleep(0.05)
    elpg = gpu.rd32(0x000140)
    print(f"  ELPG_ENABLE = 0x{elpg:08x}")

    # Re-attempt PMC_ENABLE = 0xffffffff
    gpu.wr32(0x000200, 0xffffffff)
    time.sleep(0.05)
    pmc2 = gpu.rd32(0x000200)
    print(f"  PMC_ENABLE  = 0x{pmc2:08x}  (was 0x{pmc_max:08x})")

    # Check if PWR engine appears reachable now
    pwr_state = gpu.rd32(0x10a100)
    print(f"  PWR_FALCON_CPUCTL = 0x{pwr_state:08x}")

    gpu.close()


if __name__ == "__main__":
    main()
