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
import time

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
# We only ever READ these. NOTE: a bare read does NOT bail on an un-clocked
# block -- guarded() cannot catch an AXI stall (see m4_common.guarded's
# docstring). The ONLY safe protection is to check the block's PMGR gate is
# ACTIVE first and refuse the MMIO otherwise (RUN 25 wedged m1n1 by skipping
# that check). See acio_status().
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


# ---------------------------------------------------------------- SMP probe

# Constants from m1n1/src/smp.c + pmgr.h -- keep in lockstep so the Python read
# matches the C address derivation exactly.
CPU_START_OFF_T8112 = 0x34000        # smp.c:21; used for T8132 (smp.c:289-291)
PMGR_DIE_OFFSET     = 0x2000000000   # pmgr.h:8
RVBAR_LOCK          = 1 << 0         # smp.c:29
RVBAR_ADDR_MASK     = 0x0000FFFFFFFFF000  # GENMASK(47,12), smp.c:30
CPU_REG_CORE_MASK    = 0x00FF        # GENMASK(7,0),  smp.c:25
CPU_REG_CLUSTER_MASK = 0x0700        # GENMASK(10,8), smp.c:26
CPU_REG_DIE_MASK     = 0x7800        # GENMASK(14,11),smp.c:27


def _read64_guarded(addr, label, buf):
    """Read a 64-bit register through the exception guard, logging like
    _read32_live. p.read64 is exposed by the proxy (proxy.py:808). Returns the
    value or None. Only call on blocks known to be live (pmgr / CPU IMPL)."""
    log(f"    read64(0x{addr:x}) [{label}]...")
    try:
        v = p.read64(addr)
    except Exception as e:
        msg = f"{e.__class__.__name__}: {e}"
        buf.write(f"  {label:28s} @ 0x{addr:x} = <{msg}>\n")
        log(f"      -> FAILED: {msg}")
        return None
    buf.write(f"  {label:28s} @ 0x{addr:x} = 0x{v:016x}\n")
    log(f"      -> 0x{v:016x}")
    return v


