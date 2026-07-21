## RUN 1 findings

RUN 1 = RUN S baseline + `--phycmn-early`. Single-variable delta: adds `mask32(phy_common+0, MODE_MASK=0x3, MODE_ON=0x1)` between step 6.f (T8140 marker) and any phy_ip access. Hypothesis: CLK_MODE=ON combined with RUN S's naked axi2af + naked pcieclkgen applies is the phy_ip ungate.

### 1. Step 5.8.a (axi2af naked apply -> axi_base) — reproduces RUN S bit-for-bit
```
STUCK     : 14  (bit-31 sets @ axi_base+0x00..0x34 except +0x38, and +0x3c)
NO-OP     :  2  (@ axi_base+0x38 val=0x80000016 mask=0x800000ff;
                 @ axi_base+0x40 val=0x8000000c mask=0x800000ff)
SKIP-NOOP : 42  (pre-value already matched target — no write needed)
PARTIAL   :  0
READ_FAIL :  0
total     : 58 of 58
```
Every entry's pre/wrote/post triple matched RUN S exactly. The `--phycmn-early` write fires AFTER 5.8.a, so identical results here confirm determinism of the naked-mask-RMW path across boots.

### 2. Step 5.8.b (pcieclkgen naked apply -> axi_sub5_base) — reproduces RUN S
```
#0 @ 0x495046200 mask=0x3e0 value=0x220 pre=0x00000a01 new=0x00000a21 post=0x00000a21  [STUCK]
STUCK: 1, total: 1 of 1
```

### 3. Step 7 (RUN 1's new write: `--phycmn-early`) — LANDED
`mask32(phy_common+0, MODE_MASK=0x3, MODE_ON=0x1)` returned `2150629377` (`= 0x80300001`). Guard `exc_count delta = 0`. Reachable-scan confirms:

```
phy_common+0:  0x80300000  ->  0x80300001   (bit 0 = MODE_ON set)
```

Bit 0 stays set through every subsequent checkpoint including post-6.f.T8140-marker (the last scan before 6.g).

### 4. Step 6.g — **STILL STALLS on the same offset**
```
==> ABOUT TO TOUCH phy_ip_base FOR THE FIRST TIME.
    --- 6.g.tunables apcie-phy-ip-pll-tunables (slice-filtered) ---
    plan (29 entries):  shared = 29
    RAISED at #0 (shared) 0x497040038: UartTimeout: Expected 1 bytes, got 0 bytes
```
Identical failure address (`phy_ip_base+0x38`) and identical error class (`UartTimeout`, i.e. m1n1 UART-froze / silent AXI stall) as every RUN A..S.

## Signal that jumps out

**RUN 1 hypothesis is FALSIFIED.** CLK_MODE=ON writes cleanly, stays set, and does NOT unblock phy_ip when combined with the naked axi2af + naked pcieclkgen applies. Neither ordering (CLK_MODE=ON after naked applies) nor either component alone (RUN I: CLK_MODE=ON only; RUN S: naked applies only) reaches phy_ip.

The `axi_base+0x38 <-> phy_ip+0x38` offset-alignment lead flagged in RUN S findings.md still stands. Of the 16 axi2af entries at `axi_base+0x00..0x40` that attempted bit-31 sets, 14 stuck and only two (`+0x38`, `+0x40`) refused. `phy_ip+0x38` is exactly where 6.g wedges. Either genuine aperture / enable-bit correlation or coincidence -- still undistinguished.

## Bit-31 clear-window tightened

RUN S's findings.md noted "something between 5.8.a and 6.f cleared bit 31 at axi_base+0." RUN 1's per-entry data narrows that window substantially:

| Checkpoint                        | axi_base+0x00 | Note |
|-----------------------------------|---------------|------|
| F.entry, post-1.pmgr, post-5      | `0x0000001c`  | reset state |
| post-5.75.naked-write-test        | **`0x8000001c`** | naked-write-test's last write set bit 31, STUCK |
| naked-extra axi2af #0 pre-read    | **`0x8000001c`** | SKIP-NOOP (matched already, no write) |
| post-5.8.a.axi2af-naked-apply     | `0x0000001c`  | **bit 31 CLEARED** |
| post-5.8.b, 6.b, 6.d, 7, 6.f      | `0x0000001c`  | stays cleared |

