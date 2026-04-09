#!/usr/bin/env python3
"""Pascal Secure Boot — ACR firmware loading on Falcon PMU.

Implements the ACR (Authenticated Code Resolver) boot sequence:
1. Load ACR bootloader (bl.bin) into PMU Falcon IMEM
2. Load ACR ucode (ucode_load.bin) into PMU Falcon DMEM
3. Start PMU Falcon — ACR verifies and loads FECS/GPCCS firmware
4. Wait for completion and verify success

Based on nouveau's gm200_secboot.c / acr_r352.c implementation.
"""

import os
import struct
import time

# Falcon register offsets
FALCON_IRQMCLR    = 0x014
FALCON_IRQSCLR    = 0x004
FALCON_MAILBOX0   = 0x040
FALCON_MAILBOX1   = 0x044
FALCON_CPUCTL     = 0x100
FALCON_BOOTVEC    = 0x104
FALCON_HWCFG      = 0x108
FALCON_DMACTL     = 0x10c
FALCON_DMATRFBASE = 0x110
FALCON_DMATRFMOFFS= 0x114
FALCON_DMATRFCMD  = 0x118
FALCON_DMATRFFBOFFS=0x11c
FALCON_IMEMC      = 0x180
FALCON_IMEMD      = 0x184
FALCON_IMEMT      = 0x188
FALCON_DMEMC      = 0x1c0
FALCON_DMEMD      = 0x1c4

# CPUCTL bits
CPUCTL_STARTCPU = (1 << 1)
CPUCTL_HALTED   = (1 << 4)

FIRMWARE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "firmware", "blobs", "gp106")


