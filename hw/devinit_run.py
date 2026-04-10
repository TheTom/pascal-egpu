#!/usr/bin/env python3
"""Run the Pascal VBIOS devinit script via host MMIO.

The init script @ 0x8523 (found via BIT I) is what the Option ROM
executes during normal POST. It writes to FB controllers, clocks, FBPA,
PGRAPH, etc — exactly the registers that need to come alive.

We can't replay it perfectly because many target regs are dead from
host context, but executing it might:
1. Trigger priv ring side effects that unstick stations
2. Initialize whatever IS reachable (FECS, parts of FB, scratch state)
3. Tell us EXACTLY which write is the first one that fails — narrowing
   the bringup blocker.
"""

import os
import sys
import struct
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from transport.tinygrad_transport import PascalGPU


VBIOS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "firmware", "blobs", "gp106", "vbios_full.rom")


class DevInit:
    def __init__(self, gpu, vbios):
        self.gpu = gpu
        self.buf = vbios
        self.execute = True
        self.dry_run = False
        self.stats = {"writes_ok": 0, "writes_bad": 0, "writes_dead": 0, "ops": 0}
        self.bad_addrs = set()
        self.depth = 0

    def rd8(self, off):  return self.buf[off]
    def rd16(self, off): return struct.unpack_from('<H', self.buf, off)[0]
    def rd32(self, off): return struct.unpack_from('<I', self.buf, off)[0]

    def reg_write(self, addr, val, mask=None):
        """Write a register, classify result.

        nouveau NV_REG semantics:
          val_out = (cur & mask) | data
        i.e., mask=1 keeps current bits, mask=0 zeros them, then OR with data.
        """
        if not self.execute or self.dry_run:
            return
        # SAFETY: refuse to clear PMC_ENABLE bits — that wrecks priv ring
        if addr == 0x000200:
            self.stats["writes_bad"] += 1
            return
        try:
            if mask is not None:
                cur = self.gpu.rd32(addr)
                if (cur & 0xfff00000) == 0xbad00000 or (cur & 0xffff0000) == 0xbadf0000:
                    self.stats["writes_dead"] += 1
                    self.bad_addrs.add(addr)
                    return
                val = (cur & mask) | val
            self.gpu.wr32(addr, val)
            # Verify by reading back
            rb = self.gpu.rd32(addr)
            if (rb & 0xfff00000) == 0xbad00000 or (rb & 0xffff0000) == 0xbadf0000:
                self.stats["writes_dead"] += 1
                self.bad_addrs.add(addr)
            else:
                self.stats["writes_ok"] += 1
        except Exception:
            self.stats["writes_bad"] += 1

    def trace(self, msg):
        print(f"  {'  '*self.depth}{msg}")

    # === Opcode handlers ===

    def op_zm_reg(self, off):
        addr = self.rd32(off + 1)
        val  = self.rd32(off + 5)
        self.trace(f"ZM_REG       0x{addr:06x} = 0x{val:08x}")
        self.reg_write(addr, val)
        return 9

    def op_zm_reg_sequence(self, off):
        base = self.rd32(off + 1)
        cnt  = self.rd8(off + 5)
        self.trace(f"ZM_REG_SEQ   base=0x{base:06x} cnt={cnt}")
        for i in range(cnt):
            v = self.rd32(off + 6 + i * 4)
            self.reg_write(base + i * 4, v)
        return 6 + cnt * 4

    def op_zm_reg_group(self, off):
        addr = self.rd32(off + 1)
        cnt  = self.rd8(off + 5)
        self.trace(f"ZM_REG_GRP   reg=0x{addr:06x} cnt={cnt}")
        for i in range(cnt):
            v = self.rd32(off + 6 + i * 4)
            self.reg_write(addr, v)
        return 6 + cnt * 4

    def op_nv_reg(self, off):
        addr = self.rd32(off + 1)
        mask = self.rd32(off + 5)
        val  = self.rd32(off + 9)
        self.trace(f"NV_REG       0x{addr:06x} mask=0x{mask:08x} val=0x{val:08x}")
        self.reg_write(addr, val, mask=mask)
        return 13

    def op_zm_reg16(self, off):
        addr = self.rd32(off + 1)
        val  = self.rd16(off + 5)
        self.trace(f"ZM_REG16     0x{addr:06x} = 0x{val:04x}")
        self.reg_write(addr, val)
        return 7

    def op_index_io(self, off):
        # 1 + 2 (port) + 1 (index) + 1 (mask) + 1 (data) = 6
        port  = self.rd16(off + 1)
        index = self.rd8(off + 3)
        mask  = self.rd8(off + 4)
        data  = self.rd8(off + 5)
        self.trace(f"INDEX_IO     port=0x{port:04x} idx=0x{index:02x} (skipped — IO port)")
        return 6

    def op_zm_index_io(self, off):
        port  = self.rd16(off + 1)
        index = self.rd8(off + 3)
        data  = self.rd8(off + 4)
        self.trace(f"ZM_INDEX_IO  port=0x{port:04x} idx=0x{index:02x} (skipped — IO port)")
        return 5

    def op_time(self, off):
        us = self.rd16(off + 1)
        self.trace(f"TIME         delay {us}us")
        if self.execute and not self.dry_run:
            time.sleep(us / 1_000_000)
        return 3

    def op_done(self, off):
        self.trace("DONE")
        return -1  # signal done

    def op_resume(self, off):
        # Re-enable execution after CONDITION
        self.execute = True
        self.trace("RESUME (re-enable execution)")
        return 1

    def op_condition(self, off):
        cond = self.rd8(off + 1)
        self.trace(f"CONDITION    cond={cond} (assumed true)")
        # Don't disable execution — assume conditions met
        return 2

    def op_condition_time(self, off):
        cond  = self.rd8(off + 1)
        retries = self.rd8(off + 2)
        self.trace(f"CONDITION_TIME cond={cond} retries={retries} (assumed met)")
        return 3

    def op_sub_direct(self, off):
        sub_addr = self.rd16(off + 1)
        self.trace(f"SUB_DIRECT   -> 0x{sub_addr:04x}")
        if self.execute:
            self.depth += 1
            self.run(sub_addr)
            self.depth -= 1
        return 3

    def op_sub(self, off):
        index = self.rd8(off + 1)
        self.trace(f"SUB          idx={index} (skip — needs script table lookup)")
        return 2

    def op_jump(self, off):
        addr = self.rd16(off + 1)
        self.trace(f"JUMP         -> 0x{addr:04x}")
        return ('jump', addr)

    def op_io_mask_or(self, off):
        # 1 + 2 (idx) + 1 (mask) + 1 (data) ≈ 5? actually
        # INIT_IO_MASK_OR: 1 + 2 + 1 = 4
        return 4

    def op_io_or(self, off):
        return 4

    OPCODES = {
        0x32: ('IO_RESTRICT_PROG',     'iorprog'),
        0x33: ('REPEAT',               2),
        0x34: ('IO_RESTRICT_PLL',      'iorpll'),
        0x36: ('END_REPEAT',           1),
        0x37: ('COPY',                 11),
        0x38: ('NOT',                  1),
        0x39: ('IO_FLAG_CONDITION',    2),
        0x3a: ('GENERIC_CONDITION',    3),
        0x3b: ('IO_MASK_OR',           4),
        0x3c: ('IO_OR',                4),
        0x47: ('ANDN_REG',             9),
        0x48: ('OR_REG',               9),
        0x4a: ('IO_RESTRICT_PLL2',     'iorpll2'),
        0x4b: ('PLL2',                 9),
        0x4c: ('I2C_BYTE',             'i2cbyte'),
        0x4d: ('ZM_I2C_BYTE',          'zmi2cbyte'),
        0x4e: ('ZM_I2C',               'zmi2c'),
        0x4f: ('TMDS',                 5),
        0x50: ('ZM_TMDS_GROUP',        'zmtmdsgrp'),
        0x51: ('CR_INDEX_LATCHED',     5),
        0x52: ('CR',                   4),
        0x53: ('ZM_CR',                3),
        0x54: ('ZM_CR_GROUP',          'zmcrgrp'),
        0x56: ('CONDITION_TIME',       'condtime'),
        0x57: ('LTIME',                3),
        0x58: ('ZM_REG_SEQ',           'zmregseq'),
        0x5a: ('ZM_REG_INDIRECT',      9),
        0x5b: ('SUB_DIRECT',           'subdirect'),
        0x5c: ('JUMP',                 'jump'),
        0x5e: ('I2C_IF',               6),
        0x5f: ('COPY_NV_REG',          14),
        0x62: ('ZM_INDEX_IO',          'zmindexio'),
        0x63: ('COMPUTE_MEM',          1),
        0x65: ('RESET',                13),
        0x66: ('CONFIGURE_MEM',        1),
        0x67: ('CONFIGURE_CLK',        1),
        0x69: ('IO',                   5),
        0x6b: ('SUB',                  'sub'),
        0x6d: ('RAM_CONDITION',        3),
        0x6e: ('NV_REG',               'nvreg'),
        0x6f: ('MACRO',                2),
        0x71: ('DONE',                 'done'),
        0x72: ('RESUME',               'resume'),
        0x74: ('TIME',                 'time'),
        0x75: ('CONDITION',            'condition'),
        0x76: ('IO_CONDITION',         2),
        0x77: ('ZM_REG16',             7),
        0x78: ('INDEX_IO',             'indexio'),
        0x79: ('PLL',                  7),
        0x7a: ('ZM_REG',               'zmreg'),
        0x87: ('RAM_RESTRICT_PLL',     2),
        0x8c: ('RESET_BEGUN',          1),
        0x8d: ('RESET_END',            1),
        0x8e: ('GPIO',                 1),
        0x8f: ('RAM_RESTRICT_ZM_GRP',  'ramrestrict'),
        0x90: ('COPY_ZM_REG',          9),
        0x91: ('ZM_REG_GROUP',         'zmreggrp'),
        0x92: ('RESERVED',             1),
        0x96: ('XLAT',                 11),
        0x97: ('ZM_MASK_ADD',          13),
        0x98: ('AUXCH',                'auxch'),
        0x99: ('ZM_AUXCH',             'zmauxch'),
        0x9a: ('I2C_LONG_IF',          8),
    }

    def run(self, start, max_ops=2000):
        off = start
        ops = 0
        while ops < max_ops:
            if off >= len(self.buf):
                self.trace(f"RAN OFF END at 0x{off:x}")
                return
            op = self.buf[off]
            self.stats["ops"] += 1

            if op not in self.OPCODES:
                self.trace(f"0x{off:04x}: UNKNOWN 0x{op:02x} — STOPPING")
                return

            name, length = self.OPCODES[op]

            # Dispatch named handlers
            if length == 'zmreg':
                consumed = self.op_zm_reg(off)
            elif length == 'zmregseq':
                consumed = self.op_zm_reg_sequence(off)
            elif length == 'zmreggrp':
                consumed = self.op_zm_reg_group(off)
            elif length == 'nvreg':
                consumed = self.op_nv_reg(off)
            elif length == 'time':
                consumed = self.op_time(off)
            elif length == 'done':
                self.op_done(off)
                return
            elif length == 'resume':
                consumed = self.op_resume(off)
            elif length == 'condition':
                consumed = self.op_condition(off)
            elif length == 'condtime':
                consumed = self.op_condition_time(off)
            elif length == 'subdirect':
                consumed = self.op_sub_direct(off)
            elif length == 'sub':
                consumed = self.op_sub(off)
            elif length == 'jump':
                addr = self.rd16(off + 1)
                self.trace(f"JUMP -> 0x{addr:04x}")
                off = addr
                continue
            elif length == 'indexio':
                consumed = self.op_index_io(off)
            elif length == 'zmindexio':
                consumed = self.op_zm_index_io(off)
            elif length == 'zmi2cbyte':
                # 1 + 1 (port) + 1 (addr) + 1 (count) + count*2
                cnt = self.rd8(off + 3)
                self.trace(f"ZM_I2C_BYTE  count={cnt} (skip i2c)")
                consumed = 4 + cnt * 2
            elif length == 'i2cbyte':
                # 1 + 1 (port) + 1 (addr) + 1 (count) + count*3
                cnt = self.rd8(off + 3)
                self.trace(f"I2C_BYTE     count={cnt} (skip i2c)")
                consumed = 4 + cnt * 3
            elif length == 'zmi2c':
                cnt = self.rd8(off + 3)
                self.trace(f"ZM_I2C       count={cnt} (skip i2c)")
                consumed = 4 + cnt
            elif length == 'zmtmdsgrp':
                cnt = self.rd8(off + 2)
                self.trace(f"ZM_TMDS_GRP  count={cnt} (skip)")
                consumed = 3 + cnt * 2
            elif length == 'zmcrgrp':
                cnt = self.rd8(off + 1)
                self.trace(f"ZM_CR_GRP    count={cnt} (skip)")
                consumed = 2 + cnt * 2
            elif length == 'iorprog':
                cnt = self.rd8(off + 5)
                self.trace(f"IO_RESTRICT_PROG count={cnt} (skip)")
                consumed = 6 + cnt * 4
            elif length == 'iorpll':
                cnt = self.rd8(off + 6)
                self.trace(f"IO_RESTRICT_PLL count={cnt} (skip)")
                consumed = 7 + cnt * 2
            elif length == 'iorpll2':
                cnt = self.rd8(off + 5)
                self.trace(f"IO_RESTRICT_PLL2 count={cnt} (skip)")
                consumed = 6 + cnt * 4
            elif length == 'auxch':
                cnt = self.rd8(off + 1)
                self.trace(f"AUXCH        count={cnt} (skip)")
                consumed = 2 + cnt * 2
            elif length == 'zmauxch':
                cnt = self.rd8(off + 1)
                self.trace(f"ZM_AUXCH     count={cnt} (skip)")
                consumed = 2 + cnt
            elif length == 'ramrestrict':
                # 1 + 1 (regs) + 1 (groups) + regs*4 + groups*regs*4
                regs = self.rd8(off + 1)
                groups = self.rd8(off + 2)
                self.trace(f"RAM_RESTRICT_ZM_GRP regs={regs} groups={groups} (skip)")
                consumed = 3 + regs * 4 + groups * regs * 4
            elif isinstance(length, int):
                # Generic skip
                self.trace(f"0x{off:04x}: {name} (skip {length})")
                consumed = length
            else:
                self.trace(f"0x{off:04x}: {name} variable len — STOPPING")
                return

            off += consumed
            ops += 1


