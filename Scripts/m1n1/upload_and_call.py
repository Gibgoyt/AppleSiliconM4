#!/usr/bin/env python3
"""Upload a raw kernel.bin payload into m1n1 and p.call() it.

The kernel writes its text output into a caller-supplied buffer
(see include/log.h). We allocate that buffer here, pass its
(address, size) as the first two call args, and after the call
returns we read the buffer back and print it as ASCII.
"""

import sys, pathlib

sys.path.append(str(pathlib.Path.home() /
    "Projects/AsahiLinux/m4/m1n1/proxyclient"))

from m1n1.setup import *   # provides u, p, iface, IODEV

LOG_SIZE = 8192

if len(sys.argv) < 2:
    sys.exit("usage: upload_and_call.py <kernel.bin>")

kernel = pathlib.Path(sys.argv[1]).read_bytes()
size   = len(kernel)

# Allocate + zero the log buffer first so a garbled call still
# reads back cleanly instead of showing stale heap contents.
log_buf = u.memalign(0x40, LOG_SIZE)
iface.writemem(log_buf, b"\x00" * LOG_SIZE)

# Upload the kernel payload and flush caches for it.
kernel_addr = u.memalign(0x4000, size)
print(f"[host] uploading {size} bytes to 0x{kernel_addr:x}")
u.compressed_writemem(kernel_addr, kernel, True)
p.dc_cvau(kernel_addr, size)
p.ic_ivau(kernel_addr, size)

print(f"[host] log_buf @ 0x{log_buf:x} (size 0x{LOG_SIZE:x})")
print(f"[host] p.call(0x{kernel_addr:x}, 0x{log_buf:x}, 0x{LOG_SIZE:x}, 0)")
ret = p.call(kernel_addr, log_buf, LOG_SIZE, 0)
print(f"[host] kmain returned 0x{ret:x}")

# Read the log back and strip trailing zeros.
raw = iface.readmem(log_buf, LOG_SIZE)
nul = raw.find(b"\x00")
if nul >= 0:
    raw = raw[:nul]
try:
    text = raw.decode("utf-8", errors="replace")
except Exception:
    text = repr(raw)

print()
print("--- kernel log ---")
print(text, end="" if text.endswith("\n") else "\n")
print("--- end kernel log ---")
