# RUN 15 findings

**Dispatch:** `./Scripts/m1n1/perstn-run.sh 15` (dispatcher `4317cc3`) on
m1n1 `8a569ad`. Banner + `require-build OK` both good.

**Result: two tooling interactions — the 2026-07-11-recipe hypothesis is
STILL untested.** Plus one major positive: golden-state parity confirmed.

## What worked

- `p.pcie_init() -> 0` again; TTY breadcrumb sequence identical to RUN 14
  (`run.log:193-213`), ports failed idle as expected.
- **The new early tier-1 dump captured post-init port state for the first
  time — and it is register-identical to pcie_up_1's golden dump:**

  | Register | pcie_up_1 (2026-07-10) | RUN 15 | Match |
  |---|---|---|---|
  | port0 LINKSTS | 0x8300020c | 0x8300020c | ✓ |
  | port2 LINKSTS | 0x83000204 | 0x83000204 | ✓ |
  | port0 PHY_CTRL | 0x2300066f | 0x2300066f | ✓ |
  | port0 phy+0x4 | 0x00000030 | 0x00000030 | ✓ |
  | port2 PHY_CTRL | 0x2300066f | 0x2300066f | ✓ |
  | port2 phy+0x4 | 0x00000030 | 0x00000030 | ✓ |
  | PHYCMN_CLK | 0x80300001 | 0x80300001 | ✓ |

  Also captured: APPCLK `0x00100101`, STATUS `0x00000005` on both ports.
  The current boot reaches EXACTLY the state from which the 2026-07-11
  boot proceeded to decode phy_ip.

## Tooling interaction 1: Phase F post-init silently skipped

`nic-runtime.txt:519-520`:

    === Phase F: T8140 controller-init replay (pcie.c) ===
      SKIPPED: Phase D did not confirm gate 151 ACTIVE.

Phase F's entry check (perstn.py:3168) requires `apcie.phaseD_gate151_
active` — a **python-side flag that only Phase D (`--gate-poke`) sets**,
and RUN 15 deliberately dropped `--gate-poke`. The flag does not reflect
hardware: post-`pcie_init` the gates ARE active (the C-side
`BC pmgr power enable done` breadcrumb proves the pmgr walk succeeded).
(Note: an earlier "Phase F only checks phaseE_rc_axi_ok" verification
missed this check due to a truncated grep — lesson noted.)

## Tooling interaction 2: unconditional Tier 3a wedged the boot

With Phase F skipped, the flow fell through to the full tier-3 dump. The
Tier 3a phy_ip harvest ran unconditionally and its first read wedged:

    read32(0x497040000) [phy_ip +0x0000]... FAILED: UartTimeout
    m1n1 DEAD -- bailing from dump

This cost the boot the LTSSM kick and ECAM sections.

## Fixes for RUN 16 (both python-side, no reflash)

1. **Post-init PMGR gate verification** before the Phase F call: re-read
   every apcie power-gate PS register (safe PMGR reads, same helpers as
   Phase B); if all real gates ACTIVE, set `phaseD_gate151_active=True`
   from hardware truth so Phase F actually runs.
2. **Tier 3a gated on `phaseF_shared_up`** (Phase F full success,
   perstn.py:4288): a skipped/failed Phase F boot now stays alive
   through the rest of tier 3, the LTSSM kick, and the ECAM walk.

## RUN 16 plan

`./Scripts/m1n1/perstn-run.sh 16` — same flags as RUN 15
(`--preinit-probe --tier3 --t8140-replay-post-init
--require-build=rc1-59-g`). Matrix unchanged: Phase F post-init →
6.g 29 pll entries → 6.h auspma filtered → 7-10 → `PHASE F SUCCESS` →
Tier 3a harvest → LTSSM kick → ECAM (NIC vendor/device ID = goal); a
6.g wedge → diff the d664bd9-era port-body pre-idle phy writes
(pcie.c:836-843) vs `8a569ad` / bisect; an earlier step failure → its
label names it and the boot survives to harvest everything else.
