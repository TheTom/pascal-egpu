#!/usr/bin/env python3
"""Pascal Secure Boot v3 — with proper ACR bootloader descriptor.

Key fix: The ACR bootloader (bl.bin) expects a flcn_bl_dmem_desc
struct at the start of DMEM, NOT the raw ucode. The descriptor
tells the bootloader where to DMA the ucode from VRAM.

Sequence:
1. Place ACR ucode in VRAM
2. Build flcn_bl_dmem_desc pointing to VRAM ucode
3. Load bl.bin into PMU IMEM
4. Load flcn_bl_dmem_desc into PMU DMEM
5. Start PMU — bootloader DMAs ucode, ucode runs ACR
"""

import os
import sys
import struct
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from transport.tinygrad_transport import PascalGPU
from hw.mmu import PascalMMU, VRAMAllocator
from hw.secboot import FalconEngine, load_firmware, FALCON_MAILBOX0, FALCON_MAILBOX1, FALCON_CPUCTL
from hw.acr_desc import build_bl_dmem_desc, build_wpr_header, build_wpr_header_terminator, FALCON_ID_FECS, FALCON_ID_GPCCS

# Falcon registers
FALCON_INST_BLOCK = 0x480

# PMC
PMC_ENABLE = 0x000200
PMC_ENABLE_PMU = (1 << 13)
PMC_ENABLE_SEC2 = (1 << 29)
PMC_ENABLE_PGRAPH = (1 << 12)
PMC_ENABLE_PFIFO = (1 << 8)
PMC_ENABLE_CE0 = (1 << 17)


