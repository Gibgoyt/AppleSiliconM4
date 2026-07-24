# Plan — Fix the `acio_status` wedge + re-orient RUN 26 recon to the spin-table lead

## Context

RUN 25 (`perstn-run.sh 25` → `soc_bringup.py`, on t8132 / j773g M4 mini) ran four probes and
returned decisive data. Logs: `Scripts/m1n1/logs/25/{run.log,nic-runtime.txt}`.

**What RUN 25 established:**
1. **`--smp-start`: 9/9 secondary cores `Failed!`** (cpu6 is the running boot P-core). The
   "Failed!" is `m1n1/src/smp.c:181` — after m1n1 writes the CPU-start MMIO, the core never sets
   `spin_table[index].flag` within the 100 ms poll (`smp.c:173-181`).
2. **`--smp-diag`: the refusing cores are POWERED, not power-gated.** ECPU0-5 (gates 1-6) and
   PCPU0-3 (gates 7-10) all read `actual=0xf (ON)`, `target=0x0`, `was_pwrgated`
   (nic-runtime.txt:19-28). Their ADT `state='waiting'`. So the blocker is a **reset-vector /
   spin-table handoff** problem, **not** power. This *refutes* RUN 24's "fix cluster power first".
3. **`--soc-recon`:** ACIO gates 338/339/341 are **VIRTUAL (no PS reg, `flags.on=False`)**; apcie
   gate 151 (APCIE_PHY_SW) `actual=0x4, OFF, was_clkgated`. (Confirms RUN 24.)
4. **`--acio-status`: WEDGED m1n1.** `read32(0x401108044)` (acio-cpu0 CPU_CONTROL) →
   `Exception: SYNC` → UART timeout → dead (run.log:146-150).

