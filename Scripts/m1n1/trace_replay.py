#!/usr/bin/env python3
"""RUN T/U -- replay Yureka's macOS MMIO trace (pcie.log) against m1n1.

Every RUN A..S and 1..33 wedged on the first phy_ip access
(0x497040038, UartTimeout on an AXI stall). pcie.log line 35 shows macOS
reading that exact register successfully, so trace lines 1..34 are the
complete unlock preamble. This driver replays trace segments
(parsed/planned by trace_ops.py) with the usual guard + partial-flush
machinery, then probes phy_ip.

Replay semantics (see trace_ops.plan_segment):
  rmw       read live pre-value, log MISMATCH vs the trace's pre-value,
            apply only the trace's bit delta (set/clr masks) -- bits
            outside the delta are preserved, never forced
  write     naked absolute write (trace op had no preceding read)
  read      read + compare to the trace's value (never fatal)
  pollmask  poll until the handshake-ack bits appear
  pollval   poll until the register reaches the trace's final value
            (link polls); logs last value on timeout, never fatal

Key deltas vs our previous RUN S replay, all encoded in the trace:
  - axi_base+0x104 / +0x108 |= 1 first (never done before)
  - rc_base+0x04 = 0
  - per-port PHY pre-writes for ALL 3 ports before the CLKREQ handshake
  - after CLK1ACK: clear BIT 4, KEEP bit 7 (we used to clear bit 7)
  - phy_shared+4 = 1 plain; no |=0x11 / phy_common MODE / |=0x300 extras

Segments (see trace_ops.SEGMENTS): preamble, phyip, port0, port2, enum.
RUN T = --segments preamble (default) + the phy_ip probe.
RUN U = --segments preamble,phyip,port2 [--ecam]

Usage (via trace_replay.sh; needs a booted m1n1 on USB):
    ./Scripts/m1n1/trace_replay.sh                       # RUN T
    ./Scripts/m1n1/trace_replay.sh --segments preamble,phyip,port2 --ecam
"""

import argparse
import io
import pathlib
import sys
import time
import traceback

sys.path.append(str(pathlib.Path(__file__).resolve().parents[3] /
    "m1n1" / "proxyclient"))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

from m4_common import (
    u, p, iface,
    log, try_, flush_partial_log,
    guarded, check_alive,
    GUARD_SENTINEL,
    require_build,
)
from pcie_regs import ApcieMap
from pcie_common import (
    smc_power,
    PERSTN_PIN, CLKREQ_PIN,
    deassert_perstn, gpio_set_periph,
    ecam_walk, enable_nic, watch_linksts,
    cfg_read32, cfg_write32,
)
import trace_ops
from trace_ops import describe_step, poll_timeout_for


class ReplayWedged(Exception):
    """m1n1 stopped responding mid-replay (AXI stall). Abort the run."""


# ------------------------------------------------------------- MMIO by width

def _read(op):
    if op.width == 4:
        return p.read32(op.addr)
    if op.width == 2:
        return p.read16(op.addr)
    return p.read8(op.addr)


def _write(op, value):
    if op.width == 4:
        return p.write32(op.addr, value)
    if op.width == 2:
        return p.write16(op.addr, value)
    return p.write8(op.addr, value)


def _mmio(fn, step, buf, what):
    """Run one MMIO access; on exception decide wedged-vs-alive."""
    try:
        return fn()
    except Exception as e:
        msg = f"{e.__class__.__name__}: {e}"
        buf.write(f"    !! {what} FAILED at L{step.op.line} "
                  f"0x{step.op.addr:x}: {msg}\n")
        log(f"    !! {what} FAILED at trace L{step.op.line}: {msg}")
        if not check_alive():
            buf.write("    [ABORT] m1n1 not responding -- wedged\n")
            raise ReplayWedged(f"L{step.op.line} 0x{step.op.addr:x}") from e
        buf.write("    (m1n1 still alive; continuing)\n")
        return None


# ------------------------------------------------------------- step execution

