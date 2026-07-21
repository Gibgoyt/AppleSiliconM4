# RUN 7 findings

**Dispatch:** `./Scripts/m1n1/perstn-run.sh 7` (dispatcher `92b7e29`).

**Hypothesis (candidate B, from `docs/project-m4-pcie-bringup.md` post-RUN-6
block):** pcieclkgen's mask-RMW (`mask32(sub5+0, 0x3e0, 0x220)`) has been
clearing iBoot bits 6, 8 of `axi_sub5+0` on every RUN S/1/2/3/4/5/6 (both
bits are inside the 0x3e0 mask; value 0x220 supplies 0 for them). If either
bit is a PLL enable, phy_ip has no clock and every access AXI-stalls —
matching the persistent wedge signature exactly. RUN 7 replaced the mask-RMW
with `--pcieclkgen-set5-only-to=axi_sub5_base`: a bit-5-only
`set32(sub5+0, 0x20)` that preserves all iBoot bits.

**Result: FALSIFIED.** The bit-5-only write landed exactly as designed —
first boot ever with iBoot bits 6, 8 intact end-to-end through Phase F —
yet step 6.g wedged IDENTICALLY to every RUN A..6. Same address
`phy_ip_base + 0x38 = 0x497040038`, same entry index #0, same error class
`UartTimeout: Expected 1 bytes, got 0 bytes`.

## The 5.8.b block: everything worked

`Scripts/m1n1/logs/7/nic-runtime.txt:2642-2648`:

    --- 5.8.b.pcieclkgen-set5-only (RUN 7: bit-5-only set32 to axi_sub5_base, preserving iBoot bits 6, 8) ---
      pre:  axi_sub5_base+0 = 0x00081f55  bit5=CLEAR  bit6=SET  bit8=SET  bit9=SET
    --- 5.8.b.set32(axi_sub5_base+0, 0x20) [bit-5-only] ---
      -> 532341
    [guard] 5.8.b.set32(axi_sub5_base+0, 0x20) [bit-5-only]: exc_count delta = 0 (before=0, after=0)
      post: axi_sub5_base+0 = 0x00081f75  bit5=SET  bit6=SET  bit8=SET  bit9=SET  (delta=0x00000020)
      RESULT: bit 5 STUCK cleanly; iBoot bits 6, 8 preserved (goal achieved -- single-bit delta vs RUN 4)

- Pre-value confirmed the iBoot control word `0x00081f55` (bits 6, 8 SET)
- Bit 5 STUCK; delta exactly `0x20`; guard delta=0; no SError
- `axi_sub5+0 = 0x00081f75` held through every later reachable-scan
  checkpoint (post-6.b, post-6.d, post-7, post-6.5)

## All pre-6.g steps clean (guard delta=0 throughout)

| Step | Result | Log line |
|---|---|---|
| 6.b CLK0ACK poll | `conv=True val=0xf3c03095 (0.1 ms)` | `nic-runtime.txt:3089` |
| 6.d CLK1ACK poll | `conv=True val=0xf3c0309f (0.1 ms)` | `nic-runtime.txt:3529` |
| 6.e RESET clear | ok, guard delta=0 | `nic-runtime.txt:3963-3965` |
| 6.f T8140 marker | `phy_shared+4: 0x00000000 -> 0x00000001` STUCK | `nic-runtime.txt:3969-3974` |
| 7.early phycmn MODE_ON | `phy_common+0 = 0x80300001` STUCK | post-7 diag |
| 6.5 T8122 post-write | `phy_shared+0: 0xf3c0301f -> 0xf3c0321f` (bit 9 STUCK) | `nic-runtime.txt:4845-4854` |

`phy_shared+0x8 = 0x00000000` before and after 6.5 — unchanged, as in RUN 6.

## The wedge (identical to every prior RUN A..6)

`nic-runtime.txt:5288-5295` (end of log):

    ==> ABOUT TO TOUCH phy_ip_base FOR THE FIRST TIME.
       Entries in the INACTIVE port-1 slice (0x10000..0x18000)
       will be SKIPPED to avoid the AXI stall observed on
       the 2026-07-11 run. See phy-ip-report for entry list.
    --- 6.g.tunables apcie-phy-ip-pll-tunables (slice-filtered) ---
        plan (29 entries):
          shared               = 29
        RAISED at #0 (shared) 0x497040038: UartTimeout: Expected 1 bytes, got 0 bytes

## What RUN 7 tells us

