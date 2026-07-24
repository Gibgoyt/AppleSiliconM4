#!/usr/bin/env python3
"""soc_bringup -- SoC-first bring-up for the M4 (t8132 / j773) mini over m1n1.

RUN 25 onward. After six falsified single-core PCIe runs (18-23), RUN 24
pivoted to "bring the whole SoC up before touching PCIe" and found, read-only:

  * 9/9 secondary CPU cores REFUSED to start (p.smp_start_secondaries()).
  * The ACIO companion IOPs (acio-cpu0/1/3, role ACIO0/1/3,
    compatible=iop,mxwrap-acio) -- the owners of the CIO3-PLL / phy_ip PHY
    fabric that AXI-stalls -- have their PMGR gates OFF and were never booted.
  * apcie gate 151 (APCIE_PHY_SW) OFF, its parent 150 (APCIE_SYS_ST) hard
    power-gated; the RC is cycling but never links.

This script is the new home for that SoC-first work, split out of the (6.7k
line) PCIe-focused perstn.py. It shares all low-level primitives with perstn.py
via m4_common. RUN 25 is READ-ONLY recon only -- no IOP boot, no phy_ip touch,
no pcie_init, no reflash. It answers the two questions RUN 24 left open:

  --smp-diag    : WHY do the cores refuse? (cluster power-gated vs
                  spin-table not taken -- PMGR CPU-gate state per refused core)
  --acio-status : is each acio-cpuN rtkit actually running / stopped / in
                  reset? (the CPU_STATUS read RUN 24 skipped -- this is what
                  proves or breaks "the PHY is owned by an un-booted ACIO IOP")

Outcome decides RUN 26: boot the ACIO IOP vs fix core power first.
"""

import argparse
import io
import pathlib

from m4_common import (
    u, p, GUARD,
    log, try_, flush_partial_log, require_build,
    guarded, check_alive,
    _read32_live, GUARD_SENTINEL,
    _load_pmgr_devices, _read_pmgr_gate_state,
    _adt_compat_str, _IOP_COMPAT_MARKERS,
)

# ASC (rtkit IOP) register offsets, from m1n1 proxyclient m1n1/hw/asc.py:
#   CPU_CONTROL @ +0x44  -- bit 4 = RUN
#   CPU_STATUS  @ +0x48  -- bit 0 = RUNNING, bit 1 = STOPPED, bit 5 = IDLE
# We only ever READ these; a bare read is wedge-guarded and cannot boot the IOP.
ASC_CPU_CONTROL = 0x44
ASC_CPU_STATUS  = 0x48
ASC_CPU_CONTROL_RUN = 1 << 4
ASC_CPU_STATUS_RUNNING = 1 << 0
ASC_CPU_STATUS_STOPPED = 1 << 1
ASC_CPU_STATUS_IDLE    = 1 << 5


# ---------------------------------------------------------------- SoC recon

