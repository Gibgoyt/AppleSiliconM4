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


def check_alive_fast(exc_count_before, timeout=0.3):
    """Cheaper liveness probe than check_alive() -- gets exc_count once,
    returns (alive, delta). Use INSIDE a guarded read loop where a
    full proxy round-trip on top of an already-faulted read may itself
    wedge. This still makes one proxy call, but only one, and with
    a strict short timeout.
    """
    old = None
    try:
        old = iface.dev.timeout
        iface.dev.timeout = timeout
    except Exception:
        pass
    try:
        cnt = p.get_exc_count()
        return True, cnt - exc_count_before
    except Exception:
        return False, -1
    finally:
        if old is not None:
            try:
                iface.dev.timeout = old
            except Exception:
                pass


def flush_partial_log(out_path, buf, tag):
    """Persist buf to out_path mid-run so a subsequent wedge doesn't
    destroy the log we already have. Call after every stable section
    in main().
    """
    try:
        out_path.write_text(buf.getvalue())
        log(f"[flush:{tag}] wrote partial log ({len(buf.getvalue())} bytes) "
            f"to {out_path}")
    except Exception as e:
        log(f"[flush:{tag}] FAILED: {e.__class__.__name__}: {e}")


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


# ---------------------------------- t8132 extra tunables (cio3pllcore, pcieclkgen)

# t8132's ADT has two apcie-node tunables that m1n1's pcie.c (as of
# v1.6.0-rc1-56-g6b277bc) does not apply on any codepath -- they were
# added to the ADT for this SoC but never wired into the C driver:
#
#   apcie-cio3pllcore-tunables  -- suspected CIO3 PLL core init.
#                                  Likely provides the reference clock
#                                  the phy_ip window needs before its
#                                  MMIO becomes readable/writable.
#
#   apcie-pcieclkgen-tunables   -- suspected PCIe clock generator init.
#                                  Together with cio3pllcore this is
#                                  the missing "ungate phy_ip" step.
#
# On the 2026-07-11 run, Phase F got as far as step 6.g and wedged on
# the first phy_ip write (mask32 @ 0x497040038) despite the phy_shared
# CLK0/CLK1 handshake succeeding. Our current hypothesis: applying
# these two tunable groups BEFORE 6.g is what unblocks phy_ip.
_EXTRA_TUNABLE_PROPS = (
    "apcie-cio3pllcore-tunables",
    "apcie-pcieclkgen-tunables",
)


def _infer_tunable_target_reg(apcie, entries):
    """Guess which reg[] index a tunables prop targets based on the max
    offset in its entries vs the ADT reg block sizes. Returns
    (reg_idx, base_addr, size, reason) or (None, None, None, reason).

    Heuristic: pick the smallest reg[] whose size strictly contains the
    max offset. Matches how m1n1's C-side `tunables_apply_local` works
    (it takes a reg_idx and adds tunable->offset to that base).
    """
    if not entries:
        return None, None, None, "no entries"
    max_off = max(off for off, _sz, _mask, _val in entries)
    min_off = min(off for off, _sz, _mask, _val in entries)
    candidates = [
        (1, "rc_base",         apcie.rc_base,         apcie.rc_size),
        (2, "phy_packed_base", apcie.phy_packed_base, apcie.phy_packed_size),
        (3, "phy_ip_base",     apcie.phy_ip_base,     apcie.phy_ip_size),
        (4, "axi_base",        apcie.axi_base,        apcie.axi_size),
    ]
    valid = [c for c in candidates if max_off < c[3]]
    if not valid:
        return None, None, None, (f"max_off=0x{max_off:x} exceeds "
                                  f"every reg block size")
    # Pick the smallest reg block that fits (index 3 = size)
    valid.sort(key=lambda c: c[3])
    idx, name, base, size = valid[0]
    reason = (f"max_off=0x{max_off:x} min_off=0x{min_off:x} "
              f"fits in {name} (reg[{idx}], size 0x{size:x})")
    return idx, base, size, reason


def dump_extra_tunables_report(apcie, buf):
    """Dump apcie-cio3pllcore-tunables + apcie-pcieclkgen-tunables per
    entry, with target reg inference. ADT-only, wedge-immune -- safe
    even if the AXI fabric is dead.

    For each prop:
      * lists every entry (offset, size, mask, value)
      * infers the likely reg_idx and target base
      * prints target_addr = base + offset per entry
      * flags entries whose target lands in a known-inactive port slice
        of phy_ip_base (would AXI-stall like the port-1 auspma entries)
    """
    buf.write("=== extra apcie tunables report (t8132-specific, "
              "from ADT, no MMIO) ===\n")
    buf.write("These are the two apcie-node tunable properties that\n"
              "m1n1's pcie.c does NOT apply on the T8140 codepath.\n"
              "They may be the missing PCIe clock/PLL setup that the\n"
              "phy_ip window needs before its MMIO becomes reachable.\n\n")

    for prop in _EXTRA_TUNABLE_PROPS:
        entries = apcie.apcie_tunables(u, prop)
        buf.write(f"--- {prop} ({len(entries)} entries) ---\n")
        if not entries:
            buf.write("  (property missing from ADT)\n\n")
            continue

        idx, base, size, reason = _infer_tunable_target_reg(apcie, entries)
        buf.write(f"  target inference: {reason}\n")
        if idx is None:
            buf.write("  --> can NOT confidently apply this prop\n\n")
            continue
        buf.write(f"  --> reg_idx={idx}, base=0x{base:x}, size=0x{size:x}\n\n")

        buf.write("  #  offset     sz  mask               "
                  "value              -> target_addr    warning\n")
        for i, (off, size_bytes, mask, val) in enumerate(entries):
            target = base + off
            warning = ""
            # If we inferred phy_ip_base as the target, run the
            # slice-classifier so we see inactive port entries.
            if idx == 3:
                info = apcie.classify_phy_ip_offset(off)
                if info["kind"] == "port_slice" and not info["port_active"]:
                    warning = (f"INACTIVE port{info['port_index']} "
                               f"(would AXI-stall)")
            buf.write(f"  {i:3d} 0x{off:08x} {size_bytes:2d}  "
                      f"0x{mask:016x} 0x{val:016x}    "
                      f"0x{target:09x}  {warning}\n")
        buf.write("\n")


# ---------------------------------- pre-pcie_init probes (phase 0/A/B/C)

def _decode_pmgr_name(dev):
    n = getattr(dev, "name", None)
    if n is None:
        return "?"
    if isinstance(n, (bytes, bytearray)):
        try:
            return n.rstrip(b"\x00").decode("ascii", "replace")
        except Exception:
            return n.hex()
    return str(n)


# PS register (Apple PMGR device state) field layout on t8xxx. Authoritative
# source: m1n1/src/pmgr.c:9-17 and pmgr.h:13-15.
#   bits [3:0]   PS_TARGET  -- requested power state
#   bits [7:4]   PS_ACTUAL  -- current power state
#   bit  8       WAS_PWRGATED (sticky)
#   bit  9       WAS_CLKGATED (sticky)
#   bit  10      DEV_DISABLE
#   bit  11      PARENT_OFF
#   bits [27:24] PS_AUTO    -- auto-clockgate floor when idle
#   bit  28      AUTO_ENABLE
#   bit  31      RESET
# State encoding: 0xf = ACTIVE (ON), 0x4 = CLKGATE, 0x0 = PWRGATE (OFF).
PMGR_PS_TARGET_MASK   = 0x0000000f
PMGR_PS_ACTUAL_MASK   = 0x000000f0
PMGR_WAS_PWRGATED     = 1 << 8
PMGR_WAS_CLKGATED     = 1 << 9
PMGR_DEV_DISABLE      = 1 << 10
PMGR_PARENT_OFF       = 1 << 11
PMGR_PS_AUTO_MASK     = 0x0f000000
PMGR_AUTO_ENABLE      = 1 << 28
PMGR_RESET            = 1 << 31

PMGR_PS_ACTIVE  = 0xf
PMGR_PS_CLKGATE = 0x4
PMGR_PS_PWRGATE = 0x0


def _decode_ps_state(val):
    """Return short human summary of interesting bits in a PS reg value."""
    parts = []
    ps_auto = (val & PMGR_PS_AUTO_MASK) >> 24
    if val & PMGR_AUTO_ENABLE:
        parts.append(f"auto_enable ps_auto=0x{ps_auto:x}")
    elif ps_auto != 0:
        parts.append(f"ps_auto=0x{ps_auto:x}")
    if val & PMGR_WAS_PWRGATED:
        parts.append("was_pwrgated")
    if val & PMGR_WAS_CLKGATED:
        parts.append("was_clkgated")
    if val & PMGR_DEV_DISABLE:
        parts.append("dev_disable")
    if val & PMGR_PARENT_OFF:
        parts.append("parent_off")
    if val & PMGR_RESET:
        parts.append("RESET")
    return ", ".join(parts) if parts else "-"


def _read_pmgr_gate_state(dev_by_idx, gate, buf, indent="  "):
    """Read one PMGR gate's PS register. Returns dict or None on failure.

    Virtual (no_ps) devices are pure parent-chain aggregators with no real
    PS register; report them explicitly and skip the address read (which
    would otherwise silently read pmgr_base+0 -- a garbage die-info fallback
    that used to be labelled "ON" in the log).
    """
    dev = dev_by_idx.get(int(gate))
    if dev is None:
        buf.write(f"{indent}gate {gate:4d}: <NOT FOUND in pmgr.devices>\n")
        return None
    name = _decode_pmgr_name(dev)

    if dev.flags.no_ps:
        # Matches m1n1's `flags & PMGR_FLAG_VIRTUAL` skip in pmgr.c:166,185.
        on_hint = bool(dev.flags.on)
        buf.write(f"{indent}gate {gate:4d} name={name!r:24s} "
                  f"VIRTUAL (no PS reg, flags.on={on_hint})\n")
        return {"gate": int(gate), "name": name, "virtual": True,
                "on": on_hint, "ps_addr": None, "raw": None,
                "target": None, "actual": None}

    try:
        ps_addr = u.adt.pmgr_dev_get_addr(dev)
    except Exception as e:
        buf.write(f"{indent}gate {gate:4d} name={name!r:24s} "
                  f"pmgr_dev_get_addr failed: "
                  f"{e.__class__.__name__}: {e}\n")
        return None
    try:
        ps_val = p.read32(ps_addr)
    except Exception as e:
        buf.write(f"{indent}gate {gate:4d} name={name!r:24s} "
                  f"ps@0x{ps_addr:x} read FAILED "
                  f"({e.__class__.__name__}: {e})\n")
        return None
    target = ps_val & PMGR_PS_TARGET_MASK
    actual = (ps_val & PMGR_PS_ACTUAL_MASK) >> 4
    on = (actual == PMGR_PS_ACTIVE)
    buf.write(f"{indent}gate {gate:4d} name={name!r:24s} "
              f"ps@0x{ps_addr:x} = 0x{ps_val:08x} "
              f"(target=0x{target:x}, actual=0x{actual:x}, "
              f"{'ON' if on else 'OFF'}"
              f"; {_decode_ps_state(ps_val)})\n")
    return {"gate": int(gate), "name": name, "virtual": False,
            "ps_addr": ps_addr, "raw": ps_val,
            "target": target, "actual": actual, "on": on}


