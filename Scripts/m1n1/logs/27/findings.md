# RUN 27 findings — the RVBAR lock is a red herring; the release path is `function-enable_core`

**Dispatch:** `./Scripts/m1n1/perstn-run.sh 27` → `soc_bringup.sh` on m1n1 `v1.6.0-rc1-60-g`
(guard OK, `/dev/ttyACM1`). Flags: `--smp-start --smp-probe --smp-release-probe`. The run completed
cleanly (`[flush:done]`, m1n1 alive).

## The RVBAR-lock hypothesis (H1) is DEAD

`--smp-release-probe` manually replicated `smp_start_cpu` for cpu1 (`reg=0x1`): it wrote
`_vectors_start` (`0x10003dfc000`) back into the RVBAR to clear `RVBAR_LOCK`, exactly as m1n1's
`smp.c:161` does on cyc_ovrd parts. Result:

```
[write] RVBAR <- 0x10003dfc000 (clears LOCK, re-arms vector)
[post] RVBAR=0x0010010003dfc001 (LOCK=True)
-> LOCK STAYED SET after write -> RVBAR lock is sticky-until-reset
```

**The lock did not clear.** So m1n1's own `write64(impl, _vectors_start)` would *also* fail to clear
it on M4 — the naive "add cyc_ovrd / rvbar_rewrite" fix cannot work. And `--smp-probe` re-confirmed
the boot core **cpu6 runs fine with `LOCK=True`**. Therefore **the RVBAR lock neither blocks release
nor needs clearing — it is a red herring.** (This is the "LOCK stayed set → rethink" branch RUN 27's
own outcome matrix anticipated.) The address logic is sound: RVBAR = `0x10003dfc000` = `m1n1 base` =
`_vectors_start` (relocated this boot); every core matches. CPU-start offset `0x34000` is correct
for t8132.

The secondaries still emit **zero** UART (no `OK` from `start.S:149`, no `RVBAR entry on secondary
CPU` from `startup.c:222`) — they never execute one instruction. The strobe isn't releasing them.

## The real lead: `function-enable_core` (a PMGR release recipe m1n1 ignores)

Every `/cpus/cpuN` node carries **`function-enable_core = 138:Core(<core-bitmask>)`**
(cpu0=0x1, cpu1=0x2, … cpu6=0x40, cpu7=0x80…). **Phandle 138 is the PMGR node**
(`compatible=[pmgr1,t8132]`, `m4_recon/adt.txt:6886-6889`). So on M4, core-enable is a **PMGR
device-function** that iBoot uses to release cores — and **m1n1's `smp_start_cpu` never invokes it**;
it only writes the legacy `pmgr+0x34000` strobe (`smp.c:168/171`) with a *per-cluster* `1<<core`
encoding, whereas the recipe uses a *flat per-core bit* (cpu6=0x40). The boot core cpu6 was released
via `function-enable_core`; the 9 secondaries only ever get the strobe.

**Hypothesis H4:** M4 secondaries must be released via the PMGR `Core(bitmask)` recipe, not the
legacy strobe.

Crucially, the 'Core' FourCC function has **no register-level implementation in m1n1 or Asahi
Linux** — `m1n1/src/pmgr.c` only models `clock-gates`→PS registers (no generic function-evaluator);
Linux's `smp_spin_table.c` assumes the cores were already released by iBoot/m1n1. The recipe lives
in iBoot/SMC firmware, so the register **can't be cribbed from source** — it must be found live.

## RUN 28 plan (read-only, no reflash)

`./Scripts/m1n1/perstn-run.sh 28` — `--smp-start --smp-probe --enable-core-parse --core-diff-scan`:
- **`--enable-core-parse`** (ADT-only): confirm every core's `function-enable_core` = phandle-138
  PMGR `Core(1<<cpu_id)`.
- **`--core-diff-scan`** (read-only MMIO): differential-read **cpu6 (running)** vs **cpu7 (waiting)**,
  same cluster (1), across the **shared** `acc-impl-reg` (`0x211F00000`) and `cpm-impl-reg`
  (`0x211E40000`) windows + the `cpu-impl+0x100` status word (`smp.c:223`). A bit **set for cpu6 /
  clear for cpu7** in a shared window is the enable-register candidate. Gated on the cluster PMGR
  gate being ACTIVE; guarded + `check_alive` per read; aborts on any wedge. **No writes.**

**Decides RUN 29:** a clear shared-window candidate → gated write test (write the bit for cpu7, watch
TTY for `RVBAR entry on secondary CPU`). No memory-mapped bit → the recipe pokes SMC/AOP or a pmgr
offset outside these windows → RUN 29 becomes a reflash/instrumentation experiment; the
`cpu-impl+0x100` differential still gives the definitive "cpu7 held in reset" signal.

(Unrelated: DCP rtkit still crashes at boot with `ASSERT!Messenger.c:299` — a display-firmware issue
independent of the SMP wall.)
