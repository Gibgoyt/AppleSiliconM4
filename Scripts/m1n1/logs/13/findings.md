# RUN 13 findings

**Dispatch:** `./Scripts/m1n1/perstn-run.sh 13` (dispatcher `16c219f`).

**Result: STALE BINARY — no hypothesis tested.** The tunables-skip m1n1
(fork `7728fb0`) was staged at `/tmp/m4-serve/` but **never enrolled**:

- Banner (`run.log:38`): `m1n1 v1.6.0-rc1-56-g6b277bc-dirty` — the RUN
  11/12 binary. The expected new banner was `…-57-gb404263-dirty`.
- The new `pcie: t8132: skipping phy-ip tunables` printf appears nowhere.
- Diff vs RUN 12: pre-init state byte-identical (same PMGR gate values,
  same Phase 0/0.5/A/D outputs; only counter/timing noise).

RUN 13 was therefore a byte-identical re-run of RUN 12: same wedge
(`pcie: Initializing t8132 PCIe controller` → UartTimeout), same known
cause (the old binary still applies the phy-ip tunables pre-port-init,
the 6b277bc ordering bug). **The ordering fix remains untested.** The
new 60 s timeout fired correctly; the 30 s recovery-probe outcome is
missing from the log (ends at "post-timeout recovery probe…", likely
interrupted).

## Hardening for RUN 14 (so this cannot recur)

1. **`--require-build=SUBSTR`** (perstn.py): reads the m1n1 USB product
   string via sysfs (`/sys/class/tty/<dev>/device/../product`, e.g.
   `m1n1 uartproxy v1.6.0-rc1-59-g` — truncated ~30 chars, so match on
   the commit count `rc1-59-g`) and **aborts with exit 2 before any
   device state change** on mismatch.
2. **Flushed C-side breadcrumbs** (m1n1 fork `8a569ad`): `PCIE_BC(...)`
   = `printf + iodev_console_flush()` — the exception-handler delivery
   mechanism (iodev.c: spins `usb_dwc3_handle_events` until the TX ring
   drains) — at every `pcie_init_controller` stage: ADT parse, pmgr
   enable, axi2af/common/phy tunables, per-phy CLK handshake, the t8132
   phy-ip skip, shared rc handshake, per-port init entry/exit (with
   LINKSTS), controller done. If `pcie_init` ever wedges again, the last
   breadcrumb names the exact stage despite the console-ring buffering
   that hid the RUN 12/13 wedge location.
3. **Recovery probe now logs each iteration** and flushes at start, so
   its outcome always survives.
4. **`--gate-poke` dropped from RUN 14**: Phase D's direct gate-151 poke
   was the one state-changing pre-init step the proven pcie_up_1 boot
   (`p.pcie_init() -> 0`, 2026-07-10) did not have; `pcie.c:425` does
   its own `pmgr_adt_power_enable`. RUN 14's pre-init state = pcie_up_1
   + read-only readouts only.

## RUN 14 plan

`./Scripts/m1n1/perstn-run.sh 14` after enrolling m1n1 `8a569ad`
(banner `v1.6.0-rc1-59-g8a569ad` — clean describe, trivially
verifiable). Flags: `--preinit-probe --tier3 --post-init-phy-ip
--require-build=rc1-59-g`. Experiment unchanged from RUN 13's design;
interpretation matrix unchanged (see `docs/project-m4-pcie-bringup.md`
post-RUN-12/13 blocks):
- `pcie_init -> 0` + post-init pll lands + Tier 3a phy_ip live →
  auspma → LINKSTS → LTSSM kick → ECAM walk (NIC vendor/device ID =
  goal).
- Wedge with tunables skipped → the breadcrumbs name the exact stage.
- Post-init pll wedge → diff vs the 2026-07-11 environment.
