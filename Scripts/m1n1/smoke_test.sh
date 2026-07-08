#!/bin/bash
#
# smoke_test.sh -- upload build/kernel.bin into m1n1, p.call() it,
# and print the kernel log (buffer readback from Python).
#
# Usage (from anywhere):
#   ./Scripts/m1n1/smoke_test.sh
#
# Assumes the M4 mini has m1n1 running and /dev/ttyACM0 is present.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [ ! -c /dev/ttyACM0 ]; then
    echo "error: /dev/ttyACM0 missing" >&2
    echo "       m1n1 USB gadget is not enumerated." >&2
    echo "       Long-press power on the M4 to force off, then short-press to boot." >&2
    exit 1
fi

if [ ! -f "$REPO_ROOT/build/kernel.bin" ]; then
    echo "error: $REPO_ROOT/build/kernel.bin not found; run 'make' first" >&2
    exit 1
fi

sudo -E env "PATH=$PATH" M1N1DEVICE=/dev/ttyACM0 \
    python3 "$SCRIPT_DIR/upload_and_call.py" "$REPO_ROOT/build/kernel.bin"
