#!/bin/bash
#
# chainload.sh -- OLD-STYLE raw chainload of build/kernel.bin.
#
# This uses m1n1's `chainload.py -r -E 0`, which REPLACES m1n1
# with our binary in memory. m1n1 dies; USB-CDC dies with it; no
# return path. After the payload runs, you must power-cycle the
# M4 to get m1n1 (and /dev/ttyACM{0,1}) back.
#
# The normal fast-iteration path is Scripts/m1n1/smoke_test.sh,
# which uses p.call() and leaves m1n1 alive. Only use this script
# if you specifically need the full-replacement path.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
M1N1_DIR="${M1N1_DIR:-$HOME/Projects/AsahiLinux/m4/m1n1}"

if [ ! -c /dev/ttyACM0 ]; then
    echo "error: /dev/ttyACM0 missing" >&2
    echo "       Power-cycle the M4 (long-press off, short-press on)." >&2
    exit 1
fi

if [ ! -f "$REPO_ROOT/build/kernel.bin" ]; then
    echo "error: $REPO_ROOT/build/kernel.bin not found; run 'make' first" >&2
    exit 1
fi

if [ ! -f "$M1N1_DIR/proxyclient/tools/chainload.py" ]; then
    echo "error: chainload.py not found under $M1N1_DIR" >&2
    echo "       Set M1N1_DIR env var to override." >&2
    exit 1
fi

echo "NOTE: raw chainload REPLACES m1n1. USB-CDC will die on jump."
echo "      You will need to power-cycle the M4 afterwards."
sleep 1

sudo -E env "PATH=$PATH" M1N1DEVICE=/dev/ttyACM0 \
    python3 "$M1N1_DIR/proxyclient/tools/chainload.py" -r -E 0 \
    "$REPO_ROOT/build/kernel.bin"