def smp_probe(buf):
    """RUN 26 (read-only): harvest the RVBAR / spin-table / CPU-start evidence
    the RUN-25 'cores powered but Failed!' finding points to. Mirrors m1n1's
    own address derivation in src/smp.c so the reads line up with what the C
    does when it gives up at smp.c:181.

    For each /cpus node: decode die/cluster/core from 'reg' (smp.c:25-27),
    read its RVBAR (cpu-impl-reg[0]) -- value, RVBAR_LOCK bit, masked address
    (smp.c:149/381 check these) -- and the impl+0x100 status word (smp.c:223).
    Then dump the CPU-start block words (pmgr reg[0] + 0x34000, + die offset).

    All targets (pmgr, per-CPU IMPL windows) are live blocks m1n1 itself reads,
    so this is wedge-safe; reads are still guarded + liveness-checked.
    """
    buf.write("\n=== SMP probe (RVBAR / CPU-start evidence, read-only) ===\n")
    log("SMP probe (RVBAR + CPU-start block)...")

    # CPU-start base = pmgr reg[0] + CPU_START_OFF_T8112 (T8132 case).
    try:
        pmgr_reg = u.adt["arm-io/pmgr"].get_reg(0)[0]
        cpu_start_base0 = pmgr_reg + CPU_START_OFF_T8112
        buf.write(f"  pmgr reg[0] = 0x{pmgr_reg:x}; CPU-start base (die0) = "
                  f"0x{cpu_start_base0:x} (+0x{CPU_START_OFF_T8112:x})\n")
    except Exception as e:
        buf.write(f"  cannot resolve pmgr reg / CPU-start base: "
                  f"{e.__class__.__name__}: {e}\n")
        cpu_start_base0 = None

    # Per-CPU RVBAR posture.
    buf.write("  --- per-CPU RVBAR (cpu-impl-reg[0]) + status ---\n")
    rvbars = {}          # cpu-id -> masked RVBAR addr (for die0/die1 compare)
    dies_seen = set()
    try:
        cpus = u.adt["cpus"]
    except Exception as e:
        buf.write(f"  cannot open /cpus: {e.__class__.__name__}: {e}\n")
        cpus = []

    for cpu in cpus:
        name = getattr(cpu, "name", "?")
        # Use .getprop() with the literal ADT names -- unambiguous vs the
        # underscore/hyphen normalization ADTNode.__getattr__ does.
        reg = cpu.getprop("reg", None)
        if reg is None:
            buf.write(f"  cpu '{name}': no 'reg' property, skipping\n")
            continue
        reg = int(reg)
        core = reg & CPU_REG_CORE_MASK
        cluster = (reg & CPU_REG_CLUSTER_MASK) >> 8
        die = (reg & CPU_REG_DIE_MASK) >> 11
        dies_seen.add(die)
        state = cpu.getprop("state", None)
        try:
            impl = list(cpu.getprop("cpu-impl-reg", []) or [])
        except Exception:
            impl = []
        buf.write(f"  cpu '{name}' reg=0x{reg:x} die={die} cluster={cluster} "
                  f"core={core} state={state!r}\n")
        if not impl:
            buf.write("      (no cpu-impl-reg; cannot read RVBAR)\n")
            continue
        impl_base = int(impl[0])
        with guarded(buf, label=f"rvbar-{name}"):
            rv = _read64_guarded(impl_base, f"{name}.RVBAR", buf)
            if not check_alive():
                buf.write("      [ABORT] m1n1 wedged reading RVBAR; stopping\n")
                return
            _read64_guarded(impl_base + 0x100, f"{name}.impl+0x100", buf)
        if rv is not None:
            locked = bool(rv & RVBAR_LOCK)
            addr = rv & RVBAR_ADDR_MASK
            rvbars[core if die == 0 else (die, cluster, core)] = addr
            buf.write(f"      -> {name}: RVBAR addr=0x{addr:x} "
                      f"LOCK={locked}\n")

    # die0-vs-die1 comparison + lock summary.
    buf.write("  --- RVBAR cross-check ---\n")
    buf.write(f"  dies seen in /cpus: {sorted(dies_seen)}\n")
    uniq = sorted(set(rvbars.values()))
    buf.write(f"  distinct RVBAR target addresses: "
              f"{[hex(a) for a in uniq]}\n")
    if len(uniq) <= 1:
        buf.write("  -> all cores point at the SAME RVBAR entry (expected: "
                  "m1n1's _vectors_start). RVBAR delivery looks correct.\n")
    else:
        buf.write("  -> cores point at DIFFERENT RVBAR entries -- a die/cluster "
                  "cpu-impl-reg derivation problem is possible.\n")

    # CPU-start block words (read-only): are prior --smp-start enables latched?
    if cpu_start_base0 is not None:
        buf.write("  --- CPU-start block words (read-only) ---\n")
        for die in sorted(dies_seen):
            base = cpu_start_base0 + die * PMGR_DIE_OFFSET
            buf.write(f"  die {die}: CPU-start base 0x{base:x}\n")
            with guarded(buf, label=f"cpustart-die{die}"):
                _read32_live(base + 0x0, f"die{die}.cpustart+0x0", buf)
                if not check_alive():
                    buf.write("      [ABORT] m1n1 wedged reading CPU-start "
                              "block; stopping\n")
                    return
                _read32_live(base + 0x4, f"die{die}.cpustart+0x4", buf)
                # +0x8 + 4*cluster is the per-cluster start word; dump a couple.
                for cl in range(2):
                    _read32_live(base + 0x8 + 4 * cl,
                                 f"die{die}.cpustart+0x8+4*{cl}", buf)

    buf.write("  (RVBARs sane+unlocked & enables latched but flag never set -> "
              "core faults after release, before _vectors_start -> M4 per-part "
              "init/chicken gap in m1n1 src (chickens.c features_m4). Wrong/"
              "missing die-1 RVBAR or unlatched enables -> address-math bug.)\n")


# ---------------------------------------------------------------- SMP release probe

