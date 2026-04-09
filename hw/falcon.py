#!/usr/bin/env python3
"""Falcon microcontroller interface for Pascal GPUs.

Falcon is NVIDIA's embedded microcontroller used for:
  - PMU (Power Management Unit) at 0x10a000
  - FECS (Front End Context Switch) at 0x409000
  - GPCCS (GPC Context Switch) at 0x41a000
  - SEC2 (Security Engine 2) at 0x840000

This module handles:
  - Reading Falcon status registers
  - Loading firmware into Falcon IMEM/DMEM
  - Starting/stopping Falcon execution
  - DMA transfers to/from Falcon
"""

import struct
import time
from hw.mmio import MMIO


# Falcon register offsets (relative to engine base)
FALCON_IRQSSET      = 0x000
FALCON_IRQSCLR      = 0x004
FALCON_IRQSTAT       = 0x008
FALCON_IRQMSET      = 0x010
FALCON_IRQMCLR      = 0x014
FALCON_IRQMASK       = 0x018
FALCON_IRQDEST       = 0x01c
FALCON_MAILBOX0      = 0x040
FALCON_MAILBOX1      = 0x044
FALCON_IDLESTATE     = 0x04c
FALCON_OS            = 0x050
FALCON_ENGCTL        = 0x060
FALCON_CPUCTL        = 0x100
FALCON_BOOTVEC       = 0x104
FALCON_HWCFG         = 0x108
FALCON_DMACTL        = 0x10c
FALCON_DMATRFBASE    = 0x110
FALCON_DMATRFMOFFS   = 0x114
FALCON_DMATRFCMD     = 0x118
FALCON_DMATRFFBOFFS  = 0x11c
FALCON_IMEMC         = 0x180
FALCON_IMEMD         = 0x184
FALCON_IMEMT         = 0x188
FALCON_DMEMC         = 0x1c0
FALCON_DMEMD         = 0x1c4

# CPUCTL bits
CPUCTL_STARTCPU = (1 << 1)
CPUCTL_HALTED   = (1 << 4)
CPUCTL_STOPPED  = (1 << 5)

# DMA transfer command bits
DMATRFCMD_IDLE     = (1 << 1)
DMATRFCMD_IMEM     = (0 << 4)
DMATRFCMD_DMEM     = 0  # bit 4 = 0 for DMEM
DMATRFCMD_WRITE    = (1 << 5)  # Write to falcon memory
DMATRFCMD_SIZE_256 = (6 << 8)  # 256 bytes per transfer


