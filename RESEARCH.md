# Pascal GPU (GP106/GTX 1060) Bare-Metal Driver Research

## TL;DR: The Hard Truth

**Pascal REQUIRES signed firmware** for compute (GR engine). You CANNOT init the graphics/compute engine without NVIDIA-signed firmware blobs loaded through the ACR (Authenticated Code Radix) secure boot chain. This is the #1 blocker.

However, there IS a viable path: nouveau already does this on Linux using the signed firmware from `linux-firmware`. The firmware blobs are freely distributable. The question is whether you can replicate nouveau's init sequence from macOS userspace.

**TinyGPU (tinygrad) has already solved the macOS PCIe access problem** for Ampere+ GPUs. Their approach (DriverKit extension + userspace Python driver) is the exact template to follow, though Pascal is older than their minimum supported gen.

---

## 1. Nouveau Driver: Pascal Init Sequence

### GP106 Device Configuration (from `drivers/gpu/drm/nouveau/nvkm/engine/device/base.c`)

GP106 uses these implementations:
```
.acr   = gp102_acr_new     // Secure boot (ACR) - loads signed firmware
.bar   = gm107_bar_new     // BAR management
.fb    = gp102_fb_new      // Framebuffer/VRAM controller
.pmu   = gp102_pmu_new     // Power Management Unit falcon
.ce    = gp102_ce_new      // Copy Engines (4 instances, mask 0x0f)
.fifo  = gp100_fifo_new    // FIFO/channel management (GPFIFO)
.gr    = gp104_gr_new      // Graphics/Compute engine
.nvdec = gm107_nvdec_new   // Video decoder
.nvenc = gm107_nvenc_new   // Video encoder (1 instance)
.sec2  = gp102_sec2_new    // Security processor 2
```

### Boot Sequence (what nouveau does)

1. **Read PMC_BOOT_0** (register `0x000000`): Identifies GPU chip
2. **DEVINIT scripts**: Execute VBIOS init scripts (register writes, delays, conditions)
3. **Enable PCI bus mastering**: Set `PCI_COMMAND_MASTER` bit
4. **Map BAR0** (MMIO, 16MB), **BAR1** (VRAM aperture), **BAR2** (RAMIN)
5. **FB init**: Configure framebuffer, memory controller
6. **MMU init**: Set up page tables (Pascal uses v2 MMU format)
7. **ACR secure boot**: Load signed firmware through the trust chain
8. **FIFO init**: Set up GPFIFO channels for command submission
9. **GR init**: Initialize graphics/compute engine with firmware

### Key MMIO Registers (BAR0 layout, GF100+/Pascal)

| Address Range | Name | Function |
|---|---|---|
| `0x000000-0x000FFF` | PMC | Master control, GPU ID, interrupt control |
| `0x001000-0x001FFF` | PBUS | Bus control |
| `0x002000-0x003FFF` | PFIFO | FIFO engine control |
| `0x009000-0x009FFF` | PMASTER | Master interrupt dispatch |
| `0x00E000-0x00EFFF` | PNVIO | GPIO, I2C, PWM |
| `0x010000-0x01FFFF` | PMC timers | |
| `0x020000-0x020FFF` | PTIMER | Timer/clock |
| `0x060000-0x06FFFF` | PCOPY | Copy engine |
| `0x100000-0x1FFFFF` | PFB | Framebuffer / memory controller |
| `0x10A000-0x10AFFF` | PPMU (PDAEMON) | Power management falcon |
| `0x400000-0x41FFFF` | PGRAPH | Graphics/compute engine |
| `0x610000-0x61FFFF` | PDISP | Display engine |

### GR (Graphics/Compute) Init Registers

From `gp100.c` and `gp102.c`:

```c
// Shader exception handling
TPC_UNIT(t, m, 0x644) = 0x00dffffe  // shader exceptions
TPC_UNIT(t, m, 0x64c) = 0x00000105

// FECS exceptions
0x409c24 = 0x000e0002

// ZBC (Zero Bandwidth Clear) color registers
0x418010, 0x41804c, 0x418088, 0x4180c4  // color clear
0x418110, 0x41814c                       // depth clear
0x41815c, 0x418198                       // stencil clear (GP102+)

// ROP active FBPs
0x12006c  // read FBP count
0x408850  // ZROP config
0x408958  // CROP config

// init_419c9c
0x419c9c  // masked with 0x00010000, 0x00020000
```

