#!/usr/bin/env python3
"""Phase 3 -- PCIe bring-up + NIC PERSTN toggle + ECAM walk on the M4 mini.

Same shape as pcie_up.py but with a PERSTN GPIO poke inserted between
SMC power-on and p.pcie_init(). On j773g the NIC on pci-bridge2 has an
external PERSTN wired to gpio0 pin 165 (see m4_recon/nic-adt.txt):
    function_perst = GPIO(phandle=120 -> gpio0, args=[165, 0])

Without this poke, m1n1's pcie_init sets the internal T602X port reset
but the endpoint stays in reset, LTSSM never trains, LINKSTS stays
BUSY on port 2, and the ECAM walk sees all-0xff.

Sequence:
    1. SMC power writes (gP0d=0x800001, gP1a=1).
    2. Deassert PERSTN on gpio0 pin 165 (unless --no-perstn).
    3. p.pcie_init().
    4. dump_pcie_regs() -- controller-side state snapshot.
    5. ECAM walk of /arm-io/apcie.
    6. Enable MEM+BM on the class-0x02 NIC, capture BAR0.

Every ECAM config read/write is wrapped in try/except so a partial
link-up leaves /tmp/m4-recon/nic-runtime.txt behind rather than a
silent SLVERR reboot.

Apple GPIO register layout (sourced from Linux
drivers/pinctrl/pinctrl-apple-gpio.c):
    reg = gpio_base + pin * 4
    bit 0            DATA          (output value)
    bits 3:1         MODE          (1 = OUT, 7 = IN_IRQ_OFF, ...)
    bits 6:5         PERIPH        (peripheral function; 0 = GPIO)
    bits 8:7         PULL          (0 = off, 1 = down, 3 = up)
    bit 9            INPUT_ENABLE
    ...

Usage:
    ./Scripts/m1n1/perstn.py                # default: PERSTN on
    ./Scripts/m1n1/perstn.py --no-perstn    # skip the GPIO poke
"""

import argparse
import io
import pathlib
import sys
import time
import traceback

sys.path.append(str(pathlib.Path.home() /
    "Projects/AsahiLinux/m4/m1n1/proxyclient"))

from m1n1.setup import *          # noqa: F401,F403 -- exposes u, p, iface
from m1n1.fw.smc import SMCClient


# PCI config space offsets.
PCI_VENDOR_ID     = 0x00
PCI_DEVICE_ID     = 0x02
PCI_COMMAND       = 0x04
PCI_STATUS        = 0x06
PCI_REVISION      = 0x08
PCI_CLASS_PROG    = 0x09
PCI_CLASS_DEV     = 0x0a
PCI_CLASS_BASE    = 0x0b
PCI_HEADER_TYPE   = 0x0e
PCI_BAR0          = 0x10
PCI_PRIMARY_BUS   = 0x18   # bridge only
PCI_SECONDARY_BUS = 0x19   # bridge only
PCI_SUBORDINATE   = 0x1a   # bridge only

PCI_CMD_IO     = 0x0001
PCI_CMD_MEM    = 0x0002
PCI_CMD_BM     = 0x0004


def log(msg):
    print(f"[pcie_up] {msg}")


def try_(fn, label):
    try:
        return fn()
    except Exception as e:
        log(f"WARN {label}: {e.__class__.__name__}: {e}")
        traceback.print_exc(limit=3)
        return None


def ecam_addr(base, bus, dev, fn, off):
    return base + (bus << 20) + (dev << 15) + (fn << 12) + off


def cfg_read32(base, bus, dev, fn, off):
    return p.read32(ecam_addr(base, bus, dev, fn, off))


def cfg_read16(base, bus, dev, fn, off):
    return p.read16(ecam_addr(base, bus, dev, fn, off))


def cfg_read8(base, bus, dev, fn, off):
    return p.read8(ecam_addr(base, bus, dev, fn, off))


def cfg_write16(base, bus, dev, fn, off, val):
    p.write16(ecam_addr(base, bus, dev, fn, off), val)


