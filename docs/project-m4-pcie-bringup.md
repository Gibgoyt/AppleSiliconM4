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

**Related memories:** [[ref-m4-repos]] (repo paths + tooling)
