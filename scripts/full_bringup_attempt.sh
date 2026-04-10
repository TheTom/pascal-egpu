#!/bin/bash
# Full Pascal eGPU bringup attempt runner.
#
# PRE-REQUISITES (Tom does these once):
#   1. SIP disabled (boot to Recovery → Terminal → csrutil disable → reboot)
#   2. eGPU plugged in and powered on
#   3. This script run from a normal Terminal session
#
# This script:
#   a. Verifies SIP is off
#   b. Verifies GPU is reachable
#   c. Verifies patched source is in place
#   d. Runs install_nosip.sh to build + codesign + install + activate
#   e. Waits for the new dext to be loaded
#   f. Checks that the SystemExtension was approved (or prompts for approval)
#   g. Runs the reset+attack test
#
# After it runs, look at the output for "★★★" markers indicating
# any protected region that became unlocked.

set -e

PASCAL_REPO="/Users/tom/dev/pascal-egpu"
TINYGRAD_REPO="/Users/tom/dev/tinygrad-pascal"
INSTALLER_DIR="$TINYGRAD_REPO/extra/usbgpu/tbgpu/installer"

echo "=========================================================================="
echo "  Pascal eGPU full bringup attempt"
echo "=========================================================================="

# --- Step 1: SIP check ------------------------------------------------------
echo ""
echo "[1/7] Checking SIP status..."
SIP=$(csrutil status 2>&1 || true)
if [[ "$SIP" == *"enabled"* ]]; then
    echo "  ERROR: SIP is enabled."
    echo "  You must disable SIP first:"
    echo "    1. Shut down the Mac"
    echo "    2. Hold power button until 'Loading startup options' appears"
    echo "    3. Click Options → Continue, enter password"
    echo "    4. Menu bar: Utilities → Terminal"
    echo "    5. Run: csrutil disable"
    echo "    6. Apple menu → Restart"
    echo "    7. After reboot, re-run this script"
    exit 1
fi
echo "  ✓ SIP is disabled"

# --- Step 2: GPU reachability check ----------------------------------------
echo ""
echo "[2/7] Checking GPU is reachable..."
cd "$PASCAL_REPO"
GPU_ALIVE=$(python3 -c "
import sys
sys.path.insert(0, '.')
from transport.tinygrad_transport import PascalGPU
try:
    g = PascalGPU()
    boot = g.rd32(0)
    g.close()
    print(f'{boot:08x}')
except Exception as e:
    print(f'ERR:{e}')
" 2>&1 | tail -1)

if [[ "$GPU_ALIVE" == ERR:* ]]; then
    echo "  ERROR: Cannot reach GPU: $GPU_ALIVE"
    echo "  Check that:"
    echo "    - Razer Core X V2 is powered on and TB cable connected"
    echo "    - GTX 1060 is seated in the enclosure"
    echo "    - TinyGPU.app is installed in /Applications"
    exit 2
fi

if [[ "$GPU_ALIVE" == "ffffffff" ]]; then
    echo "  WARNING: GPU returns 0xffffffff (not responding)"
    echo "  Power-cycle the Razer Core enclosure and retry"
    exit 3
fi
echo "  ✓ GPU responds: PMC_BOOT_0 = 0x$GPU_ALIVE"

if [[ "$GPU_ALIVE" != "136000a1" ]]; then
    echo "  (Expected 0x136000a1 for GTX 1060 GP106 rev A1, got 0x$GPU_ALIVE — unusual but continuing)"
fi

# --- Step 3: Source patch check --------------------------------------------
echo ""
echo "[3/7] Verifying patched source is in place..."
if ! grep -q "WarmResetDisable" "$INSTALLER_DIR/TinyGPUDriverExtension/TinyGPUDriver.cpp"; then
    echo "  ERROR: Patch missing from TinyGPUDriver.cpp"
    echo "  Source at $INSTALLER_DIR/TinyGPUDriverExtension/TinyGPUDriver.cpp"
    exit 4
fi
echo "  ✓ Source has WarmResetDisable patch"

# --- Step 4: Build + install -----------------------------------------------
echo ""
echo "[4/7] Building and installing patched dext (runs install_nosip.sh)..."
echo "  This will do a clean xcodebuild + codesign + copy to /Applications + activate."
echo "  macOS may pop up a dialog asking to approve the system extension."
echo "  If it does: click 'Open System Settings' → Privacy & Security → Allow."
echo ""
cd "$INSTALLER_DIR"
./install_nosip.sh
echo "  ✓ Install script completed"

# --- Step 5: Wait for dext to be active ------------------------------------
echo ""
echo "[5/7] Waiting for SystemExtension to be loaded..."
for i in 1 2 3 4 5 6 7 8 9 10; do
    STATE=$(systemextensionsctl list 2>&1 | grep -i "tinygpu" | head -1 || echo "NOT_LISTED")
    if echo "$STATE" | grep -qi "activated.*enabled"; then
        echo "  ✓ Dext activated and enabled"
        break
    fi
    echo "  ($i/10) State: $STATE"
    sleep 2
done

# Final check
STATE=$(systemextensionsctl list 2>&1 | grep -i "tinygpu" || echo "NOT_LISTED")
echo "  Final state: $STATE"

if echo "$STATE" | grep -qi "waiting for user"; then
    echo ""
    echo "  ⚠ SystemExtension is waiting for user approval."
    echo "  Open System Settings → Privacy & Security."
    echo "  Look for 'System extension...was blocked from loading'."
    echo "  Click Allow, then press Enter here to continue."
    read -r
fi

# --- Step 6: Verify the new dext is the patched one ------------------------
echo ""
echo "[6/7] Verifying installed dext has all 4 reset types..."
INSTALLED_BIN="/Applications/TinyGPU.app/Contents/Library/SystemExtensions/org.tinygrad.tinygpu.driver2.dext/org.tinygrad.tinygpu.driver2"
if [ ! -f "$INSTALLED_BIN" ]; then
    echo "  ERROR: Installed binary not found at $INSTALLED_BIN"
    exit 5
fi

FOUND_RESETS=$(otool -tvV "$INSTALLED_BIN" 2>/dev/null | grep -c "mov\s*w1, #0x[48]" || true)
if [ "$FOUND_RESETS" -lt 2 ]; then
    echo "  ERROR: Installed binary doesn't contain WarmResetDisable/Enable ($FOUND_RESETS found)"
    echo "  Something went wrong during install. Try running install_nosip.sh manually:"
    echo "    cd $INSTALLER_DIR && ./install_nosip.sh"
    exit 6
fi
echo "  ✓ Installed binary has PERST# reset chain ($FOUND_RESETS mov w1 instructions found)"

# --- Step 7: Run the reset + attack test -----------------------------------
echo ""
echo "[7/7] Running reset + race-attack test..."
echo ""
cd "$PASCAL_REPO"
python3 hw/reset_and_attack.py

echo ""
echo "=========================================================================="
echo "  Done. Look above for '★★★' markers indicating unlocked regions."
echo "  Full JSON result saved under pascal-egpu/hw/reset_result_*.json"
echo "=========================================================================="