# ---------------------------------------------------------------- SMC power

def smc_power(buf):
    smc_addr = u.adt["arm-io/smc"].get_reg(0)[0]
    buf.write(f"SMC smc_addr = 0x{smc_addr:x}\n")
    smc = SMCClient(u, smc_addr, None)
    smc.start()
    smc.start_ep(0x20)
    smc.smcep.write32("gP0d", 0x800001)
    buf.write("gP0d <- 0x800001  (apcie power)\n")
    smc.smcep.write32("gP1a", 1)
    buf.write("gP1a <- 1         (apcie-ge power)\n")
    smc.stop()


# ---------------------------------------------------------------- PERSTN GPIO

# gpio0 controller (AAPL,phandle=120 in the t8132 ADT) lives at 0x19a000000,
# 224 pins. Each pin has one 32-bit register at (gpio_base + pin*4).
#
# The physical address 0x19a000000 is NOT in m1n1's direct MMU map on
# t8132: mmu_remap_ranges only adds the ADT `pmap-io-ranges` entries
# (ECAM, DART carveouts, ...), and gpio0 is not in that list. Reading
# 0x19a000000 directly triggers an "Exception: SYNC" and wedges m1n1.
#
# m1n1 does provide full 32 GB MMIO aliases at 0xc/d/e/f000000000
# (see src/memory.c: mmu_add_default_mappings). Use the 0xf alias
# (Device-nGnRE) — same memory type gpio0 needs — so no m1n1 change
# is required.
GPIO0_BASE_PHYS = 0x19a000000
GPIO0_MMIO_ALIAS = 0xf000000000
GPIO0_BASE = GPIO0_MMIO_ALIAS | GPIO0_BASE_PHYS
GPIO0_PIN_COUNT = 224

# Bit layout of the per-pin register, verbatim from Linux
# drivers/pinctrl/pinctrl-apple-gpio.c.
REG_GPIOx_DATA          = 1 << 0
REG_GPIOx_MODE_MASK     = 0x7 << 1
REG_GPIOx_MODE_OUT      = 1 << 1
REG_GPIOx_MODE_IN_IRQ_OFF = 7 << 1
REG_GPIOx_PERIPH_MASK   = 0x3 << 5
REG_GPIOx_PULL_MASK     = 0x3 << 7
REG_GPIOx_PULL_UP       = 3 << 7
REG_GPIOx_INPUT_ENABLE  = 1 << 9
REG_GPIOx_LOCK          = 1 << 21

# NIC PERSTN pin, from m4_recon/nic-adt.txt:
#   function_perst = GPIO(phandle=120 -> gpio0, args=[165, 0])
PERSTN_PIN = 165


def _gpio_reg_addr(pin):
    return GPIO0_BASE + pin * 4


def gpio_read(pin):
    return p.read32(_gpio_reg_addr(pin))


def gpio_set_output(pin, value, buf=None):
    """Drive `pin` as a GPIO output at level `value` (0 or 1).

    Mirrors what pinctrl-apple-gpio.c does in .direction_output/.set:
        clear PERIPH | MODE | DATA,
        set   MODE = OUT | (DATA if value else 0).
    Preserves pull, drive-strength, and other unrelated bits.
    """
    addr = _gpio_reg_addr(pin)
    old = p.read32(addr)
    new = old & ~(REG_GPIOx_PERIPH_MASK | REG_GPIOx_MODE_MASK | REG_GPIOx_DATA)
    new |= REG_GPIOx_MODE_OUT
    if value:
        new |= REG_GPIOx_DATA
    p.write32(addr, new)
    read_back = p.read32(addr)
    if buf is not None:
        phys = GPIO0_BASE_PHYS + pin * 4
        buf.write(f"gpio0[{pin}] @ phys 0x{phys:x} (via alias 0x{addr:x}): "
                  f"0x{old:08x} -> 0x{new:08x} (read-back 0x{read_back:08x})\n")
    return old, new, read_back


