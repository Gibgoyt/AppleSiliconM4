# RUN 28 findings — enable_core recipe confirmed; the SMP fix is NOT upstream; the CPU-start offset is the lead

**Dispatch:** `./Scripts/m1n1/perstn-run.sh 28` → `soc_bringup.sh` on m1n1 `v1.6.0-rc1-60-g`.
Flags: `--smp-start --smp-probe --enable-core-parse --core-diff-scan`.

## 1. `--enable-core-parse`: the recipe is confirmed (clean, ADT-only)

Every `/cpus` node decodes to **`function-enable_core = 138:Core(1<<cpu_id)`** — phandle 138 = the
PMGR node (`pmgr1,t8132`), name `Core`, arg = the **flat per-core bit** (cpu0=0x1, cpu1=0x2, …
cpu6=0x40, … cpu9=0x200), every arg matching `1<<cpu_id`. This is iBoot's core-release recipe. m1n1
never invokes it — `smp_start_cpu` writes only the legacy `pmgr+0x34000` strobe, and with a
**per-cluster `1<<core`** encoding (a *different* encoding than the recipe's flat bitmask).

## 2. `--core-diff-scan`: the per-cluster ACC window SErrors and wedges m1n1

The scan's gate precondition passed (all CPU gates `actual=0xf`), then the **first** read of
`acc-impl-reg` (`0x211F00000`) threw **`Exception: SError`** → UART timeout → m1n1 dead (needed a
power-cycle). So the per-cluster `acc-impl`/`cpm-impl` "impl" windows are **not plain AP-MMIO**
(they're CPU-local / system-register space). The differential-MMIO approach to those windows is
**dead**, and — the probe lesson — **an SError desyncs the proxy UART even under `guarded()`**
(worse than an AXI stall; `guarded()` does not make an SError-prone read safe). No DIFF lines were
produced. (The `cpu-impl+0x100` "difference" between cpu6/cpu7 is just their MPIDR/affinity bits —
not an enable signal.)

## 3. Branch-diff verdict: the M4 SMP fix is NOT upstream

A full m1n1 git-archaeology pass (our `t8132-pcie` HEAD vs `main`/`upstream_source/main`/
`m4-integration`/`yuyuyureka`) is decisive:
- Our `smp_start_cpu` is **already functionally equal to — actually stricter than — upstream's** M4
  path (`!cyc_ovrd` == upstream's renamed `!apple_sysregs_unlocked`; upstream `7339546`; we keep a
  `return` upstream dropped). RVBAR handling is not the missing piece.
- `features_m4` is **identically `// XXX figure out what features are actually available on M4`** on
  every branch (init fn NULL, minimal flags). `start.S`/`startup.c` are **byte-identical**.
- There is **no sysreg/RVBAR unlock sequence anywhere in the tree** (`git grep -i unlock`). The
  strategy is strictly defensive: don't touch the locked MMIO, rely on iBoot's RVBAR — consistent
  with our acc-impl SError.
- A rebase/cherry-pick would change **nothing observable**. (Two commits are worth taking *later*,
  once cores boot: `954f80c` mmu_secondary_setup dsb+cache-invalidate, `707d564` L2C_ERR gating —
  but both fire *after* a core is running, so neither explains our zero-output symptom.)

## 4. The leading hypothesis (H5): wrong CPU-start offset for M4

m1n1 lumps T8132 into `CPU_START_OFF_T8112 = 0x34000` (`smp.c:289-291`) — an **unverified M2/M3
guess**. We have **direct evidence M4 relocated per-cluster MMIO** (the acc-impl SError). If M4 also
moved the CPU-start block, the strobe lands on the wrong address → the reset is never released → the
core emits **zero UART output** (no `OK`, no `RVBAR entry on secondary CPU`) — exactly the symptom.
Supporting hint: the block at `0x380734000` reads `+0x0/+0x4 = 0x300` (bits 8,9); under m1n1's
`1<<(4*cluster+core)` formula bits 8,9 map to *cluster 2* — which doesn't exist on this 2-cluster
part — so the value already looks inconsistent with the assumed layout/encoding.

Baseline re-confirmed: 9/9 secondaries still `Failed!`; cores powered (`actual=0xf`); RVBAR correct
(all → m1n1 `_vectors_start`) but `LOCK=True`.

## RUN 29 plan (read-only, no reflash)

`./Scripts/m1n1/perstn-run.sh 29` — `--smp-start --smp-probe --cpustart-decode` (**no**
`--core-diff-scan` — it SErrors). **Power-cycle the M4 first.** The new **`--cpustart-decode`** dumps
and decodes the CPU-start block at `pmgr+0x34000` (proven-readable pmgr space) against the
running/waiting core set, under both the per-cluster `1<<(4*cluster+core)` and flat `1<<cpu_id`
encodings — to find where the running core's bit actually is and whether m1n1's intended strobe bits
even land there.

**Decides RUN 30:** running-core bit matches the flat encoding, or m1n1's intended per-cluster bits
absent → wrong offset/encoding → RUN 30 = corrected `CPU_START_OFF_T8132` / flat-bitmask strobe +
reflash (success = a secondary prints `OK`/`RVBAR entry`/`Started.`). Intended bits present yet cores
don't start → offset right, blocker elsewhere (deeper RE).

(Unrelated: DCP rtkit still crashes at boot with `ASSERT!Messenger.c:299`.)
