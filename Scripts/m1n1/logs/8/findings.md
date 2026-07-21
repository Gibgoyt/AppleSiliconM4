# RUN 8 findings

**Dispatch:** `./Scripts/m1n1/perstn-run.sh 8` (dispatcher `71323fd`).

**Hypothesis (candidate C, from `docs/project-m4-pcie-bringup.md` post-RUN-7
block):** bit 4 of `phy_shared+4` — set by T602X/T8122's
`set32(phy_base+4, 0x10)` (pcie.c:529), skipped by the T8140 codepath — is
the phy_ip decode gate on t8132. RUN L tested `--phy4-x10-early` alone and
wedged; RUN 8 combined it with the strongest baseline ever built. With the
RUN 8 flag set, the write order 6.i → 7.early → 6.5 reproduces T8122's
native pcie.c order 529 → 535 → 543-551, hoisted above the phy_ip tunables.

**Result: FALSIFIED.** The 6.i write landed exactly as designed, but step
6.g wedged IDENTICALLY to every RUN A..7. Same address
`phy_ip_base + 0x38 = 0x497040038`, same entry index #0, same error class
`UartTimeout: Expected 1 bytes, got 0 bytes`.

## The 6.i block: everything worked

`Scripts/m1n1/logs/8/nic-runtime.txt:3975-3977`:

    --- 6.i.set32(phy_shared+4, 0x10) [T8122 line 529, moved-early, RUN L] ---
      -> 17
    [guard] 6.i.set32(phy_shared+4, 0x10) [...]: exc_count delta = 0 (before=0, after=0)

Post-6.i reachable-scan (`nic-runtime.txt:4204`):

    +0x00=0xf3c0301f +0x04=0x00000011 +0x08=0x00000000 +0x0c=0x00000000

- `phy_shared+4: 0x00000001 → 0x00000011` — bit 4 STUCK, guard delta=0
- `phy_shared+0x8` UNCHANGED at `0x00000000` — the T8122 poll-target bit 0
  is not activated by the bit-4 write either
- `phy_shared+0` unchanged (`0xf3c0301f`) — no side effects on the control
  word

## The PMGR sweep: first dataset (NEW in RUN 8)

`nic-runtime.txt:5724-5765` — `--pmgr-pre6g-scan` matched **39 devices**
immediately before step 6.g. Key rows:

| Gate | Name | actual | Status |
|---|---|---|---|
| 135 | APCIE_GP | 0xf | ON |
| 136 | APCIE_SYS_GP | 0xf | ON (ps_auto=0xf) |
| 149 | APCIE_ST | 0xf | ON (was_clkgated) |
| 150 | APCIE_SYS_ST | 0xf | ON (ps_auto=0xf, was_pwrgated, was_clkgated) |
| 151 | APCIE_PHY_SW | 0xf | ON (ps_auto=0xf, was_clkgated) |
| 97-132 | ATC{0-3}_PCIE/CIO/CIO_PCIE/CIO_USB, DPTX_PHY | 0x0 | OFF (Type-C tunnels, unrelated to apcie) |
| 338-351, 380-383 | CIO*_RECONFIG-V, AUSB*_AONPCIE-V, APCIE-*-V | — | VIRTUAL (no PS reg) |

One DELTA vs Phase 0 (`nic-runtime.txt:5747`): gate 151 APCIE_PHY_SW
`actual 0x4 → 0xf` — this is the expected Phase D poke, not new signal.

**Conclusion: every APCIE-family power domain is ON at the wedge point.**
No `dev_disable`, `parent_off`, or `RESET` flags on any apcie/phy family
device. The PMGR/power-domain hypothesis (iv) is now WEAKENED — nothing
observable in PMGR blocks phy_ip.

## All other steps identical to RUN 7

- 5.8.b: `axi_sub5+0 = 0x00081f75`, iBoot bits 6, 8 preserved (held
  through the final scan, line 4250)
