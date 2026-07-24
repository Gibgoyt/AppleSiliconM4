#!/bin/bash
#
# soc_bringup.sh -- SoC-first bring-up wrapper. Runs soc_bringup.py against
# a running m1n1 on /dev/ttyACM0.
#
# RUN 25 onward: SMP start + companion-IOP (ACIO) recon before any PCIe work.
# Read-only unless --smp-start is passed. Sibling of perstn.sh; both share the
# low-level primitives in m4_common.py.
#
# Usage:
#   ./Scripts/m1n1/soc_bringup.sh [soc_bringup.py args...]
#
# Output: /tmp/m4-recon/nic-runtime.txt

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${OUT_DIR:-/tmp/m4-recon}"

if [ ! -c /dev/ttyACM0 ]; then
    echo "error: /dev/ttyACM0 missing" >&2
    echo "       m1n1 USB gadget is not enumerated." >&2
    echo "       Long-press power on the M4 to force off, then short-press to boot." >&2
    exit 1
fi

mkdir -p "$OUT_DIR"

sudo -E env "PATH=$PATH" M1N1DEVICE=/dev/ttyACM0 \
    python3 "$SCRIPT_DIR/soc_bringup.py" --out "$OUT_DIR" "$@"

echo
echo "--- $OUT_DIR/nic-runtime.txt ---"
cat "$OUT_DIR/nic-runtime.txt"
