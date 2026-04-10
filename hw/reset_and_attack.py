#!/usr/bin/env python3
"""Full post-reset attack sequence.

Assumes patched TinyGPU dext is installed (with WarmResetDisable chain).
Does the following:
  1. Capture pre-reset state across all critical registers
  2. Call gpu.dev.reset() — dext will try PERST# → WarmReset → HotReset → FLR
  3. Re-enable PCI memory space IMMEDIATELY
  4. Race-attack: write PLM unlocks + SCTL=0 across all writable Falcons
  5. Capture state at +1ms, +5ms, +20ms, +100ms
  6. Classify the result:
       - "Silicon reset": if SCTL, WPR2_HI, FBP/GPC changed meaningfully
       - "Link reset only": if AER errors cleared but silicon state identical
       - "No-op": if nothing changed at all
  7. Write structured JSON result for later diffing
"""

import os
import sys
import json
import time
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from transport.tinygrad_transport import PascalGPU


# Full register snapshot — everything we care about
SNAPSHOT_REGS = {
    "PMC_BOOT_0":     0x000000,
    "PMC_ENABLE":     0x000200,
    "PMC_BOOT_42":    0x000a00,
    "PBUS_TIMEOUT":   0x001130,
    "PFB_CFG0":       0x100200,
    "PFB_NISO":       0x100c70,
    "PFB_LTCS":       0x100eb8,
    # Note: 0x100cd4 observed as a time-varying counter, not WPR2_HI.
    # WPR2_HI address on GP10x is not publicly documented; skipping.
    "PMU_CPUCTL":     0x10a100,
    "PMU_SCTL":       0x10a240,
    "PMU_DMACTL":     0x10a10c,
    "PMU_HWCFG":      0x10a108,
    "PMU_ENGCTL":     0x10a0a4,
    "FECS_CPUCTL":    0x409100,
    "FECS_SCTL":      0x409240,
    "FECS_DMACTL":    0x40910c,
    "FECS_HWCFG":     0x409108,
    "MSENC_CPUCTL":   0x1c8100,
    "MSENC_SCTL":     0x1c8240,
    "SEC2_CPUCTL":    0x087100,
    "SEC2_SCTL":      0x087240,
    "GPCCS_CPUCTL":   0x41a100,
    "GPCCS_SCTL":     0x41a240,
    "FBPA_9a0530":    0x9a0530,
    "PWR_17e244":     0x17e244,
    "CLK_132800":     0x132800,
    "PPRIV_FBP0":     0x12a270,
    "PPRIV_GPC0":     0x128280,
    "PPRIV_SYS":      0x1200a8,
    "PPRIV_GCTL":     0x120048,
    "PPRIV_ENUM":     0x120060,
}

FALCON_BASES = [
    ("FECS",  0x409000),
    ("MSENC", 0x1c8000),
    ("SEC2",  0x087000),
    ("PMU",   0x10a000),
    ("GPCCS", 0x41a000),
]

PLM_OFFSETS = [0x308, 0x30c, 0x310, 0x314, 0x318, 0x31c, 0x340,
               0x408, 0x40c, 0x410, 0x414, 0x418, 0x41c, 0x428, 0x42c]


def snap(gpu):
    out = {}
    for name, addr in SNAPSHOT_REGS.items():
        try:
            out[name] = gpu.rd32(addr)
        except Exception:
            out[name] = None
    return out


def classify(val):
    if val is None:
        return "exc"
    if val == 0xffffffff:
        return "DEAD"
    if (val & 0xffff0000) == 0xbadf0000:
        return "BADF"  # engine not registered / not ready
    if (val & 0xfff00000) == 0xbad00000:
        return "BAD"   # priv ring reject
    return "OK"


def diff_report(label, before, after):
    changes = []
    for name in SNAPSHOT_REGS:
        b = before.get(name)
        a = after.get(name)
        if b != a:
            bc = classify(b)
            ac = classify(a)
            tag = ""
            if bc in ("DEAD", "BADF", "BAD") and ac == "OK":
                tag = " ★★★ BECAME ALIVE"
            elif bc == "OK" and ac in ("DEAD", "BADF", "BAD"):
                tag = "     became rejected"
            b_s = f"0x{b:08x}" if b is not None else "?"
            a_s = f"0x{a:08x}" if a is not None else "?"
            changes.append((name, b_s, a_s, tag))
    print(f"\n[{label}]  {len(changes)} changes")
    for name, b, a, tag in changes:
        print(f"  {name:14}: {b} -> {a}{tag}")
    return changes


def attempt_unlock(gpu):
    """Race: write all PLMs to 0xff, drop SCTL, clear DMACTL."""
    for _, base in FALCON_BASES:
        for off in PLM_OFFSETS:
            try: gpu.wr32(base + off, 0xffffffff)
            except Exception: pass
        try: gpu.wr32(base + 0x240, 0)   # SCTL = 0
        except Exception: pass
        try: gpu.wr32(base + 0x10c, 0)   # DMACTL = 0
        except Exception: pass


def get_console_logs():
    """Read recent TinyGPU os_log lines to see which reset type executed."""
    try:
        out = subprocess.run(
            ["log", "show", "--last", "30s", "--predicate",
             'eventMessage CONTAINS "tinygpu"', "--style", "compact"],
            capture_output=True, text=True, timeout=10
        )
        return out.stdout
    except Exception as e:
        return f"(log read failed: {e})"