def deassert_perstn(buf, pin=PERSTN_PIN, cold_reset_us=10000, settle_ms=100):
    """Cold-reset the endpoint on `pin` (PERSTN#): drive low briefly, then high.

    Endpoints on Apple silicon expect PERSTN# asserted (low) while the fabric
    powers up, then deasserted (high) at least 100 ms before the host starts
    LTSSM training. m1n1's pcie_init handles the internal controller reset;
    this handles the external endpoint reset.
    """
    buf.write(f"=== PERSTN deassert (gpio0 pin {pin}) ===\n")
    log(f"PERSTN cold reset: drive gpio0[{pin}] low")
    gpio_set_output(pin, 0, buf)
    time.sleep(cold_reset_us / 1e6)

    log(f"PERSTN: drive gpio0[{pin}] high")
    gpio_set_output(pin, 1, buf)
    time.sleep(settle_ms / 1e3)
    buf.write(f"settled {settle_ms} ms after deassert\n\n")


# ---------------------------------------------------------------- ECAM walk

def _describe_class(cls_base, cls_dev, cls_prog):
    """Best-effort human class-code label."""
    if cls_base == 0x02 and cls_dev == 0x00:
        return "network / ethernet controller"
    if cls_base == 0x02:
        return "network / other"
    if cls_base == 0x04 and cls_dev == 0x30:
        return "wireless controller"
    if cls_base == 0x06 and cls_dev == 0x04:
        return "PCI bridge"
    if cls_base == 0x0c and cls_dev == 0x03:
        return "USB controller"
    if cls_base == 0x0c and cls_dev == 0x33:
        return "USB4 host"
    return f"class {cls_base:#04x}/{cls_dev:#04x}"


def probe_device(base, bus, dev, fn, buf):
    """Read one BDF's config header. Return dict or None if vacant."""
    tag = f"{bus:02x}:{dev:02x}.{fn}"
    try:
        vid_did = cfg_read32(base, bus, dev, fn, PCI_VENDOR_ID)
    except Exception as e:
        buf.write(f"  {tag}: config read failed: "
                  f"{e.__class__.__name__}: {e}\n")
        return None
    if vid_did == 0xffffffff or vid_did == 0x00000000:
        buf.write(f"  {tag}: vacant (VID:DID = 0x{vid_did:08x})\n")
        return None

    vid = vid_did & 0xffff
    did = (vid_did >> 16) & 0xffff

    dev_info = {"bus": bus, "dev": dev, "fn": fn, "vid": vid, "did": did}

    try:
        cls32 = cfg_read32(base, bus, dev, fn, PCI_REVISION)
        rev = cls32 & 0xff
        cls_prog = (cls32 >> 8) & 0xff
        cls_dev = (cls32 >> 16) & 0xff
        cls_base = (cls32 >> 24) & 0xff
        dev_info.update(rev=rev, cls_prog=cls_prog, cls_dev=cls_dev,
                        cls_base=cls_base)
    except Exception as e:
        buf.write(f"  {tag}: class read failed: {e}\n")

    try:
        header_type = cfg_read8(base, bus, dev, fn, PCI_HEADER_TYPE)
        dev_info["header_type"] = header_type
    except Exception as e:
        buf.write(f"  {tag}: header_type read failed: {e}\n")
        header_type = 0

    try:
        cmd = cfg_read16(base, bus, dev, fn, PCI_COMMAND)
        status = cfg_read16(base, bus, dev, fn, PCI_STATUS)
        dev_info.update(cmd=cmd, status=status)
    except Exception as e:
        buf.write(f"  {tag}: cmd/status read failed: {e}\n")

    try:
        bar0 = cfg_read32(base, bus, dev, fn, PCI_BAR0)
        dev_info["bar0"] = bar0
    except Exception as e:
        buf.write(f"  {tag}: BAR0 read failed: {e}\n")

    if (header_type & 0x7f) == 0x01:
        # PCI-to-PCI bridge
        try:
            prim = cfg_read8(base, bus, dev, fn, PCI_PRIMARY_BUS)
            sec  = cfg_read8(base, bus, dev, fn, PCI_SECONDARY_BUS)
            sub  = cfg_read8(base, bus, dev, fn, PCI_SUBORDINATE)
            dev_info.update(primary=prim, secondary=sec, subordinate=sub)
        except Exception as e:
            buf.write(f"  {tag}: bus-number read failed: {e}\n")

    label = _describe_class(dev_info.get("cls_base", 0xff),
                            dev_info.get("cls_dev", 0xff),
                            dev_info.get("cls_prog", 0xff))
    buf.write(f"  {tag}: VID:DID = {vid:04x}:{did:04x}  "
              f"rev={dev_info.get('rev', '?')}  "
              f"class={dev_info.get('cls_base', 0xff):02x}."
              f"{dev_info.get('cls_dev', 0xff):02x}."
              f"{dev_info.get('cls_prog', 0xff):02x}  ({label})\n")
    buf.write(f"          cmd=0x{dev_info.get('cmd', 0):04x} "
              f"status=0x{dev_info.get('status', 0):04x} "
              f"hdr=0x{header_type:02x} "
              f"BAR0=0x{dev_info.get('bar0', 0):08x}\n")
    if "secondary" in dev_info:
        buf.write(f"          bridge: primary={dev_info['primary']} "
                  f"secondary={dev_info['secondary']} "
                  f"subordinate={dev_info['subordinate']}\n")
    return dev_info