def _load_pmgr_devices(buf):
    """Return (pmgr_node, dev_by_idx) or (None, None) on failure."""
    try:
        pmgr = u.adt["arm-io/pmgr"]
    except Exception as e:
        buf.write(f"  ERROR: cannot open /arm-io/pmgr: "
                  f"{e.__class__.__name__}: {e}\n\n")
        return None, None
    dev_by_idx = {}
    try:
        for dev in pmgr.devices:
            try:
                idx = int(u.adt.pmgr_dev_get_id(dev))
                dev_by_idx[idx] = dev
            except Exception:
                continue
    except Exception as e:
        buf.write(f"  ERROR: pmgr.devices enumeration failed: "
                  f"{e.__class__.__name__}: {e}\n\n")
        return None, None
    return pmgr, dev_by_idx


def probe_phase0_pmgr_state(apcie, buf):
    """Phase 0 -- PMGR gate readout for apcie's power_gates.

    ZERO apcie MMIO. Only touches /arm-io/pmgr registers, which are
    always reachable (SMC + iBoot have PMGR up long before any driver
    runs). Reveals which gates SMC gP0d=0x800001 already turned on.

    Caches state on `apcie.phase0_gate_state` (gate_id -> state dict) so
    Phase A can infer MMIO reachability without doing MMIO. See
    `_read_pmgr_gate_state` above for the PS register bit layout; virtual
    (no_ps) devices are reported as such and never dereferenced.
    """
    buf.write("=== Phase 0: PMGR gate state (no apcie MMIO) ===\n")
    buf.write(f"apcie.power_gates from ADT: {list(apcie.power_gates)}\n\n")

    apcie.phase0_gate_state = {}
    apcie.phase0_dev_by_idx = None

    _pmgr, dev_by_idx = _load_pmgr_devices(buf)
    if dev_by_idx is None:
        return
    apcie.phase0_dev_by_idx = dev_by_idx

    for gate in apcie.power_gates:
        st = _read_pmgr_gate_state(dev_by_idx, gate, buf)
        if st is not None:
            apcie.phase0_gate_state[int(gate)] = st
    buf.write("\n")


def _walk_pmgr_parents(dev_by_idx, dev, visited, depth, buf, indent):
    """Recursively read PS state for each parent of `dev`.

    ZERO apcie MMIO. Only reads PMGR PS registers via
    _read_pmgr_gate_state, which is safe on all boots. Cycle-guarded via
    `visited` (set of gate IDs already walked in this chain) and
    depth-bounded to 10 to survive malformed ADTs.
    """
    if depth > 10:
        buf.write(f"{indent}(depth limit reached)\n")
        return
    try:
        parents = u.adt.pmgr_dev_get_parents(dev)
    except Exception as e:
        buf.write(f"{indent}<parents accessor failed: "
                  f"{e.__class__.__name__}: {e}>\n")
        return
    any_parent = False
    for pid in parents:
        pid = int(pid)
        if pid == 0:
            continue
        any_parent = True
        if pid in visited:
            buf.write(f"{indent}parent {pid}: <cycle -- already visited>\n")
            continue
        visited.add(pid)
        pdev = dev_by_idx.get(pid)
        if pdev is None:
            buf.write(f"{indent}parent {pid}: <NOT FOUND in pmgr.devices>\n")
            continue
        _read_pmgr_gate_state(dev_by_idx, pid, buf, indent=indent)
        _walk_pmgr_parents(dev_by_idx, pdev, visited, depth + 1, buf,
                           indent=indent + "  ")
    if not any_parent and depth == 1:
        buf.write(f"{indent}(no parents)\n")


def probe_phase0_5_parents(apcie, buf):
    """Phase 0.5 -- PMGR parent-chain readout for every apcie gate.

    ZERO apcie MMIO. Only touches /arm-io/pmgr registers. Explains why
    m1n1's pmgr_set_mode_recursive() may wedge inside
    pmgr_adt_power_enable('/arm-io/apcie'): the recursive walk RMWs
    every ancestor's PS register. If any ancestor is in a state where
    the RMW-then-poll blocks (e.g. an unclocked PMGR clock domain that
    only a downstream helper knows how to raise), m1n1 hangs inside
    poll32() -- exactly the "TTY> Exception: SYNC" wedge we see.

    Output layout: for each apcie gate, dump its state (already covered
    in Phase 0) then walk each parent chain, printing PS state at every
    level with increasing indent.
    """
    buf.write("=== Phase 0.5: PMGR parent-chain readout ===\n")
    dev_by_idx = getattr(apcie, "phase0_dev_by_idx", None)
    if dev_by_idx is None:
        buf.write("  ERROR: Phase 0 did not populate dev_by_idx; skipping\n\n")
        return
    for gate in apcie.power_gates:
        dev = dev_by_idx.get(int(gate))
        if dev is None:
            buf.write(f"  gate {gate}: not found in pmgr.devices; skipping\n")
            continue
        name = _decode_pmgr_name(dev)
        buf.write(f"  --- gate {gate} ({name!r}) parent chain ---\n")
        visited = {int(gate)}
        try:
            _walk_pmgr_parents(dev_by_idx, dev, visited, 1, buf,
                               indent="    ")
        except Exception as e:
            buf.write(f"    ERROR: parent walk failed: "
                      f"{e.__class__.__name__}: {e}\n")
    buf.write("\n")


def probe_phaseA_preinit_single(apcie, buf, timeout=0.2):
    """Phase A -- pre-PMGR MMIO reachability INFERENCE (no MMIO).

    Prior versions did one guarded read at phy_ip_base+0x0. That read
    wedges m1n1: an ungated AXI slave takes a SYNC exception whose
    handler prints 'TTY> Exception: SYNC' and then leaves the proxy
    reply state half-populated -- control never returns to Python, and
    check_alive_fast() cannot recover because the caller never gets
    back from the read call. GUARD.SKIP only covers SLVERR-shaped
    aborts, not the AXI stall / SError path that ungated slaves take
    on t8132.

    This version does ZERO MMIO. It uses `apcie.phase0_gate_state`
    (populated by probe_phase0_pmgr_state) to infer which shared apcie
    blocks would be reachable NOW, then reports the decision.

    Sets `apcie.preinit_reads_safe` (bool) so downstream phases can
    decide whether to attempt guarded MMIO or wait for Phase B's PMGR
    enable. Never touches the AXI fabric; safe to run even if the
    fabric has already wedged.
    """
    buf.write("=== Phase A: pre-PMGR reachability inference (no MMIO) ===\n")

    state = getattr(apcie, "phase0_gate_state", None)
    if not state:
        buf.write("  ERROR: no phase0_gate_state cached -- Phase 0 did not "
                  "run or found no gates. Cannot infer reachability.\n")
        buf.write("  --> assuming pre-PMGR reads are NOT safe\n\n")
        apcie.preinit_reads_safe = False
        return

    # Virtual gates have no MMIO of their own; they cannot gate the
    # shared apcie MMIO blocks. Only real (non-no_ps) gates matter for
    # reachability inference.
    real_gates = {g: st for g, st in state.items() if not st.get("virtual")}
    all_on = all(st["on"] for st in real_gates.values()) if real_gates else False
    off_gates = [(g, st["name"], st["target"], st["actual"])
                 for g, st in real_gates.items() if not st["on"]]

    shared_blocks = [
        ("rc_base",         apcie.rc_base,         apcie.rc_size),
        ("phy_common_base", apcie.phy_common_base, apcie.phy_packed_size),
        ("phy_ip_base",     apcie.phy_ip_base,     apcie.phy_ip_size),
        ("axi_base",        apcie.axi_base,        apcie.axi_size),
    ]
    buf.write("  shared apcie MMIO blocks (all behind the apcie power_gates "
              "as a set):\n")
    for name, base, size in shared_blocks:
        buf.write(f"    {name:16s} 0x{base:09x}  size 0x{size:x}\n")

    buf.write("\n  gate summary from Phase 0:\n")
    for gate, st in state.items():
        if st.get("virtual"):
            buf.write(f"    gate {gate:4d} name={st['name']!r:24s} "
                      f"VIRTUAL (flags.on={st['on']})\n")
        else:
            buf.write(f"    gate {gate:4d} name={st['name']!r:24s} "
                      f"{'ON' if st['on'] else 'OFF'} "
                      f"(target=0x{st['target']:x}, "
                      f"actual=0x{st['actual']:x})\n")

    if all_on:
        apcie.preinit_reads_safe = True
        buf.write("\n  --> all real apcie gates ON pre-pcie_init; pre-PMGR "
                  "MMIO reads would be safe (SMC gP0d already powered them "
                  "up).\n"
                  "      Phase B will still re-run pmgr_adt_power_enable to "
                  "match m1n1 pcie.c:425 ordering, then read the blocks.\n\n")
    else:
        apcie.preinit_reads_safe = False
        buf.write("\n  --> the following real apcie gates are not ACTIVE; "
                  "ANY read into their MMIO would SYNC-abort and wedge "
                  "m1n1:\n")
        for gate, name, target, actual in off_gates:
            buf.write(f"      gate {gate} ({name}) "
                      f"target=0x{target:x} actual=0x{actual:x}\n")
        buf.write("      Skipping pre-PMGR MMIO. Phase B will call "
                  "p.pmgr_adt_power_enable('/arm-io/apcie') first, then "
                  "read.\n\n")


