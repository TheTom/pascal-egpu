#!/usr/bin/env python3
"""Verify Falcon really executes our code (not just transitions state).

Test sequence:
  Test A: Load IMEM with HALT, run, expect TRACEPC near 0
  Test B: Load IMEM with all 0x00, run — should also halt (0x00 = jmpi $r0?)
  Test C: Load IMEM with NOPs followed by HALT — TRACEPC should be at HALT addr
  Test D: Hand-encoded mov+iowr+halt — write known value to MAILBOX1
"""

import os
import sys
import struct
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from transport.tinygrad_transport import PascalGPU


# Use FECS as our test target
FECS = 0x409000

HALT = 0xf8020000   # 4-byte halt instruction
NOP  = 0xf8000000   # NOP? actually unknown — try


def reset_falcon(gpu, base):
    gpu.wr32(base + 0x10c, 0)            # DMACTL
    gpu.wr32(base + 0x014, 0xffffffff)   # IRQMCLR
    gpu.wr32(base + 0x004, 0xffffffff)   # IRQSCLR
    gpu.wr32(base + 0x040, 0xA5A5A5A5)   # MAILBOX0 sentinel
    gpu.wr32(base + 0x044, 0xB5B5B5B5)   # MAILBOX1 sentinel
    gpu.wr32(base + 0x100, 0x10)         # CPUCTL = HALTED


def load_imem(gpu, base, words, offset=0):
    """Load words into IMEM at byte offset (must be 256-byte aligned)."""
    word_addr = offset // 4
    block_idx = offset // 256
    gpu.wr32(base + 0x180, (word_addr << 2) | (1 << 24))  # IMEMC AINCW
    gpu.wr32(base + 0x188, block_idx)                     # IMEMT tag
    for w in words:
        gpu.wr32(base + 0x184, w)


def read_imem(gpu, base, count, offset=0):
    word_addr = offset // 4
    gpu.wr32(base + 0x180, (word_addr << 2) | (1 << 25))  # AINCR
    return [gpu.rd32(base + 0x184) for _ in range(count)]


def state(gpu, base, label):
    cpuctl = gpu.rd32(base + 0x100)
    mb0    = gpu.rd32(base + 0x040)
    mb1    = gpu.rd32(base + 0x044)
    pc     = gpu.rd32(base + 0x14c)  # TRACEPC
    os_    = gpu.rd32(base + 0x080)
    excode = gpu.rd32(base + 0x16c)  # EXCI? or HWCFG2 — check
    print(f"  {label:20}: CPUCTL=0x{cpuctl:08x}  PC=0x{pc:08x}  "
          f"MB0=0x{mb0:08x}  MB1=0x{mb1:08x}  OS=0x{os_:08x}")
    return cpuctl, pc, mb0, mb1


def run_test(gpu, name, ucode, expect_mb1=None):
    print(f"\n--- {name} ---")
    print(f"  ucode: {' '.join(f'0x{w:08x}' for w in ucode[:8])}{'...' if len(ucode) > 8 else ''}")

    # Reset
    reset_falcon(gpu, FECS)

    # Pad to multiple of 64 words (256 bytes / block)
    while len(ucode) % 64 != 0:
        ucode.append(HALT)

    # Load
    load_imem(gpu, FECS, ucode)

    # Verify
    rb = read_imem(gpu, FECS, min(4, len(ucode)))
    print(f"  IMEM verify: {' '.join(f'0x{w:08x}' for w in rb)}")
    if rb[0] != ucode[0]:
        print(f"  IMEM VERIFY FAILED")
        return

    # Snapshot before
    state(gpu, FECS, "before")

    # Start
    gpu.wr32(FECS + 0x104, 0)            # BOOTVEC = 0
    gpu.wr32(FECS + 0x100, 0x02)         # CPUCTL = STARTCPU
    time.sleep(0.05)

    # Snapshot after
    cpuctl, pc, mb0, mb1 = state(gpu, FECS, "after")

    # Verify
    if expect_mb1 is not None:
        if mb1 == expect_mb1:
            print(f"  *** SUCCESS: MAILBOX1 = 0x{mb1:08x} (expected) — REAL EXECUTION CONFIRMED ***")
        else:
            print(f"  MAILBOX1 unchanged or wrong: got 0x{mb1:08x}, expected 0x{expect_mb1:08x}")


def main():
    print("=" * 70)
    print("  Falcon Execution Verification")
    print("=" * 70)

    gpu = PascalGPU()
    if gpu.rd32(0) == 0xffffffff:
        print("GPU dead"); return

    cmd = gpu.cfg_read(0x04, 2)
    if not (cmd & 0x06):
        gpu.cfg_write(0x04, cmd | 0x06, 2)

    gpu.wr32(0x000200, 0xffffffff)
    time.sleep(0.05)

    # Test A: just halt
    run_test(gpu, "halt at IMEM[0]", [HALT])

    # Test B: zeros (might execute as something)
    run_test(gpu, "zeros at IMEM[0]", [0x00000000])

    # Test C: NOP NOP NOP HALT — check TRACEPC advances
    run_test(gpu, "nops then halt", [NOP, NOP, NOP, HALT])

    # Test D: hand-encoded mov + iowr + halt
    # mov b32 $r0 0xdeadbeef    : let's try ?? + 0x0f imm32
    # iowr I[$r1] $r0           : ??
    # halt                       : f8020000
    # We don't know exact encodings yet — try a few candidates
    #
    # From envytools: "mov b32 $rX imm32" is "fe Xs ii ii ii ii ii ii"
    # Actually for 32-bit immediates: "f0 0f"... still not sure
    #
    # Try LITERAL bytes for mov $r0, 0xdeadbeef + iowr + halt
    # Encoding guess (Pascal Falcon v5):
    #   bf 04 ef be ad de   - mov32 r0, 0xdeadbeef
    #   bf 14 44 00 00 00   - mov32 r1, 0x44 (mailbox1 offset)
    #   fa 10 00            - iowr [$r1+0], $r0  (3 bytes)
    #   f8 02 00 00         - halt
    # Pack into u32 little-endian:
    #
    # Actually, Falcon uses byte-stream with variable length instructions.
    # IMEM is 32-bit access but instructions are byte-aligned within.
    # Let's pack as bytes then convert to u32.
    bytes_prog = bytes([
        0xbf, 0x04, 0xef, 0xbe, 0xad, 0xde,   # mov32 $r0, 0xdeadbeef
        0xbf, 0x14, 0x44, 0x00, 0x00, 0x00,   # mov32 $r1, 0x44
        0xfa, 0x10, 0x00,                      # iowr I[$r1+0], $r0
        0xf8, 0x02, 0x00, 0x00,                # halt
    ])
    # Pad to multiple of 4
    while len(bytes_prog) % 4 != 0:
        bytes_prog += b'\x00'
    words = list(struct.unpack(f'<{len(bytes_prog)//4}I', bytes_prog))
    run_test(gpu, "mov+iowr mailbox1=0xdeadbeef", words, expect_mb1=0xdeadbeef)

    gpu.close()


if __name__ == "__main__":
    main()
