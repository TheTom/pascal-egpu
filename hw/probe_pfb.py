#!/usr/bin/env python3
"""Probe PFB / FBPA / clock register reachability before running devinit.

We're about to attempt running the VBIOS devinit script's DRAM init
subroutine on the host. Before we do that we need to know which of those
target registers are even reachable in the GPU's current state.

The devinit script writes to these regions (extracted from trace):
  - 0x10e000-0x10ec00  PFB DRAM training / FB controller
  - 0x9a0530-0x9a0560  FBPA clock controllers
  - 0x17e240-0x17e370  Power management
  - 0x132800-0x137100  Clock controllers
  - 0x419xxx           PGRAPH (we know FECS works)
  - 0x021800-0x021900  PMC area (some)
  - 0x040800-0x041c00  Other priv ring area

For each region, write a known pattern and read it back. Three outcomes:
  1. Read returns our pattern → fully writable, register exists
  2. Read returns 0xbadXXXXX → priv ring rejected (FBP/clock dead)
  3. Read returns 0xffffffff or differs → register sticky/strappable
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from transport.tinygrad_transport import PascalGPU


# Sample one register from each region the devinit script touches.
PROBES = [
    # (label, addr, write_value)
    # Register space we KNOW works
    ("PMC_BOOT_0",            0x000000, None),       # read-only
    ("PMC_ENABLE",            0x000200, None),
    ("FECS_MAILBOX0",         0x409040, 0xCAFEBABE),

    # PFB / FB controller
    ("PFB_NISO_FLUSH",        0x100c70, 0x00000001),
    ("PFB_FBHUB_NUM_ACTIVE_LTCS", 0x100eb8, None),
    ("FB_0x10e608",           0x10e608, 0x00000000),
    ("FB_0x10e60c",           0x10e60c, 0x00000001),
    ("FB_0x10e810",           0x10e810, 0x00000020),
    ("FB_0x10ea00",           0x10ea00, 0x00000000),
    ("FB_0x10e03c",           0x10e03c, 0x00023f01),

    # FBPA / clock
    ("FBPA_0x9a0530",         0x9a0530, 0x00000000),
    ("FBPA_0x9a0550",         0x9a0550, 0x00000002),

    # Power
    ("PWR_0x17e244",          0x17e244, None),
    ("PWR_0x17e314",          0x17e314, None),
    ("PWR_0x17e360",          0x17e360, 0x00010000),

    # Clock
    ("CLK_0x132800",          0x132800, None),
    ("CLK_0x137040",          0x137040, None),
    ("CLK_0x1370e0",          0x1370e0, None),

    # PGRAPH-internal
    ("GR_0x409654",           0x409654, 0x00000080),
    ("GR_0x419e2c",           0x419e2c, 0x00000000),
    ("GR_0x419f80",           0x419f80, 0x80000000),
    ("GR_0x40895c",           0x40895c, None),

    # Misc
    ("REG_0x021804",          0x021804, 0x00000000),
    ("REG_0x020024",          0x020024, None),
    ("REG_0x088a10",          0x088a10, 0x04fa04fa),

    # PRIV ring stations (sanity)
    ("PPRIV_FBP0",            0x12a270, None),
    ("PPRIV_GPC0",            0x128280, None),
]


def label_status(read_back, written):
    if read_back == 0xffffffff:
        return "DEAD (0xffffffff)"
    if (read_back & 0xfff00000) == 0xbad00000:
        return f"PRIV REJECTED (0x{read_back:08x})"
    if read_back == 0xbadf1100:
        return "FB UNINITIALIZED"
    if (read_back & 0xfff00000) == 0xbad0ac00:
        return "PRIV STATION DEAD"
    if written is None:
        return f"OK (read 0x{read_back:08x})"
    if read_back == written:
        return "WRITABLE ✓"
    return f"DIFF (got 0x{read_back:08x}, wrote 0x{written:08x})"


def main():
    print("=" * 70)
    print("  Pascal Register Reachability Probe")
    print("=" * 70)

    gpu = PascalGPU()

    boot = gpu.rd32(0)
    if boot == 0xffffffff:
        print("\nGPU not responding (PMC_BOOT_0 = 0xffffffff)")
        print("Power-cycle the eGPU and re-run.")
        return
    print(f"\nGPU alive: PMC_BOOT_0 = 0x{boot:08x}")

    # Make sure memory space decode is on (after hot-plug it can be off)
    cmd = gpu.cfg_read(0x04, 2)
    print(f"PCI Command: 0x{cmd:04x}")
    if not (cmd & 0x06):
        print("  Memory space decode disabled — enabling")
        gpu.cfg_write(0x04, cmd | 0x06, 2)
        time.sleep(0.05)

    # PMC clean state — first try max enable
    print("\nPMC_ENABLE = 0x7fffffff (try enable all)")
    gpu.wr32(0x000200, 0x7fffffff)
    time.sleep(0.1)
    pmc = gpu.rd32(0x000200)
    print(f"  read back: 0x{pmc:08x}")

    print(f"\n{'Label':<30} {'Addr':<10} {'Result'}")
    print("-" * 70)
    for label, addr, write_val in PROBES:
        try:
            if write_val is not None:
                gpu.wr32(addr, write_val)
            time.sleep(0.001)
            got = gpu.rd32(addr)
            status = label_status(got, write_val)
        except Exception as e:
            status = f"EXCEPTION: {e}"
            got = None
        print(f"{label:<30} 0x{addr:06x}   {status}")

    gpu.close()


if __name__ == "__main__":
    main()
