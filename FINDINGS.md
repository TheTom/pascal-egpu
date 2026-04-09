# Pascal eGPU on macOS Apple Silicon — Findings

## TL;DR

Pascal GPUs over Thunderbolt eGPU on Apple Silicon cannot be bootstrapped to run compute via pure host-side register writes. The NVIDIA hardware security architecture (PMU-based DEVINIT + Falcon boot ROM signature checks + privilege level masks) creates a circular dependency that requires either a working VBIOS POST path (which doesn't exist on Apple Silicon over Thunderbolt) or an Ampere+ GPU with GSP (which has a host-bootstrappable firmware entry point).

This document captures the full investigation.

## What Works

| Capability | Notes |
|---|---|
| PCIe enumeration | IOKit `IOPCIDevice` matching on vendor 0x10de |
| BAR0 MMIO mapping | Via TinyGPU DriverKit extension (Ampere+ restriction is only in tinygrad userspace, not the DEXT) |
| GPU identification | `PMC_BOOT_0 = 0x136000a1` reads reliably (Pascal GP106 rev A1) |
| PTIMER reads | Timer ticking at ~nanosecond resolution |
| PMC_ENABLE writes | Can toggle engine enable bits |
| PBUS_BAR0_WINDOW writes | PRAMIN window base control |
| Priv ring (priv ring master at 0x009080) | Readable and partially writable |
| FECS Falcon access | IMEM/DMEM fully writable in clean state, verified with test patterns AND real firmware |
| FECS CPUCTL writes | Can set STARTCPU and ALIAS_EN bits |
| VBIOS ROM extraction | 128KB dumped from BAR0 + 0x300000 (PROM space) |
| BIT table parsing | Pascal BIT header at 0x1e2, 16 entries, init_script_tbl_ptr at 0x497e |
| Devinit script discovery | Script 0 at 0x8523 calls 6 INIT_SUB_DIRECT subroutines |
| PMU DEVINIT blob location | BIT I +0x14/0x16 (tables) and +0x18/0x1a (boot scripts) |

## What Doesn't Work

| Symptom | Return value | Cause |
|---|---|---|
| PMC scratch writes | `0xbad00200` | PMC scratch domain clock-gated |
| PMU Falcon writes (in some states) | `0xbad00200` / `0xbad0da00` / `0xdead5ec1` | PMU in HS mode, writes gated by security |
| VRAM via BAR1 or PRAMIN | `0xbad0acXX` (incrementing counter) | FBP priv ring stations not registered |
| SEC2 Falcon reads | `0xffffffff` | SEC2 engine powered off |
| GPCCS Falcon reads | `0xffffffff` | GPC priv ring stations not registered |
| FECS firmware execution | CPUCTL = 0x52 with STARTCPU stuck set | Falcon rejects unsigned code (UCODE_LEVEL = 3) |
| Secure IMEM write mode (bit 28) | Bit ignored | Not exposed at current priv level |
| PCIe FLR | Not supported | GTX 1060 dev_cap bit 28 = 0 |
| TinyGPU soft reset | No effect on WPR2_HI | Soft reset, not hardware SBR |
| Cold boot with eGPU connected | No effect | Apple Silicon EFI does not run GPU Option ROM |
| D3/D0 power state cycle | No effect | Returns to same state |
| Nouveau GP10b priv ring init writes | Broke state further | Wrong chip, made things worse |
| Raw RING_CMD writes | Fully collapsed priv ring | Cumulative side effects, hardware lockup |

## Diagnostic Register Values

Captured from a working session before the priv ring collapse:

```
PMC_BOOT_0:      0x136000a1   (GP106 rev A1)
PMC_ENABLE:      0x40003120   (initial), can write up to 0x5c6cf1e1
PMC scratch:     0xbad00200   (clock gated)
PFB_CFG0:        0xbadf1100   (FB controller not initialized)
WPR2_HI:         0xccccfcfc   (sticky, does not clear on reset)
PBUS_PRI_TIMEOUT_SAVE_0: 0xbad00200  (captures priv ring errors)

FECS Falcon (clean state after PGRAPH PMC reset):
  CPUCTL:   0x00000010   (HALTED)
  SCTL:     0x00003000   (UCODE_LEVEL = 3, LS mode)
  HWCFG:    0x20202060   (IMEM 24KB, DMEM 4KB)
  HWCFG2:   0x00084145   (SEC_MODE field = 4)
  CPUCTL_PRIV_LEVEL_MASK: 0x00000000 (writable, became 0xffffffff)

PMU Falcon (clean state):
  CPUCTL:   0x00000000   (full reset, but accessible)
  HWCFG:    0x400e0100   (IMEM 64KB, DMEM 64KB)
  DMACTL:   0x00000080   (REQUIRE_CTX bit set, write-locked)
  SCTL:     0x00003002   (HSMODE + UCODE_LEVEL=3)

PPRIV priv ring stations:
  PPRIV_SYS_DECODE_CFG   0x1200a8: 0x00000001 (alive)
  PPRIV_GPC0_MASTER_CMD  0x128280: 0xffffffff (DEAD — no GPC)
  PPRIV_FBP0_MASTER_CMD  0x12a270: 0xffffffff (DEAD — no FBP)
```

## The Fundamental Blocker

On a PC cold boot, the system BIOS/UEFI runs the GPU's VBIOS Option ROM which:

1. Configures PLLs (clock tree setup)
2. Enables power rails for PMU, SEC2, FBP, GPC
3. Runs DRAM training (memory controller init)
4. Registers FBP and GPC priv ring stations
5. Loads initial firmware onto PMU/SEC2 Falcons in HS mode
6. Sets up Privilege Level Masks (PLMs) for the running state

On Apple Silicon over Thunderbolt eGPU, the Option ROM is never executed. The GPU comes up in a minimal state with only the PMC, PBUS, and PGRAPH/FECS priv ring stations alive. FBP and GPC stations are unregistered. PMU is powered but in deep reset. Without the Option ROM's register writes (or the PMU DEVINIT firmware running), those stations never come up.

### The Circular Dependency

```
Compute needs PGRAPH + PFIFO + VRAM
  PGRAPH needs FECS (running signed ucode) + GPCCS (station dead)
  VRAM needs FBP stations registered (dead)

FECS needs signed code verified by ACR
  ACR needs PMU running in HS mode
  PMU needs DEVINIT firmware loaded

PMU DEVINIT firmware needs DMA from VRAM
  VRAM is dead

VRAM needs FBP stations registered
  FBP stations get registered by PMU DEVINIT
  PMU DEVINIT needs VRAM to load from
```

The only known cut-through is the VBIOS Option ROM running on the CPU during system POST. It pokes registers via the priv ring to bring up FBP stations and DRAM, then hands off to PMU DEVINIT for the rest. We don't have this path.

## Paths We Tried

### VBIOS extraction and parsing ✅
Dumped 128KB from BAR0 + 0x300000. Parsed BIT table, found init script table at 0x497e pointing to script 0 at 0x8523 which consists of 6 INIT_SUB_DIRECT calls. Found BIT I +0x14/+0x16 and +0x18/+0x1a which point to the PMU DEVINIT tables and boot scripts inside the VBIOS.

### FECS Falcon bootstrap ❌
FECS is writable in LS mode. Loaded `fecs_inst.bin` (20927 bytes) into IMEM with proper block tagging, verified byte-for-byte readback. Set CPUCTL = 0x42 (ALIAS_EN | STARTCPU). STARTCPU bit persists in read-back (should self-clear on successful start), indicating the Falcon refuses to execute. Verified the same behavior with trivial halt instructions and with the signed `fecs_bl.bin` bootloader after parsing out its `nvfw_bin_hdr` wrapper.

### PMU Falcon direct load ❌
PMU is in HS mode (SCTL = 0x3002), IMEM writes blocked (return `0xdead5ec1`). Cannot bootstrap.

### Priv ring recovery ❌
Tried nouveau GP10b init sequence (wrong chip — GP10b is Tegra), RING_CMD variants (0x1 START, 0x2 ACK, 0x4 ENUMERATE). Each attempt progressively corrupted the priv ring state. Final attempt left the priv ring collapsed with all registers returning 0xffffffff (GPU unresponsive).

### Secure IMEM write ❌
Tried IMEMC bit 28 (SECURE). The bit was not stored (read back as 0). Not accessible at our priv level.

### Signature injection ❌
Tried writing fecs_sig.bin to SIG_DATA registers. Registers accept writes but have no effect — the signature verification is done by ACR, not by a local SIG register.

### PLM (Privilege Level Mask) unlock ❌
CPUCTL_PLM at 0x40c was unlockable (wrote 0xffffffff, read back 0xffffffff). Did not change Falcon execution behavior.

### Power state cycling ❌
D3hot → D0 transition via PCI config space. No effect on locked registers.

### Cold boot with eGPU connected ❌
Apple Silicon EFI does not run GPU Option ROMs on Thunderbolt devices.

### TinyGPU PCI reset ❌
Soft reset, doesn't trigger a real PCIe Secondary Bus Reset. WPR2_HI remains `0xccccfcfc`.

## Paths Not Yet Tried (Stopped By GPU Lockup)

- **Thunderbolt Bridge Secondary Bus Reset (SBR)** — writing bit 6 of the upstream PCIe bridge's Bridge Control Register for 1us. Would require TinyGPU to expose writes to OTHER PCI devices' config space (the bridge), not just the GPU. Unknown if possible.
- **X86 Option ROM emulation** — interpreting the x86 code in the VBIOS and executing just the register pokes. Non-trivial (60KB of x86 code).
- **Host-side devinit opcode interpreter** — implementing the 50+ devinit opcodes (INIT_ZM_REG, INIT_IO_MASK_OR, INIT_PLL, INIT_TIME, etc.) as a Python interpreter and running the script at 0x8523. Might unlock some domains but full DRAM training requires priv writes to FBP which is dead.
- **Debug register access** — chips have undocumented bringup/debug modes accessed via specific register sequences. No public documentation.

## What Works Today

### CPU-only decode node (James' tqbridge-server)

The GTX 1060 machine (when recovered) can still serve as a CPU-only decode node for the turboquant-tinygrad-bridge cluster. No GPU compute required:

```bash
cd turboquant-tinygrad-bridge/deploy/src
cc -O2 -o tqbridge-server tqbridge-server.c tqbridge.c tqbridge_net.c -lm
./tqbridge-server --port 9473
```

51 KB static binary, zero runtime dependencies. Runs on macOS Apple Silicon, Linux, anywhere with libc.

### Ampere+ GPU on the same setup

Any RTX 30-series or newer card with GSP works out of the box with TinyGPU (per docs.tinygrad.org/tinygpu). GSP is host-bootstrappable via RPC, which bypasses the VBIOS Option ROM dependency entirely. Used RTX 3060 12GB runs ~$280 on eBay.

## Recovering From The Lockup

The GPU is currently unresponsive (PMC_BOOT_0 returns 0xffffffff). TinyGPU's soft reset does not recover this state. To recover:

1. Unplug the Thunderbolt cable from the Mac
2. Power off the eGPU enclosure
3. Wait 30 seconds
4. Power on the enclosure
5. Plug the Thunderbolt cable back in
6. Run `python3 transport/pci_scan.py` to verify the GPU re-enumerates
7. Run `python3 transport/tinygpu_client.py` to verify BAR0 reads work

## The Code We Wrote

Despite hitting the hardware wall, the investigation produced working code for:

- `transport/pci_scan.py` + `bar_map.py` — IOKit PCIe enumeration (clean)
- `transport/tinygpu_client.py` — standalone TinyGPU socket client
- `transport/tinygrad_transport.py` — tinygrad-backed PascalGPU wrapper (what we use)
- `hw/mmio.py` — MMIO register abstractions (PMC_ENABLE bits, engine management)
- `hw/falcon.py` — Falcon microcontroller interface (IMEM/DMEM load, DMA, status)
- `hw/mmu.py` — Pascal v2 MMU page tables, instance blocks, PRAMIN access
- `hw/secboot.py` + `secboot_v2.py` + `secboot_v3.py` — ACR bootloader sequences (would work if PMU were alive)
- `hw/pgraph.py` — PGRAPH init flow
- `hw/fecs_bootstrap.py` — FECS direct firmware load with PMC reset sequence
- `hw/acr_desc.py` — ACR descriptor builders (flcn_bl_dmem_desc, wpr_header)
- `firmware/blobs/gp106/` — all 23 NVIDIA-signed firmware blobs, extracted from Arch linux-firmware-nvidia package
- `firmware/blobs/gp106/vbios_full.rom` — 128 KB full VBIOS dump (2 segments: x86 + EFI)

If anyone ever finds a way to bootstrap PMU on Pascal over Thunderbolt eGPU on macOS (Thunderbolt SBR, x86 emulation, undocumented debug registers, etc.), this code should work end-to-end.

## References

- [Nouveau gm200 devinit](https://codebrowser.dev/linux/linux/drivers/gpu/drm/nouveau/nvkm/subdev/devinit/gm200.c.html) — reference PMU DEVINIT flow
- [Nouveau gm200 secboot](https://codebrowser.dev/linux/linux/drivers/gpu/drm/nouveau/nvkm/subdev/secboot/gm200.c.html) — ACR secure boot implementation
- [Nouveau falcon base](https://codebrowser.dev/linux/linux/drivers/gpu/drm/nouveau/nvkm/falcon/base.c.html) — Falcon IMEM/DMEM load, start, reset
- [BIT table spec](https://nvidia.github.io/open-gpu-doc/BIOS-Information-Table/BIOS-Information-Table.html) — NVIDIA BIOS Information Table
- [Falcon Security](https://nvidia.github.io/open-gpu-doc/Falcon-Security/Falcon-Security.html) — Falcon HS/LS security model
- [envytools nvbios](https://envytools.readthedocs.io/) — VBIOS format reverse engineering
- [TinyGPU docs](https://docs.tinygrad.org/tinygpu/) — Apple DriverKit extension for eGPU BAR access
- [NVIDIA open-gpu-kernel-modules](https://github.com/NVIDIA/open-gpu-kernel-modules) — Reference for PMU DEVINIT tables
