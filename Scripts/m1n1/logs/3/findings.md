## RUN 3 findings

RUN 3 = RUN 2 baseline but redirects `--cio3pllcore-naked-apply-to` from `axi_sub5_base` to `phy_common_base`. Tests the atc.c CIO3PLL analogy from `docs/ref-asahi-t8132-pcie.md` §8.1 (atc.c:882-884 applies its common tunables to `regs.core`; atc.c:112-114 places `CIO3PLL_CLK_CTRL` at `regs.core+0x2a00` and `CIO3PLL_DCO_NCTRL` at `+0x2a38`).

Full dispatch flags: `--no-pcie-init --preinit-probe --pmgr-enable --pmgr-per-port --gate-poke --phy-ip-probe --t8140-replay --phy-ip-diag --fuse-recon --naked-write-test --axi2af-naked-apply --pcieclkgen-naked-apply-to=axi_sub5_base --cio3pllcore-naked-apply-to=phy_common_base --reachable-scan --phycmn-early --phy-ip-diag-at=none`.

### 1. Step 5.8.c cio3pllcore apply on phy_common_base — 0 STUCK, 1 PARTIAL, 5 NO-OP, 1 SKIP-NOOP

From `nic-runtime.txt:3105-3144`:

| # | offset | mask     | value    | pre         | new         | post        | tag       |
|---|--------|----------|----------|-------------|-------------|-------------|-----------|
| 0 | 0x000  | 0xa0b    | 0xa01    | 0x80300000  | 0x80300a01  | 0x80300001  | PARTIAL   |
| 1 | 0x024  | 0xc00    | 0x800    | 0x00000000  | 0x00000800  | 0x00000000  | NO-OP     |
| 2 | 0x028  | 0xf00    | 0xb00    | 0x00000000  | 0x00000b00  | 0x00000000  | NO-OP     |
| 3 | 0x038  | 0x20     | 0x0      | 0x00000000  | —           | —           | SKIP-NOOP |
| 4 | 0x04c  | 0xff     | 0x94     | 0x00000000  | 0x00000094  | 0x00000000  | NO-OP     |
| 5 | 0x0e8  | 0xe0000  | 0x20000  | 0x00000000  | 0x00020000  | 0x00000000  | NO-OP     |
| 6 | 0x100  | 0xffffff | 0xb40b4  | 0x00000000  | 0x000b40b4  | 0x00000000  | NO-OP     |

Entry #0 PARTIAL: only bit 0 (CLK_MODE=ON) stuck. Bits 9 (`0x200`) and 11 (`0x800`) rejected. This is exactly what `--phycmn-early`'s `mask32(phy_common+0, 0x3, 0x1)` already sets — the PARTIAL result on #0 is redundant with a write we already do.

Entries #1-#6 all read back zero after write. Every guard `delta = 0` — no SError, no SYNC. `phy_common` at these offsets is either R/O or write-drop; it is definitively not the target block for cio3pllcore entries #1-#6.

### 2. `phy_common +0x2a00..+0x2c00` — 128 words of zero

From `nic-runtime.txt:3206-3366`, the widened reachable-scan window:

```
+0x2a00=0x00000000  +0x2a04=0x00000000  ...  +0x2a38=0x00000000  ...
+0x2b00=0x00000000  +0x2b04=0x00000000  ...  +0x2bfc=0x00000000
```

Zero across the entire range. The atc.c analogy that motivated RUN 3 (CIO3PLL_CLK_CTRL at core+0x2a00; DCO_NCTRL at core+0x2a38 exactly aligned with our `phy_ip+0x38` wedge address) does **not** map to phy_common on t8132. Either the block is unmapped in phy_common's window (reads return 0 with no fault) or there is genuinely no CIO3PLL structure at this offset in phy_common.

### 3. `axi_sub5` IS the real cio3pllcore target — and we are clobbering it

From `nic-runtime.txt:3413-3459`, the sub5 reachable-scan at post-5.8.c shows heavy iBoot pre-programming, matching a fully-configured PLL analog block:

