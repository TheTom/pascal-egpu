# Pascal eGPU

**Driverless NVIDIA Pascal compute from macOS Apple Silicon over Thunderbolt eGPU**

> First confirmed register-level access to a GTX 1060 from an Apple Silicon Mac — no NVIDIA drivers, no CUDA, no SIP disabled.

## What This Is

A from-scratch userspace driver that talks directly to NVIDIA Pascal GPUs (GTX 10-series) over Thunderbolt/USB4 eGPU from macOS Apple Silicon. Uses [TinyGPU](https://docs.tinygrad.org/tinygpu/) for PCIe BAR mapping via Apple's DriverKit framework.

TinyGPU officially supports Ampere+ (RTX 30-series and newer). **This project proves Pascal works too** — the Ampere+ restriction is in tinygrad's userspace code, not in the DriverKit extension itself.

## Current Status

| Milestone | Status |
|-----------|--------|
| PCIe enumeration (find GPU on bus) | ✅ Working |
| BAR0 MMIO register access | ✅ Working |
| GPU identification (PMC_BOOT_0) | ✅ `0x136000a1` — GP106 rev A1 confirmed |
| GPU timer (PTIMER) | ✅ Ticking — hardware alive |
| Falcon PMU init | ⏳ Next |
| ACR secure boot (FECS/GPCCS firmware) | ⏳ Planned |
| MMU / page table setup | ⏳ Planned |
| FIFO channel + GPFIFO | ⏳ Planned |
| Compute dispatch (vector add) | ⏳ Planned |

## First Register Read

```
$ python3 transport/tinygpu_client.py

=== TinyGPU Pascal GPU Probe ===

Connected to TinyGPU server!
Server ping: OK

PMC_BOOT_0 (0x000000): 0x136000a1
  Architecture: 0x136
  => PASCAL (GP10x) confirmed! 🎉

PTIMER delta: 9,484,448 ns
  => GPU timer is TICKING! Hardware is alive! 🔥

=== SUCCESS: GTX 1060 is talking to us over Thunderbolt! ===
```

## Hardware

| Component | Model | Notes |
|-----------|-------|-------|
| **Host** | Mac Studio M5 Max 128GB | macOS, Apple Silicon |
| **eGPU Enclosure** | Razer Core X Chroma V2 | Thunderbolt 3, 650W PSU |
| **GPU** | NVIDIA GTX 1060 6GB (Gigabyte) | Pascal GP106, PCI ID `0x10de:0x1c03` |
| **Connection** | Thunderbolt 3 | ~2.7 GB/s PCIe bandwidth |

## Quick Start

### 1. Install tinygrad (for TinyGPU driver)

```bash
git clone https://github.com/tinygrad/tinygrad.git
cd tinygrad && pip3 install -e . --break-system-packages
```

### 2. Install TinyGPU DriverKit extension

```bash
python3 -c "
from tinygrad.runtime.support.system import APLRemotePCIDevice
APLRemotePCIDevice.ensure_app()
"
```

### 3. Approve TinyGPU in System Settings

**System Settings → Privacy & Security → Allow TinyGPU**

Or: **System Settings → General → Login Items & Extensions → Driver Extensions → Toggle TinyGPU ON**

### 4. Connect eGPU and scan

```bash
python3 transport/pci_scan.py
```

### 5. Read GPU registers

```bash
python3 transport/tinygpu_client.py
```

## Why Pascal is Hard

TinyGPU/tinygrad only supports Ampere+ because those GPUs use **GSP** (GPU System Processor) — a RISC-V core that handles GPU management via RPC. Pascal uses the older **Falcon microcontroller** architecture, which requires:

- Direct MMIO register programming (no GSP RPC)
- ACR secure boot to load NVIDIA-signed FECS/GPCCS firmware
- Manual FIFO channel setup via PFIFO registers
- QMD v01_07 compute dispatch (not v03/v05)

The signed firmware blobs are publicly available in [linux-firmware](https://git.kernel.org/pub/scm/linux/kernel/git/firmware/linux-firmware.git) (`nvidia/gp106/`). The [nouveau](https://nouveau.freedesktop.org/) Linux driver is our primary reference for the init sequence.

See [PLAN.md](PLAN.md) for the full 8-phase implementation plan with register-level detail.

## Supported GPUs

Any NVIDIA Pascal GPU should work (same init sequence, different PCI device IDs):

| GPU | Chip | Device IDs |
|-----|------|-----------|
| GTX 1060 3GB/6GB | GP106 | `0x1c02`, `0x1c03` |
| GTX 1070 / 1070 Ti | GP104 | `0x1b81`, `0x1b82` |
| GTX 1080 / 1080 Ti | GP104 / GP102 | `0x1b80`, `0x1b06` |
| GTX 1050 / 1050 Ti | GP107 | `0x1c82`, `0x1c81` |
| Titan X (Pascal) / Titan Xp | GP102 | `0x1b00`, `0x1b02` |

## Project Structure

```
pascal-egpu/
├── transport/
│   ├── pci_scan.py          # IOKit PCIe device enumeration
│   ├── bar_map.py           # BAR address probing via IOKit properties
│   └── tinygpu_client.py    # TinyGPU socket client for BAR mapping + MMIO
├── hw/                      # GPU hardware abstraction (planned)
│   └── regs/                # Register definitions by engine
├── compute/                 # Compute dispatch (planned)
├── firmware/                # Firmware loader (planned)
│   └── blobs/               # Downloaded firmware (gitignored)
├── tests/
├── PLAN.md                  # Full implementation plan (8 phases)
├── RESEARCH.md              # Deep research: nouveau, envytools, macOS PCIe
├── SETUP.md                 # Detailed setup instructions
└── README.md
```

## References

- [TinyGPU docs](https://docs.tinygrad.org/tinygpu/) — DriverKit eGPU extension
- [tinygrad](https://github.com/tinygrad/tinygrad) — GPU framework with PCIe direct access
- [nouveau](https://nouveau.freedesktop.org/) — Open-source NVIDIA driver (Pascal reference)
- [envytools](https://envytools.readthedocs.io/) — Reverse-engineered NVIDIA hardware docs
- [NVIDIA open-gpu-doc](https://github.com/NVIDIA/open-gpu-doc) — Official class/register documentation
- [linux-firmware nvidia/gp106](https://git.kernel.org/pub/scm/linux/kernel/git/firmware/linux-firmware.git/tree/nvidia/gp106) — Signed Pascal firmware blobs

## Related Projects

- [TurboQuant+](https://github.com/TheTom/turboquant_plus) — KV cache compression for llama.cpp (Metal + CUDA)
- [llama-cpp-turboquant](https://github.com/TheTom/llama-cpp-turboquant) — llama.cpp fork with TurboQuant support
- [turboquant-tinygrad-bridge](https://github.com/CG-8663/turboquant-tinygrad-bridge) — Cross-backend KV cache bridge for split inference

## License

MIT