def smp_release_probe(buf, target_reg=0x1):
    """RUN 27 (WRITES MMIO): manually replicate m1n1's smp_start_cpu (smp.c:
    149-183) for ONE secondary core, from Python, to test the RVBAR-lock
    hypothesis (H1) with NO reflash.

    RUN 26 showed every core's RVBAR is correct (== _vectors_start) but LOCKED,
    and the UART never printed 'RVBAR entry on secondary CPU' -- the cores never
    left reset. m1n1 only re-writes RVBAR (which also clears RVBAR_LOCK) inside
    `if (cpu_features->cyc_ovrd)` (smp.c:161), and features_m4 omits cyc_ovrd,
    so on M4 that unlock-write is skipped. Here we do it by hand: clear the lock
    (write64 _vectors_start into RVBAR), then strobe the CPU-start register, and
    watch for the 'RVBAR entry on secondary CPU' UART marker.

    WRITES: only this one core's RVBAR (cpu-impl-reg[0]) + the CPU-start enable/
    start words -- the identical registers smp_start_cpu writes. No proxy
    spin_table bookkeeping is wired, so SUCCESS = the UART marker, not a proxy
    return. Worst case the core still doesn't start (same as the normal path).
    """
    buf.write("\n=== SMP release probe (manual smp_start_cpu, WRITES MMIO) "
              "===\n")
    log("SMP release probe (manual core release, writes RVBAR + CPU-start)...")

    # CPU-start base = pmgr reg[0] + CPU_START_OFF_T8112 (same as smp_probe).
    try:
        pmgr_reg = u.adt["arm-io/pmgr"].get_reg(0)[0]
    except Exception as e:
        buf.write(f"  cannot resolve pmgr reg[0]: {e.__class__.__name__}: {e}\n")
        return
    cpu_start_base0 = pmgr_reg + CPU_START_OFF_T8112

    # Find the target /cpus node by its 'reg' value.
    node = None
    try:
        for cpu in u.adt["cpus"]:
            r = cpu.getprop("reg", None)
            if r is not None and int(r) == target_reg:
                node = cpu
                break
    except Exception as e:
        buf.write(f"  cannot walk /cpus: {e.__class__.__name__}: {e}\n")
        return
    if node is None:
        buf.write(f"  no /cpus node with reg=0x{target_reg:x}; aborting\n")
        return

    name = getattr(node, "name", "?")
    reg = int(node.getprop("reg"))
    core = reg & CPU_REG_CORE_MASK
    cluster = (reg & CPU_REG_CLUSTER_MASK) >> 8
    die = (reg & CPU_REG_DIE_MASK) >> 11
    state = node.getprop("state", None)
    try:
        impl_arr = list(node.getprop("cpu-impl-reg", []) or [])
    except Exception:
        impl_arr = []
    if not impl_arr:
        buf.write(f"  cpu '{name}': no cpu-impl-reg; cannot release\n")
        return
    impl = int(impl_arr[0])
    cpu_start_base = cpu_start_base0 + die * PMGR_DIE_OFFSET

    buf.write(f"  target cpu '{name}' reg=0x{reg:x} die={die} cluster={cluster} "
              f"core={core} state={state!r}\n")
    buf.write(f"  impl(RVBAR)=0x{impl:x}  CPU-start base=0x{cpu_start_base:x}\n")

    if str(state) == "running":
        buf.write("  REFUSING: target is the running boot core; pick a "
                  "'waiting' core via --release-core\n")
        return

    exc0 = 0
    try:
        exc0 = p.get_exc_count()
    except Exception:
        pass

    # 1) Pre-state (read-only).
    try:
        rv_pre = p.read64(impl)
    except Exception as e:
        buf.write(f"  RVBAR pre-read FAILED: {e.__class__.__name__}: {e}; "
                  f"aborting\n")
        return
    locked_pre = bool(rv_pre & RVBAR_LOCK)
    vectors = rv_pre & RVBAR_ADDR_MASK
    buf.write(f"  [pre] RVBAR=0x{rv_pre:016x} (addr=0x{vectors:x}, "
              f"LOCK={locked_pre})\n")
    try:
        en_pre = p.read32(cpu_start_base + 0x4)
        st_pre = p.read32(cpu_start_base + 0x8 + 4 * cluster)
        buf.write(f"  [pre] cpustart+0x4=0x{en_pre:08x}  "
                  f"cpustart+0x8+4*{cluster}=0x{st_pre:08x}\n")
    except Exception as e:
        buf.write(f"  [pre] CPU-start read FAILED: "
                  f"{e.__class__.__name__}: {e}\n")

    # 2) Clear LOCK / re-arm RVBAR (smp.c:161). Writing _vectors_start back also
    #    clears RVBAR_LOCK on Apple cores.
    buf.write(f"  [write] RVBAR <- 0x{vectors:x} (clears LOCK, re-arms vector)\n")
    log(f"    write64(0x{impl:x}, 0x{vectors:x}) [{name}.RVBAR unlock]...")
    try:
        p.write64(impl, vectors)
    except Exception as e:
        buf.write(f"  RVBAR write FAILED: {e.__class__.__name__}: {e}; "
                  f"aborting\n")
        return
    if not check_alive():
        buf.write("  [ABORT] m1n1 wedged after RVBAR write; stopping\n")
        return
    rv_post = p.read64(impl)
    locked_post = bool(rv_post & RVBAR_LOCK)
    buf.write(f"  [post] RVBAR=0x{rv_post:016x} (LOCK={locked_post})\n")
    if locked_post:
        buf.write("  -> LOCK STAYED SET after write -> RVBAR lock is sticky-"
                  "until-reset; m1n1's in-line write64 can't clear it either. "
                  "H1's fix must avoid needing to clear it. NOT strobing "
                  "start.\n")
        return
    buf.write("  -> LOCK CLEARED. Proceeding to CPU-start strobe.\n")

    # 3) Enable + start strobes (smp.c:168, smp.c:171).
    en_val = 1 << (4 * cluster + core)
    st_val = 1 << core
    buf.write(f"  [write] cpustart+0x4 <- 0x{en_val:x} (enable, smp.c:168)\n")
    log(f"    write32(0x{cpu_start_base + 0x4:x}, 0x{en_val:x}) [enable]...")
    try:
        p.write32(cpu_start_base + 0x4, en_val)
        if not check_alive():
            buf.write("  [ABORT] m1n1 wedged after enable write; stopping\n")
            return
        buf.write(f"  [write] cpustart+0x8+4*{cluster} <- 0x{st_val:x} "
                  f"(start, smp.c:171)\n")
        log(f"    write32(0x{cpu_start_base + 0x8 + 4 * cluster:x}, "
            f"0x{st_val:x}) [start]...")
        p.write32(cpu_start_base + 0x8 + 4 * cluster, st_val)
    except Exception as e:
        buf.write(f"  CPU-start write FAILED: {e.__class__.__name__}: {e}\n")
        return
    if not check_alive():
        buf.write("  [ABORT] m1n1 wedged after start write; stopping\n")
        return

    # 4) Observe. The core, if released, prints 'RVBAR entry on secondary CPU'
    #    from _cpu_reset_c (startup.c:222) on the TTY console. We can't read
    #    m1n1's spin_table flag, so the UART marker is the success signal.
    buf.write("  [observe] started strobe issued. WATCH THE TTY> CONSOLE for "
              "'RVBAR entry on secondary CPU' (the _cpu_reset_c marker) --\n"
              "            that string appearing == the core LEFT RESET == H1 "
              "CONFIRMED.\n")
    log("    strobe issued; watch TTY for 'RVBAR entry on secondary CPU'...")
    # Give the core a moment; keep the proxy alive-check cheap.
    for _ in range(10):
        time.sleep(0.05)
        if not check_alive():
            buf.write("  [ABORT] m1n1 stopped responding during observe "
                      "window\n")
            return
    try:
        exc1 = p.get_exc_count()
        buf.write(f"  [observe] boot-core exc_count delta = {exc1 - exc0} "
                  f"(0 = boot core did not fault)\n")
    except Exception:
        pass

    buf.write("  (Marker on TTY -> H1 CONFIRMED: locked-un-rearmed RVBAR was "
              "the blocker -> RUN 28 = minimal m1n1-source fix (unconditional "
              "RVBAR re-write in smp_start_cpu, or rvbar_rewrite flag on "
              "features_m4) + reflash. LOCK cleared but NO marker -> start-"
              "register layout (H3). LOCK never cleared -> sticky lock, rethink."
              ")\n")


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


