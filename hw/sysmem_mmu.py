#!/usr/bin/env python3
"""Pascal MMU with System Memory — bypasses VRAM entirely.

On eGPU over Thunderbolt, the FB controller isn't POSTed so VRAM
is inaccessible. This module places all page tables, instance blocks,
and compute buffers in system memory (host RAM) accessible via DMA.

Pascal supports DMA from system memory with aperture=2 (SYSMEM_NONCOH)
or aperture=1 (SYSMEM_COH). TinyGPU's alloc_sysmem() provides
physically contiguous buffers with known bus addresses.

This is the same approach tinygrad uses for Ampere+ eGPU — the GPU
DMAs everything from host memory, never touching VRAM.

Architecture:
    Host RAM                          GPU
    ┌──────────────┐                  ┌───────────┐
    │ Page Tables   │◄── DMA read ───│ MMU       │
    │ Instance Block│                 │           │
    │ Compute Buffer│◄── DMA r/w ───│ SM cores  │
    │ GPFIFO        │◄── DMA read ──│ PFIFO     │
    └──────────────┘                  └───────────┘

References:
    nouveau: nvkm/subdev/mmu/vmm/gp100.c
    tinygrad: runtime/ops_nv.py (NVDevice.__init__, HCQ pattern)
    NVIDIA open-gpu-doc: pascal/gp100-mmu-format.pdf
"""

import struct
from hw.mmu import make_pde, make_pte, make_instance_block


# Aperture constants for PDE/PTE entries (page tables)
APERTURE_VIDMEM = 0
APERTURE_SYSMEM_COH = 1
APERTURE_SYSMEM_NONCOH = 2

# CRITICAL: Instance block (NV_RAMIN) uses DIFFERENT aperture encoding!
# From NVIDIA open-gpu-kernel-modules: src/common/inc/swref/published/pascal/gp100/dev_ram.h
#   NV_RAMIN_PAGE_DIR_BASE_TARGET_VID_MEM            = 0x00
#   NV_RAMIN_PAGE_DIR_BASE_TARGET_SYS_MEM_COHERENT   = 0x02
#   NV_RAMIN_PAGE_DIR_BASE_TARGET_SYS_MEM_NONCOHERENT = 0x03
# This mismatch (PDE uses 2, RAMIN uses 3 for SYSMEM_NONCOH) is a known
# source of init failures — the Falcon can't DMA if the aperture is wrong.
RAMIN_TARGET_VIDMEM = 0
RAMIN_TARGET_SYSMEM_COH = 2
RAMIN_TARGET_SYSMEM_NONCOH = 3


class SysmemAllocator:
    """Allocates physically contiguous system memory via TinyGPU.

    Each allocation returns (virtual_ptr, bus_address) where bus_address
    is the physical address the GPU sees for DMA.
    """

    def __init__(self, gpu):
        self.gpu = gpu
        self._allocs = []
        self._total = 0

    def alloc(self, size: int, align: int = 0x1000) -> tuple:
        """Allocate system memory accessible by GPU.

        Returns:
            (memview, bus_addr) — memview for CPU access, bus_addr for GPU DMA
        """
        # Round up to alignment
        size = (size + align - 1) & ~(align - 1)

        mem = self.gpu.alloc_sysmem(size, contiguous=True)

        # TinyGPU returns a memoryview-like object with .bus_addr
        # The exact API depends on tinygrad version
        if hasattr(mem, 'bus_addr'):
            bus_addr = mem.bus_addr
        elif hasattr(mem, 'paddr'):
            bus_addr = mem.paddr
        elif isinstance(mem, tuple):
            mem, bus_addr = mem
        else:
            # Fallback: try to get address from the object
            bus_addr = getattr(mem, 'addr', 0)

        self._allocs.append((mem, bus_addr, size))
        self._total += size
        return mem, bus_addr

    @property
    def total_allocated(self) -> int:
        return self._total