def main():
    print("=" * 72)
    print("  Pascal eGPU — Full reset + race attack")
    print("=" * 72)

    gpu = PascalGPU()

    boot = gpu.rd32(0)
    if boot == 0xffffffff:
        print("\nGPU not responding. Power-cycle the eGPU enclosure first.")
        sys.exit(2)
    print(f"\nGPU alive: PMC_BOOT_0 = 0x{boot:08x}")

    cmd_before = gpu.cfg_read(0x04, 2)
    if not (cmd_before & 0x06):
        gpu.cfg_write(0x04, cmd_before | 0x06, 2)
    gpu.wr32(0x000200, 0xffffffff)
    time.sleep(0.05)
    pmc = gpu.rd32(0x000200)
    print(f"PMC_ENABLE: 0x{pmc:08x}")

    # Pre-reset snapshot
    before = snap(gpu)
    print("\nKey pre-reset state:")
    for k in ["PMU_SCTL", "FECS_SCTL", "SEC2_SCTL", "PPRIV_FBP0", "PPRIV_GPC0",
              "PFB_CFG0", "WPR2_HI_1"]:
        v = before.get(k, 0)
        print(f"  {k:14}: 0x{v if v is not None else 0:08x}  [{classify(v)}]")

    # Reset
    print("\n>>> Calling gpu.dev.reset() — dext tries PERST# → Warm → Hot → FLR chain")
    t0 = time.perf_counter()
    try:
        gpu.dev.reset()
    except Exception as e:
        print(f"  reset() raised: {e}")
        sys.exit(3)
    t_ms = (time.perf_counter() - t0) * 1000
    print(f"  reset() returned in {t_ms:.1f} ms")

    # Re-enable PCI cmd if needed
    cmd_after = gpu.cfg_read(0x04, 2)
    if not (cmd_after & 0x06):
        print("  re-enabling PCI memory space")
        gpu.cfg_write(0x04, cmd_after | 0x06, 2)

    # Is GPU alive?
    boot2 = gpu.rd32(0)
    if boot2 == 0xffffffff:
        print(f"  GPU dead after reset. Retrying cmd enable...")
        gpu.cfg_write(0x04, 0x0006, 2)
        time.sleep(0.1)
        boot2 = gpu.rd32(0)
        if boot2 == 0xffffffff:
            print("  GPU still dead. Power-cycle required.")
            sys.exit(4)
    print(f"  PMC_BOOT_0 post-reset: 0x{boot2:08x}")

    # IMMEDIATE race attack — no time.sleep
    attempt_unlock(gpu)

    # Capture at multiple delays
    snaps = [("immediate", snap(gpu))]
    for delay_ms, label in [(1, "+1ms"), (5, "+5ms"), (20, "+20ms"), (100, "+100ms")]:
        time.sleep(delay_ms / 1000.0)
        snaps.append((label, snap(gpu)))

    # Diff each
    all_changes = {}
    for label, sn in snaps:
        changes = diff_report(label, before, sn)
        all_changes[label] = [(n, b, a, t) for n, b, a, t in changes]

    # Critical signals
    print("\n" + "=" * 72)
    print("  CRITICAL SIGNALS")
    print("=" * 72)
    final = snaps[-1][1]
    wins = []

    # Check for unlocked SCTL
    for k in ["PMU_SCTL", "FECS_SCTL", "MSENC_SCTL", "SEC2_SCTL", "GPCCS_SCTL"]:
        b = before.get(k)
        a = final.get(k)
        if b != a and a not in (None, 0xffffffff):
            if isinstance(a, int) and (a & 0x3000) != 0x3000:
                wins.append(f"  ★★★ {k}: 0x{b or 0:08x} → 0x{a:08x}  UCODE_LEVEL DROPPED")
            else:
                wins.append(f"  {k}: 0x{b or 0:08x} → 0x{a:08x}  changed but still locked")

    # Check priv ring stations
    for k in ["PPRIV_FBP0", "PPRIV_GPC0", "PFB_LTCS", "PFB_CFG0"]:
        b = before.get(k)
        a = final.get(k)
        bc, ac = classify(b), classify(a)
        if bc in ("BADF", "BAD", "DEAD") and ac == "OK":
            wins.append(f"  ★★★ {k} BECAME ALIVE: 0x{b or 0:08x} → 0x{a:08x}")

    if wins:
        print("\n".join(wins))
        print("\n  >>> AT LEAST PARTIAL VICTORY <<<")
    else:
        print("\n  No protected region unlocked.")
        if any(all_changes.values()):
            print("  But some registers did change — see diff above.")
        else:
            print("  Nothing changed at all. Reset was a no-op.")

    # Console logs
    print("\n" + "=" * 72)
    print("  Console.app logs (what reset type the dext actually ran)")
    print("=" * 72)
    print(get_console_logs())

    # Write JSON
    result = {
        "timestamp": time.time(),
        "pmc_boot_0_before": before.get("PMC_BOOT_0"),
        "pmc_boot_0_after": final.get("PMC_BOOT_0"),
        "before": before,
        "snapshots": {label: sn for label, sn in snaps},
        "wins": wins,
    }
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            f"reset_result_{int(time.time())}.json")
    with open(out_path, 'w') as f:
        json.dump(result, f, indent=2, default=str)
    print(f"\nResult saved: {out_path}")

    gpu.close()


if __name__ == "__main__":
    main()
