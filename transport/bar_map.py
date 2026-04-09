#!/usr/bin/env python3
"""Map PCIe BARs on macOS via IOKit.

Maps BAR0 (MMIO registers) of an NVIDIA GPU to userspace memory.
This requires either:
  - TinyGPU.app DriverKit extension (recommended)
  - Disabled SIP + IOKit kext
  - Root + IOPCIDevice API

For initial testing, we try IOKit's IOPCIDevice memory mapping.
"""

import ctypes
import ctypes.util
import mmap
import os
import struct

# IOKit framework
iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

kCFAllocatorDefault = ctypes.c_void_p.in_dll(cf, "kCFAllocatorDefault")
kCFStringEncodingUTF8 = 0x08000100
kIOMapAnywhere = 0x01

# Additional IOKit signatures for memory mapping
iokit.IOServiceOpen.restype = ctypes.c_int
iokit.IOServiceOpen.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]

iokit.IOServiceClose.restype = ctypes.c_int
iokit.IOServiceClose.argtypes = [ctypes.c_uint]

iokit.IOConnectMapMemory64.restype = ctypes.c_int
iokit.IOConnectMapMemory64.argtypes = [
    ctypes.c_uint,      # connection
    ctypes.c_uint,      # memoryType
    ctypes.c_uint,      # intoTask (mach_task_self)
    ctypes.POINTER(ctypes.c_uint64),  # atAddress
    ctypes.POINTER(ctypes.c_uint64),  # ofSize
    ctypes.c_uint,      # options
]

# Get mach_task_self
libc = ctypes.CDLL(ctypes.util.find_library("c"))
libc.mach_task_self.restype = ctypes.c_uint

# IORegistryEntry property reading
cf.CFStringCreateWithCString.restype = ctypes.c_void_p
cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]

cf.CFDataGetLength.restype = ctypes.c_long
cf.CFDataGetLength.argtypes = [ctypes.c_void_p]

cf.CFDataGetBytes.restype = None
cf.CFDataGetBytes.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_long, ctypes.c_void_p]

cf.CFGetTypeID.restype = ctypes.c_ulong
cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
cf.CFDataGetTypeID.restype = ctypes.c_ulong
cf.CFNumberGetTypeID.restype = ctypes.c_ulong
cf.CFNumberGetValue.restype = ctypes.c_bool
cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]

cf.CFRelease.restype = None
cf.CFRelease.argtypes = [ctypes.c_void_p]

iokit.IORegistryEntryCreateCFProperty.restype = ctypes.c_void_p
iokit.IORegistryEntryCreateCFProperty.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]


def _read_prop_data(svc, key):
    """Read raw bytes from an IORegistry CFData property."""
    cfkey = cf.CFStringCreateWithCString(kCFAllocatorDefault, key.encode(), kCFStringEncodingUTF8)
    if not cfkey:
        return None
    cfdata = iokit.IORegistryEntryCreateCFProperty(svc, cfkey, kCFAllocatorDefault, 0)
    cf.CFRelease(cfkey)
    if not cfdata:
        return None

    if cf.CFGetTypeID(cfdata) != cf.CFDataGetTypeID():
        cf.CFRelease(cfdata)
        return None

    length = cf.CFDataGetLength(cfdata)
    buf = (ctypes.c_uint8 * length)()
    cf.CFDataGetBytes(cfdata, 0, length, buf)
    cf.CFRelease(cfdata)
    return bytes(buf)


def get_bar_info(svc):
    """Read BAR (Base Address Register) info from IOPCIDevice properties.

    Returns list of (bar_index, phys_addr, size) tuples.
    """
    bars = []

    # IOPCIDevice stores BAR info in "assigned-addresses" property
    # Format: array of 5-cell entries (each cell 4 bytes = 20 bytes per entry)
    #   cell[0]: phys.hi (encoded with bar index, space type)
    #   cell[1]: phys.mid (always 0 for 32-bit)
    #   cell[2]: phys.lo (physical address low)
    #   cell[3]: size.hi
    #   cell[4]: size.lo
    data = _read_prop_data(svc, "assigned-addresses")
    if not data:
        return bars

    entry_size = 20  # 5 x 4 bytes
    for i in range(0, len(data), entry_size):
        if i + entry_size > len(data):
            break
        phys_hi, phys_mid, phys_lo, size_hi, size_lo = struct.unpack_from('<IIIII', data, i)

        # Extract BAR number from phys_hi
        # Bits [12:10] = register number / 4, so BAR index = (reg - 0x10) / 4
        reg = ((phys_hi >> 8) & 0xff)
        bar_idx = (reg - 0x10) // 4 if reg >= 0x10 else reg

        phys_addr = (phys_mid << 32) | phys_lo
        size = (size_hi << 32) | size_lo

        space_type = phys_hi & 0x3  # 0=config, 1=IO, 2=mem32, 3=mem64
        prefetchable = bool(phys_hi & (1 << 30))

        bars.append({
            "index": bar_idx,
            "phys_addr": phys_addr,
            "size": size,
            "space_type": space_type,
            "prefetchable": prefetchable,
            "reg_offset": reg,
        })

    return bars


