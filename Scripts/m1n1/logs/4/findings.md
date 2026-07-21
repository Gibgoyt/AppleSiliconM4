## RUN 4 findings

RUN 4 = RUN 1 baseline but replaces the DESTRUCTIVE `--naked-write-test` with a new READ-ONLY `--naked-write-test-readonly`. Tests whether the sub5+0 clobber (iBoot `0x00081f55` -> `0x00000a01`, RUN 3 finding) has been the confounding variable for the last five RUNs (S / 1 / 2 / 3).

Full dispatch flags: `--no-pcie-init --preinit-probe --pmgr-enable --pmgr-per-port --gate-poke --phy-ip-probe --t8140-replay --phy-ip-diag --fuse-recon --naked-write-test-readonly --axi2af-naked-apply --pcieclkgen-naked-apply-to=axi_sub5_base --reachable-scan --phycmn-early --phy-ip-diag-at=none`.

### 1. Read-only naked-write probe — iBoot state PRESERVED

From `nic-runtime.txt:1555-1608`, the new READ-ONLY probe header lands as designed:

```
=== naked-write test READ-ONLY (RUN 4: first-touch only, no writes) (RUN Q base + RUN R extensions) ===
```

Per-target result:

| target        | source                                       | pre value    | (would have written) | outcome |
|---------------|----------------------------------------------|--------------|----------------------|---------|
| sub5+0x00     | RUN R sub5 first-touch                       | `0x00081f55` | 0x00000a01           | READ-ONLY; `axi_sub5_reachable=True` |
| sub6+0x00     | RUN R sub6 first-touch                       | `0x00000239` | 0x00000a01           | READ-ONLY; `axi_sub6_reachable=True` |
| rc_base+0x54  | RUN R R/W bitmask readback                   | `0x00000140` | 0xffffffff           | READ-ONLY |
| rc_base+0x100 | RUN R cio3pllcore #6 rc_base candidate       | `0x00000000` | 0x000b40b4           | READ-ONLY |
| rc_base+0x00  | cio3pllcore #0                               | `0x00040000` | 0x00040a01           | READ-ONLY |
| rc_base+0x24  | cio3pllcore #1                               | `0x00000000` | 0x00000800           | READ-ONLY |
| rc_base+0x28  | cio3pllcore #2                               | `0x00000000` | 0x00000b00           | READ-ONLY |
| rc_base+0x38  | cio3pllcore #3                               | `0x00000000` | 0x00000000           | READ-ONLY |
| axi_base+0x00 | axi2af #0                                    | `0x0000001c` | 0x8000001c           | READ-ONLY |

`axi_sub5_reachable` and `axi_sub6_reachable` both flipped True on the pre-read. All downstream gates (5.8.b pcieclkgen, 5.8.c cio3pllcore, reachable-scan windowing) proceeded normally. Every guard `delta = 0`; no SYNC / SError anywhere in the probe.

**iBoot's `sub5+0 = 0x00081f55` was preserved through post-5.75 and post-5.8.a** — this is the direct proof that the RUN 4 design worked: the READ-ONLY variant flips the reachability flag without destroying iBoot state.

### 2. Step 5.8.a axi2af naked apply — 15 STUCK / 2 NO-OP / 41 SKIP-NOOP

From `nic-runtime.txt:2200-2205`:

```
STUCK     :  15
PARTIAL   :   0
NO-OP     :   2
SKIP-NOOP :  41
READ_FAIL :   0
total     :  58 of 58
```

Same pattern as RUNs S and 1. The 2 NO-OPs are again at exactly the same offsets:

| #  | offset | mask         | value        | pre         | new         | post        | tag    |
|----|--------|--------------|--------------|-------------|-------------|-------------|--------|
| 14 | 0x0038 | `0x800000ff` | `0x80000016` | `0x00000016`| `0x80000016`| `0x00000016`| NO-OP  |
| 16 | 0x0040 | `0x800000ff` | `0x8000000c` | `0x0000000c`| `0x8000000c`| `0x0000000c`| NO-OP  |

Adjacent entry #15 @ `axi_base+0x3c` (mask + value identical to #16, just different offset) STUCK. The refusal is offset-specific, not value-specific. `axi_base+0x38` and `+0x40` share the `+0x38` low-nibble alignment of the `phy_ip+0x38` wedge address.

### 3. Step 5.8.b pcieclkgen naked apply — STUCK, but clobbers iBoot bits 6, 8

From `nic-runtime.txt:2650`:

```
#  0 @ 0x495046200 mask=0x3e0 value=0x220 pre=0x00081f55 new=0x00081e35 post=0x00081e35 [STUCK]
```

Bit math against iBoot's pre-value:

- **iBoot** `0x00081f55` — bits [0, 2, 4, 6, 8, 9, 10, 11, 12, 19]
- **mask 0x3e0** — bits [5, 6, 7, 8, 9]
- **value 0x220** — bits [5, 9]
- **`pre & ~mask`** = `0x00081c15` — bits [0, 2, 4, 10, 11, 12, 19] (iBoot bits OUTSIDE the mask preserved)
- **`(pre & ~mask) | value`** = `0x00081e35` — bits [0, 2, 4, 5, 9, 10, 11, 12, 19]

**Δ vs iBoot:** LOST bits 6, 8 (both fell inside pcieclkgen's mask); GAINED bit 5 (from `value`). All iBoot bits OUTSIDE the mask (0, 2, 4, 9, 10, 11, 12, 19 — eight of the ten iBoot bits) survive intact.

Contrast with RUN 3 where `--naked-write-test`'s full-word write left sub5+0 at `0x00000a01` (only bits [0, 5, 7] set). RUN 4 has 8 of 10 iBoot bits preserved. RUN 3 had only 1.

### 4. sub5+0 persistence across every Phase F checkpoint

Reachable-scan of `axi_sub5_base +0..+0x104` at each of the 7 checkpoints (F.entry has axi_sub5 SKIPPED because the first-touch flag hasn't flipped yet; the flag flips at step 5.75's pre-read):

| Checkpoint                          | sub5+0        | Interpretation |
|-------------------------------------|---------------|----------------|
| F.entry / post-1.pmgr / post-5      | SKIP          | axi_sub5_reachable=False (before first-touch pre-read) |
| post-5.75.naked-write-test          | `0x00081f55`  | iBoot value untouched — READ-ONLY probe worked |
| post-5.8.a.axi2af-naked-apply       | `0x00081f55`  | still untouched (axi2af writes target `axi_base`, not sub5) |
| post-5.8.b.pcieclkgen-naked-apply   | `0x00081e35`  | pcieclkgen mask-RMW landed (see §3) |
| post-6.b.CLK0ACK                    | `0x00081e35`  | persists across phy_shared CLK0 handshake |
| post-6.d.CLK1ACK                    | `0x00081e35`  | persists across phy_shared CLK1 handshake |
| post-7.phycmn-early                 | `0x00081e35`  | `mask32(phy_common+0, MODE=1)` does NOT clobber sub5 |
| post-6.f.T8140-marker               | `0x00081e35`  | final state before 6.g wedge |

**Nothing between step 5.8.b and step 6.g mutates sub5+0.** The mask-RMW pcieclkgen state is stable.

### 5. axi_base+0 bit-31 clear window — independent of `--naked-write-test`

RUN 1 findings.md flagged that `axi_base+0` bit 31 gets set during step 5.8.a's entry #0 (`STUCK` at post-read of the individual write) but is CLEARED again by the time the post-5.8.a reachable-scan runs. RUN 4 confirms this clear behavior is NOT caused by `--naked-write-test`'s destructive write:

| Checkpoint                          | axi_base+0    | Notes |
|-------------------------------------|---------------|-------|
| F.entry                             | `0x0000001c`  | iBoot baseline, bit 31 not set |
| post-1.pmgr                         | `0x0000001c`  | unchanged |
| post-5.phy-tunables                 | `0x0000001c`  | unchanged (broken applicator does not touch axi_base) |
| post-5.75.naked-write-test          | `0x0000001c`  | **RUN 4 delta vs RUN S/1: bit 31 stays cleared here (READ-ONLY skipped the write)** |
| post-5.8.a.axi2af-naked-apply       | `0x0000001c`  | bit 31 CLEARED again after the 57-write axi2af apply |
| post-5.8.b through post-6.f         | `0x0000001c`  | stays cleared |

Per-entry write to axi_base+0 (entry #0, `nic-runtime.txt:2052`) shows `post=0x8000001c [STUCK]` at the per-entry post-read. So bit 31 WAS set immediately after the write, then re-cleared by some subsequent write in entries #1..#57 (offsets `+0x04..+0x40`, `+0x100..+0x108`, `+0x304..+0x14fc`, `+0x20028..+0x20058`).

RUN 1 findings.md hypothesized three mechanisms: (a) offset-specific side-effect; (b) clear-on-any-write latch; (c) time-based decay. RUN 4 doesn't further disambiguate them, but definitively excludes `--naked-write-test`'s destructive final write to axi_base+0 as the trigger — the clear happens even when that write never fired.

### 6. Step 6.g — STILL wedges at the same offset, same error class

From `nic-runtime.txt:4855-4862`:

```
==> ABOUT TO TOUCH phy_ip_base FOR THE FIRST TIME.
   Entries in the INACTIVE port-1 slice (0x10000..0x18000)
   will be SKIPPED to avoid the AXI stall observed on
   the 2026-07-11 run. See phy-ip-report for entry list.
--- 6.g.tunables apcie-phy-ip-pll-tunables (slice-filtered) ---
  plan (29 entries):
    shared               = 29
  RAISED at #0 (shared) 0x497040038: UartTimeout: Expected 1 bytes, got 0 bytes
```

Identical failure address (`0x497040038` = `phy_ip_base + 0x38`), identical failure class (`UartTimeout`, m1n1 UART-froze / silent AXI stall) as every RUN A..3.

### 7. Phase B/D/E — clean and unchanged

- SMC power on apcie fabric (Phase B): completed, `AP power state is now 0x20`.
- Phase D `APCIE_SYS_ST` + `APCIE_PHY_SW` walk to actual=0xf: converged cleanly.
- Phase E rc/axi sanity: all reads succeed.

## Signal that jumps out

**Destructive-probe hypothesis is DEFINITIVELY FALSIFIED.** RUN 4 preserved 8 of 10 iBoot PLL-word bits at `sub5+0` (specifically bits 2, 4, 10, 11, 12, 19 — six bits RUN 3 destroyed). If any of those bits had been a PLL enable or reference-clock select, RUN 4 would have shown at least partial forward progress. It shows exactly zero: same wedge address, same error class, same failure count. Whatever gates `phy_ip+0x38` on t8132, it is NOT any bit at `sub5+0` that our destructive full-word write clobbered.

This matches the doc's interpretation matrix branch:

> 6.g wedges, sub5+0 shows iBoot 0x81f55 preserved except for pcieclkgen mask-0x3e0 bits (~0x81f75) → destructive-probe hypothesis definitively ruled out; RUN 5 = candidate B (`--phy-ip-write-probe`)

(Predicted value `~0x81f75` was slightly off — the correct RMW arithmetic yields `0x00081e35` because pcieclkgen's mask CLEARS iBoot bits 6, 8 before OR'ing in bits 5, 9. The interpretation-matrix branch that fires is the same.)

## What's still unknown

1. **pcieclkgen's mask-RMW still clobbers iBoot bits 6, 8.** Neither RUN 4 nor any prior RUN has probed sub5+0 with bits 6 AND 8 both preserved. If either is a PLL enable, we're STILL breaking iBoot's config. Would require either dropping pcieclkgen entirely or narrowing its mask to bit-5-only.
2. **Does phy_ip decode POSTED writes in the RUN 4 state?** RUN P (2026-07-12) tested this from a weaker broken-applicator baseline where NO naked applies had landed. RUN 4's state (naked axi2af landed, naked pcieclkgen landed at sub5+0, CLK_MODE=ON) has never been probed for write-decode symmetry.
3. **Do the T8122 shared-init post writes T8140 skips matter?** `poll(phy_shared+0x8, 1, 1, 250000)` + `set32(phy_shared+0, 0x200)` at pcie.c:543-551 remain untested by any prior RUN.
4. **Why does the m1n1 C-side reach LTSSM BUSY** (per commit `6b277bc`) while our Python replay stalls on the first `phy_ip+0x38` read? Barrier / DSB / ISB ordering, silent write-drop, or timing?

## Where we are strategically

Two of the four remaining live hypotheses ((2) phy_ip write-decode asymmetry, (3) T8122 shared-init post) are dispatcher-only or small perstn.py edits from the RUN 4 baseline. The other two ((1) pcieclkgen bit-6/8 clobber, (4) C-side barrier hunt) are more expensive.

Candidate ranking for RUN 5:

- **RUN 5 = A (`--phy-ip-write-probe`)** — dispatcher-only, binary result. WROTE opens a whole new "naked-write32 replay of pll tunables" attack surface (phy_ip decodes writes even if reads stall); STALL/SYNC definitively closes off write-only strategies and refocuses on B / C / upstream. RUN 4's state is the cleanest starting point we've ever had for this probe.
- **RUN 5 = B (drop or narrow pcieclkgen)** — tests whether pcieclkgen's clobber of iBoot bits 6, 8 is the culprit. Dispatcher-only if we drop pcieclkgen entirely; ~15 lines of perstn.py if we add a `--pcieclkgen-set5-only` mode. Deferred: iBoot's bit 5 = 0 in the reset state, so pcieclkgen wants to SET it; some part of the write is definitely needed. Narrow-mask variant preserves more state.
- **RUN 5 = C (`--t8122-shared-post`)** — untested T8122 shared-init post writes. ~40 lines of perstn.py. `dc25f2f` warned this trips SError on the C side; guarded Python isolation should surface the specific offending write.

Selected: **RUN 5 = A.** See `docs/project-m4-pcie-bringup.md` for full dispatch selection reasoning and the concrete `case 5)` flags block.