### GR Class Definitions

GP100: `PASCAL_A` (0xC097), `PASCAL_COMPUTE_A` (0xC0C0)
GP102/GP104/GP106: `PASCAL_B` (0xC197), `PASCAL_COMPUTE_B` (0xC1C0)

**GP106 uses PASCAL_COMPUTE_B (0xC1C0)** for compute operations.

---

## 2. Firmware Requirements (THE BLOCKER)

### Required Firmware Files for GP106

Located in `linux-firmware` at `nvidia/gp106/`:

**ACR (Secure Boot Chain):**
- `acr/bl.bin` - ACR bootloader
- `acr/ucode_load.bin` - ACR load microcode
- `acr/ucode_unload.bin` - ACR unload microcode
- `acr/unload_bl.bin` - ACR unload bootloader

**GR (Graphics/Compute Engine):**
- `gr/fecs_bl.bin` - FECS (Front End Controller) bootloader
- `gr/fecs_data.bin` - FECS data segment
- `gr/fecs_inst.bin` - FECS instruction segment
- `gr/fecs_sig.bin` - FECS signature (RSA-3K)
- `gr/gpccs_bl.bin` - GPCCS (GPC Controller) bootloader
- `gr/gpccs_data.bin` - GPCCS data segment
- `gr/gpccs_inst.bin` - GPCCS instruction segment
- `gr/gpccs_sig.bin` - GPCCS signature
- `gr/sw_bundle_init.bin` - Software bundle init
- `gr/sw_ctx.bin` - Software context

**SEC2 (Security Engine 2):**
- `sec2/sig.bin` - SEC2 signature

### Secure Boot Chain (ACR)

The boot sequence is:
```
Hardware (Falcon Boot ROM)
  -> HS (Heavy Secure) ACR ucode runs on PMU/SEC2
    -> Validates LS (Light Secure) firmware signatures
      -> Loads FECS firmware onto GR engine falcon
      -> Loads GPCCS firmware onto GPC falcons
        -> GR engine is now ready for compute
```

**Key ACR details from `gp102_acr`:**
- WPR (Write-Protected Region) headers map falcon IDs to firmware offsets
- LSB (Load and Signature Block) headers contain signatures + bootloader data
- Memory layout: WPR start/end addresses shifted right 8 bits
- Read mask: 0xf, Write mask: 0xc, Client mask: 0x2

### PMU Falcon Registers (GP102)

```c
// PMU falcon function table
.debug = 0xc08
.cmdq  = { 0x4a0, 0x4b0, 4 }   // command queue registers
.msgq  = { 0x4c8, 0x4cc, 0 }   // message queue registers
```

PMU uses `gm200_pmu_nofw` - meaning **nouveau runs PMU WITHOUT proprietary firmware**. The PMU falcon is enabled/disabled via `gm200_flcn_enable`/`gm200_flcn_disable`, and reset via `gp102_flcn_reset_eng`. This means **basic PMU operation does not require proprietary PMU firmware**, but reclocking does.

### What Works WITHOUT Firmware

- Basic GPU detection (PMC_BOOT_0)
- VRAM access through BAR1
- Display output (basic modesetting)
- **GPU runs at boot clocks only** (no reclocking without PMU firmware)
- No compute, no acceleration

### What REQUIRES Signed Firmware

- GR engine (compute/graphics) - **requires FECS + GPCCS signed firmware**
- Reclocking to higher frequencies - requires PMU firmware (not available)
- SEC2 operations

---

## 3. Command Submission: GPFIFO / Pushbuffer

### Pascal Channel GPFIFO (class 0xC06F)

From `clc06f.h` - the channel control structure:

