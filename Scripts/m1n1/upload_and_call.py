#!/usr/bin/env python3
import sys, pathlib
sys.path.append(str(pathlib.Path.home() /
    "Projects/AsahiLinux/m4/m1n1/proxyclient"))
from m1n1.setup import *   # gives u, p, iface, IODEV

if len(sys.argv) < 2:
    sys.exit("usage: upload_and_call.py <kernel.bin>")
kernel = pathlib.Path(sys.argv[1]).read_bytes()
size   = len(kernel)

addr = u.memalign(0x4000, size)
print(f"[host] uploading {size} bytes to 0x{addr:x}")
u.compressed_writemem(addr, kernel, True)
p.dc_cvau(addr, size)
p.ic_ivau(addr, size)

print(f"[host] p.call(0x{addr:x}, 0, 0, 0)")
ret = p.call(addr, 0, 0, 0)
print(f"[host] kmain returned 0x{ret:x}")
