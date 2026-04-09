#!/usr/bin/env python3
"""MMIO register access abstraction for Pascal GPU.

Wraps TinyGPU socket client with named register access,
engine enable/disable, and wait-for-condition helpers.
"""

import sys
import os
import time
import struct

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from transport.tinygpu_client import TinyGPUClient


class MMIO:
    """GPU MMIO register interface via TinyGPU."""

    def __init__(self, client: TinyGPUClient):
        self.client = client

    def rd32(self, offset: int) -> int:
        """Read a 32-bit MMIO register."""
        return self.client.mmio_read32(0, offset)

    def wr32(self, offset: int, value: int):
        """Write a 32-bit MMIO register."""
        self.client.mmio_write32(0, offset, value)

    def rd_block(self, offset: int, size: int) -> bytes:
        """Read a block of MMIO data."""
        return self.client.mmio_read(0, offset, size)

    def wr_block(self, offset: int, data: bytes):
        """Write a block of MMIO data."""
        self.client.mmio_write(0, offset, data)

    def mask(self, offset: int, clear: int, set_bits: int):
        """Read-modify-write: clear bits then set bits."""
        val = self.rd32(offset)
        val = (val & ~clear) | set_bits
        self.wr32(offset, val)

    def wait(self, offset: int, mask: int, expected: int, timeout_ms: int = 1000) -> bool:
        """Poll a register until (reg & mask) == expected or timeout."""
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            val = self.rd32(offset)
            if (val & mask) == expected:
                return True
            time.sleep(0.001)
        return False


# --- PMC Register Definitions ---
PMC_BOOT_0       = 0x000000
PMC_BOOT_42      = 0x000108
PMC_ENABLE        = 0x000200
PMC_INTR_0        = 0x000100
PMC_INTR_EN_0     = 0x000140

# PMC_ENABLE bit positions for Pascal
PMC_ENABLE_PFIFO  = (1 << 8)
PMC_ENABLE_PGRAPH = (1 << 12)
PMC_ENABLE_PMU    = (1 << 13)
PMC_ENABLE_CE0    = (1 << 17)
PMC_ENABLE_CE1    = (1 << 22)
PMC_ENABLE_SEC2   = (1 << 29)

# PTIMER
PTIMER_TIME_0     = 0x009400
PTIMER_TIME_1     = 0x009410

# PFB / MMU
PFB_PRI_MMU_WPR2_ADDR_LO = 0x100CE0
PFB_PRI_MMU_WPR2_ADDR_HI = 0x100CE4


class GPUEngine:
    """Manage GPU engine enable/disable via PMC_ENABLE."""

    def __init__(self, mmio: MMIO):
        self.mmio = mmio

    def read_enable(self) -> int:
        return self.mmio.rd32(PMC_ENABLE)

    def enable(self, bits: int):
        """Enable engine(s) by setting bits in PMC_ENABLE."""
        current = self.read_enable()
        self.mmio.wr32(PMC_ENABLE, current | bits)

    def disable(self, bits: int):
        """Disable engine(s) by clearing bits in PMC_ENABLE."""
        current = self.read_enable()
        self.mmio.wr32(PMC_ENABLE, current & ~bits)

    def reset(self, bits: int):
        """Reset engine(s): disable, wait, re-enable."""
        self.disable(bits)
        time.sleep(0.01)
        self.enable(bits)
        time.sleep(0.01)

    def is_enabled(self, bit: int) -> bool:
        return bool(self.read_enable() & bit)

    def status_str(self) -> str:
        enable = self.read_enable()
        engines = [
            ("PFIFO",  PMC_ENABLE_PFIFO),
            ("PGRAPH", PMC_ENABLE_PGRAPH),
            ("PMU",    PMC_ENABLE_PMU),
            ("CE0",    PMC_ENABLE_CE0),
            ("CE1",    PMC_ENABLE_CE1),
            ("SEC2",   PMC_ENABLE_SEC2),
        ]
        parts = []
        for name, bit in engines:
            state = "ON" if enable & bit else "OFF"
            parts.append(f"{name}={state}")
        return f"PMC_ENABLE=0x{enable:08x} [{', '.join(parts)}]"