def ecam_walk(base, buf):
    """Walk bus 0 devs 0..3 (root + up to 3 switches), then each
    switch's downstream bus dev 0. Returns list of dev_info dicts."""
    devices = []
    buf.write("=== ECAM walk ===\n")
    buf.write(f"ecam_base = 0x{base:x}\n\n")

    buf.write("--- root bus 0 ---\n")
    root_devs = []
    for d in range(8):
        info = try_(lambda d=d: probe_device(base, 0, d, 0, buf),
                    f"cfg 00:{d:02x}.0")
        if info is not None:
            root_devs.append(info)
            devices.append(info)

    for bridge in root_devs:
        sec = bridge.get("secondary")
        if sec is None or sec == 0:
            continue
        buf.write(f"\n--- downstream bus {sec} "
                  f"(behind {bridge['bus']:02x}:{bridge['dev']:02x}."
                  f"{bridge['fn']}) ---\n")
        # Endpoints normally at dev 0; probe dev 1 as belt-and-braces.
        for d in range(2):
            info = try_(lambda d=d, sec=sec: probe_device(base, sec, d, 0, buf),
                        f"cfg {sec:02x}:{d:02x}.0")
            if info is not None:
                info["parent_bridge"] = f"{bridge['bus']:02x}:{bridge['dev']:02x}.{bridge['fn']}"
                devices.append(info)
    return devices


def enable_nic(base, nic, buf):
    """Set MEM+BM in NIC config+0x04. Read BAR0 back."""
    tag = f"{nic['bus']:02x}:{nic['dev']:02x}.{nic['fn']}"
    buf.write(f"\n=== enable NIC {tag} ===\n")
    try:
        old_cmd = cfg_read16(base, nic['bus'], nic['dev'], nic['fn'],
                             PCI_COMMAND)
        new_cmd = old_cmd | PCI_CMD_MEM | PCI_CMD_BM
        cfg_write16(base, nic['bus'], nic['dev'], nic['fn'],
                    PCI_COMMAND, new_cmd)
        read_back = cfg_read16(base, nic['bus'], nic['dev'], nic['fn'],
                               PCI_COMMAND)
        buf.write(f"CMD: 0x{old_cmd:04x} -> 0x{new_cmd:04x} "
                  f"(readback 0x{read_back:04x})\n")
    except Exception as e:
        buf.write(f"CMD write failed: {e.__class__.__name__}: {e}\n")
        return

    try:
        bar0 = cfg_read32(base, nic['bus'], nic['dev'], nic['fn'], PCI_BAR0)
        buf.write(f"BAR0 (raw)         = 0x{bar0:08x}\n")
        bar0_masked = bar0 & 0xfffffff0
        buf.write(f"BAR0 (mem-mapped)  = 0x{bar0_masked:08x}\n")
        nic["bar0_masked"] = bar0_masked
        # Probe MMIO: read the first word of the BAR window.
        try:
            first_word = p.read32(bar0_masked)
            buf.write(f"read32(BAR0)       = 0x{first_word:08x}"
                      f"{' (SLVERR-like)' if first_word == 0xffffffff else ''}\n")
            nic["bar0_first_word"] = first_word
        except Exception as e:
            buf.write(f"read32(BAR0) failed: {e.__class__.__name__}: {e}\n")
    except Exception as e:
        buf.write(f"BAR0 read failed: {e.__class__.__name__}: {e}\n")


