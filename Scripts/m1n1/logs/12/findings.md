# RUN 12 findings

**Dispatch:** `./Scripts/m1n1/perstn-run.sh 12` (dispatcher `febe8e8`) on the
patched m1n1 (`b404263` port-slice filter, banner
`v1.6.0-rc1-56-g6b277bc-dirty`).

**Goal:** full C-side `p.pcie_init()` with Phase B defused (straight to the
C init).

**Result:** `pcie: Initializing t8132 PCIe controller` printed, then m1n1
went silent — `p.pcie_init raised: UartTimeout` (`run.log:195-197`). No
filter printout, no port init, no dumps. Log ends at the `pcie-init` flush
(33370 bytes).

## Why the wedge looked impossible — and isn't

The next expected print, `pcie: ADT uses %d reg entries per port`
(pcie.c:502), never appeared — yet everything between the two printfs is
pure ADT/RAM parsing that cannot hang. Resolution: **console output during
a proxy request is buffered** (drained by the proxy main loop; only the
first line escapes the FIFO synchronously). The "ADT uses" line was stuck
in the buffer; the CPU executed past it and hung later, at the first real
phy_ip MMIO.

## The archaeology that settles it

- **`Scripts/m1n1/logs/pcie_up_1.log`** (2026-07-10, pre-`6b277bc` m1n1 —
  NO phy-ip tunables in C; identical pre-init sequence SMC gP0d/gP1a →
  CLKREQ assert → PERSTN cold reset): **`p.pcie_init() -> 0`** (line 552).
  Completed, per-port bring-up included, ports at LINKSTS BUSY
  (`0x8300020c`/`0x83000204`).
- **`Scripts/m1n1/logs/pcie_up_2.log`** embeds the `6b277bc` diff in its
  header — it was the **first test of 6b277bc** (which added the phy-ip
  tunables to the T8140 path at pcie.c:518). Its result (lines 229-232):
  `pcie: Initializing t8132 PCIe controller` → UartTimeout — **the exact
  RUN 12 signature**, on 2026-07-10, before any of our patches.

## Corrected conclusion: 6b277bc is an ORDERING bug on t8132

- phy_ip does not decode until per-port bring-up has run (pcie.c:571-852)
  — established by RUNs A..10 (30 wedges from every pre-port-init state)
  and by the 2026-07-11 evidence (post-`pcie_init` C-applicator walked all
  29 pll entries cleanly).
- `6b277bc` applies the phy-ip tunables at pcie.c:518, **before** the
  per-port loop → the first shared pll entry at `phy_ip+0x38` AXI-stalls
  the fabric inside `pcie_init`, with the intermediate printfs lost in the
  console buffer. That is what pcie_up_2 hit, what motivated
  `--no-pcie-init`, and what RUN 12 reproduced.
- The port-slice filter (`b404263`) fixed a real but **second-order** bug
  (absent-port slices — the 2026-07-11 6.h killer). It could not help
  RUN 12 because the 29 **shared** entries still fire pre-port-init.

## RUN 13 plan (the ordering fix — every piece proven on this machine)

Full plan in `docs/project-m4-pcie-bringup.md` (post-RUN-12 block).

1. **m1n1 fork `7728fb0`:** t8132's `pcie_init` SKIPS the phy-ip tunables
   entirely (prints `pcie: t8132: skipping phy-ip tunables pre-port-init`)
   → restores the pcie_up_1 behavior that returned 0. The filtered
   applicator stays in the tree as the in-C fix candidate once the exact
   ungating per-port step is identified.
2. **perstn.py `--post-init-phy-ip`:** after `pcie_init` returns, apply
   pll via `p.tunables_apply_local(path, "apcie-phy-ip-pll-tunables", 3)`
   (the 2026-07-11 recipe) and auspma Python-side with the port-slice
   filter; then the tier-3 dumps (Tier 3a phy_ip harvest verifies the
   tunables landed), LTSSM kick, and ECAM walk.
3. **`p.pcie_init()` robustness:** 60 s UART timeout for the call + 30 s
   post-timeout liveness polling (a slow init is not a dead init; the
   short timeout also desyncs the proxy).

Outcome branches:
- `pcie_init -> 0` + post-init pll lands + Tier 3a live → apply auspma,
  LTSSM kick, ECAM walk. **NIC vendor/device ID in ECAM = goal.** Ports
  still BUSY after tunables+kick → RUN 14 = re-run per-port init/LTSSM
  after tunables (possibly the in-C reorder).
- `pcie_init` still wedges with tunables skipped → ordering story
  falsified; the 60 s/recovery data + port breadcrumbs locate the real
  wedge.
- Post-init pll apply wedges → phy_ip needs more than per-port init on
  this boot path; diff against the 2026-07-11 environment.
