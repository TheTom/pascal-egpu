# Pascal eGPU Setup Guide

## Prerequisites

- macOS Apple Silicon (M1/M2/M3/M4/M5)
- Thunderbolt eGPU enclosure with NVIDIA Pascal GPU (GTX 1060, 1070, 1080, etc.)
- Python 3.12+

## Step 1: Install tinygrad (for TinyGPU driver)

```bash
git clone https://github.com/tinygrad/tinygrad.git
cd tinygrad
pip3 install -e . --break-system-packages
```

## Step 2: Install TinyGPU DriverKit Extension

```bash
python3 -c "
from tinygrad.runtime.support.system import APLRemotePCIDevice
APLRemotePCIDevice.ensure_app()
"
```

This downloads TinyGPU.app to `/Applications/` and triggers the driver extension install.

## Step 3: Approve TinyGPU in System Settings

**You MUST do this or BAR mapping will fail.**

1. Go to **System Settings → Privacy & Security**
2. Scroll down and click **Allow** for TinyGPU

Or:

1. Go to **System Settings → General → Login Items & Extensions → Driver Extensions**
2. Toggle **TinyGPU** ON

If you miss the prompt, you can re-trigger it:
```bash
/Applications/TinyGPU.app/Contents/MacOS/TinyGPU install
```

## Step 4: Connect eGPU and Verify

Plug in your eGPU enclosure via Thunderbolt, then:

```bash
cd pascal-egpu
python3 transport/pci_scan.py
```

Expected output:
```
=== PCIe Device Scan (NVIDIA) ===

Found 2 NVIDIA device(s):

GPU #0:
  Name:      GTX 1060 6GB
  Device ID: 0x1c03
  Vendor ID: 0x10de
  Pascal:    YES
```

## Step 5: Read GPU Registers (requires TinyGPU approved)

```bash
python3 transport/bar_map.py
```

This maps BAR0 (16 MB MMIO) and reads the GPU identification register.

## Troubleshooting

### "IOServiceOpen failed: kIOReturnNotPermitted"
TinyGPU driver extension not approved. Go to System Settings and approve it (Step 3).

### "No NVIDIA GPUs found on PCIe bus"
- Check eGPU enclosure is powered on
- Check Thunderbolt cable is connected
- Try unplugging and replugging
- Run `ioreg -r -d 1 -c IOPCIDevice | grep 10de` to check if macOS sees the device

### TinyGPU says "Ampere+ required"
That restriction is in tinygrad's `ops_nv.py`, not in TinyGPU itself. TinyGPU's DriverKit extension maps PCIe BARs for any device — the GPU architecture check is in the userspace code which we bypass.

## Hardware Tested

| GPU | Device ID | eGPU Enclosure | Status |
|-----|-----------|---------------|--------|
| GTX 1060 6GB (Gigabyte) | 0x1c03 | Razer Core X Chroma V2 | PCIe enum ✅, MMIO ✅, Falcon init ⏳ |

## BAR Layout (Pascal GP106)

| BAR | Address | Size | Purpose |
|-----|---------|------|---------|
| BAR0 | 0x000a01000000 | 16 MB | GPU MMIO registers |
| BAR1 | 0x000a40000000 | 256 MB | VRAM aperture |
| BAR2 | 0x000a50000000 | 32 MB | RAMIN (control structures) |
| BAR3 | 0x000a02000000 | 512 KB | I/O |

*Addresses will vary per system — read from IODeviceMemory property at runtime.*