# ---------------------------------------------------------------- diagnostics

# t8132 /arm-io/apcie register map, derived from the ADT dump in
# m4_recon/pcie-nodes.txt. Kept in Python for post-init inspection so we do
# not need to re-read the ADT on every run.
#
# Shared regs (indices 0..6):
#   [0] ECAM         0x1cb0000000
#   [1] RC           0x494000000
#   [2] PHY (packed) 0x497000000   (phy_common = +0x4000, phy[0] = +0x8000)
#   [3] PHY IP       0x497040000
#   [4] AXI          0x496000000
#   [5] ???          0x495046200
#   [6] ???          0x495044000
#
# Per-port (6 regs each):
#   [ 7..12] port 0  port_base=0x490028000  ltssm=0x49003c000  phy=0x497020000
#   [13..18] port 1  port_base=0x491028000  ltssm=0x49103c000  phy=0x497024000
#   [19..24] port 2  port_base=0x492028000  ltssm=0x49203c000  phy=0x497028000
PHY_COMMON_BASE = 0x497000000 + 0x4000
PORTS = [
    {"name": "port0", "port_base": 0x490028000, "phy_base": 0x497020000},
    {"name": "port1", "port_base": 0x491028000, "phy_base": 0x497024000},
    {"name": "port2", "port_base": 0x492028000, "phy_base": 0x497028000},
]


def _safe_read32(addr):
    try:
        return f"0x{p.read32(addr):08x}"
    except Exception as e:
        return f"<{e.__class__.__name__}: {e}>"


def dump_pcie_regs(buf):
    buf.write("=== PCIe controller register dump ===\n")
    buf.write(f"PHYCMN_CLK        @ 0x{PHY_COMMON_BASE:x} + 0x000 = "
              f"{_safe_read32(PHY_COMMON_BASE + 0x000)}\n\n")

    for p_ in PORTS:
        pb = p_["port_base"]
        phy = p_["phy_base"]
        buf.write(f"--- {p_['name']} (port_base=0x{pb:x}, phy_base=0x{phy:x}) ---\n")
        buf.write(f"  APPCLK      @ +0x800 = {_safe_read32(pb + 0x800)}\n")
        buf.write(f"  STATUS      @ +0x804 = {_safe_read32(pb + 0x804)}\n")
        buf.write(f"  LINKSTS     @ +0x208 = {_safe_read32(pb + 0x208)}\n")
        buf.write(f"  T602X_RESET @ +0x82c = {_safe_read32(pb + 0x82c)}\n")
        buf.write(f"  +0x104              = {_safe_read32(pb + 0x104)}\n")
        buf.write(f"  PHY_CTRL    @ phy+0 = {_safe_read32(phy + 0x000)}\n\n")


# ---------------------------------------------------------------- summary

