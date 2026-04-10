#!/usr/bin/env python3
"""Pascal Falcon DMA from system memory (not VRAM).

Breakthrough idea: VRAM is dead because FBP stations aren't registered.
BUT Falcon DMA can also read from system memory via PHYS_SYS_COH/NCOH
apertures. TinyGPU's alloc_sysmem() gives us physical addresses in
sysmem that the GPU can access over PCIe.

If we:
  1. Allocate sysmem via TinyGPU (returns physical addresses)
  2. Write firmware to the sysmem region
  3. Configure Falcon DMA aperture for PHYS_SYS_COH/NCOH
  4. Trigger Falcon DMA from the sysmem physical address
  5. Falcon IMEM/DMEM gets loaded without ever touching VRAM

Then we bypass the circular FBP/VRAM/PMU dependency entirely.

This is how nouveau's early bringup paths work on some cards — they
place firmware in system memory and let the Falcon DMA it over PCIe.
"""

import os
import sys
import struct
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from transport.tinygrad_transport import PascalGPU

# Falcon DMA register offsets
FALCON_DMATRFBASE   = 0x110  # DMA transfer base (upper 32 bits of sysmem addr >> 8)
FALCON_DMATRFBASE1  = 0x128  # DMA transfer base high (for 64-bit addrs)
FALCON_DMATRFMOFFS  = 0x114  # Falcon memory offset
FALCON_DMATRFCMD    = 0x118  # DMA transfer command
FALCON_DMATRFFBOFFS = 0x11c  # FB offset (relative to base)
FALCON_DMACTL       = 0x10c
FALCON_MAILBOX0     = 0x040
FALCON_CPUCTL       = 0x100

# DMATRFCMD bits
DMATRFCMD_IDLE  = (1 << 1)
DMATRFCMD_WRITE = (1 << 5)  # 0 = GPU->Falcon (read), 1 = Falcon->GPU (write)
DMATRFCMD_IMEM  = (1 << 4)  # 0 = DMEM, 1 = IMEM (actually bit 4 varies)

# DMA aperture indices for FBIF (different base per engine)
DMAIDX_UCODE         = 0  # Virtual addressing (needs instance block)
DMAIDX_VIRT          = 1  # Virtual addressing
DMAIDX_PHYS_VID      = 2  # Physical VRAM
DMAIDX_PHYS_SYS_COH  = 3  # Physical system memory coherent
DMAIDX_PHYS_SYS_NCOH = 4  # Physical system memory non-coherent

# FBIF aperture config register (per engine base)
# FBIF is typically at falcon_base + 0x600 (default) or 0xe00 (PMU)
FBIF_TRANSCFG_BASE_DEFAULT = 0x600
FBIF_TRANSCFG_BASE_PMU     = 0xe00

# FBIF_TRANSCFG aperture values
TRANSCFG_LOCAL_FB    = 0x0
TRANSCFG_COHERENT_FB = 0x1  # Mapped VRAM
TRANSCFG_NONCOHERENT_FB = 0x2
TRANSCFG_TARGET_LOCAL_FB     = (0 << 0)
TRANSCFG_TARGET_COHERENT_SYSMEM = (1 << 0)
TRANSCFG_TARGET_NONCOHERENT_SYSMEM = (2 << 0)


def setup_fbif_aperture(gpu, engine_base: int, dma_idx: int, target: int, fbif_base: int = 0x600):
    """Configure FBIF DMA aperture for a Falcon engine.

    Args:
        gpu: PascalGPU instance
        engine_base: Falcon engine base (e.g. 0x409000 for FECS)
        dma_idx: which DMA aperture index to configure (0-7)
        target: TRANSCFG_TARGET_* value
        fbif_base: FBIF register base offset from engine (0x600 default, 0xe00 for PMU)
    """
    # Each aperture is 4 bytes apart in FBIF_TRANSCFG
    reg = engine_base + fbif_base + (dma_idx * 4)
    gpu.wr32(reg, target)
    print(f"    FBIF TRANSCFG[{dma_idx}] at 0x{reg:06x} = 0x{target:x}")


def dma_sysmem_to_falcon(gpu, engine_base: int, sysmem_paddr: int, falcon_offset: int,
                          size: int, to_imem: bool = False, dma_idx: int = DMAIDX_PHYS_SYS_NCOH):
    """Trigger Falcon DMA from system memory to its internal IMEM/DMEM.

    Args:
        gpu: PascalGPU
        engine_base: Falcon base
        sysmem_paddr: Physical address in system memory (from alloc_sysmem)
        falcon_offset: Offset in Falcon IMEM/DMEM
        size: Transfer size (must be multiple of 256)
        to_imem: True for IMEM, False for DMEM
        dma_idx: DMA aperture index (default PHYS_SYS_NCOH)
    """
    # DMATRFBASE = high bits of physical address (shifted right by 8)
    # DMATRFMOFFS = Falcon memory offset (target in IMEM/DMEM)
    # DMATRFFBOFFS = FB offset (source offset)
    # DMATRFCMD triggers the transfer

    gpu.wr32(engine_base + FALCON_DMATRFBASE, sysmem_paddr >> 8)

    for offset in range(0, size, 256):
        gpu.wr32(engine_base + FALCON_DMATRFMOFFS, falcon_offset + offset)
        gpu.wr32(engine_base + FALCON_DMATRFFBOFFS, offset)

        cmd = (dma_idx << 12)  # DMA aperture index
        cmd |= (6 << 8)         # Transfer size = 256 bytes (size code 6)
        if to_imem:
            cmd |= (1 << 4)     # IMEM target
        # (no WRITE bit = GPU to Falcon read)

        gpu.wr32(engine_base + FALCON_DMATRFCMD, cmd)

        # Wait for idle
        deadline = time.time() + 1.0
        while time.time() < deadline:
            status = gpu.rd32(engine_base + FALCON_DMATRFCMD)
            if status & DMATRFCMD_IDLE:
                break
            time.sleep(0.001)
        else:
            print(f"    DMA timeout at offset {offset}")
            return False

    return True


