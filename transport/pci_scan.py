#!/usr/bin/env python3
"""PCIe device scanning on macOS via IOKit.

Enumerates IOPCIDevice services to find NVIDIA GPUs connected via Thunderbolt/eGPU.
"""

import ctypes
import ctypes.util

# --- IOKit bindings ---
iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

kCFAllocatorDefault = ctypes.c_void_p.in_dll(cf, "kCFAllocatorDefault")
kCFStringEncodingUTF8 = 0x08000100

# IOKit function signatures
iokit.IOServiceMatching.restype = ctypes.c_void_p
iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]

iokit.IOServiceGetMatchingServices.restype = ctypes.c_int
iokit.IOServiceGetMatchingServices.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]

iokit.IOIteratorNext.restype = ctypes.c_uint
iokit.IOIteratorNext.argtypes = [ctypes.c_uint]

iokit.IORegistryEntryCreateCFProperty.restype = ctypes.c_void_p
iokit.IORegistryEntryCreateCFProperty.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]

iokit.IORegistryEntryGetName.restype = ctypes.c_int
iokit.IORegistryEntryGetName.argtypes = [ctypes.c_uint, ctypes.c_char_p]

iokit.IORegistryEntryGetPath.restype = ctypes.c_int
iokit.IORegistryEntryGetPath.argtypes = [ctypes.c_uint, ctypes.c_char_p, ctypes.c_char_p]

iokit.IOObjectRelease.restype = ctypes.c_int
iokit.IOObjectRelease.argtypes = [ctypes.c_uint]

# CoreFoundation bindings
cf.CFStringCreateWithCString.restype = ctypes.c_void_p
cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]

cf.CFDataGetLength.restype = ctypes.c_long
cf.CFDataGetLength.argtypes = [ctypes.c_void_p]

cf.CFDataGetBytes.restype = None
cf.CFDataGetBytes.argtypes = [ctypes.c_void_p, ctypes.c_long * 2, ctypes.c_void_p]

cf.CFRelease.restype = None
cf.CFRelease.argtypes = [ctypes.c_void_p]

cf.CFGetTypeID.restype = ctypes.c_ulong
cf.CFGetTypeID.argtypes = [ctypes.c_void_p]

cf.CFDataGetTypeID.restype = ctypes.c_ulong
cf.CFStringGetTypeID.restype = ctypes.c_ulong

cf.CFStringGetCString.restype = ctypes.c_bool
cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]


def _read_prop_int(svc, key):
    """Read an integer property from an IORegistry entry."""
    cfkey = cf.CFStringCreateWithCString(kCFAllocatorDefault, key.encode(), kCFStringEncodingUTF8)
    if not cfkey:
        return None
    cfdata = iokit.IORegistryEntryCreateCFProperty(svc, cfkey, kCFAllocatorDefault, 0)
    cf.CFRelease(cfkey)
    if not cfdata:
        return None

    # Check if it's CFData (not CFString or other)
    if cf.CFGetTypeID(cfdata) != cf.CFDataGetTypeID():
        cf.CFRelease(cfdata)
        return None

    length = cf.CFDataGetLength(cfdata)
    if length <= 0 or length > 8:
        cf.CFRelease(cfdata)
        return None
    buf = (ctypes.c_uint8 * 8)()
    # CFRange is {location, length} — pass as two separate c_long args
    cf.CFDataGetBytes.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_long, ctypes.c_void_p]
    cf.CFDataGetBytes(cfdata, 0, length, buf)
    cf.CFRelease(cfdata)
    return int.from_bytes(bytes(buf[:length]), "little")


def _read_prop_str(svc, key):
    """Read a string property from an IORegistry entry."""
    cfkey = cf.CFStringCreateWithCString(kCFAllocatorDefault, key.encode(), kCFStringEncodingUTF8)
    if not cfkey:
        return None
    cfprop = iokit.IORegistryEntryCreateCFProperty(svc, cfkey, kCFAllocatorDefault, 0)
    cf.CFRelease(cfkey)
    if not cfprop:
        return None

    if cf.CFGetTypeID(cfprop) == cf.CFStringGetTypeID():
        buf = ctypes.create_string_buffer(256)
        if cf.CFStringGetCString(cfprop, buf, 256, kCFStringEncodingUTF8):
            cf.CFRelease(cfprop)
            return buf.value.decode()

    cf.CFRelease(cfprop)
    return None


def _get_name(svc):
    """Get IORegistry entry name."""
    buf = ctypes.create_string_buffer(128)
    if iokit.IORegistryEntryGetName(svc, buf) == 0:
        return buf.value.decode()
    return "unknown"


def _get_path(svc):
    """Get IORegistry entry path."""
    buf = ctypes.create_string_buffer(512)
    if iokit.IORegistryEntryGetPath(svc, b"IOService", buf) == 0:
        return buf.value.decode()
    return "unknown"


