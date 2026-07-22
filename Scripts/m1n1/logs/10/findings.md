# RUN 10 findings

**Dispatch:** `./Scripts/m1n1/perstn-run.sh 10` (dispatcher `51262d1`).

**Hypothesis (tight-timing window):** phy_ip decode has a post-handshake
time window; pcie.c reaches the first phy_ip access microseconds after the
CLK0/CLK1 REQ/ACK handshake where the proxy replay takes seconds. RUN 10
uploaded an on-CPU AArch64 stub (ARMAsm + `p.call`) that re-ran the
idempotent shared-init tail back-to-back with `dsb sy` barriers and
immediately applied the 29 pll tunable mask-RMWs.

**Result: FALSIFIED.** The stub machinery worked mechanically end-to-end,
and the SoC wedged on the first phy_ip access anyway.

## The 6.g.stub section (nic-runtime.txt:5770-5775, end of log)

    plan: 29 entries to apply, 0 skipped (inactive/out-of-window); sizes = {'4B': 29}
    stub: 340 bytes uploaded to 0x1000cce0000 (dc_cvau + ic_ivau done)
    table: 29 entries (928 bytes) at 0x1000ccdc200; first entry target 0x497040038
    --- 6.g.stub.p.call(stub, 29 entries, tail=1) ---
      RAISED: UartTimeout: Expected 1 bytes, got 0 bytes

Line 5775 is the final line. Assembly, upload, cache maintenance all clean;
`p.call` never returned. Everything before 6.g was functionally identical
to RUN 9 (diff shows only free-running counters and Phase D timing noise).

## What RUN 10 tells us

Combined with RUNs A..9: phy_ip is decode-locked for the AP from every
state constructible via pcie.c's pre-phy_ip writes — via proxy MMIO, via
on-CPU back-to-back access with barriers, with every T8140/T8122/T602X
write applied and STUCK, and with every APCIE PMGR gate at ACTUAL=0xf.
Instruction-level and timing explanations are exhausted.

## THE REFRAME: 2026-07-11 git archaeology

Digging into why the project believed phy_ip could ever work:

1. **`d664bd9` (2026-07-11, "rework Phase E; add Phase F")** — that era's
   code states: *"After p.pcie_init() has returned (with ports stuck at
   LINKSTS_BUSY), try the LTSSM kick sequences..."* — i.e. **m1n1's full
   C-side `pcie_init()` ran to completion on this machine**, including
   per-port bring-up (pcie.c:571-852) and LTSSM attempts. The m1n1 build
   of that era (pre-`6b277bc`) did NOT apply phy-ip tunables in C.
2. **Same era, Phase F step 6.g** was
   `p.tunables_apply_local(path, "apcie-phy-ip-pll-tunables", 3)` — the
   C-side applicator — and per the surviving comment in perstn.py
   ("confirmed via phaseF.post.6.g flush + no post.6.h flush"), **it
   completed all 29 pll entries through phy_ip+0x38**. The wedge that day
   was step 6.h: the applicator wrote the auspma **port-1 slice** into the
   unpowered slice (j773g has no pci-bridge1).
3. **`6b277bc`** then moved the phy-ip tunables into the C path itself
   ("fixes t8132 LTSSM stuck at BUSY") — which made `p.pcie_init()` wedge
   C-side on the same port-1 slice bug. Hence `--no-pcie-init` became the
   replay baseline (perstn.py comment: "Current m1n1 (6b277bc) wedges here
   on j773g").

**Conclusion: phy_ip decodes after m1n1's C init has run — specifically
after parts of `pcie_init` the Python replay never executes.** The replay
gated per-port bring-up behind Phase F success, but phy_ip decode plausibly
*requires* per-port state (port PHY power-up ungating the shared PHY-IP
block). Every replay boot was structurally incapable of unlocking phy_ip.

## Secondary recon (retained as fallback)

- `apcie-cio3pllcore-tunables` (7 entries) and `apcie-pcieclkgen-tunables`
  (1 entry) — t8132-only props, never applied by any code path — have
  inferred target **rc_base (reg[1])**; every prior experiment aimed them
  at axi_sub5/axi_sub6/phy_common (NO-OPs). The ref-asahi doc's atc.c
  analogy (`phy_ip+0x38` ≈ CIO3PLL DCO_NCTRL, stalls when the PLL clock is
  off) still fits; if RUN 11 surprises, the rc_base application is the
  fallback (`--pcieclkgen-naked-apply-to=rc_base
  --cio3pllcore-naked-apply-to=rc_base`, flags already exist).
- Wrong-base hypothesis REJECTED: `phy_ip_idx=3` consistent between
  pcie_regs.py, pcie.c (regs_t8140), and the ADT (0x497040000 sz 0x20000);
  all tunable offsets fit the window.

## RUN 11 plan

Full plan in `docs/project-m4-pcie-bringup.md` (post-RUN-10 block).

**RUN 11 = fix the known-good C path and run it.** m1n1 fork commit
`b404263`: `tunables_apply_phy_ip_filtered()` in pcie.c — t8132-only,
entry-by-entry application of the phy-ip pll/auspma tunables that skips
slices of absent ports (shared < 0x8000 always applies; slice idx =
(off-0x8000)/0x8000 applies iff `pci-bridge{idx}` exists). Rebuilt macho
staged at /tmp/m4-serve (with .prepatch rollback). After kmutil
re-enrollment: `./Scripts/m1n1/perstn-run.sh 11` — full `p.pcie_init()`
(no `--no-pcie-init`, no Phase F), tier-3 post-init dumps including the
new Tier 3a phy_ip shared-window harvest, LTSSM kick.
