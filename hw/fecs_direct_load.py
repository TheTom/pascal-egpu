#!/usr/bin/env python3
"""Direct FECS firmware loading — bypasses ACR secure boot entirely.

BREAKTHROUGH FINDING: On Pascal eGPU over Thunderbolt:
  - PMU Falcon is dead (power domain not initialized)
  - VRAM is not accessible (FB controller not POSTed)
  - BUT FECS Falcon (inside PGRAPH) is fully writable!

This means we can load firmware directly into FECS IMEM/DMEM
without going through the ACR secure boot chain.

FECS has 24KB IMEM and 4KB DMEM.
  fecs_inst.bin = 20,927 bytes (fits!)
  fecs_data.bin = 2,256 bytes (fits!)

Register write map (Pascal eGPU over Thunderbolt):
  ✅ PMC_ENABLE, PBUS, PGRAPH, FECS (IMEM/DMEM/MAILBOX)
  ❌ PMU, SEC2, PMC_SCRATCH, VRAM/PRAMIN
"""

import os
import sys
import struct
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from transport.tinygrad_transport import PascalGPU

FECS = 0x409000
GPCCS = 0x41a000

FW_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "firmware", "blobs", "gp106")


def load_fw(name: str) -> bytes:
    path = os.path.join(FW_DIR, name)
    with open(path, 'rb') as f:
        return f.read()