### Falsified
- **Bits 6, 8 of `axi_sub5+0` are NOT phy_ip PLL/clock enables** (or at
  least not sufficient). Preserving them changed nothing about phy_ip
  decode.
- **The pcieclkgen mask-RMW clobber was NOT the confounding variable**
  across RUNs S/1/2/3/4/5/6. The sub5+0 iBoot-bit-preservation axis is
  dead: RUN 4 preserved 8 of 10 bits, RUN 7 preserved all of them plus set
  bit 5 — identical wedge both times.

### Confirmed
- Phase F walks all 9 pre-6.g steps cleanly (5.8.b → 6.a → 6.b → 6.c →
  6.d → 6.e → 6.f → 7 → 6.5), guard delta=0 on every access, then wedges
  on the FIRST phy_ip touch. Strongest fabric state ever built: naked
  axi2af + bit-5-only pcieclkgen (iBoot bits intact) + CLK0/1 ACKed +
  RESET clear + T8140 marker + CLK_MODE=ON + phy_shared+0 bit 9 SET.
- The wedge remains fabric-level (AXI stall / bidirectional decode-lock),
  not SError. phy_ip is never routed to a live slave from ANY state we
  have built.

### Not yet ruled out
Ranked by pcie.c evidence:
1. **`set32(phy_shared+4, 0x10)` — candidate C, RUN 8 target.**
   T602X/T8122-only write (pcie.c:529) that the T8140 codepath skips;
   would take `phy_shared+4` from `0x01` (post-6.f) to `0x11`. RUN L
   tested `--phy4-x10-early` in isolation and wedged, but never combined
   with the current baseline (naked applies + t8122-shared-post +
   phycmn-early). Notably, running it in this baseline reproduces T8122's
   native relative order pcie.c:529 → 535 → 543-551, hoisted above the
   phy_ip tunables.
2. **T602X-style `set32(phy_shared+0, 0x300)` (bits 8+9) — candidate D,
   RUN 9 fallback.** RUN 6/7 only ever set bit 9; bit 8 has never been
   set on t8132.
3. **C-side vs Python sequencing gap.** m1n1's C code issues these MMIO
   ops back-to-back with barriers; the proxy inserts multi-ms USB
   round-trips between every access. Testable by porting the port-1
   slice filter into the m1n1 fork's pcie.c and letting C run 6.g
   natively, or by uploading a stub and `p.call()`ing it.
4. **PMGR/power-domain.** Some phy/auspma/cio-family PS register not at
   ACTUAL=0xf at the wedge point. RUN 8 adds a read-only pre-6.g PMGR
   sweep (`--pmgr-pre6g-scan`) to collect this data at zero risk.

## RUN 8 plan

Full plan in `docs/project-m4-pcie-bringup.md` (post-RUN-7 state block).

**RUN 8 = candidate C (`--phy4-x10-early` on the RUN 7 baseline):**
dispatcher-only for the hypothesis (the flag and its 6.i.early apply block
already exist in perstn.py from RUN L; placement after 6.f and before
7.early/6.5/6.g is exactly the candidate-C slot). Expected transition:
`phy_shared+4: 0x00000001 -> 0x00000011` (RUN L proved the write sticks
without faulting). Plus a new read-only `--pmgr-pre6g-scan` diagnostic
(PS-register sweep of APCIE/PCIE/PHY/AUSPMA/CIO-named PMGR devices just
before 6.g, diffed against the Phase 0 boot-time readout).

Single-variable delta vs RUN 7: only the `phy_shared+4 |= 0x10` write is
added. `--t8122-shared-post` and `--pcieclkgen-set5-only-to` are retained
even though falsified as ungates (dropping either would be a second
variable change).

Success criterion: step 6.g stops wedging for the first time in 25+ boots.

Failure branches for RUN 9:
- Wedges at 6.g identically → candidate C falsified. RUN 9 = candidate D
  (`set32(phy_shared+0, 0x300)`; parametrize the 6.5 value, e.g.
  `--t8122-shared-post-val=0x300`).
- Wedges somewhere new / new fault class → high-signal; analyze first.
- SError inside 6.i itself → bit 4 interacts with the combined state in a
  way RUN L's isolated test didn't show; analyze before proceeding.
- If C and D both fail → the Python-replayable pcie.c write set is
  exhausted; pivot to the sequencing-gap axis (C-side native 6.g with the
  port-1 filter) and the PMGR data from the pre-6.g sweep.
