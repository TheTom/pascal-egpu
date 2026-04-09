# Nouveau Secure Boot Reference for Pascal

See commit message and research agent output for full details.

## Key Register Findings

### Instance Block Binding (Falcon)
- `0x054`: Instance block address + location (NOT 0x480)
- `0x048`: Bit 0 = context binding enable
- `0x10c`: DMA control (1=enable, 0=disable)
- `0x090`: Bit 16 = TLB enable
- `0x0a4`: Bit 3 = TLB enable

### FBIF Aperture Base (per engine)
- PMU: 0xe00
- SEC2/default: 0x600
- NVENC: 0x800

### DMA Context Indices
- 0 = UCODE (virtual, for LS bootloader DMA from WPR)
- 1 = VIRT (virtual addressing)
- 2 = PHYS_VID (physical VRAM)
- 3 = PHYS_SYS_COH
- 4 = PHYS_SYS_NCOH

### Aperture Values (written to fbif + 4*idx)
- 0x0 = physical VRAM
- 0x4 = virtual (via instance block page tables)
- 0x5 = physical system coherent
- 0x6 = physical system non-coherent

### Falcon Write Sequence
IMEM: wr32(0x180, start | BIT(24)); loop wr32(0x184, data); wr32(0x188, tag) every 256B
DMEM: wr32(0x1c0, start | BIT(24)); loop wr32(0x1c4, data)

## BLOCKER: GPU Write Access

The fundamental issue is that ALL register writes are silently dropped.
The GPU returns:
- 0xbad00200 for PMC scratch writes
- 0xbad0acXX for VRAM writes
- 0xdead5ec1/2 for Falcon IMEM/DMEM

This means the GPU is in a security-locked state that prevents all
register modification. This is likely because VBIOS POST has not run
over the Thunderbolt eGPU connection.
