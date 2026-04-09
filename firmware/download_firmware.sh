#!/bin/bash
# Download GP106 firmware blobs for Pascal eGPU
#
# Option 1: From a Linux machine with linux-firmware installed
#   cp -r /lib/firmware/nvidia/gp106/* firmware/blobs/gp106/
#   (Files will be .zst compressed — decompress with: zstd -d *.zst)
#
# Option 2: Extract from NVIDIA open-gpu-kernel-modules (requires full clone)
#   git clone https://github.com/NVIDIA/open-gpu-kernel-modules.git
#   cd open-gpu-kernel-modules
#   python3 nouveau/extract-firmware-nouveau.py -o /tmp/nvidia-fw
#   cp -r /tmp/nvidia-fw/nvidia/gp106/* /path/to/pascal-egpu/firmware/blobs/gp106/
#
# Option 3: From Arch Linux package (any machine with pacman)
#   sudo pacman -Sw linux-firmware-nvidia
#   tar -I zstd -xf /var/cache/pacman/pkg/linux-firmware-nvidia-*.pkg.tar.zst \
#     usr/lib/firmware/nvidia/gp106 --strip-components=4 -C firmware/blobs/gp106/
#   for f in firmware/blobs/gp106/**/*.zst; do zstd -d "$f" && rm "$f"; done

set -e
DEST="$(dirname "$0")/blobs/gp106"
mkdir -p "$DEST"/{acr,gr,sec2,nvdec}

echo "=== GP106 Firmware Download ==="
echo ""
echo "Firmware blobs must be obtained from one of these sources:"
echo ""
echo "1. Linux machine with linux-firmware-nvidia package:"
echo "   scp user@linux:/lib/firmware/nvidia/gp106/**/*.bin $DEST/"
echo ""
echo "2. NVIDIA open-gpu-kernel-modules (full clone ~2GB):"
echo "   git clone https://github.com/NVIDIA/open-gpu-kernel-modules.git"
echo "   python3 open-gpu-kernel-modules/nouveau/extract-firmware-nouveau.py -o /tmp/fw"
echo "   cp -r /tmp/fw/nvidia/gp106/* $DEST/"
echo ""
echo "3. Mac Mini SSH (if you have a Linux box):"
echo "   ssh tom@toms-mac-mini.local 'apt list --installed 2>/dev/null | grep firmware'"
echo ""
echo "Required files:"
echo "  acr/bl.bin, acr/ucode_load.bin, acr/ucode_unload.bin, acr/unload_bl.bin"
echo "  gr/fecs_bl.bin, gr/fecs_data.bin, gr/fecs_inst.bin, gr/fecs_sig.bin"
echo "  gr/gpccs_bl.bin, gr/gpccs_data.bin, gr/gpccs_inst.bin, gr/gpccs_sig.bin"
echo "  gr/sw_bundle_init.bin, gr/sw_ctx.bin, gr/sw_method_init.bin, gr/sw_nonctx.bin"
echo "  sec2/desc.bin, sec2/image.bin, sec2/sig.bin"