def falcon_load_imem(gpu, base: int, data: bytes):
    """Load data into Falcon IMEM with proper tagging.

    IMEM is written via port registers:
      IMEMC (0x180): control — set offset + auto-increment
      IMEMD (0x184): data — write 4 bytes at a time
      IMEMT (0x188): tag — set every 256 bytes

    Tag must be set BEFORE writing data for each 256-byte block.
    """
    num_words = (len(data) + 3) // 4
    num_blocks = (len(data) + 255) // 256

    for block in range(num_blocks):
        block_off = block * 256
        block_words = min(64, (len(data) - block_off + 3) // 4)  # 256/4 = 64 words per block

        # Set tag for this block
        gpu.wr32(base + 0x180, (block << 2))  # IMEMC: point to block
        gpu.wr32(base + 0x188, block)           # IMEMT: set tag

        # Set write mode with auto-increment at this block's word offset
        word_off = block * 64
        gpu.wr32(base + 0x180, (word_off << 2) | (1 << 24))  # auto-inc write

        # Write data words for this block
        for i in range(block_words):
            byte_off = block_off + i * 4
            if byte_off + 4 <= len(data):
                word = struct.unpack_from('<I', data, byte_off)[0]
            else:
                chunk = data[byte_off:].ljust(4, b'\x00')
                word = struct.unpack('<I', chunk)[0]
            gpu.wr32(base + 0x184, word)


def falcon_load_dmem(gpu, base: int, data: bytes, offset: int = 0):
    """Load data into Falcon DMEM."""
    gpu.wr32(base + 0x1c0, ((offset >> 2) << 2) | (1 << 24))  # DMEMC: auto-inc write
    for i in range(0, len(data), 4):
        if i + 4 <= len(data):
            word = struct.unpack_from('<I', data, i)[0]
        else:
            chunk = data[i:].ljust(4, b'\x00')
            word = struct.unpack('<I', chunk)[0]
        gpu.wr32(base + 0x1c4, word)


def falcon_verify_imem(gpu, base: int, data: bytes, check_words: int = 8) -> bool:
    """Verify IMEM contents match expected data."""
    gpu.wr32(base + 0x180, (0 << 2) | (1 << 25))  # read mode, auto-inc
    ok = True
    for i in range(min(check_words, len(data) // 4)):
        got = gpu.rd32(base + 0x184)
        expected = struct.unpack_from('<I', data, i * 4)[0]
        if got != expected:
            print(f"    IMEM[{i*4}]: 0x{got:08x} != 0x{expected:08x}")
            ok = False
    return ok


def main():
    print("=" * 60)
    print("  FECS Direct Firmware Load (Bypass ACR)")
    print("=" * 60)

    gpu = PascalGPU()

    # Verify GPU
    boot_0 = gpu.rd32(0x000000)
    print(f"\nGPU: 0x{boot_0:08x} (Pascal GP106)")

    # Ensure PGRAPH is enabled
    pmc = gpu.rd32(0x000200)
    if not (pmc & (1 << 12)):
        print("Enabling PGRAPH...")
        gpu.wr32(0x000200, pmc | (1 << 12))
        time.sleep(0.1)

    # FECS status
    print(f"\n--- FECS Falcon ---")
    hwcfg = gpu.rd32(FECS + 0x108)
    imem_size = ((hwcfg >> 0) & 0x1ff) * 256
    dmem_size = ((hwcfg >> 9) & 0x1ff) * 256
    cpuctl = gpu.rd32(FECS + 0x100)
    print(f"  IMEM: {imem_size}B, DMEM: {dmem_size}B")
    print(f"  CPUCTL: 0x{cpuctl:08x}")

    # Load firmware
    fecs_inst = load_fw("gr/fecs_inst.bin")
    fecs_data = load_fw("gr/fecs_data.bin")
    print(f"\n  fecs_inst.bin: {len(fecs_inst)}B {'(fits!)' if len(fecs_inst) <= imem_size else 'TOO BIG'}")
    print(f"  fecs_data.bin: {len(fecs_data)}B {'(fits!)' if len(fecs_data) <= dmem_size else 'TOO BIG'}")

    # Reset FECS
    print(f"\n--- Reset ---")
    gpu.wr32(FECS + 0x014, 0xffffffff)  # IRQMCLR
    gpu.wr32(FECS + 0x004, 0xffffffff)  # IRQSCLR
    time.sleep(0.05)

    # Set sentinel
    gpu.wr32(FECS + 0x040, 0xdeada5a5)  # MAILBOX0
    gpu.wr32(FECS + 0x044, 0x00000000)  # MAILBOX1

    # Load IMEM
    print(f"\n--- Loading IMEM ({len(fecs_inst)}B) ---")
    falcon_load_imem(gpu, FECS, fecs_inst)

    # Verify
    print("  Verifying first 8 words...")
    if falcon_verify_imem(gpu, FECS, fecs_inst, check_words=8):
        print("  IMEM verification PASSED ✅")
    else:
        print("  IMEM verification FAILED ❌")

    # Load DMEM
    print(f"\n--- Loading DMEM ({len(fecs_data)}B) ---")
    falcon_load_dmem(gpu, FECS, fecs_data)

    # Start FECS
    print(f"\n--- Starting FECS ---")
    gpu.wr32(FECS + 0x104, 0)  # BOOTVEC = 0

    # Enable alias + start
    gpu.wr32(FECS + 0x100, 0x40)   # ALIAS_EN
    time.sleep(0.01)
    gpu.wr32(FECS + 0x130, 0x02)   # STARTCPU via alias
    time.sleep(0.5)

    # Check result
    cpuctl = gpu.rd32(FECS + 0x100)
    mb0 = gpu.rd32(FECS + 0x040)
    mb1 = gpu.rd32(FECS + 0x044)
    os_reg = gpu.rd32(FECS + 0x050)

    print(f"  CPUCTL:   0x{cpuctl:08x}")
    print(f"  MAILBOX0: 0x{mb0:08x}")
    print(f"  MAILBOX1: 0x{mb1:08x}")
    print(f"  OS:       0x{os_reg:08x}")

    halted = bool(cpuctl & 0x10)
    running = bool(cpuctl & 0x02)
    mailbox_changed = (mb0 != 0xdeada5a5)

    print(f"\n--- Result ---")
    print(f"  CPU: {'HALTED' if halted else 'RUNNING' if running else 'STOPPED'}")
    print(f"  Firmware executed: {'YES' if mailbox_changed else 'UNKNOWN'}")

    if mailbox_changed and mb0 == 0:
        print(f"  STATUS: SUCCESS! FECS firmware initialized! 🔥")
    elif mailbox_changed:
        print(f"  STATUS: Firmware ran but returned error 0x{mb0:08x}")
    else:
        print(f"  STATUS: Mailbox unchanged — firmware may need DMA context")

    print(f"\n{'='*60}")
    gpu.close()


if __name__ == "__main__":
    main()
