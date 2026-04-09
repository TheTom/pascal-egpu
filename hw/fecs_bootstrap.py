#!/usr/bin/env python3
"""Bootstrap FECS Falcon with real firmware.

Clean state requirement:
  PMC_ENABLE = 0x7FFFFFFF (all engines)
  FECS SCTL = 0x3000 (LS mode, no HS)
  FECS CPUCTL = 0x10 (HALTED, clean reset state)

Strategy:
  1. Reset FECS (clear IRQs, DMACTL)
  2. Load fecs_inst.bin into IMEM (with correct tagging)
  3. Load fecs_data.bin into DMEM
  4. Set sentinel in mailbox
  5. Start FECS via CPUCTL
  6. Wait for halt / mailbox change
  7. Report status
"""

import os
import sys
import struct
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from transport.tinygrad_transport import PascalGPU

FECS = 0x409000
FW_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "firmware", "blobs", "gp106")


def load_fw(name: str) -> bytes:
    with open(os.path.join(FW_DIR, name), 'rb') as f:
        return f.read()


def falcon_load_imem(gpu, base: int, data: bytes, offset: int = 0):
    """Correct IMEM load with per-block tagging.

    Each 256-byte block needs a tag written to IMEMT register
    BEFORE any data is written to that block. The correct sequence is:
      For each block i:
        1. Write IMEMC = (block_word_addr << 2) | AUTOINC_WRITE | SECURE=0
        2. For each of 64 words in the block:
             Before the first word of the block, write IMEMT = block_idx
             Write IMEMD = data word
    """
    num_bytes = len(data)
    num_blocks = (num_bytes + 255) // 256

    for block_idx in range(num_blocks):
        block_offset = offset + block_idx * 256
        word_offset = block_offset // 4

        # Set IMEMC for auto-increment write starting at this block
        imemc = (word_offset << 2) | (1 << 24)  # bit 24 = AINCW (auto-inc write)
        gpu.wr32(base + 0x180, imemc)

        # Write the tag for this 256-byte block
        gpu.wr32(base + 0x188, (block_offset >> 8))  # IMEMT = tag index

        # Write 64 words (256 bytes) for this block
        for w in range(64):
            byte_off = block_idx * 256 + w * 4
            if byte_off >= num_bytes:
                break
            if byte_off + 4 <= num_bytes:
                word = struct.unpack_from('<I', data, byte_off)[0]
            else:
                chunk = data[byte_off:].ljust(4, b'\x00')
                word = struct.unpack('<I', chunk)[0]
            gpu.wr32(base + 0x184, word)


def falcon_load_dmem(gpu, base: int, data: bytes, offset: int = 0):
    """Load data into Falcon DMEM."""
    word_off = offset // 4
    gpu.wr32(base + 0x1c0, (word_off << 2) | (1 << 24))  # DMEMC: auto-inc write

    for i in range(0, len(data), 4):
        if i + 4 <= len(data):
            word = struct.unpack_from('<I', data, i)[0]
        else:
            chunk = data[i:].ljust(4, b'\x00')
            word = struct.unpack('<I', chunk)[0]
        gpu.wr32(base + 0x1c4, word)


