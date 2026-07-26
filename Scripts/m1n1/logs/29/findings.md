# RUN 29 findings — H5 confirmed: pmgr+0x34000 is the WRONG CPU-start register for M4

**Dispatch:** `./Scripts/m1n1/perstn-run.sh 29` → `soc_bringup.sh` on m1n1 `v1.6.0-rc1-60-g`.
Flags: `--smp-start --smp-probe --cpustart-decode`. The run **completed cleanly** (`[flush:done]`,
m1n1 alive, exc_count delta 0 — pmgr-space reads don't SError, unlike RUN 28's ACC window).

## The CPU-start block at pmgr+0x34000 is inert (H5 confirmed)

`--cpustart-decode` dumped `0x380734000 .. +0x3c` after `--smp-start` (so m1n1 had already written
its strobe this boot):

```
+0x0 = 0x00000300   (bits 8,9)
+0x4 = 0x00000300   (bits 8,9)
+0x8 .. +0x3c = 0x00000000
```

- **Identical to RUN 26/28** — m1n1's `--smp-start` writes to `+0x4` (`1<<(4*cluster+core)`)
  produced **no observable change**; `0x300` persists across every run.
- **The running core cpu6 (cluster1/core0/cpu_id6) is absent under every encoding at every offset:**
  per-cluster would need bit 4 (`4*1+0`), flat would need bit 6 — neither appears anywhere. The only
  set bits (8,9) map to a nonexistent cluster 2 under m1n1's encoding, and to *waiting* cpu8/cpu9
  under the flat encoding — i.e. not "which cores are running" under any scheme.

If `0x34000` were the real CPU-start register, cpu6 (which **is** running) would show as enabled
somewhere. It doesn't → **the register is elsewhere.** Combined with RUN 28's acc-impl SError (M4
provably relocated per-cluster MMIO), the M2/M3-inherited `0x34000` is the wrong address for M4.

## The decisive lead: upstream split the M4 family's CPU-start offset

`git show upstream_source/main:src/smp.c`:
- **T6040 (M4 Pro/Max) → `CPU_START_OFF_T6031` = `0x88000`** (the M3-Max value) — *not* 0x34000.
- **T8132 / T8140 (our base M4) → still `CPU_START_OFF_T8112` = `0x34000`**, added by Yureka as a
  bare `case T8132:` in the T8112 group (commit `0a9302e`, **no reasoning**) — an unverified guess,
  never validated for T8132.

So a sibling M4 die uses `0x88000`, and our die's `0x34000` was never checked. The real T8132 offset
is unknown and **cannot be cribbed** from Linux (spin-table; cores pre-released by iBoot; no
`cpu-start` reg in the DT) or the ADT (no property names it) — it must be found empirically.

Baseline re-confirmed: 9/9 secondaries `Failed!`; cores powered; RVBAR correct (all →
`0x10003f8c000` = m1n1 `_vectors_start`) but `LOCK=True`.

## RUN 30 plan (read-only, no reflash)

`./Scripts/m1n1/perstn-run.sh 30` — probes candidate CPU-start offsets. `--cpustart-decode` now
takes `--cpustart-offset` and flags any word whose bits **exactly match the running core set** (cpu6)
under the flat or per-cluster encoding — that word's block is the real register. RUN 30's arm probes
**`0x88000` (the T6040 value) first**; other candidates (`0x30000`, `0x38000`, `0x54000`, `0x28000`)
via `./perstn-run.sh 30 --cpustart-offset=0x…`, **one per boot, power-cycling between** (a wrong
offset may SError and wedge m1n1 — the probe reads the first word first and aborts on wedge, naming
the offset).

**Decides RUN 31:** a candidate block matches cpu6 → real offset found → RUN 31 = reflash smp.c with
`CPU_START_OFF_T8132 = <that offset>` + a dedicated `case T8132:`; success = a secondary prints
`OK`/`RVBAR entry`/`Started.`. No candidate matches / all SError → release isn't a simple pmgr strobe
(SMC/AIC/mailbox) → deeper RE (genuinely-unsolved territory — no m1n1 branch boots M4 secondaries).
