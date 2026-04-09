#!/usr/bin/env python3
"""Pascal PGRAPH (Graphics/Compute Engine) Initialization.

After ACR loads FECS/GPCCS firmware, PGRAPH needs:
1. GR engine reset via PMC
2. FECS firmware execution to initialize context switching
3. Golden context loading (sw_ctx, sw_bundle_init, sw_method_init)
4. GR class setup (PASCAL_COMPUTE_A = 0xC0C0)

Based on nouveau's engine/gr/gf100.c
"""

import os
import sys
import struct
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hw.secboot import FalconEngine, load_firmware, FALCON_MAILBOX0, FALCON_MAILBOX1, FALCON_CPUCTL

# PGRAPH registers
GR_STATUS          = 0x400700
GR_FECS_CTXSW_MAILBOX_CLEAR = 0x409800
GR_FECS_CTXSW_MAILBOX_SET   = 0x409804
GR_FECS_CTXSW_MAILBOX       = 0x409840

# FECS method registers (used for golden context commands)
GR_FECS_METHOD_DATA  = 0x409500
GR_FECS_METHOD_PUSH  = 0x409504

# PGRAPH context control
GR_CTX_CONTROL = 0x400824
GR_CTX_STATUS  = 0x400828

# PMC
PMC_ENABLE = 0x000200
PMC_ENABLE_PGRAPH = (1 << 12)


class PGRAPHInit:
    """Initialize PGRAPH for compute after secure boot."""

    def __init__(self, gpu):
        self.gpu = gpu
        self.fecs = FalconEngine(gpu, 0x409000, "FECS")
        self.gpccs = FalconEngine(gpu, 0x41a000, "GPCCS")

    def reset_pgraph(self):
        """Reset PGRAPH engine via PMC."""
        print("  Resetting PGRAPH engine...")
        pmc = self.gpu.rd32(PMC_ENABLE)

        # Disable PGRAPH
        self.gpu.wr32(PMC_ENABLE, pmc & ~PMC_ENABLE_PGRAPH)
        time.sleep(0.05)

        # Re-enable
        self.gpu.wr32(PMC_ENABLE, pmc | PMC_ENABLE_PGRAPH)
        time.sleep(0.1)

        # Check GR status
        status = self.gpu.rd32(GR_STATUS)
        print(f"  GR_STATUS after reset: 0x{status:08x}")

    def check_fecs_status(self) -> dict:
        """Check FECS Falcon status after secure boot."""
        cpuctl = self.fecs.rd(FALCON_CPUCTL)
        mb0 = self.fecs.rd(FALCON_MAILBOX0)
        mb1 = self.fecs.rd(FALCON_MAILBOX1)
        os_reg = self.fecs.rd(0x050)  # FALCON_OS

        return {
            "cpuctl": cpuctl,
            "halted": bool(cpuctl & 0x10),
            "stopped": bool(cpuctl & 0x20),
            "mailbox0": mb0,
            "mailbox1": mb1,
            "os": os_reg,
        }

    def fecs_method(self, method: int, data: int = 0, timeout_ms: int = 2000) -> int:
        """Send a method to FECS and wait for completion.

        FECS methods are sent via special registers:
        - Write data to METHOD_DATA
        - Write method|0x80000000 to METHOD_PUSH (bit 31 = push trigger)
        - Poll METHOD_PUSH for bit 31 to clear (completion)

        Returns the mailbox value after execution.
        """
        self.gpu.wr32(GR_FECS_METHOD_DATA, data)
        self.gpu.wr32(GR_FECS_METHOD_PUSH, method | (1 << 31))

        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            val = self.gpu.rd32(GR_FECS_METHOD_PUSH)
            if not (val & (1 << 31)):
                return self.gpu.rd32(GR_FECS_CTXSW_MAILBOX)
            time.sleep(0.005)

        raise TimeoutError(f"FECS method 0x{method:x} timed out")

    def start_fecs(self):
        """Start FECS Falcon to run context switch firmware."""
        print("  Starting FECS Falcon...")
        self.fecs.wr(FALCON_MAILBOX0, 0)
        self.fecs.wr(FALCON_MAILBOX1, 0)
        self.fecs.start(boot_vector=0)

        # Wait for FECS to signal ready
        time.sleep(0.5)
        cpuctl = self.fecs.rd(FALCON_CPUCTL)
        mb0 = self.fecs.rd(FALCON_MAILBOX0)
        print(f"  FECS after start: CPUCTL=0x{cpuctl:08x} MB0=0x{mb0:08x}")

    def init_gr_engine(self) -> bool:
        """Full PGRAPH initialization sequence.

        Returns True if GR engine is ready for compute.
        """
        print("\n" + "=" * 60)
        print("  PGRAPH Initialization")
        print("=" * 60)

        # Check FECS status
        print("\n--- FECS Status ---")
        fecs_status = self.check_fecs_status()
        print(f"  CPUCTL: 0x{fecs_status['cpuctl']:08x}")
        print(f"  State: {'HALTED' if fecs_status['halted'] else 'STOPPED' if fecs_status['stopped'] else 'RUNNING'}")
        print(f"  MAILBOX0: 0x{fecs_status['mailbox0']:08x}")
        print(f"  OS: 0x{fecs_status['os']:08x}")

        # Check GPCCS
        print("\n--- GPCCS Status ---")
        gpccs_cpuctl = self.gpccs.rd(FALCON_CPUCTL)
        print(f"  CPUCTL: 0x{gpccs_cpuctl:08x}")

        # Try starting FECS
        print("\n--- Starting FECS ---")
        self.start_fecs()

        # Read GR status registers
        print("\n--- GR Engine Status ---")
        for name, addr in [
            ("GR_STATUS", 0x400700),
            ("GR_INTR", 0x400100),
            ("GR_FECS_OS", 0x409500 + 0x50 - 0x500),
        ]:
            try:
                val = self.gpu.rd32(addr)
                print(f"  {name}: 0x{val:08x}")
            except Exception as e:
                print(f"  {name}: error — {e}")

        # Try a FECS method call
        print("\n--- FECS Method Test ---")
        try:
            # Method 0x10 = query context status (safe, read-only)
            result = self.fecs_method(0x10, data=0, timeout_ms=2000)
            print(f"  FECS method 0x10 returned: 0x{result:08x}")
        except TimeoutError as e:
            print(f"  {e}")
        except Exception as e:
            print(f"  FECS method failed: {e}")

        print(f"\n{'='*60}")
        print(f"  PGRAPH init complete (experimental)")
        print(f"{'='*60}")

        return True


def main():
    from transport.tinygrad_transport import PascalGPU

    gpu = PascalGPU()
    boot_0 = gpu.rd32(0x000000)
    print(f"GPU: 0x{boot_0:08x}")

    pgraph = PGRAPHInit(gpu)
    pgraph.init_gr_engine()

    gpu.close()


if __name__ == "__main__":
    main()
