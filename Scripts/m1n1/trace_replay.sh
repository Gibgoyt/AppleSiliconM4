#!/bin/bash
#
# trace_replay.sh -- RUN T/U: replay Yureka's macOS MMIO trace (pcie.log)
# against a running m1n1 on the M4 mini. See trace_replay.py.
#
# RUN T (default): unlock preamble (trace lines 1-34) + phy_ip probe.
#   ./Scripts/m1n1/trace_replay.sh
#
# RUN U (after RUN T passes): continue through phy_ip programming and
# port-2 bring-up to link-up + ECAM:
#   ./Scripts/m1n1/trace_replay.sh --segments preamble,phyip,port2 --ecam
#
# Output: /tmp/m4-recon/nic-runtime.txt

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${OUT_DIR:-/tmp/m4-recon}"

# Find the m1n1 proxy port by USB product string (same logic as perstn.sh:
# another CDC-ACM device can steal /dev/ttyACM0).
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
    echo "       Is the M4 booted into m1n1? If it is on a known port, set" >&2
    echo "       M1N1DEVICE=/dev/ttyACMx explicitly. Otherwise long-press" >&2
    echo "       power on the M4 to force off, then short-press to boot." >&2
    exit 1
fi

echo "using m1n1 device: $M1N1DEVICE"

mkdir -p "$OUT_DIR"

# The m1n1 checkout lives at ~/Projects/C/embedded/m1n1 (m4_common.py's
# parents[3] guess predates that layout); no sudo needed -- we're in uucp.
export PYTHONPATH="${M1N1_PROXYCLIENT:-$HOME/Projects/C/embedded/m1n1/proxyclient}${PYTHONPATH:+:$PYTHONPATH}"

M1N1DEVICE="$M1N1DEVICE" \
    python3 "$SCRIPT_DIR/trace_replay.py" --out "$OUT_DIR" \
    --require-build=v1.6.0-40-g "$@"

echo
echo "--- $OUT_DIR/nic-runtime.txt ---"
cat "$OUT_DIR/nic-runtime.txt"