def load_firmware(name: str) -> bytes:
    """Load a firmware blob from the blobs directory."""
    path = os.path.join(FIRMWARE_DIR, name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Firmware not found: {path}")
    with open(path, 'rb') as f:
        data = f.read()
    print(f"  Loaded {name}: {len(data)} bytes")
    return data


class FalconEngine:
    """Direct Falcon register access via GPU transport."""

    def __init__(self, gpu, base: int, name: str):
        self.gpu = gpu
        self.base = base
        self.name = name

    def rd(self, offset: int) -> int:
        return self.gpu.rd32(self.base + offset)

    def wr(self, offset: int, value: int):
        self.gpu.wr32(self.base + offset, value)

    def is_halted(self) -> bool:
        return bool(self.rd(FALCON_CPUCTL) & CPUCTL_HALTED)

    def hwcfg(self) -> dict:
        val = self.rd(FALCON_HWCFG)
        return {
            "imem_size": ((val >> 0) & 0x1ff) * 256,
            "dmem_size": ((val >> 9) & 0x1ff) * 256,
        }

    def reset(self):
        """Reset Falcon CPU."""
        print(f"  Resetting {self.name} Falcon...")
        self.wr(FALCON_IRQMCLR, 0xffffffff)
        self.wr(FALCON_IRQSCLR, 0xffffffff)
        # Write 0 to CPUCTL to reset
        self.wr(FALCON_CPUCTL, 0)
        time.sleep(0.05)
        print(f"  {self.name} reset complete")

    def load_imem(self, data: bytes, offset: int = 0):
        """Load data into Falcon IMEM via port registers."""
        print(f"  Loading {len(data)} bytes into {self.name} IMEM at 0x{offset:x}...")

        # Set IMEM auto-increment write mode
        block = offset >> 8
        self.wr(FALCON_IMEMC, (block << 2) | (1 << 24))

        # Write data 4 bytes at a time
        for i in range(0, len(data), 4):
            if i + 4 <= len(data):
                word = struct.unpack_from('<I', data, i)[0]
            else:
                chunk = data[i:].ljust(4, b'\x00')
                word = struct.unpack('<I', chunk)[0]
            self.wr(FALCON_IMEMD, word)

        # Set IMEM tags (each 256-byte block needs a tag)
        num_blocks = (len(data) + 255) // 256
        for i in range(num_blocks):
            tag = (offset >> 8) + i
            self.wr(FALCON_IMEMC, (tag << 2))
            self.wr(FALCON_IMEMT, tag)

        print(f"  IMEM loaded ({num_blocks} blocks)")

    def load_dmem(self, data: bytes, offset: int = 0):
        """Load data into Falcon DMEM via port registers."""
        print(f"  Loading {len(data)} bytes into {self.name} DMEM at 0x{offset:x}...")

        self.wr(FALCON_DMEMC, ((offset >> 2) << 2) | (1 << 24))

        for i in range(0, len(data), 4):
            if i + 4 <= len(data):
                word = struct.unpack_from('<I', data, i)[0]
            else:
                chunk = data[i:].ljust(4, b'\x00')
                word = struct.unpack('<I', chunk)[0]
            self.wr(FALCON_DMEMD, word)

        print(f"  DMEM loaded")

    def start(self, boot_vector: int = 0):
        """Start Falcon at the given boot vector."""
        print(f"  Starting {self.name} at boot vector 0x{boot_vector:x}...")
        self.wr(FALCON_BOOTVEC, boot_vector)
        self.wr(FALCON_CPUCTL, CPUCTL_STARTCPU)

    def wait_halted(self, timeout_s: float = 5.0) -> bool:
        """Wait for Falcon to halt."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.is_halted():
                return True
            time.sleep(0.01)
        return False


def secboot_load_acr(gpu) -> bool:
    """Load ACR firmware onto PMU Falcon and start secure boot.

    Returns True if ACR boot succeeded, False otherwise.
    """
    print("\n" + "=" * 60)
    print("  Secure Boot — Loading ACR on PMU Falcon")
    print("=" * 60)

    pmu = FalconEngine(gpu, 0x10a000, "PMU")

    # Check PMU state
    hw = pmu.hwcfg()
    print(f"\n  PMU IMEM: {hw['imem_size']} bytes, DMEM: {hw['dmem_size']} bytes")

    cpuctl = pmu.rd(FALCON_CPUCTL)
    print(f"  PMU CPUCTL: 0x{cpuctl:08x} (halted={bool(cpuctl & 0x10)}, stopped={bool(cpuctl & 0x20)})")

    # Step 1: Load firmware blobs
    print(f"\n--- Loading Firmware ---")
    acr_bl = load_firmware("acr/bl.bin")
    acr_ucode = load_firmware("acr/ucode_load.bin")

    # Validate sizes
    if len(acr_bl) > hw["imem_size"]:
        raise RuntimeError(f"ACR bootloader ({len(acr_bl)}B) exceeds IMEM ({hw['imem_size']}B)")
    if len(acr_ucode) > hw["dmem_size"]:
        raise RuntimeError(f"ACR ucode ({len(acr_ucode)}B) exceeds DMEM ({hw['dmem_size']}B)")

    # Step 2: Reset PMU Falcon
    print(f"\n--- Reset PMU ---")
    pmu.reset()

    # Step 3: Set sentinel in mailbox
    pmu.wr(FALCON_MAILBOX0, 0xdeada5a5)
    pmu.wr(FALCON_MAILBOX1, 0x00000000)

    # Step 4: Load ACR bootloader into IMEM
    print(f"\n--- Load ACR Bootloader into IMEM ---")
    pmu.load_imem(acr_bl, offset=0)

    # Step 5: Load ACR ucode into DMEM
    print(f"\n--- Load ACR Ucode into DMEM ---")
    pmu.load_dmem(acr_ucode, offset=0)

    # Step 6: Start PMU Falcon
    print(f"\n--- Starting PMU Falcon ---")
    pmu.start(boot_vector=0)

    # Step 7: Wait for completion
    print(f"  Waiting for ACR to complete...", flush=True)
    if pmu.wait_halted(timeout_s=10.0):
        mailbox0 = pmu.rd(FALCON_MAILBOX0)
        mailbox1 = pmu.rd(FALCON_MAILBOX1)
        print(f"  PMU halted!")
        print(f"  MAILBOX0: 0x{mailbox0:08x}")
        print(f"  MAILBOX1: 0x{mailbox1:08x}")

        if mailbox0 != 0xdeada5a5:
            print(f"  ACR boot appears to have run (mailbox changed)")
            if mailbox0 == 0:
                print(f"  STATUS: SUCCESS (mailbox0 = 0)")
                return True
            else:
                print(f"  STATUS: FAILED (mailbox0 = 0x{mailbox0:08x})")
                return False
        else:
            print(f"  WARNING: Mailbox unchanged — ACR may not have executed")
            return False
    else:
        print(f"  TIMEOUT: PMU did not halt within 10 seconds")
        cpuctl = pmu.rd(FALCON_CPUCTL)
        mailbox0 = pmu.rd(FALCON_MAILBOX0)
        print(f"  CPUCTL: 0x{cpuctl:08x}")
        print(f"  MAILBOX0: 0x{mailbox0:08x}")
        return False


def main():
    """Run secure boot sequence."""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from transport.tinygrad_transport import PascalGPU

    print("=" * 60)
    print("  Pascal Secure Boot")
    print("=" * 60)

    gpu = PascalGPU()

    # Verify Pascal
    boot_0 = gpu.rd32(0x000000)
    arch = (boot_0 >> 20) & 0x1ff
    print(f"\nGPU: 0x{boot_0:08x} (arch=0x{arch:03x})")

    # Enable PMU engine
    pmc_enable = gpu.rd32(0x000200)
    if not (pmc_enable & (1 << 13)):
        print("Enabling PMU engine...")
        gpu.wr32(0x000200, pmc_enable | (1 << 13))
        time.sleep(0.1)

    # Run ACR boot
    success = secboot_load_acr(gpu)

    if success:
        print(f"\n{'='*60}")
        print(f"  SECURE BOOT SUCCEEDED!")
        print(f"  FECS/GPCCS firmware should now be loaded.")
        print(f"  Next: PGRAPH init + compute dispatch")
        print(f"{'='*60}")
    else:
        print(f"\n{'='*60}")
        print(f"  SECURE BOOT DID NOT COMPLETE")
        print(f"  This is expected — ACR needs proper DMA setup,")
        print(f"  instance blocks, and WPR configuration.")
        print(f"  The IMEM/DMEM direct load is a first step.")
        print(f"{'='*60}")

    gpu.close()


if __name__ == "__main__":
    main()
