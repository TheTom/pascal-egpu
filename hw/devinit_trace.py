#!/usr/bin/env python3
"""Trace devinit script opcodes from Pascal VBIOS without executing.

Walks the script bytecode (including subroutine calls) and reports:
  - Every opcode encountered, with its address
  - Statistics on which opcodes need an interpreter
  - Register addresses being written / conditions checked

This gives us the surface area we need to implement in the interpreter.

Reference: drivers/gpu/drm/nouveau/nvkm/subdev/bios/init.c
"""

import os
import struct

VBIOS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "firmware", "blobs", "gp106", "vbios_full.rom")


# Opcode table: name, fixed length (or None for variable)
# Subset of nvkm/subdev/bios/init.c opc table — covers what gp10x typically uses.
OPCODES = {
    0x32: ("INIT_IO_RESTRICT_PROG",          None),
    0x33: ("INIT_REPEAT",                    2),
    0x34: ("INIT_IO_RESTRICT_PLL",           None),
    0x36: ("INIT_END_REPEAT",                1),
    0x37: ("INIT_COPY",                      11),
    0x38: ("INIT_NOT",                       1),
    0x39: ("INIT_IO_FLAG_CONDITION",         2),
    0x3a: ("INIT_GENERIC_CONDITION",         3),
    0x3b: ("INIT_IO_MASK_OR",                3),
    0x3c: ("INIT_IO_OR",                     3),
    0x47: ("INIT_ANDN_REG",                  9),
    0x48: ("INIT_OR_REG",                    9),
    0x49: ("INIT_IDX_ADDR_LATCHED",          None),
    0x4a: ("INIT_IO_RESTRICT_PLL2",          None),
    0x4b: ("INIT_PLL2",                      9),
    0x4c: ("INIT_I2C_BYTE",                  None),
    0x4d: ("INIT_ZM_I2C_BYTE",               None),
    0x4e: ("INIT_ZM_I2C",                    None),
    0x4f: ("INIT_TMDS",                      5),
    0x50: ("INIT_ZM_TMDS_GROUP",             None),
    0x51: ("INIT_CR_INDEX_ADDR_LATCHED",     None),
    0x52: ("INIT_CR",                        4),
    0x53: ("INIT_ZM_CR",                     3),
    0x54: ("INIT_ZM_CR_GROUP",               None),
    0x56: ("INIT_CONDITION_TIME",            3),
    0x57: ("INIT_LTIME",                     3),
    0x58: ("INIT_ZM_REG_SEQUENCE",           None),  # 1 + 4 + 1 + count*4
    0x5a: ("INIT_PMU",                       9),
    0x5b: ("INIT_SUB_DIRECT",                3),
    0x5c: ("INIT_JUMP",                      3),
    0x5e: ("INIT_I2C_IF",                    6),
    0x5f: ("INIT_COPY_NV_REG",               14),
    0x62: ("INIT_ZM_INDEX_IO",               4),
    0x63: ("INIT_COMPUTE_MEM",               1),
    0x65: ("INIT_RESET",                     13),
    0x66: ("INIT_CONFIGURE_MEM",             1),
    0x67: ("INIT_CONFIGURE_CLK",             1),
    0x68: ("INIT_CONFIGURE_PREINIT",         1),
    0x69: ("INIT_IO",                        5),
    0x6b: ("INIT_SUB",                       2),
    0x6d: ("INIT_RAM_CONDITION",             3),
    0x6e: ("INIT_NV_REG",                    13),  # 1 + 4 + 4 + 4
    0x6f: ("INIT_MACRO",                     2),
    0x71: ("INIT_DONE",                      1),
    0x72: ("INIT_RESUME",                    1),
    0x74: ("INIT_TIME",                      3),
    0x75: ("INIT_CONDITION",                 2),
    0x76: ("INIT_IO_CONDITION",              2),
    0x77: ("INIT_ZM_REG16",                  7),
    0x78: ("INIT_INDEX_IO",                  6),
    0x79: ("INIT_PLL",                       7),
    0x7a: ("INIT_ZM_REG",                    9),  # 1 + 4 + 4
    0x87: ("INIT_RAM_RESTRICT_PLL",          2),
    0x8c: ("INIT_RESET_BEGUN",               1),
    0x8d: ("INIT_RESET_END",                 1),
    0x8e: ("INIT_GPIO",                      1),
    0x8f: ("INIT_RAM_RESTRICT_ZM_REG_GROUP", None),
    0x90: ("INIT_COPY_ZM_REG",               9),
    0x91: ("INIT_ZM_REG_GROUP",              None),  # 1 + 4 + 1 + count*4
    0x92: ("INIT_RESERVED",                  1),
    0x96: ("INIT_XLAT",                      11),
    0x97: ("INIT_ZM_MASK_ADD",               13),
    0x98: ("INIT_AUXCH",                     None),
    0x99: ("INIT_ZM_AUXCH",                  None),
    0x9a: ("INIT_I2C_LONG_IF",               8),
}


def rd8(buf, off):  return buf[off]
def rd16(buf, off): return struct.unpack_from('<H', buf, off)[0]
def rd32(buf, off): return struct.unpack_from('<I', buf, off)[0]


