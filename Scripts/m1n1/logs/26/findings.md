# RUN 26 findings — RVBAR is correct but LOCKED; the cores never reach the vector

**Dispatch:** `./Scripts/m1n1/perstn-run.sh 26` → `soc_bringup.sh` on m1n1 `v1.6.0-rc1-60-g`
(guard OK). Flags: `--smp-start --smp-diag --smp-probe --soc-recon`. **The run completed cleanly**
(reached `[flush:done]`, 10867 bytes, m1n1 alive) — both fixes from the prior commit worked:
- **Port auto-detect**: a Samsung phone had grabbed `/dev/ttyACM0`; the wrapper found m1n1 by USB
  product string and used `/dev/ttyACM1` (`run.log:2` `using m1n1 device: /dev/ttyACM1`).
- **`acio_status` gate-check**: `--soc-recon` ran without touching the un-clocked ACIO block; no
  wedge (contrast RUN 25, which hard-wedged on that read).

## The decisive new data (`--smp-probe`)

All 9 refusing cores are POWERED (`--smp-diag`: PMGR `actual=0xf`) — power is not the issue
(re-confirmed). The RVBAR read is the breakthrough:

- **Every core's RVBAR is CORRECT**: `cpu-impl-reg[0]` masked addr = **`0x100021ec000`**
  (= m1n1's `_vectors_start`), identical across cluster 0 and cluster 1 (single die, die=0 for all).
  This **rules out** the address-math / die-offset hypothesis.
- **`RVBAR_LOCK` (bit 0) is SET on every core** (raw value `0x00100100021ec001` etc.), including the
  running boot core cpu6.
- CPU-start block (`0x380734000`): `+0x0`/`+0x4` = `0x300`; `+0x8`/`+0xc` = `0` after `--smp-start`
  (consistent with a write-1 strobe that self-clears — cpu6 was started this exact way and runs).

## The bisector: cores never reach the reset vector

m1n1's `_cpu_reset_c` (`src/startup.c:222`) prints **`"RVBAR entry on secondary CPU"`** the instant
a secondary reaches the vector — *before* `init_cpu()` and *before* the spin_table flag write.
**RUN 26's UART contains no such line.** So the released cores die *before the first instruction*
of m1n1's secondary path — a pure release failure, not a later fault. This eliminates the
"M4 per-part init / chicken-bit gap" hypothesis for *this* boot (that would require the core to
reach the vector first).

## Mechanism → the lead hypothesis (H1)

`smp_start_cpu` re-writes RVBAR (which "also clears RVBAR_LOCK") **only** inside
`if (cpu_features->cyc_ovrd)` (`smp.c:159-162`). `features_m4` (`chickens.c:113-117`) **omits
`cyc_ovrd`** (init fn NULL, `chickens.c:155-156`; `// XXX figure out what features are actually
available on M4`). Every known-good Apple part (M1/M2/M3) sets `cyc_ovrd=true`, so m1n1 always
re-arms RVBAR before strobing start on working hardware — **M4 is the first part that skips it.**

**H1:** the cores are strobed against a locked, un-rearmed RVBAR and never leave reset.

## RUN 27 plan (no reflash) — confirm H1 from Python

`./Scripts/m1n1/perstn-run.sh 27` — `--smp-start --smp-probe --smp-release-probe`. The new
**`--smp-release-probe`** manually replicates `smp_start_cpu` for ONE secondary (default `reg=0x1`,
an E-core): `p.write64(impl, _vectors_start)` to clear RVBAR_LOCK, verify the lock cleared, then
`p.write32` the CPU-start enable + start strobes — the identical registers m1n1 writes, no reflash.
Success signal = **`RVBAR entry on secondary CPU`** on the TTY console (we can't read m1n1's internal
spin_table flag).

**Decides RUN 28:**
- **Marker appears** → H1 CONFIRMED → RUN 28 = minimal m1n1-source fix (unconditional RVBAR re-write
  in `smp_start_cpu`, or a `rvbar_rewrite` flag on `features_m4` — preferred over enabling full
  `cyc_ovrd`, which also turns on CYC_OVRD WFI-mode setup and widens blast radius to T8140) + reflash.
- **LOCK cleared but no marker** → start-register layout (H3) → offset sweep, dedicated
  `CPU_START_OFF_T8132`.
- **LOCK never cleared** → sticky-until-reset lock → the in-m1n1 write can't help either; rethink.

(Unrelated: the DCP rtkit still crashes at boot with `ASSERT!Messenger.c:299` — a display-firmware
issue independent of the SMP wall.)
