#!/bin/bash
#
# recon.sh -- Phase 1 reconnaissance wrapper. Runs recon.py against a
# running m1n1 on /dev/ttyACM0 and dumps facts under /tmp/m4-recon/.
#
# Usage:
#   ./Scripts/m1n1/recon.sh                     # ADT-only, no side effects
#   ./Scripts/m1n1/recon.sh --pcie              # + SMC/pcie_init + ECAM sweep
#   ./Scripts/m1n1/recon.sh --pcie --dart-dump  # + DART page-table dumps
#
# Assumes m1n1 is running and /dev/ttyACM0 is present.

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
    python3 "$SCRIPT_DIR/recon.py" --out "$OUT_DIR" "$@"

echo
echo "--- $OUT_DIR/ ---"
ls -la "$OUT_DIR/"
echo
echo "Read $OUT_DIR/recon-summary.md and paste it back."