```c
typedef volatile struct {
    NvU32 Ignored00[0x010];     //                     0x0000-0x003f
    NvU32 Put;                  // put offset, r/w     0x0040-0x0043
    NvU32 Get;                  // get offset, r/o     0x0044-0x0047
    NvU32 Reference;            // ref value, r/o      0x0048-0x004b
    NvU32 PutHi;                // high put bits       0x004c-0x004f
    NvU32 Ignored01[0x002];     //                     0x0050-0x0057
    NvU32 TopLevelGet;          // top level get, r/o  0x0058-0x005b
    NvU32 TopLevelGetHi;        // high top get bits   0x005c-0x005f
    NvU32 GetHi;                // high get bits       0x0060-0x0063
    NvU32 Ignored02[0x007];     //                     0x0064-0x007f
    NvU32 Ignored03;            //                     0x0080-0x0083
    NvU32 Ignored04[0x001];     //                     0x0084-0x0087
    NvU32 GPGet;                // GP FIFO get, r/o    0x0088-0x008b
    NvU32 GPPut;                // GP FIFO put         0x008c-0x008f
} Nvc06fControl;
```

### GPFIFO Entry Format (8 bytes)

From `dev_pbdma.ref.txt` (Volta docs, similar for Pascal):

**GP_ENTRY0** (lower 32 bits):
- Bits [31:2]: GET - 30-bit dword address (left-shift 2 for byte address)
- Bit [0]: FETCH control (0=unconditional, 1=conditional)

**GP_ENTRY1** (upper 32 bits):
- Bits [30:10]: LENGTH - number of pushbuffer entries
- Bits [7:0]: GET_HI - upper address bits
- Bit [9]: LEVEL (0=MAIN, 1=SUBROUTINE)
- Bit [31]: SYNC (0=PROCEED, 1=WAIT)
- If LENGTH=0: control entry (OPCODE in bits [7:0])

**Full virtual address**: `(GP_ENTRY1_GET_HI << 32) + (GP_ENTRY0_GET << 2)`

### Pushbuffer Method Format

Each pushbuffer entry is 32 bits:
```
Bits [31:29]: Type (2 = non-incrementing, 1 = incrementing)
Bits [28:16]: Count (number of data words following)
Bits [15:13]: Subchannel (0-7)
Bits [12:2]:  Method offset (>> 2)
```

### How tinygrad Submits Commands (from `ops_nv.py`)

```python
# Method submission helper
def nvm(subchannel, mthd, *args, typ=2):
    q((typ << 28) | (len(args) << 16) | (subchannel << 13) | (mthd >> 2), *args)

# GPFIFO submission - write entry to ring buffer, poke doorbell
gpfifo.ring[put_value % entries_count] = (cmdq_addr//4 << 2) | (len(q) << 42) | (1 << 41)
gpfifo.gpput[0] = (put_value + 1) % entries_count
memory_barrier()
gpu_mmio[0x90 // 4] = gpfifo.token  # doorbell register at BAR0+0x90
```

### Compute Dispatch Flow

1. Set compute class: `nvm(1, SET_OBJECT, PASCAL_COMPUTE_B)`
2. Set shader memory windows: `SET_SHADER_LOCAL_MEMORY_WINDOW_A/B`, `SET_SHADER_SHARED_MEMORY_WINDOW_A/B`
3. Set shader local memory: `SET_SHADER_LOCAL_MEMORY_A/B`
4. Set local memory per TPC: `SET_SHADER_LOCAL_MEMORY_NON_THROTTLED_A/B/C`
5. Build QMD (Queue Meta Data) structure
6. Dispatch: `SEND_PCAS_A` (QMD address >> 8) + `SEND_SIGNALING_PCAS2_B` (9)
7. Signal completion: Semaphore release via channel methods

---

## 4. Compute Dispatch: QMD Structure

### Pascal QMD Version 01_07 (from `clc0c0qmd.h`)

The QMD is a 64-dword (256-byte) structure. Key fields:

| Field | Bits | Description |
|---|---|---|
| `QMD_VERSION` | MW(579:576) | Must be 7 for Pascal |
| `QMD_MAJOR_VERSION` | MW(583:580) | Must be 1 for Pascal |
| `PROGRAM_OFFSET` | MW(287:256) | Shader code offset |
| `CTA_RASTER_WIDTH` | MW(415:384) | Grid dimension X |
| `CTA_RASTER_HEIGHT` | MW(431:416) | Grid dimension Y |
| `CTA_RASTER_DEPTH` | MW(447:432) | Grid dimension Z |
| `CTA_THREAD_DIMENSION0` | MW(607:592) | Block dimension X |
| `CTA_THREAD_DIMENSION1` | MW(623:608) | Block dimension Y |
| `CTA_THREAD_DIMENSION2` | MW(639:624) | Block dimension Z |
| `SHARED_MEMORY_SIZE` | MW(561:544) | Shared memory per block |
| `REGISTER_COUNT` | MW(1503:1496) | Registers per thread |
| `BARRIER_COUNT` | MW(1471:1467) | Number of barriers |
| `SHADER_LOCAL_MEMORY_LOW_SIZE` | MW(1463:1440) | Per-thread local mem |
| `SHADER_LOCAL_MEMORY_HIGH_SIZE` | MW(1495:1472) | High local memory |
| `CONSTANT_BUFFER_VALID(i)` | MW(640+i) | CB valid flags (8 slots) |
| `CONSTANT_BUFFER_ADDR_LOWER(i)` | MW(959+i*64:928+i*64) | CB addresses |
| `CONSTANT_BUFFER_ADDR_UPPER(i)` | MW(967+i*64:960+i*64) | CB addr upper |
| `CONSTANT_BUFFER_SIZE(i)` | MW(991+i*64:975+i*64) | CB sizes |
| `RELEASE0_ADDRESS_LOWER` | MW(767:736) | Semaphore release addr |
| `RELEASE0_ENABLE` | implied by SEMAPHORE_RELEASE_ENABLE0 | |
| `RELEASE0_PAYLOAD` | MW(831:800) | Semaphore payload value |
| `FP32_NAN_BEHAVIOR` | MW(376:376) | NaN handling |
| `SAMPLER_INDEX` | MW(382:382) | Sampler indexing mode |
| `SM_GLOBAL_CACHING_ENABLE` | MW(198:198) | L1 caching |

MW(X:Y) notation: bit X is the MSB, bit Y is the LSB. Word index = bit_number // 32.

---

## 5. Pascal Compute Class Methods (0xC0C0 / 0xC1C0)

Key methods for compute dispatch:

```
0x0000  SET_OBJECT                  // Select compute class
0x0100  NO_OPERATION
0x0110  WAIT_FOR_IDLE
0x01B0  LAUNCH_DMA                  // DMA transfer with semaphore
0x0214  SET_SHADER_SHARED_MEMORY_WINDOW
0x021C  INVALIDATE_SHADER_CACHES
0x0298  INVALIDATE_SKED_CACHES
0x02A0  SET_SHADER_SHARED_MEMORY_WINDOW_A (upper 17 bits)
0x02A4  SET_SHADER_SHARED_MEMORY_WINDOW_B (lower 32 bits)
0x02B4  SEND_PCAS_A                 // QMD address >> 8 (dispatch!)
0x02B8  SEND_PCAS_B                 // FROM/DELTA fields
0x02BC  SEND_SIGNALING_PCAS_B       // Invalidate + Schedule
0x02E4  SET_SHADER_LOCAL_MEMORY_NON_THROTTLED_A/B/C
0x02F0  SET_SHADER_LOCAL_MEMORY_THROTTLED_A/B/C
0x0310  SET_SPA_VERSION             // SM architecture version
0x077C  SET_SHADER_LOCAL_MEMORY_WINDOW
0x0790  SET_SHADER_LOCAL_MEMORY_A   // Address upper
0x0794  SET_SHADER_LOCAL_MEMORY_B   // Address lower
0x07B0  SET_SHADER_LOCAL_MEMORY_WINDOW_A
0x07B4  SET_SHADER_LOCAL_MEMORY_WINDOW_B
0x120C  INVALIDATE_SAMPLER_CACHE_ALL
0x1210  INVALIDATE_TEXTURE_HEADER_CACHE_ALL
0x1574  SET_TEX_HEADER_POOL_A/B/C
0x1608  SET_PROGRAM_REGION_A        // Shader code base address upper
0x160C  SET_PROGRAM_REGION_B        // Shader code base address lower
0x1698  INVALIDATE_SHADER_CACHES_NO_WFI
0x1B00  SET_REPORT_SEMAPHORE_A/B/C/D
0x2608  SET_BINDLESS_TEXTURE
0x260C  SET_TRAP_HANDLER
```

---

## 6. macOS PCIe Access: How TinyGPU Does It

### Architecture

TinyGPU uses a **three-layer** approach:
1. **TinyGPU.app** - A macOS app with an Apple-signed DriverKit extension
2. **Unix socket server** - TinyGPU runs as a server process
3. **Python userspace driver** (tinygrad) - Connects via socket, sends RPC commands