def soc_recon(buf):
    """Read-only recon of the SoC's companion processors (IOPs) and which
    apcie/CIO power domains are asleep. ADT-parse + safe PMGR PS reads only --
    no risky MMIO, no IOP boot. Tests the theory that the CIO3-PLL / phy_ip
    block (which AXI-stalls) is owned by the never-booted ACIO IOP
    (iop,mxwrap-acio, role ACIO0), i.e. we must bring the SoC up before PCIe.
    (Moved verbatim from perstn.py; RUN 24's probe.)
    """
    buf.write("\n=== SoC recon (companion IOPs + asleep domains, "
              "read-only) ===\n")
    log("SoC recon (IOPs + power domains)...")

    _pmgr, dev_by_idx = _load_pmgr_devices(buf)

    def _gate_state_str(gate):
        if dev_by_idx is None:
            return "?"
        st = _read_pmgr_gate_state(dev_by_idx, gate, buf, indent="      ")
        if st is None:
            return "?"
        if st.get("virtual"):
            return f"VIRTUAL(on={st['on']})"
        return "ON" if st["on"] else "OFF"

    # 1) Walk /arm-io for companion processors (IOP/ASC nodes).
    buf.write("  --- companion processors (/arm-io IOP/ASC nodes) ---\n")
    try:
        arm_io = u.adt["arm-io"]
        for child in arm_io:
            compat = _adt_compat_str(child)
            if not any(m in compat for m in _IOP_COMPAT_MARKERS):
                continue
            name = getattr(child, "name", "?")
            role = getattr(child, "role", None)
            try:
                cg = list(getattr(child, "clock_gates", []) or [])
            except Exception:
                cg = []
            buf.write(f"  IOP node '{name}' role={role} "
                      f"compat=[{compat}]\n")
            buf.write(f"    clock-gates={cg}\n")
            for g in cg:
                buf.write(f"    gate {g}: {_gate_state_str(g)}\n")
    except Exception as e:
        buf.write(f"  IOP walk failed: {e.__class__.__name__}: {e}\n")

    # 2) apcie power-gate posture (are the apcie / CIO domains up?).
    buf.write("  --- apcie power-gates ---\n")
    try:
        apcie_node = u.adt["arm-io/apcie"]
        gates = list(getattr(apcie_node, "power_gates", []) or [])
        buf.write(f"  apcie power-gates={gates}\n")
        for g in gates:
            buf.write(f"    gate {g}: {_gate_state_str(g)}\n")
    except Exception as e:
        buf.write(f"  apcie gate readout failed: "
                  f"{e.__class__.__name__}: {e}\n")

    buf.write("  (No IOP boot performed. If acio-cpu0 is powered but its "
              "rtkit is not running, RUN 26 = boot the ACIO IOP before "
              "pcie_init.)\n")


# ---------------------------------------------------------------- SMP start

def smp_start(buf):
    """RUN 24/25: start all M4 CPU cores. m1n1 prints 'Starting CPU N
    (die:cluster:core)...' per core on the TTY console; RUN 24 saw 9/9
    'Failed!'. This is the standard proxy op with an explicit T8132 case
    (smp.c). Low-risk, but a real state change -- kept behind --smp-start."""
    log("starting secondary CPUs (p.smp_start_secondaries())...")
    buf.write("=== SMP: p.smp_start_secondaries() ===\n")
    try_(lambda: p.smp_start_secondaries(), "smp_start_secondaries")
    buf.write("(see TTY console for 'Starting CPU N ...' lines)\n\n")


# ---------------------------------------------------------------- SMP diagnosis

def smp_diag(buf):
    """RUN 25 (read-only): diagnose WHY the secondary cores refuse to start.

    For each CPU node under /cpus, report its PMGR CPU-gate state (is the
    cluster power-gated?) so we can distinguish "cores are power-gated /
    parent-off" from "cores are powered but the spin-table entry was never
    taken". Pure ADT parse + safe PMGR PS reads -- same safety class as
    soc_recon(), zero risky MMIO.
    """
    buf.write("\n=== SMP diagnosis (why do the cores refuse?, read-only) "
              "===\n")
    log("SMP diagnosis (per-core PMGR gate state)...")

    _pmgr, dev_by_idx = _load_pmgr_devices(buf)
    if dev_by_idx is None:
        buf.write("  (no PMGR devices -- cannot read CPU gate state)\n")
        return

    # /cpus enumerates every core; each node carries its cluster/core id and,
    # where present, a clock-gates list pointing at its PMGR CPU gate.
    try:
        cpus = u.adt["cpus"]
    except Exception as e:
        buf.write(f"  cannot open /cpus: {e.__class__.__name__}: {e}\n")
        return

    for cpu in cpus:
        name = getattr(cpu, "name", "?")
        cluster = getattr(cpu, "cluster-id", getattr(cpu, "cluster_id", "?"))
        cpu_id = getattr(cpu, "cpu-id", getattr(cpu, "cpu_id", "?"))
        state = getattr(cpu, "state", None)
        try:
            cg = list(getattr(cpu, "clock_gates", []) or [])
        except Exception:
            cg = []
        buf.write(f"  cpu '{name}' cluster={cluster} cpu-id={cpu_id} "
                  f"state={state!r} clock-gates={cg}\n")
        for g in cg:
            _read_pmgr_gate_state(dev_by_idx, g, buf, indent="      ")

    # Cluster-level PMGR gates (CPUn / PSTATE) sit under pmgr too; dump any
    # device whose name hints at a CPU/cluster power domain so we can see if a
    # whole cluster is held OFF even though its per-core gates look fine.
    buf.write("  --- pmgr devices matching CPU/cluster power domains ---\n")
    try:
        for idx, dev in sorted(dev_by_idx.items()):
            nm = getattr(dev, "name", b"")
            if isinstance(nm, (bytes, bytearray)):
                nm = nm.rstrip(b"\x00").decode("ascii", "replace")
            nm = str(nm)
            low = nm.lower()
            if any(k in low for k in ("cpu", "acc", "cluster", "pcpu",
                                      "ecpu")):
                _read_pmgr_gate_state(dev_by_idx, idx, buf, indent="      ")
    except Exception as e:
        buf.write(f"  cluster-gate scan failed: "
                  f"{e.__class__.__name__}: {e}\n")

    buf.write("  (Cores power-gated / parent-off -> fix cluster power in "
              "RUN 26. Cores ON but refused -> spin-table / reset-vector "
              "path, not power.)\n")


