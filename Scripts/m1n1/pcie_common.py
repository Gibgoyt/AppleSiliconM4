#!/usr/bin/env python3
"""pcie_common -- shared PCIe/ECAM primitives for the M4 (t8132 / j773) m1n1
bring-up scripts.

Extracted verbatim from perstn.py so perstn.py (PCIe init + endpoint diag) and
nic_bringup.py (Phase 3.5: descend behind the bridge, identify the NIC) share
exactly ONE copy of:

  * PCI config-space accessors + constants,
  * the apcie SMC power-on,
  * the NIC PERSTN / CLKREQ GPIO helpers,
  * the ECAM enumeration walk (probe_device / ecam_walk / enable_nic),
  * the LINKSTS decode + watch.

These are the SELF-CONTAINED pieces -- they depend only on m4_common (the
connection handles `u`/`p`, SMCClient, and the log/guard helpers). The heavier,
perstn-entangled bring-up steps (setup_refclk, perst_resequence,
t602x_port_init_replay) intentionally stay in perstn.py; nic_bringup.py drives
the happy path (smc_power -> GPIO -> pcie_init -> watch_linksts) plus a minimal
local PERST fallback.

Importing this module transitively imports m4_common, which OPENS the UART and
establishes `u`/`p`. Only one bring-up script runs per invocation, so there is
exactly one connection per run.
"""

import time

from m4_common import (
    u, p, SMCClient,
    log, try_,
)


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


# ---------------------------------------------------------------- ECAM address

def ecam_addr(base, bus, dev, fn, off):
    return base + (bus << 20) + (dev << 15) + (fn << 12) + off


def cfg_read32(base, bus, dev, fn, off):
    return p.read32(ecam_addr(base, bus, dev, fn, off))


def cfg_read16(base, bus, dev, fn, off):
    return p.read16(ecam_addr(base, bus, dev, fn, off))


def cfg_read8(base, bus, dev, fn, off):
    return p.read8(ecam_addr(base, bus, dev, fn, off))


def cfg_write32(base, bus, dev, fn, off, val):
    p.write32(ecam_addr(base, bus, dev, fn, off), val)


def cfg_write16(base, bus, dev, fn, off, val):
    p.write16(ecam_addr(base, bus, dev, fn, off), val)


def cfg_write8(base, bus, dev, fn, off, val):
    p.write8(ecam_addr(base, bus, dev, fn, off), val)


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
    # RUN 23: read the keys back IN THE SAME SESSION (before stop()). RUN 22's
    # endpoint_diag read them in a fresh session post-stop() and got 0 --
    # confounded. This tells us whether the writes stick vs whether gP0d/gP1a
    # are write-triggered keys that read 0 by design.
    for key, want in (("gP0d", 0x800001), ("gP1a", 1)):
        try:
            val = smc.smcep.read32(key)
            ok = "OK" if val == want else "MISMATCH"
            buf.write(f"gP{key[2:]} same-session readback = 0x{val:x} "
                      f"(want 0x{want:x}) [{ok}]\n")
        except Exception as e:
            buf.write(f"{key} same-session read RAISED: "
                      f"{e.__class__.__name__}: {e}\n")
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
    # try_() in the caller) leaving nic-runtime.txt behind instead of wedging.
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


def gpio_set_periph(pin, func, buf=None):
    """Mux `pin` to peripheral function `func` (0-3), mirroring Linux
    pinctrl-apple-gpio.c's pinmux_set_mux: modify only the PERIPH field
    and set INPUT_ENABLE, preserving everything else. The ADT declares
    CLKREQ as GPIO(162, 2) -- alt-function 2, i.e. the PCIe controller
    manages CLKREQ# itself.
    """
    addr = _gpio_reg_addr(pin)
    old = p.read32(addr)
    new = old & ~REG_GPIOx_PERIPH_MASK
    new |= (func & 0x3) << 5
    new |= REG_GPIOx_INPUT_ENABLE
    p.write32(addr, new)
    read_back = p.read32(addr)
    if buf is not None:
        buf.write(f"gpio0[{pin}] @ 0x{addr:x}: 0x{old:08x} -> "
                  f"0x{new:08x} (read-back 0x{read_back:08x}) "
                  f"[periph func {func}]\n")
    return old, new, read_back


