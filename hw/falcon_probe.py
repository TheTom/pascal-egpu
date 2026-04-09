#!/usr/bin/env python3
"""Probe all Falcon engines on Pascal GP106.

Reads status registers from every known Falcon microcontroller
to determine their current state before we attempt init.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from transport.tinygpu_client import TinyGPUClient

# Falcon engine base addresses on Pascal GP106
FALCONS = {
    "PMU":   0x10a000,  # Power Management Unit
    "FECS":  0x409000,  # Front End Context Switch (in PGRAPH)
    "GPCCS": 0x41a000,  # GPC Context Switch (in PGRAPH)
    "SEC2":  0x840000,  # Security Engine 2
}

# Common Falcon register offsets
FALCON_IRQSTAT    = 0x008
FALCON_IRQMASK    = 0x018
FALCON_MAILBOX0   = 0x040
FALCON_MAILBOX1   = 0x044
FALCON_IDLESTATE  = 0x048  # Also doubles as reset control
FALCON_OS         = 0x050
FALCON_ENGCTL     = 0x060
FALCON_CPUCTL     = 0x100
FALCON_BOOTVEC    = 0x104
FALCON_HWCFG      = 0x108
FALCON_DMACTL     = 0x10c
FALCON_IMEM_SIZE  = 0x108  # HWCFG contains IMEM size


def probe_falcon(client, name, base):
    """Read status registers from a Falcon engine."""
    print(f"\n{'='*50}")
    print(f"  {name} Falcon (base: 0x{base:06x})")
    print(f"{'='*50}")

    regs = {}
    for reg_name, offset in [
        ("IRQSTAT",   FALCON_IRQSTAT),
        ("IRQMASK",   FALCON_IRQMASK),
        ("MAILBOX0",  FALCON_MAILBOX0),
        ("MAILBOX1",  FALCON_MAILBOX1),
        ("IDLESTATE", FALCON_IDLESTATE),
        ("OS",        FALCON_OS),
        ("ENGCTL",    FALCON_ENGCTL),
        ("CPUCTL",    FALCON_CPUCTL),
        ("BOOTVEC",   FALCON_BOOTVEC),
        ("HWCFG",     FALCON_HWCFG),
        ("DMACTL",    FALCON_DMACTL),
    ]:
        try:
            val = client.mmio_read32(0, base + offset)
            regs[reg_name] = val
            print(f"  {reg_name:12s} (+0x{offset:03x}): 0x{val:08x}")
        except Exception as e:
            print(f"  {reg_name:12s} (+0x{offset:03x}): ERROR - {e}")
            regs[reg_name] = None

    # Decode CPUCTL
    if regs.get("CPUCTL") is not None:
        cpuctl = regs["CPUCTL"]
        halted = bool(cpuctl & (1 << 4))
        stopped = bool(cpuctl & (1 << 5))
        print(f"\n  CPU State: {'HALTED' if halted else 'running'}, {'STOPPED' if stopped else 'active'}")

    # Decode HWCFG (contains IMEM/DMEM sizes)
    if regs.get("HWCFG") is not None:
        hwcfg = regs["HWCFG"]
        imem_size = ((hwcfg >> 0) & 0x1ff) * 256  # in bytes
        dmem_size = ((hwcfg >> 9) & 0x1ff) * 256
        print(f"  IMEM size: {imem_size} bytes ({imem_size//1024}KB)")
        print(f"  DMEM size: {dmem_size} bytes ({dmem_size//1024}KB)")

    # Decode IDLESTATE
    if regs.get("IDLESTATE") is not None:
        idle = regs["IDLESTATE"]
        ext_idle = bool(idle & (1 << 0))
        print(f"  Idle: {'YES (engine idle)' if ext_idle else 'NO (busy)'}")

    return regs


def probe_pgraph(client):
    """Check PGRAPH engine state."""
    print(f"\n{'='*50}")
    print(f"  PGRAPH Engine Status")
    print(f"{'='*50}")

    # PMC_ENABLE - check if GR is enabled
    pmc_enable = client.mmio_read32(0, 0x000200)
    gr_enabled = bool(pmc_enable & (1 << 12))
    print(f"  PMC_ENABLE: 0x{pmc_enable:08x}")
    print(f"  GR engine: {'ENABLED' if gr_enabled else 'DISABLED'}")

    # PGRAPH status
    try:
        gr_status = client.mmio_read32(0, 0x400700)
        print(f"  GR_STATUS (0x400700): 0x{gr_status:08x}")
    except:
        print(f"  GR_STATUS: read failed (PGRAPH may be powered down)")

    # PFIFO status
    try:
        pfifo_status = client.mmio_read32(0, 0x002100)
        print(f"  PFIFO_INTR (0x002100): 0x{pfifo_status:08x}")
    except:
        print(f"  PFIFO_INTR: read failed")

    # WPR2 (Write Protected Region) - indicates secure boot state
    try:
        wpr2_lo = client.mmio_read32(0, 0x100CE0)
        wpr2_hi = client.mmio_read32(0, 0x100CE4)
        print(f"  WPR2_ADDR_LO (0x100CE0): 0x{wpr2_lo:08x}")
        print(f"  WPR2_ADDR_HI (0x100CE4): 0x{wpr2_hi:08x}")
        if wpr2_hi != 0 or wpr2_lo != 0:
            print(f"  WPR2: ACTIVE (secure boot has run before)")
        else:
            print(f"  WPR2: NOT SET (secure boot needed)")
    except:
        print(f"  WPR2: read failed")


def probe_memory(client):
    """Check memory controller state."""
    print(f"\n{'='*50}")
    print(f"  Memory Controller (PFB)")
    print(f"{'='*50}")

    # Try to read VRAM size indicators
    for name, addr in [
        ("NV_PFB_PRI_MMU_CTRL", 0x100C80),
        ("NV_PFB_PRI_MMU_INVALIDATE", 0x100CB8),
        ("NV_PFB_NISO_FLUSH_SYSMEM_ADDR", 0x100C10),
    ]:
        try:
            val = client.mmio_read32(0, addr)
            print(f"  {name}: 0x{val:08x}")
        except:
            print(f"  {name}: read failed")


def main():
    client = TinyGPUClient()
    print("=== Pascal GP106 Falcon & Engine Probe ===")

    try:
        client.connect()
        print("Connected to TinyGPU server!")
    except RuntimeError as e:
        print(f"Failed: {e}")
        return

    # Probe each Falcon engine
    falcon_states = {}
    for name, base in FALCONS.items():
        falcon_states[name] = probe_falcon(client, name, base)

    # Probe PGRAPH and other engines
    probe_pgraph(client)
    probe_memory(client)

    # Summary
    print(f"\n{'='*50}")
    print(f"  SUMMARY")
    print(f"{'='*50}")
    for name, regs in falcon_states.items():
        cpuctl = regs.get("CPUCTL")
        if cpuctl is not None:
            state = "HALTED" if cpuctl & 0x10 else "RUNNING"
        else:
            state = "UNKNOWN"
        print(f"  {name:8s}: {state}")

    client.close()


if __name__ == "__main__":
    main()