**Root cause of the wedge (a real bug I introduced):** `acio_status` read the ACIO ASC MMIO
window directly, wrapped only in `guarded()`. But `guarded()` **cannot catch AXI stalls** — its
own docstring says so (`m4_common.py:126-132`: "GUARD.SKIP does NOT help against AXI bus stalls…
the M4 CPU stalls forever on the load"). The ACIO block is un-clocked (its gates are virtual/off),
so touching it AXI-stalled. The codebase already has the correct discipline for this — Phase-A
(`perstn.py:863-941`, `probe_phaseA_preinit_single`) reads PMGR gate state first and, if a gate is
not ACTIVE, **refuses the MMIO entirely** ("ANY read into their MMIO would SYNC-abort and wedge
m1n1… Skipping pre-PMGR MMIO"). `acio_status` violated that discipline.

**Why the real SMP fix is out of scope for this run:** the fix lives in m1n1 C source
(`m1n1/src/smp.c` / `chickens.c`), which is the sibling `/home/ahmed/Projects/C/embedded/m1n1`
tree, not the AppleSiliconM4 project. Notably `chickens.c:113-117` `features_m4` omits `cyc_ovrd`
and its per-part `init` fn is **NULL** (`chickens.c:155-156`), with a literal
`// XXX figure out what features are actually available on M4`. That's a firmware-port gap, a
much larger effort, and requires a reflash. **This run stays read-only, no reflash**, and gathers
the exact evidence that SMP fix will need.

**This change (two parts):**
- **(A) Fix the `acio_status` wedge** so `soc_bringup.py` is safe to re-run — gate every ACIO MMIO
  read on PMGR state, reusing the established Phase-A pattern. Since the ACIO gates are virtual/off,
  it will now correctly report "un-clocked → refusing MMIO" instead of wedging.
- **(B) Add a read-only `smp_probe` and a RUN 26 arm** that harvests the RVBAR / spin-table /
  CPU-start evidence the logs point to — die0-vs-die1 `cpu-impl-reg` RVBAR values and the CPU-start
  block — so the next (reflashing) m1n1-source step is well-targeted.

## Approach

### 1. Fix `acio_status` to gate MMIO on PMGR state — `Scripts/m1n1/soc_bringup.py`

In `acio_status()` (soc_bringup.py:222-292), **before** resolving/reading any ASC base:
- For each `acio-cpuN` node, read its `clock_gates` PMGR state via the existing
  `_load_pmgr_devices` / `_read_pmgr_gate_state` (already imported from `m4_common`).
- If the node has **no real ON gate** (all virtual, or `actual != 0xf`), write
  `"acio-cpuN: gates VIRTUAL/OFF → ASC block un-clocked; NOT reading MMIO (would AXI-stall/wedge)"`
  and `continue` — **never touch the MMIO**. This is exactly the RUN-25 state, so the probe becomes
  safe and still reports the finding (ACIO un-clocked ⇒ owner not up).
- Only if a gate reads ACTIVE do we fall through to the CPU_STATUS/CPU_CONTROL reads (keep that
  path for a future run where the ACIO has been powered). Even then, additionally raise the
  `guarded(..., short_timeout=…)` robustness: the current inline comment falsely claims the guarded
  read "bails before we burn the session" — correct the comment to state the truth (AXI stalls are
  NOT recoverable; the gate check is the only real protection).

Delete the misleading `# Wedge-guarded: …` comment at soc_bringup.py:263-264 and the
`_acio_asc_bases`-then-read flow's implicit assumption of safety.

### 2. New read-only `smp_probe(buf)` — `Scripts/m1n1/soc_bringup.py`

The RUN-26 evidence-gatherer. Pure ADT parse + safe MMIO reads of blocks we KNOW are live (pmgr is
always mapped; the per-CPU IMPL/RVBAR regs are readable — m1n1 itself reads them at `smp.c:149`).
Mirrors how `smp.c:237-396` derives its addresses, so the Python read matches the C exactly:
- Resolve `pmgr_reg` = `/arm-io/pmgr` reg[0]; compute the **CPU-start base** =
  `pmgr_reg + 0x34000` (CPU_START_OFF_T8112, used for T8132 per `smp.c:21,289-291`), plus
  `die * 0x2000000000` (PMGR_DIE_OFFSET, `pmgr.h:8`) for die-1 cores.
- For **each** `/cpus` node: read its `reg` (decode die/cluster/core via the `smp.c:25-27`
  GENMASKs), and its `cpu-impl-reg[0]` (RVBAR). Read `read64(impl) & RVBAR_ADDR` (mask
  `GENMASK(47,12)`) and the `RVBAR_LOCK` bit (bit 0), and compare against… we can't know
  `_vectors_start` from Python, but we CAN report **whether die-0 and die-1 cores' RVBARs differ**
  and whether any are LOCKED — the two failure modes `smp.c:149`/`smp.c:381` check. Read
  `impl + 0x100` (the status word `smp_stop` polls at `smp.c:223`) for each core.
- Dump the CPU-start block words (`cpu_start_base + 0x0/0x4/0x8+4*cluster`) per die — read-only —
  to see whether the enable bits from a prior `--smp-start` are still latched.
- All reads guarded + liveness-checked (`_read32_live`/`_read64` via `guarded`); pmgr and the CPU
  IMPL windows are live so no wedge risk, but keep the belt-and-suspenders.

Add a `--smp-probe` flag (read-only) and wire it into `main()` alongside the existing probes.

> `p.read64` is already exposed (`proxy.py:808`), so read RVBAR with it directly (wrapped in a
> `guarded` block + liveness check); no new `m4_common` primitives are needed.

### 3. RUN 26 dispatcher arm — `Scripts/m1n1/perstn-run.sh`

Add a `26)` arm (after `25)`), `RUNNER=soc_bringup.sh`. **Drop `--acio-status`** from the default
(the fix makes it safe, but it now only re-confirms "ACIO un-clocked"; keep it available manually).
Flags: `--smp-start --smp-diag --smp-probe --soc-recon --require-build=rc1-60-g`. Add `26` to the
two usage strings. (Leave RUN 25's arm as the historical record.)

### 4. Record findings — `Scripts/m1n1/logs/25/findings.md`

Write the RUN 25 outcome (cores powered-but-not-taking-spin-table; ACIO un-clocked; the
`acio_status` wedge and its fix) so the log series stays self-documenting. This also clears part of
the long-standing "findings.md still owed" follow-up.

## Critical files

- **`Scripts/m1n1/soc_bringup.py`** — gate `acio_status` MMIO on PMGR state (the wedge fix); new
  `smp_probe(buf)` + `--smp-probe` flag; wire into `main()`. Reuse `_load_pmgr_devices`,
  `_read_pmgr_gate_state`, `_read32_live`, `guarded` (all already imported from `m4_common`).
- **`Scripts/m1n1/perstn-run.sh`** — new `26)` arm + usage strings.
- **`Scripts/m1n1/logs/25/findings.md`** — new; RUN 25 outcome + RUN 26 direction.
- Reference only (the eventual SMP fix, NOT this run — different repo, needs reflash):
  `m1n1/src/smp.c:136-187` (smp_start_cpu, the "Failed!" poll), `smp.c:237-396` (address
  derivation), `m1n1/src/chickens.c:113-117,155-156` (M4 `features_m4` missing `cyc_ovrd`, NULL
  init), `m1n1/proxyclient/m1n1/proxy.py:974` (smp_start_secondaries returns nothing).

## Verification

Real hardware (M4 mini + m1n1 over UART); the **user** runs the live steps. No reflash (rc1-60-g).

1. **Static (safe here):**
   - `python3 -c "import ast; ast.parse(open('Scripts/m1n1/soc_bringup.py').read())"`
   - `bash -n Scripts/m1n1/perstn-run.sh`
   - Confirm `perstn-run.sh 26` routes to `soc_bringup.sh` with the expected flags, and that the
     `--smp-probe`/`--acio-status` args parse (extract `build_argparser` and `parse_args`, as done
     for RUN 25).
2. **Live:** `./Scripts/m1n1/perstn-run.sh 26`.
3. **Read** `/tmp/m4-recon/nic-runtime.txt` + the `TTY>` console, keying on:
   - **`--acio-status` no longer wedges** — it prints "ACIO gates VIRTUAL/OFF → not reading MMIO"
     and the run **completes** (reaches `[flush:done]` with all sections, m1n1 still alive). This is
     the primary regression check for the bug fix.
   - **`--smp-probe`**: per-core RVBAR values — do die-0 and die-1 cores' RVBARs match each other
     and look sane (non-zero, unlocked)? Any core with `RVBAR_LOCK` set? Do the CPU-start block
     words show the enable bits latched after `--smp-start`? This is the evidence the m1n1-source
     SMP fix needs.
   - exc_count deltas stay 0; the whole run is wedge-free.
4. **Outcome → the SMP fix (record in `logs/26/findings.md`):**
   - RVBARs sane + unlocked + enable bits latched, yet flag never set → the core faults *after*
     release, before `_vectors_start` → the M4 per-part `init`/chicken gap (`chickens.c`) is the
     prime suspect → next step is an m1n1-source change (separate, reflashing task).
   - die-1 RVBARs wrong/zero or CPU-start writes not landing on die 1 → the die-offset / cpu-impl-reg
     derivation is off for t8132 → fix the address math in the m1n1 source.

## Non-goals for this run

No m1n1-source (`smp.c`/`chickens.c`) edits, no reflash, no IOP boot, no `pcie_init`, no phy_ip
touch, no writes to CPU-start/RVBAR (read-only probe only). The split from the prior task
(`m4_common.py`, the `soc_bringup.{sh,py}` scaffold) is already done and unchanged here.
