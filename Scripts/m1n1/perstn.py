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
from contextlib import contextmanager

sys.path.append(str(pathlib.Path.home() /
    "Projects/AsahiLinux/m4/m1n1/proxyclient"))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

from m1n1.setup import *          # noqa: F401,F403 -- exposes u, p, iface
from m1n1.fw.smc import SMCClient
from m1n1.proxy import GUARD

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


@contextmanager
def guarded(buf=None, label="", silent=True, short_timeout=0.3):
    """Enable m1n1's SYNC/SError exception guard + shortened UART timeout.

    Faulting p.read32/p.write32 calls to blocks that respond with SLVERR
    return a sentinel (0xacce5515) and bump m1n1's exc_count. That covers
    synchronous CPU exceptions (SLVERR, memory-type mismatch, etc.).

    IMPORTANT LIMITATION: GUARD.SKIP does NOT help against AXI bus stalls.
    If a target block is completely un-clocked (PMGR gate off) or held in
    reset, the AXI transaction never completes, no CPU exception fires, and
    the M4 CPU stalls forever on the load. In that case the proxy request
    never returns; the UART times out on the Python side and m1n1 is dead
    until the next power-cycle. `short_timeout` bounds how long we wait
    before declaring m1n1 wedged (default 300 ms vs. the 3 s default).

    Use with check_alive() after any timeout to bail before wasting more
    reads. Set silent=False to see per-fault TTY> prints on the M4 side.
    """
    mode = GUARD.SKIP | (GUARD.SILENT if silent else 0)
    cnt_before = 0
    try:
        cnt_before = p.get_exc_count()
    except Exception as e:
        if buf is not None:
            buf.write(f"[guard] {label}: get_exc_count(pre) failed: "
                      f"{e.__class__.__name__}: {e}\n")

    old_timeout = None
    if short_timeout is not None:
        try:
            old_timeout = iface.dev.timeout
            iface.dev.timeout = short_timeout
        except Exception as e:
            if buf is not None:
                buf.write(f"[guard] {label}: could not set short timeout: "
                          f"{e.__class__.__name__}: {e}\n")
            old_timeout = None

    p.set_exc_guard(mode)
    try:
        yield
    finally:
        # Restore timeout FIRST so cleanup proxy ops don't fail on the
        # tightened budget.
        if old_timeout is not None:
            try:
                iface.dev.timeout = old_timeout
            except Exception:
                pass
        try:
            p.set_exc_guard(GUARD.OFF)
        except Exception as e:
            if buf is not None:
                buf.write(f"[guard] {label}: set_exc_guard(OFF) failed: "
                          f"{e.__class__.__name__}: {e}\n")
        try:
            cnt_after = p.get_exc_count()
            delta = cnt_after - cnt_before
            if buf is not None:
                buf.write(f"[guard] {label}: exc_count delta = {delta} "
                          f"(before={cnt_before}, after={cnt_after})\n")
        except Exception as e:
            if buf is not None:
                buf.write(f"[guard] {label}: get_exc_count(post) failed: "
                          f"{e.__class__.__name__}: {e}\n")


def check_alive(timeout=0.5):
    """Non-destructive probe: does m1n1 still respond?

    Temporarily lowers the UART timeout, issues a cheap proxy request
    (get_exc_count), returns True iff it comes back. Restores timeout.

    Use after a read fails inside a `guarded()` block to decide whether
    the rest of the dump is worth trying or whether m1n1 is wedged.
    """
    old = None
    try:
        old = iface.dev.timeout
        iface.dev.timeout = timeout
    except Exception:
        pass
    try:
        p.get_exc_count()
        return True
    except Exception:
        return False
    finally:
        if old is not None:
            try:
                iface.dev.timeout = old
            except Exception:
                pass


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


# Sentinel value m1n1's GUARD.SKIP handler returns from a faulting load.
GUARD_SENTINEL = 0xacce5515


class DumpAborted(Exception):
    """Raised to bail from a dump when m1n1 has stopped responding.

    A single AXI stall wedges m1n1's CPU; every subsequent p.read32 will
    time out. Rather than burn one UART-timeout per remaining register,
    the read helper raises this so callers unwind quickly to the next
    guarded() block.
    """


def _read32_live(addr, label, buf, log_progress=True, alive_probe=True):
    """Read one 32-bit register, printing progress on stdout AND to buf.

    - On success: writes '<label> @ <addr> = 0x<value>' (annotates the
      m1n1 GUARD.SKIP sentinel).
    - On Python exception (UART timeout etc.): writes '<FAILED>' and,
      if alive_probe, checks m1n1 liveness. If m1n1 is dead, raises
      DumpAborted so the whole dump bails.

    Returns the register value on success, or None on failure.
    """
    if log_progress:
        log(f"    read32(0x{addr:x}) [{label}]...")
    try:
        v = p.read32(addr)
    except Exception as e:
        msg = f"{e.__class__.__name__}: {e}"
        buf.write(f"  {label:26s} @ 0x{addr:x} = <{msg}>\n")
        if log_progress:
            log(f"      -> FAILED: {msg}")
        if alive_probe:
            log("      probing m1n1 liveness after failed read...")
            if not check_alive():
                buf.write(f"  [ABORT] m1n1 is not responding; "
                          f"stopping dump\n")
                log("      m1n1 DEAD -- bailing from dump")
                raise DumpAborted() from e
            log("      m1n1 still alive; continuing")
        return None
    tag = ""
    if v == GUARD_SENTINEL:
        tag = "   <-- GUARD.SKIP sentinel (SLVERR caught)"
    buf.write(f"  {label:26s} @ 0x{addr:x} = 0x{v:08x}{tag}\n")
    if log_progress:
        log(f"      -> 0x{v:08x}{tag}")
    return v


# Backward-compat shim (keeps any lingering callers happy).
def _safe_read32(addr):
    try:
        return f"0x{p.read32(addr):08x}"
    except Exception as e:
        return f"<{e.__class__.__name__}: {e}>"


# ------------------------------------------------ phy_ip tunables report

def _slice_label(info):
    k = info["kind"]
    if k == "shared":
        return "shared", "-"
    if k == "port_slice":
        return (f"port{info['port_index']}",
                "yes" if info["port_active"] else "NO")
    return "!OOR", "-"