### APLRemotePCIDevice (from `system.py`)

```python
class APLRemotePCIDevice(RemotePCIDevice):
    APP_PATH = "/Applications/TinyGPU.app/Contents/MacOS/TinyGPU"

    def __init__(self, devpref, pcibus):
        # Downloads and installs TinyGPU.app if needed
        self.ensure_app()
        # Connects to TinyGPU server via Unix socket
        sock_path = temp("tinygpu.sock")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        subprocess.Popen([self.APP_PATH, "server", sock_path])
        sock.connect(sock_path)
        super().__init__(devpref, "usb4", sock=sock)
```

### PCI Device Scanning on macOS (from `system.py`)

```python
# Uses IOKit to enumerate PCI devices
iokit.IOServiceGetMatchingServices(0,
    iokit.IOServiceMatching(b"IOPCIDevice"),
    ctypes.byref(iterator))
while svc := iokit.IOIteratorNext(iterator):
    vendor_id = read_prop(svc, "vendor-id")
    device_id = read_prop(svc, "device-id")
```

### BAR Mapping via DriverKit

The DriverKit extension provides:
- `MAP_BAR` - Maps a PCI BAR to a shared memory region
- `MAP_SYSMEM_FD` - Allocates system memory, returns physical addresses via shared FD
- `CFG_READ`/`CFG_WRITE` - PCI config space access
- `MMIO_READ`/`MMIO_WRITE` - Register access
- `RESET` - Device reset

The RPC protocol (from `RemoteCmd` enum):
```python
class RemoteCmd(enum.IntEnum):
    PROBE, MAP_BAR, MAP_SYSMEM_FD, CFG_READ, CFG_WRITE,
    RESET, MMIO_READ, MMIO_WRITE, MAP_SYSMEM,
    SYSMEM_READ, SYSMEM_WRITE, RESIZE_BAR, PING = range(13)
```

### USB/Thunderbolt Path

TinyGPU also supports USB path via ASM2464PD controller:
```python
class USBPCIDevice(PCIDevice):
    # Sets up PCI bridges, resizes BARs, configures memory windows
    # All done through USB PCI config space requests
    bars = System.pci_setup_usb_bars(usb, gpu_bus=4,
                                      mem_base=0x10000000,
                                      pref_mem_base=(32 << 30))
```

This sets up PCI bridge buses, configures memory/prefetchable memory base addresses, resizes BAR0, and enables bus mastering - all through USB-tunneled PCI config space transactions.

### Key Limitation for Pascal

TinyGPU requires **Ampere+ (SM 8.0+)** because it relies on the GSP (GPU System Processor) firmware model used in Turing and newer. Pascal doesn't have GSP. You'd need to replicate nouveau's init sequence instead.

---

## 7. Pascal MMU (Page Tables)

### MMU Version 2 (Pascal)

Pascal uses MMU v2 with a multi-level page table:

| Depth | HW Level | VA Bits |
|---|---|---|
| 1 | PDE3 | 47:38 |
| 2 | PDE2 | 37:29 |
| 3 | PDE1 (or 512M PTE) | 28:21 |
| 4 | PDE0 (dual 64K/4K PDE, or 2M PTE) | 20:16/20:12 |
| 5 | PTE_64K / PTE_4K | small pages |

### From tinygrad's NVDev (MMU v2 for non-Hopper):

```python
# Pascal MMU setup
bits = 48
shifts = [12, 21, 29, 38, 47]  # page sizes: 4K, 2M, 512M, ...

# PTE format (v2)
pte.encode(valid=1, address_sys=paddr >> 12,
           aperture=2 if SYS else 0,  # 2=system memory, 0=video memory
           kind=6, vol=uncached)

# PDE format (v2)
pde.encode(is_pte=False, aperture=1 if valid else 0,
           address_sys=paddr >> 12, no_ats=1)

# TLB invalidation
NV_VIRTUAL_FUNCTION_PRIV_MMU_INVALIDATE =
    (1 << 0) | (1 << 1) | (1 << 6) | (1 << 31)
```

---

## 8. PTX Compilation for SM 6.1

### ptxas (NVIDIA's PTX assembler)

`ptxas` compiles PTX intermediate code to SASS (native GPU assembly).