class Stats:
    def __init__(self):
        self.writes = 0
        self.rmws = 0
        self.reads_match = 0
        self.reads_mismatch = 0
        self.polls_ok = 0
        self.polls_timeout = 0
        self.pre_mismatch = 0
        self.faults = 0

    def summary(self):
        return (f"writes={self.writes} rmws={self.rmws} "
                f"read match/mismatch={self.reads_match}/"
                f"{self.reads_mismatch} "
                f"polls ok/timeout={self.polls_ok}/{self.polls_timeout} "
                f"pre-mismatch={self.pre_mismatch} faults={self.faults}")


def exec_step(step, buf, stats):
    op = step.op
    tag = f"L{op.line:<4d} 0x{op.addr:09x} w{op.width}"

    if step.kind == "write":
        _mmio(lambda: _write(op, step.a), step, buf, "write")
        buf.write(f"  {tag} WRITE 0x{step.a:x}\n")
        stats.writes += 1
        return

    if step.kind == "rmw":
        pre = _mmio(lambda: _read(op), step, buf, "rmw pre-read")
        if pre is None:
            stats.faults += 1
            return
        if op.width == 4 and pre == GUARD_SENTINEL:
            buf.write(f"  {tag} RMW pre-read hit GUARD sentinel (SLVERR) "
                      f"-- write skipped\n")
            stats.faults += 1
            return
        note = ""
        if step.exp_pre is not None and pre != step.exp_pre:
            note = f" PRE-MISMATCH (trace pre=0x{step.exp_pre:x})"
            stats.pre_mismatch += 1
        # Always issue the write, even when new == pre: trace pairs like
        # R 0x1000; W 0x1000 are write-1-to-clear status acks, and
        # suppressing the "same value" write would drop that side effect.
        new = (pre | step.a) & ~step.b
        _mmio(lambda: _write(op, new), step, buf, "rmw write")
        buf.write(f"  {tag} RMW set=0x{step.a:x} clr=0x{step.b:x} "
                  f"pre=0x{pre:x} -> 0x{new:x}"
                  f"{' (same)' if new == pre else ''}{note}\n")
        stats.rmws += 1
        return

    if step.kind == "read":
        v = _mmio(lambda: _read(op), step, buf, "read")
        if v is None:
            stats.faults += 1
            return
        if v == step.a:
            buf.write(f"  {tag} READ 0x{v:x} MATCH\n")
            stats.reads_match += 1
        else:
            buf.write(f"  {tag} READ 0x{v:x} MISMATCH "
                      f"(trace 0x{step.a:x})\n")
            stats.reads_mismatch += 1
        return

    if step.kind in ("pollmask", "pollval"):
        timeout = poll_timeout_for(op)
        deadline = time.time() + timeout
        v = None
        converged = False
        while time.time() < deadline:
            v = _mmio(lambda: _read(op), step, buf, "poll read")
            if v is None:
                stats.faults += 1
                return
            if step.kind == "pollmask" and (v & step.a) == step.a:
                converged = True
                break
            if step.kind == "pollval" and v == step.a:
                converged = True
                break
            time.sleep(0.001)
        want = (f"(val & 0x{step.a:x}) == 0x{step.a:x}"
                if step.kind == "pollmask" else f"val == 0x{step.a:x}")
        if converged:
            buf.write(f"  {tag} POLL {want} CONVERGED val=0x{v:x}\n")
            stats.polls_ok += 1
        else:
            buf.write(f"  {tag} POLL {want} TIMEOUT after {timeout}s, "
                      f"last=0x{v:x}\n")
            stats.polls_timeout += 1
        return

    buf.write(f"  {tag} UNKNOWN step kind {step.kind}\n")


def replay_segment(name, steps, buf, flush, stats):
    log(f"=== replaying segment '{name}' ({len(steps)} steps) ===")
    buf.write(f"\n=== segment {name} ({len(steps)} steps) ===\n")
    flush(f"{name}-start")
    for idx, step in enumerate(steps):
        label = f"{name}[{idx}] {step.kind} L{step.op.line}"
        with guarded(buf, label):
            exec_step(step, buf, stats)
        # Preamble is the experiment -- keep per-op evidence. Bigger
        # segments flush every 10 steps to bound wedge-loss.
        if name == "preamble" or idx % 10 == 9:
            flush(f"{name}-{idx}")
    buf.write(f"--- segment {name} done: {stats.summary()} ---\n")
    flush(f"{name}-done")