def opcode_length(buf, off):
    """Compute length of variable-length opcodes."""
    op = buf[off]
    name, fixed = OPCODES.get(op, (f"UNK_{op:02x}", None))
    if fixed is not None:
        return fixed, name
    # Variable length opcodes
    if op == 0x58:  # INIT_ZM_REG_SEQUENCE: 1 + 4 + 1 + cnt*4
        cnt = buf[off + 5]
        return 6 + cnt * 4, name
    if op == 0x91:  # INIT_ZM_REG_GROUP: 1 + 4 + 1 + cnt*4 ?
        cnt = buf[off + 5]
        return 6 + cnt * 4, name
    if op == 0x8f:  # INIT_RAM_RESTRICT_ZM_REG_GROUP: 1 + 4 + 1 + 1 + N*regs
        return None, name  # complex
    if op == 0x32:  # INIT_IO_RESTRICT_PROG: 1+2+1+1+1+cnt*4
        cnt = buf[off + 5]
        return 6 + cnt * 4, name
    if op == 0x34:  # INIT_IO_RESTRICT_PLL: 1+2+1+1+1+1+cnt*2
        cnt = buf[off + 6]
        return 7 + cnt * 2, name
    if op == 0x4a:  # INIT_IO_RESTRICT_PLL2: 1+2+1+1+1+cnt*4
        cnt = buf[off + 5]
        return 6 + cnt * 4, name
    if op == 0x4c:  # INIT_I2C_BYTE: 1+1+1+cnt*2
        cnt = buf[off + 3]
        return 4 + cnt * 2, name
    if op == 0x4d:  # INIT_ZM_I2C_BYTE: 1+1+1+cnt*2
        cnt = buf[off + 3]
        return 4 + cnt * 2, name
    if op == 0x4e:  # INIT_ZM_I2C: 1+1+1+cnt
        cnt = buf[off + 3]
        return 4 + cnt, name
    if op == 0x50:  # INIT_ZM_TMDS_GROUP: 1+1+cnt*2
        cnt = buf[off + 2]
        return 3 + cnt * 2, name
    if op == 0x54:  # INIT_ZM_CR_GROUP: 1+cnt*2
        cnt = buf[off + 1]
        return 2 + cnt * 2, name
    return None, name


def trace_script(buf, start, visited, name="", depth=0, max_steps=2000):
    """Walk a script from `start`, recording opcodes and recursing into subs."""
    if start in visited:
        return
    visited.add(start)
    indent = "  " * depth
    print(f"{indent}--- Script @ 0x{start:04x} {name} ---")
    off = start
    steps = 0
    while steps < max_steps:
        if off >= len(buf):
            print(f"{indent}  RAN OFF END at 0x{off:x}")
            return
        op = buf[off]
        length, opname = opcode_length(buf, off)

        # Print line
        if length is None:
            print(f"{indent}  0x{off:04x}: {op:02x} {opname}  <UNKNOWN-LEN>")
            return

        databytes = buf[off + 1: off + length]
        hex_data = ' '.join(f'{b:02x}' for b in databytes[:12])
        if len(databytes) > 12:
            hex_data += " ..."

        # Decode common ones inline
        info = ""
        if op == 0x7a and length == 9:  # ZM_REG
            addr = rd32(buf, off + 1)
            data = rd32(buf, off + 5)
            info = f" reg=0x{addr:06x} val=0x{data:08x}"
        elif op == 0x58 and length and length >= 6:  # ZM_REG_SEQUENCE
            addr = rd32(buf, off + 1)
            cnt = buf[off + 5]
            info = f" base=0x{addr:06x} cnt={cnt}"
        elif op == 0x91 and length and length >= 6:  # ZM_REG_GROUP
            addr = rd32(buf, off + 1)
            cnt = buf[off + 5]
            info = f" reg=0x{addr:06x} cnt={cnt}"
        elif op == 0x6e and length == 13:  # NV_REG (mask+data)
            addr = rd32(buf, off + 1)
            mask = rd32(buf, off + 5)
            data = rd32(buf, off + 9)
            info = f" reg=0x{addr:06x} mask=0x{mask:08x} val=0x{data:08x}"
        elif op == 0x5b and length == 3:  # SUB_DIRECT
            sub = rd16(buf, off + 1)
            info = f" -> 0x{sub:04x}"
        elif op == 0x74 and length == 3:  # TIME
            t = rd16(buf, off + 1)
            info = f" us={t}"
        elif op == 0x75 and length == 2:  # CONDITION
            c = buf[off + 1]
            info = f" cond={c}"
        elif op == 0x56 and length == 3:  # CONDITION_TIME
            c = buf[off + 1]
            t = buf[off + 2]
            info = f" cond={c} retries={t}"

        print(f"{indent}  0x{off:04x}: {op:02x} {opname}{info}")

        # Recurse into SUB_DIRECT
        if op == 0x5b and length == 3:
            sub_addr = rd16(buf, off + 1)
            trace_script(buf, sub_addr, visited, name=f"(sub from 0x{off:04x})",
                         depth=depth + 1, max_steps=max_steps)

        # Stop on DONE
        if op == 0x71:
            return

        off += length
        steps += 1

    print(f"{indent}  STOPPED at max_steps")


def main():
    with open(VBIOS_PATH, 'rb') as f:
        buf = f.read()

    # init script table at 0x497e (per BIT I + 0)
    script0 = rd16(buf, 0x497e)
    print(f"Tracing init script 0 @ 0x{script0:04x}\n")

    visited = set()
    trace_script(buf, script0, visited)

    print(f"\n=== Summary ===")
    print(f"Visited {len(visited)} unique scripts")


if __name__ == "__main__":
    main()
