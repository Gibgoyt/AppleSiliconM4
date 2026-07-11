---
name: project-m4-pcie-bringup
description: Long-term project bringing PCIe up on Apple M4 mini (t8132/j773g) via a patched m1n1 fork, culminating in port-2 NIC training + TCP server
metadata:
  type: project
---

Multi-week project to get PCIe working on Apple M4 mini so the port-2 NIC can be trained and a TCP server run on top of it. Working directory: `/home/ahmed/Projects/C/embedded/AppleSiliconM4`. m1n1 fork: `~/Projects/AsahiLinux/m4/m1n1`.

**Why:** This is a from-scratch bring-up experiment, not a simple config change. m1n1's stock T8140 codepath (which pcie.c uses for t8132) is not sufficient on j773g; each iteration exposes another missing step. Ultimate goal: get to a working NIC and run a TCP server on it.

**How to apply:** Every Phase F wedge should be treated as "we learned another gate/tunable/clock is needed" rather than "the fix isn't working". Iteration is slow (needs m1n1 reboot between runs), so make progress by adding one well-instrumented probe per commit and per-step flush so the wedge log tells us exactly what to try next.

**State as of 2026-07-11 (post RUN D):**
- Phase F (T8140 replay in Python) runs cleanly steps 1..6.f in RUN A/B/C. RUN D added `--phy-ip-diag / --pmgr-explore / --dart-power`, and the diag F.entry probe itself wedged m1n1 with `Exception: SYNC` before any Phase F flush -- log ended at the DART section.
- `--pmgr-explore` exhausted: no OFF PMGR gate matching PCIE/PHY/APCIE/ANS/DART_APCIE that isn't ATC/CIO Thunderbolt (unrelated to on-package NIC).
- `--dart-power` is a dead path: `dart-apcie{0,2}` ADT nodes have no `clock-gates` property; `p.pmgr_adt_power_enable` returns -1 and does nothing.
- `guarded()` with `GUARD.SKIP|SILENT` does NOT catch this class of SYNC. The docstring implied it would; RUN D shows it doesn't.
- Ruled out as unblocking phy_ip: PMGR gates 150+151, phy_shared CLK0/CLK1 handshake, RC-side cio3pllcore/pcieclkgen tunables, phy_common CLK MODE=ON ordering, DART power path, hidden PMGR gate names.
- **Current lead: fuse-bits programming.** m1n1's pcie.c:496-503 programs phy_ip PHY-PLL calibration bits from OTP fuses on t8103/t6000/t8112 (`pcie_fuse_bits_t{8103,6000,8112}`), but sets `fuse_bits = NULL` for t8122/t8140/t6020/t6030/t6031/t8132 (pcie.c:284-318). This is the very first phy_ip write in each supported family's controller-init. Without it, the PHY-PLL never receives per-die calibration -> never locks -> any subsequent phy_ip write aborts. Matches RUN A/B/C's SYNC-at-first-phy_ip-write symptom exactly.
- Next iteration (RUN E, delivered 2026-07-11): diag F.entry replaced with a phy_common+0 probe (safe alive check, phy_common was proven reachable in RUN A/B/C step 5); new `--fuse-recon` flag scours the ADT for the fuse-programming source m1n1 is missing.

**State as of 2026-07-11 (post RUN E):**
- RUN E validated the diag fix: F.entry no longer crashes; phy_common+0 REACHABLE (val=0x80300000). First run to flush past the DART section into Phase F.
- Step 1 (`pmgr_adt_power_enable /arm-io/apcie`) does NOT ungate phy_ip on t8132. post-1.pmgr phy_ip+0 read AXI-stalled m1n1 (UartTimeout, not SYNC). Definitive: pmgr enable alone is not the ungate.
- `--fuse-recon` came up empty: 0 fuse-matching properties among 30 on `/arm-io/apcie`, 25/25 reg[] mapped, all 5 well-known fuse paths absent. Cross-ref had a shape bug (`parse_tunables_container` returns 4-tuples, recon read `.offset` attribute) so reported 0/9 hits even though 0x1204 IS in the shared slice.
- Design constraint learned: any phy_ip probe against unclocked hardware permanently wedges m1n1 (`guarded()` docstring at `perstn.py:100-106` documented this). Multi-checkpoint diag was self-defeating; RUN E killed itself at post-1.pmgr and never got to see post-5/6.b/6.d/6.f.
- Next iteration (RUN F, delivered 2026-07-11): `--phy-ip-diag-at=<checkpoint>` for single-shot phy_ip probes (bisect across boots, default post-6.f.T8140-marker). Cross-ref bug fixed. fuse-recon broadened: full `/arm-io/apcie` property list, `/arm-io/` children matching /fuse|otp|efuse|chip.?id|calib/i, and `/chosen` properties matching /fuse|otp|calib|phy|pcie/i.