# Known NVIDIA GPU device IDs
PASCAL_DEVICE_IDS = {
    # GP106 (GTX 1060)
    0x1c02: "GTX 1060 3GB",
    0x1c03: "GTX 1060 6GB",
    0x1c04: "GTX 1060 5GB",
    0x1c06: "GTX 1060 6GB (Rev A)",
    0x1c07: "GTX 1060 (P106-100)",
    0x1c09: "GTX 1060 (P106-090)",
    # GP104 (GTX 1070/1080)
    0x1b80: "GTX 1080",
    0x1b81: "GTX 1070",
    0x1b82: "GTX 1070 Ti",
    0x1b83: "GTX 1060 6GB (GP104)",
    0x1b84: "GTX 1060 3GB (GP104)",
    # GP107 (GTX 1050/1050Ti)
    0x1c81: "GTX 1050 Ti",
    0x1c82: "GTX 1050",
    # GP102 (GTX 1080 Ti, Titan Xp)
    0x1b00: "Titan X (Pascal)",
    0x1b02: "Titan Xp",
    0x1b06: "GTX 1080 Ti",
}

NV_VENDOR_ID = 0x10de


def scan_pci_devices(vendor_filter=None):
    """Scan all IOPCIDevice services and return device info."""
    devices = []
    iterator = ctypes.c_uint()

    matching = iokit.IOServiceMatching(b"IOPCIDevice")
    if not matching:
        raise RuntimeError("Failed to create IOPCIDevice matching dict")

    ret = iokit.IOServiceGetMatchingServices(0, matching, ctypes.byref(iterator))
    if ret != 0:
        raise RuntimeError(f"IOServiceGetMatchingServices failed: {ret}")

    while True:
        svc = iokit.IOIteratorNext(iterator)
        if not svc:
            break

        vendor_id = _read_prop_int(svc, "vendor-id")
        device_id = _read_prop_int(svc, "device-id")
        class_code = _read_prop_int(svc, "class-code")
        name = _get_name(svc)
        path = _get_path(svc)
        subsys_vendor = _read_prop_int(svc, "subsystem-vendor-id")
        subsys_id = _read_prop_int(svc, "subsystem-id")
        revision = _read_prop_int(svc, "revision-id")

        if vendor_filter and vendor_id != vendor_filter:
            iokit.IOObjectRelease(svc)
            continue

        dev_info = {
            "service": svc,
            "name": name,
            "path": path,
            "vendor_id": vendor_id,
            "device_id": device_id,
            "class_code": class_code,
            "subsys_vendor": subsys_vendor,
            "subsys_id": subsys_id,
            "revision": revision,
        }
        devices.append(dev_info)
        # Don't release svc — caller may need it

    return devices


def find_nvidia_gpus():
    """Find all NVIDIA GPUs on the PCIe bus."""
    devices = scan_pci_devices(vendor_filter=NV_VENDOR_ID)
    gpus = []
    for dev in devices:
        did = dev["device_id"]
        gpu_name = PASCAL_DEVICE_IDS.get(did, f"Unknown NVIDIA (0x{did:04x})" if did else "Unknown")
        dev["gpu_name"] = gpu_name
        dev["is_pascal"] = did in PASCAL_DEVICE_IDS if did else False
        gpus.append(dev)
    return gpus


def main():
    print("=== PCIe Device Scan (NVIDIA) ===\n")

    gpus = find_nvidia_gpus()

    if not gpus:
        print("No NVIDIA GPUs found on PCIe bus.")
        print("\nChecking ALL PCIe devices...")
        all_devs = scan_pci_devices()
        print(f"Found {len(all_devs)} total PCIe devices:")
        for dev in all_devs:
            vid = dev["vendor_id"]
            did = dev["device_id"]
            cc = dev["class_code"]
            # class_code upper byte is base class: 0x03 = display controller
            base_class = (cc >> 16) & 0xff if cc else 0
            print(f"  {dev['name']:30s} vendor=0x{vid:04x} device=0x{did:04x} class=0x{cc:06x} {'[DISPLAY]' if base_class == 0x03 else ''}")
        return

    print(f"Found {len(gpus)} NVIDIA device(s):\n")
    for i, gpu in enumerate(gpus):
        print(f"GPU #{i}:")
        print(f"  Name:      {gpu['gpu_name']}")
        print(f"  Device ID: 0x{gpu['device_id']:04x}")
        print(f"  Vendor ID: 0x{gpu['vendor_id']:04x}")
        print(f"  Revision:  0x{gpu['revision']:02x}" if gpu['revision'] is not None else "  Revision:  N/A")
        print(f"  Class:     0x{gpu['class_code']:06x}" if gpu['class_code'] else "  Class:     N/A")
        print(f"  Path:      {gpu['path']}")
        print(f"  Pascal:    {'YES' if gpu['is_pascal'] else 'NO'}")
        if gpu.get('subsys_vendor'):
            print(f"  Subsystem: 0x{gpu['subsys_vendor']:04x}:0x{gpu['subsys_id']:04x}")
        print()


if __name__ == "__main__":
    main()
