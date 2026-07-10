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
sys.path.insert(0, str(pathlib.Path(__file__).parent))

from m1n1.setup import *          # noqa: F401,F403 -- exposes u, p, iface
from m1n1.fw.smc import SMCClient

from pcie_regs import ApcieMap


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

# gpio0 controller (AAPL,phandle=120) has ADT reg 0x19a000000 -- but that is
# a BUS address in /arm-io space, not a physical address. On t8132
# /arm-io/ranges[0] is a non-identity relocation
# (bus 0..0x3a0000000 -> parent 0x200000000..0x59a000000), so the real
# physical base is 0x39a000000. proxyclient's ADTNode.get_reg() does the
# translation for us; that PA lands inside m1n1's mmu_map_mmio identity
# window (0..0x3a0000000), so plain p.read32/write32 reach the device with
# no 0xf-alias trick.
#
# Previous iteration ("Exception: SYNC") computed 0xf000000000 | 0x19a000000
# = 0xf19a000000 -- via m1n1's 0xf alias that lands on PA 0x19a000000, which
# is a dead hole on t8132. Same reason SMC's ADT reg 0x18c600000 works only
# after get_reg() translates it to PA 0x38c600000.
GPIO0_PIN_COUNT = 224


def _gpio0_base():
    return u.adt["arm-io/gpio0"].get_reg(0)[0]

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

# NIC CLKREQ pin, from m4_recon/nic-adt.txt:
#   function_clkreq = GPIO(phandle=120 -> gpio0, args=[162, 2])
# args[1]=2 in the ADT would normally mean "peripheral function alt-2" (the
# PCIe controller drives CLKREQ# itself). For this experiment we drive it as
# a manual GPIO output LOW throughout the reset dance so the endpoint sees
# CLKREQ# asserted (== refclk request active) before/during/after PERSTN
# deassert. If this un-sticks port 2's LINKSTS BUSY, we know CLKREQ was the
# missing piece; either way, the runtime dump lets us iterate.
CLKREQ_PIN = 162


def _gpio_reg_addr(pin):
    return _gpio0_base() + pin * 4


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
        buf.write(f"gpio0[{pin}] @ 0x{addr:x}: 0x{old:08x} -> 0x{new:08x} "
                  f"(read-back 0x{read_back:08x})\n")
    return old, new, read_back


def deassert_perstn(buf, pin=PERSTN_PIN, cold_reset_us=10000, settle_ms=100):
    """Cold-reset the endpoint on `pin` (PERSTN#): drive low briefly, then high.

    Endpoints on Apple silicon expect PERSTN# asserted (low) while the fabric
    powers up, then deasserted (high) at least 100 ms before the host starts
    LTSSM training. m1n1's pcie_init handles the internal controller reset;
    this handles the external endpoint reset.
    """
    buf.write(f"=== PERSTN deassert (gpio0 pin {pin}) ===\n")

    # Safety probe: read pin 0's reg before touching pin 165. If gpio0
    # addressing is wrong, this fails cleanly (Python exception, wrapped by
    # try_() in main) leaving nic-runtime.txt behind instead of wedging m1n1.
    probe = _gpio_reg_addr(0)
    buf.write(f"gpio0 probe: read32(0x{probe:x}) = ")
    v = p.read32(probe)
    buf.write(f"0x{v:08x}\n")

    log(f"PERSTN cold reset: drive gpio0[{pin}] low")
    gpio_set_output(pin, 0, buf)
    time.sleep(cold_reset_us / 1e6)

    log(f"PERSTN: drive gpio0[{pin}] high")
    gpio_set_output(pin, 1, buf)
    time.sleep(settle_ms / 1e3)
    buf.write(f"settled {settle_ms} ms after deassert\n\n")


def assert_clkreq(buf, pin=CLKREQ_PIN, settle_ms=1):
    """Drive CLKREQ# (gpio0 pin `pin`) low so the endpoint has a valid refclk
    request asserted before we release PERSTN#. Called BEFORE deassert_perstn.

    Left in this state through pcie_init() so the endpoint continues to see
    CLKREQ# asserted while LTSSM trains. If iteration N+1 needs the pin muxed
    back to peripheral function, we'll do that after link-up.
    """
    buf.write(f"=== CLKREQ assert (gpio0 pin {pin}) ===\n")
    log(f"CLKREQ: drive gpio0[{pin}] low (assert)")
    gpio_set_output(pin, 0, buf)
    time.sleep(settle_ms / 1e3)
    buf.write(f"settled {settle_ms} ms after CLKREQ assert\n\n")


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