def try_map_bar_ioconnect(svc, bar_type=0):
    """Attempt to map a BAR using IOServiceOpen + IOConnectMapMemory64.

    This typically requires a matching user client in the driver.
    Without a custom DriverKit extension, this will likely fail with kIOReturnNotPermitted.
    """
    connection = ctypes.c_uint()
    task = libc.mach_task_self()

    # Try opening a connection (type 0 = default)
    ret = iokit.IOServiceOpen(svc, task, 0, ctypes.byref(connection))
    if ret != 0:
        return None, f"IOServiceOpen failed: {ret:#x} (kIOReturnNotPermitted={0xe00002c1:#x})"

    addr = ctypes.c_uint64(0)
    size = ctypes.c_uint64(0)

    ret = iokit.IOConnectMapMemory64(connection, bar_type, task, ctypes.byref(addr), ctypes.byref(size), kIOMapAnywhere)
    if ret != 0:
        iokit.IOServiceClose(connection)
        return None, f"IOConnectMapMemory64 failed: {ret:#x}"

    return (addr.value, size.value, connection.value), None


def main():
    """Probe BAR info for the NVIDIA GPU."""
    from pci_scan import find_nvidia_gpus

    gpus = find_nvidia_gpus()
    pascal_gpus = [g for g in gpus if g.get("is_pascal")]

    if not pascal_gpus:
        print("No Pascal GPU found!")
        return

    gpu = pascal_gpus[0]
    svc = gpu["service"]
    print(f"=== BAR Info for {gpu['gpu_name']} (0x{gpu['device_id']:04x}) ===\n")

    bars = get_bar_info(svc)
    if not bars:
        print("No BAR info found in 'assigned-addresses' property.")
        print("Trying 'ranges' property...")
        # TODO: try alternative property names
    else:
        for bar in bars:
            space_names = {0: "config", 1: "I/O", 2: "mem32", 3: "mem64"}
            print(f"BAR {bar['index']}:")
            print(f"  Physical Address: 0x{bar['phys_addr']:016x}")
            print(f"  Size:            {bar['size'] / (1024*1024):.1f} MB ({bar['size']:#x})")
            print(f"  Type:            {space_names.get(bar['space_type'], 'unknown')}")
            print(f"  Prefetchable:    {bar['prefetchable']}")
            print(f"  Register:        0x{bar['reg_offset']:02x}")
            print()

    # Try direct memory mapping (will likely need TinyGPU or DriverKit)
    print("=== Attempting BAR0 Memory Map ===\n")
    result, err = try_map_bar_ioconnect(svc, bar_type=0)
    if result:
        addr, size, conn = result
        print(f"BAR0 mapped at 0x{addr:016x}, size {size} bytes")
        # Try reading PMC_BOOT_0
        mmio = ctypes.cast(addr, ctypes.POINTER(ctypes.c_uint32))
        boot_0 = mmio[0]  # offset 0x000000
        print(f"PMC_BOOT_0 = 0x{boot_0:08x}")
    else:
        print(f"Direct mapping failed: {err}")
        print("\nThis is expected without TinyGPU.app or a custom DriverKit extension.")
        print("The GPU is visible on PCIe — we just need privileged BAR access.")
        print("\nNext steps:")
        print("  1. Install TinyGPU.app (from tinygrad) for DriverKit-based BAR mapping")
        print("  2. Or: write a custom DriverKit DEXT that matches on 0x10de:0x1c03")
        print("  3. Or: use the BAR physical addresses above with /dev/mem (requires SIP disabled)")

        if bars:
            bar0 = next((b for b in bars if b["index"] == 0), None)
            if bar0:
                print(f"\n  BAR0 physical address for manual mapping: 0x{bar0['phys_addr']:016x}")
                print(f"  BAR0 size: {bar0['size']:#x} ({bar0['size'] // (1024*1024)} MB)")


if __name__ == "__main__":
    main()
