# Pascal GTX 1060 (GP106) Bare-Metal Init from macOS Apple Silicon

## Implementation Plan - Driverless GPU Compute over Thunderbolt/PCIe

---

## Table of Contents
1. [Architecture Overview](#1-architecture-overview)
2. [Phase 1: PCIe Enumeration & BAR Mapping](#2-phase-1-pcie-enumeration--bar-mapping)
3. [Phase 2: GPU Bring-Up & Falcon/PMU Init](#3-phase-2-gpu-bring-up--falconpmu-init)
4. [Phase 3: Memory Management (MMU/VM)](#4-phase-3-memory-management-mmuvm)
5. [Phase 4: FIFO Channel Setup](#5-phase-4-fifo-channel-setup)
6. [Phase 5: Compute Dispatch](#6-phase-5-compute-dispatch)
7. [Phase 6: Shader Compilation](#7-phase-6-shader-compilation)
8. [Critical Differences: Pascal vs Turing+](#8-critical-differences-pascal-vs-turing)
9. [Risk Assessment & Blockers](#9-risk-assessment--blockers)
10. [Code Structure](#10-code-structure)
11. [References](#11-references)

---

## 1. Architecture Overview

### What We're Building
A minimal userspace driver that talks directly to an NVIDIA GTX 1060 (GP106, SM 6.1) over Thunderbolt/PCIe from macOS Apple Silicon, bypassing both NVIDIA's proprietary driver and Apple's GPU framework entirely.

### Why Pascal is Different from Tinygrad's Approach
**Tinygrad only supports Turing+ (RTX 30/40/50 series)** because those GPUs use GSP (GPU System Processor) - a RISC-V core that runs a full firmware OS and handles most GPU management. Pascal uses the older **Falcon microcontroller** architecture for PMU/ACR, which requires a completely different initialization sequence.

Key architectural differences:
- **Turing+**: GSP-RM firmware (RISC-V) manages GPU state, channels, memory. Host just sends RPC messages.
- **Pascal**: No GSP. Host must directly program MMIO registers for FIFO, MMU, PGRAPH. Falcon PMU handles power management and secure boot only.

### GP106 Specifications
- PCIe device ID: `0x1c03` (GTX 1060 6GB) or `0x1c02` (GTX 1060 3GB)
- Vendor ID: `0x10de`
- Compute capability: SM 6.1
- CUDA cores: 1280 (6GB) / 1152 (3GB)
- Memory: 6GB/3GB GDDR5, 192-bit bus
- PCIe: Gen 3 x16
- Falcon engines: PMU, FECS (FE Context Switch), GPCCS (GPC Context Switch)

---

## 2. Phase 1: PCIe Enumeration & BAR Mapping

### 2.1 macOS PCIe Access Strategy

There are three viable approaches on macOS Apple Silicon:

#### Option A: TinyGPU App + USB4 Dock (Recommended for prototyping)
Tinygrad's `APLRemotePCIDevice` class shows the path:
1. Use an ADT-UT3G USB4 dock (ASMedia ASM2464PD controller)
2. The TinyGPU.app acts as a privileged DriverKit system extension
3. Communicates over a Unix domain socket to userspace
4. Handles BAR mapping and DMA translation

```
APLRemotePCIDevice -> Unix socket -> TinyGPU.app (DriverKit DEXT)
                                          |
                                    PCIDriverKit IOPCIDevice
                                          |
                                    USB4/TB -> eGPU dock -> GTX 1060
```

#### Option B: Custom DriverKit System Extension
Write a minimal PCIDriverKit driver that:
1. Matches on `IOPCIDevice` with vendor=0x10de
2. Maps BARs via `IOPCIDevice::_CopyDeviceMemoryWithIndex()`
3. Exposes mapped memory to userspace via shared memory

#### Option C: Custom IOKit Kext (requires SIP disabled)
Use classic IOKit with `IOPCIDevice::getDeviceMemoryWithIndex()`:
```c
// Kext approach (SIP must be disabled)
IOMemoryMap* bar0Map = pciDevice->getDeviceMemoryWithIndex(0)->map();
volatile uint32_t* mmio = (uint32_t*)bar0Map->getVirtualAddress();
```

**For this plan, we'll use Option A (TinyGPU infrastructure) as the transport layer, but implement our own GPU init on top.**

### 2.2 PCIe Device Discovery

From tinygrad's `system.py`, macOS device scanning uses IOKit:

```python
# Enumerate IOPCIDevice services
iokit.IOServiceGetMatchingServices(0, iokit.IOServiceMatching(b"IOPCIDevice"), &iterator)
while svc := iokit.IOIteratorNext(iterator):
    vendor_id = read_prop(svc, "vendor-id")  # Look for 0x10de
    device_id = read_prop(svc, "device-id")  # Look for 0x1c03 (GTX 1060 6GB)
```

Pascal device IDs to match:
```
GP106: 0x1c02, 0x1c03, 0x1c04, 0x1c06, 0x1c07, 0x1c09
```

### 2.3 BAR Layout (Pascal GP106)

Pascal exposes 3 PCIe BARs:

| BAR | Type | Default Size | Purpose |
|-----|------|-------------|---------|
| BAR0 | 32-bit MMIO | 16 MB | GPU registers (MMIO space) |
| BAR1 | 64-bit prefetchable | 256 MB (resizable to VRAM size) | VRAM aperture (through VM) |
| BAR2/3 | 64-bit non-prefetchable | 32 MB | RAMIN / control structures (through VM) |

**BAR mapping code:**
```python
# Map BAR0 (MMIO registers) - 16MB of register space
mmio = pci_dev.map_bar(0, fmt='I')  # 32-bit word access

# Map BAR1 (VRAM aperture) - needs VM setup first
vram = pci_dev.map_bar(1)

# Map BAR2/3 (RAMIN) - kernel control structures
ramin = pci_dev.map_bar(2)  # BAR2 on PCIe = BAR3 concept in docs
```

### 2.4 PCI Config Space Setup

Before accessing BARs, enable bus mastering:
```python
cmd = pci_dev.read_config(PCI_COMMAND, 2)
pci_dev.write_config(PCI_COMMAND, cmd | PCI_COMMAND_MASTER | PCI_COMMAND_MEMORY, 2)
```

For the USB4 dock path, tinygrad's `pci_setup_usb_bars()` handles bridge configuration and BAR resizing automatically.

---

## 3. Phase 2: GPU Bring-Up & Falcon/PMU Init

### 3.1 MMIO Register Map (BAR0)

All registers accessed as 32-bit words at BAR0 + offset:

| Offset | Engine | Purpose |
|--------|--------|---------|
| `0x000000` | PMC | Master control, GPU ID, engine enable, interrupts |
| `0x001000` | PBUS | Bus control, debug registers |
| `0x002000` | PFIFO | DMA FIFO command submission |
| `0x009000` | PTIMER | Time measurement, alarms |
| `0x00a000` | PCOUNTER | Performance counters |
| `0x00e000` | PNVIO | GPIO, I2C, PWM |
| `0x088000` | PPCI | PCI config access window |
| `0x100000` | PFB | Memory interface, VM/MMU control |
| `0x101000` | PSTRAPS | Strap readout (board config) |
| `0x110000` | PGSP/Falcon | Falcon microcontroller (GSP on Turing+, FWSEC on Pascal) |
| `0x10a000` | PMU Falcon | Power Management Unit falcon |
| `0x400000` | PGRAPH | Graphics/compute engine |
| `0x610000` | PDISPLAY | Display engine (not needed for compute) |
| `0x700000` | PMEM | Indirect VRAM access |
| `0x800000` | PFIFO (BAR0) | FIFO BAR0 map |
| `0x840000` | SEC2 Falcon | Security engine 2 |

### 3.2 Initial GPU State Check

```python
def rreg(addr): return mmio[addr // 4]
def wreg(addr, val): mmio[addr // 4] = val

# Step 1: Read GPU ID
boot_0 = rreg(0x000000)  # NV_PMC_BOOT_0
# For GP106: should read 0x13X0X0XX where bits identify Pascal arch
chip_id = (boot_0 >> 20) & 0x1ff  # Architecture + implementation

# Step 2: Check if GPU needs reset
# On Pascal, check if any engines are in bad state
pmc_enable = rreg(0x000200)  # NV_PMC_ENABLE
```

### 3.3 Pascal Secure Boot / ACR Sequence

**This is the hardest part of the entire project.**

Pascal GPUs have hardware-enforced secure boot. Starting with Maxwell GM20x, NVIDIA locked the Falcon FECS and GPCCS engines behind cryptographic verification. You CANNOT run compute shaders without properly authenticated firmware on these engines.

#### The Secure Boot Chain:

```
1. FWSEC (HS Falcon on SEC2/PMU)
   - Loaded from VBIOS
   - Runs in Heavy Secure (HS) mode
   - Establishes Write-Protected Region (WPR) in VRAM
   - Verifies and loads ACR (Authenticated Code Resolver)

2. ACR (runs on PMU/SEC2 in HS mode)
   - Verifies signatures of LS (Light Secure) firmware
   - Loads FECS firmware (required for graphics/compute)
   - Loads GPCCS firmware (required for GPC scheduling)

3. FECS + GPCCS (run in LS mode)
   - Handle context switching for PGRAPH
   - Required for ANY compute or graphics work
```

#### Required Firmware Files (from linux-firmware):

```
nvidia/gp106/acr/bl.bin          - ACR bootloader
nvidia/gp106/acr/ucode_load.bin  - ACR load ucode (HS)
nvidia/gp106/acr/ucode_unload.bin - ACR unload ucode
nvidia/gp106/gr/fecs_bl.bin      - FECS bootloader
nvidia/gp106/gr/fecs_data.bin    - FECS data segment
nvidia/gp106/gr/fecs_inst.bin    - FECS instruction segment
nvidia/gp106/gr/fecs_sig.bin     - FECS signature (NVIDIA-signed)
nvidia/gp106/gr/gpccs_bl.bin     - GPCCS bootloader
nvidia/gp106/gr/gpccs_data.bin   - GPCCS data segment
nvidia/gp106/gr/gpccs_inst.bin   - GPCCS instruction segment
nvidia/gp106/gr/gpccs_sig.bin    - GPCCS signature (NVIDIA-signed)
nvidia/gp106/gr/sw_bundle_init.bin
nvidia/gp106/gr/sw_ctx.bin
nvidia/gp106/gr/sw_method_init.bin
nvidia/gp106/gr/sw_nonctx.bin
```

#### ACR Init Sequence (from nouveau gm200 secboot):

```python
# Step 1: Allocate instance block for Falcon DMA
inst_block = alloc_vram(0x1000, align=0x1000)
page_dir = alloc_vram(0x8000, align=0x8000)

# Configure instance block (offsets in instance block memory):
#   +0x200: page directory address (lower 32 bits)
#   +0x204: page directory address (upper 8 bits)
#   +0x208: VM limit (lower)
#   +0x20C: VM limit (upper)
write_vram(inst_block + 0x200, page_dir_addr & 0xffffffff)
write_vram(inst_block + 0x204, (page_dir_addr >> 32) & 0xff)

# Step 2: Set up Falcon execution context
FALCON_BASE = 0x10a000  # PMU Falcon (or 0x840000 for SEC2)

# Reset Falcon
wreg(FALCON_BASE + 0x10c, 0xffffffff)  # FALCON_IRQMCLR - clear interrupts
wreg(FALCON_BASE + 0x048, 0x10)        # Reset falcon

# Wait for reset
while rreg(FALCON_BASE + 0x048) & 0x10: pass

# Bind instance block
wreg(FALCON_BASE + 0x480, inst_block >> 12)  # FALCON_IMEMC - set inst block

# Step 3: Load HS bootloader into Falcon IMEM
# The HS bootloader is loaded via DMA from system memory
# Set mailbox to error sentinel
wreg(FALCON_BASE + 0x040, 0xdeada5a5)  # FALCON_MAILBOX0

# Load ACR ucode via Falcon DMA...
# (DMA the bl.bin + ucode_load.bin into Falcon IMEM/DMEM)

# Step 4: Start Falcon
wreg(FALCON_BASE + 0x104, acr_start_addr)  # FALCON_BOOTVEC
wreg(FALCON_BASE + 0x100, 0x2)             # FALCON_CPUCTL - start

# Step 5: Wait for completion (poll for HALT)
timeout = time.time() + 0.1  # 100ms timeout
while time.time() < timeout:
    if rreg(FALCON_BASE + 0x100) & 0x10:  # CPUCTL.HALTED
        break

# Step 6: Verify success
mailbox = rreg(FALCON_BASE + 0x040)
assert mailbox != 0xdeada5a5, "ACR boot failed"
```

#### WPR (Write Protected Region) Verification:
After FWSEC runs, verify WPR is established:
```python
wpr2_hi = rreg(0x100CE4)  # NV_PFB_PRI_MMU_WPR2_ADDR_HI
assert wpr2_hi != 0, "WPR2 not initialized - FWSEC failed"
```

### 3.4 PGRAPH Initialization

After ACR loads FECS/GPCCS firmware:

```python
# Enable PGRAPH engine
pmc_enable = rreg(0x000200)
wreg(0x000200, pmc_enable | (1 << 12))  # Enable GR engine

# Wait for PGRAPH to come up
while not (rreg(0x400700) & 0x1): pass  # GR status

# Load golden context (sw_ctx.bin, sw_bundle_init.bin, etc.)
# These configure the default GR state
```

### 3.5 Simplified Path: Skip Secure Boot?

**Bad news**: There is NO way to skip secure boot on Pascal for compute. NVIDIA's hardware physically prevents PGRAPH from executing shaders without authenticated FECS/GPCCS firmware. This is a hard silicon-level enforcement.

However, the signed firmware blobs ARE publicly available in linux-firmware. So the path is:
1. Download the signed firmware from linux-firmware git
2. Use the ACR mechanism to load them (the firmware is signed by NVIDIA, the ACR verifies the signature in hardware)
3. Once FECS/GPCCS are running, PGRAPH accepts compute work

---

## 4. Phase 3: Memory Management (MMU/VM)

### 4.1 Pascal MMU Architecture

Pascal uses a **2-level or multi-level page table** with these characteristics:
- Page sizes: 4KB (small) and 64KB or 128KB (big)
- Virtual address space: up to 48 bits
- Physical address: 40 bits (1 TB)
- Instance block contains page directory base

From the Turing dev_ram reference (Pascal is similar but uses MMU v2):

```
Instance Block (4096 bytes, 4K-aligned):
  +0x000 .. +0x1FF: RAMFC (host/FIFO state, 512 bytes)
  +0x200 (128*32 bits): Page Directory Base Config
    [1:0]   PAGE_DIR_BASE_TARGET  (0=VID_MEM, 2=SYS_MEM_COHERENT)
    [2]     PAGE_DIR_BASE_VOL
    [10]    USE_VER2_PT_FORMAT (set to TRUE for Pascal)
    [11]    BIG_PAGE_SIZE (0=128KB, 1=64KB -- use 64KB)
    [31:12] PAGE_DIR_BASE_LO (address >> 12)
  +0x204 (129*32):
    [31:0]  PAGE_DIR_BASE_HI
```

### 4.2 Page Table Setup

```python
# GP100 MMU format (from open-gpu-doc/pascal/gp100-mmu-format.pdf):
#
# Level 0: PDE3 - covers bits [47:38] -> 256 GB per entry
# Level 1: PDE2 - covers bits [37:29] -> 512 MB per entry
# Level 2: PDE1 - covers bits [28:21] -> 2 MB per entry (big page PTE here)
# Level 3: PDE0 - dual PDE (64k/4k) at bits [20:16]/[20:12]
# Level 4: PTE  - 4KB or 64KB pages

# For a minimal setup, we can use a flat mapping:
# Allocate a single PDE3 -> PDE2 -> PDE1 chain
# Map first N MB of VRAM linearly

def setup_page_tables(vram_base_phys, vram_size):
    # Allocate page directory levels
    pde3 = alloc_vram(0x1000, align=0x1000)  # 512 entries, 8 bytes each
    pde2 = alloc_vram(0x1000, align=0x1000)
    pde1 = alloc_vram(0x1000, align=0x1000)

    # PDE entry format (8 bytes):
    # [0]    is_pte (0 = PDE, 1 = PTE/leaf)
    # [2:1]  target aperture (0=VID_MEM, 1=SYS_MEM_COH, 2=SYS_MEM_NONCOH)
    # [31:12] address[31:12]
    # [39:32] address[39:32]

    # Point PDE3[0] -> PDE2
    write_vram_u64(pde3, (pde2 >> 12) << 12 | (0 << 1) | 0)  # VID_MEM, not PTE

    # Point PDE2[0] -> PDE1
    write_vram_u64(pde2, (pde1 >> 12) << 12 | (0 << 1) | 0)

    # PDE1 entries point to PTEs (2MB each, or use big pages)
    # For 64KB big pages in PDE1:
    for i in range(vram_size // (2 << 20)):
        phys = vram_base_phys + i * (2 << 20)
        # Big page PTE: [0]=valid, [4]=vol, [31:12]=addr[31:12], [39:32]=addr[39:32]
        pte = (phys >> 12) << 12 | 1  # valid
        write_vram_u64(pde1 + i * 8, pte)

    return pde3
```

### 4.3 Instance Block Configuration

```python
def setup_instance_block(page_dir_addr):
    inst = alloc_vram(0x1000, align=0x1000)

    # RAMFC portion (first 512 bytes) - will be filled during channel setup

    # Page directory base (offset 0x200 = word 128)
    pd_lo = (page_dir_addr >> 12) << 12  # address bits [31:12]
    pd_lo |= (1 << 10)   # USE_VER2_PT_FORMAT = TRUE
    pd_lo |= (1 << 11)   # BIG_PAGE_SIZE = 64KB
    pd_lo |= (0 << 0)    # TARGET = VID_MEM

    pd_hi = (page_dir_addr >> 32) & 0xffffffff

    write_vram_u32(inst + 0x200, pd_lo)
    write_vram_u32(inst + 0x204, pd_hi)

    return inst
```

---

## 5. Phase 4: FIFO Channel Setup

### 5.1 Channel Architecture (Pascal)

Pascal uses the **MAXWELL_CHANNEL_GPFIFO_A** (class `0xB06F`) channel type:

```
User-visible control structure (mapped at USERD):
  +0x040: Put      (write pointer - CPU writes)
  +0x044: Get      (read pointer - GPU advances, read-only)
  +0x048: Reference
  +0x04C: PutHi
  +0x058: TopLevelGet
  +0x060: GetHi
  +0x088: GPGet    (GPFIFO get pointer, read-only)
  +0x08C: GPPut    (GPFIFO put pointer - CPU writes)
```

### 5.2 GPFIFO Setup

```python
# GPFIFO is a circular buffer of GP entries (8 bytes each)
GPFIFO_ENTRIES = 128  # Must be power of 2
GPFIFO_SIZE = GPFIFO_ENTRIES * 8

gpfifo_mem = alloc_vram(GPFIFO_SIZE, align=0x1000)
userd_mem = alloc_vram(0x200, align=0x200)  # 512 bytes, RAMUSERD

# RAMFC fields (in instance block, first 512 bytes):
# These mirror the PBDMA register layout
# Offset 0x00: GP_PUT (initialized to 0)
# Offset 0x14: GP_GET (initialized to 0)
# Offset 0x18: PB_GET (pushbuffer get)
# Offset 0x48: GP_BASE (GPFIFO base address)
# Offset 0x4c: GP_BASE_HI (upper address + limit)
# Offset 0x54: GP_FETCH
# Offset 0x84: PB_HEADER
# etc.

def setup_channel(inst_block, gpfifo_addr, userd_addr):
    # Write GPFIFO base into RAMFC
    write_vram_u32(inst_block + 0x48, gpfifo_addr & 0xffffffff)
    gp_base_hi = ((gpfifo_addr >> 32) & 0xff) | ((GPFIFO_ENTRIES - 1) << 16)
    write_vram_u32(inst_block + 0x4c, gp_base_hi)

    # Write USERD address
    write_vram_u32(inst_block + 0x08, userd_addr & 0xffffffff)  # USERD offset in RAMFC
    write_vram_u32(inst_block + 0x0c, (userd_addr >> 32) & 0xff)
```

### 5.3 Channel Registration with PFIFO

```python
# On Pascal, channels are registered via PFIFO MMIO registers
# (unlike Turing+ where GSP-RM handles this via RPC)

PFIFO_BASE = 0x002000

# Enable PFIFO engine
pmc_enable = rreg(0x000200)
wreg(0x000200, pmc_enable | (1 << 8))  # Enable PFIFO

# Configure channel (runlist-based scheduling on GK104+)
# Each channel belongs to a runlist, and runlists map to engines

# Runlist setup:
# 1. Allocate runlist memory
# 2. Write channel entries into runlist
# 3. Submit runlist to hardware

RUNLIST_BASE = 0x002270  # NV_PFIFO_RUNLIST_BASE
RUNLIST_SUBMIT = 0x002274

runlist_mem = alloc_vram(0x1000, align=0x1000)

# Runlist entry format (8 bytes):
# [11:0]  channel ID
# [13:12] type (0=channel, 1=TSG)
# [14]    enable
channel_id = 0
runlist_entry = channel_id | (0 << 12) | (1 << 14)  # type=channel, enabled
write_vram_u64(runlist_mem, runlist_entry)

# Submit runlist
wreg(RUNLIST_BASE, (runlist_mem >> 12))
wreg(RUNLIST_SUBMIT, (1 << 20) | 1)  # length=1, trigger submit

# Bind channel to engine
# On Pascal, GR engine is typically runlist 0, engine 0
# Channel binding happens through the runlist
```

### 5.4 Pushbuffer Format

GP entries point to pushbuffer segments. Pushbuffer data uses this encoding:

```python
# GP Entry format (8 bytes):
# Word 0: [31:2] = address[39:10] (dword-aligned), [0] = fetch mode
# Word 1: [7:0] = address[47:40], [30:10] = length, [9] = level, [31] = sync

def make_gp_entry(pb_addr, length_dwords, sync=False):
    word0 = (pb_addr >> 2) & 0xfffffffc  # address bits [39:10] shifted
    word1 = ((pb_addr >> 40) & 0xff)
    word1 |= (length_dwords << 10)
    word1 |= (0 << 9)  # MAIN level
    word1 |= (int(sync) << 31)
    return struct.pack('<II', word0, word1)

# Pushbuffer method encoding (increasing method):
# [28:29] = 0b01 (increasing), [12:0] = method>>2, [28:16] = count, [15:13] = subchannel
def pb_mthd(subchannel, method, count):
    return (1 << 29) | (count << 16) | (subchannel << 13) | (method >> 2)

def pb_data(value):
    return value
```

---

## 6. Phase 5: Compute Dispatch

### 6.1 Compute Class Setup

Pascal uses `MAXWELL_COMPUTE_B` (class `0xB1C0`) or `MAXWELL_COMPUTE_A` (class `0xB0C0`).

```python
# First, bind the compute class to a subchannel via pushbuffer:
# Method 0x0000 = SET_OBJECT
pb = []
pb.append(pb_mthd(subchannel=1, method=0x0000, count=1))  # SET_OBJECT on subchannel 1
pb.append(0xB1C0)  # MAXWELL_COMPUTE_B class ID
```

### 6.2 Compute Launch via QMD

Pascal uses **QMD (Queue Meta Data)** version 01_07 for compute dispatch. The QMD is a 256-byte (2048-bit) structure in GPU memory.

```python
# QMD V01_07 key fields (bit positions as MW(hi:lo)):
#
# PROGRAM_OFFSET:           MW(287:256)    - shader code offset
# CTA_RASTER_WIDTH:         MW(415:384)    - grid X dimension
# CTA_RASTER_HEIGHT:        MW(431:416)    - grid Y dimension
# CTA_RASTER_DEPTH:         MW(447:432)    - grid Z dimension
# SHARED_MEMORY_SIZE:       MW(561:544)    - shared mem per block
# QMD_VERSION:              MW(579:576)    - set to 7 (V01_07)
# QMD_MAJOR_VERSION:        MW(583:580)    - set to 1
# CTA_THREAD_DIMENSION0:    MW(607:592)    - block X (threads)
# CTA_THREAD_DIMENSION1:    MW(623:608)    - block Y
# CTA_THREAD_DIMENSION2:    MW(639:624)    - block Z
# CONSTANT_BUFFER_VALID(i): MW(640+i)      - enable CB[i]
# L1_CONFIGURATION:         MW(671:669)    - L1/shared split
# CONSTANT_BUFFER_ADDR(i):  MW(959+i*64 : 928+i*64) + upper
# CONSTANT_BUFFER_SIZE(i):  MW(991+i*64 : 975+i*64)
# SHADER_LOCAL_MEMORY_LOW:  MW(1463:1440)
# BARRIER_COUNT:            MW(1471:1467)
# SHADER_LOCAL_MEMORY_HIGH: MW(1495:1472)
# REGISTER_COUNT:           MW(1503:1496)  - GPRs per thread
# SASS_VERSION:             MW(1535:1528)  - SM version (0x61 for GP106)

def build_qmd_v01_07(program_offset, grid_dims, block_dims,
                      shared_mem_size, register_count, cb_addr, cb_size):
    qmd = bytearray(256)  # 2048 bits = 256 bytes

    def set_field(qmd, hi, lo, value):
        """Set a multi-word field in the QMD."""
        for bit in range(lo, hi + 1):
            byte_idx = bit // 8
            bit_idx = bit % 8
            if value & (1 << (bit - lo)):
                qmd[byte_idx] |= (1 << bit_idx)

    # QMD version
    set_field(qmd, 579, 576, 7)    # QMD_VERSION = 7
    set_field(qmd, 583, 580, 1)    # QMD_MAJOR_VERSION = 1

    # Program offset (byte offset from code address)
    set_field(qmd, 287, 256, program_offset)

    # Grid dimensions
    set_field(qmd, 415, 384, grid_dims[0])   # CTA_RASTER_WIDTH
    set_field(qmd, 431, 416, grid_dims[1])   # CTA_RASTER_HEIGHT
    set_field(qmd, 447, 432, grid_dims[2])   # CTA_RASTER_DEPTH

    # Block (CTA) dimensions
    set_field(qmd, 607, 592, block_dims[0])  # CTA_THREAD_DIMENSION0
    set_field(qmd, 623, 608, block_dims[1])  # CTA_THREAD_DIMENSION1
    set_field(qmd, 639, 624, block_dims[2])  # CTA_THREAD_DIMENSION2

    # Shared memory
    set_field(qmd, 561, 544, shared_mem_size)

    # Register count (per thread)
    set_field(qmd, 1503, 1496, register_count)

    # SASS version (SM 6.1 for GP106)
    set_field(qmd, 1535, 1528, 0x61)

    # Barrier count (at least 1 for __syncthreads)
    set_field(qmd, 1471, 1467, 1)

    # L1 configuration (48KB shared preferred)
    set_field(qmd, 671, 669, 3)  # 48KB

    # Constant buffer 0 (kernel arguments)
    set_field(qmd, 640, 640, 1)  # CONSTANT_BUFFER_VALID[0] = TRUE
    set_field(qmd, 959, 928, cb_addr & 0xffffffff)       # CB0 addr lower
    set_field(qmd, 967, 960, (cb_addr >> 32) & 0xff)     # CB0 addr upper
    set_field(qmd, 991, 975, cb_size)                     # CB0 size

    # Cache invalidation (do it on first launch)
    set_field(qmd, 254, 254, 1)  # INVALIDATE_INSTRUCTION_CACHE
    set_field(qmd, 253, 253, 1)  # INVALIDATE_SHADER_DATA_CACHE
    set_field(qmd, 255, 255, 1)  # INVALIDATE_SHADER_CONSTANT_CACHE

    return bytes(qmd)
```

### 6.3 Compute Dispatch Pushbuffer Sequence

```python
def dispatch_compute(subchannel, code_addr, qmd_addr):
    pb = []

    # Set shader code address
    # NVB0C0_SET_SHADER_LOCAL_MEMORY_A = 0x0790 (high 32 bits)
    # NVB0C0_SET_SHADER_LOCAL_MEMORY_B = 0x0794 (low 32 bits)

    # Set code address (where compiled shader binary lives in GPU VA)
    # Method 0x1608: CODE_ADDRESS_HIGH
    # Method 0x160C: CODE_ADDRESS_LOW
    pb.append(pb_mthd(subchannel, 0x1608, 2))
    pb.append((code_addr >> 32) & 0xffffffff)
    pb.append(code_addr & 0xffffffff)

    # Point to QMD and launch
    # Method 0x02B4: LAUNCH_DESC_ADDRESS (QMD address >> 8)
    pb.append(pb_mthd(subchannel, 0x02B4, 1))
    pb.append(qmd_addr >> 8)

    # Method 0x02BC: LAUNCH - triggers execution
    pb.append(pb_mthd(subchannel, 0x02BC, 1))
    pb.append(0)  # value doesn't matter, writing triggers launch

    return pb
```

### 6.4 Semaphore-Based Completion Notification

```python
# Use host semaphore to know when compute is done
# SEMAPHOREA (0x0010): address upper
# SEMAPHOREB (0x0014): address lower
# SEMAPHOREC (0x0018): payload
# SEMAPHORED (0x001C): operation

def signal_completion(subchannel, sem_addr, sem_value):
    pb = []
    pb.append(pb_mthd(subchannel, 0x0010, 4))
    pb.append((sem_addr >> 32) & 0xff)
    pb.append(sem_addr & 0xfffffffc)
    pb.append(sem_value)
    pb.append(0x2 | (1 << 24))  # RELEASE, 4-byte
    return pb
```

---

## 7. Phase 6: Shader Compilation

### 7.1 Compiling PTX to SASS for SM 6.1

You need SM 6.1 SASS (Streaming ASSembler) binary code. Options:

#### Option A: Use nvcc/ptxas (easiest, requires CUDA toolkit)
```bash
# Write PTX for vector add
cat > vecadd.ptx << 'EOF'
.version 5.0
.target sm_61
.address_size 64

.visible .entry vecadd(
    .param .u64 a_ptr,
    .param .u64 b_ptr,
    .param .u64 c_ptr,
    .param .u32 n
) {
    .reg .u32 %tid, %n;
    .reg .u64 %a, %b, %c, %off;
    .reg .f32 %fa, %fb, %fc;
    .reg .pred %p;

    mov.u32 %tid, %tid.x;
    ld.param.u32 %n, [n];
    setp.ge.u32 %p, %tid, %n;
    @%p bra done;

    cvt.u64.u32 %off, %tid;
    shl.b64 %off, %off, 2;        // offset = tid * 4 (sizeof float)

    ld.param.u64 %a, [a_ptr];
    ld.param.u64 %b, [b_ptr];
    ld.param.u64 %c, [c_ptr];

    add.u64 %a, %a, %off;
    add.u64 %b, %b, %off;
    add.u64 %c, %c, %off;

    ld.global.f32 %fa, [%a];
    ld.global.f32 %fb, [%b];
    add.f32 %fc, %fa, %fb;
    st.global.f32 [%c], %fc;

done:
    ret;
}
EOF

# Compile to cubin
ptxas -arch=sm_61 -o vecadd.cubin vecadd.ptx
```

#### Option B: Use CuAssembler (Python, assembles SASS directly)
```bash
pip install CuAssembler
# Supports SM 6.1 Pascal SASS instruction set
```

#### Option C: Use LLVM NVPTX backend
```bash
# LLVM can compile to PTX, but you still need ptxas for PTX->SASS
clang -target nvptx64 -S -O2 vecadd.cl -o vecadd.ptx
```

#### Option D: Hand-write SASS (hardcore)
Using envytools' `envydis` for reference:
```bash
# Disassemble existing cubin to study SM 6.1 encoding
envydis -m sm61 -i vecadd.cubin
```

### 7.2 Shader Binary Format

The compiled cubin contains:
- ELF header
- `.nv.info` section: kernel metadata
- `.nv.shared` section: shared memory declarations
- `.text.vecadd` section: SASS binary code

For bare-metal dispatch, extract the `.text` section and upload to GPU VRAM at the code address referenced in QMD.

### 7.3 Constant Buffer for Kernel Arguments

Kernel parameters are passed via constant buffer 0 (c[0][...]):
```python
# For vecadd(a_ptr, b_ptr, c_ptr, n):
# CB0 layout:
#   [0x00]: a_ptr (u64)
#   [0x08]: b_ptr (u64)
#   [0x10]: c_ptr (u64)
#   [0x18]: n (u32)

cb_data = struct.pack('<QQQi', a_gpu_addr, b_gpu_addr, c_gpu_addr, n)
```

---

## 8. Critical Differences: Pascal vs Turing+

| Aspect | Pascal (GP106) | Turing+ (tinygrad) |
|--------|---------------|-------------------|
| Management CPU | Falcon (PMU) - simple microcontroller | GSP (RISC-V) - runs full firmware OS |
| Init approach | Direct MMIO register programming | RPC messages to GSP-RM firmware |
| Secure boot | ACR on PMU/SEC2 falcon, loads FECS/GPCCS | FWSEC -> GSP boots everything |
| Channel setup | Direct PFIFO register writes | GSP-RM API: `fifo.chan.alloc` |
| MMU version | v2 (48-bit VA) | v2 or v3 (Hopper: 56-bit VA) |
| Compute class | MAXWELL_COMPUTE_B (0xB1C0) | AMPERE_COMPUTE_A (0xC6C0) or later |
| QMD version | V01_07 | V02_x or V03_x |
| GPFIFO class | MAXWELL_CHANNEL_GPFIFO_A (0xB06F) | AMPERE_CHANNEL_GPFIFO_A (0xC46F)+ |
| BAR1 resize | Limited (256MB default) | Large BAR support standard |
| Doorbell | MMIO write to USERD GPPut | Doorbell register mechanism |

---

## 9. Risk Assessment & Blockers

### CRITICAL BLOCKERS

1. **Secure Boot Firmware Loading (HIGH RISK)**
   - The ACR/Falcon boot sequence is the most complex and least documented part
   - Must correctly load NVIDIA-signed firmware blobs in the right order
   - A single register wrong = Falcon hangs, no compute possible
   - Nouveau's implementation spans thousands of lines across many files
   - **Mitigation**: Start by porting nouveau's `gm200_secboot.c` and `acr_r352.c` line-by-line

2. **macOS PCIe Access (MEDIUM RISK)**
   - Apple Silicon's DART IOMMU complicates DMA
   - BAR mapping from userspace requires either DriverKit DEXT or disabled SIP
   - Physical address translation for DMA descriptors is non-trivial
   - **Mitigation**: Leverage TinyGPU.app infrastructure (already solves this)

3. **PGRAPH Context Initialization (MEDIUM RISK)**
   - After FECS/GPCCS are loaded, GR engine needs golden context setup
   - Requires loading `sw_ctx.bin`, `sw_bundle_init.bin`, `sw_method_init.bin`
   - Wrong context = PGRAPH exceptions, compute hangs
   - **Mitigation**: Study nouveau's `gf100_gr.c` golden context loading

4. **PTX/SASS Compilation (LOW RISK)**
   - Need ptxas from CUDA toolkit (can run on Linux, cross-compile)
   - Or use CuAssembler for direct SASS assembly
   - The compiled binary format is well-documented

### NICE-TO-HAVE CONCERNS

5. **Performance** - Running at minimum clocks (no PMU reclocking on nouveau)
6. **Stability** - No error recovery without full driver
7. **Memory** - BAR1 only 256MB, may need careful VRAM mapping

---

## 10. Code Structure

```
pascal-gpu/
├── transport/
│   ├── pci_device.py          # PCIe device abstraction (BAR mapping, config space)
│   ├── macos_iokit.py         # IOKit service matching and memory mapping
│   └── usb4_dock.py           # ADT-UT3G USB4 dock communication
├── hw/
│   ├── mmio.py                # MMIO register read/write helpers
│   ├── falcon.py              # Falcon microcontroller interface
│   ├── secboot.py             # ACR/secure boot sequence
│   ├── mmu.py                 # Page table setup, instance blocks
│   ├── fifo.py                # PFIFO channel management, GPFIFO, runlists
│   ├── pgraph.py              # PGRAPH init, golden context, compute class
│   └── regs/
│       ├── pmc.py             # PMC register definitions (0x000000)
│       ├── pbus.py            # PBUS register definitions (0x001000)
│       ├── pfifo.py           # PFIFO register definitions (0x002000)
│       ├── pfb.py             # PFB/MMU register definitions (0x100000)
│       └── pgraph.py          # PGRAPH register definitions (0x400000)
├── compute/
│   ├── qmd.py                 # QMD (Queue Meta Data) builder for V01_07
│   ├── pushbuf.py             # Pushbuffer / command stream builder
│   ├── shader.py              # Shader binary loader (cubin parser)
│   └── dispatch.py            # High-level compute dispatch
├── firmware/
│   ├── loader.py              # Firmware blob loader from linux-firmware
│   └── blobs/                 # Downloaded firmware files (gitignored)
│       ├── acr/
│       ├── gr/
│       └── pmu/
├── tests/
│   ├── test_pci_enum.py       # Test: can we see the GPU?
│   ├── test_mmio.py           # Test: can we read PMC_BOOT_0?
│   ├── test_secboot.py        # Test: can we load ACR and FECS?
│   ├── test_channel.py        # Test: can we create a FIFO channel?
│   └── test_vecadd.py         # Test: vector add end-to-end
└── main.py                    # Full pipeline: init -> dispatch -> readback
```

### Implementation Order

1. **Week 1**: PCIe enumeration + BAR0 mapping + read PMC_BOOT_0
2. **Week 2**: Falcon interface + FWSEC VBIOS extraction + WPR setup
3. **Week 3**: ACR boot + FECS/GPCCS firmware load
4. **Week 4**: MMU page tables + instance block + BAR1 VRAM mapping
5. **Week 5**: FIFO channel + GPFIFO + pushbuffer framework
6. **Week 6**: PGRAPH init + compute class binding + QMD
7. **Week 7**: Shader compilation + vector add test
8. **Week 8**: Debug, stabilize, optimize

---

## 11. References

### Primary Sources (code)
- **tinygrad NV runtime**: `tinygrad/tinygrad/runtime/support/nv/nvdev.py` - GSP-based init for Turing+
- **tinygrad system.py**: `tinygrad/tinygrad/runtime/support/system.py` - macOS PCIe/IOKit/USB4 transport
- **nouveau driver**: `drivers/gpu/drm/nouveau/nvkm/` in Linux kernel - Pascal support
- **nouveau secboot**: `nvkm/subdev/secboot/gm200.c` + `acr_r352.c` - ACR/Falcon boot
- **nouveau FIFO**: `nvkm/engine/fifo/gk104.c` - GPFIFO channel management
- **nouveau GR**: `nvkm/engine/gr/gf100.c` - PGRAPH initialization
- **NVIDIA open-gpu-doc**: `classes/compute/clb0c0.h`, `clb0c0qmd.h`, `classes/host/clb06f.h`
- **NVIDIA open-gpu-doc**: `pascal/gp100-mmu-format.pdf` - MMU page table format
- **NVIDIA dev_pbdma.ref.txt**: GPFIFO and pushbuffer register reference (Volta, similar to Pascal)
- **NVIDIA dev_ram.ref.txt**: Instance block / RAMFC / RAMUSERD layout

### Documentation
- **envytools**: https://envytools.readthedocs.io - Reverse-engineered NVIDIA hardware docs
  - MMIO register map: `/hw/mmio.html`
  - BAR layout: `/hw/bus/bars.html`
  - PBUS: `/hw/bus/pbus.html`
  - FIFO: `/hw/fifo/intro.html`
- **Linux kernel Falcon docs**: https://docs.kernel.org/gpu/nova/core/falcon.html
- **NVIDIA open-gpu-kernel-modules**: https://github.com/NVIDIA/open-gpu-kernel-modules

### Firmware
- **linux-firmware**: `nvidia/gp106/` directory contains all required signed firmware blobs
- Download from: https://git.kernel.org/pub/scm/linux/kernel/git/firmware/linux-firmware.git

### Tools
- **CuAssembler**: https://github.com/cloudcores/CuAssembler - Direct SASS assembly for SM 6.1
- **envytools**: https://github.com/envytools/envytools - GPU register database and disassembler
- **ptxas**: Part of CUDA Toolkit, compiles PTX to SASS

---

## Appendix A: Minimal "Hello World" Test Sequence

Before attempting compute, verify basic GPU access:

```python
#!/usr/bin/env python3
"""Minimal GP106 GPU identification test."""

# Step 1: Map BAR0
mmio = map_bar0()  # Returns mmap'd 16MB MMIO space

# Step 2: Read GPU ID
boot_0 = mmio[0x000000 // 4]  # NV_PMC_BOOT_0
print(f"PMC_BOOT_0: {boot_0:#010x}")
# Expected for GP106: 0x134XX0a1 or similar
# Bits [23:20] = 3 means GK1xx/GM1xx/GP1xx family
# Bits [27:24] = architecture (13 = Pascal)

# Step 3: Read more identification
boot_42 = mmio[0x000042 // 4]  # Not standard, but...
pmc_id = mmio[0x000000 // 4]

# Step 4: Check PTIMER (always works, even without init)
ptimer_lo = mmio[0x009400 // 4]  # PTIMER_TIME_0
ptimer_hi = mmio[0x009410 // 4]  # PTIMER_TIME_1
print(f"GPU timer: {(ptimer_hi << 32) | ptimer_lo} ns")

# If timer is ticking, basic MMIO access works!
```

## Appendix B: Pascal Falcon Register Quick Reference

Falcon base addresses on GP106:
```
PMU:   0x10a000
FECS:  0x409000 (within PGRAPH)
GPCCS: 0x41a000 (within PGRAPH)
SEC2:  0x840000
```

Common Falcon registers (offset from base):
```
+0x000: FALCON_IRQSSET    - Set interrupt
+0x004: FALCON_IRQSCLR    - Clear interrupt
+0x008: FALCON_IRQSTAT     - Interrupt status
+0x010: FALCON_IRQMSET    - Set interrupt mask
+0x014: FALCON_IRQMCLR    - Clear interrupt mask
+0x040: FALCON_MAILBOX0   - Mailbox register 0
+0x044: FALCON_MAILBOX1   - Mailbox register 1
+0x048: FALCON_IDLESTATE  - Idle state / reset
+0x050: FALCON_OS          - OS indicator
+0x060: FALCON_ENGCTL     - Engine control
+0x100: FALCON_CPUCTL      - CPU control (start/halt)
+0x104: FALCON_BOOTVEC     - Boot vector (entry point)
+0x108: FALCON_HWCFG       - Hardware config
+0x10C: FALCON_DMACTL      - DMA control
+0x110: FALCON_DMATRFBASE  - DMA transfer base
+0x114: FALCON_DMATRFMOFFS - DMA transfer offset
+0x118: FALCON_DMATRFCMD   - DMA transfer command
+0x11C: FALCON_DMATRFFBOFFS- DMA transfer FB offset
+0x180: FALCON_IMEMCTL     - IMEM access control
+0x184: FALCON_IMEMLO      - IMEM data (low)
+0x188: FALCON_IMEMHI      - IMEM data (high)
+0x1C0: FALCON_DMEMCTL     - DMEM access control
+0x1C4: FALCON_DMEMDATA    - DMEM data port
```