def ecam_walk(base, buf, active_ports=None):
    """Walk bus 0 devs 0..3 (root + up to 3 switches), then each active
    switch's downstream bus dev 0. Returns list of dev_info dicts.

    active_ports (from ApcieMap.active_ports) filters which downstream buses
    to touch -- reading an inactive port's ECAM range faults the fabric.
    Bus number for downstream = m1n1's implicit "port index + 1" convention
    when maximum-link-speed is set (root ports are 00:0N.0 -> bus N+1)."""
    devices = []
    buf.write("=== ECAM walk ===\n")
    buf.write(f"ecam_base = 0x{base:x}\n\n")
    active_set = set(active_ports) if active_ports is not None else None
    if active_set is not None:
        buf.write(f"active ports: {sorted(active_set)}\n\n")

    buf.write("--- root bus 0 ---\n")
    root_devs = []
    # On t8132 the three root complex functions live at 00:00.0/00:01.0/
    # 00:02.0 (one per port). Probe just the port count, not 0..7.
    for d in range(3):
        # Skip inactive-port root functions -- their downstream config isn't
        # backed by anything and even a header read can fault.
        if active_set is not None and d not in active_set:
            buf.write(f"  00:{d:02x}.0: skipped (port {d} inactive)\n")
            continue
        info = try_(lambda d=d: probe_device(base, 0, d, 0, buf),
                    f"cfg 00:{d:02x}.0")
        if info is not None:
            info["port"] = d
            root_devs.append(info)
            devices.append(info)

    for bridge in root_devs:
        sec = bridge.get("secondary")
        if sec is None or sec == 0:
            continue
        buf.write(f"\n--- downstream bus {sec} "
                  f"(behind {bridge['bus']:02x}:{bridge['dev']:02x}."
                  f"{bridge['fn']}, port {bridge.get('port', '?')}) ---\n")
        # Endpoints normally at dev 0; probe dev 1 as belt-and-braces.
        for d in range(2):
            info = try_(lambda d=d, sec=sec: probe_device(base, sec, d, 0, buf),
                        f"cfg {sec:02x}:{d:02x}.0")
            if info is not None:
                info["parent_bridge"] = f"{bridge['bus']:02x}:{bridge['dev']:02x}.{bridge['fn']}"
                info["parent_port"] = bridge.get("port")
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

# All addresses are pulled from the ADT via pcie_regs.ApcieMap.from_adt(u)
# (in main()). See pcie_regs.py for the full annotated t8132 layout, port
# GPIO wiring, and DART overlap notes.


def _safe_read32(addr):
    try:
        return f"0x{p.read32(addr):08x}"
    except Exception as e:
        return f"<{e.__class__.__name__}: {e}>"


