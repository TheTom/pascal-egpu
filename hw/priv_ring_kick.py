#!/usr/bin/env python3
"""Try to re-trigger Pascal priv ring enumeration without breaking things.

State observed:
  MASTER_RING_GLOBAL_CTL = 0x53 (alive)
  MASTER_RING_ENUMERATE  = 0x1  (enumeration completed earlier, no FBPs found)
  MASTER_RING_START_RING = 0    (ring not currently being started)
  HUB_RING_LIST_VLD      = 0    (hub ring list invalid — no valid stations)
  SYS_PRI_FBP_LIST       = 1    (only 1 FBP, others unknown to ring)

Try (carefully, with rollback):
  1. Read GLOBAL_CTL
  2. Write START_RING = 1 — request ring start
  3. Write ENUMERATE = 1 — request re-enumeration
  4. Wait, read status
  5. Check if FBP/GPC stations come alive
  6. If anything goes wrong, restore PMC_ENABLE
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from transport.tinygrad_transport import PascalGPU


def snap(gpu):
    return {
        "PMC_BOOT_0": gpu.rd32(0),
        "PMC_ENABLE": gpu.rd32(0x200),
        "GLOBAL_CTL": gpu.rd32(0x120048),
        "GLOBAL_STATUS": gpu.rd32(0x12004c),
        "INTR_STATUS": gpu.rd32(0x120050),
        "START_RING": gpu.rd32(0x12005c),
        "ENUMERATE": gpu.rd32(0x120060),
        "FBP_LIST": gpu.rd32(0x1200ac),
        "FBP0": gpu.rd32(0x12a270),
        "GPC0": gpu.rd32(0x128280),
        "PFB_LTCS": gpu.rd32(0x100eb8),
        "PMU_CPUCTL": gpu.rd32(0x10a100),
    }


def show(label, s):
    print(f"\n[{label}]")
    for k, v in s.items():
        print(f"  {k:14}: 0x{v:08x}")


def diff(b, a):
    print("\n[diff]")
    for k in b:
        if b[k] != a[k]:
            print(f"  {k:14}: 0x{b[k]:08x} -> 0x{a[k]:08x}")


def main():
    gpu = PascalGPU()
    if gpu.rd32(0) == 0xffffffff:
        print("GPU dead"); return
    cmd = gpu.cfg_read(0x04, 2)
    if not (cmd & 0x06):
        gpu.cfg_write(0x04, cmd | 0x06, 2)
    gpu.wr32(0x000200, 0xffffffff)
    time.sleep(0.05)

    s0 = snap(gpu); show("baseline", s0)

    # Test 1: Write START_RING = 1
    print("\n>>> Write START_RING = 1")
    gpu.wr32(0x12005c, 1)
    time.sleep(0.01)
    s1 = snap(gpu); diff(s0, s1)

    # Test 2: Write START_RING = 0 (clear)
    print("\n>>> Write START_RING = 0")
    gpu.wr32(0x12005c, 0)
    time.sleep(0.01)
    s2 = snap(gpu); diff(s1, s2)

    # Test 3: Write ENUMERATE_AND_START_RING (bit 0=START, bit 1=ENUMERATE)
    print("\n>>> Write ENUMERATE = 0x3 (start + enumerate)")
    gpu.wr32(0x120060, 0x3)
    time.sleep(0.05)
    s3 = snap(gpu); diff(s2, s3)

    # Test 4: Write ENUMERATE = 0x1
    print("\n>>> Write ENUMERATE = 0x1")
    gpu.wr32(0x120060, 0x1)
    time.sleep(0.05)
    s4 = snap(gpu); diff(s3, s4)

    # Recovery check
    print("\n>>> Recovery check")
    gpu.wr32(0x000200, 0xffffffff)
    time.sleep(0.05)
    sf = snap(gpu); show("final", sf)
    diff(s0, sf)

    gpu.close()


if __name__ == "__main__":
    main()
