#!/usr/bin/env python3
"""Read-only probe of Pascal priv ring master registers.

PPRIV_MASTER controls priv ring routing — which stations are enabled,
how they're enumerated, error reporting, etc. We previously broke things
writing to these. Now: just READ them and look for a pattern.

Key registers (from nouveau gp100):
  0x120000-0x1200ff: PPRIV_MASTER global
  0x120048: PPRIV_MASTER_RING_GLOBAL_CTL
  0x12004c: PPRIV_MASTER_RING_GLOBAL_STATUS
  0x120050: PPRIV_MASTER_RING_INTERRUPT
  0x12005c: PPRIV_MASTER_RING_START_RING
  0x120060: PPRIV_MASTER_RING_ENUMERATE
  0x122204: PPRIV_MASTER_HUB_RING_LIST_VLD
  0x12a370: PPRIV_FBP0_*
  0x128380: PPRIV_GPC0_*
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from transport.tinygrad_transport import PascalGPU


# Wide read of PPRIV master space (0x120000-0x12ffff)
RANGES = [
    ("MASTER_global", 0x120000, 0x100),
    ("MASTER_ring",   0x120040, 0x100),
    ("MASTER_intr",   0x120100, 0x100),
    ("HUB",           0x122200, 0x100),
    ("SYS",           0x121000, 0x100),
    ("GPC_routing",   0x128000, 0x400),
    ("FBP_routing",   0x12a000, 0x400),
]


def main():
    gpu = PascalGPU()
    if gpu.rd32(0) == 0xffffffff:
        print("GPU dead"); return
    cmd = gpu.cfg_read(0x04, 2)
    if not (cmd & 0x06):
        gpu.cfg_write(0x04, cmd | 0x06, 2)
    gpu.wr32(0x000200, 0xffffffff)
    time.sleep(0.05)

    print(f"GPU: 0x{gpu.rd32(0):08x}\n")

    for label, base, size in RANGES:
        print(f"\n=== {label} @ 0x{base:06x}-0x{base+size:06x} ===")
        alive_count = 0
        first_alive = None
        for off in range(0, size, 4):
            v = gpu.rd32(base + off)
            is_dead = v == 0xffffffff or (v & 0xfff00000) == 0xbad00000 or (v & 0xffff0000) == 0xbadf0000
            if not is_dead:
                if alive_count < 16:
                    print(f"  +0x{off:03x} (0x{base+off:06x}): 0x{v:08x}")
                alive_count += 1
                if first_alive is None:
                    first_alive = off
        print(f"  Total alive: {alive_count} / {size//4}")

    # Specifically check key priv ring regs
    print("\n=== Key priv ring registers ===")
    keys = [
        ("MASTER_RING_GLOBAL_CTL",      0x120048),
        ("MASTER_RING_GLOBAL_STATUS",   0x12004c),
        ("MASTER_RING_INTR_STATUS",     0x120050),
        ("MASTER_RING_INTR_ENABLE",     0x120054),
        ("MASTER_RING_START_RING",      0x12005c),
        ("MASTER_RING_ENUMERATE",       0x120060),
        ("MASTER_RING_VL_FBP",          0x12005c),
        ("HUB_RING_LIST_VLD",           0x122204),
        ("HUB_RING_NETLIST",            0x122210),
        ("SYS_DECODE_CFG",              0x1200a8),
        ("SYS_PRI_FBP_LIST",            0x1200ac),
    ]
    for name, addr in keys:
        v = gpu.rd32(addr)
        marker = " ALIVE" if (v != 0xffffffff and (v & 0xfff00000) != 0xbad00000) else " dead"
        print(f"  {name:30} 0x{addr:06x}: 0x{v:08x}{marker}")

    gpu.close()


if __name__ == "__main__":
    main()