```
sub5+0x00 = 0x00000a21  (RUN 3 mutated: iBoot 0x00081f55 -> naked-write 0x00000a01 -> pcieclkgen 0x00000a21)
sub5+0x04 = 0x00800000
sub5+0x08 = 0x00170001
sub5+0x0c = 0x0000008a
sub5+0x10 = 0x31cc0000
sub5+0x14 = 0x00000000
sub5+0x18 = 0x000001e1
sub5+0x1c = 0x0000262b
sub5+0x24 = 0x00000818
sub5+0x28 = 0x00000b18
sub5+0x2c = 0x00000010
sub5+0x30 = 0x0001e814
sub5+0x34 = 0x80000000  <-- bit 31 SET, persistently
sub5+0x38 = 0x0000451a
sub5+0x44 = 0x00310000
sub5+0x4c = 0x1f800094  <-- cio3pllcore #4 target 0x94 already in low byte
sub5+0x60 = 0xec040018
sub5+0x64 = 0x0b51991e
sub5+0x78 = 0x000015a0
sub5+0x7c = 0x00010005
sub5+0x80 = 0x00103fc0
sub5+0x84 = 0x0a283e80
sub5+0x88 = 0x00000909
sub5+0x8c = 0x7c3f8a05
```

Compare to the same offsets on phy_common (all zero except +0 and +0x48). Sub5 is clearly the CIO3 PLL analog register bank; phy_common is not.

**Destructive first-touch probe.** At `nic-runtime.txt:1567-1573`, the `--naked-write-test`'s sub5+0 first-touch probe:

```
--- axi_sub5_base+0x00 @ 0x495046200 ---
    source: RUN R sub5 first-touch: cio3pllcore #0 candidate target A
    pre=0x00081f55 wrote=0x00000a01 post=0x00000a01  [STUCK]
```

iBoot set `sub5+0 = 0x00081f55` (bits 0, 2, 4, 6, 8-12, 19 populated — a fully-programmed PLL control word). Our probe wrote `0x00000a01` with full-width mask, LOSING bits 2, 4, 6, 8, 10, 12, 19. Every RUN S / 1 / 2 / 3 has been running this same destructive probe, meaning **for the last five RUNs we've been trashing iBoot's PLL control register before Phase F even starts.**

### 4. Step 6.g — STILL wedges at `phy_ip_base+0x38`

From `nic-runtime.txt:5343-5348`:

```
==> ABOUT TO TOUCH phy_ip_base FOR THE FIRST TIME.
     Entries in the INACTIVE port-1 slice (0x10000..0x18000) ...
  --- 6.g.tunables apcie-phy-ip-pll-tunables (slice-filtered) ---
    plan (29 entries):
      shared               = 29
    RAISED at #0 (shared) 0x497040038: UartTimeout: Expected 1 bytes, got 0 bytes
```

Identical failure address (`0x497040038` = `phy_ip_base + 0x38`) and identical failure class (`UartTimeout`, m1n1 UART-froze / silent AXI stall) as every RUN A..2.

### 5. `--phycmn-early` write LANDED — but is now redundant with cio3pllcore #0

`nic-runtime.txt:3105-3114`'s cio3pllcore #0 already sets phy_common+0 bit 0 (PARTIAL). By the time step 7's `mask32(phy_common+0, 0x3, 0x1)` fires, bit 0 is already set. Both RUN 1 and RUN 3 confirm bit 0 stays through post-6.f. But this state has been reached in RUN I, RUN 1, RUN 3 with the same wedge outcome — CLK_MODE=ON is confirmed not the missing piece regardless of application path.

### 6. Naked-write-test cross-reference (from `nic-runtime.txt:1567-1622`)

For posterity, all first-touch write results on candidate cio3pllcore targets:

| target       | source                                       | pre         | wrote       | post        | tag       |
|--------------|----------------------------------------------|-------------|-------------|-------------|-----------|
| sub5+0x00    | RUN R sub5 first-touch (cio3pllcore #0 A)    | 0x00081f55  | 0x00000a01  | 0x00000a01  | STUCK*    |
| sub6+0x00    | RUN R sub6 first-touch (cio3pllcore #0 B)    | 0x00000239  | 0x00000a01  | 0x00000201  | PARTIAL   |
| rc_base+0x54 | RUN R R/W-bitmask readback                   | 0x00000140  | 0xffffffff  | 0xffffffff  | STUCK     |
| rc_base+0x100| RUN R cio3pllcore #6 rc_base candidate       | 0x00000000  | 0x000b40b4  | 0x00000000  | NO-OP     |
| rc_base+0x00 | cio3pllcore #0 (ADT-declared target)         | 0x00040000  | 0x00040a01  | 0x00040000  | NO-OP     |
| rc_base+0x24 | cio3pllcore #1                               | 0x00000000  | 0x00000800  | 0x00000000  | NO-OP     |
| rc_base+0x28 | cio3pllcore #2                               | 0x00000000  | 0x00000b00  | 0x00000000  | NO-OP     |
| rc_base+0x38 | cio3pllcore #3                               | 0x00000000  | 0x00000000  | 0x00000000  | STUCK\*\* |
| axi_base+0x00| axi2af #0                                    | 0x0000001c  | 0x8000001c  | 0x8000001c  | STUCK     |

\* Destructive: iBoot value clobbered. \*\* Trivial: wrote 0 to already-0.

## Signal that jumps out

**Target-block search for cio3pllcore is EXHAUSTED across all four candidate blocks:**

| block          | cio3pllcore result                                                       |
|----------------|--------------------------------------------------------------------------|
| `axi_sub5`     | RUN 2 — all 7 SKIP-NOOP (iBoot already programmed the block to match)    |
| `axi_sub6`     | RUN R first-touch — PARTIAL on #0 (bit 3 rejected)                       |
| `rc_base`      | RUN Q / RUN R — NO-OP on #0/#1/#2, only bit-0 SKIP-NOOP on #3            |
| `phy_common`   | RUN 3 — PARTIAL on #0 (only CLK_MODE bit 0), NO-OP on #1-#6              |

**cio3pllcore's true home is `axi_sub5`, iBoot has already programmed it to the target values, and any further attempt to apply the tunables via naked mask-RMW is either redundant (SKIP-NOOP) or destructive to the surrounding iBoot state.** The atc.c analogy for `phy_ip+0x38` = CIO3PLL_DCO_NCTRL does not extend to a discoverable Apple layout in phy_common's window.

Combined across RUNs A..3: the wedge at `phy_ip+0x38` has not moved for 21+ boots. Nothing tested has changed its failure mode except RUN J's momentary flip from silent-stall to `Exception: SYNC` — which RUN O later showed was an artifact of a phy_ip read hitting a different fault class, not evidence of state change.

## What's still unknown

1. **Is the naked-write-test's sub5+0 clobber breaking iBoot's PLL config?** `pre=0x00081f55 -> post=0x00000a01` destroys 5 bits set by iBoot (bits 2, 4, 6, 8, 12) that are outside cio3pllcore's target mask. If any of those bits is a PLL enable or a reference-clock select, we may be gating our own PLL off before Phase F ever runs. Untested: RUN S baseline without `--naked-write-test`.
2. **Does phy_ip decode POSTED writes when reads AXI-stall?** RUN P tested this from a broken-applicator baseline (no naked applies had landed); the RUN 1/3 state (naked axi2af + pcieclkgen landed, CLK_MODE=ON) has never been probed for phy_ip write-decode.
3. **Do the T8122 shared-init post writes T8140 skips matter?** `poll(phy_shared+0x8, 1, 1)` + `set32(phy_shared+0, 0x200)` at pcie.c:543-551 remain untested by any prior RUN.
4. **Why does the m1n1 C-side reach LTSSM BUSY** (per commit `6b277bc`) while our Python replay stalls on the first `phy_ip+0x38` read? Barrier / DSB / ISB ordering the compiler emits? Silent write-drop with no fault? Timing?

## Where we are strategically

Local naked-apply target-block brute-forcing is exhausted. All four candidate blocks (`sub5`, `sub6`, `rc_base`, `phy_common`) have been probed for cio3pllcore. No target-block permutation of `--cio3pllcore-naked-apply-to=...` remains that has a meaningful chance of moving the wedge.

Continuing with RUNs 4, 5, 6, ..., 99 that keep permuting target blocks is not productive; **the answer is not in that search space.** The next productive experiment is either upstream of everything we've tried (drop the destructive `--naked-write-test` probe) or lateral (test phy_ip write-decode / T8122 shared-init post).

See `docs/project-m4-pcie-bringup.md` for the ranked RUN 4 candidate list and dispatch selection.