def dump_pcie_regs(apcie, buf, tag="post-init", include_inactive=False):
    """Dump shared regs + per-port regs. Only iterates apcie.active_ports
    unless include_inactive is True. IMPORTANT: reading an inactive port's
    MMIO (its PHY was never enabled, PMGR gate is off) faults the fabric
    and wedges m1n1 -- keep include_inactive False in normal use."""
    buf.write(f"=== PCIe controller register dump ({tag}) ===\n")
    buf.write(f"PHYCMN_CLK    @ 0x{apcie.phy_common_base:x} + 0x000 = "
              f"{_safe_read32(apcie.phy_common_base + 0x000)}\n")
    buf.write(f"RC_BASE       @ 0x{apcie.rc_base:x} + 0x03c = "
              f"{_safe_read32(apcie.rc_base + 0x03c)}   "
              f"(T602X APCIE sets to 0x1)\n\n")

    port_indices = (range(apcie.N_PORTS) if include_inactive
                    else apcie.active_ports)
    for i in port_indices:
        p_ = apcie.ports[i]
        pb = p_.port_base
        lt = p_.ltssm_base
        phy = p_.phy_base
        phyx = p_.phy_extra_base
        ctrl = p_.ctrl_lo_base
        tag_line = f"--- port{i} port_base=0x{pb:x} ltssm=0x{lt:x} " \
                   f"phy=0x{phy:x} phy_extra=0x{phyx:x} ctrl_lo=0x{ctrl:x}"
        if not p_.exists:
            tag_line += "  [INACTIVE]"
        buf.write(tag_line + " ---\n")
        # Known port_base registers.
        buf.write(f"  APPCLK      @ port+0x800 = {_safe_read32(pb + 0x800)}\n")
        buf.write(f"  STATUS      @ port+0x804 = {_safe_read32(pb + 0x804)}\n")
        buf.write(f"  LINKSTS     @ port+0x208 = {_safe_read32(pb + 0x208)}\n")
        buf.write(f"  T602X_RESET @ port+0x82c = {_safe_read32(pb + 0x82c)}\n")
        buf.write(f"  +0x010                   = {_safe_read32(pb + 0x010)}   "
                  f"(T602X APCIE writes 0x2)\n")
        buf.write(f"  +0x104                   = {_safe_read32(pb + 0x104)}\n")
        # PHY.
        buf.write(f"  PHY_CTRL    @ phy+0x000  = {_safe_read32(phy + 0x000)}\n")
        # LTSSM debug block (16 KB).
        buf.write(f"  LTSSM +0x10              = {_safe_read32(lt + 0x10)}   "
                  f"(T602X non-APCIE writes 0x2)\n")
        buf.write(f"  LTSSM +0x14              = {_safe_read32(lt + 0x14)}   "
                  f"(T602X non-APCIE writes 0x1)\n")
        buf.write(f"  LTSSM +0x1c              = {_safe_read32(lt + 0x1c)}   "
                  f"(T602X non-APCIE writes 0x4)\n")
        buf.write(f"  LTSSM +0x20              = {_safe_read32(lt + 0x20)}   "
                  f"(T602X non-APCIE sets bit 1)\n")
        # phy_extra (overlaps shared PHY IP), ctrl_lo (overlaps DART) --
        # read-only sample, first few words. See pcie_regs.py header notes.
        buf.write(f"  phy_extra +0x000         = {_safe_read32(phyx + 0x000)}\n")
        buf.write(f"  phy_extra +0x004         = {_safe_read32(phyx + 0x004)}\n")
        buf.write(f"  phy_extra +0x008         = {_safe_read32(phyx + 0x008)}\n")
        buf.write(f"  ctrl_lo   +0x000         = {_safe_read32(ctrl + 0x000)}\n")
        buf.write(f"  ctrl_lo   +0x004         = {_safe_read32(ctrl + 0x004)}\n")
        buf.write(f"  ctrl_lo   +0x008         = {_safe_read32(ctrl + 0x008)}\n")
        buf.write(f"  ctrl_lo   +0x100         = {_safe_read32(ctrl + 0x100)}\n")
        buf.write("\n")


# ---------------------------------------------------------------- LTSSM kick

def _linksts_decode(v):
    """Human-readable decode of APCIE_PORT_LINKSTS bits we know about."""
    bits = []
    if v & (1 << 0):  bits.append("UP")
    if v & (1 << 2):  bits.append("BUSY")
    if v & (1 << 3):  bits.append("bit3")
    if v & (1 << 6):  bits.append("L2")
    if v & (1 << 9):  bits.append("bit9")
    if v & (1 << 24): bits.append("bit24")
    if v & (1 << 25): bits.append("bit25")
    if v & (1 << 31): bits.append("bit31")
    return "|".join(bits) if bits else "none"