def dump_phy_ip_tunables_report(apcie, buf):
    """Dump apcie-phy-ip-{pll,auspma}-tunables with phy_ip_base slice
    classification. ADT-only -- no MMIO reads, no proxy calls.

    Safe to run regardless of pcie_init state: if m1n1 has wedged during
    a previous p.pcie_init() call in the same session, this still works
    because everything comes out of u.adt (loaded once at startup).

    Answers the two questions blocking Phase 3.3:
      1) which tunable entries write into which phy_ip_base slice, and
      2) how many entries land in slices whose pci-bridge{N} is absent
         from the ADT (currently port 1 on j773g).
    """
    buf.write("=== phy_ip tunables report (from ADT, no MMIO) ===\n")
    buf.write(f"phy_ip_base       = 0x{apcie.phy_ip_base:x} "
              f"sz 0x{apcie.phy_ip_size:x}\n")
    buf.write(f"active ADT ports  = {apcie.active_ports}\n")
    buf.write("slice geometry    = shared [0x0000..0x8000)  "
              "port0 [0x08000..0x10000)  "
              "port1 [0x10000..0x18000)  "
              "port2 [0x18000..0x20000)\n\n")

    first_wedge = None

    for prop in ("apcie-phy-ip-pll-tunables",
                 "apcie-phy-ip-auspma-tunables"):
        entries = apcie.apcie_tunables(u, prop)
        buf.write(f"--- {prop} ({len(entries)} entries) ---\n")
        if not entries:
            buf.write("  (property missing from ADT)\n\n")
            continue

        tally = {"shared": 0, "out_of_window": 0}
        range_lo = {}
        range_hi = {}

        buf.write("  #  offset     sz  mask               "
                  "value              -> target_addr slice   active\n")
        for i, (off, size, mask, val) in enumerate(entries):
            info = apcie.classify_phy_ip_offset(off)
            slabel, active = _slice_label(info)

            key = slabel
            if info["kind"] == "port_slice":
                key = f"port{info['port_index']}_" + \
                      ("active" if info["port_active"] else "inactive")
            tally[key] = tally.get(key, 0) + 1

            range_lo.setdefault(slabel, off)
            range_hi.setdefault(slabel, off)
            range_lo[slabel] = min(range_lo[slabel], off)
            range_hi[slabel] = max(range_hi[slabel], off)

            if (first_wedge is None
                    and info["kind"] == "port_slice"
                    and not info["port_active"]):
                first_wedge = (prop, i, off, info["target_addr"])

            buf.write(f"  {i:3d} 0x{off:08x} {size:2d}  0x{mask:016x} "
                      f"0x{val:016x}    0x{info['target_addr']:09x} "
                      f"{slabel:6s} {active}\n")

        buf.write("  --- tally by slice ---\n")
        for k in sorted(tally.keys()):
            if tally[k]:
                buf.write(f"    {k:20s} {tally[k]:4d}\n")
        buf.write("  --- offset ranges by slice ---\n")
        for sl in sorted(range_lo.keys()):
            buf.write(f"    {sl:8s} 0x{range_lo[sl]:08x} .. "
                      f"0x{range_hi[sl]:08x}\n")
        buf.write("\n")

    if first_wedge is not None:
        prop, i, off, addr = first_wedge
        buf.write("=== port-1 wedge hypothesis ===\n")
        buf.write(f"first tunable entry targeting an INACTIVE port slice:\n")
        buf.write(f"  {prop} entry #{i}: offset=0x{off:x} "
                  f"-> target_addr=0x{addr:x}\n")
        buf.write("If m1n1's tunable applicator hits this address, the AXI\n")
        buf.write("write to an unpowered slave hangs the fabric silently.\n")
        buf.write("This matches the observed wedge in pcie_up_2.log.\n\n")
    else:
        buf.write("=== port-1 wedge hypothesis ===\n")
        buf.write("no tunable entries target an inactive port slice.\n\n")


# ---------------------------------- pre-pcie_init shared MMIO probe

def probe_preinit_regs(apcie, buf, timeout=0.3):
    """Probe a small set of SHARED apcie MMIO addresses BEFORE p.pcie_init().

    Only shared blocks are touched -- port_base / ltssm / intr2axi /
    ctrl_lo require per-port PMGR gates that only pcie_init enables, and
    probing them unpowered will AXI-stall.

    Even the shared reads are done under guarded() with a short timeout
    so the first wedge aborts the rest of the probe cleanly.
    """
    probes = [
        (apcie.rc_base + 0x00,          "rc_base +0x00"),
        (apcie.rc_base + 0x04,          "rc_base +0x04"),
        (apcie.rc_base + 0x24,          "rc_base +0x24 (PHYIF_CTRL)"),
        (apcie.rc_base + 0x3c,          "rc_base +0x3c"),
        (apcie.rc_base + 0x50,          "rc_base +0x50"),
        (apcie.rc_base + 0x54,          "rc_base +0x54"),
        (apcie.rc_base + 0x58,          "rc_base +0x58"),
        (apcie.phy_common_base + 0x00,  "phy_common_base +0x00 (PHYCMN_CLK)"),
        (apcie.phy_ip_base + 0x00,      "phy_ip_base +0x00 (PLL area head)"),
        (apcie.phy_ip_base + 0x8000,    "phy_ip_base +0x08000 (port 0 slice head)"),
        (apcie.phy_ip_base + 0x10000,   "phy_ip_base +0x10000 (port 1 slice head -- INACTIVE)"),
        (apcie.phy_ip_base + 0x18000,   "phy_ip_base +0x18000 (port 2 slice head)"),
        (apcie.axi_base + 0x00,         "axi_base +0x00"),
    ]
    buf.write("=== pre-pcie_init shared MMIO probe ===\n")
    buf.write("(reads only; port_base / ltssm / ctrl_lo skipped -- "
              "need per-port PMGR)\n\n")
    for addr, label in probes:
        val = _safe_read32(addr)
        buf.write(f"  read32(0x{addr:09x}) [{label}] = {val}\n")
        if not check_alive(timeout=timeout):
            buf.write("  !!! m1n1 wedged during preinit probe; bailing\n")
            break
    buf.write("\n")