# ---------------------------------------------------------------- ACIO status

def _acio_asc_bases(node):
    """Return candidate ASC register bases for an acio-cpuN node: the node's
    own reg first, then any '-nub' (rtbuddy-v2) child's reg. The mxwrap-acio
    node is the mailbox wrapper; the ASC CPU_CONTROL/STATUS regs may live on
    either, so we probe both."""
    bases = []
    try:
        bases.append(("self", node.get_reg(0)[0]))
    except Exception:
        pass
    try:
        for child in node:
            cc = _adt_compat_str(child)
            cn = str(getattr(child, "name", ""))
            if "nub" in cn.lower() or "rtbuddy" in cc:
                try:
                    bases.append((f"nub:{cn}", child.get_reg(0)[0]))
                except Exception:
                    continue
    except Exception:
        pass
    return bases


def acio_status(buf):
    """RUN 25 (read-only): read each acio-cpuN rtkit IOP's CPU_STATUS /
    CPU_CONTROL -- is it RUNNING, STOPPED, or held in reset? This is the read
    RUN 24 SKIPPED (it only read PMGR gates). It's the precondition for the
    RUN-26 decision "boot the ACIO IOP": if the ACIO fabric that owns the
    CIO3-PLL/phy_ip PHY is powered but STOPPED, that's strong evidence the PHY
    stalls because its owner was never started.

    We only ever READ CPU_STATUS/CPU_CONTROL -- a bare, wedge-guarded read
    cannot boot the IOP. We do NOT instantiate ASC() or send a mailbox message
    (which would spin on INBOX_CTRL.FULL against a stopped IOP).
    """
    buf.write("\n=== ACIO rtkit status (running/stopped/reset, read-only) "
              "===\n")
    log("ACIO status (CPU_STATUS/CPU_CONTROL, read-only)...")

    acio_nodes = []
    try:
        for child in u.adt["arm-io"]:
            compat = _adt_compat_str(child)
            role = str(getattr(child, "role", "") or "")
            if "mxwrap-acio" in compat or role.upper().startswith("ACIO"):
                acio_nodes.append(child)
    except Exception as e:
        buf.write(f"  ACIO node walk failed: {e.__class__.__name__}: {e}\n")
        return

    if not acio_nodes:
        buf.write("  (no acio-cpuN nodes found in /arm-io)\n")
        return

    for node in acio_nodes:
        name = getattr(node, "name", "?")
        role = getattr(node, "role", None)
        buf.write(f"  --- {name} (role={role}) ---\n")
        bases = _acio_asc_bases(node)
        if not bases:
            buf.write("    (no reg base resolvable from ADT)\n")
            continue
        for tag, base in bases:
            buf.write(f"    reg[{tag}] base=0x{base:x}\n")
            # Wedge-guarded: if the ASC block is un-clocked the read AXI-stalls;
            # short_timeout + check_alive() bail before we burn the session.
            with guarded(buf, label=f"acio-{name}-{tag}"):
                ctrl = _read32_live(base + ASC_CPU_CONTROL,
                                    f"{name}.{tag}.CPU_CONTROL", buf,
                                    alive_probe=True)
                if not check_alive():
                    buf.write("    [ABORT] m1n1 wedged reading this ASC "
                              "block; stopping ACIO status\n")
                    return
                status = _read32_live(base + ASC_CPU_STATUS,
                                      f"{name}.{tag}.CPU_STATUS", buf,
                                      alive_probe=True)
            if status is not None and status != GUARD_SENTINEL:
                running = bool(status & ASC_CPU_STATUS_RUNNING)
                stopped = bool(status & ASC_CPU_STATUS_STOPPED)
                idle = bool(status & ASC_CPU_STATUS_IDLE)
                run_bit = (ctrl is not None and ctrl != GUARD_SENTINEL
                           and bool(ctrl & ASC_CPU_CONTROL_RUN))
                verdict = ("RUNNING" if running and not stopped
                           else "STOPPED" if stopped
                           else "?")
                buf.write(f"    -> {name}.{tag}: {verdict} "
                          f"(RUNNING={running} STOPPED={stopped} IDLE={idle} "
                          f"CPU_CONTROL.RUN={run_bit})\n")

    buf.write("  (Any ACIO STOPPED/RUN=0 while its PMGR gate is powered -> "
              "RUN 26 = boot the ACIO IOP (ASC(u, base).boot()) before "
              "pcie_init. All ACIO already RUNNING -> ownership theory "
              "weakens.)\n")


