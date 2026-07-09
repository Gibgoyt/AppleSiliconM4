#!/bin/bash
#
# perstn.sh -- Phase 3 PCIe bring-up wrapper. Runs perstn.py against
# a running m1n1 on /dev/ttyACM0.
#
# REQUIRES: m1n1 patched with the apcie,t8132 clause (t8132-pcie branch
#           of ~/Projects/AsahiLinux/m4/m1n1/). On stock m4-bringup m1n1
#           the pcie_init step will fail and the ECAM reads will SLVERR.
#
# Usage:
#   ./Scripts/m1n1/perstn.sh
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
    python3 "$SCRIPT_DIR/perstn.py" --out "$OUT_DIR" "$@"

echo
echo "--- $OUT_DIR/nic-runtime.txt ---"
cat "$OUT_DIR/nic-runtime.txt"