def try_ltssm_kick(apcie, buf, port_indices=None):
    """After p.pcie_init() has returned (with ports stuck at LINKSTS_BUSY),
    try the LTSSM kick sequences that the T602X code paths use but the
    T8140/t8132 path skips. Read LINKSTS before and after each write so we
    can tell which one (if any) changed hardware state.

    Sequences tried in order per port:
      A) T602X APCIE:  rc_base+0x3c |= 0x1;  port_base+0x10 <- 0x2
      B) T602X non-APCIE LTSSM kick + APPCLK bit8 clear
      C) cycle T602X_PORT_RESET (deassert, reassert, deassert)
    """
    if port_indices is None:
        port_indices = apcie.active_ports
    buf.write("\n=== LTSSM kick experiment (post-init) ===\n")
    for i in port_indices:
        p_ = apcie.ports[i]
        pb = p_.port_base
        lt = p_.ltssm_base
        name = f"port{i}"

        def snap(label):
            v = None
            try:
                v = p.read32(pb + 0x208)
            except Exception as e:
                buf.write(f"  {name} {label:22s} LINKSTS: <{e.__class__.__name__}: {e}>\n")
                return
            buf.write(f"  {name} {label:22s} LINKSTS = 0x{v:08x}  [{_linksts_decode(v)}]\n")

        buf.write(f"\n--- {name} @ port_base=0x{pb:x} ltssm=0x{lt:x} ---\n")
        snap("baseline")

        # Sequence A: T602X APCIE-branch kick.
        try:
            buf.write(f"  seq A: set32(rc_base+0x3c, 0x1)  # 0x{apcie.rc_base + 0x3c:x}\n")
            p.set32(apcie.rc_base + 0x3c, 0x1)
            buf.write(f"  seq A: write32(port_base+0x10, 0x2)\n")
            p.write32(pb + 0x10, 0x2)
        except Exception as e:
            buf.write(f"  seq A FAILED: {e.__class__.__name__}: {e}\n")
        time.sleep(0.01)
        snap("after seq A")

        # Sequence B: T602X non-APCIE LTSSM kick.
        try:
            buf.write(f"  seq B: write32(ltssm+0x10, 0x2)\n")
            p.write32(lt + 0x10, 0x2)
            buf.write(f"  seq B: write32(ltssm+0x1c, 0x4)\n")
            p.write32(lt + 0x1c, 0x4)
            buf.write(f"  seq B: set32(ltssm+0x20, 0x2)\n")
            p.set32(lt + 0x20, 0x2)
            buf.write(f"  seq B: write32(ltssm+0x14, 0x1)\n")
            p.write32(lt + 0x14, 0x1)
            buf.write(f"  seq B: clear32(port_base+0x800, 0x100)  (APPCLK bit 8)\n")
            p.clear32(pb + 0x800, 0x100)
        except Exception as e:
            buf.write(f"  seq B FAILED: {e.__class__.__name__}: {e}\n")
        time.sleep(0.05)
        snap("after seq B")

        # Sequence C: cycle T602X_PORT_RESET (deassert, reassert, deassert)
        # -- copies what m1n1 does for T602X APCIE at line 752-754.
        try:
            buf.write(f"  seq C: clear+set T602X_RESET (port_base+0x82c)\n")
            p.clear32(pb + 0x82c, 0x1)
            time.sleep(0.001)
            p.set32(pb + 0x82c, 0x1)
        except Exception as e:
            buf.write(f"  seq C FAILED: {e.__class__.__name__}: {e}\n")
        time.sleep(0.05)
        snap("after seq C")

        # Extended settle in case training is slow.
        time.sleep(0.2)
        snap("after 200ms settle")


# ---------------------------------------------------------------- unmapped probe

def probe_unmapped(apcie, buf, span_words=16):
    """Read the first `span_words` 32-bit words of each active port's
    phy_extra and ctrl_lo blocks. Read-only; safe as long as the port's
    fabric is powered on (which it is post-SMC + post-pcie_init).

    Purpose: reveal the initial contents of the two per-port blocks that
    m1n1's t8140-path code does not touch. phy_extra sits inside the
    shared PHY IP window (m1n1 does apply apcie-phy-ip-* tunables at
    offsets that overlap here). ctrl_lo overlaps the DART MMIO for that
    port; interesting values might be DART registers left over from
    iBoot's own bring-up."""
    buf.write("\n=== unmapped block probe (read-only) ===\n")
    for i in apcie.active_ports:
        port = apcie.ports[i]
        buf.write(f"\n--- port{i} phy_extra @ 0x{port.phy_extra_base:x} "
                  f"(sz 0x{port.phy_extra_size:x}) ---\n")
        for off in range(0, span_words * 4, 4):
            buf.write(f"  +0x{off:04x} = "
                      f"{_safe_read32(port.phy_extra_base + off)}\n")
        buf.write(f"\n--- port{i} ctrl_lo @ 0x{port.ctrl_lo_base:x} "
                  f"(sz 0x{port.ctrl_lo_size:x}, overlaps dart-apcie{i}) ---\n")
        for off in range(0, span_words * 4, 4):
            buf.write(f"  +0x{off:04x} = "
                      f"{_safe_read32(port.ctrl_lo_base + off)}\n")


