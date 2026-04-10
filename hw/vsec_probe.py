#!/usr/bin/env python3
"""Probe NVIDIA Vendor-Specific Extended Capability (VSEC) and
Secondary PCIe capability for hidden reset paths.

VSEC at 0x600 — NVIDIA-specific. Various GPUs use this for reset,
Optimus, NVLink. We've never explored this on the 1060.

Secondary PCIe at 0x900 — has Link Control 3 with "Perform
Equalization" which retrains the link. Less drastic than SBR but
can change device state.

Also: AER at 0x420 — read uncorrectable error status to see what
errors the GPU has logged. May explain why priv stations are dead.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from transport.tinygrad_transport import PascalGPU


def dump_cap(gpu, base, name, length=64):
    print(f"\n{name} @ 0x{base:03x}:")
    for off in range(0, length, 4):
        try:
            v = gpu.cfg_read(base + off, 4)
        except Exception as e:
            v = None
            print(f"  +0x{off:02x}: EXC")
            continue
        print(f"  +0x{off:02x}: 0x{v:08x}")


def main():
    gpu = PascalGPU()
    if gpu.rd32(0) == 0xffffffff:
        print("GPU dead"); return
    cmd = gpu.cfg_read(0x04, 2)
    if not (cmd & 0x06):
        gpu.cfg_write(0x04, cmd | 0x06, 2)

    # Dump the VSEC fully
    dump_cap(gpu, 0x600, "NVIDIA VSEC", length=64)

    # Dump Secondary PCIe
    dump_cap(gpu, 0x900, "Secondary PCIe", length=64)

    # Dump AER
    dump_cap(gpu, 0x420, "AER", length=64)

    # AER specifically — clear error statuses
    print("\nAER UNCOR_STATUS (0x420 + 0x04):")
    uncor = gpu.cfg_read(0x420 + 0x04, 4)
    print(f"  current: 0x{uncor:08x}")
    if uncor:
        print("  Clearing all errors...")
        gpu.cfg_write(0x420 + 0x04, 0xffffffff, 4)
        new = gpu.cfg_read(0x420 + 0x04, 4)
        print(f"  after clear: 0x{new:08x}")

    print("\nAER COR_STATUS (0x420 + 0x10):")
    cor = gpu.cfg_read(0x420 + 0x10, 4)
    print(f"  current: 0x{cor:08x}")
    if cor:
        gpu.cfg_write(0x420 + 0x10, 0xffffffff, 4)
        print(f"  after clear: 0x{gpu.cfg_read(0x420 + 0x10, 4):08x}")

    # Look at the legacy PCI capabilities (PMC at 0x60, MSI at 0x68, PCIe at 0x78)
    print("\nLegacy PCI caps:")
    dump_cap(gpu, 0x60, "PMC", length=8)
    dump_cap(gpu, 0x68, "MSI", length=20)
    dump_cap(gpu, 0x78, "PCIe", length=64)

    # PCIe Cap: Device Control 2 has bit 4 = "AtomicOp Egress Blocking"
    # and Device Control has bit 15 = "BridgeConfigRetry Enable"
    # Most importantly: Device Status has bit 4 = "Transactions Pending"
    pcie_dev_ctl = gpu.cfg_read(0x78 + 0x08, 2)
    pcie_dev_sts = gpu.cfg_read(0x78 + 0x0a, 2)
    print(f"\nPCIe DevCtl: 0x{pcie_dev_ctl:04x}")
    print(f"PCIe DevSts: 0x{pcie_dev_sts:04x}")
    print(f"  CorErr: {(pcie_dev_sts >> 0) & 1}")
    print(f"  NonFatal: {(pcie_dev_sts >> 1) & 1}")
    print(f"  Fatal: {(pcie_dev_sts >> 2) & 1}")
    print(f"  UnsupReq: {(pcie_dev_sts >> 3) & 1}")
    print(f"  AuxPower: {(pcie_dev_sts >> 4) & 1}")
    print(f"  TransPend: {(pcie_dev_sts >> 5) & 1}")

    # Link Control 2 — bit 4 = "Selectable De-emphasis"
    # Link Status — bit 11 = "Link Training"
    pcie_lnk_ctl = gpu.cfg_read(0x78 + 0x10, 2)
    pcie_lnk_sts = gpu.cfg_read(0x78 + 0x12, 2)
    print(f"\nPCIe LnkCtl: 0x{pcie_lnk_ctl:04x}")
    print(f"PCIe LnkSts: 0x{pcie_lnk_sts:04x}")
    cur_speed = pcie_lnk_sts & 0xf
    cur_width = (pcie_lnk_sts >> 4) & 0x3f
    print(f"  Speed: 0x{cur_speed:x}, Width: x{cur_width}")
    print(f"  Link Training: {(pcie_lnk_sts >> 11) & 1}")

    # Try to trigger link retrain via Link Control bit 5 ("Retrain Link")
    print("\n[EXPERIMENT] Setting Link Control 'Retrain Link' bit (bit 5)...")
    gpu.cfg_write(0x78 + 0x10, pcie_lnk_ctl | (1 << 5), 2)
    time.sleep(0.1)
    new_lnk_sts = gpu.cfg_read(0x78 + 0x12, 2)
    print(f"  LnkSts after: 0x{new_lnk_sts:04x}")
    boot = gpu.rd32(0)
    print(f"  PMC_BOOT_0 after retrain: 0x{boot:08x}")

    gpu.close()


if __name__ == "__main__":
    main()
