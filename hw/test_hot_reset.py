#!/usr/bin/env python3
"""Test the patched HotReset path on the GTX 1060.

After installing the patched TinyGPU dext (which calls
kIOPCIDeviceResetTypeHotReset = upstream bridge SBR), this should:

1. Capture pre-reset state (PMC_BOOT_0, WPR2_HI, FBP/GPC station status)
2. Call dev.reset()
3. Wait for the GPU to come back
4. Re-enable PCI memory space (DriverKit may save/restore but let's be safe)
5. Capture post-reset state
6. Check if anything that was previously dead is now alive:
   - WPR2_HI should clear (was 0xccccfcfc)
   - PFB_CFG0 should change (was 0xbadf1100)
   - PMU SCTL should drop HSMODE if reset re-ran whatever set it
   - FBP/GPC priv stations should register
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from transport.tinygrad_transport import PascalGPU


SNAPSHOT = [
    ("PMC_BOOT_0",         0x000000),
    ("PMC_ENABLE",         0x000200),
    ("PMC_BOOT_42",        0x000a00),
    ("PFB_CFG0",           0x100200),
    ("WPR2_HI",            0x100cd4),
    ("PPRIV_FBP0_MASTER",  0x12a270),
    ("PPRIV_GPC0_MASTER",  0x128280),
    ("PPRIV_SYS_DECODE",   0x1200a8),
    ("PMU_CPUCTL",         0x10a100),
    ("PMU_SCTL",           0x10a240),
    ("PMU_DMACTL",         0x10a10c),
    ("FECS_CPUCTL",        0x409100),
    ("FECS_SCTL",          0x409240),
    ("PFB_NISO_FLUSH",     0x100c70),
    ("FB_0x10ea00",        0x10ea00),
    ("CLK_0x132800",       0x132800),
    ("FBPA_0x9a0530",      0x9a0530),
    ("PWR_0x17e244",       0x17e244),
]


def snapshot(gpu, label):
    print(f"\n=== {label} ===")
    state = {}
    for name, addr in SNAPSHOT:
        try:
            v = gpu.rd32(addr)
        except Exception as e:
            v = None
        state[name] = v
        if v is None:
            print(f"  {name:20} 0x{addr:06x}: EXCEPTION")
        elif v == 0xffffffff:
            print(f"  {name:20} 0x{addr:06x}: DEAD (0xffffffff)")
        elif (v & 0xfff00000) == 0xbad00000 or (v & 0xffff0000) == 0xbadf0000:
            print(f"  {name:20} 0x{addr:06x}: REJECTED 0x{v:08x}")
        else:
            print(f"  {name:20} 0x{addr:06x}: 0x{v:08x}")
    return state


def diff(before, after):
    print("\n=== Diff ===")
    for name, addr in SNAPSHOT:
        b = before.get(name)
        a = after.get(name)
        if b != a:
            print(f"  {name:20}: 0x{b if b is not None else 0:08x} -> 0x{a if a is not None else 0:08x}")


def main():
    print("=" * 70)
    print("  HotReset Test (requires patched TinyGPU dext)")
    print("=" * 70)

    gpu = PascalGPU()
    boot = gpu.rd32(0)
    if boot == 0xffffffff:
        print("\nGPU not responding. Power-cycle the eGPU first.")
        return
    print(f"\nGPU: 0x{boot:08x}")

    # Make sure we're in clean PCI state
    cmd = gpu.cfg_read(0x04, 2)
    if not (cmd & 0x06):
        gpu.cfg_write(0x04, cmd | 0x06, 2)
    gpu.wr32(0x000200, 0xffffffff)
    time.sleep(0.05)

    before = snapshot(gpu, "BEFORE reset")

    # Issue the reset
    print("\n--- Calling gpu.dev.reset() (HotReset via upstream bridge SBR) ---")
    try:
        gpu.dev.reset()
        print("  reset() returned")
    except Exception as e:
        print(f"  reset() raised: {e}")
        return

    # Wait for link to come back up
    print("  Waiting 500ms for link...")
    time.sleep(0.5)

    # Re-enable PCI memory space (in case DriverKit didn't restore)
    cmd2 = gpu.cfg_read(0x04, 2)
    print(f"  PCI Command after reset: 0x{cmd2:04x}")
    if not (cmd2 & 0x06):
        print("  Re-enabling memory space")
        gpu.cfg_write(0x04, cmd2 | 0x06, 2)
        time.sleep(0.05)

    # Test if GPU is alive
    boot2 = gpu.rd32(0)
    print(f"  PMC_BOOT_0 after reset: 0x{boot2:08x}")
    if boot2 == 0xffffffff:
        print("  GPU is unresponsive after reset. May need to re-set PCI cmd.")
        # Try one more time
        gpu.cfg_write(0x04, 0x0006, 2)
        time.sleep(0.1)
        boot2 = gpu.rd32(0)
        print(f"  Second attempt: 0x{boot2:08x}")
        if boot2 == 0xffffffff:
            return

    # Re-establish PMC clean state
    gpu.wr32(0x000200, 0xffffffff)
    time.sleep(0.05)

    after = snapshot(gpu, "AFTER reset")
    diff(before, after)

    # Critical checks
    print("\n=== Critical signals ===")
    wpr_before = before.get("WPR2_HI") or 0
    wpr_after  = after.get("WPR2_HI") or 0
    if wpr_before == 0xccccfcfc and wpr_after != 0xccccfcfc:
        print(f"  *** WPR2_HI CLEARED: 0x{wpr_before:08x} -> 0x{wpr_after:08x} ***")
        print(f"  *** This means a real PCIe reset happened ***")

    fbp_before = before.get("PPRIV_FBP0_MASTER")
    fbp_after  = after.get("PPRIV_FBP0_MASTER")
    if fbp_before == 0xffffffff and fbp_after != 0xffffffff:
        print(f"  *** FBP STATION ALIVE: was dead, now 0x{fbp_after:08x} ***")

    pmu_sctl = after.get("PMU_SCTL") or 0
    if pmu_sctl != 0x3002 and pmu_sctl is not None:
        print(f"  *** PMU SCTL CHANGED: 0x{pmu_sctl:08x} (was 0x3002) ***")

    gpu.close()


if __name__ == "__main__":
    main()