def main():
    print("=" * 60)
    print("  Pascal Falcon DMA from System Memory")
    print("=" * 60)

    gpu = PascalGPU()

    boot = gpu.rd32(0)
    if boot == 0xffffffff:
        print("\nGPU not responding. Power cycle the eGPU first.")
        return

    print(f"\nGPU: 0x{boot:08x}")

    # Clean PMC state
    gpu.wr32(0x000200, 0x40003120)
    time.sleep(0.1)
    pmc = gpu.rd32(0x000200)
    gpu.wr32(0x000200, pmc & ~(1 << 12))  # disable PGRAPH
    time.sleep(0.1)
    gpu.wr32(0x000200, pmc | (1 << 12))    # re-enable
    time.sleep(0.2)

    # Allocate system memory for firmware
    print("\n[1] Allocating system memory for firmware...")
    SIZE = 256 * 1024  # 256 KB
    try:
        memview, paddrs = gpu.alloc_sysmem(SIZE, contiguous=True)
        print(f"    Allocated {SIZE} bytes")
        print(f"    Physical addresses: {len(paddrs)} pages")
        if paddrs:
            print(f"    First page: 0x{paddrs[0]:016x}")
        contiguous = all(paddrs[i+1] - paddrs[i] == 0x1000 for i in range(len(paddrs)-1))
        print(f"    Contiguous: {contiguous}")
    except Exception as e:
        print(f"    Failed: {e}")
        return

    if not paddrs:
        print("    No physical addresses returned")
        return

    sysmem_base = paddrs[0]

    # Load test data into sysmem
    print("\n[2] Writing test data to sysmem...")
    test_data = b'\x00\x00\x02\xf8' * 64  # halt instructions
    test_data += b'\xef\xbe\xad\xde' * 16  # 0xdeadbeef marker
    for i in range(0, len(test_data), 4):
        val = struct.unpack_from('<I', test_data, i)[0]
        memview.view(fmt='I')[i // 4] = val

    # Verify sysmem write
    v0 = memview.view(fmt='I')[0]
    print(f"    sysmem[0] write test: 0x{v0:08x} {'OK' if v0 == 0xf8020000 else 'FAIL'}")

    # Now configure FECS Falcon for sysmem DMA
    FECS = 0x409000

    print("\n[3] Configuring FECS FBIF aperture for PHYS_SYS_NCOH...")
    # FECS FBIF is at FECS + 0x600 (default)
    setup_fbif_aperture(gpu, FECS, DMAIDX_PHYS_SYS_NCOH, TRANSCFG_TARGET_NONCOHERENT_SYSMEM, fbif_base=0x600)

    # Enable DMA
    print("\n[4] Enabling FECS DMA...")
    gpu.wr32(FECS + FALCON_DMACTL, 0)  # Clear require-ctx
    dmactl = gpu.rd32(FECS + FALCON_DMACTL)
    print(f"    DMACTL: 0x{dmactl:08x}")

    # Attempt DMA from sysmem to FECS IMEM
    print(f"\n[5] DMA from sysmem 0x{sysmem_base:012x} to FECS IMEM...")
    ok = dma_sysmem_to_falcon(gpu, FECS, sysmem_base, 0, 256, to_imem=True,
                               dma_idx=DMAIDX_PHYS_SYS_NCOH)
    print(f"    DMA result: {'OK' if ok else 'FAILED'}")

    # Check IMEM contents
    print("\n[6] Reading FECS IMEM after DMA...")
    gpu.wr32(FECS + 0x180, (0 << 2) | (1 << 25))  # read mode
    for i in range(4):
        v = gpu.rd32(FECS + 0x184)
        expected = struct.unpack_from('<I', test_data, i * 4)[0]
        print(f"    IMEM[{i*4}]: 0x{v:08x} (want 0x{expected:08x}) {'OK' if v == expected else 'MISMATCH'}")

    # Check DMA status
    dmacmd = gpu.rd32(FECS + FALCON_DMATRFCMD)
    print(f"\n    DMATRFCMD final: 0x{dmacmd:08x}")

    gpu.close()


if __name__ == "__main__":
    main()