**State as of 2026-07-11 (post RUN F):**
- **Definitive:** T8140 codepath (Phase F steps 1..6.f) does NOT ungate phy_ip on t8132. All 6 `[phy-common-diag @ ...]` REACHABLE with val=0x80300000 unchanged; `[phy-ip-diag @ post-6.f.T8140-marker]` AXI-stalled m1n1. Every T8140 shared-init step completed with delta=0.
- phy_common+0 = 0x80300000: bit 31 (`CLK_100MHZ`) set (ref clock UP), bits 20-21 set (undocumented in m1n1), `CLK_MODE=00` (step 7 hasn't run). T8140 codepath doesn't touch phy_common at all.
- Fuse-recon with bug fix: cross-ref 1/9 hit (`0x1204` confirms partial t8112 family compat). 0 hits among 138 `/arm-io/` children matching fuse|otp|calib. `/chosen` has only irrelevant matches (`esdm-fuses=0`, `bootp-response`). No ADT source of PCIe fuse calibration on t8132.
- New hypothesis: pcie.c XOR between T81XX PHYIF_CTRL_RUN write (rc_base + 0x024 <- BIT(0), pcie.c:487) and T8140 marker (phy_shared + 4 <- 0x01, pcie.c:492). Both marked `/* ??? */` in m1n1. t8132 uses T8140 branch (regs_t8140 pin) so only the marker gets written. Test: also do the T81XX write.
- Next iteration (RUN G, delivered 2026-07-11): `--phyif-ctrl-run` flag adds the T81XX PHYIF_CTRL_RUN write between step 6.f and the post-6.f diag. fuse-recon dumps the full 138-name `/arm-io/` child list for eyeball scan.

**State as of 2026-07-11 (post RUN G):**
- Step 6.f.5 (`--phyif-ctrl-run`: T81XX PHYIF_CTRL_RUN write at rc_base+0x024) completed cleanly. Write returned 1 (was 0, OR'd BIT(0)), exc_delta=0, guard OFF succeeded.
- Immediately after, m1n1 died. Nothing after the step 6.f.5 post-flush (36174 bytes) reached disk -- not even the `[phy-common-diag @ post-6.f.T8140-marker]` header. Either `p.get_exc_count()` hung indefinitely inside the phy_common probe, or m1n1 became UART-unresponsive in the ~1 ms gap.
- **Conclusion:** the PHYIF_CTRL_RUN write is NOT benign on t8132. It materially changes state. Whether it ungates phy_ip or destabilizes the fabric is currently indeterminate.
- fuse-recon `/arm-io/` full child dump (138 names): no fuse/otp block, no obviously overlooked candidate. Notable non-target finds: `lan-10gb-sync` (on-package NIC, relevant for later work), `apciec0/1/3` (CIO PCIe, unrelated). No hidden ADT source of PCIe calibration.
- Next iteration (RUN H, delivered 2026-07-11): (i) flush-after-header inside probe_phy_common_reachability / probe_phy_ip_reachability -- header lands on disk before the first proxy call so a hang reveals which probe entered; (ii) guarded `read32(rc_base + 0x024)` with individual entry+result flushes before AND after the RUN write. Combined output disambiguates whether the write's fault is immediate (post-read hangs) or delayed (post-read fine, phy_common probe entered but hangs).

**Related memories:** [[ref-m4-repos]] (repo paths + tooling)
