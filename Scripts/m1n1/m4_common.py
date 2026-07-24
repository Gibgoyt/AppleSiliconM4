#!/usr/bin/env python3
"""m4_common -- shared low-level primitives for the M4 (t8132 / j773) m1n1
bring-up scripts.

Both perstn.py (PCIe init + endpoint diag) and soc_bringup.py (SoC-first:
SMP + companion-IOP bring-up) import from here so there is exactly ONE copy
of the connection preamble, the guarded-read/liveness helpers, and the PMGR
gate-state readers.

Importing this module runs `from m1n1.setup import *`, which OPENS the UART
(M1N1DEVICE, default /dev/ttyACM0) and establishes the proxy handles
`u` (ADT interface), `p` (proxy client), `iface` (serial interface). Only one
bring-up script runs per invocation, and neither imports the other, so there
is exactly one connection per run -- no double-open.

Everything here is lifted verbatim from perstn.py to keep behavior identical;
see perstn.py's history for the RUN-by-RUN rationale behind each helper.
"""

import os
import pathlib
import traceback
from contextlib import contextmanager

sys_path_root = pathlib.Path(__file__).resolve().parents[3] / "m1n1" / "proxyclient"
import sys
sys.path.append(str(sys_path_root))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

from m1n1.setup import *          # noqa: F401,F403 -- exposes u, p, iface
from m1n1.fw.smc import SMCClient  # noqa: F401 -- re-exported for callers
from m1n1.proxy import GUARD


# ---------------------------------------------------------------- build guard

def _usb_product_string():
    """Read the m1n1 USB gadget's product string via sysfs for the tty
    named by M1N1DEVICE (default /dev/ttyACM0). Returns e.g.
    'm1n1 uartproxy v1.6.0-rc1-59-g' or None if unavailable. Used by
    --require-build to catch stale enrollments before any device state
    changes (RUN 13 lesson)."""
    dev = os.environ.get("M1N1DEVICE", "/dev/ttyACM0").split(":")[0]
    name = os.path.basename(dev)
    try:
        iface_dir = pathlib.Path(f"/sys/class/tty/{name}/device").resolve()
    except OSError:
        return None
    for cand in (iface_dir, iface_dir.parent, iface_dir.parent.parent):
        f = cand / "product"
        try:
            if f.exists():
                return f.read_text().strip()
        except OSError:
            continue
    return None


def require_build(substr):
    """RUN 14 stale-binary guard. RUN 13 burned a boot silently re-running
    an old run because the new m1n1 build was staged but never enrolled. The
    USB product string carries the build version (truncated ~30 chars, e.g.
    "m1n1 uartproxy v1.6.0-rc1-59-g"), so match on a substring like
    "rc1-60-g". Call BEFORE any device state changes.

    Returns True to continue; calls sys.exit(2) on a confirmed mismatch.
    """
    if not substr:
        return True
    product = _usb_product_string()
    if product is None:
        log(f"WARNING: cannot read the USB product string to verify the "
            f"enrolled build (--require-build={substr!r}); continuing "
            f"UNVERIFIED")
        return True
    if substr not in product:
        log("=" * 64)
        log("FATAL: enrolled m1n1 build mismatch!")
        log(f"  expected substring: {substr!r}")
        log(f"  USB product string: {product!r}")
        log("  The staged build at /tmp/m4-serve was NOT enrolled.")
        log("  Re-enroll via 1TR kmutil, power-cycle, and re-run.")
        log("=" * 64)
        sys.exit(2)
    log(f"require-build OK: {product!r} contains {substr!r}")
    return True


# ---------------------------------------------------------------- logging

def log(msg):
    print(f"[pcie_up] {msg}")


def try_(fn, label):
    try:
        return fn()
    except Exception as e:
        log(f"WARN {label}: {e.__class__.__name__}: {e}")
        traceback.print_exc(limit=3)
        return None


def flush_partial_log(out_path, buf, tag):
    """Persist buf to out_path mid-run so a subsequent wedge doesn't
    destroy the log we already have. Call after every stable section.
    """
    try:
        out_path.write_text(buf.getvalue())
        log(f"[flush:{tag}] wrote partial log ({len(buf.getvalue())} bytes) "
            f"to {out_path}")
    except Exception as e:
        log(f"[flush:{tag}] FAILED: {e.__class__.__name__}: {e}")


# ---------------------------------------------------------------- guard / liveness

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


# ---------------------------------------------------------------- guarded MMIO read

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


# ---------------------------------------------------------------- PMGR gate state

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


# ---------------------------------------------------------------- ADT helper

# Compatible-string fragments that mark a companion processor (IOP/ASC).
_IOP_COMPAT_MARKERS = ("iop,", "ascwrap", "rtbuddy", "mxwrap-acio",
                       "iop-nub")


def _adt_compat_str(node):
    """Return a node's 'compatible' as a lowercase string, or '' on failure."""
    try:
        c = getattr(node, "compatible", None)
        if c is None:
            return ""
        if isinstance(c, (list, tuple)):
            return " ".join(str(x) for x in c).lower()
        return str(c).lower()
    except Exception:
        return ""