```bash
ptxas --gpu-name sm_61 --verbose kernel.ptx -o kernel.cubin
```

### Getting ptxas Without Full CUDA

```bash
# Via conda (minimal install)
conda install -c nvidia cuda-nvcc

# Or download CUDA toolkit and extract just ptxas
# ptxas binary is standalone, ~50MB
```

### Alternative: Use tinygrad's NAKRenderer

Tinygrad has a `NAKRenderer` that can compile directly to SASS using Mesa's NAK compiler backend (Rust-based). This doesn't need ptxas at all but currently targets Turing+.

### Alternative: Compiler Explorer

Compiler Explorer (godbolt.org) supports PTX to SASS compilation online for quick testing.

---

## 9. Practical Implementation Path

### Phase 1: Access the GPU from macOS

**Option A: Fork TinyGPU (recommended)**
- TinyGPU already has Apple-signed DriverKit extension
- Has all the PCIe BAR mapping infrastructure
- Would need to add Pascal-specific init (not just GSP boot)

**Option B: Write custom DriverKit extension**
- Use `IOPCIDevice` to match NVIDIA vendor ID 0x10DE
- Map BAR0 (`mapDeviceMemoryWithRegister`)
- Map BAR1 for VRAM access
- Expose via IOKit user client or shared memory

**Option C: macUSPCIO kext (requires SIP disabled)**
- Quick prototype path using `IOPCIDevice` from IOKit
- Exposes PCI I/O space to userspace
- Not production-ready, SIP must be off

### Phase 2: Basic GPU Init

1. Read `BAR0[0x0]` (PMC_BOOT_0) to confirm GP106
2. Enable PCI bus mastering
3. Execute DEVINIT scripts from VBIOS (or hardcode known-good register sequence)
4. Init FB/memory controller
5. Set up page tables (MMU v2)

### Phase 3: Secure Boot + Firmware Load

1. Load ACR firmware blobs from `linux-firmware`
2. Set up WPR (Write-Protected Region) in VRAM
3. Load HS (Heavy Secure) ACR ucode onto SEC2 or PMU falcon
4. ACR validates and loads LS firmware (FECS, GPCCS) onto GR engine
5. GR engine is now ready

### Phase 4: Channel Setup + Compute

1. Allocate GPFIFO ring buffer in VRAM
2. Create channel instance (PASCAL_CHANNEL_GPFIFO_A = 0xC06F)
3. Set up runlist entry
4. Submit compute methods via pushbuffer:
   - `SET_OBJECT` with PASCAL_COMPUTE_B (0xC1C0)
   - Configure shader memory regions
   - Build QMD structure
   - `SEND_PCAS_A` + `SEND_SIGNALING_PCAS_B` to dispatch
5. Wait for completion via semaphore

### Phase 5: Run Compute Kernels

1. Compile PTX to SASS cubin (ptxas --gpu-name sm_61)
2. Upload cubin to GPU memory
3. Set up constant buffers with kernel arguments
4. Build QMD with grid/block dimensions, register count, etc.
5. Dispatch via GPFIFO

---

## 10. Key Resources & Links

### Source Code
- **Nouveau (Linux kernel)**: `drivers/gpu/drm/nouveau/` in https://github.com/torvalds/linux
  - GP106 device def: `nvkm/engine/device/base.c`
  - GR init: `nvkm/engine/gr/gp100.c`, `gp102.c`
  - FIFO: `nvkm/engine/fifo/gp100.c`
  - ACR: `nvkm/subdev/acr/gp102.c`
  - PMU: `nvkm/subdev/pmu/gp102.c`
- **tinygrad**: https://github.com/tinygrad/tinygrad
  - NV runtime: `tinygrad/runtime/ops_nv.py`
  - PCIe/macOS access: `tinygrad/runtime/support/system.py`
  - NV device init: `tinygrad/runtime/support/nv/nvdev.py`
- **NVIDIA open-gpu-kernel-modules**: https://github.com/NVIDIA/open-gpu-kernel-modules
  - Pascal compute class: `src/common/sdk/nvidia/inc/class/clc0c0.h`
  - Pascal compute B: `src/common/sdk/nvidia/inc/class/clc1c0.h`
  - GPFIFO channel: `src/common/sdk/nvidia/inc/class/clc06f.h`