class SysmemMMU:
    """Pascal MMU using system memory for all page tables and buffers.

    Bypasses VRAM entirely — works on eGPU where FB isn't POSTed.
    """

    def __init__(self, gpu):
        self.gpu = gpu
        self.alloc = SysmemAllocator(gpu)
        self._inst_addr = None
        self._pd_addr = None
        self._mappings = []  # (va, pa, size) tuples

    def setup(self, va_size_mb: int = 64) -> int:
        """Set up page tables in system memory.

        Creates a linear mapping so the GPU can access system memory
        buffers at known virtual addresses.

        Args:
            va_size_mb: Virtual address space size to map (default 64MB)

        Returns:
            Bus address of the instance block (for FIFO setup)
        """
        va_size = va_size_mb * 1024 * 1024

        print(f"[SysmemMMU] Setting up {va_size_mb}MB VA space in system memory")

        # Allocate page table pages in system memory
        # PDE3: top-level, 1 page (512 entries × 8 bytes)
        pde3_mem, pde3_addr = self.alloc.alloc(0x1000)

        # PDE2: 1 page
        pde2_mem, pde2_addr = self.alloc.alloc(0x1000)

        # For 64MB: need 32 × 2MB big-page PTEs → fits in 1 page
        num_2mb_pages = va_size // (2 * 1024 * 1024)
        pde1_mem, pde1_addr = self.alloc.alloc(0x1000)

        # Instance block: 4KB
        inst_mem, inst_addr = self.alloc.alloc(0x1000)

        print(f"  PDE3:     bus=0x{pde3_addr:012x}")
        print(f"  PDE2:     bus=0x{pde2_addr:012x}")
        print(f"  PDE1:     bus=0x{pde1_addr:012x} ({num_2mb_pages} entries)")
        print(f"  Instance: bus=0x{inst_addr:012x}")

        # Zero all pages
        for mem in (pde3_mem, pde2_mem, pde1_mem, inst_mem):
            if hasattr(mem, '__setitem__'):
                for i in range(len(mem)):
                    mem[i] = 0

        # PDE3[0] → PDE2 (in system memory)
        pde3_entry = make_pde(pde2_addr, target=APERTURE_SYSMEM_NONCOH, vol=True)
        struct.pack_into('<Q', pde3_mem, 0, pde3_entry)

        # PDE2[0] → PDE1 (in system memory)
        pde2_entry = make_pde(pde1_addr, target=APERTURE_SYSMEM_NONCOH, vol=True)
        struct.pack_into('<Q', pde2_mem, 0, pde2_entry)

        # PDE1: initially empty — filled by map_buffer()
        # (we don't identity-map anything yet)

        # Instance block: point to PDE3 in system memory
        # CRITICAL: use RAMIN_TARGET encoding (3), NOT PDE encoding (2)
        inst_data = make_instance_block(pde3_addr, target=RAMIN_TARGET_SYSMEM_NONCOH)
        if hasattr(inst_mem, '__setitem__'):
            for i, b in enumerate(inst_data):
                inst_mem[i] = b

        # Set VOL flag (bit 2) — required for system memory
        pd_lo = struct.unpack_from('<I', inst_data, 0x200)[0]
        pd_lo |= (1 << 2)  # NV_RAMIN_PAGE_DIR_BASE_VOL_TRUE
        struct.pack_into('<I', inst_mem, 0x200, pd_lo)

        self._inst_addr = inst_addr
        self._pd_addr = pde3_addr
        self._pde1_mem = pde1_mem
        self._pde1_addr = pde1_addr
        self._next_va = 0

        print(f"  Total sysmem: {self.alloc.total_allocated} bytes "
              f"({self.alloc.total_allocated // 1024}KB)")

        return inst_addr

    def map_buffer(self, size: int) -> tuple:
        """Allocate a system memory buffer and map it into GPU virtual address space.

        Returns:
            (cpu_mem, gpu_va, bus_addr) — CPU access, GPU virtual address, bus address
        """
        # Round up to 2MB for big-page alignment
        aligned_size = max(size, 2 * 1024 * 1024)
        aligned_size = (aligned_size + (2 * 1024 * 1024 - 1)) & ~(2 * 1024 * 1024 - 1)

        # Allocate system memory
        mem, bus_addr = self.alloc.alloc(aligned_size, align=2 * 1024 * 1024)

        # Map into GPU VA space via PDE1 entries
        va = self._next_va
        num_pages = aligned_size // (2 * 1024 * 1024)

        for i in range(num_pages):
            page_idx = (va // (2 * 1024 * 1024)) + i
            page_addr = bus_addr + i * (2 * 1024 * 1024)

            # Write PTE as 2MB big page pointing to system memory
            pte = make_pte(
                page_addr,
                valid=True,
                target=APERTURE_SYSMEM_NONCOH,
                vol=True,
                kind=0,  # pitch linear
            )
            struct.pack_into('<Q', self._pde1_mem, page_idx * 8, pte)

        self._next_va = va + aligned_size
        self._mappings.append((va, bus_addr, aligned_size))

        print(f"  Mapped {size} bytes: VA=0x{va:08x} → bus=0x{bus_addr:012x}")
        return mem, va, bus_addr

    def invalidate_tlb(self):
        """Invalidate GPU TLB after page table changes."""
        # NV_VIRTUAL_FUNCTION_PRIV_MMU_INVALIDATE
        # Write to 0x100CB8 (PRI_MMU_INVALIDATE)
        self.gpu.wr32(0x100CB8,
                      (1 << 0) |   # ALL_VA
                      (1 << 1) |   # ALL_PDB
                      (1 << 6) |   # HUBTLB_ONLY
                      (1 << 31))   # TRIGGER

    @property
    def instance_addr(self) -> int:
        return self._inst_addr