def falcon_verify_imem(gpu, base: int, data: bytes, count_words: int = 8) -> bool:
    """Read back first N words from IMEM and compare."""
    # Set IMEMC for auto-increment read starting at 0
    gpu.wr32(base + 0x180, (0 << 2) | (1 << 25))  # AINCR (auto-inc read)

    ok = True
    for i in range(min(count_words, len(data) // 4)):
        got = gpu.rd32(base + 0x184)
        expected = struct.unpack_from('<I', data, i * 4)[0]
        if got != expected:
            print(f"    IMEM[{i*4}]: got 0x{got:08x}, expected 0x{expected:08x}")
            ok = False
    return ok


def main():
    print("=" * 60)
    print("  FECS Bootstrap — Clean State Attempt")
    print("=" * 60)

    gpu = PascalGPU()

    # 1. Set PMC_ENABLE clean state
    print("\n[1] Setting PMC_ENABLE = 0x7FFFFFFF...")
    gpu.wr32(0x000200, 0x7fffffff)
    time.sleep(0.1)
    pmc = gpu.rd32(0x000200)
    print(f"    PMC_ENABLE = 0x{pmc:08x}")

    # 2. Check FECS state
    print("\n[2] FECS initial state:")
    for name, off in [('CPUCTL', 0x100), ('SCTL', 0x240), ('DMACTL', 0x10c),
                       ('HWCFG', 0x108), ('MAILBOX0', 0x040)]:
        val = gpu.rd32(FECS + off)
        print(f"    {name:9s}: 0x{val:08x}")

    # 3. Reset FECS
    print("\n[3] Resetting FECS Falcon...")
    gpu.wr32(FECS + 0x014, 0xffffffff)  # IRQMCLR
    gpu.wr32(FECS + 0x004, 0xffffffff)  # IRQSCLR
    gpu.wr32(FECS + 0x10c, 0)            # DMACTL = 0
    time.sleep(0.05)

    # Set sentinel
    gpu.wr32(FECS + 0x040, 0xDEADA5A5)  # MAILBOX0
    gpu.wr32(FECS + 0x044, 0xDEADB0B0)  # MAILBOX1

    # 4. Load FECS firmware
    print("\n[4] Loading firmware...")
    fecs_inst = load_fw("gr/fecs_inst.bin")
    fecs_data = load_fw("gr/fecs_data.bin")
    print(f"    fecs_inst.bin: {len(fecs_inst)} bytes")
    print(f"    fecs_data.bin: {len(fecs_data)} bytes")

    hwcfg = gpu.rd32(FECS + 0x108)
    imem_size = ((hwcfg >> 0) & 0x1ff) * 256
    dmem_size = ((hwcfg >> 9) & 0x1ff) * 256
    print(f"    FECS IMEM: {imem_size} bytes, DMEM: {dmem_size} bytes")

    if len(fecs_inst) > imem_size:
        print(f"    ERROR: fecs_inst.bin ({len(fecs_inst)}) > IMEM ({imem_size})")
        return
    if len(fecs_data) > dmem_size:
        print(f"    ERROR: fecs_data.bin ({len(fecs_data)}) > DMEM ({dmem_size})")
        return

    print("    Loading IMEM...")
    falcon_load_imem(gpu, FECS, fecs_inst)

    print("    Loading DMEM...")
    falcon_load_dmem(gpu, FECS, fecs_data)

    # 5. Verify IMEM load
    print("\n[5] Verifying IMEM (first 16 words)...")
    if falcon_verify_imem(gpu, FECS, fecs_inst, count_words=16):
        print("    IMEM verification PASSED ✅")
    else:
        print("    IMEM verification FAILED ❌")
        return

    # 6. Start FECS
    print("\n[6] Starting FECS...")
    gpu.wr32(FECS + 0x104, 0)       # BOOTVEC = 0
    gpu.wr32(FECS + 0x100, 0x02)    # CPUCTL = STARTCPU

    # Monitor for 10 seconds
    print("    Monitoring execution (10s)...")
    prev_mb0 = 0xDEADA5A5
    start = time.time()

    while time.time() - start < 10:
        cpuctl = gpu.rd32(FECS + 0x100)
        mb0 = gpu.rd32(FECS + 0x040)
        mb1 = gpu.rd32(FECS + 0x044)

        if mb0 != prev_mb0:
            elapsed = time.time() - start
            print(f"    [{elapsed:.2f}s] MBOX changed: mb0=0x{mb0:08x} mb1=0x{mb1:08x} cpuctl=0x{cpuctl:08x}")
            prev_mb0 = mb0

        if cpuctl & 0x10:  # HALTED
            print(f"    FECS HALTED at {time.time() - start:.2f}s")
            break

        time.sleep(0.1)
    else:
        print("    Timeout — still running or stuck")

    # Final status
    print("\n[7] Final state:")
    for name, off in [('CPUCTL', 0x100), ('MAILBOX0', 0x040), ('MAILBOX1', 0x044),
                       ('OS', 0x050), ('IRQSTAT', 0x008)]:
        val = gpu.rd32(FECS + off)
        print(f"    {name:9s}: 0x{val:08x}")

    # Check GPCCS — did FECS bring it up?
    print("\n[8] GPCCS check:")
    GPCCS = 0x41a000
    for name, off in [('CPUCTL', 0x100), ('HWCFG', 0x108), ('SCTL', 0x240)]:
        val = gpu.rd32(GPCCS + off)
        status = "ALIVE" if val != 0xffffffff else "still dead"
        print(f"    {name:9s}: 0x{val:08x} ({status})")

    # Check VRAM access
    print("\n[9] VRAM check:")
    gpu.wr32(0x001700, 0x10)
    gpu.wr32(0x700000, 0xFEEDFACE)
    v = gpu.rd32(0x700000)
    print(f"    VRAM[0x100000]: 0x{v:08x} {'UNLOCKED!' if v == 0xFEEDFACE else 'still dead'}")

    gpu.close()


if __name__ == "__main__":
    main()
