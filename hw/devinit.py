#!/usr/bin/env python3
"""NVIDIA VBIOS devinit script interpreter for Pascal.

Interprets the devinit bytecode found in the GPU's VBIOS ROM to bring up
hardware subsystems (FBP, GPC, PLLs, DRAM) that are normally initialized
by the system BIOS running the Option ROM at POST time.

On eGPU over Thunderbolt from macOS, the Option ROM never runs because
Apple Silicon EFI doesn't execute GPU Option ROMs on hot-plug. This
interpreter replaces that step by executing the same register writes
via BAR0 MMIO.

Based on nouveau's nvkm/subdev/bios/init.c (Ben Skeggs, Red Hat).

Usage:
    gpu = PascalGPU()
    rom = open("firmware/blobs/gp106/vbios_full.rom", "rb").read()
    interpreter = DevinitInterpreter(gpu, rom)
    interpreter.run_script(0)  # Run devinit script 0
"""

import struct
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class DevinitInterpreter:
    """Interprets NVIDIA VBIOS devinit bytecode scripts.

    The devinit scripts are a simple bytecode format stored in the VBIOS ROM.
    Each opcode is 1 byte followed by opcode-specific parameters. The scripts
    configure PLLs, write registers, set up memory controllers, and bring up
    hardware subsystems.

    This interpreter supports the opcodes needed for Pascal GPU bring-up.
    Unsupported opcodes are logged and skipped.
    """

    def __init__(self, gpu, rom: bytes, verbose: bool = True, dry_run: bool = False):
        """
        Args:
            gpu: PascalGPU instance with rd32/wr32 methods
            rom: Full VBIOS ROM bytes (128KB typical)
            verbose: Print each opcode as it executes
            dry_run: Log register writes but don't actually write
        """
        self.gpu = gpu
        self.rom = rom
        self.verbose = verbose
        self.dry_run = dry_run
        self.execute = True  # Conditional execution state
        self.nested = 0
        self.offset = 0
        self._writes = 0
        self._reads = 0
        self._errors = []

        # Parse BIT table to find script pointers
        self._bit_offset = None
        self._init_script_table = None
        self._condition_table = None
        self._find_bit_table()

    # ── ROM access ─────────────────────────────────────────────

    def rd08(self, off: int) -> int:
        if off < len(self.rom):
            return self.rom[off]
        return 0

    def rd16(self, off: int) -> int:
        if off + 1 < len(self.rom):
            return struct.unpack_from('<H', self.rom, off)[0]
        return 0

    def rd32(self, off: int) -> int:
        if off + 3 < len(self.rom):
            return struct.unpack_from('<I', self.rom, off)[0]
        return 0

    # ── GPU register access ────────────────────────────────────

    def reg_rd32(self, reg: int) -> int:
        self._reads += 1
        return self.gpu.rd32(reg)

    def reg_wr32(self, reg: int, val: int):
        self._writes += 1
        if self.verbose:
            print(f"    WR32 [0x{reg:06x}] = 0x{val:08x}")
        if not self.dry_run:
            self.gpu.wr32(reg, val)

    def reg_mask(self, reg: int, mask: int, val: int):
        data = self.reg_rd32(reg)
        self.reg_wr32(reg, (data & ~mask) | val)

    # ── BIT table parsing ──────────────────────────────────────

    def _find_bit_table(self):
        """Find the BIT header in the VBIOS ROM."""
        for off in range(0, len(self.rom) - 4):
            if self.rom[off:off+4] == b'\xff\xb8\x7c\x00' or \
               self.rom[off:off+3] == b'BIT':
                self._bit_offset = off
                break

        # Also search for the init script table pointer
        # BIT 'I' entry contains devinit script pointers
        if self._bit_offset:
            self._parse_bit_entries()

    def _parse_bit_entries(self):
        """Parse BIT table entries to find devinit script table."""
        off = self._bit_offset
        if self.rom[off:off+3] == b'BIT':
            off += 4  # Skip "BIT\x00"
            hdr_size = self.rd08(off)
            num_entries = self.rd08(off + 1)
            off += hdr_size

            for i in range(num_entries):
                entry_type = self.rd08(off)
                entry_ver = self.rd08(off + 1)
                entry_size = self.rd16(off + 2)
                entry_data = self.rd16(off + 4)

                if entry_type == ord('I'):
                    # Init table entry
                    if entry_size >= 2:
                        self._init_script_table = self.rd16(entry_data)
                    if entry_size >= 4:
                        # Macro table
                        pass
                    if entry_size >= 6:
                        self._condition_table = self.rd16(entry_data + 4)

                off += 6  # Each BIT entry is 6 bytes

    # ── Script execution ───────────────────────────────────────

    def find_scripts(self) -> list:
        """Find all devinit scripts in the VBIOS."""
        scripts = []
        if self._init_script_table:
            off = self._init_script_table
            while True:
                addr = self.rd16(off)
                if addr == 0 or addr == 0xFFFF:
                    break
                scripts.append(addr)
                off += 2
        return scripts

    def run_script(self, index: int = 0):
        """Run devinit script by index."""
        scripts = self.find_scripts()
        if index >= len(scripts):
            print(f"Script {index} not found (have {len(scripts)} scripts)")
            return False

        addr = scripts[index]
        print(f"[devinit] Running script {index} at ROM offset 0x{addr:04x}")
        self.offset = addr
        self.nested = 1
        self.execute = True
        self._run()
        print(f"[devinit] Script {index} complete: {self._writes} writes, "
              f"{self._reads} reads, {len(self._errors)} errors")
        return len(self._errors) == 0

    def run_all_scripts(self):
        """Run all devinit scripts in order."""
        scripts = self.find_scripts()
        print(f"[devinit] Found {len(scripts)} scripts")
        for i, addr in enumerate(scripts):
            print(f"\n[devinit] === Script {i} at 0x{addr:04x} ===")
            self.offset = addr
            self.nested = 1
            self.execute = True
            self._run()

    def _run(self):
        """Execute opcodes starting at self.offset until INIT_DONE."""
        max_ops = 10000  # Safety limit
        ops = 0
        while ops < max_ops:
            opcode = self.rd08(self.offset)

            if opcode == 0x71:  # INIT_DONE
                if self.verbose:
                    print(f"  [0x{self.offset:04x}] DONE")
                self.offset += 1
                return

            handler = self._opcodes.get(opcode)
            if handler:
                if self.verbose:
                    print(f"  [0x{self.offset:04x}] opcode 0x{opcode:02x}", end="")
                handler(self)
            else:
                print(f"  [0x{self.offset:04x}] UNKNOWN opcode 0x{opcode:02x} — STOPPING")
                self._errors.append(f"Unknown opcode 0x{opcode:02x} at 0x{self.offset:04x}")
                return

            ops += 1

        print(f"  WARNING: hit max ops limit ({max_ops})")

    # ── Opcode implementations ─────────────────────────────────

    def _op_zm_reg(self):
        """0x7a: Write register unconditionally."""
        addr = self.rd32(self.offset + 1)
        data = self.rd32(self.offset + 5)
        if self.verbose:
            print(f" ZM_REG R[0x{addr:06x}] = 0x{data:08x}")
        self.offset += 9
        if self.execute:
            # Special case: PMC_ENABLE must keep bit 0 set
            if addr == 0x000200:
                data |= 0x00000001
            self.reg_wr32(addr, data)

    def _op_zm_reg_group(self):
        """0x91: Write multiple registers."""
        addr = self.rd32(self.offset + 1)
        count = self.rd08(self.offset + 5)
        if self.verbose:
            print(f" ZM_REG_GROUP R[0x{addr:06x}] x{count}")
        self.offset += 6
        for i in range(count):
            data = self.rd32(self.offset)
            self.offset += 4
            if self.execute:
                self.reg_wr32(addr + i * 4, data)

    def _op_sub_direct(self):
        """0x5b: Call subroutine at ROM address."""
        addr = self.rd16(self.offset + 1)
        if self.verbose:
            print(f" SUB_DIRECT → 0x{addr:04x}")
        save = self.offset + 3
        self.offset = addr
        self.nested += 1
        self._run()
        self.nested -= 1
        self.offset = save

    def _op_time(self):
        """0x74: Delay in microseconds."""
        usec = self.rd16(self.offset + 1)
        if self.verbose:
            print(f" TIME {usec} us")
        self.offset += 3
        if self.execute:
            time.sleep(usec / 1_000_000)

    def _op_condition(self):
        """0x75: Conditional execution based on register value."""
        cond = self.rd08(self.offset + 1)
        if self.verbose:
            print(f" CONDITION {cond}")
        self.offset += 2
        if self._condition_table:
            reg = self.rd32(self._condition_table + cond * 12 + 0)
            msk = self.rd32(self._condition_table + cond * 12 + 4)
            val = self.rd32(self._condition_table + cond * 12 + 8)
            actual = self.reg_rd32(reg)
            met = (actual & msk) == val
            if self.verbose:
                print(f"    R[0x{reg:06x}] & 0x{msk:08x} == 0x{val:08x} → "
                      f"0x{actual:08x} → {'MET' if met else 'NOT MET'}")
            if not met:
                self.execute = False

    def _op_nv_reg(self):
        """0x6e: Read-modify-write register."""
        reg = self.rd32(self.offset + 1)
        mask = self.rd32(self.offset + 5)
        data = self.rd32(self.offset + 9)
        if self.verbose:
            print(f" NV_REG R[0x{reg:06x}] &= ~0x{mask:08x} |= 0x{data:08x}")
        self.offset += 13
        if self.execute:
            self.reg_mask(reg, mask, data)

    def _op_or_reg(self):
        """0x48: OR register."""
        reg = self.rd32(self.offset + 1)
        data = self.rd32(self.offset + 5)
        if self.verbose:
            print(f" OR_REG R[0x{reg:06x}] |= 0x{data:08x}")
        self.offset += 9
        if self.execute:
            val = self.reg_rd32(reg)
            self.reg_wr32(reg, val | data)

    def _op_andn_reg(self):
        """0x47: AND-NOT register."""
        reg = self.rd32(self.offset + 1)
        data = self.rd32(self.offset + 5)
        if self.verbose:
            print(f" ANDN_REG R[0x{reg:06x}] &= ~0x{data:08x}")
        self.offset += 9
        if self.execute:
            val = self.reg_rd32(reg)
            self.reg_wr32(reg, val & ~data)

    def _op_not(self):
        """0x38: Invert execution condition."""
        if self.verbose:
            print(f" NOT")
        self.offset += 1
        self.execute = not self.execute

    def _op_resume(self):
        """0x72: Resume execution (unconditional)."""
        if self.verbose:
            print(f" RESUME")
        self.offset += 1
        self.execute = True

    def _op_repeat(self):
        """0x33: Start repeat block."""
        count = self.rd08(self.offset + 1)
        if self.verbose:
            print(f" REPEAT x{count}")
        self.offset += 2
        # Simple implementation: just continue (repeat handled by END_REPEAT)
        self._repeat_count = count
        self._repeat_offset = self.offset

    def _op_end_repeat(self):
        """0x36: End repeat block."""
        if self.verbose:
            print(f" END_REPEAT")
        self.offset += 1
        if hasattr(self, '_repeat_count') and self._repeat_count > 1:
            self._repeat_count -= 1
            self.offset = self._repeat_offset

    def _op_copy(self):
        """0x37: Copy register bits."""
        src_reg = self.rd32(self.offset + 1)
        shift = self.rd08(self.offset + 5)
        src_mask = self.rd32(self.offset + 6)
        dst_reg = self.rd32(self.offset + 10)
        dst_mask = self.rd32(self.offset + 14)
        if self.verbose:
            print(f" COPY R[0x{src_reg:06x}] → R[0x{dst_reg:06x}]")
        self.offset += 18
        if self.execute:
            data = self.reg_rd32(src_reg)
            if shift < 0x80:
                data >>= shift
            else:
                data <<= (0x100 - shift)
            data &= src_mask
            val = self.reg_rd32(dst_reg) & ~dst_mask
            self.reg_wr32(dst_reg, val | data)

    def _op_zm_reg_sequence(self):
        """0x58: Write sequence of registers."""
        base = self.rd32(self.offset + 1)
        count = self.rd08(self.offset + 5)
        if self.verbose:
            print(f" ZM_REG_SEQ R[0x{base:06x}] x{count}")
        self.offset += 6
        for i in range(count):
            data = self.rd32(self.offset)
            self.offset += 4
            if self.execute:
                self.reg_wr32(base + i * 4, data)

    def _op_pll(self):
        """0x79: Configure PLL (simplified — just write the registers)."""
        reg = self.rd32(self.offset + 1)
        freq = self.rd16(self.offset + 5) * 10  # kHz
        if self.verbose:
            print(f" PLL R[0x{reg:06x}] = {freq} kHz (SIMPLIFIED)")
        self.offset += 7
        # PLL configuration is complex — for now, log but skip
        # Full implementation needs PLL parameter tables from VBIOS

    def _op_io(self):
        """0x69: I/O port access (skip on macOS — no VGA ports)."""
        port = self.rd16(self.offset + 1)
        mask = self.rd08(self.offset + 3)
        data = self.rd08(self.offset + 4)
        if self.verbose:
            print(f" IO port 0x{port:04x} (SKIPPED — no VGA)")
        self.offset += 5

    def _op_sub(self):
        """0x6b: Call subroutine by index."""
        index = self.rd08(self.offset + 1)
        if self.verbose:
            print(f" SUB #{index}")
        self.offset += 2
        scripts = self.find_scripts()
        if index < len(scripts):
            save = self.offset
            self.offset = scripts[index]
            self.nested += 1
            self._run()
            self.nested -= 1
            self.offset = save

    def _op_macro(self):
        """0x6f: Execute macro (indexed register write from macro table)."""
        index = self.rd08(self.offset + 1)
        if self.verbose:
            print(f" MACRO #{index} (SKIPPED — no macro table)")
        self.offset += 2

    def _op_done(self):
        """0x71: End of script."""
        if self.verbose:
            print(f" DONE")
        self.offset += 1

    def _op_reserved(self):
        """0x92/0xaa: Reserved/NOP."""
        if self.verbose:
            print(f" RESERVED")
        self.offset += 1

    def _op_generic_condition(self):
        """0x3a: Generic condition check."""
        cond = self.rd08(self.offset + 1)
        if self.verbose:
            print(f" GENERIC_CONDITION {cond} (assuming MET)")
        self.offset += 2
        # Without full condition tables, assume met

    def _op_gpio(self):
        """0x8e: GPIO setup (skip — no GPIO on eGPU)."""
        if self.verbose:
            print(f" GPIO (SKIPPED)")
        # GPIO entries are variable length — read the count
        self.offset += 2  # Simplified

    def _op_ltime(self):
        """0x57: Long time delay."""
        msec = self.rd16(self.offset + 1)
        if self.verbose:
            print(f" LTIME {msec} ms")
        self.offset += 3
        if self.execute:
            time.sleep(msec / 1000)

    # ── Opcode dispatch table ──────────────────────────────────

    _opcodes = {
        0x33: _op_repeat,
        0x36: _op_end_repeat,
        0x37: _op_copy,
        0x38: _op_not,
        0x3a: _op_generic_condition,
        0x47: _op_andn_reg,
        0x48: _op_or_reg,
        0x57: _op_ltime,
        0x58: _op_zm_reg_sequence,
        0x5b: _op_sub_direct,
        0x69: _op_io,
        0x6b: _op_sub,
        0x6e: _op_nv_reg,
        0x6f: _op_macro,
        0x71: _op_done,
        0x72: _op_resume,
        0x74: _op_time,
        0x75: _op_condition,
        0x79: _op_pll,
        0x7a: _op_zm_reg,
        0x8e: _op_gpio,
        0x91: _op_zm_reg_group,
        0x92: _op_reserved,
        0xaa: _op_reserved,
    }


