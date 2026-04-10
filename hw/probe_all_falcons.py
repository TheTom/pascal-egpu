#!/usr/bin/env python3
"""Probe ALL Falcon engines on the Pascal GPU.

Each Falcon has a base address with consistent register layout.
Find which engines are:
  - Reachable (CPUCTL readable, not 0xffffffff)
  - In LS mode (SCTL bit 0 = 0) — can potentially run unsigned
  - In HS mode (SCTL bit 0 = 1) — locked
  - Have writable IMEM (test write+read)
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from transport.tinygrad_transport import PascalGPU


# Pascal GP10x Falcon engine bases
FALCONS = {
    "PMU":     0x10a000,  # Power management
    "MSVLD":   0x084000,  # Video decode
    "MSPDEC":  0x085000,  # Video decode (post-decode)
    "MSPPP":   0x086000,  # Video post-proc
    "MSENC":   0x1c8000,  # Video encode
    "SEC2":    0x087000,  # Security engine
    "NVDEC":   0x084000,  # Newer name for MSVLD location
    "NVENC0":  0x1c8000,
    "NVENC1":  0x1d0000,
    "FECS":    0x409000,  # Front-end CS
    "GPCCS":   0x41a000,  # GPC CS
    "MINION":  0x0a8000,  # NVLink minion (none on Pascal consumer)
    "DPU":     0x010000,  # Display Processing Unit
}


def probe_falcon(gpu, name, base):
    cpuctl_addr = base + 0x100
    sctl_addr   = base + 0x240
    hwcfg_addr  = base + 0x108
    hwcfg2_addr = base + 0x16c

    cpuctl = gpu.rd32(cpuctl_addr)
    if cpuctl == 0xffffffff:
        return None  # not present
    if (cpuctl & 0xfff00000) == 0xbad00000:
        return f"PRIV REJECTED 0x{cpuctl:08x}"
    if (cpuctl & 0xfff00000) == 0xbadf0000:
        return f"NOT INIT 0x{cpuctl:08x}"

    sctl  = gpu.rd32(sctl_addr)
    hwcfg = gpu.rd32(hwcfg_addr)
    imem_size = (hwcfg & 0x1ff) * 256
    dmem_size = ((hwcfg >> 9) & 0x1ff) * 256
    hsmode = sctl & 1
    ucode_lvl = (sctl >> 4) & 0xf

    # Try IMEM write at offset 0
    try:
        gpu.wr32(base + 0x180, (1 << 24))  # IMEMC: word 0 + AINCW
        gpu.wr32(base + 0x188, 0)            # tag
        gpu.wr32(base + 0x184, 0xCAFEBABE)
        gpu.wr32(base + 0x180, (1 << 25))    # AINCR
        v = gpu.rd32(base + 0x184)
    except Exception:
        v = None

    if v == 0xCAFEBABE:
        imem_state = "WRITABLE ✓"
    elif v == 0xdead5ec1:
        imem_state = "blocked (HS)"
    elif v is None:
        imem_state = "exception"
    else:
        imem_state = f"got 0x{v:08x}"

    return (
        f"CPUCTL=0x{cpuctl:08x}  "
        f"SCTL=0x{sctl:08x} ({'HS' if hsmode else 'LS'}, lvl={ucode_lvl})  "
        f"IMEM={imem_size}B DMEM={dmem_size}B  "
        f"IMEM[0]: {imem_state}"
    )


def main():
    print("=" * 80)
    print("  Probe All Falcons")
    print("=" * 80)

    gpu = PascalGPU()
    if gpu.rd32(0) == 0xffffffff:
        print("GPU dead"); return

    cmd = gpu.cfg_read(0x04, 2)
    if not (cmd & 0x06):
        gpu.cfg_write(0x04, cmd | 0x06, 2)

    gpu.wr32(0x000200, 0xffffffff)
    time.sleep(0.05)
    pmc = gpu.rd32(0x000200)
    print(f"PMC_ENABLE: 0x{pmc:08x}\n")

    print(f"{'Engine':<10} {'Base':<10} Status")
    print("-" * 90)
    for name, base in FALCONS.items():
        result = probe_falcon(gpu, name, base)
        if result is None:
            status = "(not present)"
        else:
            status = result
        print(f"{name:<10} 0x{base:06x}   {status}")

    gpu.close()


if __name__ == "__main__":
    main()