def dump_pcie_regs(apcie, buf, tag="post-init", tier=1):
    """Dump PCIe controller state, gated by safety tier.

    tier=1 (DEFAULT, always safe):
        Registers m1n1 provably wrote to during pcie_init on the t8132
        branch (regs_t8140 shared_reg_count=7). Per active port: APPCLK,
        STATUS, LINKSTS, T602X_RESET, +0x104. Plus rc_base+0x3c.
    tier=2 (--tier2):
        Adds phy_common (m1n1 applies phy-common tunables here), and the
        per-port phy_base which m1n1 pokes when releasing PHY reset.
    tier=3 (--tier3, DANGEROUS):
        Adds ltssm_base, phy_extra, ctrl_lo. ctrl_lo OVERLAPS the
        dart-apcie* MMIO -- if the DART is not clocked (m1n1 doesn't
        touch it during pcie_init), reads will AXI-stall and wedge m1n1.
        GUARD.SKIP does not save us from AXI stalls; only from SLVERR.

    After every read that fails, m1n1 liveness is probed. If m1n1 is
    dead, DumpAborted is raised and the dump exits early.
    """
    buf.write(f"=== PCIe controller register dump ({tag}, tier={tier}) ===\n")
    log(f"  dump_pcie_regs tier={tier} tag={tag} "
        f"active_ports={apcie.active_ports}")

    try:
        # --- Tier 1: always safe. ---
        _read32_live(apcie.rc_base + 0x03c,
                     "rc_base + 0x03c", buf)
        for i in apcie.active_ports:
            p_ = apcie.ports[i]
            pb = p_.port_base
            buf.write(f"\n--- port{i} (T1) port_base=0x{pb:x} ---\n")
            log(f"  === port{i} Tier 1 (port_base=0x{pb:x}) ===")
            # Bring-up + status registers.
            _read32_live(pb + 0x800, f"port{i} APPCLK      (+0x800)", buf)
            _read32_live(pb + 0x804, f"port{i} STATUS      (+0x804)", buf)
            _read32_live(pb + 0x208, f"port{i} LINKSTS     (+0x208)", buf)
            _read32_live(pb + 0x82c, f"port{i} T602X_RESET (+0x82c)", buf)
            _read32_live(pb + 0x814, f"port{i} PORT_RESET  (+0x814)", buf)
            # T602X-style init state that m1n1's t8140 branch writes on
            # t8132 (pcie.c:644-687 runs for type == T8140). Reading these
            # tells us which writes stuck and which need overriding.
            _read32_live(pb + 0x010, f"port{i} +0x010", buf)
            _read32_live(pb + 0x088, f"port{i} +0x088", buf)
            _read32_live(pb + 0x100, f"port{i} +0x100", buf)
            _read32_live(pb + 0x104, f"port{i} +0x104", buf)
            _read32_live(pb + 0x124, f"port{i} +0x124", buf)
            _read32_live(pb + 0x140, f"port{i} +0x140", buf)
            _read32_live(pb + 0x144, f"port{i} +0x144", buf)
            _read32_live(pb + 0x148, f"port{i} +0x148", buf)
            _read32_live(pb + 0x210, f"port{i} +0x210", buf)
            _read32_live(pb + 0x808, f"port{i} +0x808", buf)
            _read32_live(pb + 0x397c, f"port{i} +0x397c (T602X only)", buf)

        if tier < 2:
            return

        # --- Tier 2: PHY common + per-port PHY_CTRL. ---
        buf.write(f"\n=== Tier 2 (--tier2) ===\n")
        log("  === Tier 2 (phy_common + phy_base) ===")
        _read32_live(apcie.phy_common_base + 0x000,
                     "PHYCMN_CLK (phy_common+0x000)", buf)
        _read32_live(apcie.rc_base + 0x024,
                     "PHYIF_CTRL (rc_base+0x024)", buf)
        for i in apcie.active_ports:
            p_ = apcie.ports[i]
            phy = p_.phy_base
            buf.write(f"\n--- port{i} (T2) phy_base=0x{phy:x} ---\n")
            log(f"  === port{i} Tier 2 (phy_base=0x{phy:x}) ===")
            _read32_live(phy + 0x000, f"port{i} PHY_CTRL (+0x000)", buf)
            _read32_live(phy + 0x004, f"port{i} PHY      (+0x004)", buf)

        if tier < 3:
            return

        # --- Tier 3: LTSSM + phy_extra + ctrl_lo. May AXI-stall. ---
        buf.write(f"\n=== Tier 3 (--tier3, DANGEROUS) ===\n")
        log("  === Tier 3 (ltssm + phy_extra + ctrl_lo) ===")
        for i in apcie.active_ports:
            p_ = apcie.ports[i]
            lt = p_.ltssm_base
            phyx = p_.phy_extra_base
            ctrl = p_.ctrl_lo_base
            buf.write(f"\n--- port{i} (T3) ltssm=0x{lt:x} "
                      f"phy_extra=0x{phyx:x} ctrl_lo=0x{ctrl:x} ---\n")
            log(f"  === port{i} Tier 3 ===")
            _read32_live(lt + 0x10, f"port{i} LTSSM +0x10", buf)
            _read32_live(lt + 0x14, f"port{i} LTSSM +0x14", buf)
            _read32_live(lt + 0x1c, f"port{i} LTSSM +0x1c", buf)
            _read32_live(lt + 0x20, f"port{i} LTSSM +0x20", buf)
            _read32_live(phyx + 0x000, f"port{i} phy_extra +0x000", buf)
            _read32_live(phyx + 0x004, f"port{i} phy_extra +0x004", buf)
            _read32_live(phyx + 0x008, f"port{i} phy_extra +0x008", buf)
            _read32_live(ctrl + 0x000, f"port{i} ctrl_lo +0x000", buf)
            _read32_live(ctrl + 0x004, f"port{i} ctrl_lo +0x004", buf)
            _read32_live(ctrl + 0x008, f"port{i} ctrl_lo +0x008", buf)
            _read32_live(ctrl + 0x100, f"port{i} ctrl_lo +0x100", buf)
    except DumpAborted:
        log("  dump aborted (m1n1 wedged)")
        buf.write(f"[dump_pcie_regs] aborted -- m1n1 not responding\n")


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


# ---------------------------------------------------------------- T602X init replay

# APCIE_T602X_PORT_MSIMAP offset (m1n1 src/pcie.c:71).
T602X_PORT_MSIMAP = 0x3800