class Falcon:
    """Interface to a single Falcon microcontroller engine."""

    def __init__(self, mmio: MMIO, base: int, name: str):
        self.mmio = mmio
        self.base = base
        self.name = name

    def rd(self, offset: int) -> int:
        """Read a Falcon register."""
        return self.mmio.rd32(self.base + offset)

    def wr(self, offset: int, value: int):
        """Write a Falcon register."""
        self.mmio.wr32(self.base + offset, value)

    # --- Status ---

    def is_halted(self) -> bool:
        return bool(self.rd(FALCON_CPUCTL) & CPUCTL_HALTED)

    def is_stopped(self) -> bool:
        return bool(self.rd(FALCON_CPUCTL) & CPUCTL_STOPPED)

    def hwcfg(self) -> dict:
        """Read hardware configuration (IMEM/DMEM sizes)."""
        val = self.rd(FALCON_HWCFG)
        return {
            "imem_size": ((val >> 0) & 0x1ff) * 256,
            "dmem_size": ((val >> 9) & 0x1ff) * 256,
        }

    def mailbox0(self) -> int:
        return self.rd(FALCON_MAILBOX0)

    def mailbox1(self) -> int:
        return self.rd(FALCON_MAILBOX1)

    def status(self) -> dict:
        """Read full status."""
        cpuctl = self.rd(FALCON_CPUCTL)
        hw = self.hwcfg()
        return {
            "name": self.name,
            "base": self.base,
            "halted": bool(cpuctl & CPUCTL_HALTED),
            "stopped": bool(cpuctl & CPUCTL_STOPPED),
            "cpuctl": cpuctl,
            "mailbox0": self.rd(FALCON_MAILBOX0),
            "mailbox1": self.rd(FALCON_MAILBOX1),
            "os": self.rd(FALCON_OS),
            "idlestate": self.rd(FALCON_IDLESTATE),
            **hw,
        }

    def print_status(self):
        s = self.status()
        state = "HALTED" if s["halted"] else ("STOPPED" if s["stopped"] else "RUNNING")
        print(f"  {s['name']} (0x{s['base']:06x}): {state}")
        print(f"    CPUCTL:   0x{s['cpuctl']:08x}")
        print(f"    MAILBOX0: 0x{s['mailbox0']:08x}")
        print(f"    MAILBOX1: 0x{s['mailbox1']:08x}")
        print(f"    OS:       0x{s['os']:08x}")
        print(f"    IMEM:     {s['imem_size']} bytes ({s['imem_size']//1024}KB)")
        print(f"    DMEM:     {s['dmem_size']} bytes ({s['dmem_size']//1024}KB)")

    # --- Control ---

    def reset(self):
        """Reset the Falcon CPU."""
        # Clear all interrupts
        self.wr(FALCON_IRQMCLR, 0xffffffff)
        self.wr(FALCON_IRQSCLR, 0xffffffff)
        # Set CPUCTL to trigger reset
        self.wr(FALCON_CPUCTL, 0x00)
        time.sleep(0.01)

    def start(self, boot_vector: int = 0):
        """Start Falcon execution at the given boot vector."""
        self.wr(FALCON_BOOTVEC, boot_vector)
        self.wr(FALCON_CPUCTL, CPUCTL_STARTCPU)

    def wait_halted(self, timeout_ms: int = 2000) -> bool:
        """Wait for Falcon to halt (indicates completion)."""
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            if self.is_halted():
                return True
            time.sleep(0.005)
        return False

    # --- IMEM/DMEM Direct Access ---

    def load_imem(self, data: bytes, offset: int = 0):
        """Load data into Falcon IMEM via port access.

        IMEM is accessed via IMEMC (control) and IMEMD (data) registers.
        IMEMC[7:2] = starting block (256-byte aligned)
        IMEMC[24] = auto-increment
        IMEMC[25] = secure mode (0 = non-secure)
        """
        # Set IMEM access: auto-increment, starting at offset
        block = offset >> 8
        self.wr(FALCON_IMEMC, (block << 2) | (1 << 24))  # auto-increment

        # Write data 4 bytes at a time
        for i in range(0, len(data), 4):
            word = struct.unpack_from('<I', data, i)[0] if i + 4 <= len(data) else \
                   int.from_bytes(data[i:], 'little')
            self.wr(FALCON_IMEMD, word)

        # Set tag for each 256-byte block
        for i in range(0, len(data), 256):
            tag_block = (offset + i) >> 8
            self.wr(FALCON_IMEMC, (tag_block << 2))
            self.wr(FALCON_IMEMT, tag_block)

    def load_dmem(self, data: bytes, offset: int = 0):
        """Load data into Falcon DMEM via port access.

        DMEMC[7:2] = starting block (4-byte aligned offset >> 2)
        DMEMC[24] = auto-increment
        """
        self.wr(FALCON_DMEMC, ((offset >> 2) << 2) | (1 << 24))

        for i in range(0, len(data), 4):
            if i + 4 <= len(data):
                word = struct.unpack_from('<I', data, i)[0]
            else:
                word = int.from_bytes(data[i:].ljust(4, b'\x00'), 'little')
            self.wr(FALCON_DMEMD, word)

    def read_dmem(self, offset: int, size: int) -> bytes:
        """Read data from Falcon DMEM."""
        self.wr(FALCON_DMEMC, ((offset >> 2) << 2) | (1 << 25))  # auto-increment, read

        result = bytearray()
        for i in range(0, size, 4):
            word = self.rd(FALCON_DMEMD)
            result.extend(struct.pack('<I', word))
        return bytes(result[:size])

    # --- DMA Transfers ---

    def dma_wait_idle(self, timeout_ms: int = 500) -> bool:
        """Wait for DMA engine to be idle."""
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            if self.rd(FALCON_DMATRFCMD) & DMATRFCMD_IDLE:
                return True
            time.sleep(0.001)
        return False

    def dma_xfer(self, fb_offset: int, falcon_offset: int, size: int, to_imem: bool = False, write: bool = True):
        """DMA transfer between framebuffer and Falcon IMEM/DMEM.

        Args:
            fb_offset: Offset in framebuffer (VRAM)
            falcon_offset: Offset in Falcon IMEM or DMEM
            size: Transfer size (must be multiple of 256)
            to_imem: True for IMEM, False for DMEM
            write: True to write to Falcon, False to read from Falcon
        """
        # Set DMA base (upper bits of FB address)
        self.wr(FALCON_DMATRFBASE, fb_offset >> 8)

        for off in range(0, size, 256):
            self.wr(FALCON_DMATRFMOFFS, falcon_offset + off)
            self.wr(FALCON_DMATRFFBOFFS, off)

            cmd = DMATRFCMD_SIZE_256
            if to_imem:
                cmd |= DMATRFCMD_IMEM
            if write:
                cmd |= DMATRFCMD_WRITE

            self.wr(FALCON_DMATRFCMD, cmd)

            if not self.dma_wait_idle():
                raise RuntimeError(f"{self.name}: DMA transfer timeout at offset {off}")


# Pre-defined Falcon instances for Pascal GP106
FALCON_BASES = {
    "PMU":   0x10a000,
    "FECS":  0x409000,
    "GPCCS": 0x41a000,
    "SEC2":  0x840000,
}


def create_falcons(mmio: MMIO) -> dict[str, Falcon]:
    """Create Falcon instances for all engines."""
    return {name: Falcon(mmio, base, name) for name, base in FALCON_BASES.items()}