- 6.5: `phy_shared+0: 0xf3c0301f → 0xf3c0321f` (bit 9 STUCK, lines
  5282-5289); `phy_shared+0x8 = 0` before and after
- `rc_base+0 = 0x00040000`, `phy_common+0 = 0x80300001` post-7.early
- No SError, guard delta=0 on every access

## The wedge (identical to every prior RUN A..7)

`nic-runtime.txt:5766-5773` (end of log):

    ==> ABOUT TO TOUCH phy_ip_base FOR THE FIRST TIME.
    ...
    --- 6.g.tunables apcie-phy-ip-pll-tunables (slice-filtered) ---
        plan (29 entries):
          shared               = 29
        RAISED at #0 (shared) 0x497040038: UartTimeout: Expected 1 bytes, got 0 bytes

## What RUN 8 tells us

### Falsified
- **Bit 4 of `phy_shared+4` is NOT the phy_ip decode gate on t8132**, not
  even in combination with the full baseline (naked axi2af + bit-5-only
  pcieclkgen + CLK0/1 ACKed + RESET clear + T8140 marker + CLK_MODE=ON +
  phy_shared+0 bit 9). The hoisted-T8122-tail-in-native-order sequence
  (529 → 535 → 543-551) does not unlock phy_ip.
- **PMGR power state is not the blocker** (as far as PS registers show):
  all APCIE-family gates are ON at the wedge point.

### Confirmed
- Phase F walks all 10 pre-6.g steps cleanly, then wedges on the FIRST
  phy_ip touch. The wedge remains fabric-level (AXI stall / bidirectional
  decode-lock), not SError.

### Not yet ruled out
Ranked:
1. **T602X-style `set32(phy_shared+0, 0x300)` (bits 8+9) — candidate D,
   RUN 9 target.** RUNs 6/7/8 only ever set bit 9 (the T8122 0x200
   variant); **bit 8 has never been set on t8132**. pcie.c:549 is the
   last remaining un-replayed pre-phy_ip write variant in pcie.c.
2. **C-side vs Python sequencing gap — RUN 10 axis if D fails.** m1n1's C
   code issues the phy_ip tunable writes back-to-back with barriers; the
   proxy inserts multi-ms USB round-trips. Cheapest half-step: upload a
   tiny stub and `p.call()` it so the 29 pll-tunable writes execute
   on-CPU. Full step: port the port-1 slice filter into the m1n1 fork's
   `pcie_init_controller()` and let C run 6.g natively (rebuild/reflash).
3. **PMGR angle — weakened** by this run's sweep; only viable if some
   non-PS gating (clock tree, fabric routing table) exists outside the
   PS registers.

## RUN 9 plan

Full plan in `docs/project-m4-pcie-bringup.md` (post-RUN-8 state block).

**RUN 9 = candidate D (`--t8122-shared-post-val=0x300` on the RUN 8
baseline):** parametrizes the existing 6.5 block's write value (new
`--t8122-shared-post-val` flag, default `0x200`). Expected transition:
`phy_shared+0: 0xf3c0301f → 0xf3c0331f` (bits 8+9 SET in one write).

Single-variable delta vs RUN 8: only the 6.5 value changes. All
falsified-but-retained flags kept (`--phy4-x10-early`,
`--pcieclkgen-set5-only-to`, `--t8122-shared-post`); `--pmgr-pre6g-scan`
retained (free read-only data).

Success criterion: step 6.g stops wedging for the first time in 26+ boots.

Failure branches for RUN 10:
- Wedges at 6.g identically → candidate D falsified; the Python-replayable
  pcie.c write set is EXHAUSTED. RUN 10 = sequencing-gap axis (`p.call()`
  stub first, C-side native 6.g second).
- Wedges somewhere new / new fault class → high-signal; analyze first.
- SError inside 6.5 with 0x300 → bit 8 write faults where bit 9 didn't;
  high-signal.
