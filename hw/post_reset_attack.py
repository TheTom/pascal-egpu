#!/usr/bin/env python3
"""Post-reset rapid-fire init: race against the priv-level lockdown.

Theory: The Pascal GP106 comes up after PCIe reset with a brief window
before its internal state machines lock priv levels. If we can write
PLMs to "all unlocked" + drop SCTL UCODE_LEVEL + load FECS firmware
during that window, we may execute unsigned code.

Sequence:
  1. Snapshot pre-reset state at multiple key registers
  2. Call gpu.dev.reset() — patched dext tries WarmResetDisable+Enable
     (PERST# cycle), then WarmReset, HotReset, FLR
  3. Re-enable PCI memory space if needed
  4. IMMEDIATELY (no time.sleep) write PLMs to 0xff
  5. Write SCTL = 0 on FECS, MSENC, SEC2
  6. Snapshot at +1ms, +5ms, +20ms, +100ms
  7. Compare each to pre-reset and to "raw cold boot" expectations
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from transport.tinygrad_transport import PascalGPU


# Registers to snapshot — covers everything we care about
SNAPSHOT_REGS = {
    "PMC_BOOT_0":    0x000000,
    "PMC_ENABLE":    0x000200,
    "PMC_BOOT_42":   0x000a00,
    "PFB_CFG0":      0x100200,
    "PFB_NISO":      0x100c70,
    "PFB_LTCS":      0x100eb8,
    "WPR2_HI":       0x100cd4,
    "PMU_CPUCTL":    0x10a100,
    "PMU_SCTL":      0x10a240,
    "PMU_DMACTL":    0x10a10c,
    "PMU_HWCFG":     0x10a108,
    "FECS_CPUCTL":   0x409100,
    "FECS_SCTL":     0x409240,
    "FECS_DMACTL":   0x40910c,
    "MSENC_CPUCTL":  0x1c8100,
    "MSENC_SCTL":    0x1c8240,
    "SEC2_CPUCTL":   0x087100,
    "SEC2_SCTL":     0x087240,
    "GPCCS_CPUCTL":  0x41a100,
    "GPCCS_SCTL":    0x41a240,
    "FBPA_9a0530":   0x9a0530,
    "PWR_17e244":    0x17e244,
    "CLK_132800":    0x132800,
    "PPRIV_FBP0":    0x12a270,
    "PPRIV_GPC0":    0x128280,
    "PPRIV_SYS":     0x1200a8,
}

# PLMs to nuke (write 0xffffffff for full unlock attempt)
FALCON_BASES = [
    ("FECS",  0x409000),
    ("MSENC", 0x1c8000),
    ("SEC2",  0x087000),
    ("PMU",   0x10a000),
]
PLM_OFFSETS = [0x308, 0x30c, 0x310, 0x314, 0x318, 0x31c, 0x340,
               0x408, 0x40c, 0x410, 0x414, 0x418, 0x41c, 0x428, 0x42c]


def snap(gpu, label):
    out = {}
    for name, addr in SNAPSHOT_REGS.items():
        try:
            out[name] = gpu.rd32(addr)
        except Exception:
            out[name] = None
    return out


def diff(label, before, after):
    print(f"\n[diff] {label}")
    changed = 0
    for name, addr in SNAPSHOT_REGS.items():
        b = before.get(name)
        a = after.get(name)
        if b != a:
            changed += 1
            tag = ""
            if b is not None and a is not None:
                if (b & 0xffff0000) == 0xbadf0000 and (a & 0xffff0000) != 0xbadf0000:
                    tag = "  ★ became alive"
                elif (b & 0xffff0000) != 0xbadf0000 and a is not None and (a & 0xffff0000) == 0xbadf0000:
                    tag = "  became dead"
            b_s = f"0x{b:08x}" if b is not None else "?"
            a_s = f"0x{a:08x}" if a is not None else "?"
            print(f"  {name:14}: {b_s} -> {a_s}{tag}")
    if changed == 0:
        print("  (no changes)")


def attempt_unlock(gpu):
    """Race: write all PLMs to 0xff, drop SCTL, set sentinel."""
    for name, base in FALCON_BASES:
        # Unlock all PLMs
        for off in PLM_OFFSETS:
            try:
                gpu.wr32(base + off, 0xffffffff)
            except Exception:
                pass
        # Try SCTL = 0 (drop UCODE_LEVEL)
        try:
            gpu.wr32(base + 0x240, 0)
        except Exception:
            pass


def main():
    print("=" * 70)
    print("  Post-reset attack — race the priv lockdown")
    print("=" * 70)

    gpu = PascalGPU()
    if gpu.rd32(0) == 0xffffffff:
        print("\nGPU not responding. Power-cycle eGPU first."); return

    # Make sure PCI cmd is good
    cmd = gpu.cfg_read(0x04, 2)
    if not (cmd & 0x06):
        gpu.cfg_write(0x04, cmd | 0x06, 2)
    gpu.wr32(0x000200, 0xffffffff)
    time.sleep(0.05)

    # Pre-reset snapshot
    before = snap(gpu, "before")
    print("\nKey pre-reset state:")
    for k in ["PMC_BOOT_0", "PMU_SCTL", "FECS_SCTL", "PPRIV_FBP0", "PFB_LTCS"]:
        v = before.get(k)
        print(f"  {k:14}: 0x{v if v is not None else 0:08x}")

    # Issue reset
    print("\n[reset] calling gpu.dev.reset() — patched chain (PERST# cycle preferred)")
    t0 = time.perf_counter()
    try:
        gpu.dev.reset()
    except Exception as e:
        print(f"  reset() raised: {e}")
        return
    t_reset = (time.perf_counter() - t0) * 1000
    print(f"  reset() returned in {t_reset:.1f} ms")

    # Re-enable memory space ASAP
    cmd2 = gpu.cfg_read(0x04, 2)
    if not (cmd2 & 0x06):
        print("  re-enabling memory space")
        gpu.cfg_write(0x04, cmd2 | 0x06, 2)

    # Verify GPU alive
    boot = gpu.rd32(0)
    if boot == 0xffffffff:
        print(f"  GPU dead after reset (BOOT_0 = 0xffffffff)")
        # Try once more
        gpu.cfg_write(0x04, 0x0006, 2)
        time.sleep(0.05)
        boot = gpu.rd32(0)
        if boot == 0xffffffff:
            print("  Still dead. Power-cycle and try again."); return
    print(f"  PMC_BOOT_0 = 0x{boot:08x}")

    # IMMEDIATELY attempt unlock (no sleep)
    attempt_unlock(gpu)

    # Snap at multiple times
    snaps = [("immediate", snap(gpu, "immediate"))]

    for delay_ms, label in [(1, "+1ms"), (5, "+5ms"), (20, "+20ms"), (100, "+100ms")]:
        time.sleep(delay_ms / 1000.0)
        snaps.append((label, snap(gpu, label)))

    # Diff each against before
    for label, sn in snaps:
        diff(label, before, sn)

    # Critical signals
    print("\n=== Critical signals ===")
    final = snaps[-1][1]

    pmu_sctl_b = before.get("PMU_SCTL")
    pmu_sctl_a = final.get("PMU_SCTL")
    if pmu_sctl_a != pmu_sctl_b:
        print(f"  ★★★ PMU SCTL changed: 0x{pmu_sctl_b:08x} → 0x{pmu_sctl_a:08x}")
    else:
        print(f"  PMU SCTL unchanged (0x{pmu_sctl_a:08x})")

    for k in ["FECS_SCTL", "MSENC_SCTL", "SEC2_SCTL", "GPCCS_SCTL"]:
        b = before.get(k)
        a = final.get(k)
        if b != a:
            print(f"  ★★★ {k}: 0x{b or 0:08x} → 0x{a or 0:08x}")

    for k in ["PPRIV_FBP0", "PPRIV_GPC0", "PFB_LTCS", "FBPA_9a0530"]:
        b = before.get(k)
        a = final.get(k)
        was_dead = b is not None and (b & 0xffff0000) == 0xbadf0000
        now_alive = a is not None and (a & 0xffff0000) != 0xbadf0000
        if was_dead and now_alive:
            print(f"  ★★★ {k} BECAME ALIVE: 0x{b:08x} → 0x{a:08x}")

    gpu.close()


if __name__ == "__main__":
    main()
