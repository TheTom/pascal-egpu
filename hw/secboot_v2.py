#!/usr/bin/env python3
"""Pascal Secure Boot v2 — with MMU + VRAM firmware placement.

Full ACR boot sequence:
1. Allocate VRAM for page tables, instance block, and firmware
2. Set up linear MMU mapping so Falcon can DMA from VRAM
3. Place FECS/GPCCS firmware in VRAM
4. Build ACR descriptor (tells ACR where firmware is)
5. Load ACR bootloader into PMU Falcon IMEM
6. Configure Falcon with instance block for DMA
7. Start Falcon — ACR loads and verifies FECS/GPCCS
8. Verify success

Based on nouveau's secboot/acr_r352.c + secboot/gm200.c
"""

import os
import struct
import time
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from transport.tinygrad_transport import PascalGPU
from hw.mmu import PascalMMU, VRAMAllocator
from hw.secboot import FalconEngine, load_firmware, FALCON_MAILBOX0, FALCON_MAILBOX1, FALCON_CPUCTL, FALCON_DMACTL

# PMC registers
PMC_ENABLE = 0x000200
PMC_ENABLE_PMU    = (1 << 13)
PMC_ENABLE_SEC2   = (1 << 29)
PMC_ENABLE_PGRAPH = (1 << 12)
PMC_ENABLE_PFIFO  = (1 << 8)
PMC_ENABLE_CE0    = (1 << 17)

# Falcon DMA registers
FALCON_DMATRFBASE  = 0x110
FALCON_DMATRFMOFFS = 0x114
FALCON_DMATRFCMD   = 0x118
FALCON_DMATRFFBOFFS= 0x11c

# Falcon instance block binding
# On Pascal, Falcon's DMA context is set via engine-specific registers
# PMU: NV_PPWR_FALCON_INST(0) at PMU_BASE + 0x480
FALCON_INST_BLOCK   = 0x480  # Instance block binding register
FALCON_INST_BLOCK_LO = 0x480
FALCON_INST_BLOCK_HI = 0x484


def place_firmware_in_vram(mmu: PascalMMU, vram: VRAMAllocator) -> dict:
    """Place all required firmware blobs in VRAM and return their addresses."""
    print(f"\n--- Placing Firmware in VRAM ---")

    fw_addrs = {}

    # Load and place each firmware blob
    for fw_name in [
        "acr/bl.bin",
        "acr/ucode_load.bin",
        "gr/fecs_bl.bin",
        "gr/fecs_inst.bin",
        "gr/fecs_data.bin",
        "gr/fecs_sig.bin",
        "gr/gpccs_bl.bin",
        "gr/gpccs_inst.bin",
        "gr/gpccs_data.bin",
        "gr/gpccs_sig.bin",
        "gr/sw_ctx.bin",
        "gr/sw_bundle_init.bin",
        "gr/sw_method_init.bin",
        "gr/sw_nonctx.bin",
        "sec2/image.bin",
        "sec2/desc.bin",
        "sec2/sig.bin",
    ]:
        try:
            data = load_firmware(fw_name)
            # Align to 256 bytes for Falcon DMA
            addr = vram.alloc(len(data), align=256)
            mmu.write_vram_block(addr, data)
            fw_addrs[fw_name] = {"addr": addr, "size": len(data)}
            print(f"    {fw_name}: 0x{addr:08x} ({len(data)} bytes)")
        except FileNotFoundError:
            print(f"    {fw_name}: MISSING (skipped)")

    return fw_addrs


def bind_instance_block(falcon: FalconEngine, inst_addr: int):
    """Bind an instance block to a Falcon engine for DMA access.

    The instance block provides the Falcon's DMA engine with
    a page table root, enabling virtual-to-physical translation
    for DMA transfers.

    On Pascal, this is done via the FALCON_INST register:
      Bits [31:12] = instance block address >> 12
      Bit [0] = bind trigger
    """
    print(f"  Binding instance block 0x{inst_addr:08x} to {falcon.name}...")

    # Write instance block address (shifted right by 12)
    # The exact register varies by engine, but for PMU it's at base + 0x480
    val = (inst_addr >> 12) | 0x1  # address + bind flag
    falcon.wr(FALCON_INST_BLOCK_LO, val)
    falcon.wr(FALCON_INST_BLOCK_HI, 0)  # upper bits (usually 0 for <4GB)

    time.sleep(0.05)
    print(f"  Instance block bound")


