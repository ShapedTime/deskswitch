#!/bin/bash
# probe.sh - read-only discovery for deskswitch. Run it on EACH Mac:
#   bash probe.sh [monitor-name-substring]   (default: ZQE-CAA, the name macOS gives the MateView GT 34)
# Nothing here writes to the monitor or changes settings.
set -u
NAME="${1:-ZQE-CAA}"
PATH="$PATH:/opt/homebrew/bin:/usr/local/bin:$HOME/.homebrew/bin"

hr() { printf '\n== %s ==\n' "$*"; }

hr "Mac"
sw_vers 2>/dev/null
echo "model: $(sysctl -n hw.model 2>/dev/null)  cpu: $(sysctl -n machdep.cpu.brand_string 2>/dev/null)"

hr "Displays (look at 'Connection Type' per display)"
system_profiler SPDisplaysDataType 2>/dev/null | sed -n '/Displays:/,$p'

hr "Tools"
for t in betterdisplaycli m1ddc python3 displayplacer blueutil; do
  printf '%-17s %s\n' "$t" "$(command -v "$t" || echo '-')"
done
BD=""
if command -v betterdisplaycli >/dev/null 2>&1; then
  BD="betterdisplaycli"
elif [ -x /Applications/BetterDisplay.app/Contents/MacOS/BetterDisplay ]; then
  BD="/Applications/BetterDisplay.app/Contents/MacOS/BetterDisplay"
fi
printf '%-17s %s\n' "BetterDisplay" "$(pgrep -x BetterDisplay >/dev/null && echo running || echo 'not running')"

if [ -n "$BD" ]; then
  hr "BetterDisplay: displays"
  "$BD" get -identifiers 2>&1 | head -60
  hr "BetterDisplay: DDC capabilities string for '$NAME' (look for 60( ... ) = input codes)"
  "$BD" get -nameLike="$NAME" -ddcCapabilitiesString 2>&1 | head -20
  hr "BetterDisplay: parsed capabilities / input list"
  "$BD" get -nameLike="$NAME" -ddcCapabilities 2>&1 | head -40
  "$BD" get -nameLike="$NAME" -inputSourceList 2>&1 | head -20
  hr "Current input (VCP 0x60) as seen from THIS Mac"
  "$BD" get -nameLike="$NAME" -feature=ddc -vcp=0x60 -value 2>&1
  echo "(a number here = the code of the input this Mac can talk to; failure is normal if this Mac is not on screen)"
fi

if command -v m1ddc >/dev/null 2>&1; then
  hr "m1ddc display list"
  m1ddc display list detailed 2>&1 | head -40
fi

hr "HID devices macOS currently sees (Product / Transport)"
ioreg -r -c IOHIDDevice -d 1 -l -w0 2>/dev/null | grep -E '"(Product|Transport)" =' | sed 's/^[ |]*//'

hr "Logi Options+"
pgrep -fl 'logioptionsplus' | head -5 || true
ls -l /tmp/logitech_kiros_agent-* 2>/dev/null || echo "agent socket not found"

hr "HID idle"
ioreg -c IOHIDSystem -d 4 -w0 | awk '/HIDIdleTime/ {printf "%.1f s since last input\n", $NF/1e9; exit}'