# ---------------------------------------------------------------- phy_ip probe

def probe_phy_ip(apcie, buf, flush):
    """THE probe: read phy_ip+0x38 (0x497040038). Wedged on every prior
    run. Trace says macOS read 0x5c800800 here."""
    addr = apcie.phy_ip_base + 0x38
    log(f"PROBE: read32(0x{addr:x}) [phy_ip+0x38] -- the RUN A..S wedge "
        f"point...")
    buf.write(f"\n=== phy_ip probe ===\nread32(0x{addr:x}) [phy_ip+0x38]:\n")
    flush("probe-pre")
    ok = False
    with guarded(buf, "phy-ip-probe"):
        try:
            v = p.read32(addr)
            tag = " <-- GUARD sentinel (SLVERR)" if v == GUARD_SENTINEL else ""
            buf.write(f"  PROBE PASS: 0x{v:08x}{tag} "
                      f"(trace value was 0x5c800800)\n")
            log(f"  PROBE PASS: 0x{v:08x}{tag}")
            ok = v != GUARD_SENTINEL
        except Exception as e:
            buf.write(f"  PROBE FAIL: {e.__class__.__name__}: {e}\n")
            log(f"  PROBE FAIL: {e.__class__.__name__}: {e}")
            if not check_alive():
                buf.write("  [ABORT] m1n1 not responding after probe\n")
                flush("probe-wedged")
                raise ReplayWedged("phy_ip probe") from e
    flush("probe-done")
    return ok


