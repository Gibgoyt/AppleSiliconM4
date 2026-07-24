# RUN 25 findings — SMP is the real wall; cores are powered but never take the spin-table

**Dispatch:** `./Scripts/m1n1/perstn-run.sh 25` → `soc_bringup.sh` (the SoC-first script split out
of perstn.py this run) on m1n1 `v1.6.0-rc1-60-g` (guard OK). Flags: `--smp-start --soc-recon
--smp-diag --acio-status`. Read-only except `--smp-start`. No reflash.

## Result summary

| Probe | Outcome |
|---|---|
| `--smp-start` | **9/9 secondary cores `Failed!`** (cpu6 is the running boot P-core) |
| `--smp-diag` | refusing cores are **POWERED** (`actual=0xf`), not power-gated |
| `--soc-recon` | ACIO gates 338/339/341 **VIRTUAL/OFF**; apcie gate 151 (PHY_SW) OFF |
| `--acio-status` | **WEDGED m1n1** (SYNC on un-clocked ACIO read) — see below |

## 1. The refusing cores are ON, not power-gated (the key reframe)

`smp-diag` read every CPU PMGR gate. ECPU0-5 (gates 1-6) and PCPU0-3 (gates 7-10) all read
`actual=0xf (ON)`, `target=0x0`, `was_pwrgated` (nic-runtime.txt:19-28); their ADT `state='waiting'`.
So the 9 refusals are **not a power problem** — the cores are powered and simply never take the
spin-table. This **refutes RUN 24's "secondary cores refuse → fix cluster power first"** branch.

The `Failed!` is m1n1 `src/smp.c:181`: after m1n1 writes the CPU-start MMIO
(`cpu_start_base+0x4`, `+0x8+4*cluster`, smp.c:168/171), it polls `spin_table[index].flag` for
100 ms (smp.c:173-178) and the started core never sets it — i.e. the core never reaches m1n1's
secondary entry (`_vectors_start`).

**Prime suspect (m1n1 source, needs reflash):** `chickens.c:113-117` `features_m4` omits `cyc_ovrd`
and its per-part `init` fn is **NULL** (`chickens.c:155-156`), with a literal
`// XXX figure out what features are actually available on M4`. A released M4 core with no per-part
chicken-bit init can fault before writing its flag. (RVBAR delivery looks fine — "Starting CPU"
prints, which is *past* the RVBAR check at smp.c:149.)

## 2. ACIO fabric is un-clocked (confirms the RUN-24 ownership picture)

`soc-recon`: acio-cpu0/1/3 (roles ACIO0/1/3, `iop,mxwrap-acio`) gates 338/339/341 all
**VIRTUAL (no PS reg, flags.on=False)**. apcie gate 151 (APCIE_PHY_SW) `ps@0x380700550=0x1400024f`
→ `actual=0x4, OFF, was_clkgated`. The ACIO IOP that the CIO3-PLL/phy_ip theory says owns the PCIe
PHY is powered-off and un-booted — consistent, but note it can't be booted until its gate is
powered, which the AP may not own.

## 3. `--acio-status` wedged m1n1 — a real bug, now fixed

`acio_status` tried to read acio-cpu0's ASC `CPU_CONTROL` at `0x401108044` directly, wrapped only in
`guarded()`. But **`guarded()` cannot catch an AXI stall** (its own docstring, m4_common.py:126-132:
"GUARD.SKIP does NOT help against AXI bus stalls… the M4 CPU stalls forever on the load"). The ACIO
block is un-clocked (gates virtual/off), so the read AXI-stalled: `Exception: SYNC` → UART timeout →
m1n1 DEAD (run.log:146-150). The earlier sections had already flushed (start=47, smp-start=136,
smp-diag=3469, soc-recon=6817 bytes), so the recon data survived.

**Fix (this commit):** `acio_status` now reads each ACIO node's PMGR gate state FIRST and, if no gate
is ACTIVE, reports "un-clocked → NOT reading MMIO" and skips the block entirely — mirroring
perstn.py Phase-A's "not ACTIVE → skip pre-PMGR MMIO" discipline (perstn.py:933-941). It can no
longer wedge on this state.

(Unrelated: the DCP rtkit crashed during boot with `ASSERT!Messenger.c:299` — a display-firmware
issue that predates the probes and does not explain the SMP wall.)

## RUN 26 plan (read-only, no reflash)

`./Scripts/m1n1/perstn-run.sh 26` — `--smp-start --smp-diag --smp-probe --soc-recon`
(`--acio-status` dropped from the default; the fix makes it safe but it now only re-confirms
"ACIO un-clocked"). The new **`--smp-probe`** harvests, read-only, the evidence the SMP fix needs:
per-CPU RVBAR (`cpu-impl-reg[0]`) value + `RVBAR_LOCK` bit + the CPU-start block words
(`pmgr reg[0] + 0x34000`, + `die*0x2000000000`), mirroring smp.c's address math.

**Decides the SMP fix** (a separate m1n1-source + reflash task):
- RVBARs sane + unlocked + enables latched, yet flag never set → core faults after release, before
  `_vectors_start` → the M4 per-part `init`/chicken gap in `chickens.c` is the fix.
- die-1 RVBAR wrong/zero or CPU-start writes not landing → address-math bug in smp.c for t8132.

(Note: all cores in this j773g enumerate as **die 0** — `reg` for cpu6 = 0x100 → die0/cluster1;
the AIC "1/2 dies" log means the chip *supports* two dies, not that this board populates them. So a
die-offset bug is unlikely, but `--smp-probe` confirms it directly.)
