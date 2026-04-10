#!/usr/bin/env python3
"""Deep probe of PMU Falcon state.

The PMU is at 0x10a000 and now appears reachable. We previously thought
it was dead because:
  - SCTL = 0x3002 (HSMODE + UCODE_LEVEL=3)
  - DMACTL bit 7 (REQUIRE_CTX) set
  - IMEM writes returned 0xdead5ec1

But maybe with the right PMC state (and now that we know how to keep
priv ring up), we can:
  1. Reset PMU Falcon properly
  2. Clear DMACTL_REQUIRE_CTX
  3. Write SCTL to drop HSMODE
  4. Load unsigned ucode into IMEM
  5. Execute it

Let's see what's actually happening.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from transport.tinygrad_transport import PascalGPU


PMU = 0x10a000

# Falcon offsets (relative to engine base)
F_IRQSSET    = 0x000
F_IRQSCLR    = 0x004
F_IRQSTAT    = 0x008
F_IRQMODE    = 0x00c
F_IRQMSET    = 0x010
F_IRQMCLR    = 0x014
F_IRQMASK    = 0x018
F_IRQDEST    = 0x01c
F_GPTMRINT   = 0x020
F_GPTMRVAL   = 0x024
F_GPTMRCTL   = 0x028
F_PTIMER0    = 0x02c
F_PTIMER1    = 0x030
F_WDTMRVAL   = 0x034
F_WDTMRCTL   = 0x038
F_MAILBOX0   = 0x040
F_MAILBOX1   = 0x044
F_ITFEN      = 0x048
F_IDLESTATE  = 0x04c
F_CURCTX     = 0x050
F_NXTCTX     = 0x054
F_CTXACK     = 0x058
F_FHSTATE    = 0x05c
F_PRIVSTATE  = 0x060
F_SFTRESET   = 0x064
F_OS         = 0x080
F_RM         = 0x084
F_SOFT_PM    = 0x088
F_SOFT_MODE  = 0x08c
F_DEBUG1     = 0x090
F_DEBUGINFO  = 0x094
F_IBRKPT1    = 0x098
F_IBRKPT2    = 0x09c
F_CGCTL      = 0x0a0
F_ENGCTL     = 0x0a4
F_PMM        = 0x0a8
F_ADDR       = 0x0ac
F_CPUCTL     = 0x100
F_BOOTVEC    = 0x104
F_HWCFG      = 0x108
F_DMACTL     = 0x10c
F_DMATRFBASE = 0x110
F_DMATRFMOFFS= 0x114
F_DMATRFCMD  = 0x118
F_DMATRFFBOFFS = 0x11c
F_DMAPOLL_FB = 0x120
F_DMAPOLL_CP = 0x124
F_IMCTL      = 0x180
F_IMCTL_DEBUG = 0x184
F_IMSTAT     = 0x144
F_TRACEIDX   = 0x148
F_TRACEPC    = 0x14c
F_IMFILLRNG0 = 0x150
F_IMFILLRNG1 = 0x154
F_IMFILLCTL  = 0x158
F_IMCTL_     = 0x15c
F_EXCI       = 0x160
F_SVEC_SPR   = 0x164
F_RSTAT0     = 0x168
F_RSTAT3     = 0x16c
F_HWCFG2     = 0x16c
F_CPUCTL_ALIAS = 0x130
F_DBGCTL     = 0x134
F_SCP_CTL_STAT = 0x138
F_SCTL       = 0x240


def dump_falcon(gpu, base, name):
    print(f"\n{name} @ 0x{base:06x}:")
    regs = [
        ("CPUCTL",         F_CPUCTL),
        ("CPUCTL_ALIAS",   F_CPUCTL_ALIAS),
        ("BOOTVEC",        F_BOOTVEC),
        ("HWCFG",          F_HWCFG),
        ("HWCFG2",         F_HWCFG2),
        ("DMACTL",         F_DMACTL),
        ("SCTL",           F_SCTL),
        ("MAILBOX0",       F_MAILBOX0),
        ("MAILBOX1",       F_MAILBOX1),
        ("ITFEN",          F_ITFEN),
        ("OS",             F_OS),
        ("DEBUG1",         F_DEBUG1),
        ("ENGCTL",         F_ENGCTL),
        ("IRQSTAT",        F_IRQSTAT),
        ("IRQMASK",        F_IRQMASK),
        ("FHSTATE",        F_FHSTATE),
        ("PRIVSTATE",      F_PRIVSTATE),
        ("IDLESTATE",      F_IDLESTATE),
        ("RSTAT0",         F_RSTAT0),
    ]
    for label, off in regs:
        v = gpu.rd32(base + off)
        print(f"  {label:14}: 0x{v:08x}")


def main():
    print("=" * 70)
    print("  PMU Falcon Deep Probe")
    print("=" * 70)

    gpu = PascalGPU()
    boot = gpu.rd32(0)
    if boot == 0xffffffff:
        print("GPU dead — recover first"); return
    print(f"GPU: 0x{boot:08x}")

    cmd = gpu.cfg_read(0x04, 2)
    if not (cmd & 0x06):
        gpu.cfg_write(0x04, cmd | 0x06, 2)

    # Establish state
    gpu.wr32(0x000200, 0xffffffff)
    time.sleep(0.05)
    pmc = gpu.rd32(0x000200)
    print(f"PMC_ENABLE = 0x{pmc:08x}")

    # 1) Initial PMU state
    dump_falcon(gpu, PMU, "PMU initial state")

    # 2) Try clearing DMACTL_REQUIRE_CTX
    print(f"\n[Test] Write DMACTL = 0...")
    gpu.wr32(PMU + F_DMACTL, 0)
    dmactl = gpu.rd32(PMU + F_DMACTL)
    print(f"  DMACTL = 0x{dmactl:08x}")

    # 3) Try writing SCTL (drop HSMODE)
    print(f"\n[Test] Write SCTL = 0...")
    sctl_before = gpu.rd32(PMU + F_SCTL)
    gpu.wr32(PMU + F_SCTL, 0)
    sctl_after = gpu.rd32(PMU + F_SCTL)
    print(f"  SCTL: 0x{sctl_before:08x} -> 0x{sctl_after:08x}")

    # 4) Try writing CPUCTL_ALIAS (writable when ALIAS_EN set)
    print(f"\n[Test] Write CPUCTL_ALIAS = 0x10 (HALT)...")
    gpu.wr32(PMU + F_CPUCTL_ALIAS, 0x10)
    cpuctl = gpu.rd32(PMU + F_CPUCTL)
    cpuctl_alias = gpu.rd32(PMU + F_CPUCTL_ALIAS)
    print(f"  CPUCTL: 0x{cpuctl:08x}  ALIAS: 0x{cpuctl_alias:08x}")

    # 5) Try Falcon soft reset via SFTRESET
    print(f"\n[Test] Falcon soft reset (SFTRESET)...")
    gpu.wr32(PMU + F_SFTRESET, 0xffffffff)
    time.sleep(0.05)
    sftreset = gpu.rd32(PMU + F_SFTRESET)
    print(f"  SFTRESET = 0x{sftreset:08x}")

    # 6) Try CPUCTL = 1<<2 (HRESET)
    print(f"\n[Test] CPUCTL = 0x4 (HRESET)...")
    gpu.wr32(PMU + F_CPUCTL, 0x4)
    time.sleep(0.05)
    cpuctl = gpu.rd32(PMU + F_CPUCTL)
    print(f"  CPUCTL = 0x{cpuctl:08x}")

    # 7) Re-dump
    dump_falcon(gpu, PMU, "PMU after reset attempts")

    # 8) Now try IMEM write
    print(f"\n[Test] PMU IMEM write at offset 0...")
    gpu.wr32(PMU + 0x180, (0 << 2) | (1 << 24))  # IMEMC: word 0, AINCW
    gpu.wr32(PMU + 0x188, 0)                     # IMEMT: tag 0
    gpu.wr32(PMU + 0x184, 0xCAFEBABE)            # IMEMD: write
    gpu.wr32(PMU + 0x184, 0xDEADBEEF)
    # Read back
    gpu.wr32(PMU + 0x180, (0 << 2) | (1 << 25))  # IMEMC: AINCR
    v0 = gpu.rd32(PMU + 0x184)
    v1 = gpu.rd32(PMU + 0x184)
    print(f"  IMEM[0]: 0x{v0:08x}  (want 0xCAFEBABE)")
    print(f"  IMEM[4]: 0x{v1:08x}  (want 0xDEADBEEF)")
    if v0 == 0xCAFEBABE:
        print("\n  *** PMU IMEM IS WRITABLE! ***")
    elif v0 == 0xdead5ec1:
        print("\n  PMU IMEM blocked: 0xdead5ec1 (HS security)")
    else:
        print(f"\n  PMU IMEM unexpected: 0x{v0:08x}")

    # 9) Same test for DMEM
    print(f"\n[Test] PMU DMEM write at offset 0...")
    gpu.wr32(PMU + 0x1c0, (0 << 2) | (1 << 24))  # DMEMC: word 0, AINCW
    gpu.wr32(PMU + 0x1c4, 0xFEEDFACE)
    gpu.wr32(PMU + 0x1c0, (0 << 2) | (1 << 25))
    v = gpu.rd32(PMU + 0x1c4)
    print(f"  DMEM[0]: 0x{v:08x}  (want 0xFEEDFACE)")

    gpu.close()


if __name__ == "__main__":
    main()
