#!/usr/bin/env python3
"""Transport layer using tinygrad's APLRemotePCIDevice for reliable BAR access.

Our custom TinyGPU socket client had protocol issues with MMIO data reads.
tinygrad's APLRemotePCIDevice handles the BAR mapping correctly via shared
memory regions, which is much faster and more reliable.

This module provides a thin wrapper that gives us the MMIO interface we need
for the Pascal init sequence.
"""

import sys
import os

# Ensure tinygrad is importable
TINYGRAD_PATH = os.environ.get("TINYGRAD_PATH", "/Users/tom/dev/tinygrad-pascal")
if TINYGRAD_PATH not in sys.path:
    sys.path.insert(0, TINYGRAD_PATH)

from tinygrad.runtime.support.system import APLRemotePCIDevice


class PascalGPU:
    """Transport layer for Pascal GPU via TinyGPU + tinygrad."""

    def __init__(self):
        self.dev = APLRemotePCIDevice('NV', 'usb4')
        self._bar0 = self.dev.map_bar(0, fmt='I')  # 32-bit word access
        self._bar1 = None  # VRAM — map on demand
        print(f"PascalGPU: connected, BAR0={self._bar0.nbytes // (1024*1024)}MB")

    @property
    def bar0(self):
        return self._bar0

    @property
    def bar1(self):
        if self._bar1 is None:
            self._bar1 = self.dev.map_bar(1, fmt='B')  # byte access for VRAM
        return self._bar1

    def rd32(self, offset: int) -> int:
        """Read 32-bit MMIO register at BAR0 + offset."""
        return self._bar0[offset // 4]

    def wr32(self, offset: int, value: int):
        """Write 32-bit MMIO register at BAR0 + offset."""
        self._bar0[offset // 4] = value

    def rd_block(self, offset: int, count: int) -> list:
        """Read multiple 32-bit registers starting at offset."""
        start = offset // 4
        return self._bar0[start:start + count]

    def wr_block(self, offset: int, values: list):
        """Write multiple 32-bit registers starting at offset."""
        start = offset // 4
        self._bar0[start:start + len(values)] = values

    def mask(self, offset: int, clear: int, set_bits: int):
        """Read-modify-write: clear bits then set bits."""
        val = self.rd32(offset)
        self.wr32(offset, (val & ~clear) | set_bits)

    def close(self):
        """Clean up."""
        pass  # APLRemotePCIDevice handles cleanup

    # Convenience: PCI config space
    def cfg_read(self, offset: int, size: int) -> int:
        return self.dev.read_config(offset, size)

    def cfg_write(self, offset: int, value: int, size: int):
        self.dev.write_config(offset, value, size)

    # System memory allocation (for DMA)
    def alloc_sysmem(self, size: int, contiguous: bool = False):
        """Allocate system memory accessible by GPU for DMA."""
        return self.dev.alloc_sysmem(size, contiguous=contiguous)