def t602x_port_init_replay(apcie, buf, aggressive=False, do_again=False,
                            do_msimap=False, port_indices=None):
    """Replay the T602X APCIE branch of m1n1's pcie_init_controller
    (src/pcie.c lines 633-767) on top of what m1n1's t8140 branch left
    behind. Verbatim from the T602X code paths that are *disabled* by
    `state->pcie_regs->type == APCIE_T8140` on the current t8132 clause.

    What this ADDS on top of what m1n1 already did:
      L633:  set32(rc_base + 0x3c, 0x1)          -- Tier 1
      L637:  write32(port_base + 0x10, 0x2)      -- Tier 1
      L653:  write32(port_base + 0x104, 0x7fffffff) -- overrides m1n1's
             T8140 value of 0xfffffff0 for the T602X flavor
      L674:  write32(port_base + 0x397c, 0x0)    -- Tier 1
      L716:  clear32(port_phy_base + 0x000, 0x4000) -- Tier 2. m1n1
             already cleared bit 0x10 instead.
      L724:  set32(port_base + T602X_PORT_RESET, RESET_DIS) -- Tier 1
             (m1n1 already did this since line 724 also runs on T8140)

    Optional (gated flags):
      aggressive=True   -- adds L737-742 LTSSM debug writes (Tier 3,
                           ltssm_base; may AXI-stall if the block is
                           un-clocked).
      do_again=True     -- adds L752-767 "Do it again?" cycle
                           (T602X_PORT_RESET reassert then LTSSM debug).
                           Aggressive implies do_again == False; the
                           two use different LTSSM sequences.
      do_msimap=True    -- adds L845 MSIMAP populate with 0x80000000|i
                           (writes to port_base + 0x3800 + i*4 for
                           i=0..511). Big; wraps to Tier 3 territory
                           via the MSI vector table.

    After each phase, LINKSTS is snapshotted so a change is visible
    immediately. Bails cleanly on m1n1 wedge via DumpAborted.
    """
    if port_indices is None:
        port_indices = apcie.active_ports
    buf.write(f"\n=== T602X init replay (aggressive={aggressive}, "
              f"do_again={do_again}, do_msimap={do_msimap}) ===\n")
    log(f"  t602x_port_init_replay ports={list(port_indices)} "
        f"aggressive={aggressive} do_again={do_again} do_msimap={do_msimap}")

    for i in port_indices:
        p_ = apcie.ports[i]
        pb = p_.port_base
        phy = p_.phy_base
        lt = p_.ltssm_base
        buf.write(f"\n--- port{i} T602X replay ---\n")
        log(f"    port{i} port_base=0x{pb:x} phy_base=0x{phy:x} "
            f"ltssm=0x{lt:x}")

        def snap(label):
            v = _read32_live(pb + 0x208,
                             f"port{i} LINKSTS ({label})", buf)
            if v is not None:
                buf.write(f"  port{i} {label:28s} LINKSTS decode: "
                          f"[{_linksts_decode(v)}]\n")

        try:
            snap("pre-replay baseline")

            # L633: set rc_base + 0x3c bit 0. Verify writeback.
            buf.write(f"\n  L633: set32(rc_base+0x3c, 0x1)\n")
            _read32_live(apcie.rc_base + 0x3c,
                         "rc_base+0x3c (pre)", buf)
            try:
                p.set32(apcie.rc_base + 0x3c, 0x1)
            except Exception as e:
                buf.write(f"  L633 write FAILED: {e.__class__.__name__}: {e}\n")
                if not check_alive():
                    raise DumpAborted() from e
            _read32_live(apcie.rc_base + 0x3c,
                         "rc_base+0x3c (post-set)", buf)

            # L637: port_base + 0x10 = 0x2 (T602X APCIE only).
            buf.write(f"  L637: write32(port_base+0x10, 0x2)\n")
            _read32_live(pb + 0x10, f"port{i} +0x10 (pre)", buf)
            try:
                p.write32(pb + 0x10, 0x2)
            except Exception as e:
                buf.write(f"  L637 write FAILED: {e.__class__.__name__}: {e}\n")
                if not check_alive():
                    raise DumpAborted() from e
            _read32_live(pb + 0x10, f"port{i} +0x10 (post)", buf)

            # L653: port_base + 0x104 = 0x7fffffff (T602X flavor; overrides
            # m1n1's T8140 value 0xfffffff0).
            buf.write(f"  L653: write32(port_base+0x104, 0x7fffffff) "
                      f"(overriding T8140 0xfffffff0)\n")
            _read32_live(pb + 0x104, f"port{i} +0x104 (pre)", buf)
            try:
                p.write32(pb + 0x104, 0x7fffffff)
            except Exception as e:
                buf.write(f"  L653 write FAILED: {e.__class__.__name__}: {e}\n")
                if not check_alive():
                    raise DumpAborted() from e
            _read32_live(pb + 0x104, f"port{i} +0x104 (post)", buf)

            # L674: port_base + 0x397c = 0 (T602X only).
            buf.write(f"  L674: write32(port_base+0x397c, 0x0)\n")
            _read32_live(pb + 0x397c, f"port{i} +0x397c (pre)", buf)
            try:
                p.write32(pb + 0x397c, 0x0)
            except Exception as e:
                buf.write(f"  L674 write FAILED: {e.__class__.__name__}: {e}\n")
                if not check_alive():
                    raise DumpAborted() from e
            _read32_live(pb + 0x397c, f"port{i} +0x397c (post)", buf)

            # L716: clear32(port_phy_base + PHY_CTRL, 0x4000).
            # m1n1's T8140 branch cleared bit 0x10 instead. Our previous
            # dump showed bit 14 is already 0, so this is a no-op, but
            # replay it for parity.
            buf.write(f"  L716: clear32(port_phy_base+0x000, 0x4000)\n")
            _read32_live(phy + 0x0, f"port{i} PHY_CTRL (pre)", buf)
            try:
                p.clear32(phy + 0x0, 0x4000)
            except Exception as e:
                buf.write(f"  L716 write FAILED: {e.__class__.__name__}: {e}\n")
                if not check_alive():
                    raise DumpAborted() from e
            _read32_live(phy + 0x0, f"port{i} PHY_CTRL (post-clear-0x4000)", buf)

            time.sleep(0.05)
            snap("after T602X-only writes")

            if aggressive:
                # L737-742: T602X LTSSM debug writes (non-APCIE branch --
                # note m1n1 gates these on `controller != APCIE`, so the
                # main APCIE path never runs them; only APCIE_GE does).
                # Include here for A/B: on t8132 we may need them even for
                # main APCIE.
                buf.write(f"\n  L737: LTSSM debug (Tier 3 -- ltssm_base "
                          f"may AXI-stall)\n")
                log(f"    LTSSM debug writes on ltssm_base=0x{lt:x}")
                for lbl, addr, action in [
                    ("ltssm+0x10 = 0x2",    lt + 0x10, ("write", 0x2)),
                    ("ltssm+0x1c = 0x4",    lt + 0x1c, ("write", 0x4)),
                    ("ltssm+0x20 |= 0x2",   lt + 0x20, ("set",   0x2)),
                    ("ltssm+0x14 = 0x1",    lt + 0x14, ("write", 0x1)),
                ]:
                    buf.write(f"    write to {lbl}\n")
                    try:
                        if action[0] == "set":
                            p.set32(addr, action[1])
                        else:
                            p.write32(addr, action[1])
                    except Exception as e:
                        buf.write(f"    {lbl} write FAILED: "
                                  f"{e.__class__.__name__}: {e}\n")
                        if not check_alive():
                            raise DumpAborted() from e
                buf.write(f"  L742: clear32(port_base+0x800, 0x100)\n")
                try:
                    p.clear32(pb + 0x800, 0x100)
                except Exception as e:
                    buf.write(f"  L742 write FAILED: "
                              f"{e.__class__.__name__}: {e}\n")
                    if not check_alive():
                        raise DumpAborted() from e
                _read32_live(pb + 0x800,
                             f"port{i} APPCLK (post-clear-bit8)", buf)
                time.sleep(0.05)
                snap("after L737-742 LTSSM kick")

            if do_again:
                # L752-767: "Do it again?" -- T602X APCIE branch only.
                buf.write(f"\n  L752: cycle T602X_PORT_RESET "
                          f"(reassert + deassert)\n")
                log(f"    T602X_PORT_RESET cycle on port{i}")
                try:
                    p.clear32(pb + 0x82c, 0x1)
                except Exception as e:
                    buf.write(f"  L752 clear FAILED: "
                              f"{e.__class__.__name__}: {e}\n")
                    if not check_alive():
                        raise DumpAborted() from e
                _read32_live(pb + 0x82c,
                             f"port{i} T602X_RESET (reasserted)", buf)
                try:
                    p.set32(pb + 0x82c, 0x1)
                except Exception as e:
                    buf.write(f"  L754 set FAILED: "
                              f"{e.__class__.__name__}: {e}\n")
                    if not check_alive():
                        raise DumpAborted() from e
                _read32_live(pb + 0x82c,
                             f"port{i} T602X_RESET (deasserted)", buf)
                # m1n1 polls LINKSTS_BUSY = 0 for 250 ms here.
                time.sleep(0.25)
                snap("after T602X_PORT_RESET cycle")

                # udelay(1000).
                time.sleep(0.001)

                # L764-767: LTSSM debug writes again (Tier 3).
                buf.write(f"  L764: LTSSM debug (Tier 3)\n")
                log(f"    second LTSSM debug writes on ltssm_base=0x{lt:x}")
                for lbl, addr, action in [
                    ("ltssm+0x10 = 0x2",    lt + 0x10, ("write", 0x2)),
                    ("ltssm+0x1c = 0x4",    lt + 0x1c, ("write", 0x4)),
                    ("ltssm+0x20 |= 0x2",   lt + 0x20, ("set",   0x2)),
                    ("ltssm+0x14 = 0x1",    lt + 0x14, ("write", 0x1)),
                ]:
                    buf.write(f"    write to {lbl}\n")
                    try:
                        if action[0] == "set":
                            p.set32(addr, action[1])
                        else:
                            p.write32(addr, action[1])
                    except Exception as e:
                        buf.write(f"    {lbl} write FAILED: "
                                  f"{e.__class__.__name__}: {e}\n")
                        if not check_alive():
                            raise DumpAborted() from e
                time.sleep(0.05)
                snap("after do-again LTSSM kick")

            if do_msimap:
                # L845: populate MSIMAP with 0x80000000 | i. Big loop
                # (512 iterations); writes into port_base + 0x3800.
                buf.write(f"\n  L845: MSIMAP populate 512 entries\n")
                log(f"    MSIMAP populate on port{i}")
                for j in range(512):
                    addr = pb + T602X_PORT_MSIMAP + 4 * j
                    try:
                        p.write32(addr, 0x80000000 | j)
                    except Exception as e:
                        buf.write(f"    MSIMAP[{j}] @ 0x{addr:x} FAILED: "
                                  f"{e.__class__.__name__}: {e}\n")
                        if not check_alive():
                            raise DumpAborted() from e
                        break
                snap("after MSIMAP populate")

            # L839-843 epilogue writes we do NOT replay yet:
            #   write32(port_base + 0x4020, 0x3)
            #   write32(port_intr2axi_base + 0x80, 0x1)
            #   clear32(rc_base + 0x3c, 0x1)
            # rc_base+0x3c clear is important -- if the SET at L633 is a
            # "config-write-enable" mode, we must clear it to arm the port
            # for LTSSM training. Do that unconditionally here.
            buf.write(f"\n  L839: write32(port_base+0x4020, 0x3)\n")
            try:
                p.write32(pb + 0x4020, 0x3)
            except Exception as e:
                buf.write(f"  L839 write FAILED: "
                          f"{e.__class__.__name__}: {e}\n")
                if not check_alive():
                    raise DumpAborted() from e
            _read32_live(pb + 0x4020,
                         f"port{i} +0x4020 (post-write)", buf)

            if p_.intr2axi_base:
                buf.write(f"  L841: write32(intr2axi_base+0x80, 0x1)\n")
                try:
                    p.write32(p_.intr2axi_base + 0x80, 0x1)
                except Exception as e:
                    buf.write(f"  L841 write FAILED: "
                              f"{e.__class__.__name__}: {e}\n")
                    if not check_alive():
                        raise DumpAborted() from e
                _read32_live(p_.intr2axi_base + 0x80,
                             f"port{i} intr2axi+0x80 (post)", buf)

            buf.write(f"  L843: clear32(rc_base+0x3c, 0x1)  "
                      f"(disarm config-write mode)\n")
            _read32_live(apcie.rc_base + 0x3c,
                         "rc_base+0x3c (before final clear)", buf)
            try:
                p.clear32(apcie.rc_base + 0x3c, 0x1)
            except Exception as e:
                buf.write(f"  L843 clear FAILED: "
                          f"{e.__class__.__name__}: {e}\n")
                if not check_alive():
                    raise DumpAborted() from e
            _read32_live(apcie.rc_base + 0x3c,
                         "rc_base+0x3c (post-clear)", buf)

            # Extended settle to let LTSSM converge if it will.
            time.sleep(0.5)
            snap("after full T602X replay")
        except DumpAborted:
            log(f"    t602x replay aborted (m1n1 wedged) on port{i}")
            buf.write(f"[t602x_port_init_replay] aborted on port{i} "
                      f"-- m1n1 not responding\n")
            return


