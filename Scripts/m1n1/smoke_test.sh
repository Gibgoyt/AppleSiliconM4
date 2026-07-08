#!/bin/bash
#
# smoke_test.sh -- upload build/kernel.bin into m1n1, p.call() it,
# and capture the kernel banner from /dev/ttyACM1.
#
# Usage (from anywhere):
#   ./Scripts/m1n1/smoke_test.sh
#
# Assumes the M4 mini has m1n1 running and USB is enumerated, i.e.
# /dev/ttyACM0 (proxy protocol) and /dev/ttyACM1 (raw dockchannel)
# are both present.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

for tty in /dev/ttyACM0 /dev/ttyACM1; do
    if [ ! -c "$tty" ]; then
        echo "error: $tty missing" >&2
        echo "       m1n1 USB gadget is not enumerated." >&2
        echo "       Long-press power on the M4 to force off, then short-press to boot." >&2
        exit 1
    fi
done

if [ ! -f "$REPO_ROOT/build/kernel.bin" ]; then
    echo "error: $REPO_ROOT/build/kernel.bin not found; run 'make' first" >&2
    exit 1
fi

LOG="$(mktemp -t m4-ttyACM1.XXXXXX.log)"

# Start background capture of /dev/ttyACM1 (dockchannel raw). We
# sudo the reader so we don't need udev group changes to succeed;
# the trap below tears it down whatever way this script exits.
sudo cat /dev/ttyACM1 >"$LOG" &
CAT_PID=$!

cleanup() {
    # Give the last few TX bytes a moment to arrive on the host.
    sleep 0.3
    sudo kill "$CAT_PID" 2>/dev/null || true
    wait "$CAT_PID" 2>/dev/null || true
    echo
    echo "--- /dev/ttyACM1 (kernel dockchannel output) ---"
    cat "$LOG"
    echo "--- end /dev/ttyACM1 ---"
    rm -f "$LOG"
}
trap cleanup EXIT

# Give the background reader a moment to attach before we upload.
sleep 0.3

sudo -E env "PATH=$PATH" M1N1DEVICE=/dev/ttyACM0 \
    python3 "$SCRIPT_DIR/upload_and_call.py" "$REPO_ROOT/build/kernel.bin"