def _node_gate_posture(node, dev_by_idx, buf):
    """Return (has_active_gate, summary_str) for an ACIO node by reading each
    of its clock_gates' PMGR PS state. has_active_gate is True only if at least
    one real (non-virtual) gate reads ACTIVE (actual==0xf). A node with no
    gates, or only virtual/OFF gates, is un-clocked -- its MMIO would AXI-stall
    and wedge m1n1, so callers MUST NOT read it."""
    try:
        gates = list(getattr(node, "clock_gates", []) or [])
    except Exception:
        gates = []
    if not gates:
        return False, "no clock-gates"
    if dev_by_idx is None:
        return False, "PMGR devices unavailable"
    parts = []
    active = False
    for g in gates:
        st = _read_pmgr_gate_state(dev_by_idx, g, buf, indent="      ")
        if st is None:
            parts.append(f"gate {g}: ?")
            continue
        if st.get("virtual"):
            parts.append(f"gate {g}: VIRTUAL(on={st['on']})")
        elif st.get("on"):
            parts.append(f"gate {g}: ON")
            active = True
        else:
            parts.append(f"gate {g}: OFF(actual=0x{st.get('actual'):x})")
    return active, "; ".join(parts)


def acio_status(buf):
    """RUN 25 (read-only): read each acio-cpuN rtkit IOP's CPU_STATUS /
    CPU_CONTROL -- is it RUNNING, STOPPED, or held in reset? This is the read
    RUN 24 SKIPPED (it only read PMGR gates). It's the precondition for the
    RUN-26 decision "boot the ACIO IOP": if the ACIO fabric that owns the
    CIO3-PLL/phy_ip PHY is powered but STOPPED, that's strong evidence the PHY
    stalls because its owner was never started.

    We only ever READ CPU_STATUS/CPU_CONTROL -- we do NOT instantiate ASC() or
    send a mailbox message (which would spin on INBOX_CTRL.FULL against a
    stopped IOP).

    CRITICAL SAFETY (RUN 25 lesson): before touching ANY ACIO MMIO we check the
    node's PMGR gate. guarded() does NOT protect against an AXI stall on an
    un-clocked block -- RUN 25 read acio-cpu0's CPU_CONTROL while its gate was
    VIRTUAL/OFF and hard-wedged m1n1 (Exception: SYNC -> UART timeout -> dead).
    So if a node has no ACTIVE gate we report it and SKIP the MMIO entirely.
    This mirrors perstn.py Phase-A's "not ACTIVE -> skip pre-PMGR MMIO" rule.
    """
    buf.write("\n=== ACIO rtkit status (running/stopped/reset, read-only) "
              "===\n")
    log("ACIO status (gate-checked CPU_STATUS/CPU_CONTROL, read-only)...")

    _pmgr, dev_by_idx = _load_pmgr_devices(buf)

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

        # GATE CHECK FIRST -- never read MMIO of an un-clocked block.
        active, posture = _node_gate_posture(node, dev_by_idx, buf)
        buf.write(f"    gate posture: {posture}\n")
        if not active:
            buf.write(f"    -> {name}: NO ACTIVE gate -> ASC block un-clocked; "
                      f"NOT reading MMIO (would AXI-stall/wedge m1n1). "
                      f"IOP not booted.\n")
            continue

        bases = _acio_asc_bases(node)
        if not bases:
            buf.write("    (no reg base resolvable from ADT)\n")
            continue
        for tag, base in bases:
            buf.write(f"    reg[{tag}] base=0x{base:x} (gate ACTIVE, read OK)\n")
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

    buf.write("  (All ACIO gates VIRTUAL/OFF -> the IOP that owns the "
              "CIO3-PLL/phy_ip PHY is un-clocked and un-booted -> booting it "
              "requires powering its gate first, which the AP may not own. "
              "If a gate ever reads ACTIVE and the IOP is STOPPED/RUN=0 -> "
              "boot it via ASC(u, base).boot() before pcie_init.)\n")


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
    ap.add_argument("--smp-probe", action="store_true",
                    help="read-only: per-CPU RVBAR (cpu-impl-reg[0]) + LOCK "
                         "bit + CPU-start block words, mirroring m1n1 smp.c's "
                         "address derivation. Evidence for the 'cores powered "
                         "but Failed!' spin-table lead.")
    ap.add_argument("--smp-release-probe", action="store_true",
                    help="WRITES MMIO: manually release ONE secondary core "
                         "(replicates smp_start_cpu) -- clears its RVBAR_LOCK "
                         "and strobes CPU-start. Tests the RVBAR-lock "
                         "hypothesis with no reflash. Watch TTY for 'RVBAR "
                         "entry on secondary CPU'.")
    ap.add_argument("--release-core", default="0x1",
                    help="target /cpus 'reg' value for --smp-release-probe "
                         "(hex, default 0x1 = die0/cluster0/core1).")
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

    if args.smp_probe:
        try_(lambda: smp_probe(buf), "smp_probe")
        flush("smp-probe")

    if args.smp_release_probe:
        try:
            target = int(args.release_core, 0)
        except ValueError:
            target = 0x1
            log(f"bad --release-core={args.release_core!r}; using 0x1")
        try_(lambda: smp_release_probe(buf, target_reg=target),
             "smp_release_probe")
        flush("smp-release-probe")

    if args.soc_recon:
        try_(lambda: soc_recon(buf), "soc_recon")
        flush("soc-recon")

    if args.acio_status:
        try_(lambda: acio_status(buf), "acio_status")
        flush("acio-status")

    if not (args.smp_start or args.smp_diag or args.smp_probe
            or args.smp_release_probe or args.soc_recon or args.acio_status):
        log("no probe selected; pass --smp-start/--smp-diag/--smp-probe/"
            "--smp-release-probe/--soc-recon/--acio-status")

    flush("done")
    log(f"done; log at {runtime_path}")


if __name__ == "__main__":
    main()