def main():
    print("=" * 70)
    print("  Pascal devinit interpreter — host MMIO replay")
    print("=" * 70)

    with open(VBIOS_PATH, 'rb') as f:
        vbios = f.read()

    gpu = PascalGPU()
    if gpu.rd32(0) == 0xffffffff:
        print("GPU dead"); return
    cmd = gpu.cfg_read(0x04, 2)
    if not (cmd & 0x06):
        gpu.cfg_write(0x04, cmd | 0x06, 2)
    gpu.wr32(0x000200, 0xffffffff)
    time.sleep(0.05)

    # Capture pre-state
    pre = {}
    for name, addr in [("PMU_SCTL", 0x10a240), ("FECS_SCTL", 0x409240),
                        ("FBP0", 0x12a270), ("GPC0", 0x128280),
                        ("PFB_CFG0", 0x100200), ("FBPA", 0x9a0530),
                        ("CLK", 0x132800), ("PWR", 0x17e244)]:
        pre[name] = gpu.rd32(addr)

    # Run init script 0
    di = DevInit(gpu, vbios)
    print("\n--- Running init script @ 0x8523 ---\n")
    di.run(0x8523)

    print("\n--- Stats ---")
    print(f"  Ops executed: {di.stats['ops']}")
    print(f"  Writes OK:    {di.stats['writes_ok']}")
    print(f"  Writes dead:  {di.stats['writes_dead']}")
    print(f"  Writes bad:   {di.stats['writes_bad']}")
    print(f"  Unique dead addrs: {len(di.bad_addrs)}")

    # Capture post-state
    print("\n--- Pre/post state ---")
    for name, addr in [("PMU_SCTL", 0x10a240), ("FECS_SCTL", 0x409240),
                        ("FBP0", 0x12a270), ("GPC0", 0x128280),
                        ("PFB_CFG0", 0x100200), ("FBPA", 0x9a0530),
                        ("CLK", 0x132800), ("PWR", 0x17e244)]:
        post = gpu.rd32(addr)
        b = pre[name]
        marker = "  ★ CHANGED" if post != b else ""
        print(f"  {name:10}: 0x{b:08x} -> 0x{post:08x}{marker}")

    gpu.close()


if __name__ == "__main__":
    main()