- **NVIDIA open-gpu-doc**: https://github.com/NVIDIA/open-gpu-doc
  - QMD headers: `classes/compute/clc0c0qmd.h`, `clc1c0qmd.h`
  - Pascal MMU: `pascal/gp100-mmu-format.pdf`

### Documentation
- **envytools**: https://envytools.readthedocs.io/en/latest/
  - [MMIO register map](https://envytools.readthedocs.io/en/latest/hw/mmio.html)
  - [PCI BARs](https://envytools.readthedocs.io/en/latest/hw/bus/bars.html)
  - [PMC registers](https://github.com/envytools/envytools/blob/master/docs/hw/bus/pmc.rst)
- **NVIDIA DEVINIT opcodes**: https://nvidia.github.io/open-gpu-doc/Devinit/devinit.xml
- **Falcon documentation**: https://docs.kernel.org/gpu/nova/core/falcon.html
- **FWSEC documentation**: https://docs.kernel.org/gpu/nova/core/fwsec.html
- **PBDMA reference (Volta, similar for Pascal)**: https://nvidia.github.io/open-gpu-doc/manuals/volta/gv100/dev_pbdma.ref.txt

### Firmware
- **linux-firmware GP106**: https://github.com/wkennington/linux-firmware/tree/master/nvidia/gp106
- **Arch Linux firmware package file list**: https://archlinux.org/packages/core/any/linux-firmware-nvidia/files/

### macOS PCIe Access
- **TinyGPU**: https://docs.tinygrad.org/tinygpu/
- **macUSPCIO**: https://github.com/ShadyNawara/macUSPCIO
- **IOPCIDevice API**: https://developer.apple.com/documentation/pcidriverkit/iopcidevice

### Tools
- **envytools** (register database, disassemblers): https://github.com/envytools/envytools
  - `nvapeek` / `nvapoke` - MMIO register read/write
  - `nvalist` - List GPU devices
  - `nvafakebios` - Upload modified VBIOS
  - `nvdis` - Firmware disassembler
  - `demmio` - Decode MMIO traces
- **NVIDIA GPU low-level guide**: https://gist.github.com/karolherbst/4341e3c33b85640eaaa56ff69a094713

---

## 11. Risk Assessment

| Risk | Severity | Mitigation |
|---|---|---|
| Signed firmware requirement for GR engine | **HIGH** | Must load NVIDIA-signed blobs from linux-firmware |
| Pascal not supported by TinyGPU | **MEDIUM** | Fork TinyGPU, add nouveau-style init instead of GSP |
| DEVINIT scripts vary per VBIOS version | **MEDIUM** | Extract from specific card's VBIOS, or hardcode for known board |
| PMU firmware not available (no reclocking) | **LOW** | GPU will run at boot clocks (~139 MHz core). Slow but functional. |
| macOS DriverKit signing requirements | **MEDIUM** | TinyGPU already has Apple approval; fork it or get your own |
| Thunderbolt PCIe bandwidth (40 Gbps) | **LOW** | Acceptable for compute, not ideal for large data transfers |
| GPU may not enumerate on Apple Silicon TB | **MEDIUM** | TinyGPU proves it works; Pascal should enumerate same as Ampere |

---

## 12. Estimated Effort

Assuming you use TinyGPU's DriverKit extension for PCIe access:

1. **PCIe access from macOS**: Already solved by TinyGPU. Fork it. (1-2 days)
2. **Basic GPU detection**: Read PMC_BOOT_0 through BAR0. (hours)
3. **VBIOS extraction + DEVINIT**: Parse VBIOS, execute init scripts. (1-2 weeks)
4. **FB/MMU init**: Set up page tables, VRAM management. (1-2 weeks)
5. **ACR secure boot**: Port nouveau's ACR code to userspace. (2-4 weeks, hardest part)
6. **FIFO/channel setup**: Allocate GPFIFO, create channels. (1 week)
7. **Compute dispatch**: QMD + pushbuffer submission. (1 week)
8. **PTX compilation pipeline**: ptxas integration. (days)

**Total: ~2-3 months for a minimal compute pipeline.**

The hardest part by far is the ACR secure boot chain. This is 4000+ lines of kernel code in nouveau dealing with falcon microcontrollers, WPR regions, and cryptographic verification. Getting this right is the difference between "GPU detected" and "GPU runs compute kernels."