def summarize(out_path, buf, devices, nic):
    header = io.StringIO()
    header.write("# Phase 3 PCIe runtime dump\n\n")
    if nic is None:
        header.write("**NIC not found** in the ECAM walk. Check the log below.\n\n")
    else:
        tag = f"{nic['bus']:02x}:{nic['dev']:02x}.{nic['fn']}"
        header.write(f"## NIC identity\n\n")
        header.write(f"- BDF:       `{tag}` (via {nic.get('parent_bridge', '?')})\n")
        header.write(f"- VID:DID:   `{nic['vid']:04x}:{nic['did']:04x}`\n")
        header.write(f"- Class:     `{nic.get('cls_base', 0xff):02x}."
                     f"{nic.get('cls_dev', 0xff):02x}."
                     f"{nic.get('cls_prog', 0xff):02x}` "
                     f"({_describe_class(nic.get('cls_base', 0xff), nic.get('cls_dev', 0xff), nic.get('cls_prog', 0xff))})\n")
        header.write(f"- BAR0:      `0x{nic.get('bar0_masked', 0):08x}`\n")
        first = nic.get("bar0_first_word", None)
        if first is not None:
            header.write(f"- BAR0[0]:   `0x{first:08x}`"
                         f"{'  <-- SLVERR-like' if first == 0xffffffff else ''}\n")
        header.write("\n")

    header.write("## Full walk log\n\n```\n")
    header.write(buf.getvalue())
    header.write("```\n")

    out_path.write_text(header.getvalue())
    log(f"wrote {out_path}")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--out", default="/tmp/m4-recon",
                    help="output directory (default: /tmp/m4-recon)")
    ap.add_argument("--ecam-base", type=lambda s: int(s, 0),
                    default=0x1cb0000000,
                    help="/arm-io/apcie reg[0] (default: 0x1cb0000000)")
    ap.add_argument("--no-perstn", action="store_true",
                    help="skip the PERSTN GPIO toggle (debug: matches pcie_up.py)")
    ap.add_argument("--perstn-pin", type=int, default=PERSTN_PIN,
                    help=f"gpio0 pin for NIC PERSTN (default: {PERSTN_PIN})")
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    buf = io.StringIO()

    log("=== Phase 3.3 pcie_up ===")

    log("SMC power on apcie fabric...")
    try_(lambda: smc_power(buf), "SMC power")

    if args.no_perstn:
        log("PERSTN toggle skipped (--no-perstn)")
        buf.write("=== PERSTN deassert (skipped) ===\n\n")
    else:
        log(f"PERSTN deassert on gpio0 pin {args.perstn_pin}...")
        try_(lambda: deassert_perstn(buf, pin=args.perstn_pin),
             "deassert_perstn")

    log("p.pcie_init()...")
    pcie_init_ok = False
    try:
        rc = p.pcie_init()
        buf.write(f"\np.pcie_init() -> {rc!r}\n\n")
        log(f"p.pcie_init returned {rc!r}")
        pcie_init_ok = True
    except Exception as e:
        buf.write(f"\np.pcie_init raised: {e.__class__.__name__}: {e}\n\n")
        log(f"p.pcie_init raised: {e.__class__.__name__}: {e}")
        traceback.print_exc(limit=5)

    if pcie_init_ok:
        log("dumping PCIe controller registers...")
        try_(lambda: dump_pcie_regs(buf), "dump_pcie_regs")
    else:
        log("skipping PCIe register dump (m1n1 is wedged, reads would time out)")
        buf.write("=== PCIe controller register dump ===\n"
                  "SKIPPED: p.pcie_init() raised — m1n1 is not responding.\n\n")

    log(f"ECAM walk @ 0x{args.ecam_base:x} ...")
    devices = try_(lambda: ecam_walk(args.ecam_base, buf), "ecam_walk") or []

    # Pick the NIC: class-0x02 device on any downstream bus.
    nic = None
    for d in devices:
        if d.get("cls_base") == 0x02:
            nic = d
            break

    if nic is not None:
        try_(lambda: enable_nic(args.ecam_base, nic, buf), "enable_nic")
    else:
        buf.write("\n=== enable NIC ===\nNo class-0x02 device found.\n")

    summarize(out / "nic-runtime.txt", buf, devices, nic)
    log("done. Copy /tmp/m4-recon/nic-runtime.txt into m4_recon/ if it looks sane.")


if __name__ == "__main__":
    main()