# ---------------------------------------------------------------- main

def build_argparser():
    ap = argparse.ArgumentParser(
        description="SoC-first bring-up (SMP + companion-IOP recon) for the "
                    "M4 mini over m1n1. RUN 25+ replacement for the "
                    "SoC-recon path that lived in perstn.py. Read-only "
                    "unless --smp-start (which starts CPU cores).")
    ap.add_argument("--out", default="/tmp/m4-recon",
                    help="output dir for nic-runtime.txt "
                         "(default: /tmp/m4-recon)")
    ap.add_argument("--require-build", default=None,
                    help="abort unless the enrolled m1n1 USB product string "
                         "contains this substring (e.g. rc1-60-g). Runs "
                         "BEFORE any device state change (stale-binary guard).")
    ap.add_argument("--smp-start", action="store_true",
                    help="call p.smp_start_secondaries() -- start all M4 CPU "
                         "cores. The only non-read-only action here.")
    ap.add_argument("--soc-recon", action="store_true",
                    help="read-only recon of companion IOPs (esp acio-cpuN) "
                         "and asleep apcie/CIO power domains.")
    ap.add_argument("--smp-diag", action="store_true",
                    help="read-only: per-core PMGR CPU-gate state, to explain "
                         "why the secondary cores refuse to start.")
    ap.add_argument("--acio-status", action="store_true",
                    help="read-only: each acio-cpuN rtkit's CPU_STATUS/"
                         "CPU_CONTROL -- running / stopped / in reset. Does "
                         "NOT boot the IOP.")
    return ap


def main():
    ap = build_argparser()
    args = ap.parse_args()

    # Stale-binary guard, BEFORE any device state change (RUN 13 lesson).
    require_build(args.require_build)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    runtime_path = out / "nic-runtime.txt"

    buf = io.StringIO()

    def flush(tag):
        flush_partial_log(runtime_path, buf, tag)

    log("=== soc_bringup (SoC-first recon) ===")
    buf.write("=== soc_bringup: SoC-first bring-up recon ===\n\n")
    flush("start")

    # SMP start first (the one state-changing step), so smp_diag reads the
    # post-attempt gate state.
    if args.smp_start:
        smp_start(buf)
        flush("smp-start")

    if args.smp_diag:
        try_(lambda: smp_diag(buf), "smp_diag")
        flush("smp-diag")

    if args.soc_recon:
        try_(lambda: soc_recon(buf), "soc_recon")
        flush("soc-recon")

    if args.acio_status:
        try_(lambda: acio_status(buf), "acio_status")
        flush("acio-status")

    if not (args.smp_start or args.smp_diag or args.soc_recon
            or args.acio_status):
        log("no probe selected; pass --smp-start/--smp-diag/--soc-recon/"
            "--acio-status")

    flush("done")
    log(f"done; log at {runtime_path}")


if __name__ == "__main__":
    main()
