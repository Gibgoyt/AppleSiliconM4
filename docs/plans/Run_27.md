# Plan — RUN 27: manual secondary-core release probe (no-reflash test of the RVBAR-lock hypothesis)

## Context

The M4 (t8132 / j773g) SMP wall is now diagnosed to a single hypothesis, and there's a decisive test
that needs **no m1n1 rebuild and no reflash**.

**What RUN 26 established** (logs: `Scripts/m1n1/logs/26/{run.log,nic-runtime.txt}`; the run
completed cleanly on `/dev/ttyACM1` — the port auto-detect + `acio_status` gate-check fixes both
worked):
- The 9 refusing cores are **powered** (PMGR `actual=0xf`) — not a power problem.
- **RVBAR (reset vector) is correct**: every core's `cpu-impl-reg[0]` reads masked addr
  **`0x100021ec000` = m1n1's `_vectors_start`**, identical across both clusters (single die). This
  rules out the address-math / die-offset hypothesis.
- **`RVBAR_LOCK` (bit 0) is SET on every core.**
- **The UART shows NO `"RVBAR entry on secondary CPU"` marker** (`_cpu_reset_c`, m1n1
  `src/startup.c:222`). So the released cores **never reach the reset vector at all** — they die
  before the first instruction of m1n1's secondary path.

**The mechanism (verified in m1n1 source):** `smp_start_cpu` only re-writes RVBAR inside
`if (cpu_features->cyc_ovrd)` (`smp.c:159-162`), and that write "also clears RVBAR_LOCK". But
`features_m4` (`chickens.c:113-117`) **omits `cyc_ovrd`** (and its per-part `init` is NULL,
`chickens.c:155-156`, with a literal `// XXX figure out what features are actually available on M4`).
Every known-good Apple part (M1/M2/M3) has `cyc_ovrd=true`, so m1n1 *always* re-writes RVBAR before
strobing start on working hardware — M4 is the first part that skips it. Combined with the
LOCK-set + no-vector-entry evidence, the leading hypothesis (**H1**) is: *the cores are strobed
against a locked RVBAR that m1n1 never re-armed, so they never leave reset.*

**Why this run:** the Plan analysis found we can test H1 **entirely from Python, no reflash** — the
proxy runs on the boot core (cpu6) and exposes `p.read64`/`p.write64`/`p.write32`. We replicate
`smp_start_cpu` by hand for ONE secondary core: re-write its RVBAR (clearing LOCK), strobe the
CPU-start register, and watch the UART for `"RVBAR entry on secondary CPU"`. Binary answer on H1
(and incidentally H3, the start-register-layout hypothesis) before spending a reflash cycle.

**Decisions confirmed with the user:** (1) scope to **one** secondary core, gated behind a new
opt-in flag; (2) the probe **is allowed to write** the RVBAR + CPU-start MMIO — that's the whole
point; these are the same registers m1n1's own `smp_start_cpu` writes, and cpu6 was released this
exact way, so worst case the core simply doesn't start (no bricking).

## Approach

### New `smp_release_probe(buf, target_reg=0x1)` — `Scripts/m1n1/soc_bringup.py`

A manual replica of m1n1 `smp_start_cpu` (`smp.c:149-183`) for **one** secondary core, executed
from Python. Reuses the address derivation already proven in `smp_probe` (pmgr reg[0] `0x380700000`
+ `CPU_START_OFF_T8112` `0x34000` = CPU-start base `0x380734000`; per-CPU RVBAR from
`cpu-impl-reg[0]`). Sequence, logging each step to `buf` and stdout:

1. **Select target** — default the core with `reg==0x1`, which decodes to die0/cluster0/core1 (an
   E-core; cpu1 in the ADT). Resolve its `cpu-impl-reg[0]` (`impl`) and decode die/cluster/core
   from `reg` via the `smp.c:25-27` GENMASKs (same decode as `smp_probe`). Make the target `reg`
   selectable via an optional `--release-core=<hexreg>` arg (default `0x1`).
2. **Pre-state (read-only):** `p.read64(impl)` — log current RVBAR value + LOCK bit; read the
   CPU-start words at base `+0x4` and `+0x8+4*cluster` for reference. Also snapshot `p.get_exc_count()`.
3. **Clear LOCK / re-arm RVBAR** (`smp.c:161`): `vectors = p.read64(impl) & RVBAR_ADDR_MASK`
   (this is `_vectors_start`, `0x100021ec000`); `p.write64(impl, vectors)`. Then **verify**:
   `p.read64(impl) & RVBAR_LOCK` — did the lock clear? Log the result. *(If it stays set, that's
   itself decisive — the lock is sticky-until-reset and H1's in-m1n1 fix can't help either; report
   that and stop before strobing.)*
4. **Enable + start strobes** (`smp.c:168`, `smp.c:171`): only if LOCK cleared (or as configured):
   `p.write32(base + 0x4, 1 << (4*cluster + core))` then
   `p.write32(base + 0x8 + 4*cluster, 1 << core)`.
