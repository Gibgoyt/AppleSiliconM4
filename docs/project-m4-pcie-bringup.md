---
name: project-m4-pcie-bringup
description: Long-term project bringing PCIe up on Apple M4 mini (t8132/j773g) via a patched m1n1 fork, culminating in port-2 NIC training + TCP server
metadata:
  type: project
---

Multi-week project to get PCIe working on Apple M4 mini so the port-2 NIC can be trained and a TCP server run on top of it. Working directory: `/home/ahmed/Projects/C/embedded/AppleSiliconM4`. m1n1 fork: `~/Projects/AsahiLinux/m4/m1n1`.

**Why:** This is a from-scratch bring-up experiment, not a simple config change. m1n1's stock T8140 codepath (which pcie.c uses for t8132) is not sufficient on j773g; each iteration exposes another missing step. Ultimate goal: get to a working NIC and run a TCP server on it.

**How to apply:** Every Phase F wedge should be treated as "we learned another gate/tunable/clock is needed" rather than "the fix isn't working". Iteration is slow (needs m1n1 reboot between runs), so make progress by adding one well-instrumented probe per commit and per-step flush so the wedge log tells us exactly what to try next.

**State as of 2026-07-11:**
- Phase F (T8140 replay in Python) runs cleanly steps 1..6.f
- Wedges on step 6.g entry #0 (first phy_ip write at 0x497040038)
- Ruled out as unblocking phy_ip: PMGR gates 150+151 ACTIVE, phy_shared CLK0/CLK1 handshake, RC-side cio3pllcore/pcieclkgen tunables, phy_common CLK MODE=ON ordering
- Under investigation: hidden PMGR gates, DART-apcie power, SMC keys other than gP0d/gP1a

**Related memories:** [[ref-m4-repos]] (repo paths + tooling)