def main():
    """Run devinit scripts from extracted VBIOS."""
    import argparse
    parser = argparse.ArgumentParser(description="NVIDIA VBIOS devinit interpreter")
    parser.add_argument("rom", help="Path to VBIOS ROM file")
    parser.add_argument("--script", type=int, default=-1, help="Script index (-1 = all)")
    parser.add_argument("--dry-run", action="store_true", help="Log writes without executing")
    parser.add_argument("--quiet", action="store_true", help="Suppress opcode-level output")
    args = parser.parse_args()

    rom = open(args.rom, "rb").read()
    print(f"[devinit] ROM: {len(rom)} bytes")

    if args.dry_run:
        # Dry run with a mock GPU
        class MockGPU:
            def rd32(self, reg): return 0
            def wr32(self, reg, val): pass
        gpu = MockGPU()
    else:
        from transport.tinygrad_transport import PascalGPU
        gpu = PascalGPU()

    interp = DevinitInterpreter(gpu, rom, verbose=not args.quiet, dry_run=args.dry_run)

    scripts = interp.find_scripts()
    print(f"[devinit] Found {len(scripts)} scripts:")
    for i, addr in enumerate(scripts):
        print(f"  Script {i}: ROM offset 0x{addr:04x}")

    if args.script >= 0:
        interp.run_script(args.script)
    else:
        interp.run_all_scripts()


if __name__ == "__main__":
    main()
