#!/usr/bin/env python3
"""ACR Descriptor Builder for Pascal Secure Boot.

The ACR bootloader (bl.bin) loaded into PMU Falcon IMEM expects a
`flcn_bl_dmem_desc` struct at the start of DMEM. This descriptor
tells the bootloader where to DMA the ACR ucode from VRAM.

After the ACR ucode starts, it reads a `wpr_header` table from VRAM
that lists all LS (Light Secure) falcons and their firmware locations.
It verifies signatures and loads each falcon's firmware via DMA.

Structs (from nouveau include/nvfw/flcn.h and acr.h):

struct flcn_bl_dmem_desc {
    u32 reserved[4];       // 16 bytes padding
    u32 signature[4];      // 16 bytes signature
    u32 ctx_dma;           // DMA context index (0 = default)
    u32 code_dma_base;     // VRAM address of ucode code >> 8
    u32 non_sec_code_off;  // Offset of non-secure code in image
    u32 non_sec_code_size; // Size of non-secure code
    u32 sec_code_off;      // Offset of secure code
    u32 sec_code_size;     // Size of secure code
    u32 code_entry_point;  // Entry point offset
    u32 data_dma_base;     // VRAM address of ucode data >> 8
    u32 data_size;         // Size of data segment
    u32 code_dma_base1;    // Upper bits of code address
    u32 data_dma_base1;    // Upper bits of data address
};

struct wpr_header {
    u32 falcon_id;
    u32 lsb_offset;        // Offset to lsb_header in WPR region
    u32 bootstrap_owner;   // Falcon that should load this one (usually PMU)
    u32 lazy_bootstrap;    // 0 = load immediately, 1 = load on demand
    u32 status;            // WPR_HEADER_V0_STATUS_NONE = 0
};

Falcon IDs (from nouveau):
  FECS  = 0 (or 1 depending on version)
  GPCCS = 1 (or 2)
  PMU   = 7
  SEC2  = ?
"""

import struct


# Falcon IDs for Pascal ACR
FALCON_ID_FECS  = 0
FALCON_ID_GPCCS = 1
FALCON_ID_PMU   = 7

# WPR header status
WPR_HEADER_STATUS_NONE = 0
WPR_HEADER_STATUS_COPY = 1
WPR_HEADER_STATUS_VALID = 2


def build_bl_dmem_desc(code_dma_base: int, code_size: int, data_dma_base: int,
                        data_size: int, code_entry_point: int = 0) -> bytes:
    """Build the flcn_bl_dmem_desc that goes into Falcon DMEM.

    This tells the HS (High Secure) bootloader where to DMA
    the ACR ucode code and data segments from VRAM.

    Args:
        code_dma_base: VRAM physical address of code segment
        code_size: Size of code segment
        data_dma_base: VRAM physical address of data segment
        data_size: Size of data segment
        code_entry_point: Entry point offset within code
    """
    desc = struct.pack('<'
        '4I'   # reserved[4]
        '4I'   # signature[4]
        'I'    # ctx_dma
        'I'    # code_dma_base (>> 8)
        'I'    # non_sec_code_off
        'I'    # non_sec_code_size
        'I'    # sec_code_off
        'I'    # sec_code_size
        'I'    # code_entry_point
        'I'    # data_dma_base (>> 8)
        'I'    # data_size
        'I'    # code_dma_base1
        'I'    # data_dma_base1
        ,
        # reserved
        0, 0, 0, 0,
        # signature (zeros for now)
        0, 0, 0, 0,
        # ctx_dma — 0 for default DMA context
        0,
        # code_dma_base — VRAM address >> 8
        (code_dma_base >> 8) & 0xffffffff,
        # non_sec_code_off — offset of non-secure code (0 for ACR)
        0,
        # non_sec_code_size — size of non-secure portion
        code_size,
        # sec_code_off — offset of secure code (typically follows non-secure)
        0,
        # sec_code_size — size of secure portion
        code_size,
        # code_entry_point
        code_entry_point,
        # data_dma_base
        (data_dma_base >> 8) & 0xffffffff,
        # data_size
        data_size,
        # code_dma_base1 (upper 32 bits)
        (code_dma_base >> 40) & 0xffffffff,
        # data_dma_base1 (upper 32 bits)
        (data_dma_base >> 40) & 0xffffffff,
    )
    return desc


def build_wpr_header(falcon_id: int, lsb_offset: int, bootstrap_owner: int = FALCON_ID_PMU,
                      lazy_bootstrap: int = 0) -> bytes:
    """Build a WPR header entry for one falcon.

    Args:
        falcon_id: Which falcon (FECS=0, GPCCS=1, PMU=7)
        lsb_offset: Offset to this falcon's lsb_header in WPR region
        bootstrap_owner: Who loads this falcon (usually PMU)
        lazy_bootstrap: 0=immediate, 1=on demand
    """
    return struct.pack('<5I',
        falcon_id,
        lsb_offset,
        bootstrap_owner,
        lazy_bootstrap,
        WPR_HEADER_STATUS_NONE,
    )


def build_wpr_header_terminator() -> bytes:
    """Build a WPR header terminator entry (falcon_id = 0xffffffff)."""
    return struct.pack('<5I', 0xffffffff, 0, 0, 0, 0)


def build_lsf_signature(sig_data: bytes) -> bytes:
    """Build an LSF signature block from the .sig firmware file.

    The signature file contains:
      - Offset 0x00: prod_sig (96 bytes)
      - Offset 0x60: dbg_sig (96 bytes)

    Total: 192 bytes (matching our .sig file sizes)
    """
    if len(sig_data) != 192:
        # Pad or truncate to expected size
        sig_data = sig_data[:192].ljust(192, b'\x00')
    return sig_data


def build_lsb_header(sig_data: bytes, ucode_off: int, ucode_size: int,
                      data_size: int, bl_code_size: int) -> bytes:
    """Build LSB (Light Secure Bootloader) header for a falcon.

    struct lsb_header {
        struct lsf_signature signature;  // 192 bytes
        struct lsb_header_tail tail;     // variable
    };

    lsb_header_tail contains the loader_config that tells the
    LS bootloader where code and data segments are.
    """
    sig = build_lsf_signature(sig_data)

    # lsb_header_tail (simplified):
    # Contains offset/size info for the ucode image
    tail = struct.pack('<6I',
        ucode_off,       # offset to ucode within WPR blob
        ucode_size,      # total ucode image size
        data_size,        # data segment size
        bl_code_size,     # bootloader code size
        0,                # flags
        0,                # reserved
    )

    return sig + tail


# Convenience for building the full WPR blob
def build_acr_wpr_blob(fw_addrs: dict) -> tuple[bytes, int]:
    """Build the complete WPR blob with headers and firmware.

    Args:
        fw_addrs: Dict of firmware name -> {"addr": vram_addr, "size": size}

    Returns:
        (blob_data, blob_size) — the complete WPR blob to write to VRAM
    """
    # For now, just build the WPR header table
    # The full implementation needs LSB headers for each falcon

    headers = bytearray()

    # FECS entry
    if "gr/fecs_inst.bin" in fw_addrs:
        headers += build_wpr_header(FALCON_ID_FECS, lsb_offset=0)  # TODO: real offset

    # GPCCS entry
    if "gr/gpccs_inst.bin" in fw_addrs:
        headers += build_wpr_header(FALCON_ID_GPCCS, lsb_offset=0)  # TODO: real offset

    # Terminator
    headers += build_wpr_header_terminator()

    return bytes(headers), len(headers)