The clear happened during naked-extra axi2af entries #1..#57 (writes to axi_base offsets `+0x04..+0x40, +0x100..+0x108, +0x304..+0x14fc, +0x20028..+0x20058`). Reads alone did NOT clear it — the full reachable-scan of `axi_base+0..+0x40` at post-5.75 completed with bit 31 still set on entry #0. So the clearing side-effect is triggered by a WRITE somewhere in that range, not a read.

Three candidate mechanisms:
1. **Specific offset side-effect.** Some entry among #1..#57 targets a register whose write clears axi_base+0 bit 31 as a side-effect. Bisection by mid-apply scan would identify it.
2. **Clear-on-any-write latch.** axi_base+0 bit 31 is a self-clearing status latch that clears on any AXI write transaction into the block. The naked-write-test happens to write it LAST, so no subsequent transaction clears it before post-5.75's scan captures the STUCK state.
3. **Decay timer.** Bit 31 self-clears after N milliseconds regardless of transactions. Since post-5.75's scan runs milliseconds after the write and post-5.8.a's scan runs seconds after, this fits.

Mechanism (2) or (3) would mean bit 31 is not a persistent control — it's a status pulse we shouldn't rely on. Mechanism (1) leaves open the possibility of a "write axi_base+0 bit 31 LAST, then immediately access phy_ip" ordering fix. RUN 2 candidates below can distinguish.

## What's still unknown

- Which of the 57 axi2af writes clears axi_base+0 bit 31 — or does bit 31 decay independently of any specific write?
- Are `axi_base+0x38` and `+0x40` R/O in the current fabric state, or upstream-gated pending some other unlock? Same-mask same-value adjacent entry (#15 `+0x3c`) stuck, so it's offset-specific not value-specific.
- Does phy_ip decode POSTED writes even when reads AXI-stall? RUN P tested this from a weaker state (broken-applicator baseline); it wasn't tested from RUN 1's fuller state (all naked applies + CLK_MODE=ON).
- Do T8122's post-phy-tunables writes that T8140 skips (`poll(phy_shared+0x8, 1, 1)` + `set32(phy_shared+0, 0x200)`) matter on t8132? RUN L tested T8122's `set32(phy_shared+4, 0x10)` (6.i early); the pair at pcie.c:543-551 has never been tried.

## Proposed RUN 2 candidates

Rather than pick one now, four single-variable extensions of RUN 1 are queued. Actual RUN 2 selection deferred to the next dispatcher-writing session.

| RUN candidate | Change vs RUN 1 | Hypothesis being tested | perstn.py change? |
|---|---|---|---|
| **A** | Add `--phy-ip-write-probe` + `--phy-ip-diag-at=post-7.phycmn-early` | Does phy_ip decode POSTED writes to +0x38 in the RUN 1 state? Binary: WROTE unlocks a "sequence phy-ip-pll via naked write32" strategy; STALL forces upstream aperture search. | No |
| **B** | Add mid-apply reachable-scan hook (new `--axi2af-naked-apply-mid-scan=N` flag) that snapshots axi_base+0..+0x40 after every N-th naked-extra entry | Bisects which axi2af write clears axi_base+0 bit 31 (or shows monotonic decay). Directly answers finding (4)'s three-way ambiguity. | Yes (new hook + CLI arg) |
| **C** | Add `--axi-writelock-probe` that tries progressively wider mask writes at `axi_base+0x38` and `+0x40` (mask=0x80000000 alone, 0xff000000, 0xffffffff) | Distinguishes R/O from write-locked-until-upstream-unlock at the two offsets adjacent to phy_ip's wedge address | Yes (new probe function) |
| **D** | Add `--t8122-shared-post` that runs the T8122-only `poll(phy_shared+0x8, 1, 1)` + `set32(phy_shared+0, 0x200)` writes (pcie.c:543-551) between step 6.f and 6.g | Do the T8122 shared-init writes T8140 skips gate phy_ip on t8132? Untested by any prior RUN. | Yes (new step + CLI flag) |

**A** is dispatcher-only (fastest to iterate) and returns a binary result that hard-forks the next 3 runs. **B** and **C** are diagnostics that would sharpen the axi_base+0x38 / +0x40 hypothesis but do not by themselves unblock phy_ip. **D** is the last un-tried T8122/T81XX codepath transposition.
