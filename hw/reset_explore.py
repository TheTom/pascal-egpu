#!/usr/bin/env python3
"""Explore what gpu.dev.reset() actually does to the GPU.

Earlier surprise: WPR2_HI changed. Let me probe a much wider register
window before/after to see what else changes.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from transport.tinygrad_transport import PascalGPU


# Snapshot 256 32-bit words from each region of interest
REGIONS = [
    ("PMC",         0x000000, 0x400),
    ("PFB_HUB",     0x100000, 0x400),
    ("PFB_DRAM",    0x10e000, 0x400),
    ("PMU",         0x10a000, 0x400),
    ("PRIV_RING",   0x120000, 0x400),
    ("FBPA",        0x9a0000, 0x100),
    ("FECS",        0x409000, 0x400),
    ("CLOCK",       0x132000, 0x400),
    ("PWR_MGT",     0x17e000, 0x400),
]


def snapshot_region(gpu, base, size):
    out = {}
    for off in range(0, size, 4):
        try:
            v = gpu.rd32(base + off)
            out[base + off] = v
        except Exception:
            pass
    return out


def diff_regions(label, before, after):
    diffs = []
    for addr, val in after.items():
        b = before.get(addr)
        if b != val:
            diffs.append((addr, b, val))
    if diffs:
        print(f"\n  [{label}] {len(diffs)} changes:")
        for addr, b, a in diffs[:30]:
            tag = ""
            if (b is None or (b & 0xfff00000) == 0xbad00000) and (a is not None and (a & 0xfff00000) != 0xbad00000):
                tag = " ← BECAME ALIVE"
            elif b is not None and (b & 0xfff00000) != 0xbad00000 and (a & 0xfff00000) == 0xbad00000:
                tag = " ← BECAME DEAD"
            b_s = f"0x{b:08x}" if b is not None else "MISSING"
            print(f"    0x{addr:06x}: {b_s} -> 0x{a:08x}{tag}")
        if len(diffs) > 30:
            print(f"    ... ({len(diffs) - 30} more)")
    return diffs


def main():
    gpu = PascalGPU()
    boot = gpu.rd32(0)
    if boot == 0xffffffff:
        print("GPU dead"); return
    print(f"GPU: 0x{boot:08x}")

    cmd = gpu.cfg_read(0x04, 2)
    if not (cmd & 0x06):
        gpu.cfg_write(0x04, cmd | 0x06, 2)
    gpu.wr32(0x000200, 0xffffffff)
    time.sleep(0.05)

    print("\n[1] Snapshot BEFORE reset")
    before = {}
    for name, base, size in REGIONS:
        before[name] = snapshot_region(gpu, base, size)
        print(f"  {name}: {len(before[name])} regs")

    print("\n[2] Calling dev.reset() x 1")
    gpu.dev.reset()
    time.sleep(0.5)
    cmd2 = gpu.cfg_read(0x04, 2)
    if not (cmd2 & 0x06):
        gpu.cfg_write(0x04, cmd2 | 0x06, 2)
    boot2 = gpu.rd32(0)
    print(f"  PMC_BOOT_0 = 0x{boot2:08x}")

    if boot2 == 0xffffffff:
        print("GPU dead after reset"); return

    gpu.wr32(0x000200, 0xffffffff)
    time.sleep(0.05)

    print("\n[3] Snapshot AFTER reset (NO PMC re-enable yet)")
    after = {}
    for name, base, size in REGIONS:
        after[name] = snapshot_region(gpu, base, size)

    print("\n[4] Diff per region:")
    total = 0
    for name, _, _ in REGIONS:
        d = diff_regions(name, before[name], after[name])
        total += len(d)
    print(f"\nTotal changes: {total}")

    # Special checks
    print("\n[5] Critical checks:")
    pmu_sctl_b = before["PMU"].get(0x10a240)
    pmu_sctl_a = after["PMU"].get(0x10a240)
    print(f"  PMU SCTL: 0x{pmu_sctl_b or 0:08x} -> 0x{pmu_sctl_a or 0:08x}")
    pmu_dmactl_b = before["PMU"].get(0x10a10c)
    pmu_dmactl_a = after["PMU"].get(0x10a10c)
    print(f"  PMU DMACTL: 0x{pmu_dmactl_b or 0:08x} -> 0x{pmu_dmactl_a or 0:08x}")

    # Check PCI Command — DriverKit may have toggled it
    cmd_after = gpu.cfg_read(0x04, 2)
    print(f"  PCI Command (read again): 0x{cmd_after:04x}")

    # Check if any priv ring station became alive
    for addr in [0x12a270, 0x128280, 0x100200, 0x100eb8]:
        b = before["PRIV_RING"].get(addr) or before["PFB_HUB"].get(addr)
        a = after["PRIV_RING"].get(addr) or after["PFB_HUB"].get(addr)
        print(f"  0x{addr:06x}: 0x{b or 0:08x} -> 0x{a or 0:08x}")

    gpu.close()


if __name__ == "__main__":
    main()