def probe_phaseB_apcie_pmgr(apcie, buf, timeout=0.3):
    """Phase B -- enable apcie PMGR from Python + shared MMIO probe.

    Calls p.pmgr_adt_power_enable('/arm-io/apcie') which is the same C
    helper m1n1's pcie.c:425 uses. This turns on the individual per-block
    clocks/gates that make rc_base / phy_common / phy_ip / axi_base
    readable, WITHOUT running the tunable-application code that wedges
    the current m1n1.

    Then REVERIFIES every apcie gate via PMGR PS-register reads (not
    MMIO into the apcie fabric -- PMGR is always reachable). Only if
    every gate is now ON does it proceed to guarded reads of the shared
    MMIO probe list. This prevents the exact Phase A wedge from
    repeating here if pmgr_adt_power_enable silently fails for any
    subset of the gates.

    Caches the post-PMGR gate state on `apcie.phaseB_gate_state` so
    Phase C can gate its per-port MMIO reads on it.
    """
    buf.write("=== Phase B: apcie PMGR enable + shared MMIO probe ===\n")
    apcie.phaseB_gate_state = {}

    try:
        exc_before = p.get_exc_count()
    except Exception as e:
        buf.write(f"  ERROR: get_exc_count pre-PMGR failed "
                  f"({e.__class__.__name__}: {e})\n\n")
        return

    try:
        p.pmgr_adt_power_enable("/arm-io/apcie")
        buf.write("  p.pmgr_adt_power_enable('/arm-io/apcie') -> ok\n")
    except Exception as e:
        buf.write(f"  p.pmgr_adt_power_enable raised: "
                  f"{e.__class__.__name__}: {e}\n\n")
        return

    try:
        exc_after_pmgr = p.get_exc_count()
    except Exception as e:
        buf.write(f"  ERROR: get_exc_count post-PMGR failed "
                  f"({e.__class__.__name__}: {e})\n\n")
        return
    buf.write(f"  exc_count during PMGR enable: "
              f"{exc_before} -> {exc_after_pmgr} "
              f"(delta={exc_after_pmgr - exc_before})\n\n")

    # Verify every gate is now ON. This is the belt-and-braces check
    # that keeps Phase A from repeating here: if pmgr_adt_power_enable
    # returns but some gate stayed OFF (e.g. missing PMGR dependency,
    # ADT typo), we would AXI-abort on the first read into its MMIO.
    buf.write("  gate re-verify (via PMGR PS regs, no apcie MMIO):\n")
    dev_by_idx = getattr(apcie, "phase0_dev_by_idx", None)
    if dev_by_idx is None:
        _pmgr, dev_by_idx = _load_pmgr_devices(buf)
        if dev_by_idx is None:
            buf.write("  ERROR: cannot load pmgr devices; bailing Phase B\n\n")
            return

    all_on = True
    for gate in apcie.power_gates:
        st = _read_pmgr_gate_state(dev_by_idx, gate, buf, indent="    ")
        if st is None:
            all_on = False
            continue
        apcie.phaseB_gate_state[int(gate)] = st
        # Virtual devices have no PS reg and are always considered ON
        # (they only aggregate parents); real devices must reach ACTIVE.
        if not st.get("virtual") and not st["on"]:
            all_on = False

    if not all_on:
        buf.write("\n  !!! at least one real apcie gate is still not "
                  "ACTIVE after pmgr_adt_power_enable; refusing to touch "
                  "shared MMIO. Bailing Phase B (m1n1 stays alive).\n\n")
        return
    buf.write("  all real apcie gates ACTIVE; shared MMIO reads are safe.\n\n")

    probes = [
        (apcie.rc_base + 0x00,          "rc_base +0x00"),
        (apcie.rc_base + 0x04,          "rc_base +0x04"),
        (apcie.rc_base + 0x24,          "rc_base +0x24 (PHYIF_CTRL)"),
        (apcie.rc_base + 0x3c,          "rc_base +0x3c"),
        (apcie.rc_base + 0x50,          "rc_base +0x50"),
        (apcie.rc_base + 0x54,          "rc_base +0x54"),
        (apcie.rc_base + 0x58,          "rc_base +0x58"),
        (apcie.phy_common_base + 0x00,  "phy_common +0x00 (PHYCMN_CLK)"),
        (apcie.phy_ip_base + 0x00,      "phy_ip +0x00 (PLL area head)"),
        (apcie.phy_ip_base + 0x8000,    "phy_ip +0x08000 (port 0 slice head)"),
        (apcie.phy_ip_base + 0x10000,   "phy_ip +0x10000 (port 1 slice head -- INACTIVE)"),
        (apcie.phy_ip_base + 0x18000,   "phy_ip +0x18000 (port 2 slice head)"),
        (apcie.axi_base + 0x00,         "axi_base +0x00"),
    ]

    try:
        exc_running = p.get_exc_count()
    except Exception as e:
        buf.write(f"  ERROR: get_exc_count pre-read failed "
                  f"({e.__class__.__name__}: {e})\n\n")
        return

    for addr, label in probes:
        with guarded(buf, f"phaseB.0x{addr:x}", short_timeout=timeout):
            val = _safe_read32(addr)
        alive, delta = check_alive_fast(exc_running, timeout=timeout)
        buf.write(f"  read32(0x{addr:09x}) [{label}] = {val} "
                  f"(delta={delta}, alive={alive})\n")
        if not alive:
            buf.write("  !!! m1n1 unresponsive; bailing Phase B\n")
            return
        if delta != 0:
            buf.write("  !!! SYNC/SError on this read; bailing Phase B\n")
            return
        exc_running += delta
    buf.write("\n")


def probe_phaseC_port_pmgr(apcie, buf, timeout=0.3):
    """Phase C -- per-active-port PMGR + port_base probe.

    Refuses to run unless Phase B confirmed every shared apcie gate is
    ON: port_base is decoded by the same apcie AXI slave as rc_base, so
    any read into port_base with an OFF apcie gate would repeat the
    Phase A wedge.

    For each port whose pci-bridge{N} exists in the ADT, call
    p.pmgr_adt_power_enable(port.bridge_path) (mirrors m1n1's per-port
    loop in pcie.c), then re-read the shared apcie gates once to catch
    a silent PMGR flap, then probe the port_base Tier 1 register set
    with per-read exc_count bail.

    Compare the results here vs. pcie_up_1.log's post-init state to
    infer which specific registers m1n1's LATER init steps write to.
    """
    buf.write("=== Phase C: per-port PMGR enable + port_base probe ===\n")

    phaseB = getattr(apcie, "phaseB_gate_state", None)
    if not phaseB:
        buf.write("  ERROR: Phase B did not populate phaseB_gate_state.\n"
                  "  Refusing to touch port_base MMIO without a proven-safe\n"
                  "  shared apcie gate state (would risk a Phase A style\n"
                  "  wedge). Re-run with --pmgr-enable to populate.\n\n")
        return
    if not all(st.get("virtual") or st["on"] for st in phaseB.values()):
        buf.write("  ERROR: Phase B saw at least one real apcie gate not "
                  "ACTIVE; port_base reads are unsafe. Bailing Phase C.\n\n")
        return

    dev_by_idx = getattr(apcie, "phase0_dev_by_idx", None)
    if dev_by_idx is None:
        _pmgr, dev_by_idx = _load_pmgr_devices(buf)

    for port in apcie.ports:
        if not port.exists:
            buf.write(f"  port{port.index}: skipped "
                      f"(no pci-bridge{port.index} in ADT)\n")
            continue
        buf.write(f"  -- port{port.index} (bridge={port.bridge_path}) --\n")
        try:
            exc_before = p.get_exc_count()
        except Exception as e:
            buf.write(f"    ERROR: get_exc_count failed "
                      f"({e.__class__.__name__}: {e})\n")
            return
        try:
            p.pmgr_adt_power_enable(port.bridge_path)
            buf.write(f"    p.pmgr_adt_power_enable('{port.bridge_path}') -> ok\n")
        except Exception as e:
            buf.write(f"    p.pmgr_adt_power_enable raised: "
                      f"{e.__class__.__name__}: {e}\n")
            continue
        try:
            exc_after = p.get_exc_count()
        except Exception as e:
            buf.write(f"    ERROR: post-PMGR exc_count failed "
                      f"({e.__class__.__name__}: {e})\n")
            return
        buf.write(f"    exc_count during PMGR: {exc_before} -> {exc_after} "
                  f"(delta={exc_after - exc_before})\n")

        # Re-verify shared apcie gates AFTER the per-port PMGR call, in
        # case the port bring-up quietly touched the parent's gates.
        # PMGR reads only -- no apcie MMIO -- so this cannot wedge.
        if dev_by_idx is not None:
            buf.write("    shared gate re-verify:\n")
            still_all_on = True
            for gate in apcie.power_gates:
                st = _read_pmgr_gate_state(dev_by_idx, gate, buf,
                                           indent="      ")
                if st is None:
                    still_all_on = False
                elif not st.get("virtual") and not st["on"]:
                    still_all_on = False
            if not still_all_on:
                buf.write(f"    !!! shared apcie gate dropped after "
                          f"port{port.index} PMGR; skipping port_base "
                          f"reads on this port.\n")
                continue

        pb = port.port_base
        port_probes = [
            (pb + 0x800, f"port{port.index} APPCLK      (+0x800)"),
            (pb + 0x804, f"port{port.index} STATUS      (+0x804)"),
            (pb + 0x208, f"port{port.index} LINKSTS     (+0x208)"),
            (pb + 0x82c, f"port{port.index} T602X_RESET (+0x82c)"),
            (pb + 0x814, f"port{port.index} PORT_RESET  (+0x814)"),
            (pb + 0x104, f"port{port.index} +0x104"),
            (pb + 0x808, f"port{port.index} +0x808"),
        ]
        try:
            exc_running = p.get_exc_count()
        except Exception as e:
            buf.write(f"    ERROR: pre-read exc_count failed "
                      f"({e.__class__.__name__}: {e})\n")
            return
        for addr, label in port_probes:
            with guarded(buf, f"phaseC.0x{addr:x}", short_timeout=timeout):
                val = _safe_read32(addr)
            alive, delta = check_alive_fast(exc_running, timeout=timeout)
            buf.write(f"    read32(0x{addr:09x}) [{label}] = {val} "
                      f"(delta={delta}, alive={alive})\n")
            if not alive:
                buf.write(f"    !!! m1n1 unresponsive on port{port.index}; "
                          f"bailing Phase C\n")
                return
            if delta != 0:
                buf.write(f"    !!! SYNC on port{port.index} read; "
                          f"bailing this port\n")
                break
            exc_running += delta
    buf.write("\n")


# ID of the APCIE_PHY_SW gate in the pmgr device table. Only real (non-virtual)
# apcie power gate; all others in apcie.power_gates are `-V` aggregators.
_GATE_APCIE_PHY_SW = 151