def main():
    print("=" * 60)
    print("  Pascal Secure Boot v3 — Proper BL Descriptor")
    print("=" * 60)

    gpu = PascalGPU()
    boot_0 = gpu.rd32(0x000000)
    print(f"\nGPU: 0x{boot_0:08x}")

    # Enable engines
    pmc = gpu.rd32(PMC_ENABLE)
    needed = PMC_ENABLE_PMU | PMC_ENABLE_SEC2 | PMC_ENABLE_PFIFO | PMC_ENABLE_PGRAPH | PMC_ENABLE_CE0
    gpu.wr32(PMC_ENABLE, pmc | needed)
    time.sleep(0.1)

    # VRAM allocator starting at 1MB
    vram = VRAMAllocator(base=0x100000, size=64 * 1024 * 1024)
    mmu = PascalMMU(gpu, vram)

    # Set up page tables
    print("\n[1] Setting up MMU...")
    inst_addr = mmu.setup_linear_mapping(vram_size_mb=64)

    # Place ACR ucode in VRAM
    print("\n[2] Placing ACR ucode in VRAM...")
    acr_ucode = load_firmware("acr/ucode_load.bin")
    acr_ucode_addr = vram.alloc(len(acr_ucode), align=256)
    mmu.write_vram_block(acr_ucode_addr, acr_ucode)
    print(f"  ACR ucode at VRAM 0x{acr_ucode_addr:08x} ({len(acr_ucode)} bytes)")

    # Place FECS/GPCCS firmware in VRAM
    print("\n[3] Placing LS falcon firmware in VRAM...")
    fw_addrs = {}
    for name in ["gr/fecs_bl.bin", "gr/fecs_inst.bin", "gr/fecs_data.bin", "gr/fecs_sig.bin",
                  "gr/gpccs_bl.bin", "gr/gpccs_inst.bin", "gr/gpccs_data.bin", "gr/gpccs_sig.bin"]:
        data = load_firmware(name)
        addr = vram.alloc(len(data), align=256)
        mmu.write_vram_block(addr, data)
        fw_addrs[name] = {"addr": addr, "size": len(data)}
        print(f"    {name}: 0x{addr:08x}")

    # Build WPR header table in VRAM
    print("\n[4] Building WPR headers...")
    wpr_data = bytearray()
    wpr_data += build_wpr_header(FALCON_ID_FECS, lsb_offset=0x100, bootstrap_owner=7)
    wpr_data += build_wpr_header(FALCON_ID_GPCCS, lsb_offset=0x200, bootstrap_owner=7)
    wpr_data += build_wpr_header_terminator()

    wpr_addr = vram.alloc(len(wpr_data), align=256)
    mmu.write_vram_block(wpr_addr, bytes(wpr_data))
    print(f"  WPR headers at 0x{wpr_addr:08x} ({len(wpr_data)} bytes)")

    # Build ACR bootloader DMEM descriptor
    print("\n[5] Building BL DMEM descriptor...")
    # The ACR ucode is a single blob — code and data are contiguous
    # The bootloader will DMA it from VRAM to Falcon DMEM/IMEM
    bl_desc = build_bl_dmem_desc(
        code_dma_base=acr_ucode_addr,
        code_size=len(acr_ucode),
        data_dma_base=acr_ucode_addr,  # data follows code in the same blob
        data_size=0,
        code_entry_point=0,
    )
    print(f"  BL descriptor: {len(bl_desc)} bytes")
    print(f"  code_dma_base: 0x{acr_ucode_addr:08x} (>> 8 = 0x{acr_ucode_addr >> 8:08x})")

    # Configure PMU Falcon
    print("\n[6] Configuring PMU Falcon...")
    pmu = FalconEngine(gpu, 0x10a000, "PMU")

    hw = pmu.hwcfg()
    print(f"  IMEM={hw['imem_size']}B, DMEM={hw['dmem_size']}B")

    # Reset
    pmu.reset()

    # Bind instance block for DMA
    val = (inst_addr >> 12) | 0x1
    pmu.wr(FALCON_INST_BLOCK, val)
    pmu.wr(FALCON_INST_BLOCK + 4, 0)
    time.sleep(0.05)
    print(f"  Instance block bound: 0x{inst_addr:08x}")

    # Set sentinel
    pmu.wr(FALCON_MAILBOX0, 0xdeada5a5)
    pmu.wr(FALCON_MAILBOX1, 0xdeadb0b0)

    # Load bootloader into IMEM
    print("\n[7] Loading ACR bootloader into IMEM...")
    acr_bl = load_firmware("acr/bl.bin")
    pmu.load_imem(acr_bl, offset=0)

    # Load BL descriptor into DMEM (NOT the ucode — the descriptor!)
    print("\n[8] Loading BL descriptor into DMEM...")
    pmu.load_dmem(bl_desc, offset=0)

    # Start Falcon
    print("\n[9] Starting PMU Falcon...")
    pmu.start(boot_vector=0)

    # Monitor execution
    print("  Monitoring...", flush=True)
    prev_mb0 = 0xdeada5a5
    prev_mb1 = 0xdeadb0b0

    for i in range(100):
        time.sleep(0.1)
        cpuctl = pmu.rd(FALCON_CPUCTL)
        mb0 = pmu.rd(FALCON_MAILBOX0)
        mb1 = pmu.rd(FALCON_MAILBOX1)

        if mb0 != prev_mb0 or mb1 != prev_mb1:
            print(f"  [{i*0.1:.1f}s] MB0=0x{mb0:08x} MB1=0x{mb1:08x} CPUCTL=0x{cpuctl:08x}", flush=True)
            prev_mb0, prev_mb1 = mb0, mb1

        if cpuctl & 0x10:  # HALTED
            print(f"  PMU HALTED at {i*0.1:.1f}s")
            break
        if (cpuctl & 0x20) and not (cpuctl & 0x02):  # STOPPED
            print(f"  PMU STOPPED at {i*0.1:.1f}s")
            break

    # Final status
    cpuctl = pmu.rd(FALCON_CPUCTL)
    mb0 = pmu.rd(FALCON_MAILBOX0)
    mb1 = pmu.rd(FALCON_MAILBOX1)

    print(f"\n--- Final PMU Status ---")
    print(f"  CPUCTL:   0x{cpuctl:08x}")
    print(f"  MAILBOX0: 0x{mb0:08x}")
    print(f"  MAILBOX1: 0x{mb1:08x}")

    # Check FECS
    fecs = FalconEngine(gpu, 0x409000, "FECS")
    fecs_cpuctl = fecs.rd(FALCON_CPUCTL)
    fecs_mb0 = fecs.rd(FALCON_MAILBOX0)
    print(f"\n--- FECS Status ---")
    print(f"  CPUCTL:   0x{fecs_cpuctl:08x}")
    print(f"  MAILBOX0: 0x{fecs_mb0:08x}")

    # WPR
    wpr_lo = gpu.rd32(0x100CE0)
    wpr_hi = gpu.rd32(0x100CE4)
    print(f"\n--- WPR2 ---")
    print(f"  LO: 0x{wpr_lo:08x}  HI: 0x{wpr_hi:08x}")

    print(f"\n  VRAM used: {vram.used() // 1024}KB")
    print(f"{'='*60}")

    gpu.close()


if __name__ == "__main__":
    main()
