# RUN 6 findings

**Dispatch:** `./Scripts/m1n1/perstn-run.sh 6` (dispatcher `8024832`).

**Hypothesis (from `docs/project-m4-pcie-bringup.md:176-186`):** bit 9 of
`phy_shared+0` (`0x200`) is a phy_ip decode-enable gate T8140 doesn't have but
T8122/T8132 do. Setting `phy_shared+0 |= 0x200` (T8122's post-write from
pcie.c:543-551) between step 7 (phycmn-early) and step 6.g (first phy_ip
access) unlocks the phy_ip aperture that has been bidirectionally decode-locked
in every prior RUN.

**Result: FALSIFIED.** Bit 9 landed as a real hardware write (verified STUCK
by post-read; no SError; guard delta=0), but step 6.g wedges IDENTICALLY to
every RUN A..5. Same address `phy_ip_base + 0x38 = 0x497040038`, same entry
index #0, same error class `UartTimeout: Expected 1 bytes, got 0 bytes`.

## The 6.5 block: everything worked

`Scripts/m1n1/logs/6/nic-runtime.txt:4856-4868`:

    --- 6.5.T8122-shared-post replay (pcie.c:543-551, T8140 codepath skips) ---
        pre-read: phy_shared+0x8 = 0x00000000
        bit 0 CLEAR -- C-side poll would timeout at 250 ms; SKIPPING poll
        pre-write: phy_shared+0x0 = 0xf3c0301f (bit 9 = CLEAR)
      --- 6.5.set32(phy_shared+0, 0x200) [T8122 post-write] ---
        -> 4089459231
    [guard] 6.5.set32(...): exc_count delta = 0 (before=0, after=0)
        post-write: phy_shared+0x0 = 0xf3c0321f (delta=0x00000200, bit 9 = SET)
        post-write: phy_shared+0x8 = 0x00000000 (delta=0x00000000)
        RESULT: bit 9 STUCK -- phy_shared+0 now has T8122 post-write applied.