def _poll_ps_actual(ps_addr, want, timeout_ms=100):
    """Poll a PMGR PS register until PS_ACTUAL == want or timeout.

    Returns (converged, final_val, elapsed_ms). Reads only, no writes.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    started = time.monotonic()
    final = 0
    while True:
        final = p.read32(ps_addr)
        actual = (final & PMGR_PS_ACTUAL_MASK) >> 4
        if actual == want:
            return True, final, (time.monotonic() - started) * 1000.0
        if time.monotonic() >= deadline:
            return False, final, (time.monotonic() - started) * 1000.0


def _pmgr_direct_poke_gate(dev_by_idx, gate_idx, buf, indent="  ",
                            timeout_ms=100):
    """Poke ONE PMGR gate's PS register to ACTIVE. Idempotent.

    Mirrors m1n1's `pmgr_set_mode()` in src/pmgr.c:84-96: clear
    AUTO_ENABLE + sticky bits + PS_TARGET, set PS_TARGET=0xf, poll
    PS_ACTUAL. If that doesn't lift ACTUAL, escalate by raising the
    PS_AUTO floor to 0xf.

    Fast-return (True, 0) on virtual gates (nothing to poke) and gates
    that are already ACTIVE (actual==0xf). Returns (converged, exc_delta)
    on real gates we actually wrote to; exc_delta is None if the
    exc_count read failed.
    """
    dev = dev_by_idx.get(int(gate_idx))
    if dev is None:
        buf.write(f"{indent}gate {gate_idx}: NOT FOUND in pmgr.devices\n")
        return False, None
    name = _decode_pmgr_name(dev)

    if dev.flags.no_ps:
        buf.write(f"{indent}gate {gate_idx} ({name!r}): VIRTUAL, no PS "
                  f"reg to poke; skipping\n")
        return True, 0

    try:
        ps_addr = u.adt.pmgr_dev_get_addr(dev)
    except Exception as e:
        buf.write(f"{indent}gate {gate_idx} ({name!r}): pmgr_dev_get_addr "
                  f"failed: {e.__class__.__name__}: {e}\n")
        return False, None

    try:
        raw = p.read32(ps_addr)
    except Exception as e:
        buf.write(f"{indent}gate {gate_idx} ({name!r}) ps@0x{ps_addr:x}: "
                  f"initial read failed: {e.__class__.__name__}: {e}\n")
        return False, None
    actual = (raw & PMGR_PS_ACTUAL_MASK) >> 4
    if actual == PMGR_PS_ACTIVE:
        buf.write(f"{indent}gate {gate_idx} ({name!r}) ps@0x{ps_addr:x}: "
                  f"already ACTIVE (raw=0x{raw:08x})\n")
        return True, 0

    buf.write(f"{indent}gate {gate_idx} ({name!r}) ps@0x{ps_addr:x}: "
              f"initial raw=0x{raw:08x} ({_decode_ps_state(raw)})\n")

    try:
        exc_before = p.get_exc_count()
    except Exception:
        exc_before = None

    # Write #1: mirror m1n1/src/pmgr.c pmgr_set_mode() exactly.
    clear1 = PMGR_AUTO_ENABLE | PMGR_WAS_CLKGATED | PMGR_WAS_PWRGATED | \
             PMGR_PS_TARGET_MASK
    set1 = PMGR_PS_ACTIVE
    buf.write(f"{indent}  write #1 (pmgr_set_mode): "
              f"mask32(0x{ps_addr:x}, clear=0x{clear1:08x}, "
              f"set=0x{set1:08x})\n")
    try:
        p.mask32(ps_addr, clear1, set1)
    except Exception as e:
        buf.write(f"{indent}  ERROR: mask32 #1 failed: "
                  f"{e.__class__.__name__}: {e}\n")
        return False, None

    converged, final, elapsed_ms = _poll_ps_actual(
        ps_addr, PMGR_PS_ACTIVE, timeout_ms=timeout_ms)
    buf.write(f"{indent}  after #1: raw=0x{final:08x} "
              f"({_decode_ps_state(final)}) "
              f"[{elapsed_ms:.1f} ms, "
              f"{'converged' if converged else 'TIMEOUT'}]\n")

    if not converged:
        # Write #2: raise PS_AUTO floor to ACTIVE. Some blocks refuse to
        # lift ACTUAL above PS_AUTO regardless of TARGET.
        clear2 = PMGR_PS_AUTO_MASK
        set2 = (PMGR_PS_ACTIVE << 24)
        buf.write(f"{indent}  write #2 (raise PS_AUTO floor): "
                  f"mask32(0x{ps_addr:x}, clear=0x{clear2:08x}, "
                  f"set=0x{set2:08x})\n")
        try:
            p.mask32(ps_addr, clear2, set2)
        except Exception as e:
            buf.write(f"{indent}  ERROR: mask32 #2 failed: "
                      f"{e.__class__.__name__}: {e}\n")
            return False, None
        converged, final, elapsed_ms = _poll_ps_actual(
            ps_addr, PMGR_PS_ACTIVE, timeout_ms=timeout_ms)
        buf.write(f"{indent}  after #2: raw=0x{final:08x} "
                  f"({_decode_ps_state(final)}) "
                  f"[{elapsed_ms:.1f} ms, "
                  f"{'converged' if converged else 'TIMEOUT'}]\n")

    delta = None
    if exc_before is not None:
        try:
            delta = p.get_exc_count() - exc_before
        except Exception:
            pass
    return converged, delta


def _pmgr_direct_poke_chain(dev_by_idx, gate_idx, buf, indent="  ",
                             timeout_ms=100, _visited=None, _depth=0):
    """Parents-first PMGR poke: bring every ancestor of `gate_idx` (and
    `gate_idx` itself) to PS_ACTUAL=0xf.

    Recurses parents-first so a real gate is only poked after every
    ancestor is confirmed ACTIVE. Cycle-guarded via _visited; depth
    bounded to 10. Uses the same fast-path as m1n1's pmgr helpers: if a
    real gate already reads actual=0xf, assume its parents are up too
    and skip the parent walk.

    Returns (all_active, failing_gate_or_None). On the first ancestor
    that refuses to converge, returns immediately -- the child is never
    poked (its parent is off, poking the child is unsafe or pointless).
    """
    if _visited is None:
        _visited = set()
    if _depth > 10:
        buf.write(f"{indent}(depth limit reached at gate {gate_idx})\n")
        return False, int(gate_idx)
    if int(gate_idx) in _visited:
        return True, None
    _visited.add(int(gate_idx))

    dev = dev_by_idx.get(int(gate_idx))
    if dev is None:
        buf.write(f"{indent}gate {gate_idx}: NOT FOUND in pmgr.devices\n")
        return False, int(gate_idx)

    # Fast path: real, non-virtual, already ACTIVE -> parents are up too.
    if not dev.flags.no_ps:
        try:
            ps_addr = u.adt.pmgr_dev_get_addr(dev)
            raw = p.read32(ps_addr)
            if ((raw & PMGR_PS_ACTUAL_MASK) >> 4) == PMGR_PS_ACTIVE:
                return True, None
        except Exception:
            # Fall through and try the poke path anyway; the write will
            # fail loudly if the PS reg genuinely can't be reached.
            pass

    # Walk parents first.
    try:
        parents = u.adt.pmgr_dev_get_parents(dev)
    except Exception as e:
        buf.write(f"{indent}gate {gate_idx}: parents accessor failed: "
                  f"{e.__class__.__name__}: {e}\n")
        return False, int(gate_idx)
    for pid in parents:
        pid = int(pid)
        if pid == 0 or pid == int(gate_idx):
            continue
        ok, failing = _pmgr_direct_poke_chain(
            dev_by_idx, pid, buf, indent + "  ", timeout_ms,
            _visited, _depth + 1)
        if not ok:
            return False, failing

    # Now poke this gate; its parents are all confirmed ACTIVE above.
    converged, _delta = _pmgr_direct_poke_gate(
        dev_by_idx, gate_idx, buf, indent, timeout_ms)
    if not converged:
        return False, int(gate_idx)
    return True, None


def probe_phaseD_gate151_poke(apcie, buf, timeout_ms=100):
    """Phase D -- parents-first PS-register poke ending at APCIE_PHY_SW
    (gate 151).

    Previous versions only poked gate 151 itself. On the Mac16,10 J773g
    cold-boot handoff we observed, gate 151 came up fine (actual 0x4 ->
    0xf in 0.4 ms) but the very first phy_ip MMIO read in Phase E
    SYNC-aborted and wedged m1n1 with no recovery. Phase 0.5's parent
    chain readout showed the root cause: gate 151's parent
    APCIE_SYS_ST (150) was stuck OFF with target=0xf, actual=0x0,
    auto_enable=1, ps_auto=0x0. The shared apcie phy_ip window lives
    behind the ST-side power domain that gate 150 gates, so any read
    into it AXI-stalled until m1n1 gave up.

    Fix: walk gate 151's parent chain via u.adt.pmgr_dev_get_parents(),
    parents-first, and apply the same two-write escalation
    (pmgr_set_mode + raise PS_AUTO floor) to any ancestor whose
    PS_ACTUAL != 0xf. gate 151 itself is only touched after every
    ancestor is confirmed ACTIVE.

    apcie.phaseD_gate151_active is only set True if the ENTIRE chain
    converged. apcie.phaseD_failing_gate holds the id of the first
    refusing gate (None on success) so Phase E can log why it bailed.
    """
    buf.write("=== Phase D: parents-first PS poke ending at gate 151 "
              "(APCIE_PHY_SW) ===\n")
    apcie.phaseD_gate151_active = False
    apcie.phaseD_failing_gate = None

    dev_by_idx = getattr(apcie, "phase0_dev_by_idx", None)
    if dev_by_idx is None:
        _pmgr, dev_by_idx = _load_pmgr_devices(buf)
        if dev_by_idx is None:
            buf.write("  ERROR: cannot load pmgr devices; "
                      "bailing Phase D\n\n")
            return

    try:
        exc_before = p.get_exc_count()
    except Exception as e:
        buf.write(f"  ERROR: pre-poke exc_count failed: "
                  f"{e.__class__.__name__}: {e}\n\n")
        return

    ok, failing = _pmgr_direct_poke_chain(
        dev_by_idx, _GATE_APCIE_PHY_SW, buf, indent="  ",
        timeout_ms=timeout_ms)

    try:
        exc_after = p.get_exc_count()
        buf.write(f"  exc_count during chain poke: {exc_before} -> "
                  f"{exc_after} (delta={exc_after - exc_before})\n")
    except Exception as e:
        buf.write(f"  WARN: post-poke exc_count failed: "
                  f"{e.__class__.__name__}: {e}\n")

    if ok:
        buf.write("  entire gate 151 parent chain is ACTIVE; "
                  "Phase E may run.\n\n")
        apcie.phaseD_gate151_active = True
    else:
        apcie.phaseD_failing_gate = failing
        buf.write(f"  gate {failing} refuses to reach ACTUAL=0xf.\n"
                  f"  This is a genuine firmware handshake issue -- an\n"
                  f"  SMC key or phy_common MMIO write we haven't\n"
                  f"  identified is required to release gate {failing}.\n"
                  f"  Phase E will be SKIPPED to avoid wedging m1n1 on\n"
                  f"  the shared phy_ip fabric that lives behind it.\n\n")


def probe_phaseE_rc_axi_sanity(apcie, buf, timeout=0.3):
    """Phase E -- post-Phase-D fabric sanity probe (rc_base + axi_base).

    HISTORY: earlier versions of Phase E probed phy_ip_base right after
    Phase D confirmed gates 150+151 ACTIVE. That approach WEDGED m1n1
    on every j773g cold-boot run (reproduced 2026-07-10 + 2026-07-11):
    the very first read32(phy_ip_base+0) took a SYNC exception whose
    recovery path never returned, m1n1 died silently.

    Root cause (learned from that dead-end): phy_ip_base is downstream
    of the phy_base CLK0/CLK1 handshake performed by m1n1's own pcie.c
    at pcie.c:468-480 (see APCIE_PHY_CTRL_CLK0REQ/ACK, CLK1REQ/ACK).
    PMGR gates 148/149/150/151 being ACTIVE is NECESSARY but NOT
    SUFFICIENT to reach phy_ip: the fabric between the AXI backbone
    and the phy_ip window is held un-clocked until the shared PHY
    control block acks the clock request. Without that handshake, any
    load into phy_ip AXI-stalls forever (no exception, no recovery,
    GUARD.SKIP does not help -- see the doc on guarded() line 100).

    Phase E is therefore now a NON-DANGEROUS sanity probe of the two
    shared blocks whose reachability we already proved via Phase 0/D:
      - rc_base   -> lives behind gate 148 (ANS) + 149 (APCIE_ST),
                     both ACTIVE at boot per Phase 0.
      - axi_base  -> lives behind gates 135/136 (APCIE_GP/SYS_GP),
                     both ACTIVE at boot per Phase 0.
    Neither read touches phy_ip. If either SYNCs we know something
    fundamental broke between Phase D and here.

    The actual phy_ip access moved to Phase F (T8140 pcie.c replay),
    which does the CLK handshake FIRST. See probe_phaseF_t8140_replay.
    """
    buf.write("=== Phase E: post-Phase-D rc/axi sanity probe ===\n")
    if not getattr(apcie, "phaseD_gate151_active", False):
        failing = getattr(apcie, "phaseD_failing_gate", None)
        if failing is not None:
            buf.write(f"  SKIPPED: Phase D reported gate {failing} refused "
                      f"to reach ACTIVE. Reading phy_ip while an\n"
                      f"  ancestor of gate 151 is off would AXI-stall\n"
                      f"  the fabric and wedge m1n1 with no recovery.\n\n")
        else:
            buf.write("  SKIPPED: Phase D did not confirm gate 151 ACTIVE.\n\n")
        return

    # Final safeguard: re-verify every real apcie power gate right
    # before we touch phy_ip MMIO. Between Phase D and here we did
    # PMGR-only reads which shouldn't disturb PMGR state, but a
    # stale phaseD_gate151_active flag from an earlier attempt would
    # be fatal (AXI stall, no exception recovery). Re-reading the PS
    # regs takes microseconds and is guaranteed safe.
    dev_by_idx = getattr(apcie, "phase0_dev_by_idx", None)
    if dev_by_idx is not None:
        # Include gate 151's parent gates so we notice if 150 drifted.
        gates_to_check = set(int(g) for g in apcie.power_gates)
        gates_to_check.add(_GATE_APCIE_PHY_SW)
        try:
            phy_sw = dev_by_idx.get(_GATE_APCIE_PHY_SW)
            if phy_sw is not None:
                for pid in u.adt.pmgr_dev_get_parents(phy_sw):
                    pid = int(pid)
                    if pid != 0:
                        gates_to_check.add(pid)
        except Exception:
            pass
        buf.write("  re-verifying real apcie PMGR gates before phy_ip MMIO:\n")
        off_gates = []
        for gate_idx in sorted(gates_to_check):
            dev = dev_by_idx.get(int(gate_idx))
            if dev is None or dev.flags.no_ps:
                continue
            try:
                ps_addr = u.adt.pmgr_dev_get_addr(dev)
                raw = p.read32(ps_addr)
            except Exception as e:
                buf.write(f"    gate {gate_idx}: PS read failed "
                          f"({e.__class__.__name__}: {e})\n")
                off_gates.append(gate_idx)
                continue
            actual = (raw & PMGR_PS_ACTUAL_MASK) >> 4
            name = _decode_pmgr_name(dev)
            if actual != PMGR_PS_ACTIVE:
                buf.write(f"    gate {gate_idx} ({name!r}) "
                          f"raw=0x{raw:08x} actual=0x{actual:x} -- NOT ACTIVE\n")
                off_gates.append(gate_idx)
            else:
                buf.write(f"    gate {gate_idx} ({name!r}) ACTIVE\n")
        if off_gates:
            buf.write(f"  ABORT Phase E: gates {off_gates} not ACTIVE; "
                      f"phy_ip read would wedge m1n1.\n\n")
            return

    # rc_base and axi_base are under PMGR gates that Phase 0 already
    # showed ACTIVE at boot (ANS/APCIE_ST/APCIE_GP/APCIE_SYS_GP). No
    # phy_ip reads here -- those need Phase F's CLK handshake first.
    probes = [
        (apcie.rc_base + 0x00,  "rc_base  +0x000"),
        (apcie.rc_base + 0x3c,  "rc_base  +0x03c"),
        (apcie.rc_base + 0x50,  "rc_base  +0x050"),
        (apcie.rc_base + 0x54,  "rc_base  +0x054"),
        (apcie.rc_base + 0x58,  "rc_base  +0x058"),
        (apcie.axi_base + 0x00, "axi_base +0x000"),
        (apcie.axi_base + 0x600,"axi_base +0x600"),
    ]

    try:
        exc_running = p.get_exc_count()
    except Exception as e:
        buf.write(f"  ERROR: pre-read exc_count failed "
                  f"({e.__class__.__name__}: {e})\n\n")
        return

    apcie.phaseE_rc_axi_ok = True
    for addr, label in probes:
        with guarded(buf, f"phaseE.0x{addr:x}", short_timeout=timeout):
            val = _safe_read32(addr)
        alive, delta = check_alive_fast(exc_running, timeout=timeout)
        buf.write(f"  read32(0x{addr:09x}) [{label}] = {val} "
                  f"(delta={delta}, alive={alive})\n")
        if not alive:
            buf.write("  !!! m1n1 unresponsive on a supposedly-ACTIVE\n"
                      "  block; this should NOT happen after Phase D.\n"
                      "  Bailing Phase E.\n\n")
            apcie.phaseE_rc_axi_ok = False
            return
        if delta != 0:
            buf.write(f"  !!! SYNC/SError delta={delta} on a\n"
                      f"  supposedly-ACTIVE block. Something regressed\n"
                      f"  between Phase 0 and Phase E; Phase F should\n"
                      f"  NOT run until this is understood.\n")
            apcie.phaseE_rc_axi_ok = False
        exc_running += delta

    if apcie.phaseE_rc_axi_ok:
        buf.write("  rc/axi sanity OK; Phase F may run.\n\n")
    else:
        buf.write("\n")


# ------------------------------------------------ diagnostic probes

def probe_phy_ip_reachability(apcie, buf, label, timeout=0.3):
    """One guarded read32(phy_ip_base + 0) + alive check. Records
    whether phy_ip is reachable right now. Zero write risk. If phy_ip
    is unclocked the read AXI-stalls, guarded() converts it to a
    sentinel, alive check bails cleanly. Result:
        REACHABLE   -- read returned a nonzero non-sentinel value
        SENTINEL    -- guard caught SLVERR, phy_ip returned sentinel
        AXI_STALL   -- alive check failed, m1n1 dead (log + bail)
    """
    addr = apcie.phy_ip_base + 0
    buf.write(f"  [phy-ip-diag @ {label}] read32(0x{addr:x}):\n")
    try:
        exc_before = p.get_exc_count()
    except Exception as e:
        buf.write(f"    ERROR: pre exc_count failed: "
                  f"{e.__class__.__name__}: {e}\n")
        return
    val = None
    raised = None
    with guarded(buf, f"phy-ip-diag.{label}", short_timeout=timeout):
        try:
            val = p.read32(addr)
        except Exception as e:
            raised = e
    if raised is not None:
        buf.write(f"    RAISED: {raised.__class__.__name__}: {raised}\n")
        return
    alive, delta = check_alive_fast(exc_before, timeout=timeout)
    if not alive:
        buf.write(f"    AXI_STALL: m1n1 UNRESPONSIVE after read\n")
        return
    if delta:
        buf.write(f"    SENTINEL (SLVERR caught, delta={delta}): "
                  f"val=0x{val:x}\n")
    else:
        buf.write(f"    REACHABLE: val=0x{val:x} (delta=0)\n")


def probe_pmgr_explore(u, buf, name_patterns=("PCIE", "PHY", "APCIE",
                                              "ANS", "DART_APCIE")):
    """Enumerate every PMGR gate in the SoC, filter by name (case-
    insensitive substring match), dump PS state. Pure PMGR reads, no
    apcie MMIO, wedge-immune.

    Purpose: find any gate we haven't identified whose name hints at
    PHY/PCIE bring-up. On t8132 there may be an APCIE_PHY_IP or
    APCIE_CIO or similar that gates phy_ip specifically.
    """
    buf.write("=== PMGR explore (all gates matching {}) ===\n"
              .format(sorted(name_patterns)))
    _pmgr, dev_by_idx = _load_pmgr_devices(buf)
    if dev_by_idx is None:
        return
    patterns_upper = tuple(p.upper() for p in name_patterns)
    matches = []
    for gate_idx, dev in dev_by_idx.items():
        name = _decode_pmgr_name(dev).upper()
        if any(pat in name for pat in patterns_upper):
            matches.append(gate_idx)
    matches.sort()
    buf.write(f"  {len(matches)} matching gates:\n")
    for gate in matches:
        _read_pmgr_gate_state(dev_by_idx, gate, buf, indent="    ")
    buf.write("\n")


def probe_dart_power(buf, dart_paths=("/arm-io/dart-apcie0",
                                       "/arm-io/dart-apcie2")):
    """Enable DART power via p.pmgr_adt_power_enable for each active
    apcie DART. On t8132 the port_base ctrl_lo range overlaps DART
    MMIO -- powering the DART may be a prerequisite for port_base
    accesses. Whether it also affects phy_ip access is what we're
    testing.

    Skips /arm-io/dart-apcie1 because port 1 is inactive per ADT
    (would AXI-stall on any downstream access).
    """
    buf.write("=== DART power enable ===\n")
    try:
        exc_before = p.get_exc_count()
    except Exception as e:
        buf.write(f"  ERROR: pre exc_count failed: "
                  f"{e.__class__.__name__}: {e}\n\n")
        return
    for path in dart_paths:
        buf.write(f"  p.pmgr_adt_power_enable({path!r}):\n")
        try:
            r = p.pmgr_adt_power_enable(path)
            buf.write(f"    -> {r!r}\n")
        except Exception as e:
            buf.write(f"    RAISED: {e.__class__.__name__}: {e}\n")
    try:
        exc_after = p.get_exc_count()
        buf.write(f"  exc_count delta = {exc_after - exc_before}\n\n")
    except Exception as e:
        buf.write(f"  WARN: post exc_count failed: "
                  f"{e.__class__.__name__}: {e}\n\n")


# ------------------------------------------------ Phase F: T8140 replay

# T8140 reg indices, from m1n1/src/pcie.c regs_t8140 (lines 211-221).
_T8140_CONFIG_IDX  = 0
_T8140_RC_IDX      = 1
_T8140_PHY_IDX     = 2   # phy_common_idx and phy_idx both = 2 on T8140
_T8140_PHY_IP_IDX  = 3
_T8140_AXI_IDX     = 4

# APCIE_PHY_CTRL bit layout -- m1n1/src/pcie.c:38-43.
_APCIE_PHY_CTRL     = 0x000
_PHY_CTRL_CLK0REQ   = 1 << 0
_PHY_CTRL_CLK1REQ   = 1 << 1
_PHY_CTRL_CLK0ACK   = 1 << 2
_PHY_CTRL_CLK1ACK   = 1 << 3
_PHY_CTRL_RESET     = 1 << 7

# APCIE_PHYCMN_CLK mode -- m1n1/src/pcie.c:49-52.
_APCIE_PHYCMN_CLK_MODE_MASK = 0x3   # GENMASK(1, 0)
_APCIE_PHYCMN_CLK_MODE_ON   = 0x1


def _poll32_bit(addr, mask, want, timeout_ms):
    """Poll (read32(addr) & mask == want). Returns (converged, last_val,
    elapsed_ms). Reads only; raises on read failure so callers can bail.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    started = time.monotonic()
    val = 0
    while True:
        val = p.read32(addr)
        if (val & mask) == want:
            return True, val, (time.monotonic() - started) * 1000.0
        if time.monotonic() >= deadline:
            return False, val, (time.monotonic() - started) * 1000.0