5. **Poll + observe:** brief loop reading `p.get_exc_count()` and prompting the user to watch the
   `TTY>` console for **`RVBAR entry on secondary CPU`** (the `_cpu_reset_c` marker). We cannot
   read `spin_table[index].flag` (that's m1n1's internal C state, and the manual poke doesn't wire
   the proxy's secondary bookkeeping), so the **UART marker is the success signal**, not a proxy
   return. Log the exc_count delta (a fault on the boot core would show here).
6. **Verdict block:** "marker appeared → H1 CONFIRMED (locked RVBAR was the blocker) → RUN 28 =
   the minimal m1n1-source fix + reflash"; "LOCK cleared but no marker → start-register layout
   (H3) → offset sweep"; "LOCK stayed set → sticky lock → different fix".

**Safety framing:** these are the identical writes `smp_start_cpu` performs; `guarded()` isn't
needed for the writes (they target live pmgr / CPU-IMPL windows m1n1 itself writes), but wrap the
reads in the existing liveness discipline and abort cleanly if the boot core ever stops responding.

Add `--smp-release-probe` (opt-in, does the writes) and `--release-core=<hexreg>` (default `0x1`)
to `build_argparser()`, and wire into `main()` after `smp_probe`. Update the "no probe selected"
guard to include it.

### RUN 27 dispatcher arm — `Scripts/m1n1/perstn-run.sh`

Add a `27)` arm (after `26)`), `RUNNER=soc_bringup.sh`. Flags:
`--smp-start --smp-probe --smp-release-probe --require-build=rc1-60-g`. (`--smp-start` first so the
normal path's "Failed!" is on record for the same boot; then `--smp-probe` re-captures RVBAR state;
then the manual release attempt.) Add `27` to the two usage strings. Leave RUN 25/26 arms as-is.

### Record RUN 26 findings — `Scripts/m1n1/logs/26/findings.md`

Write the RUN 26 outcome: run completed clean (fixes worked); RVBAR correct + LOCK set on all cores;
**no `"RVBAR entry on secondary CPU"` in UART** ⇒ cores never reach the vector ⇒ H1 (locked-RVBAR /
missing `cyc_ovrd` re-write) is the lead; RUN 27 = the no-reflash manual-release probe to confirm.

## Critical files

- **`Scripts/m1n1/soc_bringup.py`** — new `smp_release_probe()` + `--smp-release-probe` /
  `--release-core` flags; wire into `main()`. Reuse the `smp_probe` address derivation
  (`CPU_START_OFF_T8112`, `RVBAR_LOCK`, `RVBAR_ADDR_MASK`, the `CPU_REG_*` masks — already defined),
  `p.read64`/`p.write64`/`p.write32`, `check_alive`, `log`.
- **`Scripts/m1n1/perstn-run.sh`** — new `27)` arm + usage strings.
- **`Scripts/m1n1/logs/26/findings.md`** — new; RUN 26 outcome + the H1 lead.
- Reference only (RUN 28, the eventual fix — needs reflash): `m1n1/src/smp.c:159-162` (drop the
  `if (cpu_features->cyc_ovrd)` guard around the RVBAR re-write, OR add a `rvbar_rewrite` flag to
  `features_m4` — preferred over enabling full `cyc_ovrd`, which also turns on the CYC_OVRD WFI-mode
  setup at `chickens.c:236-241` and widens blast radius to T8140 which shares `features_m4`).
  `m1n1/src/startup.c:219-242` (the `_cpu_reset_c` "RVBAR entry" marker), `m1n1/src/start.S:149-170`
  (`cpu_reset` "OK" marker).

## Verification

Real hardware (M4 mini + m1n1 over UART); the **user** runs the live steps. No reflash (rc1-60-g).

1. **Static (safe here):**
   - `python3 -c "import ast; ast.parse(open('Scripts/m1n1/soc_bringup.py').read())"`
   - `bash -n Scripts/m1n1/perstn-run.sh`
   - Confirm `perstn-run.sh 27` routes to `soc_bringup.sh` with the expected flags, and that
     `--smp-release-probe` / `--release-core=0x1` parse (extract `build_argparser` + `parse_args`,
     as done for RUN 25/26).
2. **Live:** `./Scripts/m1n1/perstn-run.sh 27`.
3. **Read** `/tmp/m4-recon/nic-runtime.txt` + the `TTY>` console, keying on:
   - The `--smp-release-probe` section: did `p.write64(impl, vectors)` **clear RVBAR_LOCK**
     (post-write `read64 & 1 == 0`)?
   - **Did the `TTY>` console print `RVBAR entry on secondary CPU`** after the start strobe? This is
     the H1 confirmation signal.
   - exc_count delta stays 0 on the boot core (the probe didn't fault cpu6); run reaches
     `[flush:done]`, m1n1 still alive.
4. **Outcome → RUN 28 (record in `logs/27/findings.md`):**
   - **Marker appears** → H1 CONFIRMED: the locked, un-rearmed RVBAR was the blocker → RUN 28 =
     minimal m1n1-source fix (unconditional RVBAR re-write in `smp_start_cpu`, or `rvbar_rewrite`
     flag) + rebuild + reflash; success = cores print "Started." on the normal `--smp-start` path.
   - **LOCK cleared but no marker** → H1 out, start-register layout (H3) suspect → read-only offset
     sweep / compare cpu6's live CPU-start bits, then a dedicated `CPU_START_OFF_T8132`.
   - **LOCK stayed set after write** → sticky-until-reset lock → the in-m1n1 `write64` can't help
     either; rethink (the fix must avoid needing to clear it, e.g. a different release path).

## Non-goals for this run

No m1n1-source edits, no rebuild, no reflash. No IOP boot, no `pcie_init`, no phy_ip. The probe
writes **only** the RVBAR + CPU-start registers for the single selected core (the same writes
`smp_start_cpu` does) — no other device state is touched.