def try_ltssm_kick(apcie, buf, port_indices=None, aggressive=False):
    """After p.pcie_init() has returned (with ports stuck at LINKSTS_BUSY),
    try the LTSSM kick sequences that the T602X code paths use but the
    T8140/t8132 path skips. Read LINKSTS before and after each write so we
    can tell which one (if any) changed hardware state.

    Sequences tried in order per port:
      A) T602X APCIE:  rc_base+0x3c |= 0x1;  port_base+0x10 <- 0x2
      B) T602X non-APCIE LTSSM kick + APPCLK bit8 clear (writes to
         ltssm_base -- DANGEROUS, gated behind `aggressive`)
      C) cycle T602X_PORT_RESET (deassert, reassert, deassert)

    LINKSTS snapshots use the liveness-aware _read32_live so a wedged
    m1n1 causes an early bail rather than one 300 ms timeout per snap.
    """
    if port_indices is None:
        port_indices = apcie.active_ports
    buf.write("\n=== LTSSM kick experiment (post-init) ===\n")
    log(f"  try_ltssm_kick ports={list(port_indices)} "
        f"aggressive={aggressive}")

    for i in port_indices:
        p_ = apcie.ports[i]
        pb = p_.port_base
        lt = p_.ltssm_base
        name = f"port{i}"

        def snap(label):
            v = _read32_live(pb + 0x208,
                             f"{name} LINKSTS ({label})", buf)
            if v is not None:
                buf.write(f"  {name} {label:22s} LINKSTS decode: "
                          f"[{_linksts_decode(v)}]\n")

        buf.write(f"\n--- {name} @ port_base=0x{pb:x} ltssm=0x{lt:x} ---\n")
        try:
            snap("baseline")

            # Sequence A: T602X APCIE-branch kick (rc_base + port_base only).
            # Verify each write's readback so we can tell whether the
            # register accepted the write (previous run showed rc_base+0x3c
            # reading back 0 after set32).
            log(f"    seq A on {name}")
            _read32_live(apcie.rc_base + 0x3c,
                         f"rc_base+0x3c (seq A pre)", buf)
            buf.write(f"  seq A: set32(rc_base+0x3c, 0x1)  # 0x{apcie.rc_base + 0x3c:x}\n")
            try:
                p.set32(apcie.rc_base + 0x3c, 0x1)
            except Exception as e:
                buf.write(f"  seq A set32 FAILED: "
                          f"{e.__class__.__name__}: {e}\n")
                if not check_alive():
                    raise DumpAborted() from e
            _read32_live(apcie.rc_base + 0x3c,
                         f"rc_base+0x3c (seq A post-set)", buf)
            _read32_live(pb + 0x10, f"{name} +0x10 (seq A pre)", buf)
            buf.write(f"  seq A: write32(port_base+0x10, 0x2)\n")
            try:
                p.write32(pb + 0x10, 0x2)
            except Exception as e:
                buf.write(f"  seq A write32 FAILED: "
                          f"{e.__class__.__name__}: {e}\n")
                if not check_alive():
                    raise DumpAborted() from e
            _read32_live(pb + 0x10, f"{name} +0x10 (seq A post)", buf)
            time.sleep(0.01)
            snap("after seq A")

            # Sequence B: T602X non-APCIE LTSSM kick. Writes to ltssm_base
            # (Tier 3). If ltssm's clock is off, this AXI-stalls.
            if aggressive:
                log(f"    seq B on {name} (aggressive; writes ltssm_base)")
                for label, addr, val in [
                    ("ltssm+0x10", lt + 0x10, 0x2),
                    ("ltssm+0x1c", lt + 0x1c, 0x4),
                    ("ltssm+0x20 |= 0x2", lt + 0x20, None),
                    ("ltssm+0x14", lt + 0x14, 0x1),
                    ("port+0x800 &= ~0x100", pb + 0x800, None),
                ]:
                    buf.write(f"  seq B: write to {label}\n")
                    try:
                        if label.endswith("|= 0x2"):
                            p.set32(addr, 0x2)
                        elif label.startswith("port+0x800"):
                            p.clear32(addr, 0x100)
                        else:
                            p.write32(addr, val)
                    except Exception as e:
                        buf.write(f"  seq B write to {label} FAILED: "
                                  f"{e.__class__.__name__}: {e}\n")
                        if not check_alive():
                            raise DumpAborted() from e
                time.sleep(0.05)
                snap("after seq B")
            else:
                buf.write("  seq B: skipped (--ltssm-kick-aggressive to enable; "
                          "writes ltssm_base which may AXI-stall)\n")

            # Sequence C: cycle T602X_PORT_RESET.
            log(f"    seq C on {name}")
            buf.write(f"  seq C: clear+set T602X_RESET (port_base+0x82c)\n")
            try:
                p.clear32(pb + 0x82c, 0x1)
                time.sleep(0.001)
                p.set32(pb + 0x82c, 0x1)
            except Exception as e:
                buf.write(f"  seq C FAILED: {e.__class__.__name__}: {e}\n")
                if not check_alive():
                    raise DumpAborted() from e
            time.sleep(0.05)
            snap("after seq C")

            # Extended settle in case training is slow.
            time.sleep(0.2)
            snap("after 200ms settle")
        except DumpAborted:
            log(f"    LTSSM kick aborted (m1n1 wedged) on {name}")
            buf.write(f"[try_ltssm_kick] aborted -- m1n1 not responding\n")
            return


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
        v = _read32_live(port.port_base + 0x208,
                         f"port{port_index} LINKSTS ({label})", buf)
        if v is not None:
            buf.write(f"  {label:24s} LINKSTS decode: "
                      f"[{_linksts_decode(v)}]\n")
        return v

    try:
        baseline = snap("baseline")
        for off in _UNBLOCK_OFFSETS:
            addr = port.ctrl_lo_base + off
            log(f"    ctrl_lo +0x{off:04x} probe")
            before = _read32_live(addr, f"ctrl_lo +0x{off:04x} (pre)", buf)
            if before is None:
                buf.write(f"  ctrl_lo +0x{off:04x} read FAILED; skipping flip\n")
                continue
            buf.write(f"\n  flipping ctrl_lo +0x{off:04x} bit 0\n")
            try:
                p.set32(addr, 0x1)
            except Exception as e:
                buf.write(f"  ctrl_lo +0x{off:04x} write FAILED: "
                          f"{e.__class__.__name__}: {e}\n")
                if not check_alive():
                    raise DumpAborted() from e
                continue
            after_set = _read32_live(addr, f"ctrl_lo +0x{off:04x} (post)", buf)
            time.sleep(settle_ms / 1e3)
            snap(f"after set +0x{off:04x}")
            # Restore original bit-0 state to keep DART intact.
            if not (before & 0x1):
                try:
                    p.clear32(addr, 0x1)
                except Exception as e:
                    buf.write(f"  ctrl_lo +0x{off:04x} restore FAILED: "
                              f"{e.__class__.__name__}: {e}\n")
                    if not check_alive():
                        raise DumpAborted() from e
        buf.write("\n")
        final = snap("final")
        if baseline is not None and final is not None and baseline != final:
            buf.write(f"  ** LINKSTS changed: 0x{baseline:08x} "
                      f"-> 0x{final:08x} **\n")
    except DumpAborted:
        log("    unblock experiment aborted (m1n1 wedged)")
        buf.write(f"[unblock_experiment] aborted -- m1n1 not responding\n")


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
    ap.add_argument("--ltssm-kick-aggressive", action="store_true",
                    help="in try_ltssm_kick, also run seq B (writes ltssm_base "
                         "-- may AXI-stall if ltssm clock is off)")
    tier_group = ap.add_mutually_exclusive_group()
    tier_group.add_argument("--tier1", action="store_const", dest="tier",
                            const=1, help="dump only Tier 1 regs (safe, "
                                          "default)")
    tier_group.add_argument("--tier2", action="store_const", dest="tier",
                            const=2, help="dump Tier 1 + phy_common + "
                                          "per-port phy_base")
    tier_group.add_argument("--tier3", action="store_const", dest="tier",
                            const=3, help="dump Tier 1 + Tier 2 + ltssm + "
                                          "phy_extra + ctrl_lo. DANGEROUS "
                                          "(may AXI-stall on un-clocked blocks)")
    ap.set_defaults(tier=1)
    ap.add_argument("--dump-timeout", type=float, default=0.3,
                    help="UART timeout (seconds) during guarded read spans "
                         "(default: 0.3). Lower = faster fail on wedged m1n1.")
    ap.add_argument("--unblock-experiment", action="store_true",
                    help="poke bit 0 at a small set of ctrl_lo offsets on "
                         "the NIC port to see if any move LINKSTS off BUSY. "
                         "OFF by default -- writes go through DART MMIO alias.")
    ap.add_argument("--unblock-port", type=int, default=2,
                    help="port index for --unblock-experiment (default: 2)")
    ap.add_argument("--t602x-init", action="store_true",
                    help="after pcie_init, replay the T602X APCIE port init "
                         "sequence (m1n1 src/pcie.c:633-767 lines the T8140 "
                         "branch skips). Adds rc_base+0x3c gating, "
                         "port_base+0x10, port_base+0x104=0x7fffffff, "
                         "port_base+0x397c, PHY_CTRL &= ~0x4000. All Tier 1/2 "
                         "addresses. Safe to combine with --tier2 dump.")
    ap.add_argument("--t602x-aggressive", action="store_true",
                    help="in --t602x-init, also do the L737-742 LTSSM debug "
                         "writes on ltssm_base (Tier 3; may AXI-stall).")
    ap.add_argument("--t602x-do-again", action="store_true",
                    help="in --t602x-init, also do the L752-767 'do it again' "
                         "cycle (T602X_PORT_RESET reassert + LTSSM debug, "
                         "Tier 3 for the debug part).")
    ap.add_argument("--t602x-msimap", action="store_true",
                    help="in --t602x-init, also populate the 512-entry MSIMAP "
                         "table with 0x80000000|i. Big loop; port_base + "
                         "0x3800 + i*4 for i in 0..511.")
    ap.add_argument("--no-pcie-init", action="store_true",
                    help="SKIP p.pcie_init(). Use with the currently-shipped "
                         "m1n1 (t8132-pcie HEAD 6b277bc) which wedges inside "
                         "pcie_init while applying auspma tunables to port "
                         "1's inactive PHY slice. Skipping lets pre-init "
                         "probing + tunables report run to completion.")
    ap.add_argument("--no-phy-ip-report", action="store_true",
                    help="skip the ADT-only apcie-phy-ip-{pll,auspma}-tunables "
                         "report (default: enabled). Report is wedge-immune -- "
                         "no MMIO, only ADT parsing.")
    ap.add_argument("--preinit-probe", action="store_true",
                    help="probe shared apcie MMIO (rc_base, phy_common, "
                         "phy_ip head + per-port slice heads, axi_base) "
                         "BEFORE p.pcie_init(). Off by default because most "
                         "of these blocks need PMGR gates that only pcie_init "
                         "enables -- probing them unpowered may AXI-stall.")
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

    # ADT-only, wedge-immune. Runs regardless of --no-pcie-init so we
    # always leave a full tunable dump in nic-runtime.txt.
    if not args.no_phy_ip_report:
        log("dumping apcie-phy-ip-{pll,auspma}-tunables report...")
        try_(lambda: dump_phy_ip_tunables_report(apcie, buf),
             "dump_phy_ip_tunables_report")

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

    timeout = args.dump_timeout

    if args.preinit_probe:
        log("probing shared apcie MMIO pre-pcie_init...")
        with guarded(buf, "probe_preinit_regs", short_timeout=timeout):
            try_(lambda: probe_preinit_regs(apcie, buf, timeout=timeout),
                 "probe_preinit_regs")

    pcie_init_ok = False
    if args.no_pcie_init:
        log("SKIPPING p.pcie_init() (--no-pcie-init)")
        buf.write("\np.pcie_init() skipped (--no-pcie-init).\n"
                  "Current m1n1 (6b277bc) wedges here on j773g -- see\n"
                  "phy_ip tunables report above for the port-1 hypothesis.\n\n")
    else:
        log("p.pcie_init()...")
        try:
            rc = p.pcie_init()
            buf.write(f"\np.pcie_init() -> {rc!r}\n\n")
            log(f"p.pcie_init returned {rc!r}")
            pcie_init_ok = True
        except Exception as e:
            buf.write(f"\np.pcie_init raised: {e.__class__.__name__}: {e}\n\n")
            log(f"p.pcie_init raised: {e.__class__.__name__}: {e}")
            traceback.print_exc(limit=5)

    def liveness_gate(label):
        """Skip subsequent sections if m1n1 died in a previous one.
        Returns True if m1n1 is still alive."""
        if check_alive(timeout=max(timeout, 0.5)):
            return True
        log(f"m1n1 DEAD before {label}; skipping this and all further sections")
        buf.write(f"[liveness] {label}: m1n1 not responding, skipping\n")
        return False

    if pcie_init_ok:
        log(f"active ports (per ADT): {apcie.active_ports}")
        log(f"dumping PCIe controller registers (post-init, tier={args.tier})...")
        with guarded(buf, "dump_pcie_regs(post-init)",
                     short_timeout=timeout):
            try_(lambda: dump_pcie_regs(apcie, buf, "post-init",
                                        tier=args.tier),
                 "dump_pcie_regs")

        if args.no_ltssm_kick:
            log("LTSSM kick skipped (--no-ltssm-kick)")
            buf.write("\n=== LTSSM kick experiment (skipped) ===\n\n")
        elif liveness_gate("LTSSM kick"):
            log(f"trying LTSSM kick sequences on ports "
                f"{apcie.active_ports} (aggressive="
                f"{args.ltssm_kick_aggressive})...")
            with guarded(buf, "try_ltssm_kick",
                         short_timeout=timeout):
                try_(lambda: try_ltssm_kick(
                        apcie, buf,
                        aggressive=args.ltssm_kick_aggressive),
                     "try_ltssm_kick")

            if liveness_gate("post-kick dump"):
                log(f"dumping PCIe controller registers "
                    f"(post-kick, tier={args.tier})...")
                with guarded(buf, "dump_pcie_regs(post-kick)",
                             short_timeout=timeout):
                    try_(lambda: dump_pcie_regs(apcie, buf, "post-kick",
                                                tier=args.tier),
                         "dump_pcie_regs")

        if args.t602x_init and liveness_gate("T602X init replay"):
            log(f"replaying T602X APCIE port init on ports "
                f"{apcie.active_ports} (aggressive="
                f"{args.t602x_aggressive}, do_again={args.t602x_do_again}, "
                f"msimap={args.t602x_msimap})...")
            with guarded(buf, "t602x_port_init_replay",
                         short_timeout=timeout):
                try_(lambda: t602x_port_init_replay(
                        apcie, buf,
                        aggressive=args.t602x_aggressive,
                        do_again=args.t602x_do_again,
                        do_msimap=args.t602x_msimap),
                     "t602x_port_init_replay")
            if liveness_gate("post-T602X dump"):
                log(f"dumping PCIe controller registers "
                    f"(post-t602x, tier={args.tier})...")
                with guarded(buf, "dump_pcie_regs(post-t602x)",
                             short_timeout=timeout):
                    try_(lambda: dump_pcie_regs(apcie, buf, "post-t602x",
                                                tier=args.tier),
                         "dump_pcie_regs")

        if args.unblock_experiment and liveness_gate("unblock experiment"):
            log(f"running unblock experiment on port {args.unblock_port}...")
            with guarded(buf, "unblock_experiment",
                         short_timeout=timeout):
                try_(lambda: unblock_experiment(apcie, buf,
                                                port_index=args.unblock_port),
                     "unblock_experiment")
            if liveness_gate("post-unblock dump"):
                log(f"dumping PCIe controller registers "
                    f"(post-unblock, tier={args.tier})...")
                with guarded(buf, "dump_pcie_regs(post-unblock)",
                             short_timeout=timeout):
                    try_(lambda: dump_pcie_regs(apcie, buf, "post-unblock",
                                                tier=args.tier),
                         "dump_pcie_regs")
    else:
        log("skipping PCIe register dump (m1n1 is wedged, reads would time out)")
        buf.write("=== PCIe controller register dump ===\n"
                  "SKIPPED: p.pcie_init() raised -- m1n1 is not responding.\n\n")

    if liveness_gate("ECAM walk"):
        log(f"ECAM walk @ 0x{apcie.ecam_base:x} ...")
        with guarded(buf, "ecam_walk", short_timeout=timeout):
            devices = try_(lambda: ecam_walk(apcie.ecam_base, buf,
                                             active_ports=apcie.active_ports),
                           "ecam_walk") or []
    else:
        devices = []

    # Pick the NIC: class-0x02 device on any downstream bus.
    nic = None
    for d in devices:
        if d.get("cls_base") == 0x02:
            nic = d
            break

    if nic is not None and liveness_gate("enable_nic"):
        with guarded(buf, "enable_nic", short_timeout=timeout):
            try_(lambda: enable_nic(apcie.ecam_base, nic, buf), "enable_nic")
    elif nic is None:
        buf.write("\n=== enable NIC ===\nNo class-0x02 device found.\n")

    summarize(out / "nic-runtime.txt", buf, devices, nic)
    log("done. Copy /tmp/m4-recon/nic-runtime.txt into m4_recon/ if it looks sane.")


if __name__ == "__main__":
    main()