def probe_phaseF_t8140_replay(apcie, buf, timeout=0.3, flush_fn=None,
                              extra_tunables=False, phycmn_first=False,
                              phy_ip_diag=False):
    """Phase F -- replay m1n1 pcie.c T8140 shared-init step by step.

    m1n1 6b277bc treats t8132 as APCIE_T8140 (pcie.c:303-315). The
    shared-init sequence in pcie_init_controller() for the T8140
    codepath is what this phase replays, one MMIO/proxy call at a
    time, with guarded() around each step so a SYNC gets converted
    to sentinel and we can bail with a full log rather than a wedge.

    Mapping to pcie.c line numbers (all in the T8140/T8122 branches):
      1. pmgr_adt_power_enable('/arm-io/apcie')       [pcie.c:425]
      2. tunables_apply_local apcie-axi2af-tunables   [pcie.c:432]
      3. controller==APCIE: write32(rc_base+0x4, 0)   [pcie.c:438-439]
      4. tunables_apply_local apcie-common-tunables   [pcie.c:443]
      5. tunables_apply_local apcie-phy-tunables      [pcie.c:454]
      6.a set32(phy_shared+0, CLK0REQ)                [pcie.c:468]
      6.b poll32 CLK0ACK, 50 ms                       [pcie.c:469-473]
      6.c set32(phy_shared+0, CLK1REQ)                [pcie.c:475]
      6.d poll32 CLK1ACK, 50 ms                       [pcie.c:476-480]
      6.e clear32(phy_shared+0, RESET); udelay(1)     [pcie.c:482-483]
      6.f T8140: set32(phy_shared+4, 0x01)            [pcie.c:492]
      6.g tunables apcie-phy-ip-pll-tunables          [pcie.c:518]
      6.h tunables apcie-phy-ip-auspma-tunables       [pcie.c:522]
      7.  mask32(phy_common+0, MODE_MASK, MODE_ON=1)  [pcie.c:535]
      8.  write32(rc_base+0x54, 0x140)                [pcie.c:558]
      9.  write32(rc_base+0x50, 0x1)                  [pcie.c:559]
     10.  poll32(rc_base+0x58, 1, 1, 250 ms)          [pcie.c:560]

    Not covered here (per-port init, phase G / future work):
      pcie.c:571-852 -- per-active-port bring-up (port_base pokes,
      port_phy_base CLK handshakes, port RESET release, RC config
      space DBI writes, LTSSM training). Do NOT run those until
      Phase F reports shared_up = True.

    CRITICAL invariant (learned the hard way -- see Phase E docstring):
      phy_ip_base is NOT reachable until step 6.a-c complete. Steps
      6.g/6.h are the first phy_ip writes this script ever does. If
      m1n1 wedges there, the CLK handshake succeeded but phy_ip is
      still gated by something else (SMC key, phy_common poke, per
      port PHY dependency). The log tells us EXACTLY which step
      broke; iterate from there.

    Preconditions:
      * apcie.phaseD_gate151_active is True (parents-first poke ran
        and every ancestor of gate 151 reached ACTUAL=0xf)
      * apcie.phaseE_rc_axi_ok is True (Phase E confirmed rc/axi
        reads work). Missing attr defaults to True (Phase E skipped).

    Postconditions:
      * apcie.phaseF_shared_up = True on full success
      * apcie.phaseF_last_step = one of:
          "not started" | "N.<name>" | "shared init complete"
        so the log always tells us EXACTLY where the replay stopped.
    """
    buf.write("=== Phase F: T8140 controller-init replay (pcie.c) ===\n")
    apcie.phaseF_shared_up = False
    apcie.phaseF_last_step = "not started"

    if not getattr(apcie, "phaseD_gate151_active", False):
        buf.write("  SKIPPED: Phase D did not confirm gate 151 ACTIVE.\n\n")
        return
    if not getattr(apcie, "phaseE_rc_axi_ok", True):
        buf.write("  SKIPPED: Phase E flagged rc/axi as unsafe.\n\n")
        return

    path = "/arm-io/apcie"
    rc_base = apcie.rc_base
    axi_base = apcie.axi_base
    # T8140 does phy_base = phy_packed + 0x8000 (pcie.c:394) and
    # phy_common_base += 0x4000 (pcie.c:395). ApcieMap has phy_common
    # pre-shifted; phy_shared_base we compute here.
    phy_shared_base = apcie.phy_packed_base + 0x8000
    phy_common_base = apcie.phy_common_base
    phy_ip_base     = apcie.phy_ip_base

    buf.write("  layout for replay (T8140 codepath):\n")
    buf.write(f"    path            = {path}\n")
    buf.write(f"    rc_base         = 0x{rc_base:x}\n")
    buf.write(f"    axi_base        = 0x{axi_base:x}\n")
    buf.write(f"    phy_shared_base = 0x{phy_shared_base:x} "
              f"(= phy_packed + 0x8000)\n")
    buf.write(f"    phy_common_base = 0x{phy_common_base:x}\n")
    buf.write(f"    phy_ip_base     = 0x{phy_ip_base:x}\n")

    # Dump the actual property names on /arm-io/apcie so we can see
    # WHICH tunable properties the ADT has (and skip the ones it
    # doesn't). pcie.c:441-457 guards its tunables_apply calls with
    # adt_getprop existence checks; missing props are just skipped.
    # We do the same on the Python side below.
    apcie_props = ()
    try:
        node = u.adt["/arm-io/apcie"]
        apcie_props = tuple(sorted(node._properties.keys()))
    except Exception as e:
        buf.write(f"    WARN: could not enumerate /arm-io/apcie "
                  f"properties: {e.__class__.__name__}: {e}\n")
    tunable_props = [k for k in apcie_props if "tunables" in k]
    buf.write(f"    tunable properties on /arm-io/apcie ({len(tunable_props)}):\n")
    for k in tunable_props:
        buf.write(f"      * {k}\n")
    buf.write("\n")

    def _prop_exists(prop):
        if not apcie_props:
            return False
        return prop in apcie_props

    try:
        exc_running = p.get_exc_count()
    except Exception as e:
        buf.write(f"  ERROR: initial get_exc_count failed: "
                  f"{e.__class__.__name__}: {e}\n\n")
        if flush_fn is not None:
            flush_fn("phaseF-init-fail")
        return

    def _flush(tag):
        if flush_fn is not None:
            flush_fn(tag)

    def diag(label):
        """No-op unless --phy-ip-diag. Guarded read32 of phy_ip_base+0
        + alive check. Answers 'is phy_ip reachable RIGHT NOW?' at this
        point in Phase F. Safe: guarded, alive-bailed."""
        if not phy_ip_diag:
            return
        probe_phy_ip_reachability(apcie, buf, label, timeout=timeout)
        _flush(f"phaseF.diag.{label}")

    def step(label, fn):
        nonlocal exc_running
        apcie.phaseF_last_step = label
        buf.write(f"  --- {label} ---\n")
        # Flush BEFORE the risky call so if it wedges we still see
        # which step we were on. Overhead is tiny (a file write).
        _flush(f"phaseF.pre.{label[:32]}")
        with guarded(buf, label, short_timeout=timeout):
            try:
                r = fn()
                if r is not None:
                    buf.write(f"    -> {r!r}\n")
            except Exception as e:
                buf.write(f"    RAISED: {e.__class__.__name__}: {e}\n")
                _flush(f"phaseF.post.{label[:32]}")
                return False
        alive, delta = check_alive_fast(exc_running, timeout=timeout)
        if not alive:
            buf.write("    m1n1 UNRESPONSIVE after step; aborting Phase F\n")
            _flush(f"phaseF.post.{label[:32]}")
            return False
        if delta:
            buf.write(f"    !!! exc delta = {delta} on this step\n")
            exc_running += delta
            _flush(f"phaseF.post.{label[:32]}")
            return False
        exc_running += delta
        _flush(f"phaseF.post.{label[:32]}")
        return True

    def tunables_step(step_id, prop, reg_idx):
        """Guarded tunables application. Mirrors pcie.c pattern:
        check ADT prop existence first, skip if missing (log the skip
        so future iterations see which props are actually present),
        else apply. This makes Phase F robust against the j773g ADT
        missing several of the tunable props m1n1 pcie.c ALSO checks.
        """
        label = f"{step_id}.tunables {prop} reg_idx={reg_idx}"
        if not _prop_exists(prop):
            apcie.phaseF_last_step = label + " (SKIPPED: prop absent)"
            buf.write(f"  --- {label} ---\n")
            buf.write(f"    SKIPPED: /arm-io/apcie has no property {prop!r}.\n"
                      f"    (matches pcie.c pattern: adt_getprop check "
                      f"before tunables_apply.)\n")
            _flush(f"phaseF.skip.{step_id}")
            return True
        return step(label,
                    lambda: p.tunables_apply_local(path, prop, reg_idx))

    def poll_step(label, addr, mask, want, timeout_ms):
        nonlocal exc_running
        apcie.phaseF_last_step = label
        buf.write(f"  --- {label} ---\n")
        buf.write(f"    poll addr=0x{addr:x} mask=0x{mask:x} "
                  f"want=0x{want:x} timeout={timeout_ms}ms\n")
        _flush(f"phaseF.pre.{label[:32]}")
        converged = False
        val = 0
        elapsed_ms = 0.0
        raised = None
        with guarded(buf, label, short_timeout=timeout):
            try:
                converged, val, elapsed_ms = _poll32_bit(
                    addr, mask, want, timeout_ms=timeout_ms)
            except Exception as e:
                raised = e
        if raised is not None:
            buf.write(f"    RAISED: {raised.__class__.__name__}: {raised}\n")
            _flush(f"phaseF.post.{label[:32]}")
            return False
        buf.write(f"    conv={converged} val=0x{val:x} ({elapsed_ms:.1f} ms)\n")
        alive, delta = check_alive_fast(exc_running, timeout=timeout)
        if not alive:
            buf.write("    m1n1 UNRESPONSIVE after poll; aborting Phase F\n")
            _flush(f"phaseF.post.{label[:32]}")
            return False
        if delta:
            buf.write(f"    !!! exc delta = {delta} during poll\n")
            exc_running += delta
            _flush(f"phaseF.post.{label[:32]}")
            return False
        exc_running += delta
        if not converged:
            buf.write(f"    !!! poll did NOT converge; aborting Phase F\n")
            _flush(f"phaseF.post.{label[:32]}")
            return False
        _flush(f"phaseF.post.{label[:32]}")
        return True

    _mask_op_for_size = {1: p.mask8, 2: p.mask16, 4: p.mask32, 8: p.mask64}

    def phy_ip_tunables_filtered(step_id, prop):
        """Apply an apcie-phy-ip-* tunables prop with slice-filtering.

        Parses the tunables entries via apcie.apcie_tunables (ADT-only,
        wedge-immune) and classifies each entry via
        apcie.classify_phy_ip_offset. Entries whose port slice is
        INACTIVE per ADT (no pci-bridge{N}) are SKIPPED -- writing to
        them would AXI-stall the fabric on j773g. Entries in the shared
        slice or in an ACTIVE port slice are applied one-at-a-time as
        p.mask{8,16,32,64}() calls under guarded() with per-entry
        alive check.

        Returns True on full success or on absent prop (mirrors pcie.c's
        adt_getprop skip pattern). Returns False on wedge with
        apcie.phaseF_last_step naming the exact failing entry.
        """
        nonlocal exc_running
        label = f"{step_id}.tunables {prop} (slice-filtered)"
        apcie.phaseF_last_step = label
        buf.write(f"  --- {label} ---\n")
        entries = apcie.apcie_tunables(u, prop)
        if not entries:
            buf.write(f"    SKIPPED: /arm-io/apcie has no property "
                      f"{prop!r} (matches pcie.c adt_getprop skip).\n")
            _flush(f"phaseF.skip.{step_id}")
            return True

        tally = {"shared": 0}
        for pi in range(len(apcie.ports)):
            tally[f"port{pi}_applied"] = 0
            tally[f"port{pi}_skipped"] = 0
        tally["out_of_window"] = 0

        plan = []
        for offset, size, mask, value in entries:
            info = apcie.classify_phy_ip_offset(offset)
            kind = info["kind"]
            if kind == "shared":
                tally["shared"] += 1
                plan.append((offset, size, mask, value, "shared", True))
            elif kind == "port_slice":
                pi = info["port_index"]
                if info["port_active"]:
                    tally[f"port{pi}_applied"] += 1
                    plan.append((offset, size, mask, value,
                                 f"port{pi}", True))
                else:
                    tally[f"port{pi}_skipped"] += 1
                    plan.append((offset, size, mask, value,
                                 f"port{pi}", False))
            else:
                tally["out_of_window"] += 1
                plan.append((offset, size, mask, value,
                             "out_of_window", False))

        buf.write(f"    plan ({len(entries)} entries):\n")
        for k, v in tally.items():
            if v > 0:
                buf.write(f"      {k:20s} = {v}\n")
        _flush(f"phaseF.plan.{step_id}")

        applied = 0
        skipped = 0
        for i, (offset, size, mask, value, tag, do_apply) in enumerate(plan):
            target = apcie.phy_ip_base + offset
            if not do_apply:
                skipped += 1
                continue
            op = _mask_op_for_size.get(size)
            if op is None:
                buf.write(f"    ERROR: entry #{i} unknown size {size}; "
                          f"aborting {step_id}\n")
                _flush(f"phaseF.err.{step_id}.{i}")
                return False
            entry_label = (f"{step_id}.#{i:03d}.{tag}."
                           f"mask{size * 8}@0x{target:x}")
            apcie.phaseF_last_step = entry_label
            with guarded(buf, entry_label, short_timeout=timeout):
                try:
                    op(target, mask, value)
                except Exception as e:
                    buf.write(f"    RAISED at #{i} ({tag}) 0x{target:x}: "
                              f"{e.__class__.__name__}: {e}\n")
                    _flush(f"phaseF.raise.{step_id}.{i}")
                    return False
            alive, delta = check_alive_fast(exc_running, timeout=timeout)
            if not alive:
                buf.write(f"    !!! m1n1 UNRESPONSIVE at #{i} ({tag}) "
                          f"0x{target:x}; aborting {step_id}\n")
                _flush(f"phaseF.dead.{step_id}.{i}")
                return False
            if delta:
                buf.write(f"    !!! SYNC delta={delta} at #{i} ({tag}) "
                          f"0x{target:x}; aborting {step_id}\n")
                exc_running += delta
                _flush(f"phaseF.sync.{step_id}.{i}")
                return False
            exc_running += delta
            applied += 1

        buf.write(f"    ok: applied {applied}, skipped {skipped} "
                  f"(port1 inactive per ADT)\n")
        # Quick verification read of each active slice's head to prove
        # phy_ip really is writable now. Guarded; one read per slice.
        buf.write(f"    verification reads:\n")
        for pi in range(len(apcie.ports)):
            if not apcie.ports[pi].exists:
                continue
            probe_off = 0x8000 + pi * 0x8000  # slice[pi] head
            addr = apcie.phy_ip_base + probe_off
            vr_label = f"{step_id}.verify.port{pi}@0x{addr:x}"
            with guarded(buf, vr_label, short_timeout=timeout):
                try:
                    v = p.read32(addr)
                    buf.write(f"      port{pi}: read32(0x{addr:x}) = 0x{v:x}\n")
                except Exception as e:
                    buf.write(f"      port{pi}: read32(0x{addr:x}) RAISED: "
                              f"{e.__class__.__name__}: {e}\n")
            alive, delta = check_alive_fast(exc_running, timeout=timeout)
            if not alive:
                buf.write(f"    !!! m1n1 unresponsive after verify port{pi}\n")
                _flush(f"phaseF.verify_dead.{step_id}.port{pi}")
                return False
            exc_running += delta
        _flush(f"phaseF.done.{step_id}")
        return True

    diag("F.entry")

    # ---- step 1: PMGR power enable
    # Gate 150 should already be ACTIVE from Phase D's parents-first
    # poke, so m1n1's internal pmgr_set_mode_recursive should complete
    # in microseconds (no polling loop hits).
    if not step("1.pmgr_adt_power_enable('/arm-io/apcie')",
                lambda: p.pmgr_adt_power_enable(path)):
        return
    diag("post-1.pmgr")

    # ---- step 2: axi2af tunables (guarded by prop existence)
    if not tunables_step("2", "apcie-axi2af-tunables", _T8140_AXI_IDX):
        return

    # ---- step 3: rc_base + 0x4 <- 0
    if not step("3.write32(rc_base+0x4, 0) [pcie.c:438-439]",
                lambda: p.write32(rc_base + 0x4, 0)):
        return

    # ---- step 4: common tunables (guarded)
    if not tunables_step("4", "apcie-common-tunables", _T8140_RC_IDX):
        return

    # ---- step 5: phy tunables (guarded)
    if not tunables_step("5", "apcie-phy-tunables", _T8140_PHY_IDX):
        return
    diag("post-5.phy-tunables")

    # ---- step 5.5: t8132-specific extra tunables (--extra-tunables).
    # These props are NOT applied by m1n1 pcie.c on any codepath.
    # Hypothesis: cio3pllcore + pcieclkgen provide the reference clock
    # phy_ip needs. Without them, step 6.g's mask32 at phy_ip_base+0x38
    # AXI-stalls (observed 2026-07-11).
    if extra_tunables:
        for step_id, prop in [
            ("5.5.a", "apcie-cio3pllcore-tunables"),
            ("5.5.b", "apcie-pcieclkgen-tunables"),
        ]:
            entries = apcie.apcie_tunables(u, prop)
            if not entries:
                apcie.phaseF_last_step = f"{step_id}.{prop}.absent"
                buf.write(f"  --- {step_id}.tunables {prop} ---\n")
                buf.write(f"    SKIPPED: /arm-io/apcie has no property "
                          f"{prop!r}.\n")
                _flush(f"phaseF.skip.{step_id}")
                continue
            idx, base, size_, reason = _infer_tunable_target_reg(
                apcie, entries)
            if idx is None:
                buf.write(f"  --- {step_id}.tunables {prop} ---\n")
                buf.write(f"    ABORT: cannot infer reg for {prop}: "
                          f"{reason}\n")
                _flush(f"phaseF.err.{step_id}")
                return
            label = f"{step_id}.tunables {prop} inferred reg_idx={idx}"
            buf.write(f"  --- {label} ---\n")
            buf.write(f"    inference: {reason}\n")
            buf.write(f"    -> base=0x{base:x} size=0x{size_:x} "
                      f"(reg_idx={idx})\n")
            if not step(label,
                        lambda pr=prop, i=idx:
                            p.tunables_apply_local(path, pr, i)):
                return
    else:
        buf.write("  --- 5.5.extra-tunables SKIPPED "
                  "(pass --extra-tunables to enable) ---\n")
        _flush("phaseF.skip.5.5")

    # ---- OPTIONAL: --phycmn-first moves step 7 (phy_common CLK MODE=ON)
    # to BEFORE step 6.a. Hypothesis: setting the phy_common CLK MODE
    # bit is what actually ungates phy_ip; the phy_shared CLK0/CLK1
    # handshake alone is not enough on t8132.
    def do_phycmn():
        return step("7.mask32(phy_common+0, MODE_MASK=0x3, MODE_ON=0x1)",
                    lambda: p.mask32(phy_common_base + 0,
                                     _APCIE_PHYCMN_CLK_MODE_MASK,
                                     _APCIE_PHYCMN_CLK_MODE_ON))
    phycmn_done = False
    if phycmn_first:
        buf.write("  --- (5.75) --phycmn-first: applying phy_common "
                  "CLK MODE=ON before step 6.a ---\n")
        _flush("phaseF.info.phycmn-first")
        if not do_phycmn():
            return
        phycmn_done = True

    # ---- step 6.a-b: CLK0 handshake
    if not step("6.a.set32(phy_shared+0, CLK0REQ=BIT(0))",
                lambda: p.set32(phy_shared_base + _APCIE_PHY_CTRL,
                                _PHY_CTRL_CLK0REQ)):
        return
    if not poll_step("6.b.poll_CLK0ACK",
                     phy_shared_base + _APCIE_PHY_CTRL,
                     _PHY_CTRL_CLK0ACK, _PHY_CTRL_CLK0ACK,
                     timeout_ms=50):
        return
    diag("post-6.b.CLK0ACK")

    # ---- step 6.c-d: CLK1 handshake
    if not step("6.c.set32(phy_shared+0, CLK1REQ=BIT(1))",
                lambda: p.set32(phy_shared_base + _APCIE_PHY_CTRL,
                                _PHY_CTRL_CLK1REQ)):
        return
    if not poll_step("6.d.poll_CLK1ACK",
                     phy_shared_base + _APCIE_PHY_CTRL,
                     _PHY_CTRL_CLK1ACK, _PHY_CTRL_CLK1ACK,
                     timeout_ms=50):
        return
    diag("post-6.d.CLK1ACK")

    # ---- step 6.e: release RESET
    if not step("6.e.clear32(phy_shared+0, RESET=BIT(7))",
                lambda: p.clear32(phy_shared_base + _APCIE_PHY_CTRL,
                                  _PHY_CTRL_RESET)):
        return
    # pcie.c does udelay(1) -- time.sleep floor on Linux is ~us but
    # gets rounded up; 1 ms is plenty of settle margin and doesn't
    # affect functional correctness.
    time.sleep(0.001)

    # ---- step 6.f: T8140 marker write (phy_shared + 4 <- 0x01)
    if not step("6.f.set32(phy_shared+4, 0x01) [T8140 marker, pcie.c:492]",
                lambda: p.set32(phy_shared_base + 4, 0x01)):
        return
    diag("post-6.f.T8140-marker")

    # ---- step 6.g-h: FIRST phy_ip access ever from this script.
    # Uses phy_ip_tunables_filtered (defined below) which parses the
    # tunables prop entry-by-entry and skips entries targeting an
    # INACTIVE port slice. On j773g, port 1 has no pci-bridge1 in the
    # ADT -- its downstream phy_ip slice is unpowered. Applying its
    # 47 auspma entries the m1n1 way (via tunables_apply_local) writes
    # into that slice and AXI-stalls the fabric. This wedged us at
    # step 6.h on the 2026-07-11 run (confirmed via phaseF.post.6.g
    # flush + no post.6.h flush + never-recovering m1n1).
    #
    # The upstream m1n1 fix for this would be a per-entry filter in
    # pcie.c:518-525 for chips where the ADT can have missing bridges
    # (t8132 is the first known case). Here we do the filter Python-
    # side so we can iterate without a m1n1 rebuild/reflash cycle.
    buf.write("  ==> ABOUT TO TOUCH phy_ip_base FOR THE FIRST TIME.\n"
              "     Entries in the INACTIVE port-1 slice (0x10000..0x18000)\n"
              "     will be SKIPPED to avoid the AXI stall observed on\n"
              "     the 2026-07-11 run. See phy-ip-report for entry list.\n")

    if not phy_ip_tunables_filtered("6.g", "apcie-phy-ip-pll-tunables"):
        return
    if not phy_ip_tunables_filtered("6.h", "apcie-phy-ip-auspma-tunables"):
        return

    # ---- step 7: phy_common CLK mode set (unless --phycmn-first
    # already applied it before step 6.a).
    if phycmn_done:
        buf.write("  --- 7.phy_common CLK MODE (already applied under "
                  "--phycmn-first) ---\n")
    elif not do_phycmn():
        return

    # ---- steps 8-10: RC init handshake
    if not step("8.write32(rc_base+0x54, 0x140)",
                lambda: p.write32(rc_base + 0x54, 0x140)):
        return
    if not step("9.write32(rc_base+0x50, 0x01)",
                lambda: p.write32(rc_base + 0x50, 0x01)):
        return
    if not poll_step("10.poll_rc_58_bit0",
                     rc_base + 0x58, 1, 1, timeout_ms=250):
        return

    apcie.phaseF_shared_up = True
    apcie.phaseF_last_step = "shared init complete"
    buf.write("\n  === PHASE F SUCCESS: T8140 shared init complete. ===\n")
    buf.write("  Shared PHY, phy_ip window, and RC control block are up.\n"
              "  Next: Phase G (per-port bring-up) or `p.pcie_init()` --\n"
              "  m1n1's own C-side init should now run without wedging.\n\n")


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
                    help="run pre-pcie_init probes. Phase 0 = PMGR gate "
                         "readout (safe, no apcie MMIO). Phase A = "
                         "reachability INFERENCE from Phase 0 state (no "
                         "MMIO). Off by default; combine with --pmgr-enable "
                         "/ --pmgr-per-port for deeper probing.")
    ap.add_argument("--pmgr-enable", action="store_true",
                    help="Phase B: call p.pmgr_adt_power_enable('/arm-io/apcie') "
                         "from Python (same helper m1n1's pcie.c:425 uses), "
                         "then probe the shared MMIO block set with per-read "
                         "exc_count bail. Requires --preinit-probe.")
    ap.add_argument("--pmgr-per-port", action="store_true",
                    help="Phase C: after --pmgr-enable, also call "
                         "pmgr_adt_power_enable on each active pci-bridge{N} "
                         "path and probe port_base Tier 1 regs. Requires "
                         "--preinit-probe and --pmgr-enable.")
    ap.add_argument("--gate-poke", action="store_true",
                    help="Phase D: direct PS-register poke for APCIE_PHY_SW "
                         "(gate 151). No-op if the gate is already ACTIVE. "
                         "Mirrors m1n1's pmgr_set_mode() write sequence; "
                         "escalates to raising the PS_AUTO floor if TARGET "
                         "alone doesn't converge. Requires --preinit-probe.")
    ap.add_argument("--phy-ip-probe", action="store_true",
                    help="Phase E: after --gate-poke confirms the gate 151 "
                         "chain ACTIVE, do a rc/axi sanity read (does NOT "
                         "touch phy_ip -- earlier versions wedged m1n1 on "
                         "the first phy_ip read because phy_ip is gated by "
                         "the phy_base CLK handshake done in Phase F, not "
                         "by PMGR). Requires --gate-poke.")
    ap.add_argument("--t8140-replay", action="store_true",
                    help="Phase F: replay m1n1 pcie.c T8140 shared-init "
                         "step by step in Python (pmgr, tunables, phy_base "
                         "CLK0/CLK1 handshake, phy_ip tunables, RC init "
                         "handshake). Each step guarded + liveness-checked "
                         "so a wedge tells us EXACTLY which m1n1 step is "
                         "broken on t8132. Requires --gate-poke.")
    ap.add_argument("--extra-tunables", action="store_true",
                    help="Phase F step 5.5: apply t8132-specific "
                         "apcie-cio3pllcore-tunables + "
                         "apcie-pcieclkgen-tunables BEFORE the phy_ip "
                         "tunables. m1n1's T8140 codepath ignores these; "
                         "on t8132 they may be the missing PCIe clock / "
                         "PLL setup that ungates phy_ip. Requires "
                         "--t8140-replay.")
    ap.add_argument("--phycmn-first", action="store_true",
                    help="Phase F ordering experiment: apply step 7 "
                         "(phy_common CLK MODE=ON) BEFORE step 6.g "
                         "(phy_ip tunables) instead of after. If the "
                         "phy_common CLK MODE bit is the ungate for "
                         "phy_ip, this reorder alone unblocks 6.g. "
                         "Requires --t8140-replay.")
    ap.add_argument("--phy-ip-diag", action="store_true",
                    help="Phase F diagnostic sweep: probe "
                         "read32(phy_ip_base+0) at 6 points in Phase F "
                         "(entry, after step 1, 5, 6.b, 6.d, 6.f) with "
                         "guarded read + alive check. Answers 'at which "
                         "step does phy_ip transition from unreachable "
                         "to reachable?'. Requires --t8140-replay.")
    ap.add_argument("--pmgr-explore", action="store_true",
                    help="Enumerate every PMGR gate whose name matches "
                         "PCIE/PHY/APCIE/ANS/DART_APCIE (case-insensitive) "
                         "and dump its PS state. Zero apcie MMIO -- PMGR "
                         "reads only. Reveals hidden gates m1n1 doesn't "
                         "poke that might gate phy_ip.")
    ap.add_argument("--dart-power", action="store_true",
                    help="Call p.pmgr_adt_power_enable() for "
                         "/arm-io/dart-apcie0 and /arm-io/dart-apcie2 "
                         "BEFORE Phase F. Skips dart-apcie1 (inactive "
                         "port). DART overlaps port ctrl_lo; enabling "
                         "DART power may be a prerequisite we've been "
                         "missing.")
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    runtime_path = out / "nic-runtime.txt"

    buf = io.StringIO()

    def flush(tag):
        flush_partial_log(runtime_path, buf, tag)

    log("=== Phase 3.3 pcie_up ===")

    # Build the ADT-sourced map first so every subsequent step has named
    # handles for the reg blocks it wants to touch.
    log("building ApcieMap from ADT...")
    apcie = ApcieMap.from_adt(u)
    apcie.describe(buf)
    buf.write("\n")
    flush("adt-map")

    # ADT-only, wedge-immune. Runs regardless of --no-pcie-init so we
    # always leave a full tunable dump in nic-runtime.txt.
    if not args.no_phy_ip_report:
        log("dumping apcie-phy-ip-{pll,auspma}-tunables report...")
        try_(lambda: dump_phy_ip_tunables_report(apcie, buf),
             "dump_phy_ip_tunables_report")
        flush("phy-ip-report")

        log("dumping apcie-{cio3pllcore,pcieclkgen}-tunables report...")
        try_(lambda: dump_extra_tunables_report(apcie, buf),
             "dump_extra_tunables_report")
        flush("extra-tunables-report")

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
    flush("smc-power")

    if args.no_clkreq:
        log("CLKREQ assert skipped (--no-clkreq)")
        buf.write("=== CLKREQ assert (skipped) ===\n\n")
    else:
        log(f"CLKREQ assert on gpio0 pin {clkreq_pin}...")
        try_(lambda: assert_clkreq(buf, pin=clkreq_pin),
             "assert_clkreq")
        flush("clkreq")

    if args.no_perstn:
        log("PERSTN toggle skipped (--no-perstn)")
        buf.write("=== PERSTN deassert (skipped) ===\n\n")
    else:
        log(f"PERSTN deassert on gpio0 pin {perstn_pin}...")
        try_(lambda: deassert_perstn(buf, pin=perstn_pin),
             "deassert_perstn")
        flush("perstn")

    timeout = args.dump_timeout

    if args.preinit_probe:
        log("Phase 0: PMGR gate readout (no apcie MMIO)...")
        try_(lambda: probe_phase0_pmgr_state(apcie, buf),
             "probe_phase0_pmgr_state")
        flush("phase0-pmgr-state")

        log("Phase 0.5: PMGR parent-chain readout (no apcie MMIO)...")
        try_(lambda: probe_phase0_5_parents(apcie, buf),
             "probe_phase0_5_parents")
        flush("phase0.5-parents")

        log("Phase A: single pre-PMGR probe at phy_ip_base+0...")
        try_(lambda: probe_phaseA_preinit_single(apcie, buf, timeout=timeout),
             "probe_phaseA_preinit_single")
        flush("phaseA-preinit-single")

        # Phase D/E run BEFORE Phase B/C so the direct poke can bypass
        # a wedge in m1n1's own pmgr_adt_power_enable helper. If both
        # --gate-poke and --pmgr-enable are set we still get D+E's
        # output even if B subsequently wedges m1n1.
        if args.gate_poke:
            log("Phase D: direct PS-register poke for gate 151...")
            try_(lambda: probe_phaseD_gate151_poke(apcie, buf),
                 "probe_phaseD_gate151_poke")
            flush("phaseD-gate151-poke")

            if args.phy_ip_probe:
                log("Phase E: post-Phase-D rc/axi sanity probe...")
                try_(lambda: probe_phaseE_rc_axi_sanity(apcie, buf, timeout=timeout),
                     "probe_phaseE_rc_axi_sanity")
                flush("phaseE-rc-axi-sanity")

            if args.pmgr_explore:
                log("PMGR explore: dumping all PHY/PCIE/APCIE gates...")
                try_(lambda: probe_pmgr_explore(u, buf),
                     "probe_pmgr_explore")
                flush("pmgr-explore")

            if args.dart_power:
                log("DART power: enabling /arm-io/dart-apcie{0,2}...")
                try_(lambda: probe_dart_power(buf),
                     "probe_dart_power")
                flush("dart-power")

            if args.t8140_replay:
                log("Phase F: T8140 controller-init replay (pcie.c)"
                    f" [extra_tunables={args.extra_tunables}, "
                    f"phycmn_first={args.phycmn_first}, "
                    f"phy_ip_diag={args.phy_ip_diag}]...")
                try_(lambda: probe_phaseF_t8140_replay(
                        apcie, buf, timeout=timeout, flush_fn=flush,
                        extra_tunables=args.extra_tunables,
                        phycmn_first=args.phycmn_first,
                        phy_ip_diag=args.phy_ip_diag),
                     "probe_phaseF_t8140_replay")
                flush("phaseF-t8140-replay")
        elif args.phy_ip_probe or args.t8140_replay:
            log("--phy-ip-probe/--t8140-replay require --gate-poke; skipping")
            buf.write("\n=== Phase E/F: skipped (need --gate-poke) ===\n\n")

        if args.pmgr_enable:
            log("Phase B: p.pmgr_adt_power_enable('/arm-io/apcie') + shared MMIO...")
            try_(lambda: probe_phaseB_apcie_pmgr(apcie, buf, timeout=timeout),
                 "probe_phaseB_apcie_pmgr")
            flush("phaseB-apcie-pmgr")

            if args.pmgr_per_port:
                log("Phase C: per-port pmgr_adt_power_enable + port_base probe...")
                try_(lambda: probe_phaseC_port_pmgr(apcie, buf, timeout=timeout),
                     "probe_phaseC_port_pmgr")
                flush("phaseC-port-pmgr")
        elif args.pmgr_per_port:
            log("--pmgr-per-port requested without --pmgr-enable; skipping Phase C")
            buf.write("\n=== Phase C: skipped (--pmgr-per-port needs "
                      "--pmgr-enable) ===\n\n")

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
    flush("pcie-init")

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
        flush("dump-post-init")

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
            flush("ltssm-kick")

            if liveness_gate("post-kick dump"):
                log(f"dumping PCIe controller registers "
                    f"(post-kick, tier={args.tier})...")
                with guarded(buf, "dump_pcie_regs(post-kick)",
                             short_timeout=timeout):
                    try_(lambda: dump_pcie_regs(apcie, buf, "post-kick",
                                                tier=args.tier),
                         "dump_pcie_regs")
                flush("dump-post-kick")

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
            flush("t602x-replay")
            if liveness_gate("post-T602X dump"):
                log(f"dumping PCIe controller registers "
                    f"(post-t602x, tier={args.tier})...")
                with guarded(buf, "dump_pcie_regs(post-t602x)",
                             short_timeout=timeout):
                    try_(lambda: dump_pcie_regs(apcie, buf, "post-t602x",
                                                tier=args.tier),
                         "dump_pcie_regs")
                flush("dump-post-t602x")

        if args.unblock_experiment and liveness_gate("unblock experiment"):
            log(f"running unblock experiment on port {args.unblock_port}...")
            with guarded(buf, "unblock_experiment",
                         short_timeout=timeout):
                try_(lambda: unblock_experiment(apcie, buf,
                                                port_index=args.unblock_port),
                     "unblock_experiment")
            flush("unblock-experiment")
            if liveness_gate("post-unblock dump"):
                log(f"dumping PCIe controller registers "
                    f"(post-unblock, tier={args.tier})...")
                with guarded(buf, "dump_pcie_regs(post-unblock)",
                             short_timeout=timeout):
                    try_(lambda: dump_pcie_regs(apcie, buf, "post-unblock",
                                                tier=args.tier),
                         "dump_pcie_regs")
                flush("dump-post-unblock")
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
        flush("ecam-walk")
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
        flush("enable-nic")
    elif nic is None:
        buf.write("\n=== enable NIC ===\nNo class-0x02 device found.\n")

    summarize(runtime_path, buf, devices, nic)
    log("done. Copy /tmp/m4-recon/nic-runtime.txt into m4_recon/ if it looks sane.")


if __name__ == "__main__":
    main()