def assert_clkreq(buf, pin=CLKREQ_PIN, settle_ms=1):
    """Drive CLKREQ# (gpio0 pin `pin`) low so the endpoint has a valid refclk
    request asserted before we release PERSTN#. Called BEFORE deassert_perstn.
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


# ---------------------------------------------------------------- LINKSTS

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


def watch_linksts(apcie, buf, secs=5.0, label="post-init"):
    """Watch each active port's LINKSTS (port_base+0x208) for up to `secs`,
    logging every state change and exiting early per port when BUSY (bit 2)
    clears. Pure reads on proven-safe port_base windows -- abandons a port's
    watch on a read exception rather than wedging."""
    buf.write(f"=== {label} LINKSTS watch ({secs:.0f} s per active "
              f"port) ===\n")
    log(f"{label} LINKSTS watch (up to {secs:.0f} s per port)...")
    for pi in apcie.active_ports:
        pb = apcie.ports[pi].port_base
        last = None
        t0 = time.monotonic()
        while time.monotonic() - t0 < secs:
            try:
                v = p.read32(pb + 0x208)
            except Exception as e:
                buf.write(f"  port{pi}: LINKSTS read RAISED "
                          f"{e.__class__.__name__}; abandoning watch\n")
                break
            if v != last:
                buf.write(f"  port{pi}: LINKSTS=0x{v:08x} "
                          f"[{_linksts_decode(v)}] at "
                          f"+{time.monotonic() - t0:.2f}s\n")
                last = v
            if not (v & (1 << 2)):
                buf.write(f"  port{pi}: BUSY CLEARED after "
                          f"{time.monotonic() - t0:.2f}s!\n")
                break
            time.sleep(0.1)
        else:
            buf.write(f"  port{pi}: still BUSY after {secs:.0f} s "
                      f"(LINKSTS=0x{last:08x})\n")


# ---------------------------------------------------------------- LTSSM / link-up

# APCIE_PORT_LINKSTS bits (m1n1 src/pcie.c:57-60).
APCIE_PORT_LINKSTS      = 0x208
APCIE_PORT_LINKSTS_UP   = 1 << 0
APCIE_PORT_LINKSTS_BUSY = 1 << 2

# LTSSM debug-block register offsets in ltssm_base. This is the exact "kick"
# sequence m1n1's C code runs for APCIE_T602X ports (src/pcie.c:718-722, 746-749)
# but SKIPS for T8132 (compat T8122). Replaying it host-side tests whether the
# T8132 downstream link needs it to leave Detect and reach L0 (LINKSTS bit0 UP).
LTSSM_KICK_10 = 0x10  # write 0x2
LTSSM_KICK_1C = 0x1c  # write 0x4
LTSSM_KICK_20 = 0x20  # set  0x2
LTSSM_START   = 0x14  # write 0x1 -- the START/enable bit


def poll_linksts_up(apcie, port_index, buf, secs=2.0):
    """Poll port `port_index` LINKSTS for the UP bit (bit0) for up to `secs`.
    Returns (is_up, last_value). Pure reads; tolerant of a read fault."""
    pb = apcie.ports[port_index].port_base
    last = None
    t0 = time.monotonic()
    while time.monotonic() - t0 < secs:
        try:
            v = p.read32(pb + APCIE_PORT_LINKSTS)
        except Exception as e:
            buf.write(f"  port{port_index}: LINKSTS_UP read RAISED "
                      f"{e.__class__.__name__}; abandoning poll\n")
            return False, last
        if v != last:
            buf.write(f"  port{port_index}: LINKSTS=0x{v:08x} "
                      f"[{_linksts_decode(v)}] at "
                      f"+{time.monotonic() - t0:.2f}s\n")
            last = v
        if v & APCIE_PORT_LINKSTS_UP:
            buf.write(f"  port{port_index}: LINK UP after "
                      f"{time.monotonic() - t0:.2f}s!\n")
            return True, v
        time.sleep(0.05)
    buf.write(f"  port{port_index}: NOT UP after {secs:.0f} s "
              f"(LINKSTS=0x{last:08x if last is not None else 0})\n")
    return False, last


def ltssm_kick(apcie, port_index, buf):
    """Replay the T602X LTSSM kick against port `port_index`'s ltssm_base --
    the sequence m1n1's C skips for T8132. Writes are guarded by the caller;
    this logs each write and reads LTSSM_START back."""
    lt = apcie.ports[port_index].ltssm_base
    buf.write(f"  port{port_index}: LTSSM kick @ ltssm_base=0x{lt:x}\n")
    for off, val, op in (
        (LTSSM_KICK_10, 0x2, "write"),
        (LTSSM_KICK_1C, 0x4, "write"),
        (LTSSM_KICK_20, 0x2, "set"),
        (LTSSM_START,   0x1, "write"),
    ):
        try:
            if op == "set":
                p.set32(lt + off, val)
            else:
                p.write32(lt + off, val)
        except Exception as e:
            buf.write(f"    ltssm+0x{off:02x} {op} 0x{val:x} FAILED: "
                      f"{e.__class__.__name__}: {e}\n")
            continue
        buf.write(f"    ltssm+0x{off:02x} <- 0x{val:x} ({op})\n")
    # Read the START bit back -- in RUN 20 it refused to latch (read 0).
    try:
        rb = p.read32(lt + LTSSM_START)
        buf.write(f"    ltssm+0x{LTSSM_START:02x} (START) readback = 0x{rb:08x}\n")
    except Exception as e:
        buf.write(f"    START readback FAILED: {e.__class__.__name__}: {e}\n")