# ------------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(
        description=__doc__.strip().splitlines()[0])
    ap.add_argument("--out", default="/tmp/m4-recon",
                    help="output directory (default: /tmp/m4-recon)")
    ap.add_argument("--trace",
                    default=str(pathlib.Path(__file__).resolve()
                                .parents[2] / "pcie.log"),
                    help="path to the macOS MMIO trace (default: repo "
                         "pcie.log)")
    ap.add_argument("--segments", default="preamble",
                    help="comma list of trace segments to replay, in "
                         "order (default: preamble). Available: "
                         + ",".join(trace_ops.SEGMENTS))
    ap.add_argument("--no-probe", action="store_true",
                    help="skip the phy_ip+0x38 probe after the preamble")
    ap.add_argument("--ecam", action="store_true",
                    help="after replay, run the existing ECAM walk + "
                         "NIC enable instead of replaying the 'enum' "
                         "segment")
    ap.add_argument("--perst-early", action="store_true",
                    help="deassert PERSTN during setup (previous runs' "
                         "behavior) instead of just before the port2 "
                         "segment")
    ap.add_argument("--require-build", default=None,
                    help="abort unless the enrolled m1n1 build string "
                         "contains this substring")
    args = ap.parse_args()

    if args.require_build:
        require_build(args.require_build)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    out_path = out / "nic-runtime.txt"
    buf = io.StringIO()

    def flush(tag):
        flush_partial_log(out_path, buf, tag)

    segments = [s.strip() for s in args.segments.split(",") if s.strip()]
    for s in segments:
        if s not in trace_ops.SEGMENTS:
            ap.error(f"unknown segment {s!r}; "
                     f"available: {','.join(trace_ops.SEGMENTS)}")

    log("=== RUN T/U: macOS MMIO trace replay ===")
    buf.write("=== RUN T/U: macOS MMIO trace replay ===\n"
              f"trace    = {args.trace}\n"
              f"segments = {segments}\n\n")

    ops = trace_ops.parse_log(args.trace)
    buf.write(f"parsed {len(ops)} trace ops\n")

    apcie = ApcieMap.from_adt(u)
    # Sanity: the trace's absolute addresses must match this machine's ADT.
    assert apcie.phy_ip_base == 0x497040000, hex(apcie.phy_ip_base)
    assert apcie.phy_packed_base == 0x497000000, hex(apcie.phy_packed_base)
    buf.write(f"ADT map ok: phy_ip=0x{apcie.phy_ip_base:x} "
              f"axi=0x{apcie.axi_base:x} rc=0x{apcie.rc_base:x}\n")

    # ---- setup: SMC power + CLKREQ periph (as every prior run) ----
    log("SMC power on apcie fabric...")
    try_(lambda: smc_power(buf), "SMC power")
    flush("smc-power")

    log(f"CLKREQ -> periph func 2 on gpio0 pin {CLKREQ_PIN}...")
    try_(lambda: gpio_set_periph(CLKREQ_PIN, 2, buf), "CLKREQ periph")
    flush("clkreq-periph")

    if args.perst_early:
        log(f"PERSTN deassert (early) on gpio0 pin {PERSTN_PIN}...")
        try_(lambda: deassert_perstn(buf), "PERSTN")
        flush("perstn-early")

    # ---- replay ----
    stats = Stats()
    probed = None
    wedged = False
    try:
        for name in segments:
            if name == "port2" and not args.perst_early:
                # PCIe spec ordering: refclk up (preamble+phyip done)
                # BEFORE PERST# release.
                log(f"PERSTN deassert on gpio0 pin {PERSTN_PIN} "
                    f"(pre-port2)...")
                try_(lambda: deassert_perstn(buf), "PERSTN")
                flush("perstn-pre-port2")
            steps = trace_ops.plan_segment(
                trace_ops.segment_ops(ops, name))
            replay_segment(name, steps, buf, flush, stats)
            if name == "preamble" and not args.no_probe:
                probed = probe_phy_ip(apcie, buf, flush)
                if not probed:
                    log("phy_ip probe did not pass -- continuing with "
                        "remaining segments would wedge; stopping replay")
                    break
    except ReplayWedged as e:
        buf.write(f"\n[WEDGED] replay aborted: {e}\n")
        log(f"[WEDGED] replay aborted: {e}")
        flush("wedged")
        wedged = True

    # ---- post: link status + optional ECAM ----
    healthy = not wedged and probed is not False
    if healthy and any(s in segments for s in ("port0", "port2")):
        try_(lambda: watch_linksts(apcie, buf, secs=2.0,
                                   label="post-replay"), "watch_linksts")
        flush("linksts")
    if healthy and args.ecam:
        log(f"ECAM walk @ 0x{apcie.ecam_base:x} ...")
        devices = try_(lambda: ecam_walk(apcie.ecam_base, buf,
                                         active_ports=apcie.active_ports),
                       "ecam_walk") or []
        # Root-port bridges come up with secondary bus 0, so the walk can't
        # descend to the endpoint. Assign bus numbers the way macOS did
        # (trace L1396: cfg+0x18 = 0x10100 on the port-2 bridge -> pri=0,
        # sec=1, sub=1), sequentially per bridge, then re-walk.
        next_bus = 1
        assigned = False
        for d in devices:
            if d.get("cls_base") == 0x06 and not d.get("secondary"):
                busreg = (next_bus << 16) | (next_bus << 8)
                cfg_write32(apcie.ecam_base, d["bus"], d["dev"], d["fn"],
                            0x18, busreg)
                rb = cfg_read32(apcie.ecam_base, d["bus"], d["dev"],
                                d["fn"], 0x18)
                buf.write(f"\nbridge {d['bus']:02x}:{d['dev']:02x}."
                          f"{d['fn']}: bus numbers <- 0x{busreg:06x} "
                          f"(readback 0x{rb:08x})\n")
                next_bus += 1
                assigned = True
        if assigned:
            log("bus numbers assigned; ECAM re-walk...")
            devices = try_(lambda: ecam_walk(apcie.ecam_base, buf,
                                             active_ports=apcie.active_ports),
                           "ecam_rewalk") or []
        nic = next((d for d in devices if d.get("cls_base") == 0x02), None)
        if nic is not None:
            try_(lambda: enable_nic(apcie.ecam_base, nic, buf),
                 "enable_nic")
        else:
            buf.write("\n=== enable NIC ===\nNo class-0x02 device found.\n")
        flush("ecam")

    buf.write(f"\n=== FINAL: {stats.summary()} ===\n")
    buf.write(f"phy_ip probe: "
              f"{'PASS' if probed else 'not run' if probed is None else 'FAIL'}\n")
    flush("final")
    log(f"done: {stats.summary()}")
    log(f"phy_ip probe: "
        f"{'PASS' if probed else 'not run' if probed is None else 'FAIL'}")


if __name__ == "__main__":
    main()