- `phy_shared+0x8 = 0` at entry (RUN 5's prediction confirmed)
- Poll correctly SKIPPED (saved 250 ms hang)
- `set32(phy_shared+0, 0x200)` STUCK cleanly
- Bit 9 verified SET post-write; delta = exactly `0x200`
- `phy_shared+0x8` unchanged post-write (bit 0 still CLEAR — the T8122 status
  bit is not caused by the bit-9 set on t8132)
- No SError, no exception, no exc_count delta at any point

Followed by full reachable-scan at `post-6.5.t8122-shared-post` (rc /
phy_common / phy_common+0x2a00-2c00 / phy_shared / axi / axi_sub5 / axi_sub6,
all offsets guarded delta=0) — cleanest post-6.5 state ever captured.

## The wedge (identical to every prior RUN A..5)

`nic-runtime.txt:5299-5306` (end of log):

    ==> ABOUT TO TOUCH phy_ip_base FOR THE FIRST TIME.
       Entries in the INACTIVE port-1 slice (0x10000..0x18000)
       will be SKIPPED to avoid the AXI stall observed on
       the 2026-07-11 run. See phy-ip-report for entry list.
    --- 6.g.tunables apcie-phy-ip-pll-tunables (slice-filtered) ---
        plan (29 entries):
          shared               = 29
        RAISED at #0 (shared) 0x497040038: UartTimeout: Expected 1 bytes, got 0 bytes

Same address, same error class, same entry index across RUNs A..6.

## What RUN 6 tells us

### Falsified
- **Bit 9 of `phy_shared+0` is NOT a phy_ip decode-enable gate on t8132.**
  We can set it (STUCK), it does not affect phy_ip decode. Hypothesis dead.
- **The T8122 shared-init post write is NOT the missing step** — at least,
  not the `set32(phy_shared+0, 0x200)` half of it.
- **Adjacent status bit `phy_shared+8` bit 0 is NOT triggered by bit 9 set.**
  So the T8122 poll-target-then-write handshake on t8132 needs some OTHER
  prerequisite that we haven't identified.

### Confirmed
- Phase F now walks steps 6.a → 6.b → 6.c → 6.d → 6.e → 6.f → 7
  (phycmn-early) → 6.5 (t8122-shared-post) cleanly, then wedges at 6.g on
  the FIRST phy_ip access. All 8 pre-6.g steps land guard delta=0.
- The wedge is fabric-level (AXI stall / decode-lock), not internal SError.
  phy_ip is never routed to a slave that can respond from ANY fabric state
  we've built.

### Not yet ruled out
Ranked by pcie.c evidence:
1. **pcieclkgen mask-RMW clobbers iBoot bits 6, 8 which are PLL enables.**
   Every RUN S/1/2/3/4/5/6 has done `mask32(sub5+0, 0x3e0, 0x220)` which
   clears iBoot bits 6, 8. If they're the PLL enables, phy_ip has no clock
   and every access AXI-stalls. **This is candidate B, RUN 7 target.**
2. `set32(phy_shared+4, 0x10)` — T602X/T8122-only write we skip; sets bit 4
   of phy_shared+4 (currently 0x01, would become 0x11). RUN L tested
   `--phy4-x10-early` in isolation and wedged, but never in combination with
   naked applies + t8122-shared-post. Queued as RUN 8 fallback.
3. T602X-style `set32(phy_shared+0, 0x300)` (bits 8+9 combined) — RUN 6 only
   set bit 9. Bit 8 alone (or bits 8+9 combined) untested. Queued as RUN 9
   fallback.
4. pcie.c:495-503 fuses to phy_ip — but these ARE phy_ip writes themselves,
   so they'd wedge the same way as the pll tunables. Not viable unless the
   first fuse write is somehow a special decode-priming write. Deferred.

## The pcieclkgen-set5-only hypothesis (RUN 7)

pcieclkgen's ADT entry:
- offset 0, mask `0x3e0` (bits 5-9), value `0x220` (bits 5, 9)

iBoot pre-programmed `axi_sub5+0 = 0x00081f55`. Breakdown of bits inside the
pcieclkgen mask (bits 5-9):

| Bit | iBoot value | pcieclkgen mask-RMW target | After RUN 6 mask-RMW |
|---|---|---|---|
| 5 | 0 | 1 (from value 0x20) | 1 (SET) |
| 6 | 1 | 0 (mask clears, value doesn't set) | 0 (CLEARED — iBoot bit LOST) |
| 7 | 0 | 0 | 0 |
| 8 | 1 | 0 (mask clears, value doesn't set) | 0 (CLEARED — iBoot bit LOST) |
| 9 | 1 | 1 (already, mask preserves via value bit 9) | 1 (SET) |

We LOSE bits 6, 8 on every RUN. If either is a PLL enable, the PLL doesn't
lock, and phy_ip has no clock. The fabric symptom (AXI stall = never routes)
matches "phy_ip has no clock" exactly.

RUN 7 = `set32(sub5+0, 0x20)`: sets only bit 5, preserves iBoot bits 6, 8.
Post-value would be `0x00081f75` = all iBoot bits + bit 5.

## RUN 7 plan

Full plan in `docs/project-m4-pcie-bringup.md` (post-RUN-6 state block).

**RUN 7 = candidate B (`--pcieclkgen-set5-only-to=axi_sub5_base`):** new
argparse flag, mutually exclusive with `--pcieclkgen-naked-apply-to`.
Replaces the pcieclkgen mask-RMW with a bit-5-only `set32(base+0, 0x20)`.
Same reachability gate (`axi_sub5_reachable`), same diag checkpoint
(`post-5.8.b.pcieclkgen-naked-apply`), same pre-read + step() + post-read
+ STUCK/PARTIAL/NO-OP classification pattern.

~15-25 lines of perstn.py. Single-variable delta vs RUN 6: pcieclkgen mode
switches from mask-RMW (mask=0x3e0 val=0x220) to bit-5-only (set32 0x20).
Everything else identical: `--naked-write-test-readonly`,
`--axi2af-naked-apply`, `--reachable-scan`, `--phycmn-early`,
`--t8122-shared-post` (RUN 6's fix stays even though it's not the ungate —
we now know it's a real STUCK write with no side effect, and dropping it
would be a second variable change).

Success criterion: step 6.g STOPS wedging at `phy_ip+0x38`. If it does, bits
6 or 8 were PLL enables and pcieclkgen's mask-RMW has been the confounding
variable across RUNs S/1/2/3/4/5/6. Massive breakthrough — new attack
surface: audit every mask-RMW tunable for iBoot-bit clobbers.

Failure branches for RUN 8:
- Wedges at 6.g identically → bits 6, 8 are not PLL enables. RUN 8 =
  candidate C (`--phy4-x10-early` on RUN 6/7 baseline).
- Wedges somewhere new → high-signal, examine wedge address.
- SError inside 5.8.b → bit-5-only write triggers fault the mask-RMW didn't
  (unlikely — mask-RMW was a superset of the writes; would be surprising).