# ---------------------------------------------------------------- unblock experiment

_UNBLOCK_OFFSETS = (0x0000, 0x0004, 0x0008, 0x000c, 0x0010, 0x0100, 0x0104,
                    0x0108, 0x010c, 0x0200)


def unblock_experiment(apcie, buf, port_index=2, settle_ms=50):
    """One-at-a-time bit-0 flip on a small offset set inside port N's
    ctrl_lo block, reading LINKSTS between each write to see if any move
    the port off LINKSTS_BUSY. Gated by --unblock-experiment because it
    writes to the DART's MMIO alias -- if any of these offsets are the
    DART's L0 base register we may lose port 2's DMA. Iterates only when
    the port is active.

    Bounded to ~10 writes so a hang burns at most one power-cycle."""
    port = apcie.ports[port_index]
    if not port.exists:
        buf.write(f"\n=== unblock experiment: port {port_index} NOT active, "
                  f"skipping ===\n")
        return
    buf.write(f"\n=== unblock experiment (port{port_index}, ctrl_lo @ "
              f"0x{port.ctrl_lo_base:x}) ===\n")
    buf.write("WARNING: these writes go through the DART's MMIO alias.\n")
    buf.write("If port 2 DMA breaks after this run, that's why.\n\n")

    def snap(label):
        try:
            v = p.read32(port.port_base + 0x208)
        except Exception as e:
            buf.write(f"  {label:24s} LINKSTS: <{e.__class__.__name__}: {e}>\n")
            return None
        buf.write(f"  {label:24s} LINKSTS = 0x{v:08x}  "
                  f"[{_linksts_decode(v)}]\n")
        return v

    baseline = snap("baseline")
    for off in _UNBLOCK_OFFSETS:
        addr = port.ctrl_lo_base + off
        try:
            before = p.read32(addr)
            buf.write(f"\n  ctrl_lo +0x{off:04x} = 0x{before:08x} "
                      f"-> flipping bit 0\n")
            p.set32(addr, 0x1)
            after_set = p.read32(addr)
            buf.write(f"  ctrl_lo +0x{off:04x} after set = "
                      f"0x{after_set:08x}\n")
            time.sleep(settle_ms / 1e3)
            snap(f"after set +0x{off:04x}")
            # Restore original bit-0 state to keep DART intact.
            if not (before & 0x1):
                p.clear32(addr, 0x1)
        except Exception as e:
            buf.write(f"  ctrl_lo +0x{off:04x} write FAILED: "
                      f"{e.__class__.__name__}: {e}\n")
            break
    buf.write("\n")
    final = snap("final")
    if baseline is not None and final is not None and baseline != final:
        buf.write(f"  ** LINKSTS changed: 0x{baseline:08x} -> 0x{final:08x} **\n")


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
    ap.add_argument("--no-perstn", action="store_true",
                    help="skip the PERSTN GPIO toggle (debug: matches pcie_up.py)")
    ap.add_argument("--perstn-pin", type=int, default=None,
                    help="override gpio0 pin for NIC PERSTN "
                         "(default: from ADT pci-bridge2.function-perst)")
    ap.add_argument("--no-clkreq", action="store_true",
                    help="skip the CLKREQ GPIO assert (A/B: matches previous run)")
    ap.add_argument("--clkreq-pin", type=int, default=None,
                    help="override gpio0 pin for NIC CLKREQ "
                         "(default: from ADT pci-bridge2.function-clkreq)")
    ap.add_argument("--no-ltssm-kick", action="store_true",
                    help="skip the post-init LTSSM kick experiment")
    ap.add_argument("--no-unmapped-probe", action="store_true",
                    help="skip the phy_extra + ctrl_lo read-only probe")
    ap.add_argument("--unblock-experiment", action="store_true",
                    help="poke bit 0 at a small set of ctrl_lo offsets on "
                         "the NIC port to see if any move LINKSTS off BUSY. "
                         "OFF by default -- writes go through DART MMIO alias.")
    ap.add_argument("--unblock-port", type=int, default=2,
                    help="port index for --unblock-experiment (default: 2)")
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    buf = io.StringIO()

    log("=== Phase 3.3 pcie_up ===")

    # Build the ADT-sourced map first so every subsequent step has named
    # handles for the reg blocks it wants to touch.
    log("building ApcieMap from ADT...")
    apcie = ApcieMap.from_adt(u)
    apcie.describe(buf)
    buf.write("\n")

    # Resolve NIC-side GPIO pins from the ADT if the caller didn't override.
    nic_port = apcie.nic_port()
    perstn_pin = args.perstn_pin
    clkreq_pin = args.clkreq_pin
    if nic_port is not None:
        if perstn_pin is None and nic_port.perst_pin is not None:
            perstn_pin = nic_port.perst_pin
        if clkreq_pin is None and nic_port.clkreq_pin is not None:
            clkreq_pin = nic_port.clkreq_pin
    if perstn_pin is None:
        perstn_pin = PERSTN_PIN
    if clkreq_pin is None:
        clkreq_pin = CLKREQ_PIN
    buf.write(f"NIC GPIO pins: PERSTN=gpio0[{perstn_pin}] "
              f"CLKREQ=gpio0[{clkreq_pin}]\n\n")

    log("SMC power on apcie fabric...")
    try_(lambda: smc_power(buf), "SMC power")

    if args.no_clkreq:
        log("CLKREQ assert skipped (--no-clkreq)")
        buf.write("=== CLKREQ assert (skipped) ===\n\n")
    else:
        log(f"CLKREQ assert on gpio0 pin {clkreq_pin}...")
        try_(lambda: assert_clkreq(buf, pin=clkreq_pin),
             "assert_clkreq")

    if args.no_perstn:
        log("PERSTN toggle skipped (--no-perstn)")
        buf.write("=== PERSTN deassert (skipped) ===\n\n")
    else:
        log(f"PERSTN deassert on gpio0 pin {perstn_pin}...")
        try_(lambda: deassert_perstn(buf, pin=perstn_pin),
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
        log(f"active ports (per ADT): {apcie.active_ports}")
        log("dumping PCIe controller registers (post-init)...")
        try_(lambda: dump_pcie_regs(apcie, buf, "post-init"),
             "dump_pcie_regs")

        if args.no_unmapped_probe:
            log("unmapped-block probe skipped (--no-unmapped-probe)")
            buf.write("\n=== unmapped block probe (skipped) ===\n\n")
        else:
            log("probing phy_extra + ctrl_lo (read-only)...")
            try_(lambda: probe_unmapped(apcie, buf), "probe_unmapped")

        if args.no_ltssm_kick:
            log("LTSSM kick skipped (--no-ltssm-kick)")
            buf.write("\n=== LTSSM kick experiment (skipped) ===\n\n")
        else:
            log(f"trying LTSSM kick sequences on ports "
                f"{apcie.active_ports}...")
            try_(lambda: try_ltssm_kick(apcie, buf),
                 "try_ltssm_kick")

            log("dumping PCIe controller registers (post-kick)...")
            try_(lambda: dump_pcie_regs(apcie, buf, "post-kick"),
                 "dump_pcie_regs")

        if args.unblock_experiment:
            log(f"running unblock experiment on port {args.unblock_port}...")
            try_(lambda: unblock_experiment(apcie, buf,
                                            port_index=args.unblock_port),
                 "unblock_experiment")
            log("dumping PCIe controller registers (post-unblock)...")
            try_(lambda: dump_pcie_regs(apcie, buf, "post-unblock"),
                 "dump_pcie_regs")
    else:
        log("skipping PCIe register dump (m1n1 is wedged, reads would time out)")
        buf.write("=== PCIe controller register dump ===\n"
                  "SKIPPED: p.pcie_init() raised -- m1n1 is not responding.\n\n")

    log(f"ECAM walk @ 0x{apcie.ecam_base:x} ...")
    devices = try_(lambda: ecam_walk(apcie.ecam_base, buf,
                                     active_ports=apcie.active_ports),
                   "ecam_walk") or []

    # Pick the NIC: class-0x02 device on any downstream bus.
    nic = None
    for d in devices:
        if d.get("cls_base") == 0x02:
            nic = d
            break

    if nic is not None:
        try_(lambda: enable_nic(apcie.ecam_base, nic, buf), "enable_nic")
    else:
        buf.write("\n=== enable NIC ===\nNo class-0x02 device found.\n")

    summarize(out / "nic-runtime.txt", buf, devices, nic)
    log("done. Copy /tmp/m4-recon/nic-runtime.txt into m4_recon/ if it looks sane.")


if __name__ == "__main__":
    main()
