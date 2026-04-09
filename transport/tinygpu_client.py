#!/usr/bin/env python3
"""TinyGPU socket client for Pascal eGPU BAR access.

Connects to TinyGPU.app's Unix socket server to map PCIe BARs
and perform MMIO reads/writes on the GPU.

TinyGPU must be installed and approved:
  1. pip3 install tinygrad
  2. python3 -c "from tinygrad.runtime.support.system import APLRemotePCIDevice; APLRemotePCIDevice.ensure_app()"
  3. Approve in System Settings → Privacy & Security
"""

import socket
import struct
import subprocess
import time
import os
import tempfile


class RemoteCmd:
    """TinyGPU RPC command IDs (must match tinygrad's RemoteCmd enum)."""
    PROBE = 0
    MAP_BAR = 1
    MAP_SYSMEM_FD = 2
    CFG_READ = 3
    CFG_WRITE = 4
    RESET = 5
    MMIO_READ = 6
    MMIO_WRITE = 7
    MAP_SYSMEM = 8
    SYSMEM_READ = 9
    SYSMEM_WRITE = 10
    RESIZE_BAR = 11
    PING = 12


class TinyGPUClient:
    """Client for TinyGPU.app Unix socket server."""

    APP_PATH = "/Applications/TinyGPU.app/Contents/MacOS/TinyGPU"

    def __init__(self, dev_id=0, sock_path=None):
        self.dev_id = dev_id
        self.sock_path = sock_path or os.path.join(tempfile.gettempdir(), "tinygpu.sock")
        self.sock = None

    def connect(self, timeout=5.0):
        """Connect to TinyGPU server, starting it if needed."""
        if not os.path.exists(self.APP_PATH):
            raise RuntimeError(
                f"TinyGPU.app not found at {self.APP_PATH}. "
                "Install with: python3 -c \"from tinygrad.runtime.support.system import APLRemotePCIDevice; APLRemotePCIDevice.ensure_app()\""
            )

        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 << 20)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 << 20)

        start = time.time()
        server_started = False

        while time.time() - start < timeout:
            try:
                self.sock.connect(self.sock_path)
                return  # Connected
            except (ConnectionRefusedError, FileNotFoundError):
                if not server_started:
                    print(f"Starting TinyGPU server at {self.sock_path}...")
                    subprocess.Popen(
                        [self.APP_PATH, "server", self.sock_path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                    server_started = True
                time.sleep(0.1)

        raise RuntimeError(
            f"Failed to connect to TinyGPU server at {self.sock_path} after {timeout}s. "
            "Make sure TinyGPU is approved in System Settings → Privacy & Security."
        )

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def _recvall(self, n):
        data = b''
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise RuntimeError("Connection closed")
            data += chunk
        return data

    def _rpc(self, cmd, *args, bar=0, readout_size=0):
        """Send RPC command and receive response."""
        padded = (*args, 0, 0, 0)[:3]
        self.sock.sendall(struct.pack('<BIIQQQ', cmd, self.dev_id, bar, *padded))

        resp = struct.unpack('<QQB', self._recvall(17))
        # resp = (value1, value2, status)

        readout = None
        if readout_size > 0:
            readout = self._recvall(readout_size)

        return resp[0], resp[1], readout

    def ping(self):
        """Check if server is alive."""
        v1, v2, _ = self._rpc(RemoteCmd.PING)
        return True

    def bar_info(self, bar_idx):
        """Get BAR physical address and size."""
        addr, size, _ = self._rpc(RemoteCmd.MAP_BAR, bar=bar_idx)
        return addr, size

    def read_config(self, offset, size):
        """Read PCI config space."""
        val, _, _ = self._rpc(RemoteCmd.CFG_READ, offset, size)
        return val

    def write_config(self, offset, value, size):
        """Write PCI config space."""
        self._rpc(RemoteCmd.CFG_WRITE, offset, size, value)

    def mmio_read(self, bar, offset, size):
        """Read MMIO data from a BAR."""
        self.sock.sendall(struct.pack('<BIIQQQ', RemoteCmd.MMIO_READ, self.dev_id, bar, offset, size, 0))
        resp = struct.unpack('<QQB', self._recvall(17))
        data = self._recvall(size)
        return data

    def mmio_read32(self, bar, offset):
        """Read a single 32-bit register from a BAR."""
        data = self.mmio_read(bar, offset, 4)
        return struct.unpack('<I', data)[0]

    def mmio_write(self, bar, offset, data):
        """Write MMIO data to a BAR."""
        self.sock.sendall(
            struct.pack('<BIIQQQ', RemoteCmd.MMIO_WRITE, self.dev_id, bar, offset, len(data), 0) + data
        )

    def mmio_write32(self, bar, offset, value):
        """Write a single 32-bit register to a BAR."""
        self.mmio_write(bar, offset, struct.pack('<I', value))


def main():
    """Test TinyGPU connection and read GPU ID."""
    client = TinyGPUClient()

    print("=== TinyGPU Pascal GPU Probe ===\n")

    try:
        client.connect()
        print("Connected to TinyGPU server!")
    except RuntimeError as e:
        print(f"Failed: {e}")
        return

    # Ping
    try:
        client.ping()
        print("Server ping: OK")
    except Exception as e:
        print(f"Ping failed: {e}")
        client.close()
        return

    # Read PCI config (vendor + device ID)
    try:
        vid_did = client.read_config(0x00, 4)
        vendor_id = vid_did & 0xffff
        device_id = (vid_did >> 16) & 0xffff
        print(f"\nPCI Config Space:")
        print(f"  Vendor ID: 0x{vendor_id:04x}")
        print(f"  Device ID: 0x{device_id:04x}")
    except Exception as e:
        print(f"Config read failed: {e}")

    # Get BAR info
    for bar_idx in range(3):
        try:
            addr, size = client.bar_info(bar_idx)
            print(f"\nBAR{bar_idx}: addr=0x{addr:012x} size={size // (1024*1024)}MB ({size:#x})")
        except Exception as e:
            print(f"BAR{bar_idx}: {e}")

    # THE BIG TEST: Read BAR0 register PMC_BOOT_0
    print("\n=== Reading GPU Registers (BAR0 MMIO) ===\n")
    try:
        boot_0 = client.mmio_read32(0, 0x000000)
        print(f"PMC_BOOT_0 (0x000000): 0x{boot_0:08x}")

        # Decode PMC_BOOT_0
        # [3:0]   = minor revision
        # [7:4]   = major revision
        # [19:12] = stepping (from NV_PMC_BOOT_0_MINOR_REVISION etc)
        # [23:20] = implementation
        # [27:24] = architecture
        arch = (boot_0 >> 20) & 0x1ff
        impl = (boot_0 >> 16) & 0xf
        print(f"  Architecture: 0x{arch:03x}")
        print(f"  Implementation: 0x{impl:x}")

        if arch >= 0x130 and arch < 0x140:
            print(f"  => PASCAL (GP10x) confirmed! 🎉")
        elif arch >= 0x120 and arch < 0x130:
            print(f"  => Maxwell (GM20x)")
        elif arch >= 0x140 and arch < 0x150:
            print(f"  => Volta (GV10x)")
        elif arch >= 0x160 and arch < 0x170:
            print(f"  => Turing (TU10x)")
        else:
            print(f"  => Unknown architecture")

        # Read PTIMER (always works, even without full init)
        timer_lo = client.mmio_read32(0, 0x009400)
        timer_hi = client.mmio_read32(0, 0x009410)
        timer_ns = (timer_hi << 32) | timer_lo
        print(f"\nPTIMER (0x009400): {timer_ns} ns ({timer_ns / 1e9:.3f} sec)")

        time.sleep(0.01)
        timer_lo2 = client.mmio_read32(0, 0x009400)
        timer_hi2 = client.mmio_read32(0, 0x009410)
        timer_ns2 = (timer_hi2 << 32) | timer_lo2
        delta = timer_ns2 - timer_ns
        print(f"PTIMER delta: {delta} ns")
        if delta > 0:
            print(f"  => GPU timer is TICKING! Hardware is alive! 🔥")
        else:
            print(f"  => Timer not advancing (GPU may need init)")

        # Read a few more ID registers
        pmc_boot_42 = client.mmio_read32(0, 0x000108)  # NV_PMC_BOOT_42
        print(f"\nPMC_BOOT_42 (0x000108): 0x{pmc_boot_42:08x}")

        # PMC_ENABLE - which engines are enabled
        pmc_enable = client.mmio_read32(0, 0x000200)
        print(f"PMC_ENABLE (0x000200): 0x{pmc_enable:08x}")

        # PBUS revision
        pbus_bar0_window = client.mmio_read32(0, 0x001700)
        print(f"PBUS_BAR0_WINDOW (0x001700): 0x{pbus_bar0_window:08x}")

        # VRAM size from straps
        pfb_cfg0 = client.mmio_read32(0, 0x100000)
        print(f"PFB_CFG0 (0x100000): 0x{pfb_cfg0:08x}")

        print(f"\n=== SUCCESS: GTX 1060 is talking to us over Thunderbolt! ===")

    except Exception as e:
        print(f"MMIO read failed: {e}")
        import traceback
        traceback.print_exc()

    client.close()


if __name__ == "__main__":
    main()
