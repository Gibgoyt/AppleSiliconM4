#!/bin/bash
#
# nic_bringup.sh -- PLAN_3 Phase 3.5 wrapper. Runs nic_bringup.py against a
# running m1n1 -- auto-detecting the uartproxy port by USB product string
# (M1N1DEVICE overrides). Another CDC-ACM device can steal ttyACM0.
#
# RUN 32: full PCIe bring-up, then program bridge bus numbers and descend
# behind pci-bridge2 to read the NIC's VID:DID + class + BAR0. Sibling of
# perstn.sh / soc_bringup.sh; all three share the low-level primitives in
# m4_common.py + pcie_common.py.
#
# Usage:
#   ./Scripts/m1n1/nic_bringup.sh [nic_bringup.py args...]
#
# Output: /tmp/m4-recon/nic-runtime.txt

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${OUT_DIR:-/tmp/m4-recon}"

# Find the m1n1 proxy port by USB product string. Another CDC-ACM device (e.g.
# a Samsung phone) can grab /dev/ttyACM0 and push m1n1 to ACM1/ACM2 -- talking
# the proxy protocol to the wrong port yields "Reply checksum error" /
# UartChecksumError. Honor an explicit M1N1DEVICE override; else auto-detect.
find_m1n1_dev() {
    local n dev prod
    for n in 0 1 2 3 4 5; do
        dev="/dev/ttyACM$n"
        [ -c "$dev" ] || continue
        prod="$(cat "/sys/class/tty/ttyACM$n/device/../product" 2>/dev/null \
                || cat "/sys/class/tty/ttyACM$n/device/product" 2>/dev/null \
                || true)"
        case "$prod" in
            *"m1n1 uartproxy"*) echo "$dev"; return 0 ;;
        esac
    done
    return 1
}

M1N1DEVICE="${M1N1DEVICE:-$(find_m1n1_dev || true)}"

if [ -z "${M1N1DEVICE:-}" ] || [ ! -c "$M1N1DEVICE" ]; then
    echo "error: no m1n1 uartproxy device found on any /dev/ttyACM*" >&2
    echo "       (checked USB product strings for 'm1n1 uartproxy')" >&2
    echo "       Is the M4 booted into m1n1? If it is on a known port, set" >&2
    echo "       M1N1DEVICE=/dev/ttyACMx explicitly. Otherwise long-press" >&2
    echo "       power on the M4 to force off, then short-press to boot." >&2
    exit 1
fi

echo "using m1n1 device: $M1N1DEVICE"

mkdir -p "$OUT_DIR"

sudo -E env "PATH=$PATH" M1N1DEVICE="$M1N1DEVICE" \
    python3 "$SCRIPT_DIR/nic_bringup.py" --out "$OUT_DIR" "$@"

echo
echo "--- $OUT_DIR/nic-runtime.txt ---"
cat "$OUT_DIR/nic-runtime.txt"