def secboot_v2(gpu: PascalGPU) -> bool:
    """Full secure boot sequence with VRAM firmware placement."""

    print("=" * 60)
    print("  Pascal Secure Boot v2 — Full Sequence")
    print("=" * 60)

    # Step 1: Enable required engines
    print(f"\n[1/7] Enabling engines...")
    pmc = gpu.rd32(PMC_ENABLE)
    needed = PMC_ENABLE_PMU | PMC_ENABLE_SEC2 | PMC_ENABLE_PFIFO | PMC_ENABLE_PGRAPH | PMC_ENABLE_CE0
    if (pmc & needed) != needed:
        gpu.wr32(PMC_ENABLE, pmc | needed)
        time.sleep(0.1)
        pmc = gpu.rd32(PMC_ENABLE)
    print(f"  PMC_ENABLE = 0x{pmc:08x}")

    # Step 2: Set up VRAM allocator
    # Start allocations at 1MB to avoid conflict with display/BIOS regions
    print(f"\n[2/7] Setting up VRAM allocator...")
    vram = VRAMAllocator(base=0x100000, size=64 * 1024 * 1024)
    mmu = PascalMMU(gpu, vram)

    # Step 3: Create MMU page tables + instance block
    print(f"\n[3/7] Creating MMU page tables...")
    inst_addr = mmu.setup_linear_mapping(vram_size_mb=64)

    # Step 4: Place firmware in VRAM
    print(f"\n[4/7] Placing firmware in VRAM...")
    fw_addrs = place_firmware_in_vram(mmu, vram)

    # Step 5: Configure PMU Falcon
    print(f"\n[5/7] Configuring PMU Falcon...")
    pmu = FalconEngine(gpu, 0x10a000, "PMU")

    # Check PMU state
    hw = pmu.hwcfg()
    print(f"  PMU IMEM={hw['imem_size']}B, DMEM={hw['dmem_size']}B")

    # Reset PMU
    pmu.reset()

    # Bind instance block for DMA
    bind_instance_block(pmu, inst_addr)

    # Set sentinel in mailbox
    pmu.wr(FALCON_MAILBOX0, 0xdeada5a5)
    pmu.wr(FALCON_MAILBOX1, 0x00000000)

    # Step 6: Load ACR into PMU
    print(f"\n[6/7] Loading ACR firmware...")

    acr_bl = load_firmware("acr/bl.bin")
    acr_ucode = load_firmware("acr/ucode_load.bin")

    # Load bootloader into IMEM
    pmu.load_imem(acr_bl, offset=0)

    # Load ucode into DMEM
    # The ucode expects certain data at specific DMEM offsets:
    # - The ACR descriptor (addresses of FECS/GPCCS firmware in VRAM)
    # For now, load the raw ucode and see what happens
    pmu.load_dmem(acr_ucode, offset=0)

    # Step 7: Start Falcon
    print(f"\n[7/7] Starting PMU Falcon...")
    pmu.start(boot_vector=0)

    # Wait and check result
    print(f"  Waiting for ACR completion...", flush=True)
    deadline = time.time() + 15.0
    last_mailbox = 0xdeada5a5

    while time.time() < deadline:
        cpuctl = pmu.rd(FALCON_CPUCTL)
        mb0 = pmu.rd(FALCON_MAILBOX0)
        mb1 = pmu.rd(FALCON_MAILBOX1)

        if mb0 != last_mailbox:
            print(f"  MAILBOX0 changed: 0x{mb0:08x} (CPUCTL=0x{cpuctl:08x})", flush=True)
            last_mailbox = mb0

        if cpuctl & 0x10:  # HALTED
            print(f"  PMU halted! MAILBOX0=0x{mb0:08x} MAILBOX1=0x{mb1:08x}")
            break

        if cpuctl & 0x20 and not (cpuctl & 0x02):  # STOPPED but not running
            # Check if it stopped due to error
            print(f"  PMU stopped. CPUCTL=0x{cpuctl:08x} MB0=0x{mb0:08x} MB1=0x{mb1:08x}")
            break

        time.sleep(0.1)
    else:
        print(f"  TIMEOUT after 15 seconds")
        cpuctl = pmu.rd(FALCON_CPUCTL)
        mb0 = pmu.rd(FALCON_MAILBOX0)
        print(f"  Final: CPUCTL=0x{cpuctl:08x} MAILBOX0=0x{mb0:08x}")

    # Check FECS status
    print(f"\n--- Post-Boot Status ---")
    fecs = FalconEngine(gpu, 0x409000, "FECS")
    try:
        fecs_cpuctl = fecs.rd(FALCON_CPUCTL)
        fecs_mb0 = fecs.rd(FALCON_MAILBOX0)
        print(f"  FECS CPUCTL: 0x{fecs_cpuctl:08x}")
        print(f"  FECS MAILBOX0: 0x{fecs_mb0:08x}")
    except:
        print(f"  FECS: not readable (PGRAPH may be off)")

    # Check WPR
    wpr2_lo = gpu.rd32(0x100CE0)
    wpr2_hi = gpu.rd32(0x100CE4)
    print(f"  WPR2: lo=0x{wpr2_lo:08x} hi=0x{wpr2_hi:08x}")

    # Summary
    final_mb0 = pmu.rd(FALCON_MAILBOX0)
    success = (final_mb0 == 0)

    print(f"\n{'='*60}")
    if success:
        print(f"  SECURE BOOT SUCCEEDED! (MAILBOX0 = 0)")
    else:
        print(f"  Secure boot status: MAILBOX0 = 0x{final_mb0:08x}")
        print(f"  Note: Full ACR boot requires proper ACR descriptor")
        print(f"  with firmware addresses — this is the next step.")
    print(f"  VRAM used: {vram.used() // 1024}KB")
    print(f"{'='*60}")

    return success


def main():
    gpu = PascalGPU()

    # Verify Pascal
    boot_0 = gpu.rd32(0x000000)
    arch = (boot_0 >> 20) & 0x1ff
    print(f"GPU: 0x{boot_0:08x} (arch=0x{arch:03x})")
    assert arch >= 0x130 and arch < 0x140, f"Not Pascal! arch=0x{arch:03x}"

    success = secboot_v2(gpu)
    gpu.close()
    return 0 if success else 1


if __name__ == "__main__":
    exit(main())
