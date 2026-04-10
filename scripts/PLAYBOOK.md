# Pascal eGPU Bringup — Playbook

The 10-minute sequence to try the patched TinyGPU with PERST# reset chain.

Everything is prepared. Just follow these steps.

## Current state (verified)

- eGPU is connected, GTX 1060 responds: `PMC_BOOT_0 = 0x136000a1`
- Patched TinyGPU source in place (`WarmResetDisable` chain added)
- Patched dext already built and verified at `extra/usbgpu/tbgpu/installer/build/Debug/TinyGPU.app` (will be rebuilt by install script anyway — that's fine)
- Test script ready: `pascal-egpu/hw/reset_and_attack.py`
- Master runner ready: `pascal-egpu/scripts/full_bringup_attempt.sh`
- SIP is currently **enabled** → needs to be disabled for the install

## Step 1 — Disable SIP (3 minutes)

1. **Save any unsaved work.** You're about to reboot.
2. **Apple menu → Shut Down.** Wait until the Mac is fully off (screen dark, fans silent).
3. **Press and HOLD the power button.** Keep holding even after the screen lights up.
4. Wait until the screen shows **"Loading startup options…"** then a disk picker with an **Options** gear icon.
5. **Click Options → Continue.** If asked to pick a user, pick yours and enter the password.
6. From the **menu bar at the top: Utilities → Terminal**.
7. In that Terminal, type:
   ```
   csrutil disable
   ```
   Press enter, type `y` if prompted, enter your password if asked. You'll see:
   ```
   Successfully disabled System Integrity Protection.
   ```
8. **Apple menu → Restart.** Mac reboots back to normal macOS.

Once you're back at your desktop, you can verify with `csrutil status` — it should say "disabled."

## Step 2 — Power-cycle the eGPU (30 seconds)

The GPU was in some state during all this. Give it a clean start.

1. Switch the Razer Core X V2 off (power switch on the back)
2. Wait ~10 seconds
3. Switch it back on
4. Wait ~10 seconds for Thunderbolt to re-enumerate

## Step 3 — Run the master script (2 minutes)

Open a normal Terminal and run:

```bash
cd /Users/tom/dev/pascal-egpu
./scripts/full_bringup_attempt.sh
```

This script will:
1. Verify SIP is off
2. Verify the GPU is reachable
3. Verify the source has the patch
4. Run `install_nosip.sh` which does: xcodebuild clean build → codesign ad-hoc → copy to /Applications → activate the SystemExtension
5. Wait for the new dext to load
6. Verify the installed binary has the PERST# reset chain
7. Run `hw/reset_and_attack.py` which does: snapshot → reset → race-attack unlock → multi-snapshot → diff → report

**If macOS pops up a "System Extension Blocked" dialog during step 4:**
- Click "Open System Settings"
- Go to Privacy & Security
- Scroll to the bottom, find a "System extension from tinygrad…" message
- Click "Allow"
- The script will detect approval and continue

## Step 4 — Read the output

Look for:

**Best case (★★★):**
```
★★★ PMU_SCTL: 0x00003002 → 0x00000000  UCODE_LEVEL DROPPED
★★★ PPRIV_FBP0 BECAME ALIVE: 0xbadf3000 → 0x...
>>> AT LEAST PARTIAL VICTORY <<<
```
This means the PERST# reset actually reset Pascal's security latches. Next step would be loading FECS firmware and running devinit.

**Middle case:**
```
tinygpu: reset via HotReset (bridge SBR)
```
PERST# wasn't supported by Apple's platform, fell back to HotReset. Check the diff — some registers may have changed but protected regions stayed locked. This tells us Apple doesn't implement PERST# for TB devices.

**Worst case:**
```
No protected region unlocked.
Nothing changed at all. Reset was a no-op.
```
None of the reset types did anything. We've definitively ruled out the Mac path and it's time to decide whether to pursue Windows internal install or park the card.

**If the GPU goes unresponsive** (everything reads 0xffffffff):
1. Don't panic — this has happened before and is always recoverable
2. Power-cycle the Razer Core (off, wait 10s, on)
3. Re-run `python3 -c "import sys; sys.path.insert(0, '.'); from transport.tinygrad_transport import PascalGPU; g = PascalGPU(); print(hex(g.rd32(0)))"`
4. Should come back to `0x136000a1`

## Step 5 — Re-enable SIP (optional, any time)

When you're done experimenting:

1. Shut down
2. Boot to Recovery the same way (hold power)
3. Options → Continue → Utilities → Terminal
4. `csrutil enable`
5. Restart

The patched TinyGPU dext will KEEP WORKING with SIP enabled as long as you don't reboot and unload it. Once you reboot with SIP on, macOS will refuse to load it again and you'd fall back to the stock TinyGPU — which means `gpu.dev.reset()` becomes a no-op again, but everything else still works.

If you want the patch to persist across reboots with SIP on, you'd need Apple Developer signing. Skip that unless this turns into a real project.

## What each file does (for reference)

| File | Purpose |
|---|---|
| `tinygrad-pascal/extra/usbgpu/tbgpu/installer/TinyGPUDriverExtension/TinyGPUDriver.cpp` | Patched dext source. Line 188 `ResetDevice()` chain WarmResetDisable→Enable→WarmReset→HotReset→FLR |
| `tinygrad-pascal/extra/usbgpu/tbgpu/installer/install_nosip.sh` | Build + ad-hoc codesign + install + activate |
| `pascal-egpu/hw/reset_and_attack.py` | Python: snapshot state, reset, race-attack, multi-snapshot, diff, classify |
| `pascal-egpu/scripts/full_bringup_attempt.sh` | The single wrapper that runs all of the above in sequence |
| `pascal-egpu/hw/reset_result_*.json` | Structured results for each run, for comparing across attempts |

## If the answer is "it worked"

Then we have unsigned Pascal compute over Thunderbolt on Apple Silicon. Celebrate. Then:
1. Load the real FECS firmware into its IMEM (we already have the code: `pascal-egpu/hw/fecs_bootstrap.py`)
2. Run the devinit script via the interpreter (`pascal-egpu/hw/devinit_run.py`) — now that priv levels are open, the dead writes should succeed
3. Build the PGraph context + first compute channel
4. Hook into tqbridge as a real GPU compute node

## If the answer is "it didn't work"

Document the result in `pascal-egpu/FINDINGS.md` and Obsidian, re-enable SIP, move on.
The 1060 either stays parked for a future need, or goes into a Windows desktop internally as a standard CUDA card.
