# RUN 11 findings

**Dispatch:** `./Scripts/m1n1/perstn-run.sh 11` (dispatcher `a63f492`) on the
PATCHED m1n1 (fork commit `b404263`, t8132 port-slice filter), enrolled via
1TR `kmutil configure-boot --raw --entry-point 2048 -c /tmp/m1n1.bin`.

**Goal:** first run of the full C-side `p.pcie_init()` with the port-slice
filter — the known-good init path (ran to completion on 2026-07-11) with
its one known-fatal bug (port-1 auspma slice writes) fixed.

**Result: TOOLING BUG, experiment never ran.** The boot died at legacy
Phase B with `Exception: SYNC` printed by m1n1, **before `p.pcie_init()`
executed**. The C-side filter is still untested. This is NOT a hypothesis
falsification.

## Deployment verification: the patched m1n1 WAS enrolled

- Banner (`run.log:38`): `m1n1 v1.6.0-rc1-56-g6b277bc-dirty` — the `-dirty`
  suffix is the patched build (compiled from the working tree at `6b277bc`
  before the patch was committed as `b404263`). RUN 10's banner had no
  `-dirty`.
- `strings` on `build/m1n1.bin` / staged machos all contain the filter's
  printout text ("absent-port phy_ip slices"); the served artifacts match
  the build outputs (md5-verified).
- The raw-bin `kmutil --raw --entry-point 2048` enrollment flow works.

## The wedge

`run.log` tail:

    [pcie_up] Phase D: direct PS-register poke for gate 151...
    [pcie_up] [flush:phaseD-gate151-poke] wrote partial log (33305 bytes) ...
    [pcie_up] Phase B: p.pmgr_adt_power_enable('/arm-io/apcie') + shared MMIO...
    TTY> Exception: SYNC

Nothing after. `nic-runtime.txt` ends at the Phase D completion (line 486)
— Phase B never flushed, so its partial output was lost (fixed for RUN 12:
Phase B now flushes before/after the pmgr call and before the MMIO sweep).

## Root cause

Phase B (`probe_phaseB_apcie_pmgr`, perstn.py) is an early-era recon phase.
With BASE_FLAGS it runs AFTER Phase F in the script flow — and Phase F
killed m1n1 in every RUN 1-10, so Phase B had not executed against a live
m1n1 in ~20 runs. RUN 11's flag set (no `--t8140-replay`) reached it alive
for the first time, and its "shared MMIO" probe list contained four phy_ip
reads that predate the wedge discipline:

    (apcie.phy_ip_base + 0x00,    "phy_ip +0x00 (PLL area head)"),
    (apcie.phy_ip_base + 0x8000,  "phy_ip +0x08000 (port 0 slice head)"),
    (apcie.phy_ip_base + 0x10000, "phy_ip +0x10000 (port 1 slice head -- INACTIVE)"),
    (apcie.phy_ip_base + 0x18000, "phy_ip +0x18000 (port 2 slice head)"),

RUNs A..10 established that phy_ip must not be touched before the C-side
init has run. (`pmgr_adt_power_enable` itself is proven safe — Phase F
step 1 ran it after the identical Phase D poke in RUNs 1-10.) The state at
the wedge (PERSTN/CLKREQ poked, gate 151 ACTIVE, apcie gates enabled, NO
tunables, NO CLK handshake) produced a SYNC exception rather than the
usual silent AXI-stall — consistent with the old RUN J observation that
phy_ip's fault mode varies with fabric state.

Everything before Phase B was identical to RUN 10's boot baseline (same
Phase 0 gate values, same ADT map; only convergence-timing noise).

## Fixes applied for RUN 12

1. **perstn.py Phase B defused:** the four phy_ip probes removed from the
   sweep (replaced by a logged SKIP note); the `pmgr_adt_power_enable`
   call wrapped in `guarded()`; flush points added (`phaseB.pre-pmgr-
   enable`, `phaseB.post-pmgr-enable`, `phaseB.pre-shared-mmio`) so Phase
   B output survives any future wedge.
2. **RUN 12 dispatcher:** drops `--pmgr-enable`/`--pmgr-per-port` entirely
   — Phase B/C are skipped; `pcie.c:425` does its own
   `pmgr_adt_power_enable`, so nothing is lost and fewer live phases run
   before the C init (closer to the 2026-07-11 environment).

## RUN 12 plan

`./Scripts/m1n1/perstn-run.sh 12` — flags `--preinit-probe --gate-poke
--tier3`. **No reflash needed** (patched m1n1 already enrolled). Same
interpretation matrix as RUN 11:

- `pcie: Initializing t8132 PCIe controller` + filter printout
  (`apcie-phy-ip-auspma-tunables: applied N, skipped M (absent-port
  phy_ip slices)`, M > 0) + `p.pcie_init()` returns + Tier 3a phy_ip
  harvest reads live → C wedge fixed; check per-port LINKSTS (port 2 =
  NIC training = jackpot; stuck BUSY → live phy_ip dump to diff for the
  unlock register).
- `pcie_init` wedges at a NEW C-side address → first new C-side wedge
  data; identify via the UART log's last line.
- `pcie_init` returns but phy_ip still unreadable → per-port-unlock
  hypothesis falsified → fallback: `--pcieclkgen-naked-apply-to=rc_base
  --cio3pllcore-naked-apply-to=rc_base`.
