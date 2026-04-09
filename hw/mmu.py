#!/usr/bin/env python3
"""Pascal MMU (Memory Management Unit) — Page Table Setup.

Pascal uses MMU v2 with up to 5 levels of page tables:
  Level 0: PDE3 — covers bits [47:38] = 256GB per entry
  Level 1: PDE2 — covers bits [37:29] = 512MB per entry
  Level 2: PDE1 — covers bits [28:21] = 2MB per entry (or big page PTE)
  Level 3: PDE0 — dual PDE (64K/4K), bits [20:16]/[20:12]
  Level 4: PTE  — 4KB or 64KB pages

For our minimal setup, we use a flat 1-level mapping:
  - One PDE3 pointing to one PDE2
  - PDE2 with entries pointing to 2MB big-page PTEs
  - Maps first N MB of VRAM linearly

Page table entries are 8 bytes each.

PDE format (non-leaf):
  [0]     — 0 (not a PTE)
  [2:1]   — target aperture (0=VIDMEM, 1=SYSMEM_COH, 2=SYSMEM_NONCOH)
  [3]     — vol (volatile, set for SYSMEM)
  [31:12] — address[31:12] of next level page table
  [39:32] — address[39:32]

PTE format (leaf):
  [0]     — valid
  [1]     — unused
  [2:1]   — aperture (0=VIDMEM, 2=SYSMEM)
  [3]     — vol (volatile)
  [4]     — encryption (0)
  [9:5]   — kind (0=pitch, 6=generic)
  [31:12] — address[31:12]
  [39:32] — address[39:32]

Instance Block format (at VRAM-aligned 4KB boundary):
  Offset 0x000-0x1FF: RAMFC (512 bytes) — FIFO channel state
  Offset 0x200: Page Directory Base Config
    [1:0]   PAGE_DIR_BASE_TARGET (0=VIDMEM, 2=SYSMEM_COH)
    [2]     PAGE_DIR_BASE_VOL
    [10]    USE_VER2_PT_FORMAT = 1 (Pascal)
    [11]    BIG_PAGE_SIZE (0=128KB, 1=64KB)
    [31:12] PAGE_DIR_BASE_LO (pd_addr >> 12)
  Offset 0x204:
    [31:0]  PAGE_DIR_BASE_HI (pd_addr >> 32)

References:
  - NVIDIA open-gpu-doc: pascal/gp100-mmu-format.pdf
  - envytools: hw/memory/gf100-vm.rst
  - nouveau: nvkm/subdev/mmu/vmm/gp100.c
"""

import struct


class VRAMAllocator:
    """Simple bump allocator for VRAM regions."""

    def __init__(self, base: int, size: int):
        self.base = base
        self.size = size
        self.offset = 0

    def alloc(self, size: int, align: int = 0x1000) -> int:
        """Allocate VRAM. Returns physical address."""
        # Align up
        self.offset = (self.offset + align - 1) & ~(align - 1)
        if self.offset + size > self.size:
            raise RuntimeError(f"VRAM exhausted: need {size} at offset {self.offset}, have {self.size}")
        addr = self.base + self.offset
        self.offset += size
        return addr

    def used(self) -> int:
        return self.offset


def make_pde(address: int, target: int = 0, vol: bool = False) -> int:
    """Create a page directory entry (non-leaf).

    Args:
        address: Physical address of next-level page table (must be 4KB aligned)
        target: 0=VIDMEM, 1=SYSMEM_COH, 2=SYSMEM_NONCOH
        vol: Volatile flag (set for system memory)
    """
    entry = 0
    entry |= (target & 0x3) << 1
    entry |= (int(vol) << 3)
    entry |= (address & 0xfffff000)  # bits [31:12]
    entry |= ((address >> 32) & 0xff) << 32  # bits [39:32]
    return entry


def make_pte(address: int, valid: bool = True, target: int = 0, vol: bool = False, kind: int = 0) -> int:
    """Create a page table entry (leaf).

    Args:
        address: Physical address of the page
        valid: Entry is valid
        target: 0=VIDMEM, 2=SYSMEM
        vol: Volatile
        kind: Memory kind (0=pitch, 6=generic)
    """
    entry = 0
    entry |= int(valid)
    entry |= (target & 0x3) << 1
    entry |= (int(vol) << 3)
    entry |= (kind & 0x1f) << 5
    entry |= (address & 0xfffff000)
    entry |= ((address >> 32) & 0xff) << 32
    return entry


def make_instance_block(pd_addr: int, target: int = 0) -> bytes:
    """Create an instance block with page directory base config.

    Args:
        pd_addr: Physical address of the top-level page directory
        target: 0=VIDMEM, 2=SYSMEM_COH

    Returns:
        4096 bytes — the full instance block
    """
    inst = bytearray(4096)

    # Page Directory Base Config at offset 0x200 (word 128)
    pd_lo = (pd_addr & 0xfffff000)  # address bits [31:12]
    pd_lo |= (target & 0x3)         # TARGET
    pd_lo |= (1 << 10)              # USE_VER2_PT_FORMAT
    pd_lo |= (1 << 11)              # BIG_PAGE_SIZE = 64KB

    pd_hi = (pd_addr >> 32) & 0xffffffff

    struct.pack_into('<I', inst, 0x200, pd_lo)
    struct.pack_into('<I', inst, 0x204, pd_hi)

    return bytes(inst)


class PascalMMU:
    """Minimal Pascal MMU setup for Falcon DMA.

    Creates a simple linear mapping of VRAM so the Falcon
    can DMA firmware from physical VRAM addresses.
    """

    def __init__(self, gpu, vram_alloc: VRAMAllocator):
        self.gpu = gpu
        self.vram = vram_alloc

    def write_vram_u64(self, addr: int, value: int):
        """Write a 64-bit value to VRAM via BAR1 or PRAMIN."""
        # Use BAR0 PRAMIN window (0x700000) for small VRAM writes
        # PRAMIN window is controlled by PBUS_BAR0_WINDOW (0x1700)
        # Window base = register value << 16
        window_base = (addr >> 16) << 16
        window_offset = addr - window_base

        # Set PRAMIN window
        self.gpu.wr32(0x001700, window_base >> 16)

        # Write via PRAMIN (BAR0 offset 0x700000 + window_offset)
        pramin_base = 0x700000
        lo = value & 0xffffffff
        hi = (value >> 32) & 0xffffffff
        self.gpu.wr32(pramin_base + window_offset, lo)
        self.gpu.wr32(pramin_base + window_offset + 4, hi)

    def write_vram_u32(self, addr: int, value: int):
        """Write a 32-bit value to VRAM via PRAMIN."""
        window_base = (addr >> 16) << 16
        window_offset = addr - window_base
        self.gpu.wr32(0x001700, window_base >> 16)
        self.gpu.wr32(0x700000 + window_offset, value)

    def write_vram_block(self, addr: int, data: bytes):
        """Write a block of data to VRAM via PRAMIN."""
        for i in range(0, len(data), 4):
            if i + 4 <= len(data):
                val = struct.unpack_from('<I', data, i)[0]
            else:
                val = int.from_bytes(data[i:].ljust(4, b'\x00'), 'little')
            self.write_vram_u32(addr + i, val)

    def read_vram_u32(self, addr: int) -> int:
        """Read a 32-bit value from VRAM via PRAMIN."""
        window_base = (addr >> 16) << 16
        window_offset = addr - window_base
        self.gpu.wr32(0x001700, window_base >> 16)
        return self.gpu.rd32(0x700000 + window_offset)

    def setup_linear_mapping(self, vram_size_mb: int = 64) -> int:
        """Set up a minimal linear VRAM mapping for Falcon DMA.

        Creates page tables that identity-map the first N MB of VRAM.
        Returns the physical address of the instance block.
        """
        vram_size = vram_size_mb * 1024 * 1024
        print(f"  Setting up {vram_size_mb}MB linear VRAM mapping...")

        # Allocate page table structures in VRAM
        # PDE3: 1 page (512 entries × 8 bytes = 4KB)
        pde3_addr = self.vram.alloc(0x1000, align=0x1000)

        # PDE2: 1 page
        pde2_addr = self.vram.alloc(0x1000, align=0x1000)

        # PDE1: enough for vram_size / 2MB entries
        num_pde1_entries = vram_size // (2 * 1024 * 1024)
        pde1_size = max(num_pde1_entries * 8, 0x1000)
        pde1_addr = self.vram.alloc(pde1_size, align=0x1000)

        # Instance block
        inst_addr = self.vram.alloc(0x1000, align=0x1000)

        print(f"    PDE3:     0x{pde3_addr:08x}")
        print(f"    PDE2:     0x{pde2_addr:08x}")
        print(f"    PDE1:     0x{pde1_addr:08x} ({num_pde1_entries} entries)")
        print(f"    Instance: 0x{inst_addr:08x}")

        # Fill PDE3[0] -> PDE2
        self.write_vram_u64(pde3_addr, make_pde(pde2_addr, target=0))

        # Fill PDE2[0] -> PDE1
        self.write_vram_u64(pde2_addr, make_pde(pde1_addr, target=0))

        # Fill PDE1 entries: each covers 2MB, using 2MB big-page PTEs
        for i in range(num_pde1_entries):
            phys = i * (2 * 1024 * 1024)
            # 2MB big page PTE: valid, VIDMEM, kind=0
            pte = make_pte(phys, valid=True, target=0, kind=0)
            self.write_vram_u64(pde1_addr + i * 8, pte)

        # Write instance block
        inst_data = make_instance_block(pde3_addr, target=0)
        self.write_vram_block(inst_addr, inst_data)

        # Verify a readback
        verify = self.read_vram_u32(inst_addr + 0x200)
        print(f"    Instance verify: 0x{verify:08x}")

        print(f"    VRAM used: {self.vram.used()} bytes ({self.vram.used() // 1024}KB)")
        return inst_addr
